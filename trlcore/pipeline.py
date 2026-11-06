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
from .translate import crossed_thresholds, translate_display_set, translate_watch_queue, usage_totals
from .entities import entity_screen, link_entities
from .prompts import load_prompt
from .llm import llm_settings
from .evaluate import evaluate_one, input_hash


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


# 財務系は決算 (四半期) 単位でしか変化しないため、毎回取りに行かず、前回の取得成功から
# 一定期間はその企業のクエリを休む。失敗したクエリは成功として記録されないため、
# 間隔の対象にならず次回も試行される (障害中の財務ソースは毎週リトライされる)。
FINANCIAL_SOURCES = frozenset({"edgar", "edinet"})
FINANCIAL_SKIP_DAYS = 20
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
            roic_fetcher=None):
    d = cfg["sources"]["defaults"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    jobs = [(sname, q) for q in cfg["sources"]["queries"] for sname in sources if q.get(sname)]
    last_ok = store.last_source_successes()
    yahoo_attempts = store.last_source_attempts("yahoo")
    yahoo_periods = store.latest_financial_periods("yahoo")
    yahoo_retrievals = store.latest_financial_retrievals("yahoo")
    kept, skipped = [], 0
    for sname, q in jobs:
        if sname == "yahoo":
            resting = _yahoo_resting(q, as_of, yahoo_attempts, yahoo_periods, yahoo_retrievals)
        elif sname in FINANCIAL_SOURCES:
            t = parse_date((last_ok.get((sname, q["name"])) or "")[:10])
            resting = t is not None and (as_of - t).days < FINANCIAL_SKIP_DAYS
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
    successes = store.last_source_successes()
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
        last = parse_date((successes.get(("roic", f"{e['id']}-roic")) or "")[:10])
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


# 実証・実装の段階を示す語。理論のみの資料には少なく、TRLを決めやすい候補の目印になる
DEMO_SIGNAL_TERMS = ("demonstrat", "prototype", "proof-of-concept", "proof of concept",
                     "experimental", "in vivo", "first-in-human", "実証", "試作", "臨床")


def has_demo_signal(c):
    """候補が実証・実装段階の材料を含むか。治験は定義上、検証段階の資料として扱う。"""
    if c.get("info_type") == "trial":
        return True
    text = f"{c.get('title') or ''} {c.get('abstract') or ''}".lower()
    return any(t in text for t in DEMO_SIGNAL_TERMS)


def select_for_eval(store, cfg, today, limit, retry_failed="no", only_new=False):
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

        # 保留・評価済み(final)候補は、再評価間隔が経過するまで選ばない。
        # ただし材料が前回評価より後に更新された候補(治験の結果公表など)は、
        # 間隔を待たずに優先して再評価する(結果待ちの解消を拾うため)。
        if c["status"] in ("hold", "evaluated"):
            last = parse_date((c["last_eval_at"] or "")[:10])
            if last and (today - last).days < sc["reeval_after_days"]:
                upd = parse_date((c.get("material_updated_at") or "")[:10])
                if not (upd and upd > last):
                    continue

        rows.append(c)

    def rank(c):
        # 対象企業に紐付く候補を優先する (TRL・事業項目の根拠が揃いやすい)。
        # strong(所属・スポンサー等で特定) > weak(言及のみ) > 紐付けなし。
        # 同じ段では「実証シグナル」のある候補を優先する (TRLを根拠付きで決めやすい)。
        # それも同じなら新しい順 (呼び出し側の安定ソートで実現)。
        strengths = {e["strength"] for e in c.get("entities", [])}
        link = 0 if "strong" in strengths else (1 if strengths else 2)
        return (link, 0 if has_demo_signal(c) else 1)

    rows.sort(key=lambda c: c["published"] or "", reverse=True)
    rows.sort(key=rank)
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
        return picked
    return capped[:limit]


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
                 only_new=False, depth="deep"):
    t0 = time.monotonic()
    today = today or datetime.now(timezone.utc).date()
    run_id = new_run_id()
    store.start_run(run_id, cfg["config_version"], cfg["config_hash"], as_of.isoformat(),
                {"sources": list(sources), "kinds": list(kinds), "max_eval": max_eval,
                 "retry_failed": retry_failed, "collect": do_collect, "only_new": only_new,
                 "depth": depth, "llm": getattr(llm, "name", "none")})
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
    store, cfg, today, limit, retry_failed=retry_failed, only_new=only_new
)
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
    counts["selected"] = len(selected)
    # 選定は実行の途中で死んでも残るよう、この時点で run レコードへ記録する。
    # 上限で外れた引き継ぎ分も連鎖が切れないよう一緒に入れる
    store.update_run_params(run_id, selected=list(dict.fromkeys(
        [c["id"] for c in handover] + [c["id"] for c in selected])))
    if selected and llm is None:
        counts["skipped_no_llm"] = len(selected)
        log("[eval] LLMプロバイダが none のため評価をスキップ(収集のみ)。")
        selected = []
    st = llm_settings(cfg)
    phase_t0, consecutive, degraded = time.monotonic(), 0, None
    model = getattr(llm, "model", None) or ""
    try:
        extract_sha = load_prompt("extract", cfg["scoring"]["prompts"]["extract"], prompt_dir)["sha"]
    except PromptError:
        extract_sha = None  # 評価側でエラーになる。キャッシュは使わない
    workers = max(1, st.get("eval_workers", 1))

    def eval_candidate(c, extraction, meta, related, financials, was_cached):
        # ワーカースレッドで動く。Store には触らず LLM 評価だけを行い、結果を返す。
        # 系統(long/event)は抽出を共有するため候補内では従来どおり直列に処理する
        # 候補1件の総時間予算 (llm.candidate_budget_sec): 失敗の連鎖で1件が
        # フェーズ予算を食い潰さないようにする (Mock 等の予算非対応は素通り)
        set_budget = getattr(llm, "set_candidate_budget", None)
        if set_budget is not None:
            set_budget(st.get("candidate_budget_sec") or None)
        try:
            per_kind = []
            entry_cached = was_cached
            extraction_model = None
            for kind in kinds:
                eval_t0 = time.monotonic()
                res = evaluate_one(c, kind, cfg, llm, as_of, extraction, meta, prompt_dir,
                                   related=related, financials=financials)
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
    idx = 0
    while idx < len(selected) and not degraded:
        if st["phase_budget_sec"] and time.monotonic() - phase_t0 > st["phase_budget_sec"]:
            degraded = "phase_budget_exceeded"
            log(f"::warning::[eval] 評価フェーズの時間予算({st['phase_budget_sec']}秒)を超えたため打ち切ります")
            break
        if run_deadline is not None and time.monotonic() > run_deadline - TAIL_RESERVE_SEC:
            degraded = "run_budget_exceeded"
            log(f"::warning::[eval] 実行全体の時間予算({run_budget}秒)の締め切りのため評価を打ち切ります")
            break
        batch = selected[idx:idx + workers]
        idx += len(batch)
        # Store の読み取り(抽出キャッシュ・関連ニュース)はメインスレッドで事前に済ませる
        prepared = []
        for c in batch:
            extraction, meta = None, None
            if extract_sha:
                extraction, meta = store.get_extraction(input_hash(c, as_of), extract_sha, model)
            related = store.related_news(c["id"], as_of) if c.get("info_type") != "news" else None
            financials = store.related_financials(c["id"], as_of) if c.get("info_type") != "news" else None
            prepared.append((extraction, meta, related, financials, extraction is not None))
            # プールの充実度の観測 (保留の主因が「根拠なし」に寄る問題の物差し):
            # 評価対象に財務プール・関連ニュースが付いた割合を数える
            counts["eval_prepared"] = counts.get("eval_prepared", 0) + 1
            if financials:
                counts["eval_pool_financial"] = counts.get("eval_pool_financial", 0) + 1
            if related:
                counts["eval_pool_related"] = counts.get("eval_pool_related", 0) + 1
            for kind in kinds:
                log(f"[eval:start] {c['id']} {kind}")
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                batch_results = list(ex.map(lambda bc: eval_candidate(bc[0], *bc[1]), zip(batch, prepared)))
        else:
            batch_results = [eval_candidate(c, *p) for c, p in zip(batch, prepared)]
        # 保存と連続失敗の判定は候補順に行う。打ち切りが決まったらバッチ内の残りは破棄する
        # (並列化で無駄になる呼び出しは最大でバッチの残り分に限る)
        for c, p, per_kind in zip(batch, prepared, batch_results):
            if degraded:
                break
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
                                           res["next_sources"], res["reeval_conditions"] or ["追加情報の取得後、または再評価間隔の経過後"],
                                           datetime.now(timezone.utc).isoformat(timespec="seconds"), res["prompt_versions"])
                    log(f"[eval] {c['id']} {kind}: {res['status']} total={res['total']} errors={res['errors']} "
                        f"missing={res['missing']} critical_missing={res['critical_missing']}")
                if outcomes:
                    status = "hold" if "hold" in outcomes else ("error" if "error" in outcomes else "evaluated")
                    store.set_status(c["id"], status, eval_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                     retry_inc=(status == "error"), retry_reset=(status != "error"))
        _flush_llm_records()
    if degraded in ("llm_fatal_error", "llm_consecutive_failures"):
        log(f"::warning::[eval] LLM APIの失敗が続くため評価フェーズを打ち切ります ({degraded})。未評価の候補は次回に回します")
    if degraded:
        counts["llm_degraded"] = degraded
    _flush_llm_records()
    # 翻訳は専用 API (Azure → DeepL → Google) で行い、評価用 LLM には任せない
    usage_before = usage_totals(store, today)
    counts["translated"], counts["translation_chars"] = translate_display_set(store, cfg, today, log)
    # ウォッチ・参考の一次情報 (論文・治験のみ、ニュースは訳さない) も同じ
    # 月次予算の内側で訳す。使用量の台帳は表示セットと合算する
    counts["translated_watch"], watch_stats = translate_watch_queue(store, cfg, today, log)
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
        e["id"]: e.get("market") or "JP" for e in (cfg.get("entities") or {}).get("entities", [])})
    if pruned["total"] or pruned["extraction_cache"]:
        counts["pruned"] = pruned
        log(f"[prune] 古い材料を削除しました: {pruned}")
    # 辞書から消えた alias で作られたリンクの掃除 (同名別組織への誤リンク対策)
    stale_links = store.prune_stale_links(
        (cfg.get("entities") or {}).get("entities", []))
    if stale_links:
        counts["stale_links_pruned"] = stale_links
        log(f"[prune] 陳腐化リンクを削除しました: {stale_links} 件")
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
        if not all_failed:
            partial = any(rows and all(not r["ok"] for r in rows)
                          for rows in by_source.values())
    if counts.get("selected", 0) > 0 and counts.get("error", 0) >= counts["selected"]:
        partial = True
    status = "failed" if all_failed else "partial" if partial else "ok"
    store.finish_run(run_id, counts, status, time.monotonic() - t0)
    return {"run_id": run_id, "counts": counts, "failed": all_failed,
            "partial": partial, "llm_degraded": degraded}
