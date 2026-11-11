"""処理フロー: 収集 → 正規化 → 重複排除 → 検証 → スクリーニング → 評価 → 保存。"""

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from .errors import PromptError
from .dates import days_since, parse_date
from .http import redact
from .normalize import gnews_title_norm, normalize
from .sources.pubmed import pubmed_fetch
from .sources.arxiv import arxiv_fetch
from .sources.gnews import gnews_fetch, prtimes_fetch
from .sources.trials import trials_fetch
from .sources.edinet import edinet_fetch
from .sources.edgar import edgar_fetch
from .sources.yahoo import yahoo_fetch
from .sources.openalex import openalex_fetch
from .sources.roic import fetch_roic, roic_enabled, roic_symbol
from .sources.simfin import fetch_simfin_bulk, simfin_enabled, simfin_market, simfin_ticker
from .sources.alphavantage import (DAILY_REQUEST_LIMIT as AV_DAILY_REQUEST_LIMIT,
                                   MAX_COMPANIES_PER_DAY as AV_MAX_COMPANIES_PER_DAY,
                                   REQUESTS_PER_COMPANY as AV_REQUESTS_PER_COMPANY,
                                   RateLimitError as AvRateLimitError,
                                   alphavantage_enabled, alphavantage_symbol,
                                   fetch_alphavantage)
from .sources.esef import esef_enabled, fetch_esef
from .sources.swiss import SWISS_COMPANIES, fetch_eqs, fetch_gnw, swiss_enabled
from .translate import crossed_thresholds, translate_display_set, translate_watch_queue, usage_totals
from .config import ROOT
from .review import INBOX_NAME, import_inbox_file
from .entities import entity_screen, link_entities
from .prompts import load_prompt
from .llm import llm_settings
from .evaluate import evaluate_one, input_hash, reference_item_state
from .scoring import reference_items
from .market import build_market_reaction
from .tiers import TIER_RULES_VERSION, compute_derived_tiers


# DB 掃除の段階投入 (trl-db-cleanup/DESIGN.md §4): N-1 (invalid_items の
# 世代掃除) とブレーキ B-1/B-2 (履歴系180日) は常時有効。N-2 (paper の
# 保持期限) は N-1 の効きを 1 実行観察したうえで 2026-11-11 に有効化した
# (段階投入の第2段)。180 は読みの実効窓 (90日) の2倍で、他の保持期間と
# 同じ導出 (利用側の窓の2倍) に揃える。
PRUNE_PAPER_DAYS = 180

DEFAULT_FETCHERS = {"pubmed": pubmed_fetch, "arxiv": arxiv_fetch, "gnews": gnews_fetch,
                    "prtimes": prtimes_fetch, "trials": trials_fetch, "edinet": edinet_fetch,
                    "edgar": edgar_fetch,
                    "yahoo": yahoo_fetch, "openalex": openalex_fetch}


def new_run_id(now=None):
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def screen(cand, as_of, cfg):
    """合格なら None、不合格なら理由。as_of より後の資料は事前評価に混入させない。"""
    sc = cfg["scoring"]["screening"]
    if not cand["published"]:
        return "no_published_date"
    end = parse_date(cand["published"], end=True)
    if end is None or end > as_of:
        return "published_after_as_of"
    if days_since(cand["published"], as_of) > sc["max_age_days"]:
        return "stale"
    # ニュース・開示のような短文ソースは info_type 別の最低文字数を優先する
    by_type = sc.get("min_abstract_chars_by_info_type") or {}
    min_chars = by_type.get(cand.get("info_type"), sc["min_abstract_chars"])
    if len(cand["abstract"] or "") < min_chars:
        return "abstract_too_short"
    reason = entity_screen(cand, sc.get("require_entity", "off"))
    if reason:
        return reason
    return None


# 財務系は決算 (四半期) 単位でしか変化しないため、毎回取りに行かず、前回の
# 試行から一定期間はその企業のクエリを休む。休止の長さは最終試行の結果で
# 変える (ISSUES-2 の S-1。補完系が採っている「1件以上取れたときだけ長い
# 休止」の型にメイン財務も揃える):
# - 取得できた (fetched>0): 四半期単位でしか変わらないため FINANCIAL_SKIP_DAYS
# - 成功したが0件 (空振り): FINANCIAL_ZERO_SKIP_DAYS の短い休止
# - 失敗: 休止の対象にせず次回も試行する (障害中の財務ソースは毎回リトライ)
FINANCIAL_SOURCES = frozenset({"edgar", "edinet"})
FINANCIAL_SKIP_DAYS = 20
# 空振り時の短い休止の長さ。実行サイクル (週4回: 平日の間隔は1日、週末明け
# は最大4日) から導く: 3日は約2回分の実行を飛ばす長さで、空振りの再試行
# コストを抑えつつ、対象の静かな消滅の検出遅れを週単位に収める。0件を
# 従来どおり20日休ませると、消滅が20日単位で見えなくなる (S-1 の問題)
FINANCIAL_ZERO_SKIP_DAYS = 3
# 社別の静かな0件の警告 (S-2)。ソース単位の全滅しか拾えない quiet_zero の
# 補完で、継続を「回数」でなく「期間」で測る: 四半期開示の周期は約90日で、
# それを超えて0件が続くのは「窓内に申告が無い」では説明しにくい。最終試行
# の鮮度の上限 (31日) は、最長休止 (補完系の30日) を過ぎた試行が無い
# クエリを「対象から外れた残骸」として除くためのもの
ZERO_FETCH_STREAK_DAYS = 90
ZERO_FETCH_ACTIVE_DAYS = 31
# 0件継続の測定対象: 社単位のクエリを持つ財務系 (メイン財務と、バルク
# 不在社が恒常0件になる SimFin)。Alpha Vantage のシンボル不一致は失敗
# (ok=0) として記録されるため (S-3)、こちらの0件継続には現れない
ZERO_FETCH_STREAK_SOURCES = ("edgar", "edinet", "simfin")
# 公表後は内容が変わらないソース (論文・ニュース・治験登録)。既知の同一アイテムは
# published が不変なら再照合・再統合を省略できる。開示 (yahoo/edgar/edinet) は
# 訂正があり得るため含めない。治験は published=最終更新日なので、更新されれば
# published が変わり、自動的に通常処理へ戻る
STEADY_SOURCES = frozenset({"pubmed", "arxiv", "openalex", "gnews", "prtimes", "trials"})
# Yahoo は非公式 API で制限の問題が起きやすいため、決算周期に合わせてさらに絞る:
# 保存済みの最新決算の期末 +97日 (次の四半期末 + 発表が始まる目安) を過ぎた企業だけを
# 取りに行く。期末の記録が無い企業は発見のため FINANCIAL_SKIP_DAYS の間隔で試行する
FINANCIAL_PERIOD_DAYS = 97
# ゲート通過後に「新しい期間なし」で終わった試行 (空振り) の間引き日数。
# 発表は次の期末の30〜45日後に集中するため、ゲート (次の期末+5日) から発表まで
# 毎週空振りするのをやめ、空振りから14日は休む。検出の遅れは最悪でも
# 間引き+実行間隔に収まる (週次実行で約3週間)
FINANCIAL_POLL_BACKOFF_DAYS = 14
# ROIC.ai 補完収集 (財務ゼロ / Yahoo のみの企業が対象。詳細は _collect_roic)。
# 成功したら30日休む: 年次財務は年1回しか変わらず、EDGAR/EDINET の20日より
# 長くてよい。それでも決算発表の集中期 (期末の30〜45日後) をまたぐ間隔に収まる
ROIC_SKIP_DAYS = 30
# 1回の実行で ROIC が対象にする社数の上限。1社あたり3〜4リクエスト ×
# 12.5秒間隔で約40〜50秒かかるため、10社でも最大で約8分に収まり、収集予算
# (既定1200秒) の中で主収集を圧迫しない。財務ゼロの企業が先に処理される
# ため、上限で切れても穴埋めの優先順位は保たれる
ROIC_MAX_PER_RUN = 10
# 取得する期間の下限は財務プールの窓 (related_financials の400日) に合わせる。
# それより古い年次はプールに入らないため、取っても材料にならない
ROIC_LOOKBACK_DAYS = 400
# SimFin バルク補完収集 (US/DE/CA の四半期・TTM。詳細は _collect_simfin)。
# 休止は ROIC と同じ30日: 四半期・TTM は四半期単位でしか変わらず、無料
# バルクは遅延つきのため、これより短い間隔で取り直しても新しい期間は
# まず増えない。成功の定義も ROIC と同じく「1件以上取れたときだけ」
SIMFIN_SKIP_DAYS = 30
# 取得窓は ROIC と同じく財務プールの窓 (related_financials の400日)
SIMFIN_LOOKBACK_DAYS = 400
# Alpha Vantage 低速キュー補完収集 (SimFin 圏外の四半期。詳細は
# _collect_alphavantage と sources/alphavantage.py)。休止と取得窓は
# ROIC / SimFin と同じ (四半期単位でしか変わらない・プールの窓に合わせる)。
# 枠 (25回/日・1社3リクエスト) の上限値は sources/alphavantage 側で
# 枠から導出して持ち、ここでは持たない (二重管理でずらさない)
ALPHAVANTAGE_SKIP_DAYS = 30
ALPHAVANTAGE_LOOKBACK_DAYS = 400
# ESEF (filings.xbrl.org) 収集 (LEI を持つコア企業が対象。詳細は _collect_esef)。
# 休止・上限・取得窓の考え方は ROIC と同じ (年次データは年1回しか変わらない)
ESEF_SKIP_DAYS = 30
ESEF_MAX_PER_RUN = 10
ESEF_LOOKBACK_DAYS = 400
# ESEF の制度圏 (EU + 英国)。スイスは ESEF 対象外。辞書に lei が無い
# コア企業を「未登録」として数えるための市場集合
ESEF_MARKETS = frozenset({"GB", "DE", "FR", "SE", "FI", "DK", "NL", "NO", "ES", "BE",
                          "IT", "IE", "PT", "CZ", "GR", "AT", "PL"})
# スイス収集 (EQS / GlobeNewswire。詳細は _collect_swiss と sources/swiss)。
# 休止は財務系の標準間隔 (EDGAR / EDINET) と同じ20日: ワイヤの決算発表は
# 半期・年次のため、財務系の確認間隔に合わせれば発表を取りこぼさない。
# 取得窓は財務プールの窓 (related_financials の400日)
SWISS_SKIP_DAYS = 20
SWISS_LOOKBACK_DAYS = 400
# 1回の実行で対象にする社数に上限は置かない: コストは社数ではなく市場数
# (最大3市場×3ファイル) で決まり、対象社を増やしても追加ダウンロードは
# 発生しないため、上限で切ると「バルクを落としたのに一部しか使わない」
# 無駄になる。歯止めは市場単位の予算判定 (予算超過なら市場ごと次回へ)


def _yahoo_resting(q, as_of, attempts, periods, retrievals=None):
    """Yahoo クエリを今回休むかどうか。前回試行が失敗なら間隔の対象にせず再試行する。"""
    att = attempts.get(("yahoo", q["name"]))
    if att is None:
        return False  # 未試行なら取りに行く
    started, ok = att
    if not ok:
        return False
    entity = q["name"].removesuffix("-yahoo")
    period = parse_date(periods.get(entity))
    if period is None:
        last = parse_date((started or "")[:10])
        return last is not None and (as_of - last).days < FINANCIAL_SKIP_DAYS
    if as_of < period + timedelta(days=FINANCIAL_PERIOD_DAYS):
        return True
    # ゲート通過後: 最新期間を取得した試行より後ろの試行は「新しい期間なし」の
    # 空振り。その試行から FINANCIAL_POLL_BACKOFF_DAYS 日は休む。期間を取得した
    # 試行自体 (取得日時と同日) は空振りに数えない
    att_date = parse_date((started or "")[:10])
    got = parse_date(((retrievals or {}).get(entity) or "")[:10])
    if att_date is not None and got is not None and att_date > got:
        return (as_of - att_date).days < FINANCIAL_POLL_BACKOFF_DAYS
    return False


def _gnews_title_key(rec, entity_links):
    """gnews の同一記事判定キー (監査 I-4)。配信社ごとに URL が違う同じ記事を、
    同一企業・正規化タイトル・同日で統合する。短いタイトルは誤統合を避けて
    キーを作らない。"""
    title = gnews_title_norm(rec.get("title"))
    if len(title) < 12 or not entity_links:
        return None
    return (f"gtitle:{entity_links[0]['entity_id']}:{title}:"
            f"{(rec.get('published') or '')[:10]}")


# gnews の日付ゆれを吸収する統合の窓 (日)。Google は同じ記事を配信日を
# 1〜3 日ずらして再配信することがあり、同日キー (gtitle) だけでは割れて
# 重複が残る。実測根拠: 本番 DB の gnews 重複 199 組のうち 182 組が日付差
# 3 日以内に収まり、4 日以上は同じ見出しの別記事である可能性が上がる帯
# だった (2026-11-04 実測)。この窓は store.upsert_candidate の窓つき統合と
# store.dedupe_gnews の統合パスの両方が使う
GNEWS_MERGE_WINDOW_DAYS = 3


def _gnews_aux_key(rec, entity_links):
    """gnews の補助キー (日付なし)。窓つき統合 (GNEWS_MERGE_WINDOW_DAYS) の
    照合専用で、通常の安定ID (rec["keys"]) には混ぜない。日付なしキーだけで
    無条件に統合すると、期間を置いた同じ見出しの別記事 (続報等) を取り
    違えるため、統合の可否は store 側で企業集合の一致と日付差を併せて
    判定する。"""
    title = gnews_title_norm(rec.get("title"))
    if len(title) < 12 or not entity_links:
        return None
    return f"gaux:{entity_links[0]['entity_id']}:{title}"


# 浅い日 (深い日以外の曜日の定期実行) の取得窓。浅い日の役割は、深い日
# (週1回・全窓) までの新規・更新を翌日までに拾うことに限る。窓の長さは
# 規則で決める: 浅い日の間隔 (1日) + 実行の欠落・遅延の吸収 (1日。
# スケジューラ遅延の実測最大は約5.5時間) を2倍にした 4日。深い日が週次で
# 全窓を担保するため、浅い窓から漏れた古い材料も最大で7日遅れで拾われる。
# 対象は発見系ソースのみ。財務系 (yahoo/edgar) は保存側の間隔ルール
# (期末+97日・20日) が支配的なので窓は触らない。gnews は窓の概念がない
SHALLOW_LOOKBACK_DAYS = 4
SHALLOW_SOURCES = ("pubmed", "trials", "openalex", "prtimes", "edinet")


def _apply_shallow_windows(cfg):
    """浅い日用に、発見系ソースの取得窓を SHALLOW_LOOKBACK_DAYS に切り詰める。
    クエリ dict を書き換える (プロセス内のみ。設定ファイルは不変)。
    切り詰めたクエリ数を返す。"""
    d = cfg["sources"]["defaults"]
    capped = 0
    for q in cfg["sources"]["queries"]:
        if any(s in q for s in SHALLOW_SOURCES):
            cur = q.get("lookback_days", d["lookback_days"])
            if cur > SHALLOW_LOOKBACK_DAYS:
                q["lookback_days"] = SHALLOW_LOOKBACK_DAYS
                capped += 1
    return capped


def collect(store, cfg, run_id, as_of, sources, fetchers, counts, log, budget_cap=None,
            roic_fetcher=None, simfin_fetcher=None, alphavantage_fetcher=None,
            esef_fetcher=None, swiss_fetchers=None):
    d = cfg["sources"]["defaults"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    jobs = [(sname, q) for q in cfg["sources"]["queries"] for sname in sources if q.get(sname)]
    # 財務系の休止判定は最終試行 (成否・取得件数つき) で行う (S-1)
    fin_attempts = {}
    for sname in FINANCIAL_SOURCES:
        fin_attempts.update(store.last_source_attempts_with_fetch(sname))
    yahoo_attempts = store.last_source_attempts("yahoo")
    yahoo_periods = store.latest_financial_periods("yahoo")
    yahoo_retrievals = store.latest_financial_retrievals("yahoo")
    kept, skipped = [], 0
    for sname, q in jobs:
        if sname == "yahoo":
            resting = _yahoo_resting(q, as_of, yahoo_attempts, yahoo_periods, yahoo_retrievals)
        elif sname in FINANCIAL_SOURCES:
            att = fin_attempts.get((sname, q["name"]))
            resting = False
            if att is not None:
                started, ok, fetched = att
                t = parse_date((started or "")[:10])
                if ok and t is not None:
                    limit = (FINANCIAL_SKIP_DAYS if (fetched or 0) > 0
                             else FINANCIAL_ZERO_SKIP_DAYS)
                    resting = (as_of - t).days < limit
        else:
            resting = False
        if resting:
            skipped += 1
            continue
        kept.append((sname, q))
    if skipped:
        counts["financial_skipped"] = counts.get("financial_skipped", 0) + skipped
        log(f"[collect] financial skip: {skipped} queries (cadence rules)")
    jobs = kept
    # 後処理の間引き用に、既知アイテムの索引と invalid の記録を先読みする。
    # 取得のたびに同じ論文・ニュースへ企業リンクの照合 (辞書300社走査) を
    # 繰り返すのが後処理の主コストだったため、変わっていないものは触らない
    chash = cfg.get("config_hash")  # collect 単体で呼ばれる場合は無いことがある
    known = store.identifier_index()
    neg = store.invalid_keys(chash)
    neg_seen = set()
    neg_adds = set()

    def fetch(job):
        # 取得だけを並列化する (外部HTTPの待ち時間が支配的)。DB書き込みはメインスレッドで
        # ジョブ順に行い、従来どおりの決定的な挙動を保つ
        sname, q = job
        t0 = time.monotonic()
        try:
            return fetchers[sname](q[sname], q.get("max_results", d["max_results"]),
                                   q.get("lookback_days", d["lookback_days"]), as_of), None, time.monotonic() - t0
        except Exception as e:
            return None, e, time.monotonic() - t0

    def process(job, result):
        # 取得結果の後処理 (正規化・企業リンク・保存) はメインスレッドでジョブ順に
        # 行う。波 (10件) の取得が終わるたびに即座に処理するので、後処理の時間は
        # 後続の波の取得時間に隠れる。処理順は従来 (全取得後にジョブ順) と同じで、
        # DB への書き込み結果は完全同一
        sname, q = job
        raws, exc, elapsed = result
        fetched, new, dups, invalid, err = 0, 0, 0, 0, None
        log(f"[collect:start] {sname}/{q['name']}")
        try:
            if exc is not None:
                raise exc
            fetched = len(raws)
            # 取得上限に到達したクエリの記録 (監査 I-5)。上限ちょうどの取得は
            # 「切り捨てられた可能性」を示す (API の総数は fetcher の外に出ない
            # ため近似として数える。trials は内部上限が要求の3倍)
            cap = q.get("max_results", d["max_results"]) * (3 if sname == "trials" else 1)
            if fetched >= cap:
                counts["cap_reached_queries"] = counts.get("cap_reached_queries", 0) + 1
            for raw in raws:
                try:
                    rec, problems = normalize(raw, now)
                    if rec is None:
                        invalid += 1
                        continue
                    if sname in STEADY_SOURCES and rec["info_type"] != "disclosure":
                        # 既知の同一アイテム (published 不変・要旨あり・リンクが現行
                        # 世代の辞書で作られたもの) は、再照合も再統合もしない。
                        # 要旨が空の保存分は補完の余地があるので通常処理に任せる
                        hit = next((known[k] for k in rec["keys"] if k in known), None)
                        if (hit is not None and hit[1] == rec["published"] and hit[2]
                                and hit[3] == chash):
                            dups += 1
                            counts["dup_skipped"] = counts.get("dup_skipped", 0) + 1
                            continue
                    neg_hit = next((k for k in rec["keys"]
                                    if (k, rec["published"] or "") in neg), None)
                    if neg_hit is not None:
                        # 現行世代の辞書でリンク不成立だった記録済みアイテム。
                        # published が変われば (治験の更新等) キーが外れて再照合される
                        invalid += 1
                        counts["invalid_cached"] = counts.get("invalid_cached", 0) + 1
                        neg_seen.add((neg_hit, rec["published"] or ""))
                        continue
                    entity_links = link_entities(rec, cfg.get("entities", {}))
                    if not entity_links:
                        # 評価もプールも企業リンクが前提。対象企業に紐付かない候補は
                        # 種別を問わず残さない (キューに死蔵されるだけになるため)
                        invalid += 1
                        for k in rec["keys"]:
                            pair = (k, rec["published"] or "")
                            neg.add(pair)
                            neg_adds.add(pair)
                        continue
                    if sname == "gnews":
                        tk = _gnews_title_key(rec, entity_links)
                        if tk:
                            rec["keys"] = list(rec.get("keys") or []) + [tk]
                        aux = _gnews_aux_key(rec, entity_links)
                        cid, is_new = store.upsert_candidate(
                            rec, run_id,
                            window_keys=[aux] if aux else None,
                            entity_ids=[l["entity_id"] for l in entity_links],
                            window_days=GNEWS_MERGE_WINDOW_DAYS)
                    else:
                        cid, is_new = store.upsert_candidate(rec, run_id)
                    store.set_entities(cid, entity_links)
                    store.set_links_hash(cid, chash)
                    for k in rec["keys"]:
                        known[k] = (cid, rec["published"], bool(rec["abstract"]), chash)
                    new += is_new
                    dups += (not is_new)
                except Exception as e:
                    # 1件の不良レコードでクエリの残り全件を落とさない
                    invalid += 1
                    log(f"::warning::source {sname} / {q['name']}: record skipped: "
                        f"{redact(f'{type(e).__name__}: {e}')[:200]}")
        except Exception as e:
            err = redact(f"{type(e).__name__}: {e}")[:300]
            log(f"::warning::source {sname} / {q['name']} failed: {err}")
        store.add_run_source(run_id, sname, q["name"], fetched, new, dups, invalid, err is None, err, elapsed)
        for k, v in (("fetched", fetched), ("new", new), ("duplicates", dups), ("invalid", invalid)):
            counts[k] += v
        counts["sources_ok" if err is None else "sources_failed"] += 1
        log(f"[collect] {sname}/{q['name']}: fetched={fetched} new={new} dup={dups} invalid={invalid}"
            + (f" ERROR={err}" if err else ""))

    # 収集フェーズ全体の時間予算 (監査 A-1): ジョブのタイムアウトだけが歯止めに
    # ならないよう、波 (10件) ごとに経過を見て、超過したら未着手のクエリを
    # 打ち切って評価へ進む。予算 0 は無制限。実行中の取得は止めない (HTTP の
    # タイムアウトと再試行で上限が付いている)
    budget = ((cfg.get("scoring") or {}).get("screening") or {}).get("collect_budget_sec", 1200)
    if budget_cap is not None:
        # 実行全体の締め切りから逆算した上限 (評価の最低枠を残した残り時間)。
        # 厳しい方を採り、締め切り超過なら最初の波で打ち切れるよう 0 にはしない
        cap = max(0.001, budget_cap)
        budget = cap if not budget else min(budget, cap)
    exceeded = 0
    collect_t0 = time.monotonic()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=5) as ex:
        idx = 0
        while idx < len(jobs):
            if idx and budget and time.monotonic() - collect_t0 > budget:
                exceeded = len(jobs) - idx
                for job in jobs[idx:]:
                    process(job, (None, RuntimeError("collect budget exceeded"), 0.0))
                break
            chunk = jobs[idx:idx + 10]
            futures = [ex.submit(fetch, j) for j in chunk]
            for job, f in zip(chunk, futures):
                process(job, f.result())
            idx += len(chunk)
    if exceeded:
        counts["collect_budget_exceeded"] = exceeded
        log(f"::warning::[collect] 収集フェーズの時間予算({budget}秒)を超えたため "
            f"{exceeded} クエリを打ち切りました")
    store.add_invalid(neg_adds, chash)
    store.touch_invalids(neg_seen, chash)
    # 財務補完 (ROIC.ai): キーがある場合だけ、主収集の残り予算で回す。
    # 補完ソースなので主収集を圧迫しない (予算超過時は対象を次回へ回す)
    if roic_enabled():
        _collect_roic(store, cfg, run_id, as_of, counts, log, budget, collect_t0,
                      roic_fetcher or fetch_roic)
    # 財務補完 (SimFin バルク): ROIC の後段。キーがある場合だけ回す
    if simfin_enabled():
        _collect_simfin(store, cfg, run_id, as_of, counts, log, budget, collect_t0,
                        simfin_fetcher or fetch_simfin_bulk)
    # 財務補完 (Alpha Vantage 低速キュー): 最後段。キーがある場合だけ回す
    if alphavantage_enabled():
        _collect_alphavantage(store, cfg, run_id, as_of, counts, log, budget, collect_t0,
                              alphavantage_fetcher or fetch_alphavantage)
    # ESEF (欧州公式開示の年次財務): 補完群の後段で回す (ゲートは esef_enabled)
    if esef_enabled():
        _collect_esef(store, cfg, run_id, as_of, counts, log, budget, collect_t0,
                      esef_fetcher or fetch_esef)
    # スイス収集 (EQS / GlobeNewswire): 後段バッチの最後に回す
    # (ゲートは swiss_enabled)
    if swiss_enabled():
        _collect_swiss(store, cfg, run_id, as_of, counts, log, budget, collect_t0,
                       swiss_fetchers or {"eqs": fetch_eqs, "gnw": fetch_gnw})


def _collect_roic(store, cfg, run_id, as_of, counts, log, budget, collect_t0, fetcher):
    """ROIC.ai による財務補完収集 (有効条件は環境変数 ROIC_API_KEY の存在)。

    対象は (a) 財務レコードが1件も無いコア企業、(b) Yahoo のみで裏取りが
    無いコア企業 の順。通常収集 (collect の process) と違い、クエリは企業
    辞書から生成するため、企業リンクは推測でなく生成元そのものを strong で
    付ける。記録の流儀 (run_sources / counts) は process に揃え、「静かな
    0件」の検知にもそのまま乗る。新テーブルは作らず、取得した財務は既存の
    財務プールと候補バージョン保全 (upsert_candidate の開示更新) に乗る。
    """
    coverage = store.financial_sources_by_entity()
    successes = store.last_source_successes_with_fetch("roic")
    targets, n_rest, n_unmapped = [], 0, 0
    for e in cfg.get("entities", {}).get("entities", []):
        if e.get("tier") != "core" or not e.get("ticker"):
            continue
        have = coverage.get(e["id"]) or set()
        if have and have != {"yahoo"}:
            continue  # 公式ソース等の裏取りがある企業は補完の対象外
        symbol = roic_symbol(e)
        if symbol is None:
            n_unmapped += 1
            continue
        # 休止は「成功して1件以上取れた」ときだけ。ok でも fetched=0 の
        # 空振りを成功として休むと、識別子の誤り (英国 LON 等) で30日
        # 閉じ込められる。本当に材料が無い企業は毎回対象に残るが、
        # 1回の上限 (10社) の範囲で収まる規模に限られる
        att = successes.get(("roic", f"{e['id']}-roic"))
        if att is not None and (att[1] or 0) > 0:
            last = parse_date((att[0] or "")[:10])
            if last is not None and (as_of - last).days < ROIC_SKIP_DAYS:
                n_rest += 1
                continue
        targets.append((1 if have else 0, e, symbol))
    targets.sort(key=lambda t: t[0])  # 安定ソート: 財務ゼロ (0) を先に、同群は辞書順
    counts["roic_targets"] = len(targets)
    counts["roic_skipped_rest"] = n_rest
    counts["roic_skipped_unmapped"] = n_unmapped
    if n_unmapped:
        log(f"[collect] roic: 市場変換できないため {n_unmapped} 社を対象外にしました")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    chash = cfg.get("config_hash")
    done = 0
    for _prio, e, symbol in targets:
        if done >= ROIC_MAX_PER_RUN or (budget and time.monotonic() - collect_t0 > budget):
            counts["roic_deferred"] = len(targets) - done
            log(f"[collect] roic: 上限/予算のため {len(targets) - done} 社を次回へ回します")
            break
        done += 1
        qname = f"{e['id']}-roic"
        t0 = time.monotonic()
        err, raws = None, []
        try:
            raws = fetcher(symbol, 5, ROIC_LOOKBACK_DAYS, as_of) or []
        except Exception as ex:
            err = redact(f"{type(ex).__name__}: {ex}")[:300]
            log(f"::warning::source roic / {qname} failed: {err}")
        fetched = len(raws) if err is None else 0
        new = dups = invalid = 0
        if err is None:
            for raw in raws:
                try:
                    rec, _problems = normalize(raw, now)
                    if rec is None:
                        invalid += 1
                        continue
                    cid, is_new = store.upsert_candidate(rec, run_id)
                    store.set_entities(cid, [{"entity_id": e["id"], "strength": "strong",
                                              "matched_by": "roic", "matched_alias": symbol}])
                    store.set_links_hash(cid, chash)
                    new += is_new
                    dups += (not is_new)
                except Exception as ex:
                    invalid += 1
                    log(f"::warning::source roic / {qname}: record skipped: "
                        f"{redact(f'{type(ex).__name__}: {ex}')[:200]}")
        elapsed = round(time.monotonic() - t0, 3)
        store.add_run_source(run_id, "roic", qname, fetched, new, dups, invalid,
                             err is None, err, elapsed)
        for k, v in (("fetched", fetched), ("new", new), ("duplicates", dups), ("invalid", invalid)):
            counts[k] += v
        counts["sources_ok" if err is None else "sources_failed"] += 1
        log(f"[collect] roic/{qname}: fetched={fetched} new={new} dup={dups} invalid={invalid}"
            + (f" ERROR={err}" if err else ""))


def _collect_simfin(store, cfg, run_id, as_of, counts, log, budget, collect_t0, fetcher):
    """SimFin バルク CSV による財務補完収集 (有効条件は SIMFIN_API_KEY)。

    対象選定・優先順位・休止・記録は ROIC (_collect_roic) と同じ流儀:
    (a) 財務レコードが1件も無いコア企業、(b) Yahoo のみで裏取りが無い
    コア企業 の順で、対象は SimFin のカバー市場 (US/DE/CA) の企業に限る。
    違いは取得単位: 銘柄単位ではなく市場単位でバルクを落とすため、対象を
    市場でまとめ、fetcher を市場ごとに1回だけ呼んで結果を社ごとに配る。
    記録は社ごと (query={id}-simfin) に ROIC と同じ形で残すため、休止判定
    (成功して1件以上取れたときだけ休む) と「静かな0件」の検知にそのまま
    乗る。市場のダウンロードが失敗したら、その市場の対象社全員を失敗と
    して記録する (0件で握りつぶさず、休止にも入らないため次回再試行)。
    新テーブルは作らず、財務は既存の財務プールに乗る。
    """
    coverage = store.financial_sources_by_entity()
    successes = store.last_source_successes_with_fetch("simfin")
    targets, n_rest, n_unmapped = [], 0, 0
    for e in cfg.get("entities", {}).get("entities", []):
        if e.get("tier") != "core" or not e.get("ticker"):
            continue
        have = coverage.get(e["id"]) or set()
        if have and have != {"yahoo"}:
            continue  # 公式ソース等の裏取りがある企業は補完の対象外 (ROIC と同じ)
        market = simfin_market(e)
        if market is None:
            n_unmapped += 1
            continue
        # 休止は「成功して1件以上取れた」ときだけ (ROIC の規則をそのまま
        # 適用)。バルクに対象社が無い・遅延で新しい期間が無い等で0件の
        # 空振りを成功として休むと、その社は30日閉じ込められる
        att = successes.get(("simfin", f"{e['id']}-simfin"))
        if att is not None and (att[1] or 0) > 0:
            last = parse_date((att[0] or "")[:10])
            if last is not None and (as_of - last).days < SIMFIN_SKIP_DAYS:
                n_rest += 1
                continue
        targets.append((1 if have else 0, e, market, simfin_ticker(e)))
    targets.sort(key=lambda t: t[0])  # 安定ソート: 財務ゼロ (0) を先に (ROIC と同じ)
    counts["simfin_targets"] = len(targets)
    counts["simfin_skipped_rest"] = n_rest
    counts["simfin_skipped_unmapped"] = n_unmapped
    if n_unmapped:
        log(f"[collect] simfin: カバー市場 (US/DE/CA) 外のため {n_unmapped} 社を対象外にしました")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    chash = cfg.get("config_hash")
    by_market = {}
    for item in targets:
        by_market.setdefault(item[2], []).append(item)
    deferred = 0
    for market in sorted(by_market):
        group = by_market[market]
        if budget and time.monotonic() - collect_t0 > budget:
            deferred += len(group)
            continue
        t0 = time.monotonic()
        err, by_ticker = None, {}
        try:
            by_ticker = fetcher(market, [t[3] for t in group],
                                SIMFIN_LOOKBACK_DAYS, as_of) or {}
        except Exception as ex:
            err = redact(f"{type(ex).__name__}: {ex}")[:300]
            log(f"::warning::source simfin / market={market} failed: {err}")
        for _prio, e, _m, ticker in group:
            qname = f"{e['id']}-simfin"
            raws = [] if err is not None else (by_ticker.get(ticker) or [])
            fetched = len(raws) if err is None else 0
            new = dups = invalid = 0
            if err is None:
                for raw in raws:
                    try:
                        rec, _problems = normalize(raw, now)
                        if rec is None:
                            invalid += 1
                            continue
                        cid, is_new = store.upsert_candidate(rec, run_id)
                        store.set_entities(cid, [{"entity_id": e["id"], "strength": "strong",
                                                  "matched_by": "simfin",
                                                  "matched_alias": f"{market}:{ticker}"}])
                        store.set_links_hash(cid, chash)
                        new += is_new
                        dups += (not is_new)
                    except Exception as ex:
                        invalid += 1
                        log(f"::warning::source simfin / {qname}: record skipped: "
                            f"{redact(f'{type(ex).__name__}: {ex}')[:200]}")
            elapsed = round(time.monotonic() - t0, 3)
            store.add_run_source(run_id, "simfin", qname, fetched, new, dups, invalid,
                                 err is None, err, elapsed)
            for k, v in (("fetched", fetched), ("new", new), ("duplicates", dups), ("invalid", invalid)):
                counts[k] += v
            counts["sources_ok" if err is None else "sources_failed"] += 1
            log(f"[collect] simfin/{qname}: fetched={fetched} new={new} dup={dups} invalid={invalid}"
                + (f" ERROR={err}" if err else ""))
    if deferred:
        counts["simfin_deferred"] = deferred
        log(f"[collect] simfin: 予算のため {deferred} 社を次回へ回します")


def _collect_alphavantage(store, cfg, run_id, as_of, counts, log, budget, collect_t0, fetcher):
    """Alpha Vantage による財務補完収集 (低速キュー。有効条件は
    環境変数 ALPHA_VANTAGE_API_KEY)。

    対象選定・優先順位・休止・記録は ROIC / SimFin と同じ流儀:
    (a) 財務レコードが1件も無いコア企業、(b) Yahoo のみで裏取りが無い
    コア企業 の順。違いは2点だけ:
    - 対象は SimFin のカバー圏外 (US/DE/CA 以外) に限る。圏内はバルクで
      一括取得できる SimFin が担い、貴重な 25回/日をそちらに使わない
    - 1実行で処理する社数は、その日の残枠 (25回/日 − 当日消費済み) で
      完走できる社数まで。当日消費済みは run_sources の当日の試行行数
      × 3リクエスト (1社分) で数える (store.source_attempts_on)。
      残りは次回へ回す (deferred として記録する)
    枠超過の応答 (RateLimitError) が返ったら、その社を失敗として記録
    して当日のキューを止める (残りは次回)。通常の失敗はその社だけを
    失敗として記録し、次へ進む (ROIC と同じ)。いずれも休止には入ら
    ないため、枠が回復する翌日以降の実行で再試行される。新テーブルは
    作らず、財務は既存の財務プールに乗る。
    """
    coverage = store.financial_sources_by_entity()
    successes = store.last_source_successes_with_fetch("alphavantage")
    targets, n_rest, n_unmapped = [], 0, 0
    for e in cfg.get("entities", {}).get("entities", []):
        if e.get("tier") != "core" or not e.get("ticker"):
            continue
        have = coverage.get(e["id"]) or set()
        if have and have != {"yahoo"}:
            continue  # 公式ソース等の裏取りがある企業は補完の対象外 (ROIC と同じ)
        symbol = alphavantage_symbol(e)
        if symbol is None:
            n_unmapped += 1
            continue
        # 休止は「成功して1件以上取れた」ときだけ (ROIC の規則をそのまま
        # 適用)。接尾辞の誤り等で0件の空振りを成功として休むと、その社は
        # 30日閉じ込められる
        att = successes.get(("alphavantage", f"{e['id']}-alphavantage"))
        if att is not None and (att[1] or 0) > 0:
            last = parse_date((att[0] or "")[:10])
            if last is not None and (as_of - last).days < ALPHAVANTAGE_SKIP_DAYS:
                n_rest += 1
                continue
        targets.append((1 if have else 0, e, symbol))
    targets.sort(key=lambda t: t[0])  # 安定ソート: 財務ゼロ (0) を先に (ROIC と同じ)
    counts["alphavantage_targets"] = len(targets)
    counts["alphavantage_skipped_rest"] = n_rest
    counts["alphavantage_skipped_unmapped"] = n_unmapped
    if n_unmapped:
        log(f"[collect] alphavantage: SimFin 圏内または接尾辞変換できないため "
            f"{n_unmapped} 社を対象外にしました")
    # その日の残枠。当日すでに他実行 (手動実行の重なり等) で消費した分を
    # 差し引くため、この実行の上限は日次上限 (8社) 以下に下がりうる
    used_today = store.source_attempts_on("alphavantage", as_of.isoformat()) \
        * AV_REQUESTS_PER_COMPANY
    remaining = AV_DAILY_REQUEST_LIMIT - used_today
    allowed = min(AV_MAX_COMPANIES_PER_DAY, max(0, remaining) // AV_REQUESTS_PER_COMPANY)
    counts["alphavantage_used_today"] = used_today
    counts["alphavantage_allowed"] = allowed
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    chash = cfg.get("config_hash")
    done = 0
    for _prio, e, symbol in targets:
        if done >= allowed or (budget and time.monotonic() - collect_t0 > budget):
            counts["alphavantage_deferred"] = len(targets) - done
            log(f"[collect] alphavantage: 枠/予算のため {len(targets) - done} 社を次回へ回します")
            break
        done += 1
        qname = f"{e['id']}-alphavantage"
        t0 = time.monotonic()
        err, raws, rate_limited = None, [], False
        try:
            raws = fetcher(symbol, 5, ALPHAVANTAGE_LOOKBACK_DAYS, as_of) or []
        except AvRateLimitError as ex:
            err = redact(f"{type(ex).__name__}: {ex}")[:300]
            rate_limited = True
            log(f"::warning::source alphavantage / {qname} rate limited: {err}")
        except Exception as ex:
            err = redact(f"{type(ex).__name__}: {ex}")[:300]
            log(f"::warning::source alphavantage / {qname} failed: {err}")
        fetched = len(raws) if err is None else 0
        new = dups = invalid = 0
        if err is None:
            for raw in raws:
                try:
                    rec, _problems = normalize(raw, now)
                    if rec is None:
                        invalid += 1
                        continue
                    cid, is_new = store.upsert_candidate(rec, run_id)
                    store.set_entities(cid, [{"entity_id": e["id"], "strength": "strong",
                                              "matched_by": "alphavantage",
                                              "matched_alias": symbol}])
                    store.set_links_hash(cid, chash)
                    new += is_new
                    dups += (not is_new)
                except Exception as ex:
                    invalid += 1
                    log(f"::warning::source alphavantage / {qname}: record skipped: "
                        f"{redact(f'{type(ex).__name__}: {ex}')[:200]}")
        elapsed = round(time.monotonic() - t0, 3)
        store.add_run_source(run_id, "alphavantage", qname, fetched, new, dups, invalid,
                             err is None, err, elapsed)
        for k, v in (("fetched", fetched), ("new", new), ("duplicates", dups), ("invalid", invalid)):
            counts[k] += v
        counts["sources_ok" if err is None else "sources_failed"] += 1
        log(f"[collect] alphavantage/{qname}: fetched={fetched} new={new} dup={dups} invalid={invalid}"
            + (f" ERROR={err}" if err else ""))
        if rate_limited:
            # 当日の枠は回復しない。残りの対象は失敗として記録せず、
            # 次回 (翌日以降) のキューにそのまま残す
            rest = len(targets) - done
            if rest:
                counts["alphavantage_deferred"] = rest
                log(f"[collect] alphavantage: 枠超過のため残り {rest} 社を次回へ回します")
            break


def _collect_esef(store, cfg, run_id, as_of, counts, log, budget, collect_t0, fetcher):
    """ESEF (filings.xbrl.org) による欧州年次財務の収集。

    対象は辞書に LEI を持つコア企業 (LEI は 2026-11-03 のスイープで確定し
    辞書に登録済み)。公式開示の裏取り源のため ROIC のような「財務ゼロ /
    Yahoo のみ」への限定はせず、全対象を回すが、優先順位は穴埋めを先に
    する (財務ゼロ → Yahoo のみ → その他)。記録の流儀は _collect_roic に
    揃える: 休止は「成功して1件以上取れた」ときだけ (fxo 未収録の企業を
    「成功・0件」で休止に閉じ込めない)、0件は run_sources に残して静かな
    0件の検知に乗せる。企業リンクは生成元そのものを strong で付ける。
    """
    coverage = store.financial_sources_by_entity()
    successes = store.last_source_successes_with_fetch("esef")
    targets, n_rest, n_no_lei = [], 0, 0
    for e in cfg.get("entities", {}).get("entities", []):
        if e.get("tier") != "core":
            continue
        lei = str(e.get("lei") or "").strip()
        if not lei:
            if e.get("market") in ESEF_MARKETS:
                n_no_lei += 1  # 制度圏なのに LEI 未登録 (スイープ後の昇格企業など)
            continue
        att = successes.get(("esef", f"{e['id']}-esef"))
        if att is not None and (att[1] or 0) > 0:
            last = parse_date((att[0] or "")[:10])
            if last is not None and (as_of - last).days < ESEF_SKIP_DAYS:
                n_rest += 1
                continue
        have = coverage.get(e["id"]) or set()
        prio = 0 if not have else 1 if have == {"yahoo"} else 2
        targets.append((prio, e, lei))
    targets.sort(key=lambda t: t[0])  # 安定ソート: 同群は辞書順
    counts["esef_targets"] = len(targets)
    counts["esef_skipped_rest"] = n_rest
    counts["esef_skipped_no_lei"] = n_no_lei
    if n_no_lei:
        log(f"[collect] esef: ESEF 圏のコア企業 {n_no_lei} 社に LEI が未登録のため対象外です")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    chash = cfg.get("config_hash")
    done = 0
    for _prio, e, lei in targets:
        if done >= ESEF_MAX_PER_RUN or (budget and time.monotonic() - collect_t0 > budget):
            counts["esef_deferred"] = len(targets) - done
            log(f"[collect] esef: 上限/予算のため {len(targets) - done} 社を次回へ回します")
            break
        done += 1
        qname = f"{e['id']}-esef"
        t0 = time.monotonic()
        err, raws = None, []
        try:
            raws = fetcher(lei, 5, ESEF_LOOKBACK_DAYS, as_of) or []
        except Exception as ex:
            err = redact(f"{type(ex).__name__}: {ex}")[:300]
            log(f"::warning::source esef / {qname} failed: {err}")
        fetched = len(raws) if err is None else 0
        new = dups = invalid = 0
        if err is None:
            # 部分抽出の観測 (S-4): 欠落項目つきで成立したレコードの数。
            # レコード自体にも欠落が明記されるが、実行単位でも数える
            n_partial = sum(1 for r in raws
                            if (r.get("completeness") or {}).get("missing"))
            if n_partial:
                counts["esef_partial"] = counts.get("esef_partial", 0) + n_partial
            for raw in raws:
                try:
                    rec, _problems = normalize(raw, now)
                    if rec is None:
                        invalid += 1
                        continue
                    cid, is_new = store.upsert_candidate(rec, run_id)
                    store.set_entities(cid, [{"entity_id": e["id"], "strength": "strong",
                                              "matched_by": "esef", "matched_alias": lei}])
                    store.set_links_hash(cid, chash)
                    new += is_new
                    dups += (not is_new)
                except Exception as ex:
                    invalid += 1
                    log(f"::warning::source esef / {qname}: record skipped: "
                        f"{redact(f'{type(ex).__name__}: {ex}')[:200]}")
        elapsed = round(time.monotonic() - t0, 3)
        store.add_run_source(run_id, "esef", qname, fetched, new, dups, invalid,
                             err is None, err, elapsed)
        for k, v in (("fetched", fetched), ("new", new), ("duplicates", dups), ("invalid", invalid)):
            counts[k] += v
        counts["sources_ok" if err is None else "sources_failed"] += 1
        log(f"[collect] esef/{qname}: fetched={fetched} new={new} dup={dups} invalid={invalid}"
            + (f" ERROR={err}" if err else ""))


# 実証・実装の段階を示す語。理論のみの資料には少なく、TRLを決めやすい候補の目印になる
DEMO_SIGNAL_TERMS = ("demonstrat", "prototype", "proof-of-concept", "proof of concept",
                     "experimental", "in vivo", "first-in-human", "実証", "試作", "臨床")


def _collect_swiss(store, cfg, run_id, as_of, counts, log, budget, collect_t0, fetchers):
    """スイス10社の決算発表を EQS / GlobeNewswire の2ワイヤから取得する
    後段バッチ (調査: ~/workspace/trl-swiss-sweep/RESULTS.md)。

    対象は辞書の market=CH の全社 (tier を問わない: 財務候補は評価プール
    だけでなくウォッチ側の材料にもなる)。企業リンクは ROIC と同じく生成元
    そのものを strong で付ける (EQS は記事フッターの ISIN で同定済み、
    GlobeNewswire は会社別フィードが同定)。記録の流儀 (run_sources /
    counts) は _collect_roic に揃え、「静かな0件」の検知にもそのまま乗る。
    休止は「成功して1件以上取れた」クエリだけが対象で、取得0件の成功では
    休まない (ROIC の英国 LON の教訓と同じ: 同定情報の誤りを長期間
    閉じ込めないため)。GNW フィードが未確認の会社は未対応として数える。
    """
    entities = [e for e in cfg.get("entities", {}).get("entities", [])
                if e.get("market") == "CH"]
    successes = {w: store.last_source_successes_with_fetch(w) for w in ("eqs", "gnw")}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    chash = cfg.get("config_hash")
    targets = []
    n_rest = n_unmapped = n_gnw_unmapped = 0
    for e in entities:
        comp = SWISS_COMPANIES.get(e["id"])
        if comp is None:
            n_unmapped += 1
            continue
        for wire in ("eqs", "gnw"):
            if wire == "gnw" and not comp.get("gnw_feed"):
                n_gnw_unmapped += 1
                continue
            att = successes[wire].get((wire, f"{e['id']}-{wire}"))
            if att is not None and (att[1] or 0) > 0:
                last = parse_date((att[0] or "")[:10])
                if last is not None and (as_of - last).days < SWISS_SKIP_DAYS:
                    n_rest += 1
                    continue
            targets.append((e, comp, wire))
    counts["swiss_targets"] = len({e["id"] for e in entities
                                   if e["id"] in SWISS_COMPANIES})
    counts["swiss_skipped_rest"] = n_rest
    if n_unmapped:
        counts["swiss_skipped_unmapped"] = n_unmapped
        log(f"[collect] swiss: 対応表に無い CH 企業 {n_unmapped} 社を対象外にしました")
    if n_gnw_unmapped:
        counts["swiss_gnw_unmapped"] = n_gnw_unmapped
        log(f"[collect] swiss: GlobeNewswire フィード未確認のため {n_gnw_unmapped} 社は EQS のみで取得します")
    done = 0
    for e, comp, wire in targets:
        if budget and time.monotonic() - collect_t0 > budget:
            counts["swiss_deferred"] = len(targets) - done
            log(f"[collect] swiss: 予算のため {len(targets) - done} クエリを次回へ回します")
            break
        done += 1
        qname = f"{e['id']}-{wire}"
        t0 = time.monotonic()
        err, raws = None, []
        try:
            raws = fetchers[wire](e["id"], 5, SWISS_LOOKBACK_DAYS, as_of) or []
        except Exception as ex:
            err = redact(f"{type(ex).__name__}: {ex}")[:300]
            log(f"::warning::source {wire} / {qname} failed: {err}")
        fetched = len(raws) if err is None else 0
        new = dups = invalid = 0
        if err is None:
            for raw in raws:
                try:
                    rec, _problems = normalize(raw, now)
                    if rec is None:
                        invalid += 1
                        continue
                    cid, is_new = store.upsert_candidate(rec, run_id)
                    alias = comp["isin"] if wire == "eqs" else comp["gnw_feed"]
                    store.set_entities(cid, [{"entity_id": e["id"], "strength": "strong",
                                              "matched_by": wire, "matched_alias": alias}])
                    store.set_links_hash(cid, chash)
                    new += is_new
                    dups += (not is_new)
                except Exception as ex:
                    invalid += 1
                    log(f"::warning::source {wire} / {qname}: record skipped: "
                        f"{redact(f'{type(ex).__name__}: {ex}')[:200]}")
        elapsed = round(time.monotonic() - t0, 3)
        store.add_run_source(run_id, wire, qname, fetched, new, dups, invalid,
                             err is None, err, elapsed)
        for k, v in (("fetched", fetched), ("new", new), ("duplicates", dups), ("invalid", invalid)):
            counts[k] += v
        counts["sources_ok" if err is None else "sources_failed"] += 1
        log(f"[collect] {wire}/{qname}: fetched={fetched} new={new} dup={dups} invalid={invalid}"
            + (f" ERROR={err}" if err else ""))


# 実証・実装の段階を示す語。理論のみの資料には少なく、TRLを決めやすい候補の目印になる
DEMO_SIGNAL_TERMS = ("demonstrat", "prototype", "proof-of-concept", "proof of concept",
                     "experimental", "in vivo", "first-in-human", "実証", "試作", "臨床")


def has_demo_signal(c):
    """候補が実証・実装段階の材料を含むか。治験は定義上、検証段階の資料として扱う。"""
    if c.get("info_type") == "trial":
        return True
    text = f"{c.get('title') or ''} {c.get('abstract') or ''}".lower()
    return any(t in text for t in DEMO_SIGNAL_TERMS)


def _selection_rank(c):
    # 対象企業に紐付く候補を優先する (TRL・事業項目の根拠が揃いやすい)。
    # strong(所属・スポンサー等で特定) > weak(言及のみ) > 紐付けなし。
    # 同じ段では「実証シグナル」のある候補を優先する (TRLを根拠付きで決めやすい)。
    # それも同じなら新しい順 (呼び出し側の安定ソートで実現)。
    strengths = {e["strength"] for e in c.get("entities", [])}
    link = 0 if "strong" in strengths else (1 if strengths else 2)
    return (link, 0 if has_demo_signal(c) else 1)


def _is_current_generation(gen, cfg):
    """候補の最新 long 評価の世代印 (store.latest_long_eval_generations の
    値) が現行世代か。世代の同一性は config_version とプロンプト版
    (extract / long) の両方で判定する (FREEZE-PLAN C 節: 世代は
    config_version だけで呼ばない)。印が欠けている評価は現行とは
    みなさない (安全側 = 旧世代として寄せの対象にする)。"""
    if not gen or gen.get("config") != cfg["config_version"]:
        return False
    prompts = (cfg["scoring"].get("prompts") or {})
    got = gen.get("prompts") or {}
    return got.get("extract") == prompts.get("extract") \
        and got.get("long") == prompts.get("long")


def select_for_eval(store, cfg, today, limit, retry_failed="no", only_new=False, stats=None):
    sc = cfg["scoring"]["screening"]
    rows = []

    force_retry = retry_failed == "force"
    retry_enabled = (
        retry_failed is True
        or retry_failed == "yes"
        or force_retry
    )

    statuses = ["screened", "hold", "error", "evaluated"]

    # 評価予算はコア企業に集中させる (案1)。リンク先がコア企業でない候補
    # (ウォッチ企業のみ、または辞書から外れた企業のみ) は評価しない。
    # 収集とプールの対象ではあり続ける。企業リンクの無い候補は収集段階で
    # 破棄されるため、ここに残っていても評価しない。
    core_ids = {e["id"] for e in (cfg.get("entities") or {}).get("entities", [])
                if (e.get("tier") or "core") == "core"}

    # 未消化の再評価予約がある候補は、再評価ゲート (14日) の起点を最後の
    # 評価日時ではなく予約の作成日時 (処分の時点) にする (TA-2)。処分から
    # 間隔が経つ前に通常選定へ戻してしまうと、処分の意図した再評価時期が
    # 最後の評価日時に引きずられてずれるため。予約の無い候補は従来どおり
    reeval_since = {}
    if hasattr(store, "pending_reeval"):
        for q in store.pending_reeval():
            d = parse_date((q.get("created_at") or "")[:10])
            if d is not None:
                reeval_since[q["candidate_id"]] = d

    for c in store.list_by_status(statuses):
        if c.get("info_type") in ("news", "disclosure"):
            continue  # ニュースと開示(財務スナップショット)は単体評価せず、プールとして同梱する
        if only_new and (c["status"] != "screened" or c["last_eval_at"]):
            continue  # 未評価のみモード: 一度でも評価された候補(hold/error含む)は対象外
        linked = {e["entity_id"] for e in c.get("entities", [])}
        if not linked or not (linked & core_ids):
            continue
        # エラー候補は、再試行が明示的に許可された場合だけ選ぶ
        if c["status"] == "error" and not store.only_infra_errors(c["id"]):
            # (旧版がAPI失敗を error として保存した候補は only_infra_errors で再試行対象に戻す)
            if not retry_enabled:
                continue
            if not force_retry and c["retry_count"] >= sc["max_retries"]:
                continue

        # 保留・評価済み(final)候補の自動再評価は、材料が前回評価より後に
        # 更新された場合に限る (TH-1 案D)。時間経過だけでは適格にならない:
        # 判定が変わりうるのは根拠の材料が変わったときだけで、同一材料への
        # 再判定は観測としての情報を持たないため。材料更新の判定は従来の
        # バイパス条件 (material_updated_at が前回評価より後) をそのまま使う。
        # 監査レビューの再評価予約 (人間の処分) がある候補は対象外で、
        # 従来どおり予約の作成日時 (処分日) を起点とする間隔ゲートで扱う
        # (TA-2。予約自体は run_pipeline 側で選定の先頭にも差し込まれる)。
        if c["status"] in ("hold", "evaluated"):
            last = parse_date((c["last_eval_at"] or "")[:10])
            upd = parse_date((c.get("material_updated_at") or "")[:10])
            if c["id"] in reeval_since:
                if (today - reeval_since[c["id"]]).days < sc["reeval_after_days"]:
                    if not (upd and last and upd > last):
                        continue
            elif last is not None and not (upd and upd > last):
                # 旧ルールでは間隔を越えれば適格だった候補が、材料更新が
                # 無く見送られた件数は観測用に数える (滞留の検知)
                if stats is not None and (today - last).days >= sc["reeval_after_days"]:
                    stats["reeval_skipped_no_material_update"] = \
                        stats.get("reeval_skipped_no_material_update", 0) + 1
                continue

        rows.append(c)

    rows.sort(key=lambda c: c["published"] or "", reverse=True)
    rows.sort(key=_selection_rank)
    # 世代の寄せ (G-5): 再評価適格 (TH-1 の材料更新ゲートを通った) 候補の
    # うち、最新 long 評価が旧世代のものを選定の前面に出し、世代混在の
    # 解消を偶発でなく選定の規則にする。前面に出せるのは上限の半分
    # (切り上げ) まで: 残り半分は通常選定 (未評価候補の初回評価を含む)
    # のために残し、週予算を旧世代の消化だけで食い潰さない。半分という
    # 値は「再評価は初回評価に従属する」という滞留対策の従来原則
    # (予約も上限の内数・別枠なし) を、世代寄せにも同じ強さで適用した
    # もの。前面分を超えた旧世代候補は通常選定の後ろに回り、材料が
    # 無くなれば (rest が尽きれば) そちらからも選ばれる。
    # 移行の進捗は stats で観測可能にする (旧世代の残数・適格数・選定数)。
    gen_map = store.latest_long_eval_generations() \
        if hasattr(store, "latest_long_eval_generations") else {}
    oldgen_ids = {cid for cid, g in gen_map.items()
                  if not _is_current_generation(g, cfg)}
    if stats is not None:
        stats["oldgen_long_remaining"] = len(oldgen_ids)
    oldgen_rows = [c for c in rows if c["id"] in oldgen_ids]
    if oldgen_rows:
        if stats is not None:
            stats["oldgen_reeval_eligible"] = len(oldgen_rows)
        cap_old = -(-limit // 2)
        head, tail = oldgen_rows[:cap_old], oldgen_rows[cap_old:]
        rows = head + [c for c in rows if c["id"] not in oldgen_ids] + tail
    # 企業あたりの保持上限: 材料の多い大手がキューを抱え込まないようにする。
    # 上限を超えた分は今回の選定から外れる (新しい材料が来れば順位で入れ替わる)。
    cap = sc.get("eval_queue_per_entity", 10)
    counts, capped = {}, []
    for c in rows:
        ents = sorted({e["entity_id"] for e in c.get("entities", [])} & core_ids)
        key = ents[0] if ents else None
        if key is not None and counts.get(key, 0) >= cap:
            continue
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
        capped.append(c)
    # 論文枠: 治験は実証シグナルで常に上位に来るため、放置すると選定が治験だけで
    # 埋まる。非治験 (論文) を一定数だけ確保し、論文が無ければ治験で埋める。
    slots = sc.get("paper_slots_per_run", 0)
    if slots and limit > 1:
        papers = [c for c in capped if c.get("info_type") != "trial"]
        want = min(slots, len(papers), limit)
        picked, picked_ids, n_paper, n_trial = [], set(), 0, 0
        for c in capped:
            if len(picked) >= limit:
                break
            if c.get("info_type") == "trial":
                if n_trial >= limit - want:
                    continue
                n_trial += 1
            else:
                if n_paper >= want:
                    continue
                n_paper += 1
            picked.append(c)
            picked_ids.add(c["id"])
        for c in capped:  # 枠で確保しきれなかった分は順位順に埋める
            if len(picked) >= limit:
                break
            if c["id"] not in picked_ids:
                picked.append(c)
                picked_ids.add(c["id"])
        if stats is not None:
            stats["oldgen_selected"] = sum(1 for c in picked if c["id"] in oldgen_ids)
        return picked
    picked = capped[:limit]
    if stats is not None:
        stats["oldgen_selected"] = sum(1 for c in picked if c["id"] in oldgen_ids)
    return picked


def select_event_fill(store, cfg, as_of, limit, exclude_ids, log=print):
    """event 充当の候補選定 (O-2)。

    価格反応データがあり、現在の材料について event 評価がまだ無い
    コア企業リンクの候補を、通常選定と同じ順位 (リンク段位 → 実証
    シグナル → 新しい順) で最大 limit 件返す。event 評価済みの判定は
    「材料の最終更新以降に final/hold の event 評価がある」こと。
    反応データの有無は build_market_reaction (系列は DB にキャッシュ
    され、古い場合だけ取得する) で候補ごとに確認し、limit 件が
    埋まった時点で打ち切る (全プールを走査しない)。
    """
    if limit <= 0:
        return []
    core_ids = {e["id"] for e in (cfg.get("entities") or {}).get("entities", [])
                if (e.get("tier") or "core") == "core"}
    pool = []
    for c in store.list_by_status(["screened", "hold", "evaluated"]):
        if c["id"] in exclude_ids:
            continue
        if c.get("info_type") in ("news", "disclosure"):
            continue  # 単体評価の対象は通常選定と同じ範囲 (プール専用は除く)
        linked = {e["entity_id"] for e in c.get("entities", [])}
        if not linked or not (linked & core_ids):
            continue
        ev = store.latest_evaluation(c["id"], "event")
        if ev and ev.get("status") in ("final", "hold"):
            upd = parse_date((c.get("material_updated_at") or "")[:10])
            evd = parse_date((ev.get("evaluated_at") or "")[:10]) \
                or parse_date((ev.get("as_of") or "")[:10])
            if upd is None or (evd is not None and evd >= upd):
                continue  # 現在の材料については event 評価済み
        pool.append(c)
    pool.sort(key=lambda c: c["published"] or "", reverse=True)
    pool.sort(key=_selection_rank)
    out = []
    for c in pool:
        if len(out) >= limit:
            break
        reaction, _name, _symbol = build_market_reaction(store, cfg, c, as_of, log=log)
        if reaction and reaction.get("available"):
            out.append(c)
    return out


def _queue_reason(res):
    if res["status"] == "error":
        return "evaluation_error: " + "; ".join(res["errors"][:3])
    crit = ",".join(res["critical_missing"])
    return "missing_items: " + ",".join(res["missing"]) + (f" (critical: {crit})" if crit else "")


# 実行全体の締め切り (scoring.json の llm.run_budget_sec、現在 1320 秒=22 分) の配分。
# ジョブ上限 30 分から前後の固定費 (テスト・復元・書き出し・保存、実測で約 5 分) と
# 余裕を引いたもの。収集は評価の最低枠を残して止まり、評価は末尾処理の枠を残して止まる。
# 根拠: 評価 1 件の最長は実測 262 秒で、評価の最低枠は 1 バッチ分 + 末尾枠。
# 末尾 (翻訳・保持整理・確定) は通常 1 分以内に収まる
EVAL_RESERVE_SEC = 420
TAIL_RESERVE_SEC = 120


def run_pipeline(store, cfg, llm, *, as_of, sources=("pubmed", "arxiv"), kinds=("long",), max_eval=None,
                 retry_failed=False, do_collect=True, fetchers=None, today=None, log=print, prompt_dir=None,
                 only_new=False, depth="deep", event_fill=False):
    t0 = time.monotonic()
    today = today or datetime.now(timezone.utc).date()
    run_id = new_run_id()
    store.start_run(run_id, cfg["config_version"], cfg["config_hash"], as_of.isoformat(),
                {"sources": list(sources), "kinds": list(kinds), "max_eval": max_eval,
                 "retry_failed": retry_failed, "collect": do_collect, "only_new": only_new,
                 "depth": depth, "event_fill": event_fill,
                 "llm": getattr(llm, "name", "none")})
    counts = {k: 0 for k in ("fetched", "new", "duplicates", "invalid", "sources_ok", "sources_failed", "screened",
                             "rejected", "selected", "final", "hold", "error", "skipped_no_llm", "llm_infra_failed",
                             "financial_skipped")}
    # 警告は Actions ログにしか残らず後から追えないため (監査 O-3)、run の
    # counts にも上限つきで残す (本文は発生箇所で秘匿済みのものに限られる)
    warnings = []
    _log = log

    def log(msg):
        s = str(msg)
        if s.startswith("::warning::") and len(warnings) < 20:
            warnings.append(s[len("::warning::"):][:200])
        _log(msg)

    log(f"[run] {run_id} as_of={as_of} config={cfg['config_version']}/{cfg['config_hash']}")
    if depth == "shallow":
        capped = _apply_shallow_windows(cfg)
        counts["shallow_capped_queries"] = capped
        log(f"[run] depth=shallow: 発見系ソースの取得窓を {SHALLOW_LOOKBACK_DAYS} 日に切り詰め ({capped} クエリ)")
    # DB の完全性: integrity_check はワークフローの Restore 段でも行うが、
    # 参照先の無い行 (孤児) はそちらでは検出できないため、開始時に数えて
    # 記録する (削除はしない。腐敗の早期検知が目的)
    orphans = store.orphan_report()
    if orphans:
        counts["db_orphans"] = sum(orphans.values())
        log(f"::warning::[db] 参照先の無い行があります: {orphans}")
    # LLM コールの内訳 (目的・実モデル・秒数・成否・トークン) を llm_calls に残す。
    # 後から見返せて、見返す価値があり、コストのかからない記録は全て残すという
    # 方針 (2026-11-03 泰崇)。ただしレコードは評価ワーカースレッドから届くため、
    # その場では貯めるだけにし、Store への書き込みはメインスレッドの flush で
    # 行う。sqlite の接続はスレッドをまたいで使えず、直接書くと ProgrammingError
    # になり、記録側の握り潰しで静かに全件消える (2026-11-03 の live 実行で発生)
    llm_records = []
    if llm is not None and hasattr(llm, "recorder"):
        def _record_llm(rec):
            llm_records.append(rec)
            log(f"[llm] {rec.get('purpose') or '-'} model={rec.get('model')} "
                f"{rec.get('seconds')}s ok={1 if rec.get('ok') else 0}"
                + (f" cand={rec['candidate_id']}" if rec.get("candidate_id") else "")
                + (f" error={rec['error']}" if rec.get("error") else "")
                + (" main-skipped" if rec.get("main_skipped") else ""))
        llm.recorder = _record_llm

    def _flush_llm_records():
        while llm_records:
            store.record_llm_call(run_id, llm_records.pop(0))
    # 監査レビューの橋 (段階B): リポジトリ直下の review_inbox.json があれば、
    # 処分・再評価予約・TRL 調整を DB の追加層へ取り込む。選定 (再評価
    # キュー) と、この実行の後に作られる export (調整の反映) の両方に
    # 効かせるため、実行の早い段階で取り込む。ファイルが無ければ何もしない
    imp = import_inbox_file(store, ROOT / INBOX_NAME, log=log)
    if imp:
        taken = {k: v for k, v in imp.items() if v}
        if taken:
            counts["review_import"] = taken
    if cfg["scoring"].get("recalc_on_run"):
        # 重み等の集計変更 (規程の A2 類) を保存済み評価へ反映する。再計算の
        # 来歴は各評価の extra_json に残る (trlcore/recalc)
        from .recalc import recalculate_evaluations
        rc = recalculate_evaluations(store, cfg, apply=True)
        counts["recalculated"] = rc["changed"]
        if rc["changed"]:
            log(f"[recalc] 保存済み評価 {rc['changed']} 件の総合点を現行設定で再計算しました")
        if rc["anomalies"]:
            log(f"::warning::[recalc] 再計算で final にならない評価が {rc['anomalies']} 件あり、対象外にしました")
    run_budget = llm_settings(cfg).get("run_budget_sec") or 0
    run_deadline = (t0 + run_budget) if run_budget else None
    if do_collect:
        # TA-1: 評価可能候補が 0 件の実行では収集フェーズを丸ごと飛ばす。
        # 収集は評価の材料集めであり、ゲート (再評価間隔等) で今回評価
        # できる候補が1件も無いのに集めるだけ集めると、ターンアラウンド
        # (収集の所要時間) だけが残り、プールも無駄に膨らむ。判定は本番の
        # 選定 (select_for_eval) を上限なしでそのまま使う (ゲートと矛盾
        # しない)。ただし候補プール自体が無い (初回実行・空 DB) 場合は
        # 収集がプールの構築そのものなので飛ばさない。また未スクリーニング
        # (new) の候補が残っている場合は、この実行のスクリーニングで
        # 評価可能になりうるため飛ばさない。評価系統が無い実行 (kinds が
        # 空の収集単体) は、評価に奉仕する収集という前提自体が無いため
        # 対象外。評価・監査など他のフェーズは通常どおり動く
        # (引き継ぎ・再評価予約の候補も従来どおり評価される)
        skip_collect = False
        if kinds and store.count_candidates() > 0 and not store.list_by_status(["new"]):
            eligible = select_for_eval(store, cfg, today, 10 ** 9,
                                       retry_failed=retry_failed, only_new=only_new)
            skip_collect = not eligible
        if skip_collect:
            counts["collect_skipped_no_eligible"] = 1
            log("[collect] 評価可能候補が 0 件のため収集フェーズをスキップします (TA-1)")
        else:
            cap = None
            if run_deadline is not None:
                cap = run_deadline - EVAL_RESERVE_SEC - time.monotonic()
            collect(store, cfg, run_id, as_of, sources, {**DEFAULT_FETCHERS, **(fetchers or {})}, counts, log,
                    budget_cap=cap)
    # gnews の重複統合 (保守パス)。gtitle 導入前の旧行や日付ゆれで割れた
    # 重複を、収集のたびに可逆な形 (rejected 化 + 識別子の付け替え) で
    # survivor へ寄せる。冪等で、統合すべき重複が無ければ 0 件になる
    merged = store.dedupe_gnews(window_days=GNEWS_MERGE_WINDOW_DAYS)
    counts["dedup_merged"] = merged
    if merged:
        log(f"[dedupe] gnews の重複 {merged} 件を survivor へ統合しました")
    for c in store.list_by_status(["new"]):
        reason = screen(c, as_of, cfg)
        store.set_status(c["id"], "rejected" if reason else "screened", reason)
        counts["rejected" if reason else "screened"] += 1
    limit = max_eval if max_eval is not None else cfg["scoring"]["screening"]["max_eval_per_run"]
    selected = select_for_eval(
    store, cfg, today, limit, retry_failed=retry_failed, only_new=only_new, stats=counts
)
    if counts.get("reeval_skipped_no_material_update"):
        log(f"[eval] 材料更新が無いため再評価を見送った候補 "
            f"{counts['reeval_skipped_no_material_update']} 件 (TH-1 案D)")
    if counts.get("oldgen_selected"):
        log(f"[eval] 世代の寄せ (G-5): 旧世代の再評価 {counts['oldgen_selected']} 件を選定 "
            f"(適格 {counts.get('oldgen_reeval_eligible', 0)} 件 / "
            f"旧世代の残り {counts.get('oldgen_long_remaining', 0)} 件)")
    # 引き継ぎ: 前回の実行が異常終了 (interrupted/partial/error) か、評価フェーズが
    # 打ち切られて (counts の llm_degraded) 選定が記録されていたら、その未完分を
    # 今回の選定の先頭に置く。前回の実行で評価が保存された候補は完了扱いで除く
    # (evaluations の run_id で判定する)
    handover = []
    prev = store.previous_run(run_id)
    prev_cut = bool(prev) and (prev["status"] in ("interrupted", "partial", "error")
                               or (json.loads(prev.get("counts_json") or "{}")).get("llm_degraded"))
    if prev_cut:
        prev_selected = (json.loads(prev.get("params_json") or "{}")).get("selected") or []
        if prev_selected:
            done_ids = {e["candidate_id"] for e in store.evaluations_for_run(prev["run_id"])}
            core_ids = {e["id"] for e in (cfg.get("entities") or {}).get("entities", [])
                        if (e.get("tier") or "core") == "core"}
            for cid in prev_selected:
                if cid in done_ids:
                    continue
                c = store.get_candidate(cid)
                if not c or c["status"] == "rejected" or c.get("info_type") in ("news", "disclosure"):
                    continue
                linked = {e["entity_id"] for e in c.get("entities", [])}
                if not linked or not (linked & core_ids):
                    continue
                handover.append(c)
    if handover:
        handover_ids = {c["id"] for c in handover}
        selected = (handover + [c for c in selected if c["id"] not in handover_ids])[:limit]
        counts["handover"] = len([c for c in selected if c["id"] in handover_ids])
        log(f"[eval] 前回の未完の選定 {counts['handover']} 件を引き継ぎました")
    # 再評価キュー (監査レビュー経路A・段階B): DB に取り込まれた予約を
    # 選定の最優先で先頭に差し込む。通常の選定ゲート (14日ルール等) は
    # バイパスするが、上限 (limit) の内数として扱い別枠にはしない
    # (滞留対策の回転数を食い潰さないため。設計書5・9節)
    queue_front = []
    pending = store.pending_reeval()
    if pending:
        core_ids = {e["id"] for e in (cfg.get("entities") or {}).get("entities", [])
                    if (e.get("tier") or "core") == "core"}
        for q in pending:
            c = store.get_candidate(q["candidate_id"])
            if not c or c["status"] == "rejected" or c.get("info_type") in ("news", "disclosure"):
                continue
            linked = {e["entity_id"] for e in c.get("entities", [])}
            if not linked or not (linked & core_ids):
                continue
            queue_front.append(c)
    if queue_front:
        front_ids = {c["id"] for c in queue_front}
        selected = (queue_front + [c for c in selected if c["id"] not in front_ids])[:limit]
        counts["reeval_queue"] = len([c for c in selected if c["id"] in front_ids])
        log(f"[eval] 再評価キューの予約 {counts['reeval_queue']} 件を選定の先頭に入れました")
    # event 充当 (O-2): スケジュール実行の政策として、通常選定で余った
    # 上限の内数で、価格反応データのある未評価の event 材料を long の
    # 後に評価する。定期実行では event が手動実行でしか生まれず、反応
    # 整合の監査 (E1/P6) の母数が空のままだったための措置。kinds を
    # 明示した実行 (手動) では発動しない (cli 側で event_fill を
    # 立てるのは kinds 未指定のときだけ)
    fill = []
    if event_fill and "event" not in kinds and limit > len(selected):
        fill = select_event_fill(store, cfg, as_of, limit - len(selected),
                                 {c["id"] for c in selected}, log=log)
        if fill:
            counts["event_fill"] = len(fill)
            log(f"[eval] event 充当 (O-2): 未評価の event 材料 {len(fill)} 件を"
                f"上限の内数で評価します")
    counts["selected"] = len(selected)
    # 選定は実行の途中で死んでも残るよう、この時点で run レコードへ記録する。
    # 上限で外れた引き継ぎ分も連鎖が切れないよう一緒に入れる
    store.update_run_params(run_id, selected=list(dict.fromkeys(
        [c["id"] for c in handover] + [c["id"] for c in selected]
        + [c["id"] for c in fill])))
    if (selected or fill) and llm is None:
        counts["skipped_no_llm"] = len(selected) + len(fill)
        log("[eval] LLMプロバイダが none のため評価をスキップ(収集のみ)。")
        selected = []
        fill = []
    st = llm_settings(cfg)
    phase_t0, consecutive, degraded = time.monotonic(), 0, None
    model = getattr(llm, "model", None) or ""
    try:
        extract_sha = load_prompt("extract", cfg["scoring"]["prompts"]["extract"], prompt_dir)["sha"]
    except PromptError:
        extract_sha = None  # 評価側でエラーになる。キャッシュは使わない
    workers = max(1, st.get("eval_workers", 1))

    def eval_candidate(c, extraction, meta, related, financials, was_cached, reaction=None,
                       cand_kinds=None):
        # ワーカースレッドで動く。Store には触らず LLM 評価だけを行い、結果を返す。
        # 系統(long/event)は抽出を共有するため候補内では従来どおり直列に処理する
        # cand_kinds は候補ごとの系統 (event 充当の候補は ("event",) のみ)
        if cand_kinds is None:
            cand_kinds = tuple(kinds)
        # 候補1件の総時間予算 (llm.candidate_budget_sec): 失敗の連鎖で1件が
        # フェーズ予算を食い潰さないようにする (Mock 等の予算非対応は素通り)
        set_budget = getattr(llm, "set_candidate_budget", None)
        if set_budget is not None:
            set_budget(st.get("candidate_budget_sec") or None)
        try:
            per_kind = []
            entry_cached = was_cached
            extraction_model = None
            for kind in cand_kinds:
                eval_t0 = time.monotonic()
                res = evaluate_one(c, kind, cfg, llm, as_of, extraction, meta, prompt_dir,
                                   related=related, financials=financials,
                                   market_reaction=reaction if kind == "event" else None)
                per_kind.append((kind, res, time.monotonic() - eval_t0, entry_cached))
                if res.get("infra_failure"):
                    break  # この候補の残りの系統は打ち切る (従来どおり)
                extraction, meta = res["extraction"], res["prompt_versions"].get("extract")
                if res.get("extraction_model"):
                    extraction_model = res["extraction_model"]
                elif extraction_model:
                    res["extraction_model"] = extraction_model  # 2系統目にも抽出の実モデルを引き継ぐ
                entry_cached = False
            return per_kind
        finally:
            if set_budget is not None:
                set_budget(None)

    from concurrent.futures import ThreadPoolExecutor
    # RT-2: 評価1候補あたりの所要時間 (候補内の全系統の合計秒) を runs に
    # 残す。候補ごとの内訳と最小限の要約 (件数・平均・最大) を counts に
    # 記録し、遅い候補・系統の偏りを後から観測できるようにする
    eval_elapsed = []
    # 作業リストは (候補, その候補で評価する系統) の列。通常選定の候補は
    # 実行の系統すべて、event 充当 (O-2) の候補は event のみ
    work = [(c, tuple(kinds)) for c in selected] + [(c, ("event",)) for c in fill]
    idx = 0
    while idx < len(work) and not degraded:
        if st["phase_budget_sec"] and time.monotonic() - phase_t0 > st["phase_budget_sec"]:
            degraded = "phase_budget_exceeded"
            log(f"::warning::[eval] 評価フェーズの時間予算({st['phase_budget_sec']}秒)を超えたため打ち切ります")
            break
        if run_deadline is not None and time.monotonic() > run_deadline - TAIL_RESERVE_SEC:
            degraded = "run_budget_exceeded"
            log(f"::warning::[eval] 実行全体の時間予算({run_budget}秒)の締め切りのため評価を打ち切ります")
            break
        batch = work[idx:idx + workers]
        idx += len(batch)
        # Store の読み取り(抽出キャッシュ・関連ニュース)はメインスレッドで事前に済ませる
        prepared = []
        for c, cand_kinds in batch:
            extraction, meta = None, None
            if extract_sha:
                extraction, meta = store.get_extraction(input_hash(c, as_of), extract_sha, model)
            related = store.related_news(c["id"], as_of) if c.get("info_type") != "news" else None
            financials = store.related_financials(c["id"], as_of) if c.get("info_type") != "news" else None
            reaction = None
            if "event" in cand_kinds:
                reaction = build_market_reaction(store, cfg, c, as_of, log=log)
                if reaction and reaction[0] and reaction[0].get("available"):
                    counts["eval_pool_market_reaction"] = counts.get("eval_pool_market_reaction", 0) + 1
            prepared.append((extraction, meta, related, financials, extraction is not None, reaction))
            # プールの充実度の観測 (保留の主因が「根拠なし」に寄る問題の物差し):
            # 評価対象に財務プール・関連ニュースが付いた割合を数える
            counts["eval_prepared"] = counts.get("eval_prepared", 0) + 1
            if financials:
                counts["eval_pool_financial"] = counts.get("eval_pool_financial", 0) + 1
            if related:
                counts["eval_pool_related"] = counts.get("eval_pool_related", 0) + 1
            for kind in cand_kinds:
                log(f"[eval:start] {c['id']} {kind}")
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                batch_results = list(ex.map(
                    lambda bc: eval_candidate(bc[0][0], *bc[1], cand_kinds=bc[0][1]),
                    zip(batch, prepared)))
        else:
            batch_results = [eval_candidate(c, *p, cand_kinds=ck)
                             for (c, ck), p in zip(batch, prepared)]
        # 保存と連続失敗の判定は候補順に行う。打ち切りが決まったらバッチ内の残りは破棄する
        # (並列化で無駄になる呼び出しは最大でバッチの残り分に限る)
        for (c, cand_kinds), p, per_kind in zip(batch, prepared, batch_results):
            if degraded:
                break
            eval_elapsed.append({
                "candidate_id": c["id"],
                "seconds": round(sum(s for _k, _r, s, _c in per_kind), 2)})
            # 評価の保存 (本体・キュー・状態) は1トランザクションに束ねる
            with store.defer_commits():
                ihash = input_hash(c, as_of)
                outcomes = []
                for kind, res, eval_seconds, cached in per_kind:
                    log(f"[eval:time] {c['id']} {kind}: {eval_seconds:.1f}s")
                    if res["extraction"] is not None and not cached and extract_sha:
                        # キャッシュのキーは抽出を実際に行ったモデル (フォールバック時は代替モデル)
                        store.put_extraction(ihash, extract_sha, res.get("extraction_model") or model,
                                             res["extraction"], res["prompt_versions"].get("extract"))
                    if res.get("infra_failure"):
                        # API側の失敗は評価結果として保存しない(error滞留とキュー汚染を防ぐ)。候補は未評価のまま次回へ
                        consecutive += 1
                        counts["llm_infra_failed"] += 1
                        log(f"::warning::[eval] {c['id']} {kind}: LLM API失敗のため未評価のまま残します: {res['errors'][0]}")
                        if not res["infra_failure"]["transient"]:
                            degraded = "llm_fatal_error"
                        elif consecutive >= st["max_consecutive_failures"]:
                            degraded = "llm_consecutive_failures"
                        break
                    consecutive = 0
                    res["extra"].update({"eval_seconds": round(eval_seconds, 2), "extract_cached": cached})
                    store.add_evaluation(run_id, c["id"], res)
                    counts[res["status"]] += 1
                    outcomes.append(res["status"])
                    if res["status"] == "final":
                        store.resolve_queue(c["id"], kind)
                    else:
                        store.upsert_queue(c["id"], kind, _queue_reason(res), res["missing"] or res["errors"],
                                           res["next_sources"], res["reeval_conditions"] or ["追加情報の取得後"],
                                           datetime.now(timezone.utc).isoformat(timespec="seconds"), res["prompt_versions"])
                    log(f"[eval] {c['id']} {kind}: {res['status']} total={res['total']} errors={res['errors']} "
                        f"missing={res['missing']} critical_missing={res['critical_missing']}")
                # 参考項目の充足 (N-1) と subject 付与 (N-2) の観測集計。
                # 参考項目は系統ごとに数える。subject は抽出が候補内で
                # 共有されるため、候補の最初の系統の分だけを数える
                # (系統ごとに数えると付与率の母数が水増しされる)
                for _i, (_kind, res, _sec, _cached) in enumerate(per_kind):
                    if res.get("items"):
                        for rk in reference_items(_kind, cfg):
                            bucket = counts.setdefault(
                                "reference_item_states",
                                {"scored": 0, "explicit_null": 0, "omitted": 0})
                            bucket[reference_item_state(res["items"].get(rk))] += 1
                    if _i == 0:
                        ss = (res.get("extra") or {}).get("subject_stats")
                        if ss:
                            counts["subject_claims"] = \
                                counts.get("subject_claims", 0) + ss["claims"]
                            counts["subject_attached"] = \
                                counts.get("subject_attached", 0) \
                                + ss["with_subject"] - ss["dropped"]
                            counts["subject_dropped"] = \
                                counts.get("subject_dropped", 0) + ss["dropped"]
                if outcomes:
                    status = "hold" if "hold" in outcomes else ("error" if "error" in outcomes else "evaluated")
                    store.set_status(c["id"], status, eval_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                     retry_inc=(status == "error"), retry_reset=(status != "error"))
        _flush_llm_records()
    if eval_elapsed:
        counts["eval_elapsed_sec"] = eval_elapsed
        secs = [e["seconds"] for e in eval_elapsed]
        counts["eval_elapsed_summary"] = {
            "n": len(secs),
            "avg_sec": round(sum(secs) / len(secs), 2),
            "max_sec": max(secs)}
    if degraded in ("llm_fatal_error", "llm_consecutive_failures"):
        log(f"::warning::[eval] LLM APIの失敗が続くため評価フェーズを打ち切ります ({degraded})。未評価の候補は次回に回します")
    if degraded:
        counts["llm_degraded"] = degraded
    _flush_llm_records()
    # 再評価キューの消化印: この実行で実際に新しい評価が保存された予約だけを
    # 消化済みにする。API 失敗などで評価が残らなかった予約は未完了のまま
    # 次回に残る (予約が静かに消えないように)
    if queue_front:
        done_ids = {e["candidate_id"] for e in store.evaluations_for_run(run_id)}
        marked = 0
        for c in queue_front:
            if c["id"] in done_ids:
                store.mark_reeval_done(c["id"])
                marked += 1
        if marked:
            counts["reeval_queue_done"] = marked
            log(f"[eval] 再評価キューの予約 {marked} 件を消化済みにしました")
    # 翻訳は専用 API (Azure → DeepL → Google) で行い、評価用 LLM には任せない
    usage_before = usage_totals(store, today)
    counts["translated"], counts["translation_chars"] = translate_display_set(store, cfg, today, log)
    # ウォッチ・参考の一次情報 (論文・治験・開示、ニュースは訳さない) も、
    # Azure の月枠から導く実行予算の内側で訳す。この実行で表示セットが
    # 使った分を spent_before として渡し、二重に枠を数えない。使用量の
    # 台帳は表示セットと合算する
    counts["translated_watch"], watch_stats = translate_watch_queue(
        store, cfg, today, log,
        spent_before=sum(counts["translation_chars"].values()))
    for _name, _chars in watch_stats.items():
        counts["translation_chars"][_name] = counts["translation_chars"].get(_name, 0) + _chars
    # 無料枠のしきい値を跨いだら記録とログに残す (azure/google は月次累計、deepl は通算)
    alerts = []
    for name, chars in sorted(counts["translation_chars"].items()):
        before = usage_before.get(name, 0)
        for t in crossed_thresholds(before, before + chars):
            alerts.append({"provider": name, "threshold": t, "total": before + chars})
            span = "通算" if name == "deepl" else "月次累計"
            log(f"[translate] 使用量アラート: {name} の{span}が {before + chars:,} 字になり、しきい値 {t:,} 字を超過しました")
    if alerts:
        counts["translation_alerts"] = alerts
    # 古い材料の削除 (DB の単調増加を抑える。評価済み・キュー入りは消さない)
    pruned = store.prune(today, entity_markets={
        e["id"]: e.get("market") or "JP" for e in (cfg.get("entities") or {}).get("entities", [])},
        config_hash=cfg.get("config_hash"), paper_days=PRUNE_PAPER_DAYS)
    if any(pruned[k] for k in ("total", "extraction_cache", "invalid_items",
                               "invalid_stale_generation", "run_sources", "llm_calls")):
        counts["pruned"] = pruned
        log(f"[prune] 古い材料を削除しました: {pruned}")
    # 辞書から消えた alias で作られたリンクの掃除 (同名別組織への誤リンク対策)
    stale_links = store.prune_stale_links(
        (cfg.get("entities") or {}).get("entities", []))
    if stale_links:
        counts["stale_links_pruned"] = stale_links
        log(f"[prune] 陳腐化リンクを削除しました: {stale_links} 件")
    # tier 導出のスナップショット記録 (append-only、T-3): 記録はパイプライン
    # 実行の末尾だけが担う。export 経由 (site 専用ジョブ等) では書かない —
    # あちらは DB を保存しないため書いても蒸発し、記録間隔が経路依存になる。
    # 導出はこの時点の DB 状態 (評価・処分取り込み・監査の反映後) から
    # 決定的に計算する。記録の失敗で実行自体は倒さず、警告に残す
    try:
        n_snap = store.record_tier_snapshot(
            compute_derived_tiers(store, cfg.get("entities")),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            TIER_RULES_VERSION)
        counts["tier_snapshot_rows"] = n_snap
    except Exception as e:
        log(f"::warning::tier スナップショットの記録に失敗しました: "
            f"{type(e).__name__}: {redact(str(e))[:200]}")
    if warnings:
        counts["warnings"] = warnings
    all_failed = do_collect and counts["sources_ok"] == 0 and counts["sources_failed"] > 0
    # 部分障害 (監査 O-1): ソース単位の全滅や、選定した評価の全件エラーは
    # 'ok' で埋もれさせず 'partial' として記録する。一部のクエリ失敗程度では
    # 倒さない (恒常化して意味を失うため、単位はソースと評価全体に取る)
    partial = False
    if do_collect:
        by_source = {}
        for row in store.run_sources(run_id):
            by_source.setdefault(row["source"], []).append(row)
        # 「静かな0件」: 全クエリが成功しているのに取得が0件のソース。
        # キー未設定のまま成功扱いになる経路や、対象の静かな消滅を検出する
        quiet = sorted(name for name, rows in by_source.items()
                       if rows and all(r["ok"] for r in rows)
                       and sum(r["fetched"] or 0 for r in rows) == 0)
        if quiet:
            counts["quiet_zero_sources"] = quiet
            log(f"::warning::[collect] 取得0件のまま成功したソースがあります: {', '.join(quiet)}")
        # 社別の静かな0件 (S-2): ソース単位の全滅検知では拾えない「特定社
        # だけ0件」を、継続期間 (四半期開示の周期) で測って警告する。
        # 一覧は counts の肥大を避けて日数の長い順に20件まで (全体数は別キー)
        streaks = store.zero_fetch_streaks(ZERO_FETCH_STREAK_SOURCES, as_of,
                                           ZERO_FETCH_STREAK_DAYS,
                                           ZERO_FETCH_ACTIVE_DAYS)
        if streaks:
            counts["zero_fetch_streaks"] = streaks[:20]
            if len(streaks) > 20:
                counts["zero_fetch_streaks_total"] = len(streaks)
            names = ", ".join(f"{s['source']}/{s['query']} ({s['days']}日)"
                              for s in streaks[:20])
            log(f"::warning::[collect] 取得0件が{ZERO_FETCH_STREAK_DAYS}日を超えて"
                f"続いているクエリがあります: {names}")
        if not all_failed:
            partial = any(rows and all(not r["ok"] for r in rows)
                          for rows in by_source.values())
    if counts.get("selected", 0) > 0 and counts.get("error", 0) >= counts["selected"]:
        partial = True
    status = "failed" if all_failed else "partial" if partial else "ok"
    store.finish_run(run_id, counts, status, time.monotonic() - t0)
    return {"run_id": run_id, "counts": counts, "failed": all_failed,
            "partial": partial, "llm_degraded": degraded}
