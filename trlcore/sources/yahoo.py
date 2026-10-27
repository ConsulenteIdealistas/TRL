"""Yahoo Finance (非公式API) から上場企業の財務数値を取得する。

クエリ文字列は Yahoo のシンボル (例: "9348.T"、"RKLB"、"ONT.L"、"IDIA.SW")。
市場ごとの接尾辞は config.build_entity_queries が entities の market から付ける。
quoteSummary の財務諸表モジュール
(四半期損益・通期損益・年次貸借対照表・四半期キャッシュフロー) と financialData から、
売上高・純利益・現預金・総資産・自己資本比率などを抽出して根拠テキスト化する。
候補は四半期ごとに1件 (source_id = ティッカー-四半期末日) で、評価時の
「関連財務根拠」プール (Store.related_financials) の材料になる。

キー・登録は不要。cookie + crumb が必要なためセッションを再利用するが、収集は
複数スレッドで並行に動くためセッションはスレッドごとに持つ (共有すると crumb の
取り直しが他スレッドの通信を壊し、取得が不安定になる)。
非公式APIのため仕様変更で壊れる可能性があるが、失敗したら空リストを返すだけで
他ソースや評価本体には影響しない (プールが空になるだけ)。
"""

import json
import random
import threading
import time
import urllib.request
from http.cookiejar import CookieJar
from datetime import date, timedelta
from ..http import http_fetch


SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
CRUMB_URL = "https://query2.finance.yahoo.com/v1/test/getcrumb"
COOKIE_URL = "https://fc.yahoo.com"
QUOTE_URL = "https://finance.yahoo.co.jp/quote/{symbol}"
MODULES = ("incomeStatementHistoryQuarterly,incomeStatementHistory,balanceSheetHistory,"
           "cashflowStatementHistoryQuarterly,financialData,quoteType,price")

# 財務諸表の報告通貨ごとの金額単位。数値は百万単位で整形するため、通貨を取り違えると
# 根拠が桁違いになる。price モジュールの currency から決め、無ければ円とみなす。
MONEY_UNITS = {"JPY": "百万円", "USD": "百万米ドル", "GBP": "百万英ポンド", "GBp": "百万英ポンド",
               "CHF": "百万スイスフラン", "EUR": "百万ユーロ",
               "CAD": "百万カナダドル", "AUD": "百万豪ドル", "SEK": "百万スウェーデンクローナ",
               "DKK": "百万デンマーククローナ"}
FINANCIAL_MARKER = "財務数値(Yahoo Finance)"

# 実在するブラウザの User-Agent 候補。bot 判定の回避のため、セッション (プロセス)
# ごとに1つをランダムに選び、そのセッション内では使い続ける (実ブラウザは
# セッション途中で UA を変えない)。失敗時は選び直して再試行する。
_UA_POOL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)

_local = threading.local()


def _sess():
    """このスレッドのセッション (opener / crumb / UA) を返す。"""
    s = getattr(_local, "session", None)
    if s is None:
        s = {"opener": None, "crumb": None, "ua": None}
        _local.session = s
    return s


def _session_ua():
    s = _sess()
    if not s["ua"]:
        s["ua"] = random.choice(_UA_POOL)
    return s["ua"]


def _fmt_money(v, unit="百万円"):
    m = v / 1_000_000
    return ("△" if m < 0 else "") + f"{abs(m):,.0f}{unit}"


def _raw(node, *keys):
    """Yahoo の {raw: 数値} 形式から数値を取り出す。0 は実値として返す。"""
    if not isinstance(node, dict):
        return None
    for k in keys:
        v = node.get(k)
        if isinstance(v, dict):
            v = v.get("raw")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
    return None


def _end_date(stmt):
    v = stmt.get("endDate") if isinstance(stmt, dict) else None
    if isinstance(v, dict):
        return v.get("fmt")
    return None


def _pl_segment(stmt, label_prefix, unit="百万円"):
    seg = []
    for label, keys in (("売上高", ("totalRevenue",)), ("営業利益", ("operatingIncome",)),
                        ("純利益", ("netIncome",))):
        v = _raw(stmt, *keys)
        if v is not None:
            seg.append(f"{label} {_fmt_money(v, unit)}")
    return f"{label_prefix} " + "、".join(seg) if seg else ""


def parse_quote_summary(data):
    """quoteSummary の JSON から (財務テキスト, 対象四半期末日, 会社名) を作る。

    数値が不足する場合は ("", None, "") を返す。
    """
    try:
        res = (data.get("quoteSummary") or {}).get("result") or []
        r = res[0] if res else {}
    except AttributeError:
        return "", None, ""
    if not isinstance(r, dict):
        return "", None, ""
    isq = ((r.get("incomeStatementHistoryQuarterly") or {}).get("incomeStatementHistory")) or []
    isa = ((r.get("incomeStatementHistory") or {}).get("incomeStatementHistory")) or []
    bs = ((r.get("balanceSheetHistory") or {}).get("balanceSheetStatements")) or []
    cfq = ((r.get("cashflowStatementHistoryQuarterly") or {}).get("cashflowStatements")) or []
    fd = r.get("financialData") or {}
    qt = r.get("quoteType") or {}
    name = qt.get("longName") or qt.get("shortName") or ""
    currency = (r.get("price") or {}).get("currency")
    unit = MONEY_UNITS.get(currency, "百万円" if not currency else f"百万{currency}")

    parts = []
    period = _end_date(isq[0]) if isq else _end_date(isa[0]) if isa else None
    if isq:
        seg = _pl_segment(isq[0], f"直近四半期({_end_date(isq[0])}まで)", unit)
        if seg:
            parts.append(seg)
    if isa:
        seg = _pl_segment(isa[0], f"通期({_end_date(isa[0])}まで)", unit)
        if seg:
            parts.append(seg)
    if bs:
        b = bs[0]
        cash, ta, eq = _raw(b, "cash"), _raw(b, "totalAssets"), _raw(b, "totalStockholderEquity")
        debts = [v for v in (_raw(b, "shortLongTermDebt"), _raw(b, "longTermDebt")) if v is not None]
        seg = []
        if cash is not None:
            seg.append(f"現金及び現金同等物 {_fmt_money(cash, unit)}")
        if ta is not None:
            seg.append(f"総資産 {_fmt_money(ta, unit)}")
        if eq is not None:
            seg.append(f"純資産 {_fmt_money(eq, unit)}")
        if debts:
            seg.append(f"有利子負債 {_fmt_money(sum(debts), unit)}")
        if ta and eq is not None:
            seg.append(f"自己資本比率 {eq / ta * 100:.1f}%")
        if seg:
            parts.append(f"貸借対照表({_end_date(b)}時点) " + "、".join(seg))
    if cfq:
        v = _raw(cfq[0], "totalCashFromOperatingActivities")
        if v is not None:
            parts.append(f"直近四半期の営業キャッシュフロー {_fmt_money(v, unit)}")
    seg = []
    tc, td, cr = _raw(fd, "totalCash"), _raw(fd, "totalDebt"), _raw(fd, "currentRatio")
    if tc is not None:
        seg.append(f"現預金 {_fmt_money(tc, unit)}")
    if td is not None:
        seg.append(f"総有利子負債 {_fmt_money(td, unit)}")
    if cr is not None:
        seg.append(f"流動比率 {cr:.2f}")
    if seg:
        parts.append("最新指標 " + "、".join(seg))
    if len(parts) < 2 or not period:
        return "", None, name
    return "、".join(parts) + "。", period, name


def _ensure_crumb(fetch):
    s = _sess()
    if s["crumb"]:
        return s["crumb"]
    headers = {"User-Agent": _session_ua()}
    try:
        fetch(COOKIE_URL, headers=headers)
    except Exception:
        pass  # エラー応答でも cookie は付くことがある。そのまま crumb 取得に進む
    raw = fetch(CRUMB_URL, headers=headers)
    crumb = raw.decode("utf-8", "replace").strip() if isinstance(raw, (bytes, bytearray)) else str(raw).strip()
    if not crumb or "<" in crumb or len(crumb) > 40:
        raise ValueError("crumb の取得に失敗しました")
    s["crumb"] = crumb
    return crumb


def yahoo_fetch(query, max_results, lookback_days, as_of, http=None, sleep=time.sleep):
    symbol = str(query).strip().upper()
    if not symbol:
        return []
    ticker = symbol.split(".")[0]  # 表示と source_id 用 (接尾辞なしのティッカー)
    s = _sess()
    if http is None:
        if s["opener"] is None:
            s["opener"] = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(CookieJar()))
        opener = s["opener"]

        def fetch(url, params=None, headers=None):
            return http_fetch(url, params=params, headers=headers, opener=opener.open)
    else:
        def fetch(url, params=None, headers=None):
            return http(url, params=params, headers=headers)

    for attempt in (0, 1):
        try:
            crumb = _ensure_crumb(fetch)
            raw = fetch(SUMMARY_URL.format(symbol=symbol),
                        params={"modules": MODULES, "crumb": crumb},
                        headers={"User-Agent": _session_ua()})
            data = json.loads(raw)
            break
        except Exception:
            # crumb 失効や bot 判定の可能性があるため、次回は crumb と UA を取り直す
            s["crumb"] = None
            s["ua"] = None
            if attempt == 1:
                return []
    text, period, name = parse_quote_summary(data)
    if not text or not period:
        return []
    try:
        period_date = date.fromisoformat(period)
    except ValueError:
        return []
    if period_date < as_of - timedelta(days=lookback_days) or period_date > as_of + timedelta(days=92):
        return []  # 古すぎる/未来の決算期はプール材料にしない
    sleep(0.3)
    return [{
        "source": "yahoo", "source_id": f"{ticker}-{period}",
        "title": f"{name or ticker} 財務情報({period} 四半期)",
        "abstract": f"{name or ticker}({ticker})の{FINANCIAL_MARKER}: {text}",
        "url": QUOTE_URL.format(symbol=symbol),
        "published": period,
        "fields": ["Yahoo Finance", "財務"],
        "affiliations": [name] if name else [],
        "info_type": "disclosure",
    }]
