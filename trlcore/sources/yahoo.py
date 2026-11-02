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
非公式APIのため仕様変更で壊れる可能性がある。通信が2回とも失敗した場合は
空リストで握りつぶさず例外として収集側に記録させる (他ソースや評価本体には
影響しない)。応答は取れたが数値が無い場合は材料なしとして空リストを返す。

429 (レート制限) への対処: http_fetch 内の再試行 (Retry-After 尊重・
5→15→30秒) をすり抜けて 429 が上がってきたら、制限がかかっている時間帯に
全スレッドが個別に叩き続けないよう、プロセス全体で COOLDOWN_SEC 秒の
休止を共有する (以降の全リクエストは休止明けまで待つ)。429 のクエリは
この層では再試行しない (下層で既に3回試しており、二重に燃やさない)。
あわせて、実行開始直後に全スレッドのセッション確立 (cookie + crumb) が
一斉に集中しないよう、スレッドごとに初回だけ 0〜3秒のジッターを入れる。
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
# 根拠が桁違いになる。price モジュールの currency から決め、取れない場合はティッカーの
# 市場サフィックスから推定する。どちらでも決まらなければ、通貨を偽って記録するより
# その銘柄の財務は作らない (通貨不明のまま円扱いする旧仕様の廃止)。
MONEY_UNITS = {"JPY": "百万円", "USD": "百万米ドル", "GBP": "百万英ポンド", "GBp": "百万英ポンド",
               "CHF": "百万スイスフラン", "EUR": "百万ユーロ",
               "CAD": "百万カナダドル", "AUD": "百万豪ドル", "SEK": "百万スウェーデンクローナ",
               "DKK": "百万デンマーククローナ",
               "NOK": "百万ノルウェークローナ"}
FINANCIAL_MARKER = "財務数値(Yahoo Finance)"

# ティッカーの市場サフィックス→通貨 (price.currency が取れないときの推定用)。
# サフィックス無しは米国市場として USD とみなす。
SUFFIX_CURRENCIES = {"": "USD", "T": "JPY", "L": "GBp", "DE": "EUR", "PA": "EUR", "AS": "EUR",
                     "BR": "EUR", "MI": "EUR", "MC": "EUR", "HE": "EUR", "CO": "DKK", "ST": "SEK",
                     "OL": "NOK", "SW": "CHF", "AX": "AUD", "TO": "CAD"}

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

_rate_lock = threading.Lock()
_last_call = [0.0]

_cooldown_lock = threading.Lock()
_cooldown_until = [0.0]  # monotonic 時刻。429 観測時に全体で共有する休止の期限
COOLDOWN_SEC = 60.0


def _note_429(retry_after=None, now=time.monotonic):
    """429 を観測したら、全体の休止期限を延ばす (短くはしない)。"""
    until = now() + max(COOLDOWN_SEC, retry_after or 0.0)
    with _cooldown_lock:
        if until > _cooldown_until[0]:
            _cooldown_until[0] = until


def _wait_cooldown(sleep, now=time.monotonic):
    """全体休止が明けるまで待つ。"""
    with _cooldown_lock:
        remain = _cooldown_until[0] - now()
    if remain > 0:
        sleep(remain)
# Yahoo 向けリクエストのプロセス全体の最小間隔 (秒)。収集は複数スレッドで並行に
# 動くため、クエリ単位の sleep ではバーストを抑えられず、非公式 API の制限に
# 触れやすい。PubMed の全体スロットル (2026-10-29) と同じ方式で全リクエストを直列化する。
MIN_INTERVAL = 1.0


def _throttle(interval, sleep=time.sleep, now=time.monotonic):
    with _rate_lock:
        wait = interval - (now() - _last_call[0])
        if wait > 0:
            sleep(wait)
        _last_call[0] = now()


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


def parse_quote_summary(data, symbol=None):
    """quoteSummary の JSON から (財務テキスト, 対象四半期末日, 会社名) を作る。

    数値が不足する場合と、通貨が特定できない場合は ("", None, "") を返す。
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
    if not currency and symbol:
        suffix = symbol.rsplit(".", 1)[1] if "." in symbol else ""
        currency = SUFFIX_CURRENCIES.get(suffix)
    if not currency:
        return "", None, ""  # 通貨が特定できない財務は、単位を偽って記録しない
    unit = MONEY_UNITS.get(currency, f"百万{currency}")

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

    _inner_fetch = fetch

    def fetch(url, params=None, headers=None):
        # cookie / crumb / quoteSummary の全リクエストを、429 の全体休止と
        # 全体スロットルに通す
        _wait_cooldown(sleep)
        _throttle(MIN_INTERVAL, sleep=sleep)
        return _inner_fetch(url, params=params, headers=headers)

    if not s.get("staggered"):
        # セッション確立 (cookie + crumb) が全スレッドで同時刻に集中しないよう、
        # スレッドの初回だけ開始をずらす
        s["staggered"] = True
        sleep(random.uniform(0, 3))

    for attempt in (0, 1):
        try:
            crumb = _ensure_crumb(fetch)
            raw = fetch(SUMMARY_URL.format(symbol=symbol),
                        params={"modules": MODULES, "crumb": crumb},
                        headers={"User-Agent": _session_ua()})
            data = json.loads(raw)
            break
        except Exception as e:
            # crumb 失効や bot 判定の可能性があるため、次回は crumb と UA を取り直す
            s["crumb"] = None
            s["ua"] = None
            if getattr(e, "status", None) == 429:
                # http_fetch が既に Retry-After 尊重で3回試した後の 429。
                # ここで二重に再試行せず、全体休止を共有して即座に失敗として残す
                _note_429(getattr(e, "retry_after", None))
                raise
            if attempt == 1:
                # 恒常的な失敗 (API仕様変更等) が無言にならないよう、空結果で
                # 握りつぶさず例外として収集側 (run_sources / sources_failed) に残す
                raise
            sleep(2)  # 一時的な制限・bot 判定を避けるため間を置いて取り直す
    text, period, name = parse_quote_summary(data, symbol)
    if not text or not period:
        return []
    try:
        period_date = date.fromisoformat(period)
    except ValueError:
        return []
    if period_date < as_of - timedelta(days=lookback_days) or period_date > as_of + timedelta(days=92):
        return []  # 古すぎる/未来の決算期はプール材料にしない
    return [{
        # source_id は市場接尾辞つきシンボルにする (接尾辞なしだと NANO.TO と NANO.PA の
        # ように市場違いの同名ティッカーで重複排除キーが衝突し、片方の財務が消える)
        "source": "yahoo", "source_id": f"{symbol}-{period}",
        "title": f"{name or ticker} 財務情報({period} 四半期)",
        "abstract": f"{name or ticker}({ticker})の{FINANCIAL_MARKER}: {text}",
        "url": QUOTE_URL.format(symbol=symbol),
        "published": period,
        "fields": ["Yahoo Finance", "財務"],
        "affiliations": [name] if name else [],
        "info_type": "disclosure",
    }]
