"""評価監査 (監査役)。

完了済みの長期評価 (kind='long') を読み直し、評価ルールへの抵触の疑いを
「要確認」として audit_results に記録するだけの追加層。本体評価 (点・
状態・TRL) には一切触れず、再評価も引き起こさない。

既定のエンジンは **規則ベース** (audit_rules.json のパターン照合、
run_audit_rules)。ブラックリスト (確定違反) → ホワイトリスト (正当な
根拠で救済) → グレー (判定保留として記録のみ) の3層で、API を呼ばない
ため全件・即時・再現可能に監査できる。規則は監査側だけで管理し、評価の
世代印 (config_hash) には含めない。

旧 LLM 経路は 2026-11-06 に廃止した (パイロット実測で 3.6 Flash は 429
により完走率約1.6% に留まり、常時回るべき点検には使えなかったため)。
監査のエンジンは規則ベースのみ。過去の LLM 監査の記録 (audit_results)
はそのまま残り、レポートとサイトは表示互換として読み続ける。設計:
~/workspace/trl-audit/DESIGN.md (v2 規則ベース版)。
"""
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ROOT
from .dates import parse_date
from .scoring import TECH_ITEMS, compute as scoring_compute

AUDIT_CHECKS = ("A1", "A2", "A3", "A4", "A5", "A6", "A7",
                "A8", "A9", "A10", "A11", "A12", "A13", "A14", "A15",
                "E1", "E2")

def _loads(s, default):
    if s is None:
        return default
    if not isinstance(s, str):
        return s
    try:
        return json.loads(s)
    except ValueError:
        return default


def _filing_period_key(title):
    """EDINET 表題の「第N期(開始－終了)」を取り出す (store と同じ定義。
    財務プールの I-6 訂正除外を監査側でも同じ条件で再現するため)。"""
    m = re.search(r"第\d+期\([^)]*\)", title or "")
    return m.group(0) if m else None


def _pool_source_ids(store, cand, as_of):
    """評価時の材料プールに入る材料の出典識別子を、監査時点の DB から
    再導出して返す (P2 の出典数え直し用)。

    プールの定義 (窓・上限・リンク条件・I-6 訂正除外) は評価時の
    store.related_news / related_financials と同一で、site.py や
    vault.py が書き出し時にプールを再導出するのと同じ流儀。評価後に
    収集・削除された材料の分だけ、評価時の実プールとはずれることが
    ある。原文は載せず識別子だけを返す: ニュースは媒体名
    (fields の先頭)、無ければ source コード。財務 (開示) は source。"""
    cid = cand.get("id")
    start = (as_of - timedelta(days=90)).isoformat()
    rows = store.conn.execute(
        "SELECT DISTINCT n.title, n.published, n.url, n.fields_json, n.source,"
        " n.media_url FROM candidates n "
        "JOIN candidate_entities ne ON ne.candidate_id = n.id "
        "JOIN candidate_entities ce ON ce.entity_id = ne.entity_id "
        "WHERE ce.candidate_id = ? AND n.id != ? AND n.info_type = 'news' "
        "AND ne.link_type = 'entity' "
        "AND NOT (n.status = 'rejected' AND n.screen_reason LIKE 'duplicate_of:%') "
        "AND n.published IS NOT NULL AND n.published <= ? AND n.published >= ? "
        "ORDER BY n.published DESC, n.id LIMIT 8",
        (cid, cid, as_of.isoformat(), start)).fetchall()
    ids = []
    for r in rows:
        fields = _loads(r["fields_json"], [])
        ids.append(fields[0] if isinstance(fields, list) and fields
                   else r["source"])
    start = (as_of - timedelta(days=400)).isoformat()
    rows = store.conn.execute(
        "SELECT DISTINCT n.title, n.published, n.url, n.abstract, n.source"
        " FROM candidates n "
        "JOIN candidate_entities ne ON ne.candidate_id = n.id "
        "JOIN candidate_entities ce ON ce.entity_id = ne.entity_id "
        "WHERE ce.candidate_id = ? AND n.id != ? AND n.info_type = 'disclosure' "
        "AND ne.strength = 'strong' AND ne.link_type = 'entity' "
        "AND n.abstract LIKE '%財務数値(%' "
        "AND n.published IS NOT NULL AND n.published <= ? AND n.published >= ? "
        "ORDER BY n.published DESC, n.id LIMIT 6",
        (cid, cid, as_of.isoformat(), start)).fetchall()
    fins = [{"title": r["title"], "source": r["source"]} for r in rows]
    corrected = {_filing_period_key(f["title"]) for f in fins
                 if "訂正" in (f["title"] or "") and _filing_period_key(f["title"])}
    if corrected:
        fins = [f for f in fins if "訂正" in (f["title"] or "")
                or _filing_period_key(f["title"]) not in corrected]
    ids += [f["source"] for f in fins[:2]]
    out = []
    for x in ids:
        if x and x not in out:
            out.append(x)
    return out


def build_audit_input(store, ev_row, cfg=None):
    """評価1件分の監査入力 JSON (設計書3節) を、保存済みレコードから組む。
    材料の原文は載せない (保存済みの抽出・項目情報だけを使う)。例外として
    材料プールの出典識別子 (pool_sources) だけは監査時点で DB から
    再導出して載せる (P2 が出典の多様性を数えるため。識別子のみで
    原文は含まない)。
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

    # P2 用: 材料プールの出典識別子。評価時 (pipeline) と同じく、ニュース
    # 候補そのものにはプールを付けない。再導出に失敗しても監査入力の
    # 組み立て自体は止めない (A13 の再導出と同じ扱い)。
    pool_sources = []
    if cand.get("info_type") != "news":
        as_of_d = parse_date(ev.get("as_of"))
        if as_of_d is not None:
            try:
                pool_sources = _pool_source_ids(store, cand, as_of_d)
            except Exception:
                pool_sources = []

    ent_cfg = {}
    if cfg and cfg.get("entities"):
        ent_cfg = {e["id"]: e for e in cfg["entities"].get("entities", [])}
    companies, sector = [], None
    for link in cand.get("entities") or []:
        meta = ent_cfg.get(link.get("entity_id")) or {}
        companies.append({"name": meta.get("name") or link.get("entity_id"),
                          "sector": meta.get("sector"),
                          "strength": link.get("strength")})
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
    # A13 (スコア再計算) 用: 保存済み項目点から現行設定で総合点を再導出し、
    # 保存値と突き合わせる (recalc と同じ呼び出し方)。導出できない形なら
    # None のままにし、チェック側で読み飛ばす
    expected_total = expected_band = None
    if cfg and ev.get("kind") in ("long", "event"):
        try:
            score_items = {k: (v or {}).get("score") for k, v in items.items()
                           if isinstance(v, dict)}
            out = scoring_compute(score_items, ev["kind"], cfg,
                                  (trl or {}).get("level")
                                  if isinstance(trl, dict) else None)
            if out.get("status") == "final":
                expected_total = out.get("total")
                expected_band = (out.get("band") or {}).get("label")
        except Exception:
            expected_total = expected_band = None
    mr = extra.get("market_reaction")
    return {
        "evaluation_id": ev.get("id"),
        "candidate_id": ev.get("candidate_id"),
        "kind": ev.get("kind"),
        "market_reaction": mr if isinstance(mr, dict) else None,
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
        "pool_sources": pool_sources,
        "published": cand.get("published"),
        "as_of": ev.get("as_of"),
        "expected_total": expected_total,
        "expected_band": expected_band,
    }


def select_unaudited(store, prompt_sha, model, limit=100, kind="long"):
    """未監査の評価 (kind 別) を古い順に返す。監査済みの判定は冪等キー
    (evaluation_id, prompt_sha, auditor_model) の一致で行うため、監査
    プロンプトが改訂される (sha が変わる) か監査モデルが変われば、
    過去に監査した評価も再び対象になる。"""
    rows = store.conn.execute(
        "SELECT e.* FROM evaluations e WHERE e.kind = ?"
        " AND NOT EXISTS (SELECT 1 FROM audit_results a"
        "   WHERE a.evaluation_id = e.id AND a.prompt_sha = ?"
        "   AND a.auditor_model = ?)"
        " ORDER BY e.evaluated_at ASC, e.id ASC LIMIT ?",
        (kind, prompt_sha, model, int(limit))).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 規則エンジン (既定)。audit_rules.json のパターン照合だけで監査する。
# 層構造: ブラックリスト (確定違反) → ホワイトリスト (正当な根拠で救済)
# → グレー (どちらのリストでも決着せず、判定保留として記録のみ)。
# ---------------------------------------------------------------------------

RULES_FILENAME = "audit_rules.json"

# 規則監査のチェック。A5 (点と理由の整合) は意味判断が必要でパターン化の
# 実測裏付けが無いため、規則化しない (設計書 v2 4節)。
# A8 以降は監査要項 (~/workspace/trl-audit/AUDIT_REQUIREMENTS.md、2026-11-06)
# の8分類に対応する決定的チェック: A8=⑦反証 / A9=⑥証拠充足 / A10=②時点 /
# A11=⑤数値 / A12=①出典形式 / A13=⑧スコア再計算 / A14=③企業帰属。
RULES_CHECKS = ("A1", "A2", "A3", "A4", "A6", "A7",
                "A8", "A9", "A10", "A11", "A12", "A13", "A14", "A15")
# event 評価の監査。A10/A12 は long と共通 (時点・出典形式)。E1/E2 は
# 実測した価格反応 (extra.market_reaction) との整合を見る (監査要項の
# event 監査節、ISA 520 の分析的手続きが発想の出典)
EVENT_CHECKS = ("A10", "A12", "E1", "E2")


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
    確認語彙が1つも無ければ要確認。セクター専用の語彙が無い場合は
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
    が入れば、その時点で初めて要確認になる。"""
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
    要確認ではなくグレーとして記録する。"""
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
    原理的に入らない語彙 (株価・出来高等) が混ざっていたら要確認。"""
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
    """A7 減点理由の具体性 (第15条4項)。要確認は複合条件の全充足のみ:
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


def _check_a8(inp, chk, ext_text, out):
    """A8 反証監査 (監査要項⑦)。確定評価で総合点が帯域「強い」の下限
    (total_threshold、既定60) 以上なのに、抽出の counter_points も項目の
    counter_evidence も空 = 反証が一切記録されていない場合に要確認。
    好材料への前のめりを拾うためのチェック。抽出自体が無い評価は
    判定材料が無いため対象外。"""
    threshold = chk.get("total_threshold", 60)
    total = inp.get("total")
    if inp.get("status") != "final" or not isinstance(total, (int, float)) \
            or isinstance(total, bool) or total < threshold:
        return
    ext = inp.get("extraction")
    if not isinstance(ext, dict):
        return
    if ext.get("counter_points"):
        return
    items = inp.get("items") or {}
    if any(isinstance(it, dict) and it.get("counter_evidence")
           for it in items.values()):
        return
    out.append({
        "check": "A8", "item": "counter_points", "verdict": "flag",
        "rule": f"total>={threshold}",
        "detail": f"確定評価 (総合点 {total}) だが、反証 (counter_points / "
                  f"counter_evidence) が一切記録されていない。否定的材料の"
                  f"拾い漏れか、材料プールに反証が無い状態で高く出ている"
                  f"疑いがある。"})


def _check_a9(inp, chk, ext_text, out):
    """A9 証拠充足 (監査要項⑥)。項目点が帯域「非常に強い」の下限
    (score_threshold、既定80) 以上なのに evidence が空 = 根拠の無い
    高得点の場合に要確認。"""
    threshold = chk.get("score_threshold", 80)
    items = inp.get("items") or {}
    for key, it in items.items():
        if not isinstance(it, dict):
            continue
        score = it.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) \
                or score < threshold:
            continue
        if not (it.get("evidence") or []):
            out.append({
                "check": "A9", "item": key, "verdict": "flag",
                "rule": f"score>={threshold}",
                "detail": f"項目点が {score} 点 (高得点帯) だが、根拠 "
                          f"(evidence) が1件も記録されていない。"})


def _check_a10(inp, chk, ext_text, out):
    """A10 時点監査 (監査要項②)。候補の公開日が評価の基準日 (as_of)
    より後 = 基準日時点で存在しない材料で評価した形になっている場合に
    要確認 (データ不整合の検出)。"""
    published, as_of = inp.get("published"), inp.get("as_of")
    if isinstance(published, str) and isinstance(as_of, str) \
            and published and as_of and published > as_of:
        out.append({
            "check": "A10", "item": "candidate", "verdict": "flag",
            "rule": "published>as_of",
            "detail": f"候補の公開日 ({published}) が評価の基準日 "
                      f"({as_of}) より後になっている。"})


_NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d{3,}|\d+(?:\.\d+)?%")


def _check_a11(inp, chk, ext_text, out):
    """A11 数値監査 (監査要項⑤)。項目の理由に現れる数値 (小数・%・
    3桁以上の整数) が、根拠側 (evidence 要約 + 抽出全文) のどこにも
    現れない場合にグレーで記録する。丸め・単位換算・関連財務からの引用
    など正当な不一致がありうるため、初版は確定にせず観測に置く
    (A3/A4 の初版と同じ扱い)。"""
    items = inp.get("items") or {}
    for key, it in items.items():
        if not isinstance(it, dict):
            continue
        reason = _norm_text(it.get("reason") or "")
        if not reason:
            continue
        side = (_evidence_text(it) + " " + ext_text).replace(",", "")
        missing = []
        for m in _NUM_RE.finditer(reason):
            num = m.group(0).replace(",", "").rstrip("%")
            if num not in side and num not in missing:
                missing.append(num)
            if len(missing) >= 3:
                break
        if missing:
            out.append({
                "check": "A11", "item": key, "verdict": "gray",
                "rule": "number-not-in-evidence",
                "detail": f"理由中の数値 {', '.join(missing)} が根拠側 "
                          f"(evidence + 抽出) に見当たらない。原文照合の"
                          f"対象として記録する。"})


def _check_a12(inp, chk, ext_text, out):
    """A12 出典形式 (監査要項①のオフライン分)。evidence の ref が無い、
    または http(s) URL でない場合に要確認。実在確認 (HTTP 照会) は
    監査の外のリンク検証スイープが担う。"""
    items = inp.get("items") or {}
    for key, it in items.items():
        if not isinstance(it, dict):
            continue
        bad = [e for e in (it.get("evidence") or []) if isinstance(e, dict)
               and not str(e.get("ref") or "").startswith(("http://", "https://"))]
        if bad:
            out.append({
                "check": "A12", "item": key, "verdict": "flag",
                "rule": "ref-missing-or-not-url",
                "detail": f"根拠 {len(bad)} 件に出典リンク (http(s) の ref) "
                          f"が無い。出典へ辿れない根拠は検証できない。"})


def _check_a13(inp, chk, ext_text, out):
    """A13 スコア監査 (監査要項⑧)。保存済み項目点から現行設定で再導出した
    総合点・バンド (build_audit_input の expected_*) と保存値が食い違う
    場合に要確認。集計の不整合・未実施の再計算を拾う。"""
    expected, total = inp.get("expected_total"), inp.get("total")
    if expected is None or total is None:
        return
    if abs(expected - total) > 0.05 or \
            (inp.get("expected_band") and inp.get("band")
             and inp["expected_band"] != inp["band"]):
        out.append({
            "check": "A13", "item": "total", "verdict": "flag",
            "rule": "recompute-mismatch",
            "detail": f"保存された総合点 {total} (バンド {inp.get('band')}) が、"
                      f"項目点からの再導出 {expected} "
                      f"(バンド {inp.get('expected_band')}) と一致しない。"})


def _check_a14(inp, chk, ext_text, out):
    """A14 企業帰属 (監査要項③)。リンクした企業が全て weak (あいまい
    一致のみ) なのに確定評価になっている場合にグレーで記録する。
    帰属の強さは収集時の照合が決めており、判定の確定には人間の処分が
    要るため初版は観測に置く。"""
    if inp.get("status") != "final":
        return
    companies = inp.get("companies") or []
    if companies and all(c.get("strength") == "weak" for c in companies):
        names = ", ".join(c.get("name") or "?" for c in companies)
        out.append({
            "check": "A14", "item": "companies", "verdict": "gray",
            "rule": "all-links-weak",
            "detail": f"確定評価だが、企業リンクが全て弱い一致 "
                      f"(weak) のみ: {names}。帰属の妥当性は判定保留。"})


def _check_a15(inp, chk, ext_text, out):
    """A15 上限の観測 (監査要項⑧)。A2 (TRL 9 の継続証拠なし) がこの監査で
    確定した評価は、証拠が示す水準を超えて最高帯域に置かないという上限
    ルールの対象になる。上限値 cap_total は帯域表の最高帯域の下限の直下
    (第2帯域の上限)。適用自体は非破壊の調整レイヤーが担うため、ここでは
    「上限を適用するとどうなるか」をグレーで観測するに留める。A2 の判定に
    依存するため、チェック順序の最後で out を見て判定する。"""
    cap = chk.get("cap_total", 79)
    total = inp.get("total")
    if not any(e.get("check") == "A2" and e.get("verdict") == "flag" for e in out):
        return
    if not isinstance(total, (int, float)) or isinstance(total, bool) \
            or total <= cap:
        return
    out.append({
        "check": "A15", "item": "total", "verdict": "gray",
        "rule": f"cap:{cap}",
        "detail": f"上限ルールの対象: TRL 9 の継続証拠が無い (A2) ため、"
                  f"総合点は上限 {cap} で抑えるのが要項の定め。現在は "
                  f"{total}。適用は調整レイヤーで行うため、ここでは観測"
                  f"として記録する。"})


def _check_e1(inp, chk, ext_text, out):
    """E1 反応整合 (event 監査)。独立に実測した価格反応が強く出ている
    (z >= z_threshold) のに、not_priced_in が高得点 (「織り込まれて
    いない」) なのは矛盾の疑いとして要確認にする。ISA 520 の分析的
    手続き (独立した期待値と記録値の差異を閾値で判定する) の型。"""
    if inp.get("status") != "final":
        return
    mr = inp.get("market_reaction") or {}
    z = mr.get("z")
    item = (inp.get("items") or {}).get("not_priced_in") or {}
    score = item.get("score") if isinstance(item, dict) else None
    zt = chk.get("z_threshold", 2.0)
    st = chk.get("score_threshold", 60)
    if mr.get("available") and isinstance(z, (int, float)) \
            and isinstance(score, (int, float)) and z >= zt and score >= st:
        out.append({
            "check": "E1", "item": "not_priced_in", "verdict": "flag",
            "rule": f"z>={zt},score>={st}",
            "detail": f"価格は既に強く反応している (標準化反応 z={z:.2f}、"
                      f"反応 {mr.get('reaction_pct')}%) のに、"
                      f"not_priced_in が {score} 点 (織り込まれていない側) "
                      f"になっている。実測との整合に疑いがある。"})


def _check_e2(inp, chk, ext_text, out):
    """E2 反応データ未使用の観測 (event 監査)。価格反応データが取得できた
    のに not_priced_in が未判定のまま保持された評価をグレーで記録する。
    event.v2 (反応があれば必須項目の強制 null を外す) の実地観察用。"""
    if inp.get("status") != "hold":
        return
    mr = inp.get("market_reaction") or {}
    item = (inp.get("items") or {}).get("not_priced_in") or {}
    if mr.get("available") and isinstance(item, dict) \
            and item.get("score") is None:
        out.append({
            "check": "E2", "item": "not_priced_in", "verdict": "gray",
            "rule": "reaction-available-but-unjudged",
            "detail": f"価格反応データがある (z={mr.get('z')}、反応 "
                      f"{mr.get('reaction_pct')}%) のに not_priced_in が"
                      f"未判定のまま保持されている。判定材料として反応が"
                      f"使われなかった可能性を記録する。"})


_CHECK_FUNCS = {"A1": _check_a1, "A2": _check_a2, "A3": _check_a3,
                "A4": _check_a4, "A6": _check_a6, "A7": _check_a7,
                "A8": _check_a8, "A9": _check_a9, "A10": _check_a10,
                "A11": _check_a11, "A12": _check_a12, "A13": _check_a13,
                "A14": _check_a14, "A15": _check_a15,
                "E1": _check_e1, "E2": _check_e2}


def audit_evaluation_rules(inp, rules):
    """評価1件 (build_audit_input の dict) に規則エンジンを適用し、verdict
    つきの記録リストを返す。各要素は {check, item, detail, verdict, rule}。
    rules は audit_rules.json の内容 (load_rules(...)["rules"])。"""
    checks = (rules or {}).get("checks") or {}
    ext_text = _extraction_text(inp.get("extraction"))
    out = []
    names = EVENT_CHECKS if inp.get("kind") == "event" else RULES_CHECKS
    for name in names:
        if name in checks:
            _CHECK_FUNCS[name](inp, checks[name], ext_text, out)
    return out


def run_audit_rules(store, cfg, *, limit=100, log=print, rules_dir=None):
    """規則エンジンで未監査の評価 (long + event) を監査し、audit_results
    に記録する。limit は種類ごとの上限。
    API を呼ばないため失敗は基本的に起きないが、想定外のレコード形で
    1件が落ちても全体は止めない (落ちた評価は未監査のまま残る)。冪等判定は
    (evaluation_id, prompt_sha, auditor_model) で、規則の
    版・内容が変われば (sha が変われば) 過去の評価も再び対象になる。"""
    batch = "aud-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    r = load_rules(rules_dir)
    model = f"rules/{r['version']}"
    rows = select_unaudited(store, r["sha"], model, limit, kind="long") \
        + select_unaudited(store, r["sha"], model, limit, kind="event")
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
            "summary": f"規則監査: 要確認 {nf} 件 / グレー {ng} 件" if entries else ""})
        audited += 1
        n_flags += nf
        n_grays += ng
        for e in entries:
            c = checks.setdefault(e["check"], {"flag": 0, "gray": 0})
            c["gray" if e.get("verdict") == "gray" else "flag"] += 1
    res = {"batch": batch, "selected": len(rows), "audited": audited,
           "flags": n_flags, "grays": n_grays, "checks": checks, "failed": failed}
    log(f"rules-audited {audited}/{len(rows)} evaluations "
        f"(要確認 {n_flags} 件, グレー {n_grays} 件, 失敗 {failed} 件) as {batch}")
    return res


def build_audit_report(store, batch):
    """バッチ単位の Markdown レポート。要確認 (verdict=flag) とグレー
    (verdict=gray、判定保留) を分けて数え、一覧も分ける。LLM 経路の旧行
    (verdict を持たない要素) は要確認として扱い、llm_calls の試行/成功/
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
             f" (要確認あり {flagged_rows}件 / グレーあり {gray_rows}件)"]
    if trials:
        lines.append(f"- 監査コール: 試行 {trials} / 成功 {ok_calls} / 失敗 {trials - ok_calls}"
                     f" (成功率 {ok_calls / trials * 100:.0f}%)")
    lines += ["", "## チェック別内訳", "",
              "| チェック | 要確認 | グレー |", "|---|---|---|"]
    for c in AUDIT_CHECKS:
        f_n, g_n = checks.get(c, [0, 0])
        lines.append(f"| {c} | {f_n} | {g_n} |")
    for title, want, empty in (("要確認一覧", "flag", "(要確認はありません)"),
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


# ---------------------------------------------------------------------------
# ポジティブ監査 (過小評価の疑い、P1〜P6) の登録。audit_positive は本
# モジュールのヘルパー (TECH_ITEMS / _find 等) を import するため、循環を
# 避けてモジュール末尾で取り込む。AUDIT_CHECKS / RULES_CHECKS /
# EVENT_CHECKS は build_audit_report / audit_evaluation_rules が実行時に
# モジュール global として参照し、trl.py の from import も本モジュールの
# 読み込み完了後に束縛されるため、末尾での再束縛で問題ない。
# ---------------------------------------------------------------------------
from .audit_positive import (POSITIVE_CHECK_FUNCS, POSITIVE_RULES_CHECKS,
                             POSITIVE_EVENT_CHECKS, POSITIVE_CHECKS)
_CHECK_FUNCS.update(POSITIVE_CHECK_FUNCS)
RULES_CHECKS = RULES_CHECKS + POSITIVE_RULES_CHECKS
EVENT_CHECKS = EVENT_CHECKS + POSITIVE_EVENT_CHECKS
AUDIT_CHECKS = AUDIT_CHECKS + POSITIVE_CHECKS

# ---------------------------------------------------------------------------
# API 照合層 (audit_api) のチェック名を監査レポートの集計対象に加える。
# audit_api は build_audit_input を関数内で遅延 import する設計のため、
# ここで取り込んでも循環しない。実行自体は cli の audit コマンドが
# 規則監査の後に run_audit_api を呼ぶ (失敗しても規則側は残る)。
# ---------------------------------------------------------------------------
from .audit_api import API_CHECKS as _API_CHECKS, run_audit_api  # noqa: F401,E402
AUDIT_CHECKS = AUDIT_CHECKS + _API_CHECKS
