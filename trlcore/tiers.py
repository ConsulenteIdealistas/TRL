"""tier 導出レイヤー (CRITERIA.md §4 の定義・T-ルール、§6 の決定 D-1〜D-12)。

辞書の tier (core / watch / reference) は変更しない。その上に導出 tier
(Core / Conditional / Look / See) を、辞書・評価・監査・処分から決定的に
導く。導出は元評価・辞書・監査結果に一切触らない非破壊の別層で、同じ入力
からは常に同じ結果が返る (純粋な判定。時刻や乱数は使わない)。

導出ルール (tiers/v1):
- 基礎 watch → Look、基礎 reference → See (D-8: See は市場属性のみ)
- 基礎 core は次の全てを満たせば Core、1つでも欠けば Conditional:
  (a) 最新の帰属可能な long 評価が存在する。帰属可能 = その企業が候補へ
      strong リンク (link_type='entity') を持つこと。未評価・event のみの
      企業は評価が存在しないものとして Conditional (D-5 と追加決定)。
  (b) 帰属: その評価の候補について、strong な企業リンクの集合が自社のみ
      (一意) である (D-4 案a)。判定の置き方は監査 P5 (第5条2: strong な
      企業名の集合がちょうど1つ) と同じで、集合は entity_id で数える。
  (c) 監査: その評価の最新監査結果 (auditor ごとに最新行。対象は現行の
      監査系である rules と api のみ) に、未処分の要確認 (verdict=flag)
      が無い (D-4 案b)。処分 (review_decisions) の最新状態が「誤検知
      (false_positive)」の flag は数えない (D-6)。「問題あり (confirmed)」
      「様子見 (watch)」の flag は数える。グレー (gray) は落とさない。
- 判定に使うのは最新 long 評価のみで、回数条件は無い (D-7)。
- 分布の目標 100/150/200/50 (20/30/40/10%) は目安 (D-1)。シェアが目標から
  ±5pt (D-2) を超えた層は警告として出すだけで、自動調整はしない (D-3)。
"""
import json

TIER_RULES_VERSION = "tiers/v1"

DERIVED_TIERS = ("core", "conditional", "look", "see")
DERIVED_LABELS = {"core": "Core", "conditional": "Conditional",
                   "look": "Look", "see": "See"}

# 目標は CRITERIA.md §4-1 の 2:3:4:1 (500社時点の社数とシェア)。D-1 で目安。
TARGET_COUNTS = {"core": 100, "conditional": 150, "look": 200, "see": 50}
TARGET_SHARES = {"core": 20.0, "conditional": 30.0, "look": 40.0, "see": 10.0}
# D-2: 許容誤差はシェア ±5pt (上限として固定)。
TOLERANCE_PT = 5.0

# 条件 (c) で見る監査系。LLM 監査 (gemini) は 2026-11-06 に廃止済みのため
# 対象にしない (休眠経路の過去行が残っていても層を左右しない)。
AUDIT_PROVIDERS = ("rules", "api")

# 処分のうち flag を「処分済み」として数えない状態は誤検知のみ (D-6)。
DISPOSITION_FALSE_POSITIVE = "false_positive"


def derive_tier(entity_id, base_tier, eval_info):
    """1社の導出 tier を決める純粋関数。

    eval_info は基礎 core のときだけ意味を持ち、次の形 (無ければ None):
      {"evaluation_id": int, "candidate_id": int,
       "strong_entity_ids": その候補に strong リンクする企業 id の集合,
       "open_flags": [(check, item), ...] 処分で消えない要確認}
    戻り値: {"derived_tier", "evaluation_id", "reasons": [...]}。
    reasons は引用した条件が分かる形 (rule / ok / detail) で全条件分を返す。
    """
    if base_tier == "watch":
        return {"derived_tier": "look", "evaluation_id": None, "reasons": [
            {"rule": "base_tier", "ok": True,
             "detail": "基礎 tier が watch のため Look (D-8)"}]}
    if base_tier == "reference":
        return {"derived_tier": "see", "evaluation_id": None, "reasons": [
            {"rule": "base_tier", "ok": True,
             "detail": "基礎 tier が reference のため See (D-8: 市場属性のみ)"}]}
    # 基礎 core (辞書の既定も core)。Core か Conditional かを3条件で決める。
    reasons = []
    if not eval_info:
        reasons.append({"rule": "latest_long_eval", "ok": False,
                        "detail": "帰属可能な long 評価が無い (未評価または event のみ)"})
        reasons.append({"rule": "attribution", "ok": False,
                        "detail": "最新 long 評価が無いため判定対象外"})
        reasons.append({"rule": "audit", "ok": False,
                        "detail": "最新 long 評価が無いため判定対象外"})
        return {"derived_tier": "conditional", "evaluation_id": None,
                "reasons": reasons}
    eid = eval_info["evaluation_id"]
    reasons.append({"rule": "latest_long_eval", "ok": True,
                    "detail": f"最新の帰属可能な long 評価は #{eid}"})
    strong = set(eval_info.get("strong_entity_ids") or ())
    if strong == {entity_id}:
        reasons.append({"rule": "attribution", "ok": True,
                        "detail": "strong リンクが自社のみで一意 (D-4 案a)"})
        attr_ok = True
    else:
        others = sorted(strong - {entity_id})
        reasons.append({"rule": "attribution", "ok": False,
                        "detail": "strong リンクが一意でない (他に "
                                  + "、".join(others) + " が strong)"})
        attr_ok = False
    open_flags = sorted({c for c, _ in (eval_info.get("open_flags") or [])})
    if open_flags:
        reasons.append({"rule": "audit", "ok": False,
                        "detail": "未処分の要確認がある: " + "、".join(open_flags)
                                  + f" (評価 #{eid})"})
        audit_ok = False
    else:
        reasons.append({"rule": "audit", "ok": True,
                        "detail": "最新の監査結果に未処分の要確認が無い (D-4 案b)"})
        audit_ok = True
    return {"derived_tier": "core" if (attr_ok and audit_ok) else "conditional",
            "evaluation_id": eid, "reasons": reasons}


def tier_distribution(derived):
    """導出結果 {entity_id: derive_tier の戻り値} から分布と警告を作る。

    警告は D-3 に従い「出すだけ」で、層の自動調整はしない。
    """
    counts = {t: 0 for t in DERIVED_TIERS}
    for r in derived.values():
        counts[r["derived_tier"]] = counts.get(r["derived_tier"], 0) + 1
    total = sum(counts.values())
    shares = {t: (round(100.0 * counts[t] / total, 1) if total else 0.0)
              for t in DERIVED_TIERS}
    warnings = []
    for t in DERIVED_TIERS:
        diff = round(shares[t] - TARGET_SHARES[t], 1)
        if abs(diff) > TOLERANCE_PT:
            warnings.append({"tier": t, "share": shares[t],
                             "target_share": TARGET_SHARES[t], "diff_pt": diff})
    return {"version": TIER_RULES_VERSION, "total": total, "counts": counts,
            "shares": shares,
            "targets": {"counts": dict(TARGET_COUNTS),
                        "shares": dict(TARGET_SHARES)},
            "tolerance_pt": TOLERANCE_PT, "warnings": warnings}


def _flag_entries(flags_json):
    """監査フラグ (JSON 文字列) を dict のリストにする。壊れた値は空扱い。"""
    try:
        flags = json.loads(flags_json or "[]")
    except (TypeError, ValueError):
        return []
    return [f for f in flags if isinstance(f, dict)] if isinstance(flags, list) else []


def load_tier_inputs(store):
    """DB から企業ごとの導出入力 (eval_info) を組み立てる (読み取りのみ)。

    「帰属可能な long 評価」= 企業が strong リンク (link_type='entity') を
    持つ候補の long 評価 (状態は final / hold)。そのうち id が最大のものが
    「最新」。error の評価は本体の他の場所 (eval_counts_long、サイトの
    ピア集約) でも「評価なし」として扱われるため、ここでも評価に数えない
    (2026-11-10 の本番 DB には long の error 評価自体が存在しない)。
    """
    conn = store.conn
    latest = {}
    for entity_id, evaluation_id, candidate_id in conn.execute(
            "SELECT ce.entity_id, e.id, e.candidate_id FROM candidate_entities ce "
            "JOIN evaluations e ON e.candidate_id = ce.candidate_id AND e.kind = 'long' "
            "AND e.status IN ('final', 'hold') "
            "WHERE ce.link_type = 'entity' AND ce.strength = 'strong'"):
        cur = latest.get(entity_id)
        if cur is None or evaluation_id > cur[0]:
            latest[entity_id] = (evaluation_id, candidate_id)
    if not latest:
        return {}
    eval_ids = sorted({v[0] for v in latest.values()})
    cand_ids = sorted({v[1] for v in latest.values()})
    # 候補ごとの strong 企業集合 (条件 b)
    strong_by_cand = {}
    for cid, eid in conn.execute(
            "SELECT candidate_id, entity_id FROM candidate_entities "
            "WHERE link_type = 'entity' AND strength = 'strong' "
            f"AND candidate_id IN ({','.join('?' * len(cand_ids))})", cand_ids):
        strong_by_cand.setdefault(cid, set()).add(eid)
    # 評価ごとの最新監査フラグ (auditor 別に id 最大の行)。id 昇順に読んで
    # 上書きすれば最後に残るのが最新行になる。
    marks = ",".join("?" * len(eval_ids))
    audits = {}
    for _row_id, eval_id, provider, flags_json in conn.execute(
            "SELECT id, evaluation_id, auditor_provider, flags_json "
            "FROM audit_results WHERE evaluation_id IN (" + marks + ") "
            f"AND auditor_provider IN ({','.join('?' * len(AUDIT_PROVIDERS))}) "
            "ORDER BY id", [*eval_ids, *AUDIT_PROVIDERS]):
        audits[(eval_id, provider)] = _flag_entries(flags_json)
    # 処分の最新状態: (評価, チェック, 項目) ごとに id 最大の行が有効
    # (段階B の取り込みと同じ「最新の行が有効」の規則)。
    dispositions = {}
    for eval_id, check_name, item, disposition in conn.execute(
            "SELECT evaluation_id, check_name, IFNULL(item, ''), disposition "
            "FROM review_decisions WHERE evaluation_id IN (" + marks + ") "
            "ORDER BY id", eval_ids):
        dispositions[(eval_id, check_name, item)] = disposition
    inputs = {}
    for entity_id, (evaluation_id, candidate_id) in latest.items():
        open_flags = []
        for (ev, _provider), flags in audits.items():
            if ev != evaluation_id:
                continue
            for f in flags:
                # verdict 欠落は「要確認」として扱う (site._audit_entries の
                # LLM 版監査に対する規約と揃える)
                if f.get("verdict", "flag") != "flag":
                    continue
                check = f.get("check") or ""
                item = f.get("item") or ""
                if dispositions.get((evaluation_id, check, item)) \
                        == DISPOSITION_FALSE_POSITIVE:
                    continue  # 誤検知と処分された flag は数えない (D-6)
                open_flags.append((check, item))
        inputs[entity_id] = {
            "evaluation_id": evaluation_id, "candidate_id": candidate_id,
            "strong_entity_ids": strong_by_cand.get(candidate_id, set()),
            "open_flags": open_flags}
    return inputs


def compute_derived_tiers(store, entities_cfg):
    """全社の導出 tier を計算する。entities_cfg は cfg["entities"] の形
    ({"entities": [企業 dict, ...]})。

    戻り値: {entity_id: derive_tier の戻り値}。DB への記録はしない
    (記録は store.record_tier_snapshot の役目で、export のタイミングに寄せる)。
    """
    inputs = load_tier_inputs(store)
    out = {}
    for ent in (entities_cfg or {}).get("entities", []):
        eid = ent["id"]
        base = ent.get("tier") or "core"
        out[eid] = derive_tier(eid, base,
                               inputs.get(eid) if base == "core" else None)
    return out
