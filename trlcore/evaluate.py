"""AI応答の検証と評価の実行(根拠抽出 → 長期 / イベント)。"""

import hashlib
import json
from .errors import HttpError, LLMError, ResponseShapeError
from .dates import freshness_info
from .http import redact
from .scoring import compute, item_keys, reference_items
from .prompts import load_prompt, render
from .llm import extract_json
from .market import format_reaction


EVIDENCE_KINDS = {"fact", "inference", "forecast", "hypothesis"}

# 参考項目が応答から丸ごと省略された場合に、不足コードとして記録する値
# (N-1)。LLM の出す missing_codes の語彙ではなく、検証側が「項目欠落」
# という記録の状態を表すために付けるコード。サイト表示の不足理由
# (missing_info) には混ぜない
ITEM_OMITTED_CODE = "item_omitted"


def reference_item_state(detail):
    """参考項目1件の充足状態 (N-1 の観測用): scored (得点あり) /
    explicit_null (項目は出たが score=null で不足理由つき) /
    omitted (項目自体が省略された)。"""
    if not isinstance(detail, dict):
        return "omitted"
    if detail.get("score") is not None:
        return "scored"
    if ITEM_OMITTED_CODE in (detail.get("missing_codes") or []):
        return "omitted"
    return "explicit_null"


EXTRACT_LISTS = ("facts", "inferences", "forecasts", "hypotheses", "counter_points", "missing_info")


STATEMENT_LISTS = ("facts", "inferences", "forecasts", "hypotheses")

# 抽出 v3 の claim 共通封筒 (凍結バッチ A1〜A5・A10、FREEZE-PLAN)。
# リスト名から決まる kind はコードが付与する (LLM に重複させない)。
CLAIM_LISTS = ("facts", "inferences", "forecasts", "hypotheses", "counter_points")
CLAIM_KINDS = {"facts": "fact", "inferences": "inference", "forecasts": "forecast",
               "hypotheses": "hypothesis", "counter_points": "counter"}
CLAIM_STATUSES = ("observed", "completed", "ongoing", "planned", "announced",
                  "delayed", "failed", "terminated", "withdrawn", "corrected",
                  "retracted", "superseded")
SUBJECT_TYPES = ("company", "product", "technology", "component", "platform",
                 "substance", "other")
SUBJECT_ROLES = ("primary", "supporting", "comparator", "other")
POLARITIES = ("positive", "negative", "neutral")
FACET_KEYS = ("study", "deployment", "regulatory", "ip")
EVENT_STATUSES = ("anticipated", "occurred", "delayed", "cancelled", "completed")


def validate_extraction(obj):
    errors = []
    for k in EXTRACT_LISTS:
        if not isinstance(obj.get(k), list):
            errors.append(f"extraction.{k}:must_be_list")
    for k in STATEMENT_LISTS:
        for i, it in enumerate(obj.get(k) or []):
            if not isinstance(it, dict) or not str(it.get("statement", "")).strip():
                errors.append(f"extraction.{k}[{i}]:statement_required")
    # counter_points は v3 から claim 構造。文字列の要素も受け付ける
    # (正規化で claim 化する)。dict なら statement が必須。
    for i, it in enumerate(obj.get("counter_points") or []):
        if not isinstance(it, str) and not (
                isinstance(it, dict) and str(it.get("statement", "")).strip()):
            errors.append(f"extraction.counter_points[{i}]:statement_required")
    # 技術帰属 (監査要項④): facts の subject は原文スパンの文字列でのみ受け付ける。
    # 欠落は許容する (帰属を特定できない fact もありうるため、監査側で未分類として扱う)
    for i, it in enumerate(obj.get("facts") or []):
        if isinstance(it, dict) and "subject" in it and it["subject"] is not None \
                and not isinstance(it["subject"], str):
            errors.append(f"extraction.facts[{i}]:subject_must_be_string")
    return errors


def _clean_claim(it):
    """claim 共通封筒の正規化。statement 以外は全て任意で、型の合わない値や
    語彙外の値は落とすだけにする (抽出を失敗させない。parse_direction と
    同じ流儀)。旧形式の要素 (subject だけの fact 等) もそのまま通る。"""
    out = {"statement": str(it.get("statement") or "").strip()}
    for f in ("quote", "source", "subject", "event_date", "period_start", "period_end"):
        v = it.get(f)
        if isinstance(v, str) and v.strip():
            out[f] = v.strip()
    for f, vocab in (("subject_type", SUBJECT_TYPES), ("subject_role", SUBJECT_ROLES),
                     ("polarity", POLARITIES), ("status", CLAIM_STATUSES)):
        if it.get(f) in vocab:
            out[f] = it[f]
    if isinstance(it.get("grounded"), bool):
        out["grounded"] = it["grounded"]
    if isinstance(it.get("subject_aliases"), list):
        aliases = [x.strip() for x in it["subject_aliases"]
                   if isinstance(x, str) and x.strip()]
        if aliases:
            out["subject_aliases"] = aliases
    rel = []
    if isinstance(it.get("relations"), list):
        for r in it["relations"]:
            if isinstance(r, dict) and str(r.get("predicate") or "").strip() \
                    and str(r.get("object") or "").strip():
                e = {"predicate": str(r["predicate"]).strip(),
                     "object": str(r["object"]).strip()}
                if isinstance(r.get("object_type"), str) and r["object_type"].strip():
                    e["object_type"] = r["object_type"].strip()
                rel.append(e)
    if rel:
        out["relations"] = rel
    qs = []
    if isinstance(it.get("quantities"), list):
        for q in it["quantities"]:
            if isinstance(q, dict) and str(q.get("metric") or "").strip() \
                    and str(q.get("value") or "").strip():
                e = {"metric": str(q["metric"]).strip(), "value": str(q["value"]).strip()}
                for f in ("comparator", "unit", "currency", "period", "quote"):
                    if isinstance(q.get(f), str) and q[f].strip():
                        e[f] = q[f].strip()
                qs.append(e)
    if qs:
        out["quantities"] = qs
    ids = []
    if isinstance(it.get("identifiers"), list):
        for d in it["identifiers"]:
            if isinstance(d, dict) and str(d.get("value") or "").strip():
                ids.append({"type": str(d.get("type") or "other").strip(),
                            "value": str(d["value"]).strip()})
    if ids:
        out["identifiers"] = ids
    fac = {}
    if isinstance(it.get("facets"), dict):
        for fk in FACET_KEYS:
            fv = it["facets"].get(fk)
            if isinstance(fv, dict):
                clean = {}
                for k, v in fv.items():
                    if isinstance(v, bool) or v is None:
                        continue
                    if isinstance(v, (int, float)):
                        clean[k] = v
                    elif isinstance(v, str) and v.strip():
                        clean[k] = v.strip()
                if clean:
                    fac[fk] = clean
    if fac:
        out["facets"] = fac
    return out


def normalize_extraction(obj):
    """抽出結果を v3 の claim 構造へ正規化し、局所 ID (C1, C2, ...) と
    kind をコードで付与する (A1・A6 のトレーサビリティの土台)。
    missing_info は文字列リストのまま残す。"""
    out = dict(obj)
    n = 0
    for key in CLAIM_LISTS:
        claims = []
        for it in obj.get(key) or []:
            if isinstance(it, str):
                it = {"statement": it}
            if not isinstance(it, dict):
                continue
            c = _clean_claim(it)
            if not c["statement"]:
                continue
            n += 1
            c["id"] = f"C{n}"
            c["kind"] = CLAIM_KINDS[key]
            claims.append(c)
        out[key] = claims
    ps = obj.get("primary_subjects")
    out["primary_subjects"] = [x.strip() for x in ps
                               if isinstance(x, str) and x.strip()] \
        if isinstance(ps, list) else []
    return out


def _span_norm(s):
    """原文スパン照合用の正規化。空白の畳み込みと casefold のみで、
    言い換えや表記ゆれ (全角・半角等) は同一視しない。"""
    return " ".join(str(s or "").split()).casefold()


def validate_subject_spans(extraction, cand):
    """claim の subject (技術帰属) が候補本文の原文スパンか照合する (N-2)。

    subject は「原文スパン」が設計 (extract の規約) だが、従来は文字列型
    しか検証しておらず、原文に存在しない名称 (言い換え・幻覚) もそのまま
    貯まっていた。ここでは抽出器に渡した候補本文 (タイトル・要旨・
    fields — _candidate_block と同じ材料) を正本として照合し、原文に
    無い subject は claim から落とす (claim 自体は残す)。照合は空白の
    畳み込みと casefold の上での部分文字列一致。
    -> (extraction, {"claims": 総数, "with_subject": 照合前の付与数,
                     "dropped": 落とした数})。落とす対象が無ければ元の
    オブジェクトをそのまま返す (コピーしない)。"""
    stats = {"claims": 0, "with_subject": 0, "dropped": 0}
    if not isinstance(extraction, dict):
        return extraction, stats
    fields = cand.get("fields") if isinstance(cand, dict) else None
    body = _span_norm(" ".join([
        str(cand.get("title") or ""), str(cand.get("abstract") or ""),
        " ".join(str(f) for f in fields if f) if isinstance(fields, list) else ""]))
    plan = {}  # list名 -> [(index, subject)]
    for key in CLAIM_LISTS:
        for i, c in enumerate(extraction.get(key) or []):
            if not isinstance(c, dict):
                continue
            stats["claims"] += 1
            subj = c.get("subject")
            if isinstance(subj, str) and subj.strip():
                stats["with_subject"] += 1
                if _span_norm(subj) not in body:
                    plan.setdefault(key, []).append(i)
                    stats["dropped"] += 1
    if not plan:
        return extraction, stats
    out = dict(extraction)
    for key, idxs in plan.items():
        lst = list(out.get(key) or [])
        for i in idxs:
            c = dict(lst[i])
            c.pop("subject", None)
            lst[i] = c
        out[key] = lst
    return out, stats


def _nonempty_list(v):
    return isinstance(v, list) and any(str(x).strip() for x in v)


def _validate_one_item(key, it, scores, details, errors):
    """1項目分の検証 (採点項目・参考項目で共通の規律)。"""
    score = it.get("score")
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float))
                              or score != score or score in (float("inf"), float("-inf"))):
        # 型の検証はここで行う(範囲 0-100 は scoring 側の out_of_range が担当)
        errors.append(f"bad_score:{key}")
    scores[key] = score
    ev = it.get("evidence")
    if score is not None:
        if not str(it.get("reason", "")).strip():
            errors.append(f"reason_required:{key}")
        if not isinstance(ev, list) or not ev:
            errors.append(f"evidence_required:{key}")
        else:
            for j, e in enumerate(ev):
                if not isinstance(e, dict) or not str(e.get("ref", "")).strip() \
                        or not str(e.get("summary", "")).strip() or e.get("kind") not in EVIDENCE_KINDS:
                    errors.append(f"bad_evidence:{key}[{j}]")
    elif not _nonempty_list(it.get("missing_info")):
        errors.append(f"missing_info_required_for_null:{key}")
    for f in ("counter_evidence", "missing_info"):
        if it.get(f) is not None and not isinstance(it.get(f), list):
            errors.append(f"{f}:must_be_list:{key}")
    # トレーサビリティ (凍結バッチ A6・A9): 根拠 claim ID・制約コード・
    # 減点コード・不足コード。任意項目で、あれば list として取り込む。
    # 未知のコードはそのまま保存し、監査側で未分類として扱う (A9 の拡張規則)
    for f in ("basis_claim_ids", "constraint_codes", "deduction_codes", "missing_codes"):
        if it.get(f) is not None and not isinstance(it.get(f), list):
            errors.append(f"{f}:must_be_list:{key}")
    details[key] = {"score": score, "reason": it.get("reason", ""), "evidence": ev or [],
                    "counter_evidence": it.get("counter_evidence") or [],
                    "missing_info": it.get("missing_info") or []}
    for f in ("basis_claim_ids", "constraint_codes", "deduction_codes", "missing_codes"):
        if isinstance(it.get(f), list):
            details[key][f] = [str(x) for x in it[f]]


def validate_assessment(obj, kind, cfg):
    """-> (scores, details, errors)。スコア無根拠(理由/出典なし)や不足理由なしのnullは不正。"""
    errors, scores, details = [], {}, {}
    items = obj.get("items")
    if not isinstance(items, dict):
        return {}, {}, ["items:must_be_object"]
    for key in item_keys(kind, cfg):
        it = items.get(key)
        if not isinstance(it, dict):
            errors.append(f"missing_item:{key}")
            continue
        _validate_one_item(key, it, scores, details, errors)
    # 参考項目 (規程第13条): 出力があれば採点項目と同じ規律で検証して取り込む。
    # 欠落していてもエラーにはせず null として記録する (参考項目の不足で
    # 評価自体を失敗・保留にしない)。ただし項目自体が省略された場合は
    # 「理由のない null」で記録せず、欠落した旨を不足コード
    # (ITEM_OMITTED_CODE) で記録する (N-1: 明示 null との区別と充足の観測)
    for key in reference_items(kind, cfg):
        it = items.get(key)
        if isinstance(it, dict):
            _validate_one_item(key, it, scores, details, errors)
        else:
            scores[key] = None
            details[key] = {"score": None, "reason": "", "evidence": [],
                            "counter_evidence": [], "missing_info": [],
                            "missing_codes": [ITEM_OMITTED_CODE]}
    for f in ("next_sources", "reeval_conditions"):
        if obj.get(f) is not None and not isinstance(obj.get(f), list):
            errors.append(f"{f}:must_be_list")
    if kind == "event":
        ev = obj.get("event")
        if not isinstance(ev, dict):
            errors.append("event:must_be_object")
        elif ev.get("classification") not in (None, 1, 2, 3, 4):
            errors.append("event.classification:invalid")
    return scores, details, errors


def parse_trl_level(v):
    """長期評価の trl_level(任意)。-> (dict|None, errors)。level は 1〜9 の整数か null。
    整数のときは reason と evidence が必須。換算や補完はしない。"""
    if v is None:
        return None, []
    if not isinstance(v, dict):
        return None, ["trl_level:must_be_object"]
    lv = v.get("level")
    out = {"level": None, "reason": str(v.get("reason") or ""), "evidence": v.get("evidence") or [],
           "missing_info": v.get("missing_info") or []}
    if isinstance(v.get("basis_claim_ids"), list):
        out["basis_claim_ids"] = [str(x) for x in v["basis_claim_ids"]]
    if lv is None:
        return out, []
    if isinstance(lv, bool) or not isinstance(lv, int) or not 1 <= lv <= 9:
        return None, ["trl_level.level:must_be_1_to_9_or_null"]
    errs = []
    if not out["reason"].strip():
        errs.append("trl_level:reason_required")
    ev = out["evidence"]
    if not isinstance(ev, list) or not ev or any(
            not isinstance(e, dict) or not str(e.get("ref", "")).strip() or not str(e.get("summary", "")).strip()
            or e.get("kind") not in EVIDENCE_KINDS for e in ev):
        errs.append("trl_level:evidence_required")
    out["level"] = lv
    return out, errs


def parse_trl_levels(v):
    """技術ごとの TRL (long v3、凍結バッチ A2)。代表値 (trl_level) を決める
    既存規律は変えず、内訳を補助情報として保持する。任意項目のため、
    壊れた要素は捨てるだけで評価自体は失敗させない (parse_direction と
    同じ流儀)。subject が無く level が範囲外の要素は捨てる。"""
    if not isinstance(v, list):
        return []
    out = []
    for it in v:
        if not isinstance(it, dict):
            continue
        subj = str(it.get("subject") or "").strip()
        lv = it.get("level")
        if not subj or (lv is not None and (isinstance(lv, bool)
                                            or not isinstance(lv, int)
                                            or not 1 <= lv <= 9)):
            continue
        e = {"subject": subj, "level": lv}
        for f in ("environment", "repetition", "duration", "reason"):
            if isinstance(it.get(f), str) and it[f].strip():
                e[f] = it[f].strip()
        if isinstance(it.get("basis_claim_ids"), list):
            e["basis_claim_ids"] = [str(x) for x in it["basis_claim_ids"]]
        out.append(e)
    return out


def normalize_event(ev):
    """event v3 の構造化フィールド (凍結バッチ A7) の正規化。status は
    制御語彙のみ残し、date_window / expectation / actual は dict のとき
    だけ残す。旧形式 (これらが無い) はそのまま通る。"""
    if not isinstance(ev, dict):
        return ev
    if ev.get("status") is not None and ev.get("status") not in EVENT_STATUSES:
        ev["status"] = None
    for f in ("date_window", "expectation", "actual"):
        if f in ev and not isinstance(ev.get(f), dict):
            ev.pop(f)
    return ev


DIRECTIONS = ("positive", "negative", "mixed")


def parse_direction(v):
    """材料の方向性(任意・表示補助)。壊れた値や欠落は None として扱い、評価自体は失敗させない。"""
    if not isinstance(v, dict):
        return None
    d = v.get("direction")
    if d not in DIRECTIONS:
        return None
    return {"direction": d, "reason": str(v.get("reason") or "")[:300]}


def _candidate_block(cand):
    return json.dumps({k: cand.get(k) for k in ("id", "title", "abstract", "url", "source", "published", "fields")},
                      ensure_ascii=False, indent=1)


def input_hash(cand, as_of):
    # as_of の日付そのものではなく鮮度バケットをキーにする。抽出で as_of が効くのは
    # バケット (30日/180日の境界) だけなので、日付まで含めると実行を跨ぐたびに
    # キャッシュがミスして再抽出のコストがかかる
    bucket = freshness_info(cand.get("published"), as_of)["bucket"]
    s = "|".join(str(cand.get(k)) for k in ("title", "abstract", "url", "published")) + "|" + bucket
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def run_extraction(cand, cfg, llm, as_of, prompt_dir=None):
    p = load_prompt("extract", cfg["scoring"]["prompts"]["extract"], prompt_dir)
    text = render(p["text"], {"as_of": as_of.isoformat(), "candidate": _candidate_block(cand),
                              "freshness": json.dumps(freshness_info(cand["published"], as_of))})
    obj = extract_json(llm.complete(text, purpose="extract", candidate_id=cand.get("id")))
    errs = validate_extraction(obj)
    if errs:
        raise LLMError("invalid extraction: " + ",".join(errs[:5]))
    return normalize_extraction(obj), {"name": "extract", "version": p["version"], "sha": p["sha"]}


def _used_model(llm):
    """その呼び出しに実際に応答したモデル名。プロバイダが last_model を持てば
    (フォールバック時は代替モデル)、無ければ主モデル名にフォールバックする。"""
    return getattr(llm, "last_model", None) or getattr(llm, "model", None)


def evaluate_one(cand, kind, cfg, llm, as_of, extraction=None, extract_meta=None, prompt_dir=None,
                 related=None, financials=None, market_reaction=None):
    """1候補・1系統の評価。失敗しても例外を投げず status=error の結果を返す。"""
    versions = {}
    result = {"kind": kind, "as_of": as_of.isoformat(), "status": "error", "total": None, "band": None,
              "items": {}, "extraction": extraction, "event": None, "next_sources": [], "reeval_conditions": [],
              "missing": [], "critical_missing": [], "errors": [], "prompt_versions": versions,
              "config_version": cfg["config_version"], "config_hash": cfg["config_hash"],
              "llm_provider": getattr(llm, "name", None), "llm_model": getattr(llm, "model", None),
              "input_hash": input_hash(cand, as_of), "extra": {}}
    try:
        if extraction is None:
            extraction, meta = run_extraction(cand, cfg, llm, as_of, prompt_dir)
            result["extraction"], extract_meta = extraction, meta
            result["extraction_model"] = _used_model(llm)  # 抽出キャッシュのキーに使う実モデル
        # 技術帰属 subject の原文スパン照合 (N-2): 新規抽出・キャッシュ・
        # 系統間の引き継ぎのどの経路でも、採点に使う前に一度ここを通す。
        # 照合済みの抽出は冪等 (落とした subject は戻らない) ため、系統を
        # 跨いで再照合しても結果は変わらない
        extraction, subj_stats = validate_subject_spans(extraction, cand)
        result["extraction"] = extraction
        result["extra"]["subject_stats"] = subj_stats
        versions["extract"] = extract_meta
        p = load_prompt(kind, cfg["scoring"]["prompts"][kind], prompt_dir)
        versions[kind] = {"name": kind, "version": p["version"], "sha": p["sha"]}
        result["extra"]["related_news_count"] = len(related or [])
        result["extra"]["related_financials_count"] = len(financials or [])
        if kind == "event":
            result["extra"]["market_reaction"] = market_reaction[0] if market_reaction else None
        reaction_text = ""
        if market_reaction:
            reaction_text = format_reaction(market_reaction[0], market_reaction[1],
                                            market_reaction[2], cand.get("source"))
        text = render(p["text"], {"as_of": as_of.isoformat(), "candidate": _candidate_block(cand),
                                  "extraction": json.dumps(extraction, ensure_ascii=False),
                                  "related": json.dumps(related or [], ensure_ascii=False),
                                  "financials": json.dumps(financials or [], ensure_ascii=False),
                                  "market_reaction": reaction_text,
                                  "item_keys": ", ".join(item_keys(kind, cfg))})
        # 検証エラー(項目の欠落などモデルの出力不備)は、不足点を伝えて1回だけ再生成する。
        # API失敗(HttpError)はここでは扱わず、従来どおり未評価のまま残す。
        feedback = ""
        obj, scores, details, errs = None, None, None, []
        for attempt in (1, 2):
            result["extra"]["assess_attempts"] = attempt
            try:
                obj = extract_json(llm.complete(text + feedback, purpose="assess",
                                                candidate_id=cand.get("id")))
            except LLMError as e:
                obj, errs = None, [f"{type(e).__name__}: {redact(e)}"[:300]]
            if obj is not None:
                scores, details, errs = validate_assessment(obj, kind, cfg)
                if kind == "long":
                    trl, trl_errs = parse_trl_level(obj.get("trl_level"))
                    errs = errs + trl_errs
                    result["extra"]["trl_level"] = trl
                    levels = parse_trl_levels(obj.get("trl_levels"))
                    if levels:
                        result["extra"]["trl_levels"] = levels
                    result["extra"]["direction"] = parse_direction(obj.get("direction"))
            if not errs:
                break
            feedback = ("\n\n[再出力の指示] 前回の出力には次の不備がありました: " + ", ".join(errs[:8])
                        + "。items には評価項目キーの全項目を1つも省略せずに含め、材料が無い項目は score=null と missing_info で出すこと。"
                          "その他の指定フィールドも全て含めて、JSON のみを再出力してください。")
        result["llm_model"] = _used_model(llm)  # 査定に実際に応答したモデルを来歴として記録する
        if errs:
            result["errors"] = errs
            return result
        if kind == "event":
            ev = normalize_event(obj.get("event") or {})
            result["event"] = ev
            reaction_ok = bool(market_reaction and market_reaction[0]
                               and market_reaction[0].get("available"))
            if not str(ev.get("market_expectation", "") or "").strip() and not reaction_ok:
                # 市場予想が確認できず、株価反応データも無い → 分類は確定せず、
                # 乖離・織り込み度は根拠なしとして保留。反応データがある場合は
                # 案A (2026-11-06): 反応を乖離の事後的証拠として採点を生かす
                ev["classification"] = None
                for k in ("expectation_gap", "not_priced_in"):
                    if scores.get(k) is not None:
                        scores[k] = None
                        details[k]["score"] = None
                        details[k]["missing_info"] = list(details[k]["missing_info"]) + ["市場予想が未確認のため保留"]
        trl_lv = (result["extra"].get("trl_level") or {}).get("level") if kind == "long" else None
        res = compute(scores, kind, cfg, trl_level=trl_lv)
        result["extra"].update({"axes": res.get("axes"), "business_stage": res.get("business_stage"),
                                 "total_scope": res.get("total_scope")})
        result.update({"status": res["status"], "total": res["total"], "band": res["band"], "items": details,
                       "missing": res["missing"], "critical_missing": res["critical_missing"], "errors": res["errors"],
                       "next_sources": [str(x) for x in obj.get("next_sources") or []],
                       "reeval_conditions": [str(x) for x in obj.get("reeval_conditions") or []]})
    except Exception as e:  # LLMError, HttpError, PromptError など。1候補の失敗で全体を止めない
        result["errors"] = [f"{type(e).__name__}: {redact(e)}"[:300]]
        if isinstance(e, HttpError):
            # API側の失敗(429/5xx/タイムアウト/認証・モデル名の誤り等)。論文の内容に起因しないため、
            # 呼び出し側は評価結果として保存せず、候補を未評価のまま残す
            result["infra_failure"] = {"transient": e.transient, "status": e.status}
        elif isinstance(e, ResponseShapeError):
            # 応答形状の異常(候補空・安全フィルタ等)も API 側事情であり内容エラーではない
            result["infra_failure"] = {"transient": True, "status": None}
    return result
