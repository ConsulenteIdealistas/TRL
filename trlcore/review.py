"""監査レビュー (段階B): review_inbox.json の DB 取り込みと、TRL 調整の再導出。

設計: REVIEW_DESIGN.md (監査レビューページ+調整レイヤー)。段階A
(web/audit.html・audit.js) で端末内に記録された処分・再評価予約・TRL 調整は、
「反映用 JSON」をリポジトリ直下の review_inbox.json に貼る人間経由の橋で
届く。パイプラインは開始時にここを読み、DB の追加層 (review_decisions /
reeval_queue / evaluation_adjustments) へ冪等に取り込む。

元評価・監査の行は一切書き換えない (非破壊)。調整は評価への上書きではなく
別の層であり、表示値は export の時点で scoring.compute により機械的に
再導出する (adjusted_view)。
"""

import json
from pathlib import Path

from .scoring import compute

INBOX_NAME = "review_inbox.json"
DISPOSITIONS = ("confirmed", "false_positive", "watch")
# 予約の理由はページ側の JSON に含まれない (候補 ID だけが届く) ため、
# 取り込み側で出所が分かる文面を付ける
REASON_PAGE_RESERVATION = "監査レビューでの予約"


def _is_level(v):
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 9


def _audit_id_for(store, evaluation_id, check, item, auditor):
    """処分対象の監査エントリが属する audit_results の id を返す。

    エントリの照合キーは段階A のページ側と同じ evaluation_id + check + item。
    同じ評価に複数の監査 (規則版・API 照合版等) がある場合は、ページが
    記録した auditor (監査モデル名) の一致する監査を優先する。どの監査にも
    エントリが見つからなければ None (監査の再実行で差し替わった等)。
    その場合も処分自体は記録する (対象エントリの同定だけが付かない)。"""
    fallback = None
    for r in store.conn.execute(
            "SELECT id, auditor_model, flags_json FROM audit_results "
            "WHERE evaluation_id=? ORDER BY id", (evaluation_id,)):
        try:
            flags = json.loads(r["flags_json"] or "[]")
        except ValueError:
            flags = []
        hit = any(isinstance(f, dict) and (f.get("check") or "") == (check or "")
                  and (f.get("item") or "") == (item or "") for f in flags)
        if not hit:
            continue
        if auditor and r["auditor_model"] == auditor:
            return r["id"]
        if fallback is None:
            fallback = r["id"]
    return fallback


def _evaluation(store, evaluation_id):
    if not isinstance(evaluation_id, int) or isinstance(evaluation_id, bool):
        return None
    row = store.conn.execute(
        "SELECT * FROM evaluations WHERE id=?", (evaluation_id,)).fetchone()
    return dict(row) if row else None


def import_inbox(store, inbox):
    """inbox (dict) を DB の追加層へ取り込む。-> 件数内訳の dict。

    1件の不良で全体を落とさない (手貼りのファイルが前提のため)。
    取り込めなかった分は skipped として数え、呼び出し側がログと
    run の counts に残す。再送 (同じ source_id・同じ内容) は重複として
    数えるだけで行は増えない。"""
    out = {"decisions": 0, "decisions_dup": 0, "adjustments": 0, "adjustments_dup": 0,
           "reeval": 0, "reeval_dup": 0, "skipped": 0}
    if not isinstance(inbox, dict):
        out["skipped"] += 1
        return out
    for d in inbox.get("decisions") or []:
        if not isinstance(d, dict) or not d.get("id") or d.get("disposition") not in DISPOSITIONS:
            out["skipped"] += 1
            continue
        ev = _evaluation(store, d.get("evaluation_id"))
        if ev is None:
            out["skipped"] += 1
            continue
        rec = {"source_id": str(d["id"]), "evaluation_id": ev["id"],
               "audit_id": _audit_id_for(store, ev["id"], d.get("check"),
                                         d.get("item"), d.get("auditor")),
               "check": d.get("check") or "", "item": d.get("item") or "",
               "auditor": d.get("auditor") or "", "disposition": d["disposition"],
               "note": d.get("note") or "", "decided_at": d.get("decided_at") or ""}
        if store.add_review_decision(rec):
            out["decisions"] += 1
        else:
            out["decisions_dup"] += 1
    for a in inbox.get("adjustments") or []:
        # 調整は TRL の読み替えだけ (経路B)。理由は必須 (設計書5節)。
        # TRL が未確定の評価には読み替えを設定できない (ページ側と同じ)
        if (not isinstance(a, dict) or not a.get("id")
                or (a.get("field") or "trl_level") != "trl_level"
                or not _is_level(a.get("to_value"))
                or not str(a.get("reason") or "").strip()):
            out["skipped"] += 1
            continue
        ev = _evaluation(store, a.get("evaluation_id"))
        if ev is None or ev["kind"] != "long":
            out["skipped"] += 1
            continue
        extra = json.loads(ev["extra_json"] or "{}")
        orig = (extra.get("trl_level") or {}).get("level")
        if not _is_level(orig):
            out["skipped"] += 1
            continue
        rec = {"source_id": str(a["id"]), "evaluation_id": ev["id"], "field": "trl_level",
               "from_value": a["from_value"] if _is_level(a.get("from_value")) else orig,
               "to_value": a["to_value"], "reason": str(a["reason"]).strip()}
        if store.add_adjustment(rec):
            out["adjustments"] += 1
        else:
            out["adjustments_dup"] += 1
    for q in inbox.get("reeval_queue") or []:
        if not isinstance(q, dict) or not q.get("candidate_id"):
            out["skipped"] += 1
            continue
        cid = str(q["candidate_id"])
        if store.get_candidate(cid) is None:
            out["skipped"] += 1
            continue
        if store.enqueue_reeval(str(q["id"]) if q.get("id") else None,
                                cid, REASON_PAGE_RESERVATION):
            out["reeval"] += 1
        else:
            out["reeval_dup"] += 1
    return out


def import_inbox_file(store, path, log=print):
    """path の review_inbox.json があれば取り込む。-> 件数内訳 or None。

    ファイルが無い場合は None (橋を使っていない通常の実行)。
    JSON として読めない場合も警告だけを残して None を返す: 手貼り
    ファイルの事故で収集・評価の実行自体まで落とさないため。"""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        inbox = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        log(f"::warning::[review] {p.name} を読み込めなかったため、"
            f"監査レビューの取り込みを省略します: {e}")
        return None
    out = import_inbox(store, inbox)
    log(f"[review] inbox 取り込み: 処分 {out['decisions']} 件 (重複 {out['decisions_dup']}) / "
        f"調整 {out['adjustments']} 件 (重複 {out['adjustments_dup']}) / "
        f"再評価予約 {out['reeval']} 件 (重複 {out['reeval_dup']}) / "
        f"スキップ {out['skipped']} 件")
    return out


def adjusted_view(store, cfg, ev_row):
    """評価行への有効な TRL 調整があれば、読み替え後の表示値を返す。無ければ None。

    総合点・帯域・二軸判定は、保存済みの項目点と読み替え後の TRL から
    scoring.compute で機械的に再導出する (人が点を書くのではない)。
    取り消しは「最新の調整行の to_value が元の TRL と同じ」ことで表す
    (設計書3節の統一形)。調整は evaluation_id に紐づくため、新しい評価が
    付いたら (経路A の再評価) 旧評価への調整は自然に対象から外れる。"""
    adj = store.latest_adjustment(ev_row["id"])
    if adj is None:
        return None
    extra = json.loads(ev_row["extra_json"] or "{}")
    orig = (extra.get("trl_level") or {}).get("level")
    if not _is_level(orig) or not _is_level(adj["to_value"]) or adj["to_value"] == orig:
        return None
    items = {}
    for k, v in (json.loads(ev_row["items_json"] or "{}") or {}).items():
        items[k] = v.get("score") if isinstance(v, dict) else None
    out = compute(items, ev_row["kind"], cfg, trl_level=adj["to_value"])
    return {"field": "trl_level", "from_value": orig, "to_value": adj["to_value"],
            "reason": adj["reason"], "adjusted_at": adj["created_at"],
            "status": out["status"], "total": out["total"],
            "band": (out["band"] or {}).get("label"),
            "axes": out["axes"], "business_stage": out["business_stage"],
            "total_scope": out["total_scope"]}
