"""Alpha Vantage の財務データ API から四半期財務諸表を取得する。

役割は財務補完 (2026-11-04 採用の財務補間3社併用の第3弾・最終弾):
SimFin のカバー圏外 (US / DE / CA 以外) の企業について、四半期財務を
25回/日の無料枠で少しずつ補う低速キュー。調査の正本は
~/workspace/research_notes/free-finance-data-apis-20261103-1531/report.md
§3 / §13 (2026-11-03 時点の提供元ページ確認):
- 無料枠は 25 リクエスト/日 (2023 年に 500回/日から 1/20 へ削減された
  実績がある。枠前提の設計にしない)。分単位の上限も併記されて
  いるため、間隔は ROIC と同じ 5回/分の前提で置く
- 3表 (INCOME_STATEMENT / BALANCE_SHEET / CASH_FLOW) は無料枠で使え、
  年次・四半期の両方を 1シンボル/1リクエストで返す。1社の取得は
  3リクエストを使う
- 非米国シンボルは取引所接尾辞つき (例: .LON / .TYO。提供元ドキュメント
  と利用実例で確認)。ただし非米国の小・中型株で実際に諸表が返るかは
  未検証 (report の Could-not-verify)。接尾辞が間違っていてシンボルが
  提供元に無い場合は Error Message が返り、例外として上げて run_sources
  に失敗 (ok=0) として記録する (成功・0件で握りつぶすと、休止判定と
  静かな0件の検知の両方から消えてしまう。ISSUES-2 の S-3)。live の
  結果を見て接尾辞を直せる (ROIC の LON→LSE と同じ運用)

枠の使い方 (恣意性を置かないための導出):
- 1日25回 ÷ 1社3リクエスト = 8社/日が上限。9社目 (25〜27回目) は
  その日の枠を超えるため、1実行の上限も「その日の残枠で完走できる
  社数」= (25 − 当日消費済み) // 3 とし、最大で 8社になる。実行は
  週4回のため、週あたり最大でも 8社×4回分がこの経路で進む
- 当日消費済みの数え方は既存の記録 (run_sources) に寄せる: 当日に
  記録された自ソースの試行行 × 3リクエスト。失敗した試行も枠は
  消費しているため数に含める (枠側に倒した保守的な数え方)。
  新しい永続化は作らない (pipeline の _collect_alphavantage が
  store.source_attempts_on で読む)

統合 (対象選定・休止・記録・枠の消化) は pipeline の
_collect_alphavantage が担う。休止の規則は ROIC / SimFin と同じ:
成功 (1件以上取得) したときだけ 30日休む。
"""

import json
import os
import threading
import time
from datetime import date, timedelta

from ..http import http_fetch, HttpError

API_BASE = "https://www.alphavantage.co/query"
FINANCIAL_MARKER = "財務数値(Alpha Vantage)"

# 無料枠 (提供元 support ページ、2026-11-03 確認): 25 リクエスト/日
DAILY_REQUEST_LIMIT = 25
# 取得する3表。1社 = 3リクエスト (report §3: 1シンボル/1リクエスト)
STATEMENT_FUNCTIONS = ("INCOME_STATEMENT", "BALANCE_SHEET", "CASH_FLOW")
REQUESTS_PER_COMPANY = len(STATEMENT_FUNCTIONS)
# 1日に完走できる社数の上限は枠から決まる (25 // 3 = 8社 = 24リクエスト。
# 余る1回では1社分を完走できないため使わない)
MAX_COMPANIES_PER_DAY = DAILY_REQUEST_LIMIT // REQUESTS_PER_COMPANY

# 分単位の上限は report §14 に併記がある。数値は無料枠の旧来値 5回/分を
# 前提に、ROIC と同じ導出で 12.5 秒間隔にする (どの60秒窓でも5回を
# 超えない)。プロセス全体を直列化する形も ROIC / EDGAR と同じ
MIN_INTERVAL_SEC = 12.5
_rate_lock = threading.Lock()
_last_call = [0.0]


def _throttle(sleep=time.sleep, now=time.monotonic):
    with _rate_lock:
        wait = MIN_INTERVAL_SEC - (now() - _last_call[0])
        if wait > 0:
            sleep(wait)
        _last_call[0] = now()


class RateLimitError(Exception):
    """無料枠の超過 (25回/日 等)。HTTP 200 の本文で通知される。
    呼び出し側はこの社の試行を失敗として記録し、その日の残りのキューを
    止める (枠が回復しない当日中に聞き直しても枠を食うだけのため)。"""


class _SymbolError(Exception):
    """シンボルが提供元に無い (Error Message)。損益表で起きた場合は
    握りつぶさず例外のまま上げ、呼び出し側で失敗 (ok=0) として記録する
    (S-3)。損益以外の表だけが無い場合は、ある表だけで材料にする。"""


def alphavantage_enabled():
    """Alpha Vantage 補完収集の有効条件。ROIC / SimFin と同じく、
    資格情報 (環境変数) がそろったときだけ有効になる。"""
    return bool(os.environ.get("ALPHA_VANTAGE_API_KEY"))


# SimFin のカバー市場。ここの企業は SimFin (バルク) が担うため対象外
SIMFIN_MARKETS = frozenset({"US", "DE", "CA"})

# entities の market → Alpha Vantage のシンボル接尾辞。対象になりうる
# (コア企業がいて SimFin 圏外の) 市場だけを置く。それ以外の市場は
# 変換不能として対象外に記録される。.LON / .TYO は提供元ドキュメント
# 由来 (report §3)、残りは同体系の取引所略号による推定で未確認。
# 間違っていれば 0件・エラーとして記録に残るため live で直せる
SUFFIX_BY_MARKET = {
    "JP": ".TYO", "GB": ".LON", "FR": ".PAR", "AU": ".AX", "SE": ".STO",
    "CH": ".SWX", "FI": ".HEL", "DK": ".CPH", "NL": ".AMS", "NO": ".OSL",
    "ES": ".MAD", "BE": ".BRU", "IT": ".MIL",
}


def alphavantage_symbol(entity):
    """entities の1社 → Alpha Vantage シンボル。変換できない場合
    (ティッカーなし・SimFin 圏内の市場・接尾辞表に無い市場) は None
    (呼び出し側で記録して対象から外す)。market 未指定は辞書の既定
    (JP) として扱う (config の yahoo_suffix と同じ既定)。"""
    ticker = str(entity.get("ticker") or "").strip()
    if not ticker:
        return None
    market = entity.get("market") or "JP"
    if market in SIMFIN_MARKETS:
        return None
    suffix = SUFFIX_BY_MARKET.get(market)
    if suffix is None:
        return None
    return ticker.upper() + suffix


def _get(function, symbol, http):
    key = os.environ.get("ALPHA_VANTAGE_API_KEY")
    if not key:
        # キー未設定を「成功・0件」で握りつぶすと休止判定まで誤作動する。
        # ROIC / EDINET と同じく例外にする
        raise RuntimeError("ALPHA_VANTAGE_API_KEY is not set")
    _throttle()
    fetch = http or http_fetch
    # 再試行は http_fetch に任せない (retries=0): 失敗したリクエストも
    # 25回/日の枠を消費するため、再試行はその日の予算を食うだけ。
    # 失敗は収集側で失敗として記録され、休止に入らないため次回実行で
    # 再試行される (ROIC と同じ判断)
    raw = fetch(API_BASE,
                params={"function": function, "symbol": symbol,
                        "apikey": key, "datatype": "json"},
                headers={"Accept": "application/json"},
                retries=0)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("unexpected Alpha Vantage response shape")
    # 枠超過は HTTP 200 の本文 (Note / Information) で通知される仕様。
    # 成功で握りつぶすと「全社0件の静かな失敗」になるため専用例外にする
    note = str(data.get("Note") or data.get("Information") or "")
    if note:
        raise RateLimitError(note[:200])
    if data.get("Error Message"):
        raise _SymbolError(str(data["Error Message"])[:200])
    return data


_CURRENCY_UNITS = {"USD": "百万米ドル", "EUR": "百万ユーロ", "GBP": "百万英ポンド",
                   "CHF": "百万スイスフラン", "CAD": "百万カナダドル", "AUD": "百万豪ドル",
                   "JPY": "百万円", "SEK": "百万スウェーデンクローナ",
                   "DKK": "百万デンマーククローネ", "NOK": "百万ノルウェークローネ",
                   "HKD": "百万香港ドル", "SGD": "百万シンガポールドル",
                   "NZD": "百万NZドル", "TWD": "百万台湾ドル"}


def _fmt_money(v, unit):
    m = v / 1_000_000
    return ("△" if m < 0 else "") + f"{abs(m):,.0f}{unit}"


def _num(row, field):
    """API の文字列セル → float。欠損は文字列 "None" で返るため、
    空・"None"・非数値はすべて None。"""
    v = (row or {}).get(field)
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or s == "None":
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


# (表示名, Alpha Vantage フィールド名)。ROIC / EDGAR と同じ項目立て・並び
_IS_ITEMS = (("売上高", "totalRevenue"),
             ("営業利益", "operatingIncome"),
             ("純利益", "netIncome"))
_CF_ITEMS = (("営業キャッシュフロー", "operatingCashflow"),)
_BS_ITEMS = (("現金及び現金同等物", "cashAndCashEquivalentsAtCarryingValue"),
             ("総資産", "totalAssets"),
             ("純資産", "totalShareholderEquity"))


def format_period(is_row, bs_row, cf_row):
    """1四半期分の3表の行 → ROIC / EDGAR と同じ形の財務テキスト。
    損益の数値が無ければ ""。"""
    if not is_row:
        return ""
    end = str(is_row.get("fiscalDateEnding") or "").strip()
    if not end:
        return ""
    currency = str(is_row.get("reportedCurrency")
                   or (bs_row or {}).get("reportedCurrency") or "")
    unit = _CURRENCY_UNITS.get(currency, f"百万{currency}" if currency else "百万米ドル")
    parts = []
    for label, field in _IS_ITEMS:
        v = _num(is_row, field)
        if v is not None:
            parts.append(f"{label} {_fmt_money(v, unit)}")
    for label, field in _CF_ITEMS:
        v = _num(cf_row, field)
        if v is not None:
            parts.append(f"{label} {_fmt_money(v, unit)}")
    # フリーキャッシュフローは列に無いため定義どおりに作る: 営業CF −
    # 設備投資 (capitalExpenditures は支出額が正で返る)。設備投資が
    # 負・欠損なら作らない (無い数値を推測で置かない。SimFin の現金増減
    # 列とは符号の約束が逆である点に注意)
    ocf = _num(cf_row, _CF_ITEMS[0][1])
    capex = _num(cf_row, "capitalExpenditures")
    if ocf is not None and capex is not None and capex >= 0:
        parts.append(f"フリーキャッシュフロー {_fmt_money(ocf - capex, unit)}")
    if not parts:
        return ""
    text = f"直近四半期({end}まで) " + "、".join(parts)
    if bs_row:
        seg = []
        for label, field in _BS_ITEMS:
            v = _num(bs_row, field)
            if v is None and field == "cashAndCashEquivalentsAtCarryingValue":
                v = _num(bs_row, "cashAndShortTermInvestments")
            if v is not None:
                seg.append(f"{label} {_fmt_money(v, unit)}")
        eq, ta = _num(bs_row, "totalShareholderEquity"), _num(bs_row, "totalAssets")
        if eq is not None and ta:
            seg.append(f"自己資本比率 {eq / ta * 100:.1f}%")
        if seg:
            bend = str(bs_row.get("fiscalDateEnding") or end).strip()
            text += f"。貸借対照表({bend}時点) " + "、".join(seg)
    return text + "。"


def _rows_by_period(data):
    out = {}
    for r in (data or {}).get("quarterlyReports") or []:
        if isinstance(r, dict) and r.get("fiscalDateEnding"):
            out[str(r["fiscalDateEnding"])] = r
    return out


# 1社あたりに作るレコード数。財務プール (related_financials) が直近400日・
# 最大2件しか同梱しないため、それより古い・多い分は取っても材料にならない
# (ROIC / SimFin と同じ)
MAX_PERIODS_PER_COMPANY = 2


def fetch_alphavantage(symbol, max_results, lookback_days, as_of, http=None):
    """symbol は alphavantage_symbol の返すシンボル (例: ONT.LON)。
    四半期の直近2期分を期間ごとのレコードにして返す (ROIC と同形)。

    シンボルが提供元に無い場合 (損益表の Error Message) は例外のまま
    上げる (S-3: 成功・0件で握りつぶすと休止判定と静かな0件の検知の
    両方から消えるため、呼び出し側で失敗として記録させる)。枠超過
    (RateLimitError) と通信失敗も同じく例外のまま上げる。損益以外の表
    だけが無い場合は、ある表だけで材料にする (ROIC と同じ)。
    """
    q = str(symbol or "").strip()
    if not q:
        return []
    tables = {}
    for fn in STATEMENT_FUNCTIONS:
        try:
            tables[fn] = _rows_by_period(_get(fn, q, http))
        except _SymbolError:
            if fn == "INCOME_STATEMENT":
                raise  # シンボル自体が無い = 失敗として記録させる (S-3)
            tables[fn] = {}  # 損益以外の表が無いだけなら、ある表で材料にする
        except HttpError as e:
            if e.status == 404 and fn != "INCOME_STATEMENT":
                tables[fn] = {}
            else:
                raise
    bare = q.split(".", 1)[0]
    out = []
    for end in sorted(tables["INCOME_STATEMENT"], reverse=True):
        if len(out) >= MAX_PERIODS_PER_COMPANY:
            break
        is_row = tables["INCOME_STATEMENT"][end]
        text = format_period(is_row, tables["BALANCE_SHEET"].get(end),
                             tables["CASH_FLOW"].get(end))
        if not text:
            continue
        try:
            period_date = date.fromisoformat(end)
        except ValueError:
            continue
        if period_date < as_of - timedelta(days=lookback_days) or period_date > as_of + timedelta(days=92):
            continue
        out.append({
            "source": "alphavantage", "source_id": f"{q}-{end}",
            "title": f"{bare} 財務情報({end} 四半期)",
            "abstract": f"{bare}の{FINANCIAL_MARKER}: {text}",
            "url": "https://www.alphavantage.co/",
            "published": end,
            "fields": ["Alpha Vantage", "財務"],
            "affiliations": [],
            "info_type": "disclosure",
        })
    return out
