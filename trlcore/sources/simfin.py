"""SimFin のバルク CSV から四半期・TTM 財務諸表を取得する。

役割は財務補完 (2026-11-04 採用の財務補間3社併用の第2弾): US / DE / CA の
企業について、四半期・TTM の財務で Yahoo / ROIC (年次) の穴を埋める。
調査の正本は ~/workspace/research_notes/free-finance-data-apis-20261103-1531/
report.md §6 / §13 (2026-11-03 時点の提供元ページ確認):
- 無料枠のバルク CSV は履歴5年・遅延あり (提供元ページに "(delayed)" 明記。
  遅延幅は未公表で、利用者報告では約12か月)。最新決算の代替ではなく、
  履歴の穴埋めとして使う
- バルクの市場カタログは us / de / ca のみ。それ以外の市場は対象外
- Web-API は2回/秒。バルクは銘柄単位では計量されないため、市場ごとに
  ファイルを一括ダウンロードして対象社分をローカルで抽出するのが本命
  (銘柄単位の Web-API は 500/429 の実測報告があり使わない)
- 公式 Python パッケージは pandas 必須で標準ライブラリのみの制約に反する
  ため、urllib + csv で直接ダウンロードする

ダウンロードの要点 (公式パッケージ simfin のダウンロード実装と
bulk-download のエンドポイント一覧で 2026-11-10 に確認):
- GET https://prod.simfin.com/api/bulk-download/s3
  ?dataset={income|balance|cashflow}&variant={ttm|quarterly}&market={us|de|ca}
- 認証は Authorization: api-key <key> ヘッダ。キーを URL に載せない
- 応答は ';' 区切り CSV (ZIP で返る場合は先頭の CSV メンバを読む)
- CSV の列は Ticker / SimFinId / Currency / Fiscal Year / Fiscal Period /
  Report Date / Publish Date / Restated Date のメタ列 + 指標列 (英語名)

取得する組み合わせは市場あたり3ファイルに固定する: 損益とキャッシュフロー
は TTM (四半期ごとの直近12か月合計。四季の季節性が消え、年次 (ROIC) や
Yahoo の四半期累計と並べて読める)、貸借対照表は時点値なので quarterly
(その期末の値そのもの)。統合 (対象選定・休止・記録) は pipeline の
_collect_simfin が担う。
"""

import csv
import io
import os
import threading
import time
import zipfile
from datetime import date, timedelta

from ..http import http_fetch

API_BASE = "https://prod.simfin.com/api/bulk-download"
FINANCIAL_MARKER = "財務数値(SimFin)"

# entities の market → SimFin のバルク市場コード。カタログにあるのは
# この3市場のみで、それ以外は変換不能として対象外に記録される
MARKET_BY_ENTITY_MARKET = {"US": "us", "DE": "de", "CA": "ca"}

# (dataset, variant) の取得セット。上記のとおり市場あたり3ファイル
DATASETS = (("income", "ttm"), ("balance", "quarterly"), ("cashflow", "ttm"))

# Web-API の上限 2回/秒に合わせたバルク取得の最小間隔 (バルク自体は
# 銘柄単位で計量されないが、提供元のレート上限を超えない側に倒す)。
# ROIC / EDGAR と同じ形でプロセス全体を直列化する
MIN_INTERVAL_SEC = 0.5
_rate_lock = threading.Lock()
_last_call = [0.0]


def _throttle(sleep=time.sleep, now=time.monotonic):
    with _rate_lock:
        wait = MIN_INTERVAL_SEC - (now() - _last_call[0])
        if wait > 0:
            sleep(wait)
        _last_call[0] = now()


def simfin_enabled():
    """SimFin 補完収集の有効条件。ROIC と同じく、資格情報 (環境変数) が
    そろったときだけ有効になる。"""
    return bool(os.environ.get("SIMFIN_API_KEY"))


def simfin_market(entity):
    """entities の1社 → SimFin のバルク市場コード ('us'/'de'/'ca')。
    カバー範囲外の市場・ティッカーなしは None (呼び出し側で記録する)。"""
    if not str(entity.get("ticker") or "").strip():
        return None
    return MARKET_BY_ENTITY_MARKET.get(entity.get("market"))


def simfin_ticker(entity):
    """entities の1社 → SimFin の Ticker 表記。クラス株の '.' は SimFin
    側の '-' 表記に寄せる (対象3市場に現行の該当社はないが、CSV 照合と
    同じ正規化をここでも使う)。"""
    t = str(entity.get("ticker") or "").strip().upper()
    return t.replace(".", "-") if t else None


def _get(dataset, variant, market, http):
    key = os.environ.get("SIMFIN_API_KEY")
    if not key:
        # キー未設定を「成功・0件」で握りつぶすと休止判定まで誤作動する。
        # ROIC / EDINET と同じく例外にする
        raise RuntimeError("SIMFIN_API_KEY is not set")
    _throttle()
    fetch = http or http_fetch
    # 再試行は http_fetch に任せない (retries=0): バルクは数 MB〜数十 MB の
    # 一括取得で、再試行は帯域と時間を倍食いするだけ。失敗は収集側で記録
    # され、休止に入らないため次回実行で再試行される (ROIC と同じ判断)
    return fetch(API_BASE + "/s3",
                 params={"dataset": dataset, "variant": variant, "market": market},
                 headers={"Authorization": f"api-key {key}",
                          "Accept": "text/csv, application/zip"},
                 retries=0)


def parse_bulk_csv(payload):
    """バルク応答 (CSV バイト列、または CSV を含む ZIP) → 行 dict のリスト。

    区切りは提供元の実ファイルが ';'。ヘッダに ';' が無ければ ',' とみなす。
    数値列は文字列のまま返す (解釈は _num 側)。壊れた行 (列数不足) は
    DictReader が None キー等で返すため、Ticker を持たない行は捨てる。
    """
    if isinstance(payload, (bytes, bytearray)) and bytes(payload[:2]) == b"PK":
        with zipfile.ZipFile(io.BytesIO(bytes(payload))) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not names:
                return []
            payload = zf.read(names[0])
    if isinstance(payload, (bytes, bytearray)):
        text = bytes(payload).decode("utf-8-sig", errors="replace")
    else:
        text = str(payload or "")
    lines = text.splitlines()
    if not lines:
        return []
    delim = ";" if ";" in lines[0] else ","
    out = []
    for row in csv.DictReader(io.StringIO(text), delimiter=delim):
        if row and str(row.get("Ticker") or "").strip():
            out.append(row)
    return out


def _num(row, field):
    """CSV の文字列セル → float。空・非数値は None。"""
    v = (row or {}).get(field)
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        return float(str(v).strip().replace(",", ""))
    except ValueError:
        return None


_CURRENCY_UNITS = {"USD": "百万米ドル", "EUR": "百万ユーロ", "GBP": "百万英ポンド",
                   "CHF": "百万スイスフラン", "CAD": "百万カナダドル", "AUD": "百万豪ドル",
                   "JPY": "百万円", "SEK": "百万スウェーデンクローナ",
                   "DKK": "百万デンマーククローネ", "NOK": "百万ノルウェークローネ"}


def _fmt_money(v, unit):
    m = v / 1_000_000
    return ("△" if m < 0 else "") + f"{abs(m):,.0f}{unit}"


# (表示名, SimFin の列名)。ROIC / EDGAR と同じ項目立て・並びにする。
# 純利益は共通株主帰属 (Net Income (Common)) を優先し、無ければ Net Income
_IS_ITEMS = (("売上高", ("Revenue",)),
             ("営業利益", ("Operating Income (Loss)",)),
             ("純利益", ("Net Income (Common)", "Net Income")))
_CF_ITEMS = (("営業キャッシュフロー", ("Net Cash from Operating Activities",)),)
_BS_ITEMS = (("現金及び現金同等物", ("Cash, Cash Equivalents & Short Term Investments",)),
             ("総資産", ("Total Assets",)),
             ("純資産", ("Total Equity",)))


def _pick(row, fields):
    for f in fields:
        v = _num(row, f)
        if v is not None:
            return v
    return None


def format_period(is_row, bs_row, cf_row):
    """1期間分の3表の行 → ROIC / EDGAR と同じ形の財務テキスト。
    損益 (TTM) の数値が無ければ ""。"""
    if not is_row:
        return ""
    end = str(is_row.get("Report Date") or "").strip()
    if not end:
        return ""
    currency = str(is_row.get("Currency") or (bs_row or {}).get("Currency") or "")
    unit = _CURRENCY_UNITS.get(currency, f"百万{currency}" if currency else "百万米ドル")
    parts = []
    for label, fields in _IS_ITEMS:
        v = _pick(is_row, fields)
        if v is not None:
            parts.append(f"{label} {_fmt_money(v, unit)}")
    for label, fields in _CF_ITEMS:
        v = _pick(cf_row, fields)
        if v is not None:
            parts.append(f"{label} {_fmt_money(v, unit)}")
    # フリーキャッシュフローは列に無いため定義どおりに作る:
    # 営業CF − 設備投資。SimFin の "Change in Fixed Assets & Intangibles" は
    # 支出が負で記録される現金増減列なので、営業CF に加算する形になる。
    # どちらか欠ければ作らない (無い数値を推測で置かない)
    ocf = _pick(cf_row, _CF_ITEMS[0][1])
    capex = _num(cf_row, "Change in Fixed Assets & Intangibles")
    if ocf is not None and capex is not None:
        parts.append(f"フリーキャッシュフロー {_fmt_money(ocf + capex, unit)}")
    if not parts:
        return ""
    period = str(is_row.get("Fiscal Period") or "").strip()
    fy = str(is_row.get("Fiscal Year") or "").strip()
    span = f"{fy} {period}".strip()
    text = f"直近TTM({end}まで" + (f"、{span}" if span else "") + ") " + "、".join(parts)
    if bs_row:
        seg = []
        for label, fields in _BS_ITEMS:
            v = _pick(bs_row, fields)
            if v is not None:
                seg.append(f"{label} {_fmt_money(v, unit)}")
        eq, ta = _pick(bs_row, _BS_ITEMS[2][1]), _pick(bs_row, _BS_ITEMS[1][1])
        if eq is not None and ta:
            seg.append(f"自己資本比率 {eq / ta * 100:.1f}%")
        if seg:
            bend = str(bs_row.get("Report Date") or end).strip()
            text += f"。貸借対照表({bend}時点) " + "、".join(seg)
    return text + "。"


def _rows_by_ticker_period(rows, wanted):
    """行リスト → {(ticker, report_date): row}。対象ティッカーだけに絞る。
    同一キーが複数あれば後勝ち (Restated が新しい行が後に並ぶ前提ではなく、
    バルクは再表示済みの最新値に統一されたスナップショットのため)。"""
    out = {}
    for r in rows or []:
        t = str(r.get("Ticker") or "").strip().upper().replace(".", "-")
        end = str(r.get("Report Date") or "").strip()
        if t in wanted and end:
            out[(t, end)] = r
    return out


def _known_by(as_of, row):
    """その期間の数値が as_of 時点で公表済みか。Publish Date が as_of より
    後なら、期末が過去でも当時は知り得なかった数値なので材料にしない
    (事前評価への混入防止は screen の published 判定と同じ考え方)。
    Publish Date が読めない行は、期末の判定 (呼び出し側) に委ねる。"""
    pub = str((row or {}).get("Publish Date") or "").strip()
    if not pub:
        return True
    try:
        return date.fromisoformat(pub) <= as_of
    except ValueError:
        return True


# 1社あたりに作るレコード数。財務プール (related_financials) が直近400日・
# 最大2件しか同梱しないため、それより古い・多い分は取っても材料にならない
MAX_PERIODS_PER_COMPANY = 2


def fetch_simfin_bulk(market, tickers, lookback_days, as_of, http=None):
    """市場1つ分のバルクを取得し、対象ティッカーのレコードを作る。

    market は 'us'/'de'/'ca'、tickers は SimFin 表記のティッカー列。
    戻り値は {ticker: [record, ...]} (期間の新しい順)。対象社がバルクに
    無ければそのティッカーはキー自体が無い (0件として記録される側)。
    通信失敗・認証失敗は例外のまま上げる: 空で握りつぶすと「成功・0件」
    になり、休止判定と静かな0件の検知の両方が壊れる。
    """
    m = str(market or "").strip().lower()
    if m not in ("us", "de", "ca"):
        return {}
    wanted = {str(t).strip().upper().replace(".", "-") for t in tickers or []}
    wanted.discard("")
    if not wanted:
        return {}
    tables = {}
    for dataset, variant in DATASETS:
        payload = _get(dataset, variant, m, http)
        tables[dataset] = _rows_by_ticker_period(parse_bulk_csv(payload), wanted)
    out = {}
    for (ticker, end), is_row in sorted(tables["income"].items(), reverse=True):
        if ticker in out and len(out[ticker]) >= MAX_PERIODS_PER_COMPANY:
            continue
        if not _known_by(as_of, is_row):
            continue
        text = format_period(is_row, tables["balance"].get((ticker, end)),
                             tables["cashflow"].get((ticker, end)))
        if not text:
            continue
        try:
            period_date = date.fromisoformat(end)
        except ValueError:
            continue
        if (period_date < as_of - timedelta(days=lookback_days)
                or period_date > as_of + timedelta(days=92)):
            continue
        out.setdefault(ticker, []).append({
            "source": "simfin", "source_id": f"{m.upper()}:{ticker}-{end}",
            "title": f"{ticker} 財務情報({end} TTM)",
            "abstract": f"{ticker}の{FINANCIAL_MARKER}: {text}",
            "url": "https://www.simfin.com/en/prices/",
            "published": end,
            "fields": ["SimFin", "財務"],
            "affiliations": [],
            "info_type": "disclosure",
        })
    return out
