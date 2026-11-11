"""filings.xbrl.org (ESEF / UKSEF の年次報告) から財務数値を取得する。

役割は欧州公式開示による年次財務の裏取り・穴埋め (2026-11-03 の LEI
スイープで EU+英国 119 社の LEI を確定済み。辞書の lei フィールドに登録)。
EDGAR (米国) ・ EDINET (日本) と同じ「公式開示の財務数値」の位置づけで、
取得した数値は評価時の「関連財務根拠」プール (Store.related_financials)
の材料になる。対象は辞書に LEI を持つコア企業で、統合 (対象選定・取得
間隔・記録) は pipeline の _collect_esef が担う (ROIC 補完と同じ後段
バッチの型)。キー・登録は不要。

API の要点 (2026-11-03 のスイープと 2026-11-10 の実装時確認):
- 提出一覧は GET /api/entities/<LEI>/filings (Accept:
  application/vnd.api+json)。属性に period_end と json_url (xBRL-JSON の
  相対パス) が入る
- xBRL-JSON (OIM) の facts は dimensions (concept / entity / period /
  unit) を持つ。期間は "2025-01-01T00:00:00/2026-01-01T00:00:00" の
  ように翌日 0 時で表されるため、実日付は -1 日して読む。追加の次元
  (資本変動の内訳等) を持つ fact は集計値ではないので除外する
- xBRL-JSON の取得に vnd.api+json の Accept を付けると 406 になるため、
  ファイル取得では付けない
- 同一 period_end に言語違いのファイルが複数ある場合は英語版
  (-en.json) を優先する
- fxo はベストエフォート収録で、事業体自体が無い企業 (ドイツ等に多い)
  は一覧が 404 または 0 件になる。これは材料なしとして空で返し、実行
  記録 (run_sources) に fetched=0 で残る (静かな 0 件の検知に乗る)
"""

import json
import os
import threading
import time
from datetime import date, timedelta

from ..http import http_fetch, HttpError

API_BASE = "https://filings.xbrl.org"
FINANCIAL_MARKER = "財務数値(ESEF XBRL)"
USER_AGENT = "ConsulenteIdealistas TRL (esef collector)"

# fxo に公開された上限は無い。下調べのスイープで使った 0.3 秒間隔に
# 倣い、EDGAR の _throttle と同じ形でプロセス全体を直列化する
MIN_INTERVAL_SEC = 0.35
_rate_lock = threading.Lock()
_last_call = [0.0]


def esef_enabled():
    """ESEF 収集の有効条件。キー不要のため本番では常時有効だが、実辞書を
    使うテストが実ネットワークへ出ないよう、無効化スイッチだけ用意する
    (ROIC の「キーがある場合だけ有効」と同じく、収集の後段バッチを
    テスト環境で切り離すためのゲート)。"""
    return not os.environ.get("TRL_ESEF_DISABLE")


def _throttle(sleep=time.sleep, now=time.monotonic):
    with _rate_lock:
        wait = MIN_INTERVAL_SEC - (now() - _last_call[0])
        if wait > 0:
            sleep(wait)
        _last_call[0] = now()


def _get(url, http, api=False):
    _throttle()
    fetch = http or http_fetch
    headers = {"User-Agent": USER_AGENT}
    if api:
        headers["Accept"] = "application/vnd.api+json"
    raw = fetch(url, headers=headers)
    return json.loads(raw)


# (表示名, ifrs-full 概念名の候補)。EDGAR の _IFRS_FALLBACK と同じ項目立て
_FLOW_ITEMS = (("売上高", ("Revenue", "RevenueFromContractsWithCustomers")),
               ("営業利益", ("ProfitLossFromOperatingActivities",)),
               ("純利益", ("ProfitLoss", "ProfitLossAttributableToOwnersOfParent")),
               ("営業キャッシュフロー", ("CashFlowsFromUsedInOperatingActivities",)))
_INSTANT_ITEMS = (("現金及び現金同等物", ("CashAndCashEquivalents",)),
                  ("総資産", ("Assets",)),
                  ("純資産", ("Equity", "EquityAttributableToOwnersOfParent")))

_CURRENCY_UNITS = {"USD": "百万米ドル", "EUR": "百万ユーロ", "GBP": "百万英ポンド",
                   "CHF": "百万スイスフラン", "CAD": "百万カナダドル", "AUD": "百万豪ドル",
                   "JPY": "百万円", "SEK": "百万スウェーデンクローナ",
                   "DKK": "百万デンマーククローネ", "NOK": "百万ノルウェークローネ"}


def _fmt_money(v, unit):
    m = v / 1_000_000
    return ("△" if m < 0 else "") + f"{abs(m):,.0f}{unit}"


def _day(s):
    """OIM の時刻表現 → 実日付。期間末は翌日 0 時で表されるため -1 日する。"""
    try:
        return date.fromisoformat(str(s)[:10]) - timedelta(days=1)
    except ValueError:
        return None


def _core_facts(doc):
    """xBRL-JSON から集計 fact だけを (概念, 種別, 終了日, 通貨, 値) で返す。

    追加次元を持つ fact (セグメント内訳・資本変動の軸など) は集計値では
    ないため除外する。同一 (概念, 期間, 通貨) の重複は同値の再掲として
    最初のものを採る。
    """
    out, seen = [], set()
    for f in ((doc or {}).get("facts") or {}).values():
        if not isinstance(f, dict):
            continue
        dims = f.get("dimensions") or {}
        if set(dims) - {"concept", "entity", "period", "unit"}:
            continue
        concept = str(dims.get("concept") or "")
        if not concept.startswith("ifrs-full:"):
            continue
        unit = str(dims.get("unit") or "")
        if not unit.startswith("iso4217:"):
            continue
        try:
            value = float(f.get("value"))
        except (TypeError, ValueError):
            continue
        period = str(dims.get("period") or "")
        if "/" in period:
            start, end = (_day(p) for p in period.split("/", 1))
            if start is None or end is None:
                continue
            key = (concept, period, unit)
            row = (concept[len("ifrs-full:"):], "flow", end, (end - start).days, unit[8:], value)
        else:
            end = _day(period)
            if end is None:
                continue
            key = (concept, period, unit)
            row = (concept[len("ifrs-full:"):], "instant", end, 0, unit[8:], value)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _pick_at(facts, concepts, kind, end):
    """終了日 end に揃えた項目の選択。概念候補の順に、その日付の行を採る
    (flow は年次 = 300〜400 日の期間のみ。重複は facts の並び順の先勝ち)。"""
    for c in concepts:
        for r in facts:
            if r[0] == c and r[1] == kind and r[2] == end \
                    and (kind == "instant" or 300 <= r[3] <= 400):
                return r
    return None


def parse_xbrl_json_full(doc):
    """xBRL-JSON → (財務テキスト, 対象期間末日, 完全性情報)。年次 flow が
    無ければ ("", None, info)。

    完全性情報 (S-4) は {"found": [項目名], "missing": [項目名],
    "currencies": [通貨], "instant_period": iso|None}。項目は期間終了日
    ごとに混在させず (従来は概念ごとに独立に最新を拾い、期の違う項目が
    1つの財務セットに混ざり得た)、flow は年次 flow のある最新の通期
    終了日の行だけで、instant は flow と同じ終了日 (無ければ最新の
    instant の日付) の行だけでセットを組む。自己資本比率は同一日付・
    同一通貨の純資産と総資産のペアからのみ計算する。欠落項目と通貨の
    混在はテキスト末尾に明記し、部分抽出が「完全な財務」に見えない
    ようにする。"""
    facts = _core_facts(doc)
    info = {"found": [], "missing": [], "currencies": [], "instant_period": None}
    labels = [label for label, _ in _FLOW_ITEMS + _INSTANT_ITEMS]
    flow_ends = [r[2] for r in facts if r[1] == "flow" and 300 <= r[3] <= 400]
    if not flow_ends:
        info["missing"] = labels
        return "", None, info
    period = max(flow_ends)
    flows = {}
    for label, concepts in _FLOW_ITEMS:
        got = _pick_at(facts, concepts, "flow", period)
        if got:
            flows[label] = got
    instants = {}
    instant_rows = [r for r in facts if r[1] == "instant"]
    iend = None
    if any(r[2] == period for r in instant_rows):
        iend = period
    elif instant_rows:
        iend = max(r[2] for r in instant_rows)
    if iend is not None:
        info["instant_period"] = iend.isoformat()
        for label, concepts in _INSTANT_ITEMS:
            got = _pick_at(facts, concepts, "instant", iend)
            if got:
                instants[label] = got
    currencies = sorted({r[4] for r in list(flows.values()) + list(instants.values())})
    info["currencies"] = currencies
    for label in labels:
        info["found" if (label in flows or label in instants) else "missing"].append(label)

    def money(row):
        # 通貨は項目ごとに自分のものを引く (混在時は混在と分かる形になる)
        return _fmt_money(row[5], _CURRENCY_UNITS.get(row[4], f"百万{row[4]}"))

    text = f"直近通期({period.isoformat()}まで) " + "、".join(
        f"{label} {money(flows[label])}" for label, _ in _FLOW_ITEMS if label in flows)
    if instants:
        seg = [f"{label} {money(instants[label])}"
               for label, _ in _INSTANT_ITEMS if label in instants]
        eq, ta = instants.get("純資産"), instants.get("総資産")
        if eq and ta and ta[5] and eq[4] == ta[4]:
            seg.append(f"自己資本比率 {eq[5] / ta[5] * 100:.1f}%")
        text += f"。貸借対照表({iend.isoformat()}時点) " + "、".join(seg)
    text += "。"
    if info["missing"]:
        text += f"欠落項目: {'・'.join(info['missing'])}。"
    if len(currencies) > 1:
        text += f"通貨混在: {'・'.join(currencies)}。"
    return text, period, info


def parse_xbrl_json(doc):
    """互換用の薄いラッパ: (財務テキスト, 対象期間末日)。"""
    text, period, _info = parse_xbrl_json_full(doc)
    return text, period


def _filing_pref(attrs):
    """同一 period_end 内の優先度: 英語版 → 追加日時の新しいもの。"""
    return (1 if str(attrs.get("json_url") or "").endswith("-en.json") else 0,
            str(attrs.get("date_added") or ""))


def fetch_esef(query, max_results, lookback_days, as_of, http=None):
    """query は LEI。提出一覧から直近 2 期分 (財務プールの窓に合う分だけ) を
    期間ごとのレコードにして返す。fxo に事業体が無い (404) 場合は材料なし。"""
    lei = str(query or "").strip().upper()
    if not lei:
        return []
    try:
        data = _get(f"{API_BASE}/api/entities/{lei}/filings?page[size]=100", http, api=True)
    except HttpError as e:
        if e.status == 404:
            return []  # fxo 未収録の事業体 = 材料なし (EDGAR の登録なしと同じ扱い)
        raise  # 通信失敗は空で握りつぶさず収集側に記録させる (他ソースと同じ)
    by_period = {}
    for row in (data or {}).get("data") or []:
        a = row.get("attributes") or {}
        pe = a.get("period_end")
        if not pe or not a.get("json_url"):
            continue
        cur = by_period.get(pe)
        if cur is None or _filing_pref(a) > _filing_pref(cur):
            by_period[pe] = a
    out = []
    for pe in sorted(by_period, reverse=True):
        if len(out) >= min(max_results, 2):
            break
        try:
            period_date = date.fromisoformat(pe)
        except ValueError:
            continue
        if period_date < as_of - timedelta(days=lookback_days) or period_date > as_of + timedelta(days=92):
            continue
        doc = _get(API_BASE + by_period[pe]["json_url"], http)
        text, _end, info = parse_xbrl_json_full(doc)
        if not text:
            continue
        out.append({
            "source": "esef", "source_id": f"{lei}-{pe}",
            "title": f"{lei} 財務情報({pe} 通期)",
            "abstract": f"{lei}の{FINANCIAL_MARKER}: {text}",
            "url": API_BASE + by_period[pe]["json_url"],
            "published": pe,
            "fields": ["ESEF", "財務"],
            "affiliations": [],
            "info_type": "disclosure",
            # 完全性情報 (S-4): どの項目が取れて何が欠けたか。normalize は
            # 固定フィールドだけを通すので、観測 (収集側の counts) 用に
            # raw のまま載せる
            "completeness": {"found": info["found"], "missing": info["missing"],
                             "currencies": info["currencies"],
                             "instant_period": info["instant_period"]},
        })
    return out
