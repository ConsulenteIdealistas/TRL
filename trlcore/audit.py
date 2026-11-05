"""評価監査 (監査役)。

完了済みの長期評価 (kind='long') を読み直し、評価ルールへの抵触の疑いを
「フラグ」として audit_results に記録するだけの追加層。本体評価 (点・
状態・TRL) には一切触れず、再評価も引き起こさない。

既定のエンジンは **規則ベース** (audit_rules.json のパターン照合、
run_audit_rules)。ブラックリスト (確定違反) → ホワイトリスト (正当な
根拠で救済) → グレー (判定保留として記録のみ) の3層で、API を呼ばない
ため全件・即時・再現可能に監査できる。規則は監査側だけで管理し、評価の
世代印 (config_hash) には含めない。

旧 LLM 経路 (run_audit、監査プロンプト audit.v1.md) は **休眠**: コードは
残すが既定では使わない。パイロット実測で 3.6 Flash は 429 により完走率
約1.6% に留まり、常時回るべき点検には使えなかったため。監査プロンプトは
scoring.json の prompts には登録せず、版は AUDIT_PROMPT_VERSION で管理
する (そのため config_hash に一切関与しない)。設計:
~/workspace/trl-audit/DESIGN.md (v2 規則ベース版)。
"""
import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT
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
    """【休眠】LLM 経路: 未監査の long 評価を順に監査し、結果を audit_results に記録する。
    既定エンジンは規則ベース (run_audit_rules) で、本関数は将来グレー判定の
    最上層に LLM を戻す余地として残してある (通常は呼ばれない)。
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


# ---------------------------------------------------------------------------
# 規則エンジン (既定)。audit_rules.json のパターン照合だけで監査する。
# 層構造: ブラックリスト (確定違反) → ホワイトリスト (正当な根拠で救済)
# → グレー (どちらのリストでも決着せず、判定保留として記録のみ)。
# ---------------------------------------------------------------------------

RULES_FILENAME = "audit_rules.json"

# 規則監査のチェック。A5 (点と理由の整合) は意味判断が必要でパターン化の
# 実測裏付けが無いため、規則化しない (設計書 v2 4節)。
RULES_CHECKS = ("A1", "A2", "A3", "A4", "A6", "A7")


def load_rules(rules_dir=None):
    """audit_rules.json を読み、{"version", "sha", "rules"} を返す。
    sha はファイル全文の sha256 先頭12桁 (load_prompt と同じ流儀)。
    規則ファイルは評価の世代印 (config_hash) には含めず、版とハッシュは
    audit_results の prompt_version / prompt_sha 列に流用して記録する。"""
    path = Path(rules_dir or ROOT) / RULES_FILENAME
    text = path.read_text(encoding="utf-8-sig")
    rules = json.loads(text)
    return {"version": rules.get("rules_version"),
            "sha": hashlib.sha256(text.encode()).hexdigest()[:12],
            "rules": rules}


def _norm_text(s):
    """照合用の正規化: NFKC で全角・半角を揃え (数字は半角になる)、空白を
    1つに圧縮する (normalize.py の流儀)。対象テキストにもパターンにも
    同じ正規化をかけるため、規則は半角数字で書ける。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip()


def _find(patterns, text):
    """パターン配列から最初に当たったものを返す。
    -> (entry, matched_text, (start, end)) | None。text は _norm_text 済み前提。"""
    for p in patterns or []:
        pat = unicodedata.normalize("NFKC", p.get("pattern") or "")
        if not pat:
            continue
        if p.get("kind") == "regex":
            m = re.search(pat, text)
            if m:
                return p, m.group(0), m.span()
        else:
            i = text.find(pat)
            if i >= 0:
                return p, pat, (i, i + len(pat))
    return None


def _quote(text, span, width=24):
    """マッチ箇所の前後を引用する (detail に当たった文面を含めるため)。"""
    if span is None:
        return text[:60]
    s, e = span
    return text[max(0, s - width):e + width]


def _evidence_text(item):
    """項目の根拠側テキスト (evidence 要約の連結、正規化済み)。"""
    if not isinstance(item, dict):
        return ""
    return _norm_text(" ".join(
        str(e.get("summary") or "") for e in (item.get("evidence") or [])
        if isinstance(e, dict)))


def _extraction_text(extraction):
    """抽出 (extraction) の全文テキスト。文字列リーフを全て集めて連結する
    (キー名は照合対象に含めない)。"""
    parts = []

    def walk(o):
        if isinstance(o, str):
            parts.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(extraction)
    return _norm_text(" ".join(parts))


def _check_a1(inp, chk, ext_text, out):
    """A1 件数・期間の推測 (原則2)。対象は技術系の reason のみ (事業系の
    財務継続は財務諸表の期の明記が根拠として成立し、機械照合では誤検知が
    量産されるため対象外)。主張パターンに当たれば、根拠側 (項目の evidence
    要約 + extraction) に回数・期間・日付の明記があるかで救済する。"""
    items = inp.get("items") or {}
    targets = []
    for key in TECH_ITEMS:
        it = items.get(key)
        if isinstance(it, dict):
            targets.append((key, it.get("reason") or "", _evidence_text(it)))
    trl = inp.get("trl_level") or {}
    if trl.get("reason"):
        targets.append(("trl_level", trl["reason"], _evidence_text(trl)))
    for key, reason_raw, ev_text in targets:
        reason = _norm_text(reason_raw)
        if not reason:
            continue
        side = (ev_text + " " + ext_text).strip()
        black = _find(chk.get("black"), reason)
        white = _find(chk.get("white"), side)
        if black and not white:
            p, matched, span = black
            out.append({
                "check": "A1", "item": key, "verdict": "flag", "rule": p["pattern"],
                "detail": f"理由に「{matched}」という件数・継続の主張があるが"
                          f" (規則: {p['pattern']})、根拠側に回数・期間・日付の明記が"
                          f"見当たらない。理由: 『{_quote(reason, span)}』"})
        elif not black:
            gray = _find(chk.get("gray"), reason)
            if gray and not white:
                p, matched, span = gray
                out.append({
                    "check": "A1", "item": key, "verdict": "gray", "rule": p["pattern"],
                    "detail": f"理由に汎用の「{matched}」があるが、確定パターンにも"
                              f"根拠側の明記にも当たらないため判定保留。"
                              f"理由: 『{_quote(reason, span)}』"})


def _check_a2(inp, chk, ext_text, out):
    """A2 TRL 9 の継続証拠 (原則2)。ホワイトリスト主導: TRL 9 のときだけ、
    根拠側 (technology_readiness の evidence + extraction) にセクター別の
    確認語彙が1つも無ければフラグ。セクター専用の語彙が無い場合は
    全セクターの和集合で判定する。"""
    trl = inp.get("trl_level") or {}
    if trl.get("level") != 9:
        return
    items = inp.get("items") or {}
    side = (_evidence_text(items.get("technology_readiness")) + " " + ext_text).strip()
    by_sector = chk.get("white_by_sector") or {}
    sector = inp.get("sector")
    pats = by_sector.get(sector)
    scope = sector if pats else "all"
    if not pats:
        pats = [p for ps in by_sector.values() for p in ps]
    if not _find(pats, side):
        out.append({
            "check": "A2", "item": "trl_level", "verdict": "flag",
            "rule": f"white_by_sector:{scope}",
            "detail": f"TRL 9 だが、根拠側 (技術成熟度の根拠+抽出) にセクター"
                      f" {scope} の継続・繰り返しの確認語彙が見当たらない。"})


def _check_a3(inp, chk, ext_text, out):
    """A3 成長推論の禁止 (原則3)。初版はブラックリストが空で、推論構文に
    当たってもグレー (判定保留) として記録だけする。将来 black にパターン
    が入れば、その時点で初めてフラグになる。"""
    items = inp.get("items") or {}
    targets = [(k, (it or {}).get("reason") or "")
               for k, it in items.items() if isinstance(it, dict)]
    trl = inp.get("trl_level") or {}
    if trl.get("reason"):
        targets.append(("trl_level", trl["reason"]))
    for key, reason_raw in targets:
        reason = _norm_text(reason_raw)
        if not reason:
            continue
        black = _find(chk.get("black"), reason)
        if black:
            p, matched, span = black
            out.append({
                "check": "A3", "item": key, "verdict": "flag", "rule": p["pattern"],
                "detail": f"理由に成長推論の構文「{matched}」がある"
                          f" (規則: {p['pattern']})。理由: 『{_quote(reason, span)}』"})
            continue
        gray = _find(chk.get("gray"), reason)
        if gray:
            p, matched, span = gray
            out.append({
                "check": "A3", "item": key, "verdict": "gray", "rule": p["pattern"],
                "detail": f"成長推論の構文に当たるが、初版ではブラックリストが"
                          f"空のため判定保留。理由: 『{_quote(reason, span)}』"})


def _check_a4(inp, chk, ext_text, out):
    """A4 技術と商用の混同 (原則1)。技術系項目の reason+evidence に商用
    マーカーがあり、技術マーカーが皆無の場合だけ拾う。バイオでは承認・
    実臨床が正当な技術証拠になりうるため、初版はチェックの mode に従い
    フラグではなくグレーとして記録する。"""
    items = inp.get("items") or {}
    verdict = chk.get("mode") or "flag"
    for key in TECH_ITEMS:
        it = items.get(key)
        if not isinstance(it, dict):
            continue
        text = _norm_text((it.get("reason") or "") + " " + _evidence_text(it))
        if not text:
            continue
        black = _find(chk.get("black"), text)
        if black and not _find(chk.get("white"), text):
            p, matched, span = black
            out.append({
                "check": "A4", "item": key, "verdict": verdict, "rule": p["pattern"],
                "detail": f"技術項目なのに商用マーカー「{matched}」のみで技術"
                          f"マーカーが見当たらない (原則1)。"
                          f"文面: 『{_quote(text, span)}』"})


def _check_a6(inp, chk, ext_text, out):
    """A6 欠落判定の妥当性。missing_info / critical_missing に、材料プールへ
    原理的に入らない語彙 (株価・出来高等) が混ざっていたらフラグ。"""
    items = inp.get("items") or {}
    targets = []
    for key, it in items.items():
        if isinstance(it, dict):
            for m in it.get("missing_info") or []:
                targets.append((key, str(m)))
    for m in inp.get("critical_missing") or []:
        targets.append(("critical_missing", str(m)))
    for key, raw in targets:
        text = _norm_text(raw)
        hit = _find(chk.get("black"), text)
        if hit:
            p, matched, span = hit
            out.append({
                "check": "A6", "item": key, "verdict": "flag", "rule": p["pattern"],
                "detail": f"不足情報に、材料プールへ原理的に入らない語彙"
                          f"「{matched}」が含まれている: 『{text[:80]}』"})


def _check_a7(inp, chk, ext_text, out):
    """A7 減点理由の具体性 (第15条4項)。フラグは複合条件の全充足のみ:
    低得点 (score <= threshold) かつ理由に漠然フレーズ (狭い集合) かつ
    missing_info が空 かつ理由本文に具体的な名指しが無い。広い漠然
    フレーズだけなら、確定条件を満たさない低得点はグレーに記録する。"""
    threshold = chk.get("score_threshold", 40)
    items = inp.get("items") or {}
    for key, it in items.items():
        if not isinstance(it, dict):
            continue
        score = it.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) \
                or score > threshold:
            continue
        reason = _norm_text(it.get("reason") or "")
        if not reason:
            continue
        black = _find(chk.get("black"), reason)
        if black and not (it.get("missing_info") or []) \
                and not _find(chk.get("white"), reason):
            p, matched, span = black
            out.append({
                "check": "A7", "item": key, "verdict": "flag", "rule": p["pattern"],
                "detail": f"低得点 ({score}点) の理由が「{matched}」という漠然と"
                          f"した表現のみで、不足情報の名指しも missing_info の"
                          f"記載もない (第15条4項)。理由: 『{_quote(reason, span)}』"})
            continue
        gray = _find(chk.get("gray"), reason) or black
        if gray:
            p, matched, span = gray
            out.append({
                "check": "A7", "item": key, "verdict": "gray", "rule": p["pattern"],
                "detail": f"低得点 ({score}点) の理由に漠然フレーズ「{matched}」が"
                          f"あるが、確定条件を満たさないため判定保留。"
                          f"理由: 『{_quote(reason, span)}』"})


_CHECK_FUNCS = {"A1": _check_a1, "A2": _check_a2, "A3": _check_a3,
                "A4": _check_a4, "A6": _check_a6, "A7": _check_a7}


def audit_evaluation_rules(inp, rules):
    """評価1件 (build_audit_input の dict) に規則エンジンを適用し、verdict
    つきの記録リストを返す。各要素は {check, item, detail, verdict, rule}。
    rules は audit_rules.json の内容 (load_rules(...)["rules"])。"""
    checks = (rules or {}).get("checks") or {}
    ext_text = _extraction_text(inp.get("extraction"))
    out = []
    for name in RULES_CHECKS:
        if name in checks:
            _CHECK_FUNCS[name](inp, checks[name], ext_text, out)
    return out


def run_audit_rules(store, cfg, *, limit=100, log=print, rules_dir=None):
    """規則エンジンで未監査の long 評価を監査し、audit_results に記録する。
    API を呼ばないため失敗は基本的に起きないが、想定外のレコード形で
    1件が落ちても全体は止めない (落ちた評価は未監査のまま残る)。冪等判定は
    LLM 経路と同じく (evaluation_id, prompt_sha, auditor_model) で、規則の
    版・内容が変われば (sha が変われば) 過去の評価も再び対象になる。"""
    batch = "aud-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    r = load_rules(rules_dir)
    model = f"rules/{r['version']}"
    rows = select_unaudited(store, r["sha"], model, limit)
    audited = failed = n_flags = n_grays = 0
    checks = {}
    for row in rows:
        try:
            inp = build_audit_input(store, row, cfg)
            entries = audit_evaluation_rules(inp, r["rules"])
        except Exception as e:  # 1件の想定外レコードで全体を落とさない
            failed += 1
            log(f"rules audit skipped (ev {row['id']}): {type(e).__name__}: {e}")
            continue
        nf = sum(1 for e in entries if e.get("verdict") == "flag")
        ng = sum(1 for e in entries if e.get("verdict") == "gray")
        store.add_audit_result({
            "evaluation_id": row["id"], "candidate_id": row["candidate_id"],
            "run_id": batch, "auditor_provider": "rules", "auditor_model": model,
            "prompt_version": r["version"], "prompt_sha": r["sha"],
            "flags": entries,
            "summary": f"規則監査: フラグ {nf} 件 / グレー {ng} 件" if entries else ""})
        audited += 1
        n_flags += nf
        n_grays += ng
        for e in entries:
            c = checks.setdefault(e["check"], {"flag": 0, "gray": 0})
            c["gray" if e.get("verdict") == "gray" else "flag"] += 1
    res = {"batch": batch, "selected": len(rows), "audited": audited,
           "flags": n_flags, "grays": n_grays, "checks": checks, "failed": failed}
    log(f"rules-audited {audited}/{len(rows)} evaluations "
        f"({n_flags} flags, {n_grays} grays, {failed} failed) as {batch}")
    return res


def build_audit_report(store, batch):
    """バッチ単位の Markdown レポート。フラグ (verdict=flag) とグレー
    (verdict=gray、判定保留) を分けて数え、一覧も分ける。LLM 経路の旧行
    (verdict を持たない要素) はフラグとして扱い、llm_calls の試行/成功/
    失敗も記録があれば併記する。"""
    rows = store.audit_results(batch)
    calls = [c for c in store.llm_calls(batch) if c.get("purpose") == "audit"]
    trials = len(calls)
    ok_calls = sum(1 for c in calls if c.get("ok"))

    def verdict(f):
        return f.get("verdict") if f.get("verdict") in ("flag", "gray") else "flag"

    checks = {}
    flagged_rows = gray_rows = 0
    for r in rows:
        vs = [verdict(f) for f in r["flags"]]
        if "flag" in vs:
            flagged_rows += 1
        if "gray" in vs:
            gray_rows += 1
        for f in r["flags"]:
            c = checks.setdefault(f["check"], [0, 0])
            c[0 if verdict(f) == "flag" else 1] += 1
    lines = [f"# 監査レポート {batch}", "",
             f"- 監査済み評価: {len(rows)}件"
             f" (フラグあり {flagged_rows}件 / グレーあり {gray_rows}件)"]
    if trials:
        lines.append(f"- 監査コール: 試行 {trials} / 成功 {ok_calls} / 失敗 {trials - ok_calls}"
                     f" (成功率 {ok_calls / trials * 100:.0f}%)")
    lines += ["", "## チェック別内訳", "",
              "| チェック | フラグ | グレー |", "|---|---|---|"]
    for c in AUDIT_CHECKS:
        f_n, g_n = checks.get(c, [0, 0])
        lines.append(f"| {c} | {f_n} | {g_n} |")
    for title, want, empty in (("フラグ一覧", "flag", "(フラグはありません)"),
                               ("グレー一覧 (判定保留)", "gray", "(グレーはありません)")):
        entries = [(r, f) for r in rows for f in r["flags"] if verdict(f) == want]
        lines += ["", f"## {title}", ""]
        if not entries:
            lines.append(empty)
        for r, f in entries:
            lines.append(f"- [{f['check']}] {r.get('candidate_title') or r['candidate_id']}"
                         f" / {f['item']}: {f['detail']}")
    lines += ["", "## 監査サマリ", ""]
    for r in rows:
        if r.get("summary"):
            lines.append(f"- {r.get('candidate_title') or r['candidate_id']}: {r['summary']}")
    return "\n".join(lines) + "\n"
