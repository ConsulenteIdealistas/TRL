"""ポジティブ監査 (過小評価の疑い)。ネガティブ監査 (A1〜A15, E1/E2) の鏡写し。

方針 (既存の監査と同じ):
- 元の評価 (点・状態・TRL) には触れない。指摘の記録まで。昇格・降格は人間が決める。
- 決定的な規則だけ。LLM も API も使わない。同じ入力なら必ず同じ結果。
- 出力は既存の flags と同じ形 {check, item, verdict, rule, detail}。
  check は P1〜P6。verdict は flag (要確認) / gray (判定保留) のまま。
  ここでの『要確認』は『過小評価の疑い』を意味する。
- 評価規程の原則を壊さない: 商用マーカーを TRL の根拠にしない (原則1)、
  計画・予定は数えない (第10条1)、結果待ちの不足は不足として数えない (第7条2)。

audit.py への取り込みは、このファイルを audit.py と同じパッケージに置き、
audit.py の末尾 (ヘルパー定義より後) に次の4行を足す:

    from .audit_positive import (POSITIVE_CHECK_FUNCS, POSITIVE_RULES_CHECKS,
                                 POSITIVE_EVENT_CHECKS, POSITIVE_CHECKS)
    _CHECK_FUNCS.update(POSITIVE_CHECK_FUNCS)
    RULES_CHECKS = RULES_CHECKS + POSITIVE_RULES_CHECKS
    EVENT_CHECKS = EVENT_CHECKS + POSITIVE_EVENT_CHECKS
    AUDIT_CHECKS = AUDIT_CHECKS + POSITIVE_CHECKS
"""
import re

from .audit import TECH_ITEMS, _evidence_text, _find, _norm_text, _quote, _scored_items


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _sentence(text, span):
    """span を含む1文 (。！？改行で区切る) を返す。"""
    s, e = span
    start = max(text.rfind(c, 0, s) for c in "。！？!?\n") + 1
    ends = [i for i in (text.find(c, e) for c in "。！？!?\n") if i >= 0]
    end = min(ends) if ends else len(text)
    return text[start:end]


def _is_plan(sentence, chk):
    return bool(_find(chk.get("plan_markers"), sentence))


def _is_design(sentence, chk):
    """試験デザイン・目的を述べる文か (v4.1)。結果の報告文とは区別する。"""
    return bool(_find(chk.get("design_markers"), sentence))


def _check_p1(inp, chk, ext_text, out):
    """P1 TRL 9 の条件を満たしている可能性 (A1/A2 の鏡)。"""
    trl = inp.get("trl_level") or {}
    level = trl.get("level")
    if not _is_num(level) or level not in (chk.get("levels") or [7, 8]):
        return
    items = inp.get("items") or {}
    side = _norm_text(" ".join([_evidence_text(items.get("technology_readiness")),
                                _evidence_text(trl), ext_text]))
    by_sector = chk.get("confirm_by_sector") or {}
    sector = inp.get("sector")
    pats = by_sector.get(sector)
    if not pats:
        # セクター別の確認語彙が引けない場合は発火させない。全セクターの
        # 語彙を束ねて照合すると、セクター別の証拠基準 (原則2・第11条) と
        # 矛盾し過検知になるため (A2 の束ね判定とは扱いを分ける。P1 は
        # 「低すぎる疑い」の能動的な指摘なので、基準が定まらない評価には
        # 付けない)。
        return
    scope = sector
    # 計画・予定の文に当たった語彙は飛ばし、実施済みの文に当たったものだけ使う。
    # 試験デザイン・目的の文 (design_markers) に当たったものは候補として保持し、
    # 非デザインのヒットが他にあればそちらを優先する。デザインのヒットしか
    # 無い場合は verdict を gray に上限する (v4.1: 試験目的文へのヒットを
    # 要確認にしないための校正)。
    hit = sent = None
    design_hit = design_sent = None
    for p in pats:
        for m in re.finditer(_pattern_regex(p), side):
            s = _sentence(side, m.span())
            if _is_plan(s, chk):
                continue
            if _is_design(s, chk):
                if design_hit is None:
                    design_hit, design_sent = (p, m.group(0), m.span()), s
                continue
            hit, sent = (p, m.group(0), m.span()), s
            break
        if hit:
            break
    capped = False
    if hit is None:
        if design_hit is None:
            return
        hit, sent, capped = design_hit, design_sent, True
    p, matched, span = hit
    same = _find(chk.get("count_patterns"), sent)
    elsewhere = None if same else _find(chk.get("count_patterns"), side)
    if same and not capped:
        verdict, cnt = "flag", same[1]
        basis = f"同じ文に回数・期間「{cnt}」の明記もある"
    elif same:
        verdict, cnt = "gray", same[1]
        basis = f"同じ文に回数・期間「{cnt}」の明記もある"
    elif elsewhere:
        verdict, cnt = "gray", elsewhere[1]
        basis = f"別の文に回数・期間「{cnt}」の明記がある (同じ文ではないため判定保留)"
    else:
        verdict = "gray"
        basis = "回数・期間の明記は見当たらないため判定保留"
    if capped:
        basis += "。ただし当たった文は試験デザイン・目的の記述のため判定保留 (グレー) とする"
    out.append({
        "check": "P1", "item": "trl_level", "verdict": verdict,
        "rule": f"confirm_by_sector:{scope}",
        "detail": f"TRL {level} だが、根拠側にセクター {scope} の継続・繰り返しの"
                  f"確認語彙「{matched}」があり、{basis}。TRL 9 の条件 (原則2) を"
                  f"満たす可能性がある (第10条2の切り下げが働いた疑い)。"
                  f"文面: 『{_quote(side, span)}』"})


def _pattern_regex(p):
    """_find と同じ解釈 (NFKC 正規化、substr は完全一致) で、finditer 用の正規表現を返す。"""
    import unicodedata
    pat = unicodedata.normalize("NFKC", p.get("pattern") or "")
    return pat if p.get("kind") == "regex" else re.escape(pat)


def _check_p2(inp, chk, ext_text, out):
    """P2 保留のままの豊富な事実 (A8 の鏡)。観測 (gray) のみ。

    出典の種類数は、抽出 facts の source と材料プール (build_audit_input が
    監査時点で再導出した関連ニュース・財務根拠) の出典識別子の和集合で
    数える (v4.1)。facts の source だけでは、抽出が候補 (記事) 単位で
    常に単一出典になるため min_sources を構造的に満たせなかった。"""
    if inp.get("status") != "hold" or not inp.get("companies"):
        return  # 企業リンクが無い保留は『企業未特定』で、設計どおり
    ext = inp.get("extraction")
    facts = ext.get("facts") if isinstance(ext, dict) else None
    if not isinstance(facts, list):
        return
    stmts = [f for f in facts if isinstance(f, dict) and f.get("statement")]
    fact_sources = {f.get("source") for f in stmts if f.get("source")}
    pool_sources = [s for s in (inp.get("pool_sources") or []) if s]
    sources = fact_sources | set(pool_sources)
    if len(stmts) >= chk.get("min_facts", 5) \
            and len(sources) >= chk.get("min_sources", 2):
        pool_part = ""
        if pool_sources:
            uniq_pool = list(dict.fromkeys(pool_sources))
            pool_part = (f" + 材料プール {len(set(pool_sources))} 種類 "
                         f"({'、'.join(uniq_pool[:5])}"
                         f"{' ほか' if len(uniq_pool) > 5 else ''})")
        out.append({
            "check": "P2", "item": "extraction", "verdict": "gray",
            "rule": f"hold&facts>={chk.get('min_facts', 5)}"
                    f"&sources>={chk.get('min_sources', 2)}",
            "detail": f"状態は保留 (hold) だが、抽出された事実が {len(stmts)} 件あり、"
                      f"出典は {len(sources)} 種類 (抽出 {len(fact_sources)} 種類"
                      f"{pool_part}) ある。未採点の必須項目: "
                      f"{', '.join(inp.get('critical_missing') or []) or 'なし'}。"
                      f"拾い漏れか保留の長期化かを確認する。"})


def _check_p3(inp, chk, ext_text, out):
    """P3 根拠が厚いのに低得点 (A9 の鏡)。"""
    smax = chk.get("score_max", 39)
    n_gray = chk.get("min_fact_evidence_gray", 2)
    n_flag = chk.get("min_fact_evidence_flag", 3)
    for key, it in _scored_items(inp).items():
        if not isinstance(it, dict):
            continue
        score = it.get("score")
        if not _is_num(score) or score > smax:
            continue
        if it.get("missing_info") or it.get("counter_evidence"):
            continue  # 不足も反証も記録済みなら、低得点には説明がある
        reason = _norm_text(it.get("reason") or "")
        if any(m in reason for m in chk.get("skip_reason_markers") or []):
            continue  # 結果待ちの不足は正当 (第7条2)
        facts = [e for e in (it.get("evidence") or [])
                 if isinstance(e, dict) and e.get("kind") == "fact"
                 and re.search(r"[0-9]", _norm_text(e.get("summary") or ""))]
        if len(facts) < n_gray:
            continue
        out.append({
            "check": "P3", "item": key,
            "verdict": "flag" if len(facts) >= n_flag else "gray",
            "rule": f"score<={smax}&fact-evidence>={n_gray}",
            "detail": f"項目点が {score} 点 (低得点帯) だが、数値を含む fact の根拠が "
                      f"{len(facts)} 件あり、不足情報 (missing_info) も反証 "
                      f"(counter_evidence) も記録されていない。根拠に対して"
                      f"低く採点されていないか確認する。"})


def _check_p4(inp, chk, ext_text, out):
    """P4 未採点なのに根拠がある (A13 の鏡)。"""
    items = inp.get("items") or {}
    for key in inp.get("critical_missing") or []:
        it = items.get(key)
        if not isinstance(it, dict) or it.get("score") is not None:
            continue
        if (it.get("evidence") or []) and not (it.get("missing_info") or []):
            out.append({
                "check": "P4", "item": key, "verdict": "flag",
                "rule": "critical-missing&evidence",
                "detail": f"必須項目 {key} が未採点 (critical_missing) だが、根拠 "
                          f"(evidence) が {len(it['evidence'])} 件記録され、不足情報は"
                          f"空。採点漏れか、根拠が採点に反映されていない疑い。"})


def _check_p5(inp, chk, ext_text, out):
    """P5 一意の strong があるのに企業項目が未採点 (A14 の鏡)。
    対象は『理由 (missing_info) も根拠 (evidence) も無い未採点』だけ。不足情報で
    説明された未採点は第7条3の正当な null、根拠つきの未採点は P4 の担当。"""
    strong = {_norm_text(c.get("name") or "").lower()
              for c in inp.get("companies") or [] if c.get("strength") == "strong"}
    strong.discard("")
    if len(strong) != 1:
        return  # 第5条2: strong かつ一意に定まる場合だけが対象
    items = inp.get("items") or {}
    biz = []
    for k in inp.get("critical_missing") or []:
        if k in TECH_ITEMS:
            continue
        it = items.get(k) if isinstance(items.get(k), dict) else {}
        if it.get("score") is None and not it.get("missing_info") \
                and not it.get("evidence"):
            biz.append(k)
    if not biz:
        return
    name = next(c.get("name") for c in inp["companies"]
                if c.get("strength") == "strong")
    out.append({
        "check": "P5", "item": "companies", "verdict": "gray",
        "rule": "unique-strong&company-items-unexplained-null",
        "detail": f"企業 {name} が strong で一意に定まるが、企業固有の必須項目 "
                  f"({', '.join(biz)}) が、不足情報も根拠も記録されないまま未採点。"
                  f"第5条2の採点条件は満たしているため、未採点の理由を確認する。"})


def _check_p6(inp, chk, ext_text, out):
    """P6 反応が無いのに『織り込み済み』 (E1 の鏡)。event 評価のみ。"""
    if inp.get("status") != "final":
        return
    mr = inp.get("market_reaction") or {}
    z = mr.get("z")
    item = (inp.get("items") or {}).get("not_priced_in") or {}
    score = item.get("score") if isinstance(item, dict) else None
    zmax, smax = chk.get("z_abs_max", 0.5), chk.get("score_max", 39)
    if mr.get("available") and _is_num(z) and _is_num(score) \
            and abs(z) < zmax and score <= smax:
        out.append({
            "check": "P6", "item": "not_priced_in", "verdict": "flag",
            "rule": f"|z|<{zmax},score<={smax}",
            "detail": f"価格はほとんど反応していない (標準化反応 z={z:.2f}、"
                      f"反応 {mr.get('reaction_pct')}%) のに、not_priced_in が "
                      f"{score} 点 (織り込み済みの側) になっている。実測との整合に"
                      f"疑いがある。"})


POSITIVE_CHECK_FUNCS = {"P1": _check_p1, "P2": _check_p2, "P3": _check_p3,
                        "P4": _check_p4, "P5": _check_p5, "P6": _check_p6}
POSITIVE_RULES_CHECKS = ("P1", "P2", "P3", "P4", "P5")
POSITIVE_EVENT_CHECKS = ("P6",)
POSITIVE_CHECKS = ("P1", "P2", "P3", "P4", "P5", "P6")
