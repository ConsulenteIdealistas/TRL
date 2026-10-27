"""trl.sqlite から静的サイト用の data.json を作る。Web(web/app.js)との契約は web/schema.json。"""

import json
from datetime import datetime, timezone
from pathlib import Path
from .config import ROOT
from .dates import parse_date
from .pipeline import select_for_eval


SITE_SCHEMA_VERSION = 1

# サイトに載せる未評価は「次に評価される上位」だけ。未評価の在庫を全件出すと
# 評価結果が埋もれてしまうため、書き出し側で絞る (エンジン側の選定と一致させる)
SITE_PENDING_LIMIT = 10


TECH_ITEMS = ("technology_readiness", "technological_advantage")


LEGACY_INFRA_QUEUE_PREFIX = "evaluation_error: HttpError"


def _ev_for_site(ev, cfg):
    if not ev:
        return None
    items = json.loads(ev["items_json"] or "{}")
    extra = json.loads(ev["extra_json"] or "{}") if "extra_json" in ev.keys() else {}
    required = (cfg["scoring"].get(ev["kind"]) or {}).get("required", [])
    out_items = {}
    for k, v in items.items():
        out_items[k] = {
            "score": v.get("score"), "reason": v.get("reason", ""),
            "evidence": [{"ref": e.get("ref", ""), "summary": e.get("summary", ""), "kind": e.get("kind", "")}
                         for e in (v.get("evidence") or []) if isinstance(e, dict)],
            "counter_evidence": [str(x) for x in (v.get("counter_evidence") or [])],
            "missing_info": [str(x) for x in (v.get("missing_info") or [])]}
    return {
        "status": ev["status"], "total": ev["total"], "band": ev["band"], "as_of": ev["as_of"],
        "evaluated_at": ev["evaluated_at"], "items": out_items,
        "critical_missing": [k for k in required
                             if k in out_items and out_items[k]["score"] is None
                             and not (extra.get("business_stage") == "pre_stage" and k not in TECH_ITEMS)],
        "errors": json.loads(ev["errors_json"] or "[]") if ev["status"] == "error" else [],
        "trl_level": extra.get("trl_level"),
        "direction": extra.get("direction"),
        "axes": extra.get("axes"),
        "business_stage": extra.get("business_stage"),
        "total_scope": extra.get("total_scope"),
    }


def display_status(cand, long_ev, store):
    """画面表示用の状態。final=選定可能 / review=要追加調査(必須項目は揃っている) / hold=保留(必須項目が不足) /
    pending=未評価(API失敗で未評価のものを含む) / error=内容起因のエラー / rejected=除外。"""
    if cand["status"] == "rejected":
        return "rejected"
    if long_ev is None or cand["status"] in ("new", "screened"):
        return "pending"
    st = long_ev["status"]
    if st == "final":
        return "final"
    if st == "hold":
        return "hold" if long_ev["critical_missing"] else "review"
    if st == "error":
        return "pending" if store.only_infra_errors(cand["id"]) else "error"
    return "pending"


def build_site_data(store, cfg, now=None):
    ent_names = {e["id"]: e["name"] for e in (cfg.get("entities") or {}).get("entities", [])}
    ent_tiers = {e["id"]: e.get("tier") or "core" for e in (cfg.get("entities") or {}).get("entities", [])}
    queue = {}
    for q in store.list_queue():
        if str(q["reason"] or "").startswith(LEGACY_INFRA_QUEUE_PREFIX):
            continue  # 旧版がAPI失敗を保存した分。論文側の不足ではないので表示しない
        queue[(q["candidate_id"], q["kind"])] = {
            "reason": q["reason"], "next_sources": q["next_sources"] or [], "reeval_conditions": q["reeval_conditions"] or []}
    run = store.conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    pool_as_of = parse_date(run["as_of"]) if run else None
    next_ids = set()
    if pool_as_of is not None:
        next_ids = {c["id"] for c in select_for_eval(store, cfg, pool_as_of, SITE_PENDING_LIMIT)}
    cands = []
    rows = store.conn.execute("SELECT id FROM candidates ORDER BY published DESC, id").fetchall()
    for (cid,) in rows:
        c = store.get_candidate(cid)
        if c["info_type"] == "news":
            continue  # ニュースは評価単位ではなく材料。候補一覧には出さない
        long_ev = _ev_for_site(store.latest_evaluation(cid, "long"), cfg)
        event_ev = _ev_for_site(store.latest_evaluation(cid, "event"), cfg)
        if long_ev is None and event_ev is None and cid not in next_ids:
            continue  # 評価済みと、次に評価される上位だけを載せる
        seen, ents = set(), []
        for e in c["entities"]:
            key = (e["entity_id"], e["strength"], e["product_name"])
            if key not in seen:
                seen.add(key)
                ents.append({"entity_id": e["entity_id"], "name": ent_names.get(e["entity_id"], e["entity_id"]),
                             "strength": e["strength"], "product_name": e["product_name"] or None,
                             "tier": ent_tiers.get(e["entity_id"], "core")})
        cands.append({
            "id": cid, "title": c["title"], "source": c["source"], "published": c["published"], "url": c["url"],
            "abstract": c["abstract"] or "", "fields": c["fields"],
            "display_status": display_status(c, long_ev, store),
            "next_up": cid in next_ids and long_ev is None and event_ev is None,
            "long": long_ev, "event": event_ev, "entities": ents,
            "related_news": store.related_news(cid, pool_as_of) if pool_as_of else [],
            "related_financials": store.related_financials(cid, pool_as_of) if pool_as_of else [],
            "queue": {k: queue[(cid, k)] for k in ("long", "event") if (cid, k) in queue}})
    run_out = None
    if run:
        counts = json.loads(run["counts_json"] or "{}")
        run_out = {"id": run["run_id"], "status": run["status"], "as_of": run["as_of"],
                   "finished_at": run["finished_at"], "duration_sec": run["duration_sec"], "counts": counts,
                   "llm_degraded": counts.get("llm_degraded")}
    long_w = cfg["scoring"]["long"]["weights"]
    return {
        "schema_version": SITE_SCHEMA_VERSION,
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "run": run_out,
        "config": {"version": cfg["config_version"], "hash": cfg["config_hash"],
                   "long_weights": long_w,
                   "item_groups": {"technology": [k for k in long_w if k in TECH_ITEMS],
                                   "business": [k for k in long_w if k not in TECH_ITEMS]}},
        "candidates": cands}


def export_site(store, cfg, out_dir, web_dir=None, now=None):
    """out_dir に data.json と web/ の静的ファイルを書き出す(GitHub Pages にそのまま配備できる)。"""
    import shutil
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = build_site_data(store, cfg, now)
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    web = Path(web_dir) if web_dir else ROOT / "web"
    if web.is_dir():
        for p in web.iterdir():
            if p.is_file() and p.name != "schema.json":
                shutil.copy2(p, out / p.name)
    return data
