"""trl.sqlite から静的サイト用の data.json を作る。Web(web/app.js)との契約は web/schema.json。"""

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus
from .audit import build_audit_catalog
from .config import ROOT
from .dates import parse_date
from .http import redact
from .barriers import barriers_for_store_evaluation, load_barrier_inputs
from .pipeline import select_for_eval
from .review import adjusted_view
from .summarize import PAPER_SOURCES, extract_summary
from .tiers import compute_derived_tiers, tier_distribution


SITE_SCHEMA_VERSION = 3

# サイトに載せる未評価は「次に評価される上位」だけ。未評価の在庫を全件出すと
# 評価結果が埋もれてしまうため、書き出し側で絞る (エンジン側の選定と一致させる)
SITE_PENDING_LIMIT = 10


TECH_ITEMS = ("technology_readiness", "technological_advantage")


LEGACY_INFRA_QUEUE_PREFIX = "evaluation_error: HttpError"


def _news_for_site(n):
    """関連ニュースの表示用に、タイトル検索URLを添える。

    Google News の記事URL(news.google.com のトークン)は直接開くとブロックされるため、
    表示側ではタイトル検索で元記事に辿り着けるようにする。媒体サイトURL (media_url)
    はフィードに含まれていれば収集時に保存してある。
    """
    out = dict(n)
    out["search_url"] = "https://www.google.com/search?q=" + quote_plus(n.get("title") or "")
    out["via_google_news"] = "news.google.com" in (n.get("url") or "")
    return out


def _split_missing(texts):
    """missing_info を通常の不足と「結果待ち：」に分ける(接頭辞は表示用に外す)。"""
    miss, wait = [], []
    for x in texts or []:
        s = str(x)
        if s.startswith("結果待ち："):
            wait.append(s[len("結果待ち："):].strip())
        elif s.startswith("結果待ち:"):
            wait.append(s[len("結果待ち:"):].strip())
        else:
            miss.append(s)
    return miss, wait


def _gen_config(extra, ev):
    """評価の世代 (規程版) の正本を返す。

    評価保存時に固定された as_evaluated の config_version を優先する
    (評価行の config_version は recalc で現行版に付け替わりうるため。
    ISSUES-2 の G-1 / G-2)。as_evaluated が無い古い評価は評価行の
    config_version にフォールバックし、どちらも無ければ None。
    """
    ae = extra.get("as_evaluated")
    if isinstance(ae, dict) and ae.get("config_version"):
        return str(ae["config_version"])
    if "config_version" in ev.keys() and ev["config_version"]:
        return str(ev["config_version"])
    return None


def _gen_for_site(ev, extra, cfg):
    """評価の世代 (表示用)。prompts は prompt_versions_json から版文字列
    だけを抜いたもの。current は export 時点の現行版と一致するかで、
    版数比較の規則は表示側に持たせない (tiers の reasons と同じ流儀)。
    世代が取れなければ評価ごと None (バッジを出さない)。"""
    config = _gen_config(extra, ev)
    if not config:
        return None
    prompts = {}
    raw = {}
    if "prompt_versions_json" in ev.keys():
        try:
            raw = json.loads(ev["prompt_versions_json"] or "{}")
        except (TypeError, ValueError):
            raw = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            ver = v.get("version") if isinstance(v, dict) else v
            if isinstance(ver, str) and ver:
                prompts[str(k)] = ver
    return {"config": config, "prompts": prompts,
            "current": config == cfg["config_version"]}


def _ev_for_site(ev, cfg):
    if not ev:
        return None
    items = json.loads(ev["items_json"] or "{}")
    extra = json.loads(ev["extra_json"] or "{}") if "extra_json" in ev.keys() else {}
    required = (cfg["scoring"].get(ev["kind"]) or {}).get("required", [])
    out_items = {}
    for k, v in items.items():
        miss, wait = _split_missing(v.get("missing_info"))
        out_items[k] = {
            "score": v.get("score") if isinstance(v.get("score"), (int, float))
                     and not isinstance(v.get("score"), bool) else None,
            "reason": v.get("reason", ""),
            "evidence": [{"ref": e.get("ref", ""), "summary": e.get("summary", ""), "kind": e.get("kind", "")}
                         for e in (v.get("evidence") or []) if isinstance(e, dict)],
            "counter_evidence": [str(x) for x in (v.get("counter_evidence") or [])],
            "missing_info": miss, "awaiting_info": wait}
        # トレーサビリティ (v3 世代の任意フィールド。旧世代では欠落したまま)
        for f in ("basis_claim_ids", "constraint_codes", "deduction_codes", "missing_codes"):
            if isinstance(v.get(f), list) and v[f]:
                out_items[k][f] = [str(x) for x in v[f]]
    trl = extra.get("trl_level")
    if isinstance(trl, dict) and trl.get("missing_info"):
        trl = dict(trl)
        _m, _w = _split_missing(trl.get("missing_info"))
        trl["missing_info"] = _m
        trl["awaiting_info"] = _w
    total_orig, band_orig = _as_evaluated(extra, ev)
    out = {
        "status": ev["status"], "total": ev["total"], "band": ev["band"], "as_of": ev["as_of"],
        # 評価当時の総合点・帯域 (重み改訂で再計算されても変わらない非破壊の記録)
        "total_orig": total_orig, "band_orig": band_orig,
        "evaluated_at": ev["evaluated_at"], "items": out_items,
        "critical_missing": [k for k in required
                             if k in out_items and out_items[k]["score"] is None
                             and not (extra.get("business_stage") == "pre_stage" and k not in TECH_ITEMS)],
        # エラー文は公開ページに出るため、収集・評価時の例外文字列に資格情報が
        # 混ざっていた場合に備えてログと同じ秘匿処理を通す
        "errors": [redact(str(e)) for e in json.loads(ev["errors_json"] or "[]")] if ev["status"] == "error" else [],
        "trl_level": trl,
        # 技術ごとの TRL 内訳 (v3 世代の任意フィールド。旧世代では None)
        "trl_levels": extra.get("trl_levels") if isinstance(extra.get("trl_levels"), list) else None,
        "direction": extra.get("direction") if ev["status"] != "error" else None,  # error 評価の方向性は出さない (一覧・詳細で共通)
        "axes": extra.get("axes"),
        "business_stage": extra.get("business_stage"),
        "total_scope": extra.get("total_scope"),
        # 評価の来歴: 実際に採点したプロバイダとモデルをサイトでも判別できるようにする
        "llm_provider": ev["llm_provider"] if "llm_provider" in ev.keys() else None,
        "llm_model": ev["llm_model"] if "llm_model" in ev.keys() else None,
        # 評価の世代 (その評価に使われた規程・プロンプトの版)。正本の
        # 取り方は _gen_for_site を参照。世代が取れない評価は None
        "gen": _gen_for_site(ev, extra, cfg),
    }
    return out


# 保留の主因の切り分けに使う、LLM が理由文・不足文へ明記する「企業を特定
# できない」系の言い回し。プロンプトの規則 (企業が一意に特定できない場合は
# 企業固有の項目を算出しない) に対応する明記だけを見る
_UNIDENTIFIED_PATTERNS = ("特定できない", "特定されていない", "特定できていない",
                          "一意に定まらない", "一意に特定できない")


def _hold_cause(ev):
    """保留の主因の内訳 (表示用): 対象企業を資料から一意に特定できないケース
    (unidentified) と、特定はできるが必須項目の根拠が資料に無いケース
    (no_evidence) を分ける。どちらが多いかで打ち手が違う (材料の同定か、
    材料の量か) ため、観測のために切り分ける。"""
    for v in ev["items"].values():
        text = (v.get("reason") or "") + " " + " ".join(v.get("missing_info") or [])
        if any(p in text for p in _UNIDENTIFIED_PATTERNS):
            return "unidentified"
    return "no_evidence"


def _as_evaluated(extra, ev):
    """評価当時の総合点・帯域を返す。保存時の固定 (as_evaluated) があれば
    それ、無ければ再計算の来歴 (recalculated の旧値)、どちらも無ければ
    現在の値 (再計算されていない評価では、それが当時の値そのもの)。
    final 以外 (総合点が無い評価) では (None, None)。"""
    ae = extra.get("as_evaluated")
    if isinstance(ae, dict) and ae.get("total") is not None:
        return ae.get("total"), ae.get("band")
    rec = extra.get("recalculated")
    if isinstance(rec, dict) and rec.get("old_total") is not None:
        return rec.get("old_total"), rec.get("old_band")
    return ev["total"], ev["band"]


def _prev_for_site(ev):
    """前回評価の要約 (前回比の表示用)。状態・総合点・TRL・項目点だけを持ち、
    根拠本文は持たない (data.json を太らせないため)。"""
    if not ev:
        return None
    items = json.loads(ev["items_json"] or "{}")
    extra = json.loads(ev["extra_json"] or "{}") if ev.get("extra_json") else {}
    trl = extra.get("trl_level") if isinstance(extra.get("trl_level"), dict) else {}
    return {
        "evaluated_at": ev["evaluated_at"], "status": ev["status"],
        "total": ev["total"], "band": ev["band"],
        # 前回評価の世代 (規程版)。表示中の評価と異なる場合に、前回比へ
        # 「世代が異なる」注記を出すために使う (版の正本は _gen_config)
        "gen_config": _gen_config(extra, ev),
        "trl_level": trl.get("level"),
        "items": {k: (v.get("score") if isinstance(v, dict)
                      and isinstance(v.get("score"), (int, float))
                      and not isinstance(v.get("score"), bool) else None)
                  for k, v in items.items()},
    }


def display_status(cand, long_ev, store, event_ev=None):
    """画面表示用の状態。final=選定可能 / review=要追加調査(必須項目は揃っている) / hold=保留(必須項目が不足) /
    pending=未評価(API失敗で未評価のものを含む) / error=内容起因のエラー / rejected=除外。"""
    if cand["status"] == "rejected":
        return "rejected"
    if long_ev is None or cand["status"] in ("new", "screened"):
        # イベント評価だけがある候補は「未評価」ではなく要確認として出す
        if long_ev is None and event_ev is not None and cand["status"] not in ("new", "screened"):
            return "review"
        return "pending"
    st = long_ev["status"]
    if st == "final":
        return "final"
    if st == "hold":
        return "hold" if long_ev["critical_missing"] else "review"
    if st == "error":
        return "pending" if store.only_infra_errors(cand["id"]) else "error"
    return "pending"


def _translation_src(c):
    """DB の translation_src (JSON文字列) を dict で返す。壊れていれば None。"""
    raw = c.get("translation_src")
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    return d if isinstance(d, dict) and d else None


def _audit_entries(flags):
    """監査エントリ (flags_json の中身) を表示用に整形する。

    規則版は verdict ("flag"=要確認 / "gray") と rule (当たった規則名) を
    持つ。LLM 版 (休眠) はどちらも持たないため、build_audit_report の
    規約どおり verdict は "flag" (要確認) として扱い、rule は None にする。
    """
    out = []
    for f in flags or []:
        if not isinstance(f, dict):
            continue
        v = f.get("verdict")
        out.append({"check": f.get("check"), "item": f.get("item"),
                    "detail": f.get("detail") or "",
                    "verdict": v if v in ("flag", "gray") else "flag",
                    "rule": f.get("rule")})
    return out


def _audits_by_eval(store):
    """kind ごとの最新評価に紐づく監査結果を {evaluation_id: [監査, ...]} で返す。

    監査は evaluation_id 単位で記録される (規則版・LLM 版の双方)。
    評価が更新されて最新でなくなった評価への監査は含めない
    (レビューページの処分・調整は常に表示中の評価に対するものにする)。
    """
    rows = store.conn.execute(
        "SELECT a.* FROM audit_results a JOIN evaluations e ON e.id = a.evaluation_id "
        "WHERE e.id = (SELECT MAX(e2.id) FROM evaluations e2 "
        "WHERE e2.candidate_id = e.candidate_id AND e2.kind = e.kind) "
        "ORDER BY a.id").fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        try:
            flags = json.loads(d.get("flags_json") or "[]")
        except json.JSONDecodeError:
            flags = []
        out.setdefault(d["evaluation_id"], []).append({
            "evaluation_id": d["evaluation_id"],
            "auditor": d.get("auditor_model") or "",
            "provider": d.get("auditor_provider") or "",
            "version": d.get("prompt_version") or "",
            "entries": _audit_entries(flags),
            "summary": d.get("summary") or "",
            "created_at": d.get("created_at") or ""})
    return out


# ウォッチ・参考ページ (watch.html) 用の同梱上限。一次情報は企業の厚みを
# 見る主対象として多めに、ニュース系はコピー導線が目的のため少数に絞る
WATCH_PRIMARY_CAP = 15
WATCH_NEWS_CAP = 5
WATCH_NEWS_SOURCES = ("gnews", "prtimes")


def _unmet_reasons(result):
    """導出結果から「未充足の理由」の detail 文だけを取り出す (表示用)。

    文面は導出 (tiers.py) の定義をそのまま使う。評価が無いために
    「判定対象外」になっている条件は、理由として重複するので除く。
    """
    if not result:
        return []
    return [r["detail"] for r in result.get("reasons") or []
            if not r.get("ok") and "判定対象外" not in (r.get("detail") or "")]


def _watch_companies(store, cfg, ent_derived=None, ent_reasons=None):
    """tier が watch / reference の企業と、そのプール材料を返す (表示専用)。

    評価対象ではないため本体の candidates には載らない材料を、企業単位で
    一次情報優先に並べて watch.html から読めるようにする。材料は新しい順、
    一次情報 (論文・治験・開示) を前段、ニュース系を後段に置く。
    derived_tier は導出レイヤー (tiers) の層で、基礎 tier から決定的に付く。"""
    ents = [e for e in (cfg.get("entities") or {}).get("entities", [])
            if (e.get("tier") or "core") in ("watch", "reference")]
    by_id = {}
    for e in ents:
        by_id[e["id"]] = {"id": e["id"], "name": e["name"],
                          "market": e.get("market") or "JP",
                          "sector": e.get("sector"),
                          "tags": ([t for t in e["tags"] if isinstance(t, str)]
                                   if isinstance(e.get("tags"), list) else []),
                          "stage": e.get("stage"), "tier": e.get("tier") or "core",
                          "derived_tier": (ent_derived or {}).get(e["id"]),
                          "derived_reasons": (ent_reasons or {}).get(e["id"], []),
                          "ticker": e.get("ticker"),
                          "counts": {"primary": 0, "news": 0},
                          "latest": None, "_primary": [], "_news": []}
    if by_id:
        rows = store.conn.execute(
            "SELECT ce.entity_id, ce.strength, c.id, c.source, c.title, c.title_ja,"
            " c.abstract, c.abstract_ja, c.url, c.published"
            " FROM candidate_entities ce JOIN candidates c ON c.id = ce.candidate_id"
            " WHERE c.status != 'rejected'"
            " ORDER BY ce.entity_id, (c.published IS NULL), c.published DESC, c.id").fetchall()
        for eid, strength, cid, source, title, title_ja, abstract, abstract_ja, url, published in rows:
            comp = by_id.get(eid)
            if comp is None:
                continue
            mat = {"id": cid, "source": source, "title": title or "",
                   "title_ja": title_ja or None, "abstract": abstract or "",
                   "abstract_ja": abstract_ja or None, "url": url or "",
                   "published": published, "strength": strength or "strong"}
            if source in PAPER_SOURCES:
                summ = extract_summary(title, abstract, abstract_ja)
                if summ:
                    mat["summary"] = summ
            bucket = "_news" if source in WATCH_NEWS_SOURCES else "_primary"
            comp["counts"]["news" if bucket == "_news" else "primary"] += 1
            if published and (comp["latest"] is None or published > comp["latest"]):
                comp["latest"] = published
            if len(comp[bucket]) < (WATCH_NEWS_CAP if bucket == "_news" else WATCH_PRIMARY_CAP):
                comp[bucket].append(mat)
    out = []
    for e in ents:
        comp = by_id[e["id"]]
        comp["materials"] = comp.pop("_primary") + comp.pop("_news")
        out.append(comp)
    return out


def build_site_data(store, cfg, now=None):
    ent_names = {e["id"]: e["name"] for e in (cfg.get("entities") or {}).get("entities", [])}
    ent_tiers = {e["id"]: e.get("tier") or "core" for e in (cfg.get("entities") or {}).get("entities", [])}
    ent_markets = {e["id"]: e.get("market") or "JP" for e in (cfg.get("entities") or {}).get("entities", [])}
    ent_sectors = {e["id"]: e.get("sector") for e in (cfg.get("entities") or {}).get("entities", [])}
    ent_tags = {e["id"]: ([t for t in e["tags"] if isinstance(t, str)] if isinstance(e.get("tags"), list) else [])
                for e in (cfg.get("entities") or {}).get("entities", [])}
    ent_stages = {e["id"]: e.get("stage") for e in (cfg.get("entities") or {}).get("entities", [])}
    queue = {}
    for q in store.list_queue():
        if str(q["reason"] or "").startswith(LEGACY_INFRA_QUEUE_PREFIX):
            continue  # 旧版がAPI失敗を保存した分。論文側の不足ではないので表示しない
        queue[(q["candidate_id"], q["kind"])] = {
            "reason": q["reason"], "next_sources": q["next_sources"] or [], "reeval_conditions": q["reeval_conditions"] or []}
    # 表示の基準にする実行は「終了した最新のもの」。異常終了で 'running' のままの行を
    # 掴むと、その実行の as_of や件数でサイトが組まれてしまう
    run = store.conn.execute(
        "SELECT * FROM runs WHERE status != 'running' ORDER BY started_at DESC LIMIT 1").fetchone()
    if run is None:
        run = store.conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    pool_as_of = parse_date(run["as_of"]) if run else None
    next_ids = set()
    if pool_as_of is not None:
        next_ids = {c["id"] for c in select_for_eval(store, cfg, pool_as_of, SITE_PENDING_LIMIT)}
    # 評価の有無は先に一括で引き、掲載する候補だけを本体ごと読み込む
    # (全候補を1件ずつ読むと候補数×数クエリになり、候補の増加に比例して重くなる)
    ev_kinds = {}
    for cid, kind in store.conn.execute(
            "SELECT candidate_id, kind FROM evaluations GROUP BY candidate_id, kind"):
        ev_kinds.setdefault(cid, set()).add(kind)
    eval_counts = store.eval_counts_long()
    audits = _audits_by_eval(store)
    # tier 導出レイヤー (tiers/v1): 辞書は不変のまま、企業ごとの導出 tier と
    # 4層の分布・警告を data.json に同梱する (追加フィールド方式)
    derived = compute_derived_tiers(store, cfg.get("entities"))
    # 関門導出レイヤー (barriers/v1): 一次情報 (治験・財務) からの決定的な
    # 導出で表示専用。入力は export 時に DB から一括で読む
    barrier_inputs = load_barrier_inputs(store)
    ent_derived = {eid: r["derived_tier"] for eid, r in derived.items()}
    ent_reasons = {eid: _unmet_reasons(r) for eid, r in derived.items()}
    cands = []
    rows = store.conn.execute("SELECT id, info_type, status FROM candidates ORDER BY published DESC, id").fetchall()
    for cid, info_type, cand_status in rows:
        if info_type == "news":
            continue  # ニュースは評価単位ではなく材料。候補一覧には出さない
        kinds_seen = ev_kinds.get(cid) or ()
        if "long" not in kinds_seen and "event" not in kinds_seen and cid not in next_ids and cand_status != "rejected":
            continue  # 評価済みと、次に評価される上位だけを載せる (除外は理由つきで別掲する)
        c = store.get_candidate(cid)
        long_row = store.latest_evaluation(cid, "long")
        event_row = store.latest_evaluation(cid, "event")
        long_ev = _ev_for_site(long_row, cfg)
        event_ev = _ev_for_site(event_row, cfg)
        # 前回比 (観測の差分): 表示中の評価が採点結果 (final/hold) のときだけ、
        # 直前の比較可能な評価の要約を付ける。再評価が無い候補には付かない
        if long_ev is not None and long_row["status"] in ("final", "hold"):
            long_ev["prev"] = _prev_for_site(store.prev_evaluation(cid, "long", long_row["id"]))
        if event_ev is not None and event_row["status"] in ("final", "hold"):
            event_ev["prev"] = _prev_for_site(store.prev_evaluation(cid, "event", event_row["id"]))
        if long_ev is not None and long_row["status"] == "hold":
            long_ev["hold_cause"] = _hold_cause(long_ev)
        # 関門 (barriers/v1): 付くのは long final の評価だけ (DESIGN-V2
        # §2-3)。hold は代表 TRL の評価として確定していないため対象外で、
        # event 評価にも付かない
        if long_ev is not None and long_row["status"] == "final":
            long_ev["barriers"] = barriers_for_store_evaluation(
                store, barrier_inputs, cid, long_row)
        # TRL 調整 (監査レビュー経路B・段階B): 有効な調整があれば、読み替え後の
        # TRL で機械的に再導出した値を併記用に同梱する。元の値 (total・band・
        # trl_level) は書き換えず、表示側が「調整後の値 (元: ○)」の形で出す。
        # 調整は最新の long 評価にだけ付く (旧評価への調整は対象から外れる)
        if long_row is not None:
            adj = adjusted_view(store, cfg, long_row)
            if adj:
                long_ev["adjustment"] = adj
        seen, ents = set(), []
        for e in c["entities"]:
            key = (e["entity_id"], e["strength"], e["product_name"])
            if key not in seen:
                seen.add(key)
                ents.append({"entity_id": e["entity_id"], "name": ent_names.get(e["entity_id"], e["entity_id"]),
                             "strength": e["strength"], "product_name": e["product_name"] or None,
                             "tier": ent_tiers.get(e["entity_id"], "core"),
                             "derived_tier": ent_derived.get(e["entity_id"]),
                             "derived_reasons": ent_reasons.get(e["entity_id"], []),
                             "market": ent_markets.get(e["entity_id"], "JP"),
                             "sector": ent_sectors.get(e["entity_id"]),
                             "tags": ent_tags.get(e["entity_id"], []),
                             "stage": ent_stages.get(e["entity_id"])})
        cand = {
            "id": cid, "title": c["title"], "source": c["source"], "published": c["published"], "url": c["url"],
            "abstract": c["abstract"] or "", "fields": c["fields"],
            "title_ja": c.get("title_ja") or None, "abstract_ja": c.get("abstract_ja") or None,
            "translation_src": _translation_src(c),
            "screen_reason": c.get("screen_reason") or None,
            "retrieved_at": c.get("retrieved_at") or None,
            "display_status": display_status(c, long_ev, store, event_ev),
            "eval_count": eval_counts.get(cid, 0),
            "next_up": cid in next_ids and long_ev is None and event_ev is None,
            "long": long_ev, "event": event_ev, "entities": ents,
            "related_news": [_news_for_site(n) for n in store.related_news(cid, pool_as_of)] if pool_as_of else [],
            "related_financials": store.related_financials(cid, pool_as_of) if pool_as_of else [],
            "queue": {k: queue[(cid, k)] for k in ("long", "event") if (cid, k) in queue}}
        # 最新の long 評価への監査 (規則版・LLM 版) があれば同梱する
        # (監査レビューページ用)。監査が無い候補には付けない
        if long_row is not None:
            au = audits.get(long_row["id"])
            if au:
                cand["audit"] = au
        # イベント評価の監査も同じ形で同梱する (監査ページのイベント表示用)
        if event_row is not None:
            au = audits.get(event_row["id"])
            if au:
                cand["audit_event"] = au
        # 論文の要点 (抄録からの決定的な抽出、表示専用)。対象でない候補には付けない
        if c["source"] in PAPER_SOURCES:
            summ = extract_summary(c["title"], c["abstract"], c.get("abstract_ja"))
            if summ:
                cand["summary"] = summ
        cands.append(cand)
    run_out = None
    if run:
        counts = json.loads(run["counts_json"] or "{}")
        run_out = {"id": run["run_id"], "status": run["status"], "as_of": run["as_of"],
                   "finished_at": run["finished_at"], "duration_sec": run["duration_sec"], "counts": counts,
                   "llm_degraded": counts.get("llm_degraded")}
    long_w = cfg["scoring"]["long"]["weights"]
    # 除外 (screening で落ちた候補) の総数。一覧にも理由つきで含めるが、
    # DB 側の総数を参照用に残す
    summary = {"rejected": store.conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE status = 'rejected'").fetchone()[0],
        # 導出 tier の分布 (社数・シェア・目標との差・±5pt 枠外の警告)。
        # 警告は D-3 により出すだけで、層の自動調整はしない
        "tiers": tier_distribution(derived)}
    return {
        "schema_version": SITE_SCHEMA_VERSION,
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "run": run_out,
        "summary": summary,
        "config": {"version": cfg["config_version"], "hash": cfg["config_hash"],
                   "long_weights": long_w,
                   "item_groups": {"technology": [k for k in long_w if k in TECH_ITEMS],
                                   "business": [k for k in long_w if k not in TECH_ITEMS]},
                   # 参考項目 (規程第13条): 総合点・必須判定に含めない項目。
                   # 表示側が「参考」として分離し、不足の集計からも外すために使う
                   "reference_items": list(cfg["scoring"]["long"].get("reference") or [])},
        "candidates": cands,
        # 監査チェックのカタログ (名前・何を見ているか・閾値・出典)。正本は
        # 監査側 (audit.build_audit_catalog) で、criteria.html の一覧と
        # audit.js のチェック名がこの同梱分を読む (転記の乖離を防ぐ)
        "audit_catalog": build_audit_catalog(),
        "watch_companies": _watch_companies(store, cfg, ent_derived, ent_reasons)}


def export_site(store, cfg, out_dir, web_dir=None, now=None):
    """out_dir に data.json と web/ の静的ファイルを書き出す(GitHub Pages にそのまま配備できる)。"""
    import shutil
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = build_site_data(store, cfg, now)
    # 整合性ガード: 評価済み・除外の候補は必ず出力に含まれるはずで、欠ける
    # のは生成側の不具合にほかならない。壊れた data.json を静かに公開せず、
    # export を失敗させて前回の公開を守る
    base = store.conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE info_type != 'news' "
        "AND (status = 'rejected' OR id IN (SELECT candidate_id FROM evaluations))").fetchone()[0]
    if base and len(data["candidates"]) < base:
        raise RuntimeError(
            f"export integrity: evaluated/rejected candidates={base} but output={len(data['candidates'])}")
    # tier 導出のスナップショット記録は export では行わない (ISSUES-2 の
    # T-3): 記録はパイプライン実行の末尾 (run_pipeline) だけが担う。
    # site 専用ジョブ経由の export は DB を保存しないため、ここで書いても
    # 蒸発するうえ、「公開された中間状態が履歴に残らない」穴の温床になる。
    # export は経路によらず読み取り専用に保つ (表示用の導出計算自体は
    # build_site_data が行う)。
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    web = Path(web_dir) if web_dir else ROOT / "web"
    if web.is_dir():
        for p in web.iterdir():
            if p.is_file() and p.name != "schema.json":
                shutil.copy2(p, out / p.name)
    return data
