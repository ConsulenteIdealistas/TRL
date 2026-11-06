"""株価反応データ (event 評価の材料)。

event 評価の必須項目 expectation_gap / not_priced_in は、プロンプトが
「市場予想が確認できなければ null」「株価や出来高の裏付けが無ければ null」
と定めるのに、材料プールには株価時系列が無く、通算33件で final 0件だった
(2026-11-06 実測)。ここでは Yahoo の日次株価から反応指標を決定的に計算し、
LLM には数値だけを渡す (判断は LLM、計算はコード)。

設計: ~/workspace/trl-event-price/DESIGN.md (案A、2026-11-06 泰崇承認)
"""

import json
import math
import statistics
import urllib.parse
from datetime import datetime, timezone

from .dates import parse_date
from .http import http_fetch

CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
SIGMA_DAYS = 60        # σ の推定期間 (取引日)。30/60/120 の感度測定で確定 (DESIGN.md)
MIN_SIGMA_DAYS = 30    # これ未満しか履歴が無い場合は σ を出さない
VOLUME_BASE_DAYS = 20  # 出来高倍率の基準期間 (1か月の平均という慣行)
PRE_WINDOW = 5         # 事前ドリフトの観察窓 [-5,-1]

# アンカー (材料の公開日 = 情報到着日とみなす) の強さ。開示・報道は市場が
# その日に知った可能性が高いが、治験登録・論文は到着点が曖昧。
STRONG_SOURCES = {"prtimes", "edgar", "edinet", "gnews", "yahoo"}
WEAK_SOURCES = {"trials", "pubmed", "openalex", "arxiv"}


def fetch_price_series(symbol, http=None, range_="2y"):
    """Yahoo chart API から日次 (date, close, volume) を新しい順で返す。

    取得できない・系列が無い場合は空リスト (呼び出し側は「データなし」として扱う)。
    日付は取引所ローカル (meta.gmtoffset で補正) にそろえる。"""
    url = CHART_URL.format(symbol=urllib.parse.quote(symbol, safe=""))
    params = {"range": range_, "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    if http is None:
        body = http_fetch(url, params=params, headers=headers, retries=1)
    else:
        body = http(url, params=params, headers=headers)
    doc = json.loads(body)
    results = (doc.get("chart") or {}).get("result") or []
    if not results:
        return []
    r = results[0]
    offset = int(((r.get("meta") or {}).get("gmtoffset")) or 0)
    quotes = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quotes.get("close") or []
    volumes = quotes.get("volume") or []
    bars = []
    for i, ts in enumerate(r.get("timestamp") or []):
        close = closes[i] if i < len(closes) else None
        if close is None:
            continue
        day = datetime.fromtimestamp(ts + offset, tz=timezone.utc).date().isoformat()
        vol = volumes[i] if i < len(volumes) else None
        bars.append((day, float(close), float(vol) if vol is not None else None))
    bars.sort()
    return bars


def _robust_sigma(returns):
    """中央絶対偏差 (MAD) ベースの頑健な日次 σ。

    素の標準偏差は Yahoo 系列の外れ値1本で壊れる (実測: MRNA で 23%/日)。
    MAD × 1.4826 は正規分布で標準偏差と一致する頑健推定。"""
    if len(returns) < MIN_SIGMA_DAYS:
        return None
    med = statistics.median(returns)
    mad = statistics.median([abs(x - med) for x in returns])
    return 1.4826 * mad


def reaction_metrics(bars, anchor_date):
    """(date, close, volume) 系列と材料の公開日から反応指標を計算する。

    返り値: 指標 dict、または計算できない理由つきで None を含む dict。
    反応窓は [0,+1] (アンカー取引日の前日終値 → 翌取引日終値)。
    イベントスタディの標準的な窓で、到着時刻・時差・週末を吸収する。"""
    days = [b[0] for b in bars]
    closes = [b[1] for b in bars]
    volumes = [b[2] for b in bars]
    idx = next((i for i, d in enumerate(days) if d >= anchor_date), None)
    if idx is None:
        return {"available": False, "anchor_date": anchor_date,
                "reason": "材料の公開日以降の取引日が系列に無い"}
    if idx < PRE_WINDOW + 1:
        return {"available": False, "anchor_date": anchor_date,
                "reason": "公開日より前の取引日が不足"}
    if idx + 1 >= len(days):
        return {"available": False, "anchor_date": anchor_date,
                "reason": "公開日直後の取引日がまだ無い"}
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    sigma = _robust_sigma(returns[max(0, idx - 1 - SIGMA_DAYS):idx - 1])
    reaction = closes[idx + 1] / closes[idx - 1] - 1
    z = None
    if sigma:
        z = reaction / (sigma * math.sqrt(2))
    pre_drift = closes[idx - 1] / closes[idx - 1 - PRE_WINDOW] - 1
    vol_ratio = None
    base = [v for v in volumes[max(0, idx - VOLUME_BASE_DAYS):idx] if v]
    post = [v for v in volumes[idx:idx + 2] if v]
    if base and post:
        vol_ratio = statistics.mean(post) / statistics.mean(base)
    return {"available": True,
            "anchor_date": anchor_date,
            "trading_date": days[idx],
            "reaction_pct": reaction * 100,
            "z": z,
            "pre_drift_pct": pre_drift * 100,
            "volume_ratio": vol_ratio,
            "sigma_pct": sigma * 100 if sigma else None,
            "sigma_days": SIGMA_DAYS if sigma else None}


def anchor_strength(source):
    if source in STRONG_SOURCES:
        return "strong"
    if source in WEAK_SOURCES:
        return "weak"
    return "weak"


def format_reaction(metrics, entity_name, symbol, source):
    """プロンプトへ差し込む市場反応ブロック (日本語テキスト)。"""
    if not metrics:
        return "市場反応データなし（対象企業が特定できないため）"
    head = f"主企業: {entity_name}（{symbol}） / 材料の公開日: {metrics.get('anchor_date', '不明')}"
    if not metrics.get("available"):
        return head + f"\n市場反応データなし（{metrics.get('reason', '系列不足')}）"
    strength = anchor_strength(source)
    strength_txt = ("強（開示・報道系の材料で、市場がその日に知った可能性が高い）"
                    if strength == "strong" else
                    "弱（登録・論文系の材料は市場への情報到着点とは限らず、反応の解釈は割り引くこと）")
    z = metrics["z"]
    vol = metrics["volume_ratio"]
    sig = metrics["sigma_pct"]
    return "\n".join([
        head,
        f"取引日（公開日直後の最初の取引日）: {metrics['trading_date']}",
        f"反応リターン [0,+1]: {metrics['reaction_pct']:+.1f}%",
        f"標準化反応 z: {z:+.2f}" if z is not None else "標準化反応 z: 計算不可（σの推定期間不足）",
        f"事前ドリフト [-5,-1]: {metrics['pre_drift_pct']:+.1f}%",
        f"出来高倍率（直前{VOLUME_BASE_DAYS}日平均比）: {vol:.2f}" if vol is not None else "出来高倍率: データなし",
        f"頑健σ（日次）: {sig:.2f}%" if sig is not None else "頑健σ: 推定不可",
        f"アンカーの強さ: {strength_txt}",
    ])


def build_market_reaction(store, cfg, cand, as_of, http=None, log=print):
    """候補の主企業の株価反応を (metrics, entity_name, symbol) で返す。

    系列は market_series にキャッシュし、古い・不足の場合だけ取得する。
    どの段階で失敗しても評価自体は止めず、データなしとして進む。"""
    from .config import YAHOO_SUFFIX
    entities = {e["id"]: e for e in (cfg.get("entities") or {}).get("entities", [])}
    links = store.candidate_entity_ids(cand["id"])
    symbol = entity_name = None
    for eid in links:
        e = entities.get(eid)
        if e and e.get("ticker"):
            market = e.get("market") or "JP"
            suffix = YAHOO_SUFFIX.get(market)
            if suffix is None:
                continue
            symbol = str(e["ticker"]) + suffix
            entity_name = e.get("name") or eid
            break
    published = (cand.get("published") or "")[:10]
    if not symbol or not published or not parse_date(published):
        return None, entity_name, symbol
    bars = store.price_series(symbol)
    stale = (not bars or bars[-1][0] < published
             or (parse_date(as_of.isoformat()) - parse_date(bars[-1][0])).days > 7)
    if stale:
        try:
            fresh = fetch_price_series(symbol, http=http)
            if fresh:
                store.save_price_series(symbol, fresh)
                bars = fresh
        except Exception as e:  # 取得失敗はデータなしとして続行 (評価は止めない)
            log(f"[market] {symbol} の株価系列を取得できませんでした: {type(e).__name__}")
    if not bars:
        return {"available": False, "reason": "株価系列を取得できなかった",
                "anchor_date": published}, entity_name, symbol
    return reaction_metrics(bars, published), entity_name, symbol
