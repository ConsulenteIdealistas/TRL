"""処理フロー: 収集 → 正規化 → 重複排除 → 検証 → スクリーニング → 評価 → 保存。"""

import time
import uuid
from datetime import datetime, timezone
from .errors import PromptError
from .dates import days_since, parse_date
from .http import redact
from .normalize import normalize
from .sources.pubmed import pubmed_fetch
from .sources.arxiv import arxiv_fetch
from .sources.gnews import gnews_fetch, prtimes_fetch
from .sources.trials import trials_fetch
from .sources.edinet import edinet_fetch
from .entities import entity_screen, link_entities
from .prompts import load_prompt
from .llm import llm_settings
from .evaluate import evaluate_one, input_hash


DEFAULT_FETCHERS = {"pubmed": pubmed_fetch, "arxiv": arxiv_fetch, "gnews": gnews_fetch,
                    "prtimes": prtimes_fetch, "trials": trials_fetch, "edinet": edinet_fetch}


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


def collect(store, cfg, run_id, as_of, sources, fetchers, counts, log):
    d = cfg["sources"]["defaults"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    jobs = [(sname, q) for q in cfg["sources"]["queries"] for sname in sources if q.get(sname)]

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

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(fetch, j) for j in jobs]
        results = [f.result() for f in futures]
    for (sname, q), (raws, exc, elapsed) in zip(jobs, results):
        fetched, new, dups, invalid, err = 0, 0, 0, 0, None
        log(f"[collect:start] {sname}/{q['name']}")
        try:
            if exc is not None:
                raise exc
            fetched = len(raws)
            for raw in raws:
                rec, problems = normalize(raw, now)
                if rec is None:
                    invalid += 1
                    continue
                entity_links = link_entities(rec, cfg.get("entities", {}))
                if rec["info_type"] == "news" and not entity_links:
                    # ニュースは評価の材料としてだけ使う。対象企業に紐付かないものは残さない
                    invalid += 1
                    continue
                cid, is_new = store.upsert_candidate(rec, run_id)
                store.set_entities(cid, entity_links)
                new += is_new
                dups += (not is_new)
        except Exception as e:
            err = redact(f"{type(e).__name__}: {e}")[:300]
            log(f"::warning::source {sname} / {q['name']} failed: {err}")
        store.add_run_source(run_id, sname, q["name"], fetched, new, dups, invalid, err is None, err, elapsed)
        for k, v in (("fetched", fetched), ("new", new), ("duplicates", dups), ("invalid", invalid)):
            counts[k] += v
        counts["sources_ok" if err is None else "sources_failed"] += 1
        log(f"[collect] {sname}/{q['name']}: fetched={fetched} new={new} dup={dups} invalid={invalid}"
            + (f" ERROR={err}" if err else ""))


def select_for_eval(store, cfg, today, limit, retry_failed="no"):
    sc = cfg["scoring"]["screening"]
    rows = []

    force_retry = retry_failed == "force"
    retry_enabled = (
        retry_failed is True
        or retry_failed == "yes"
        or force_retry
    )

    statuses = ["screened", "hold", "error"]

    for c in store.list_by_status(statuses):
        if c.get("info_type") == "news":
            continue  # ニュースは単体評価せず、同一企業の評価へ関連ニュースとして同梱する
        # エラー候補は、再試行が明示的に許可された場合だけ選ぶ
        if c["status"] == "error" and not store.only_infra_errors(c["id"]):
            # (旧版がAPI失敗を error として保存した候補は only_infra_errors で再試行対象に戻す)
            if not retry_enabled:
                continue
            if not force_retry and c["retry_count"] >= sc["max_retries"]:
                continue

        # 保留候補は、再評価間隔が経過するまで選ばない
        if c["status"] == "hold":
            last = parse_date((c["last_eval_at"] or "")[:10])
            if last and (today - last).days < sc["reeval_after_days"]:
                continue

        rows.append(c)

    rows.sort(key=lambda c: c["published"] or "", reverse=True)
    return rows[:limit]


def _queue_reason(res):
    if res["status"] == "error":
        return "evaluation_error: " + "; ".join(res["errors"][:3])
    crit = ",".join(res["critical_missing"])
    return "missing_items: " + ",".join(res["missing"]) + (f" (critical: {crit})" if crit else "")


def run_pipeline(store, cfg, llm, *, as_of, sources=("pubmed", "arxiv"), kinds=("long",), max_eval=None,
                 retry_failed=False, do_collect=True, fetchers=None, today=None, log=print, prompt_dir=None):
    t0 = time.monotonic()
    today = today or datetime.now(timezone.utc).date()
    run_id = new_run_id()
    store.start_run(run_id, cfg["config_version"], cfg["config_hash"], as_of.isoformat(),
                {"sources": list(sources), "kinds": list(kinds), "max_eval": max_eval,
                 "retry_failed": retry_failed, "collect": do_collect,
                 "llm": getattr(llm, "name", "none")})
    counts = {k: 0 for k in ("fetched", "new", "duplicates", "invalid", "sources_ok", "sources_failed", "screened",
                             "rejected", "selected", "final", "hold", "error", "skipped_no_llm", "llm_infra_failed")}
    log(f"[run] {run_id} as_of={as_of} config={cfg['config_version']}/{cfg['config_hash']}")
    if do_collect:
        collect(store, cfg, run_id, as_of, sources, {**DEFAULT_FETCHERS, **(fetchers or {})}, counts, log)
    for c in store.list_by_status(["new"]):
        reason = screen(c, as_of, cfg)
        store.set_status(c["id"], "rejected" if reason else "screened", reason)
        counts["rejected" if reason else "screened"] += 1
    limit = max_eval if max_eval is not None else cfg["scoring"]["screening"]["max_eval_per_run"]
    selected = select_for_eval(
    store, cfg, today, limit, retry_failed=retry_failed
)
    counts["selected"] = len(selected)
    if selected and llm is None:
        counts["skipped_no_llm"] = len(selected)
        log("[eval] LLMプロバイダ未設定のため評価をスキップ(収集のみ)。TRL_LLM_PROVIDER を設定してください。")
        selected = []
    st = llm_settings(cfg)
    phase_t0, consecutive, degraded = time.monotonic(), 0, None
    model = getattr(llm, "model", None) or ""
    try:
        extract_sha = load_prompt("extract", cfg["scoring"]["prompts"]["extract"], prompt_dir)["sha"]
    except PromptError:
        extract_sha = None  # 評価側でエラーになる。キャッシュは使わない
    workers = max(1, st.get("eval_workers", 1))

    def eval_candidate(c, extraction, meta, related, was_cached):
        # ワーカースレッドで動く。Store には触らず LLM 評価だけを行い、結果を返す。
        # 系統(long/event)は抽出を共有するため候補内では従来どおり直列に処理する
        per_kind = []
        entry_cached = was_cached
        for kind in kinds:
            eval_t0 = time.monotonic()
            res = evaluate_one(c, kind, cfg, llm, as_of, extraction, meta, prompt_dir, related=related)
            per_kind.append((kind, res, time.monotonic() - eval_t0, entry_cached))
            if res.get("infra_failure"):
                break  # この候補の残りの系統は打ち切る (従来どおり)
            extraction, meta = res["extraction"], res["prompt_versions"].get("extract")
            entry_cached = False
        return per_kind

    from concurrent.futures import ThreadPoolExecutor
    idx = 0
    while idx < len(selected) and not degraded:
        if st["phase_budget_sec"] and time.monotonic() - phase_t0 > st["phase_budget_sec"]:
            degraded = "phase_budget_exceeded"
            log(f"::warning::[eval] 評価フェーズの時間予算({st['phase_budget_sec']}秒)を超えたため打ち切ります")
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
            prepared.append((extraction, meta, related, extraction is not None))
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
            ihash = input_hash(c, as_of)
            outcomes = []
            for kind, res, eval_seconds, cached in per_kind:
                log(f"[eval:time] {c['id']} {kind}: {eval_seconds:.1f}s")
                if res["extraction"] is not None and not cached and extract_sha:
                    store.put_extraction(ihash, extract_sha, model, res["extraction"], res["prompt_versions"].get("extract"))
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
                                 retry_inc=(status == "error"))
    if degraded in ("llm_fatal_error", "llm_consecutive_failures"):
        log(f"::warning::[eval] LLM APIの失敗が続くため評価フェーズを打ち切ります ({degraded})。未評価の候補は次回に回します")
    if degraded:
        counts["llm_degraded"] = degraded
    all_failed = do_collect and counts["sources_ok"] == 0 and counts["sources_failed"] > 0
    store.finish_run(run_id, counts, "failed" if all_failed else "ok", time.monotonic() - t0)
    return {"run_id": run_id, "counts": counts, "failed": all_failed, "llm_degraded": degraded}
