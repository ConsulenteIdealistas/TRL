"""評価モデルの比較検証 (本番の評価には一切影響しない)。

同じ候補・同じ抽出・同じ材料プールで、査定 (assessment) だけをモデル別に
実行し、状態・総合点・TRL・項目点の一致度と、成功率・所要時間を比べる。
抽出を共有するのは、モデル差を「採点の判断」だけに分離するため。結果は
compare_results テーブルにだけ書き、本体の evaluations は読み取り専用。
"""

import json
import time
from datetime import datetime, timezone

from .evaluate import evaluate_one, run_extraction


def select_compare_candidates(store, limit):
    """比較対象の候補ID。long 評価の実績がある候補を新しい順に優先し、
    残りを未評価 (要旨あり・除外とニュースを除く) から新しい順に埋める。"""
    rows = store.conn.execute(
        "SELECT c.id FROM candidates c "
        "WHERE c.info_type != 'news' AND c.status != 'rejected' "
        "AND COALESCE(c.abstract, '') != '' "
        "ORDER BY (CASE WHEN EXISTS (SELECT 1 FROM evaluations e "
        "                    WHERE e.candidate_id = c.id AND e.kind = 'long') "
        "               THEN 0 ELSE 1 END), "
        "         (SELECT MAX(e2.evaluated_at) FROM evaluations e2 "
        "          WHERE e2.candidate_id = c.id AND e2.kind = 'long') DESC, "
        "         c.published DESC "
        "LIMIT ?", (limit,)).fetchall()
    return [r[0] for r in rows]


def _baseline(store, cid):
    """本番の最新 long 評価を、比較の基準 (baseline) として要約する。"""
    ev = store.latest_evaluation(cid, "long")
    if ev is None:
        return None, None
    extra = {}
    extraction = None
    try:
        extra = json.loads(ev["extra_json"] or "{}")
    except ValueError:
        pass
    try:
        extraction = json.loads(ev["extraction_json"] or "null")
    except ValueError:
        extraction = None
    trl = extra.get("trl_level") if isinstance(extra.get("trl_level"), dict) else {}
    return ({
        "status": ev["status"], "total": ev["total"],
        "trl": trl.get("level"), "model": ev["llm_model"],
    }, extraction)


def run_compare(store, cfg, clients, *, as_of, limit=100, batch=None, budget_sec=None,
                log=print, prompt_dir=None):
    """clients は (label, llm) のリスト。label は結果の行にそのまま残る
    モデルの識別名 (例: gemini/gemini-3.6-flash)。"""
    batch = batch or ("cmp-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    ids = select_compare_candidates(store, limit)
    log(f"[compare] batch={batch} candidates={len(ids)} models={[label for label, _ in clients]}")
    # 時間予算はモデルごとに等分し、各モデルの消費時間 (実測の累計) で数える。
    # 遅い・タイムアウトを繰り返すモデルが全体の予算を食って他のモデルが
    # 測れなくなる事態を防ぐ (初回実行で Cloudflare が予算の8割超を消費した)
    slice_sec = budget_sec / len(clients) if budget_sec and clients else None
    spent = {label: 0.0 for label, _ in clients}
    skipped = {label: 0 for label, _ in clients}
    # 抽出の土台が無い候補用に、lite と名の付くクライアントを抽出役にする
    extractor = next((c for c in clients if "lite" in c[0]), clients[0] if clients else None)
    done = 0
    for cid in ids:
        cand = store.get_candidate(cid)
        if cand is None:
            continue
        baseline, basis = _baseline(store, cid)
        if basis is None:
            if extractor is None:
                continue
            t0x = time.monotonic()
            try:
                basis, _meta = run_extraction(cand, cfg, extractor[1], as_of, prompt_dir)
            except Exception as e:  # 抽出できない候補は比較にならない
                log(f"[compare] {cid}: 抽出に失敗したため除外します ({type(e).__name__})")
                continue
            finally:
                if slice_sec:
                    spent[extractor[0]] += time.monotonic() - t0x
        related = store.related_news(cid, as_of)
        financials = store.related_financials(cid, as_of)
        for label, llm in clients:
            if slice_sec and spent[label] > slice_sec:
                skipped[label] += 1
                continue
            t0 = time.monotonic()
            res = evaluate_one(cand, "long", cfg, llm, as_of, extraction=basis,
                               extract_meta={"name": "extract", "version": "compare", "sha": None},
                               prompt_dir=prompt_dir, related=related, financials=financials)
            dt = time.monotonic() - t0
            if slice_sec:
                spent[label] += dt
            band = res.get("band")
            if isinstance(band, dict):
                band = band.get("label")
            err = None
            if res.get("infra_failure"):
                err = "infra: " + (res["errors"][0] if res["errors"] else "API failure")
            elif res["status"] == "error":
                err = res["errors"][0] if res["errors"] else "error"
            trl = (res.get("extra") or {}).get("trl_level") or {}
            store.add_compare_result({
                "batch": batch, "candidate_id": cid, "label": label,
                "status": res["status"], "total": res["total"], "band": band,
                "trl_level": trl.get("level") if isinstance(trl, dict) else None,
                "items": {k: (v or {}).get("score") for k, v in (res.get("items") or {}).items()},
                "seconds": round(dt, 2), "error": err,
                "baseline_status": baseline["status"] if baseline else None,
                "baseline_total": baseline["total"] if baseline else None,
                "baseline_trl": baseline["trl"] if baseline else None,
                "baseline_model": baseline["model"] if baseline else None,
                # 実際に応答したモデル (名札との照合用。フォールバック混入の検出)
                "actual_model": (f"{getattr(llm, 'name', '')}/{res['llm_model']}"
                                 if res.get("llm_model") else None),
            })
        done += 1
    for label, n in skipped.items():
        if n:
            log(f"[compare] {label}: モデル別の時間予算に達したため {n} 件を未実施")
    log(f"[compare] done candidates={done}")
    return {"batch": batch, "candidates": done, "selected": len(ids)}


def _pair_stats(rows_a, rows_b):
    """2つの結果列 (同一候補で対応) の一致度。"""
    a = {r["candidate_id"]: r for r in rows_a}
    b = {r["candidate_id"]: r for r in rows_b}
    common = [cid for cid in a if cid in b]
    if not common:
        return None
    both_scored = [cid for cid in common
                   if a[cid]["status"] in ("final", "hold") and b[cid]["status"] in ("final", "hold")]
    status_match = (sum(1 for cid in both_scored if a[cid]["status"] == b[cid]["status"])
                    / len(both_scored)) if both_scored else None
    totals = [(a[cid]["total"], b[cid]["total"]) for cid in both_scored
              if a[cid]["total"] is not None and b[cid]["total"] is not None]
    total_diff = (sum(abs(x - y) for x, y in totals) / len(totals)) if totals else None
    trls = [(a[cid]["trl_level"], b[cid]["trl_level"]) for cid in both_scored
            if a[cid]["trl_level"] is not None and b[cid]["trl_level"] is not None]
    trl_match = (sum(1 for x, y in trls if x == y) / len(trls)) if trls else None
    item_diffs = {}
    for cid in both_scored:
        for k, v in (a[cid]["items"] or {}).items():
            w = (b[cid]["items"] or {}).get(k)
            if isinstance(v, (int, float)) and isinstance(w, (int, float)):
                item_diffs.setdefault(k, []).append(abs(v - w))
    return {"n": len(common), "scored": len(both_scored), "status_match": status_match,
            "total_diff": total_diff, "trl_match": trl_match,
            "item_diff": {k: sum(v) / len(v) for k, v in sorted(item_diffs.items())}}


def _pct(x):
    return "—" if x is None else f"{100 * x:.0f}%"


def _num(x, nd=1):
    return "—" if x is None else f"{x:.{nd}f}"


def build_compare_report(store, batch):
    rows = store.compare_results(batch)
    labels = sorted({r["label"] for r in rows})
    lines = [f"# モデル比較レポート ({batch})", ""]
    if not rows:
        lines.append("結果がありません。")
        return "\n".join(lines) + "\n"
    lines += ["## モデル別の成績", "",
              "| モデル | 件数 | 採点完了 | 成功率 | 平均秒数 |",
              "|---|---|---|---|---|"]
    by_label = {}
    for label in labels:
        rs = [r for r in rows if r["label"] == label]
        by_label[label] = rs
        ok = sum(1 for r in rs if r["status"] in ("final", "hold"))
        secs = [r["seconds"] for r in rs if r["seconds"] is not None]
        lines.append(f"| {label} | {len(rs)} | {ok} | {_pct(ok / len(rs) if rs else None)} "
                     f"| {_num(sum(secs) / len(secs) if secs else None)} |")
    mism = [r for r in rows if r.get("actual_model") and r["total"] is not None
            and r["actual_model"] != r["label"]]
    if mism:
        lines += ["", f"> ⚠ 名札と実際に応答したモデルが異なる採点行が {len(mism)} 件あります"
                  " (フォールバック混入の疑い。この比較はそのまま鵜呑みにしないこと)。"]
    # 本番 (baseline) との一致: baseline は候補ごとに実績のある側の値
    lines += ["", "## 本番の評価との一致", "",
              "| モデル | 共通の採点済み | 状態一致 | 総合点の平均差 | TRL一致 |",
              "|---|---|---|---|---|"]
    for label in labels:
        rs = by_label[label]
        pseudo_b = [{"candidate_id": r["candidate_id"], "status": r["baseline_status"],
                     "total": r["baseline_total"], "trl_level": r["baseline_trl"],
                     "items": {}} for r in rs if r["baseline_status"] is not None]
        st = _pair_stats(rs, pseudo_b)
        if st is None:
            lines.append(f"| {label} | 0 | — | — | — |")
        else:
            lines.append(f"| {label} | {st['scored']} | {_pct(st['status_match'])} "
                         f"| {_num(st['total_diff'])} | {_pct(st['trl_match'])} |")
    lines += ["", "## モデル同士の一致", "",
              "| 組み合わせ | 共通の採点済み | 状態一致 | 総合点の平均差 | TRL一致 |",
              "|---|---|---|---|---|"]
    for i, la in enumerate(labels):
        for lb in labels[i + 1:]:
            st = _pair_stats(by_label[la], by_label[lb])
            if st is None:
                continue
            lines.append(f"| {la} × {lb} | {st['scored']} | {_pct(st['status_match'])} "
                         f"| {_num(st['total_diff'])} | {_pct(st['trl_match'])} |")
            if st["item_diff"]:
                diffs = ", ".join(f"{k} {_num(v)}" for k, v in st["item_diff"].items())
                lines.append(f"  - 項目別の平均差: {diffs}")
    lines += ["", "読み方: 状態一致とTRL一致が高いほど、モデルを替えても観測がぶれない。"
              "総合点の平均差は、final 同士の点のずれの大きさ。成功率は API の調子を含む。", ""]
    return "\n".join(lines)
