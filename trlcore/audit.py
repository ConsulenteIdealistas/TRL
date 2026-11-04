"""評価監査 (監査役) のパイロット実装。

完了済みの長期評価 (kind='long') を別のモデル (監査役) が読み直し、評価
ルールへの抵触の疑いを「フラグ」として audit_results に記録するだけの
追加層。本体評価 (点・状態・TRL) には一切触れず、再評価も引き起こさない。
監査プロンプトは scoring.json の prompts には登録しない (版はこの
モジュールの AUDIT_PROMPT_VERSION で管理する)。そのため監査プロンプトは
評価の世代印 (config_hash) に一切関与せず、評価側の仕組みに手を入れずに
監査だけを改訂できる。設計: ~/workspace/trl-audit/DESIGN.md。
"""
import json
import time
from datetime import datetime, timezone

from .evaluate import _used_model
from .llm import extract_json
from .prompts import load_prompt, render
from .scoring import TECH_ITEMS

AUDIT_CHECKS = ("A1", "A2", "A3", "A4", "A5", "A6", "A7")

# 監査プロンプトの版。scoring.json の prompts には入れず (入れると評価の
# 世代印 config_hash に混ざるため)、監査側だけで管理する。版を上げるときは
# audit.v<N>.md を用意してこの値を変える。過去の版で監査済みの評価は、
# 冪等キー (prompt_sha) が変わるため新しい版で再び監査対象になる。
AUDIT_PROMPT_VERSION = "v1"


def _loads(s, default):
    if s is None:
        return default
    if not isinstance(s, str):
        return s
    try:
        return json.loads(s)
    except ValueError:
        return default


def build_audit_input(store, ev_row, cfg=None):
    """評価1件分の監査入力 JSON (設計書3節) を、保存済みレコードから組む。
    材料の原文は載せない (保存済みの抽出・項目情報だけを使う)。
    cfg があれば企業名・セクターの解決と critical_missing の導出に使う。
    critical_missing の導出はサイト表示 (site._ev_for_site) と同じ規則:
    必須項目のうち score が None のもの。ただし事業軸が段階未達
    (pre_stage) のときは技術軸以外の事業項目を除く。"""
    ev = dict(ev_row)
    items = _loads(ev.get("items_json"), {})
    if not isinstance(items, dict):
        items = {}
    extra = _loads(ev.get("extra_json"), {})
    if not isinstance(extra, dict):
        extra = {}
    extraction = _loads(ev.get("extraction_json"), None)
    cand = store.get_candidate(ev.get("candidate_id")) or {}

    ent_cfg = {}
    if cfg and cfg.get("entities"):
        ent_cfg = {e["id"]: e for e in cfg["entities"].get("entities", [])}
    companies, sector = [], None
    for link in cand.get("entities") or []:
        meta = ent_cfg.get(link.get("entity_id")) or {}
        companies.append({"name": meta.get("name") or link.get("entity_id"),
                          "sector": meta.get("sector")})
        if sector is None and meta.get("sector"):
            sector = meta["sector"]

    critical_missing = extra.get("critical_missing")
    if not isinstance(critical_missing, list):
        critical_missing = []
        if cfg:
            required = ((cfg["scoring"].get("long") or {}).get("required")) or []
            pre_stage = extra.get("business_stage") == "pre_stage"
            critical_missing = [
                k for k in required
                if isinstance(items.get(k), dict) and items[k].get("score") is None
                and not (pre_stage and k not in TECH_ITEMS)]

    trl = extra.get("trl_level")
    return {
        "evaluation_id": ev.get("id"),
        "candidate_id": ev.get("candidate_id"),
        "title": cand.get("title"),
        "sector": sector,
        "companies": companies,
        "status": ev.get("status"),
        "total": ev.get("total"),
        "band": ev.get("band"),
        "critical_missing": critical_missing,
        "items": items,
        "trl_level": trl if isinstance(trl, dict) else None,
        "extraction": extraction,
    }


def select_unaudited(store, prompt_sha, model, limit=100):
    """未監査の long 評価を古い順に返す。監査済みの判定は冪等キー
    (evaluation_id, prompt_sha, auditor_model) の一致で行うため、監査
    プロンプトが改訂される (sha が変わる) か監査モデルが変われば、
    過去に監査した評価も再び対象になる。"""
    rows = store.conn.execute(
        "SELECT e.* FROM evaluations e WHERE e.kind = 'long'"
        " AND NOT EXISTS (SELECT 1 FROM audit_results a"
        "   WHERE a.evaluation_id = e.id AND a.prompt_sha = ?"
        "   AND a.auditor_model = ?)"
        " ORDER BY e.evaluated_at ASC, e.id ASC LIMIT ?",
        (prompt_sha, model, int(limit))).fetchall()
    return [dict(r) for r in rows]


def _valid_flags(obj):
    """監査応答の形を検証する。通過すれば flags のリスト、駄目なら None。
    flags の要素が1つでも不正なら応答全体を不正として扱う (一部だけ
    黙って捨てると、パイロットで測りたいプロンプトの質が見えなくなる)。"""
    if not isinstance(obj, dict) or not isinstance(obj.get("flags"), list):
        return None
    for f in obj["flags"]:
        if not isinstance(f, dict) or f.get("check") not in AUDIT_CHECKS:
            return None
        if not isinstance(f.get("item"), str) or not isinstance(f.get("detail"), str):
            return None
    return obj["flags"]


def run_audit(store, cfg, client, *, limit=100, budget_sec=None, batch=None,
              log=print, prompt_dir=None):
    """未監査の long 評価を順に監査し、結果を audit_results に記録する。
    API 失敗・形式不正の評価は記録せず未監査のまま残し、処理は続ける
    (1件の失敗で全体を落とさない)。budget_sec はモデル消費時間の累計で
    判定し、到達したら打ち切る (None/0 は無制限)。"""
    batch = batch or "aud-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    p = load_prompt("audit", AUDIT_PROMPT_VERSION, prompt_dir)
    model_key = getattr(client, "model", None)
    rows = select_unaudited(store, p["sha"], model_key, limit)
    audited = flagged = failed = 0
    checks = {}
    spent = 0.0
    for row in rows:
        if budget_sec and spent > budget_sec:
            log(f"audit time budget reached ({spent:.0f}s > {budget_sec}s); stopping")
            break
        cid = row["candidate_id"]
        inp = build_audit_input(store, row, cfg)
        text = render(p["text"], {"evaluation": json.dumps(inp, ensure_ascii=False, indent=1)})
        t0 = time.monotonic()
        try:
            out = client.complete(text, purpose="audit", candidate_id=cid)
            obj = extract_json(out)
            flags = _valid_flags(obj)
            if flags is None:
                raise ValueError("audit response failed shape validation")
        except Exception as e:  # API 失敗・形式不正: 記録せず未監査のまま次へ
            failed += 1
            log(f"audit skipped (ev {row['id']}): {type(e).__name__}: {e}")
            continue
        finally:
            spent += time.monotonic() - t0
        store.add_audit_result({
            "evaluation_id": row["id"], "candidate_id": cid, "run_id": batch,
            "auditor_provider": getattr(client, "name", None),
            "auditor_model": _used_model(client) or model_key,
            "prompt_version": p["version"], "prompt_sha": p["sha"],
            "flags": flags, "summary": obj.get("summary") if isinstance(obj.get("summary"), str) else ""})
        audited += 1
        if flags:
            flagged += 1
        for f in flags:
            checks[f["check"]] = checks.get(f["check"], 0) + 1
    res = {"batch": batch, "selected": len(rows), "audited": audited,
           "flagged": flagged, "checks": checks, "failed": failed}
    log(f"audited {audited}/{len(rows)} evaluations ({flagged} flagged, "
        f"{failed} failed) as {batch}")
    return res


def build_audit_report(store, batch):
    """バッチ単位の Markdown レポート。完走率の材料として llm_calls の
    試行/成功/失敗も併記する (記録が無ければ監査済み件数だけを出す)。"""
    rows = store.audit_results(batch)
    calls = [c for c in store.llm_calls(batch) if c.get("purpose") == "audit"]
    trials = len(calls)
    ok_calls = sum(1 for c in calls if c.get("ok"))
    checks = {}
    flagged_rows = 0
    for r in rows:
        if r["flags"]:
            flagged_rows += 1
        for f in r["flags"]:
            checks[f["check"]] = checks.get(f["check"], 0) + 1
    lines = [f"# 監査レポート {batch}", "",
             f"- 監査済み評価: {len(rows)}件 (フラグあり {flagged_rows}件)"]
    if trials:
        lines.append(f"- 監査コール: 試行 {trials} / 成功 {ok_calls} / 失敗 {trials - ok_calls}"
                     f" (成功率 {ok_calls / trials * 100:.0f}%)")
    lines += ["", "## チェック別フラグ件数", "",
              "| チェック | 件数 |", "|---|---|"]
    for c in AUDIT_CHECKS:
        lines.append(f"| {c} | {checks.get(c, 0)} |")
    lines += ["", "## フラグ一覧", ""]
    if not any(r["flags"] for r in rows):
        lines.append("(フラグはありません)")
    for r in rows:
        for f in r["flags"]:
            lines.append(f"- [{f['check']}] {r.get('candidate_title') or r['candidate_id']}"
                         f" / {f['item']}: {f['detail']}")
    lines += ["", "## 監査サマリ", ""]
    for r in rows:
        if r.get("summary"):
            lines.append(f"- {r.get('candidate_title') or r['candidate_id']}: {r['summary']}")
    return "\n".join(lines) + "\n"
