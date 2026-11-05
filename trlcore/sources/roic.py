"""ROIC.ai の財務データ API (v3) から年次財務諸表を取得する。

役割は財務補完 (2026-11-04 採用の財務補間3社併用の第1弾): 全16市場の
年次ベースで、財務ゼロ企業の穴埋めと Yahoo のみの企業への裏取りを担う。
公式開示 (EDINET / SEC EDGAR) の代替ではなく、標準化済みの年次数値を
財務プール (関連財務根拠) に同梱するための材料。

API の要点 (公式ドキュメント https://www.roic.ai/api/docs と提供元の
agents-plugin の fundamentals 参照で 2026-11-05 に確認):
- 取得は GET /v3.0.0/fundamental/{income-statement|balance-sheet|cash-flow}/{識別子}
  識別子は "{取引所コード}:{ティッカー}" (例: NASDAQ:AAPL、TYO:3174)
- 無料枠で使えるのは period_type=annual のみ (四半期は 402 で弾かれる)
- 認証は Authorization: Bearer <key> のみ。クエリパラメータと併用すると
  401 になるため、キーを URL に載せない (ログ秘匿の面でも有利)
- 無料枠は 5 リクエスト/分 (60秒のスライディングウィンドウ、全リクエストが
  課金単位) で履歴は2年。1社の取得は 3表 = 3リクエストを使う
- 応答は data 配列のフラットな行で、期間ヘッダ (period_end_date /
  fiscal_year / currency) に指標フィールドが並ぶ
統合 (対象選定・取得間隔・記録) は pipeline の _collect_roic が担う。
"""

import json
import os
import threading
import time
import urllib.parse
from datetime import date, timedelta

from ..http import http_fetch, HttpError

API_BASE = "https://api.roic.ai/v3.0.0"
FINANCIAL_MARKER = "財務数値(ROIC.ai)"

# 無料枠は 5 リクエスト/分。等間隔に 12.5 秒空ければ、どの 60 秒窓でも
# 5 回を超えない (5回目が +50 秒、6回目は +62.5 秒で窓の外に出る)。
# EDGAR の _throttle と同じ形でプロセス全体を直列化する (並列で呼ばれても
# 間隔を守る)。間隔の根拠は無料枠の上限そのもの
MIN_INTERVAL_SEC = 12.5
_rate_lock = threading.Lock()
_last_call = [0.0]


def _throttle(sleep=time.sleep, now=time.monotonic):
    with _rate_lock:
        wait = MIN_INTERVAL_SEC - (now() - _last_call[0])
        if wait > 0:
            sleep(wait)
        _last_call[0] = now()


def roic_enabled():
    """ROIC 補完収集の有効条件。Cloudflare 第2プロバイダと同じく、
    資格情報 (環境変数) がそろったときだけ有効になる。"""
    return bool(os.environ.get("ROIC_API_KEY"))


def _get(path, params, http):
    key = os.environ.get("ROIC_API_KEY")
    if not key:
        # キー未設定を「成功・0件」で握りつぶすと、取得間隔の休止判定まで
        # 誤作動する。EDINET と同じく例外にする (監査 A-3 と同じ理由)
        raise RuntimeError("ROIC_API_KEY is not set")
    _throttle()
    fetch = http or http_fetch
    # 再試行は http_fetch に任せない (retries=0): 失敗したリクエストも
    # 無料枠の課金単位で、再試行は次の窓の予算を食うだけ (提供元の指針)。
    # 429 は収集側で失敗として記録され、次回実行で再試行される
    raw = fetch(API_BASE + path, params=params,
                headers={"Authorization": f"Bearer {key}",
                         "Accept": "application/json"},
                retries=0)
    return json.loads(raw)


# entities の market → ROIC の取引所コード。
# 公式ドキュメントの例 (NASDAQ / NYSE) と ROIC 自社サイトの銘柄ページ表記
# (TYO / XETR / ASX / TSX / CPH / STO / OSL) で実例を確認したものと、
# 未確認の慣用コード (LON / SIX / HEL / EURONEXT / MCE / MIL) が混ざる。
# 未確認分が間違っていれば、その市場の取得は 404 → 0件として run_sources に
# 記録される (静かに失敗しない) ため、live の初回結果を見て直せる。
EXCHANGE_BY_MARKET = {
    "JP": "TYO", "GB": "LON", "DE": "XETR", "CH": "SIX", "CA": "TSX",
    "AU": "ASX", "SE": "STO", "DK": "CPH", "NO": "OSL", "FI": "HEL",
    "NL": "EURONEXT", "BE": "EURONEXT", "FR": "EURONEXT",
    "ES": "MCE", "IT": "MIL",
}
US_EXCHANGES = ("NASDAQ", "NYSE", "AMEX")


def roic_symbol(entity):
    """entities の1社 → ROIC 識別子。変換できない場合は None
    (呼び出し側で記録して対象から外す)。

    US は市場だけでは取引所 (NASDAQ / NYSE 等) が決まらないため、裸の
    ティッカーを返し、取得時に tickers/search で解決する。
    """
    ticker = str(entity.get("ticker") or "").strip()
    if not ticker:
        return None
    if entity.get("market") == "US":
        return ticker.upper()
    prefix = EXCHANGE_BY_MARKET.get(entity.get("market"))
    if not prefix:
        return None
    return f"{prefix}:{ticker}"


def _resolve_us(ticker, http):
    """裸ティッカー → 米国市場の識別子。tickers/search が提供元の用意した
    識別子リゾルバ (ISIN 等の外部IDから qualified symbol への変換用)。"""
    data = _get("/tickers/search",
                {"query": ticker, "search_by": "symbol", "limit": 20}, http)
    rows = data if isinstance(data, list) else (data or {}).get("data") or []
    fallback = None
    for row in rows:
        exchange = str(row.get("exchange") or "")
        if exchange not in US_EXCHANGES:
            continue
        if str(row.get("status") or "listed") == "delisted":
            continue
        bare = str(row.get("primary_symbol") or "")
        sym = str(row.get("symbol") or "")
        if bare != ticker and not sym.endswith(":" + ticker):
            continue
        qualified = sym if ":" in sym else f"{exchange}:{bare or ticker}"
        if row.get("is_primary"):
            return qualified
        fallback = fallback or qualified
    return fallback


_CURRENCY_UNITS = {"USD": "百万米ドル", "EUR": "百万ユーロ", "GBP": "百万英ポンド",
                   "CHF": "百万スイスフラン", "CAD": "百万カナダドル", "AUD": "百万豪ドル",
                   "JPY": "百万円", "SEK": "百万スウェーデンクローナ",
                   "DKK": "百万デンマーククローネ", "NOK": "百万ノルウェークローネ"}


def _fmt_money(v, unit):
    m = v / 1_000_000
    return ("△" if m < 0 else "") + f"{abs(m):,.0f}{unit}"


def _num(row, field):
    v = (row or {}).get(field)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# (表示名, ROIC フィールド名)。EDGAR の財務テキストと同じ項目立て・並びにする
_IS_ITEMS = (("売上高", "is_sales_revenue_turnover"),
             ("営業利益", "is_oper_income"),
             ("純利益", "is_net_income"))
_CF_ITEMS = (("営業キャッシュフロー", "cf_cash_from_oper"),
             ("フリーキャッシュフロー", "cf_free_cash_flow"))
_BS_ITEMS = (("現金及び現金同等物", "bs_cash_near_cash_item"),
             ("総資産", "bs_tot_asset"),
             ("純資産", "bs_total_equity"))


def format_period(is_row, bs_row, cf_row):
    """1期間分の3表の行 → EDGAR と同じ形の財務テキスト。数値が無ければ ""。"""
    if not is_row:
        return ""
    end = is_row.get("period_end_date")
    if not end:
        return ""
    currency = str(is_row.get("currency") or (bs_row or {}).get("currency") or "")
    unit = _CURRENCY_UNITS.get(currency, f"百万{currency}" if currency else "百万円")
    parts = []
    for label, field in _IS_ITEMS + _CF_ITEMS:
        row = is_row if field.startswith("is_") else cf_row
        v = _num(row, field)
        if v is not None:
            parts.append(f"{label} {_fmt_money(v, unit)}")
    if not parts:
        return ""
    text = f"直近通期({end}まで) " + "、".join(parts)
    if bs_row:
        seg = []
        for label, field in _BS_ITEMS:
            v = _num(bs_row, field)
            if v is not None:
                seg.append(f"{label} {_fmt_money(v, unit)}")
        eq, ta = _num(bs_row, "bs_total_equity"), _num(bs_row, "bs_tot_asset")
        if eq is not None and ta:
            seg.append(f"自己資本比率 {eq / ta * 100:.1f}%")
        if seg:
            text += f"。貸借対照表({bs_row.get('period_end_date') or end}時点) " + "、".join(seg)
    return text + "。"


def _rows_by_period(rows):
    out = {}
    for r in rows or []:
        if isinstance(r, dict) and r.get("period_end_date"):
            out[r["period_end_date"]] = r
    return out


def fetch_roic(query, max_results, lookback_days, as_of, http=None):
    """query は roic_symbol の返す識別子 (US のみ裸ティッカー)。
    直近2期分 (無料枠の履歴2年に対応) を期間ごとのレコードにして返す。"""
    q = str(query).strip()
    if not q:
        return []
    identifier = q
    if ":" not in q:
        identifier = _resolve_us(q.upper(), http)
        if not identifier:
            return []  # 米国市場に解決できない (未上場・登録外) なら材料なし
    tables = {}
    for cat in ("income-statement", "balance-sheet", "cash-flow"):
        try:
            data = _get(f"/fundamental/{cat}/{urllib.parse.quote(identifier, safe=':')}",
                        {"period_type": "annual", "order": "desc", "limit": 3}, http)
        except HttpError as e:
            if e.status == 404:
                if cat == "income-statement":
                    return []  # 識別子が ROIC 側に無い = 材料なし (EDGAR の登録なしと同じ扱い)
                data = None  # 損益以外の表が無いだけなら、ある表だけで材料にする
            else:
                raise  # 通信失敗は空で握りつぶさず収集側に記録させる (他ソースと同じ)
        rows = data if isinstance(data, list) else (data or {}).get("data")
        tables[cat] = _rows_by_period(rows)
    bare = identifier.split(":", 1)[-1]
    out = []
    for end in sorted(tables["income-statement"], reverse=True)[:2]:
        is_row = tables["income-statement"][end]
        text = format_period(is_row, tables["balance-sheet"].get(end),
                             tables["cash-flow"].get(end))
        if not text:
            continue
        try:
            period_date = date.fromisoformat(end)
        except ValueError:
            continue
        if period_date < as_of - timedelta(days=lookback_days) or period_date > as_of + timedelta(days=92):
            continue
        out.append({
            "source": "roic", "source_id": f"{identifier}-{end}",
            "title": f"{bare} 財務情報({end} 通期)",
            "abstract": f"{bare}の{FINANCIAL_MARKER}: {text}",
            "url": f"https://www.roic.ai/quote/{identifier.replace(':', '-')}",
            "published": end,
            "fields": ["ROIC.ai", "財務"],
            "affiliations": [],
            "info_type": "disclosure",
        })
    return out
