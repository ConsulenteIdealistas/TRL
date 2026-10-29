"""SEC EDGAR (米証券取引委員会) の company facts API から財務数値を取得する。

クエリ文字列はティッカー (例: "RKLB")。company_tickers.json で CIK に解決し、
companyfacts (XBRL の構造化財務データ) から売上高・純利益・現預金・総資産などを
抽出して根拠テキスト化する。候補は直近四半期 (無ければ通期) ごとに1件で、
評価時の「関連財務根拠」プール (Store.related_financials) の材料になる。
Yahoo Finance (非公式API) と同じ米上場企業を、公式データで裏取り・代替する位置づけ。

キー・登録は不要。SEC の fair access 方針に従い User-Agent を明示し、
リクエスト間隔を空ける (上限 10 req/s に対して余裕を持たせる)。
User-Agent は「運用者名 + 連絡先メール」の形式が必須で (形式を満たさないと
403 になる)、連絡先は環境変数 TRL_EDGAR_CONTACT で上書きできる (公開リポジトリに
個人メールを書かないため)。未設定時は SEC のドキュメント例示形式を使う。
失敗したら空リストを返すだけで、他ソースや評価本体には影響しない。
"""

import json
import os
import sys
import threading
import time
from datetime import date, timedelta
from ..http import http_fetch, redact


TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
BROWSE_URL = "https://www.sec.gov/edgar/browse/?CIK={cik}"
USER_AGENT = "ConsulenteIdealistas TRL " + (os.environ.get("TRL_EDGAR_CONTACT") or "contact@example.com")
FINANCIAL_MARKER = "財務数値(SEC EDGAR)"

# (表示名, us-gaap 概念名の候補, 種別) 種別: flow=期間の損益系 / instant=時点の貸借系
_FLOW_ITEMS = [
    ("売上高", ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet")),
    ("営業利益", ("OperatingIncomeLoss",)),
    ("純利益", ("NetIncomeLoss",)),
    ("研究開発費", ("ResearchAndDevelopmentExpense",)),
    ("営業キャッシュフロー", ("NetCashProvidedByUsedInOperatingActivities",)),
]
_INSTANT_ITEMS = [
    ("現金及び現金同等物", ("CashCashEquivalentsAndShortTermInvestments", "CashAndCashEquivalentsAtCarryingValue")),
    ("総資産", ("Assets",)),
    ("純資産", ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")),
]
_IFRS_FALLBACK = {  # 20-F 等の IFRS 報告 (us-gaap が無い外国企業) 向けの読み替え
    "売上高": ("Revenue",),
    "純利益": ("ProfitLoss",),
    "現金及び現金同等物": ("CashAndCashEquivalents",),
    "総資産": ("Assets",),
    "純資産": ("Equity",),
}

_tickers_cache = None
_tickers_error = None
_rate_lock = threading.Lock()
_last_call = [0.0]


def _throttle(sleep=time.sleep, now=time.monotonic):
    """SEC の上限 (10 req/s) に余裕を持たせて全体で間引きする。"""
    with _rate_lock:
        wait = 0.15 - (now() - _last_call[0])
        if wait > 0:
            sleep(wait)
        _last_call[0] = now()


def _get(url, http):
    _throttle()
    fetch = http or http_fetch
    raw = fetch(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    return json.loads(raw)


def ticker_map(http=None):
    """ティッカー → (CIK, 会社名)。プロセス内キャッシュ (週次実行で1回だけ取得)。"""
    global _tickers_cache, _tickers_error
    if _tickers_cache is not None:
        return _tickers_cache
    if _tickers_error is not None:
        raise _tickers_error  # 取得失敗を全社分リトライしない (実行内のみ有効)
    try:
        data = _get(TICKERS_URL, http)
        m = {}
        for row in (data or {}).values():
            t = str(row.get("ticker") or "").upper()
            if t and row.get("cik_str") is not None:
                m[t] = (int(row["cik_str"]), row.get("title") or t)
        _tickers_cache = m
    except Exception as e:
        _tickers_error = e
        raise
    return _tickers_cache


_CURRENCY_UNITS = {"USD": "百万米ドル", "EUR": "百万ユーロ", "GBP": "百万英ポンド",
                   "CHF": "百万スイスフラン", "CAD": "百万カナダドル", "AUD": "百万豪ドル"}


def _fmt_money(v, unit="百万米ドル"):
    m = v / 1_000_000
    return ("△" if m < 0 else "") + f"{abs(m):,.0f}{unit}"


def _series(taxonomy_facts, concept, currency="USD"):
    node = (taxonomy_facts or {}).get(concept) or {}
    units = node.get("units") or {}
    rows = units.get(currency) or []
    return [r for r in rows if isinstance(r.get("val"), (int, float)) and r.get("end")]


def _detect_currency(gaap, ifrs):
    """報告通貨を決める。全概念の単位を走査して候補を集め、優先順位で選ぶ。
    概念の走査順に依存すると、混在ケースで先に当たった概念の通貨に全体が引っ張られる。"""
    found = set()
    concepts = [c for _, cs in _FLOW_ITEMS + _INSTANT_ITEMS for c in cs]
    concepts += [c for cs in _IFRS_FALLBACK.values() for c in cs]
    for tax in (gaap, ifrs):
        for concept in concepts:
            for u in ((tax.get(concept) or {}).get("units") or {}):
                if len(u) == 3 and u.isupper():
                    found.add(u)
    for u in ("USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD"):
        if u in found:
            return u
    return sorted(found)[0] if found else "USD"


def _latest(rows, kind, as_of):
    """flow は期間の長さで四半期 (<=130日) / 通期 (>=300日) を区別し、同 end では
    filed が最新の (訂正後の) 値を採る。instant は最新の end を採る。"""
    best = {}
    for r in rows:
        try:
            end = date.fromisoformat(r["end"])
        except (KeyError, ValueError):
            continue
        if end > as_of + timedelta(days=92):
            continue
        if kind == "flow":
            try:
                start = date.fromisoformat(r["start"])
            except (KeyError, ValueError):
                continue
            days = (end - start).days
            # 131〜299日は期首累計 (YTD)。10-Q の営業CF等はこの形でしか出ないため、
            # 落とさず累計として保持する
            bucket = "q" if days <= 130 else "fy" if days >= 300 else "ytd"
            key = (bucket, end)
        else:
            key = ("i", end)
        prev = best.get(key)
        if prev is None or str(r.get("filed") or "") >= str(prev.get("filed") or ""):
            best[key] = r
    if kind == "flow":
        out = {}
        for bucket in ("q", "fy", "ytd"):
            ends = [e for (b, e) in best if b == bucket]
            if ends:
                out[bucket] = best[(bucket, max(ends))]
        return out
    ends = [e for (_, e) in best]
    return best[("i", max(ends))] if ends else None


def parse_company_facts(data, as_of):
    """companyfacts JSON から (財務テキスト, 対象期間末日, 期間種別) を作る。

    数値が不足する場合は ("", None, "") を返す。"""
    facts = (data or {}).get("facts") or {}
    gaap = facts.get("us-gaap") or {}
    ifrs = facts.get("ifrs-full") or {}
    currency = _detect_currency(gaap, ifrs)
    unit = _CURRENCY_UNITS.get(currency, "百万米ドル")
    flows, instants = {}, {}
    for label, concepts in _FLOW_ITEMS:
        rows = []
        for c in concepts:
            rows = _series(gaap, c, currency)
            if rows:
                break
        if not rows and label in _IFRS_FALLBACK:
            for c in _IFRS_FALLBACK[label]:
                rows = _series(ifrs, c, currency)
                if rows:
                    break
        if rows:
            flows[label] = _latest(rows, "flow", as_of)
    for label, concepts in _INSTANT_ITEMS:
        rows = []
        for c in concepts:
            rows = _series(gaap, c, currency)
            if rows:
                break
        if not rows and label in _IFRS_FALLBACK:
            for c in _IFRS_FALLBACK[label]:
                rows = _series(ifrs, c, currency)
                if rows:
                    break
        if rows:
            got = _latest(rows, "instant", as_of)
            if got:
                instants[label] = got
    q = {k: v["q"] for k, v in flows.items() if v.get("q")}
    fy = {k: v["fy"] for k, v in flows.items() if v.get("fy")}
    ytd = {k: v["ytd"] for k, v in flows.items() if v.get("ytd")}
    main = q or fy
    if not main:
        return "", None, ""
    kind = "四半期" if q else "通期"
    period = max(r["end"] for r in main.values())
    parts = []
    for label, concepts in _FLOW_ITEMS:
        if label in main:
            parts.append(f"{label} {_fmt_money(main[label]['val'], unit)}")
    if "営業キャッシュフロー" not in main and "営業キャッシュフロー" in ytd:
        # 四半期の営業CFは累計 (期首からの合計) でしか開示されないことが多い
        parts.append(f"営業キャッシュフロー(期首累計) {_fmt_money(ytd['営業キャッシュフロー']['val'], unit)}")
    text = f"直近{kind}({period}まで) " + "、".join(parts)
    if fy and q:
        seg = [f"{label} {_fmt_money(fy[label]['val'], unit)}" for label, _ in _FLOW_ITEMS
               if label in fy and label in ("売上高", "純利益", "営業キャッシュフロー")]
        if seg:
            text += f"。通期({max(r['end'] for r in fy.values())}まで) " + "、".join(seg)
    if instants:
        seg = [f"{label} {_fmt_money(instants[label]['val'], unit)}" for label, _ in _INSTANT_ITEMS if label in instants]
        # 現金が狭義の概念 (短期投資を含まない) で取れた場合は、短期有価証券を別掲して
        # 流動性の過小表示を防ぐ (広義の現金概念が使えた場合は二重計上になるので出さない)
        cash_node = (gaap.get("CashAndCashEquivalentsAtCarryingValue") or {})
        if instants.get("現金及び現金同等物") and cash_node.get("units"):
            for c in ("MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent", "ShortTermInvestments"):
                rows = _series(gaap, c, currency)
                if rows:
                    got = _latest(rows, "instant", as_of)
                    if got:
                        seg.append(f"短期有価証券 {_fmt_money(got['val'], unit)}")
                    break
        eq, ta = instants.get("純資産"), instants.get("総資産")
        if eq and ta and ta["val"]:
            seg.append(f"自己資本比率 {eq['val'] / ta['val'] * 100:.1f}%")
        iend = max(r["end"] for r in instants.values())
        text += f"。貸借対照表({iend}時点) " + "、".join(seg)
    return text + "。", period, kind


def edgar_fetch(query, max_results, lookback_days, as_of, http=None):
    ticker = str(query).strip().upper()
    if not ticker:
        return []
    try:
        hit = ticker_map(http).get(ticker)
        if not hit:
            return []  # SEC 登録が無い (未上場・登録外) なら材料なし
        cik, name = hit
        facts = _get(FACTS_URL.format(cik=cik), http)
    except Exception as e:
        print(f"edgar fetch failed for {ticker}: "
              f"{redact(f'{type(e).__name__}: {e}')}", file=sys.stderr)
        return []
    text, period, kind = parse_company_facts(facts, as_of)
    if not text or not period:
        return []
    try:
        period_date = date.fromisoformat(period)
    except ValueError:
        return []
    if period_date < as_of - timedelta(days=lookback_days) or period_date > as_of + timedelta(days=92):
        return []
    disp = facts.get("entityName") or name
    return [{
        "source": "edgar", "source_id": f"{ticker}-{period}",
        "title": f"{disp} 財務情報({period} {kind})",
        "abstract": f"{disp}({ticker})の{FINANCIAL_MARKER}: {text}",
        "url": BROWSE_URL.format(cik=cik),
        "published": period,
        "fields": ["SEC EDGAR", "財務"],
        "affiliations": [disp] if disp else [],
        "info_type": "disclosure",
    }]
