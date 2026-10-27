"""AI応答の検証と評価の実行(根拠抽出 → 長期 / イベント)。"""

import hashlib
import json
from .errors import HttpError, LLMError
from .dates import freshness_info
from .http import redact
from .scoring import compute, item_keys
from .prompts import load_prompt, render
from .llm import extract_json


EVIDENCE_KINDS = {"fact", "inference", "forecast", "hypothesis"}


EXTRACT_LISTS = ("facts", "inferences", "forecasts", "hypotheses", "counter_points", "missing_info")


STATEMENT_LISTS = ("facts", "inferences", "forecasts", "hypotheses")


def validate_extraction(obj):
    errors = []
    for k in EXTRACT_LISTS:
        if not isinstance(obj.get(k), list):
            errors.append(f"extraction.{k}:must_be_list")
    for k in STATEMENT_LISTS:
        for i, it in enumerate(obj.get(k) or []):
            if not isinstance(it, dict) or not str(it.get("statement", "")).strip():
                errors.append(f"extraction.{k}[{i}]:statement_required")
    return errors


def _nonempty_list(v):
    return isinstance(v, list) and any(str(x).strip() for x in v)


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
        score = it.get("score")
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
        details[key] = {"score": score, "reason": it.get("reason", ""), "evidence": ev or [],
                        "counter_evidence": it.get("counter_evidence") or [],
                        "missing_info": it.get("missing_info") or []}
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
    s = "|".join(str(cand.get(k)) for k in ("title", "abstract", "url", "published")) + "|" + as_of.isoformat()
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def run_extraction(cand, cfg, llm, as_of, prompt_dir=None):
    p = load_prompt("extract", cfg["scoring"]["prompts"]["extract"], prompt_dir)
    text = render(p["text"], {"as_of": as_of.isoformat(), "candidate": _candidate_block(cand),
                              "freshness": json.dumps(freshness_info(cand["published"], as_of))})
    obj = extract_json(llm.complete(text))
    errs = validate_extraction(obj)
    if errs:
        raise LLMError("invalid extraction: " + ",".join(errs[:5]))
    return obj, {"name": "extract", "version": p["version"], "sha": p["sha"]}


def evaluate_one(cand, kind, cfg, llm, as_of, extraction=None, extract_meta=None, prompt_dir=None,
                 related=None, financials=None):
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
        versions["extract"] = extract_meta
        p = load_prompt(kind, cfg["scoring"]["prompts"][kind], prompt_dir)
        versions[kind] = {"name": kind, "version": p["version"], "sha": p["sha"]}
        result["extra"]["related_news_count"] = len(related or [])
        result["extra"]["related_financials_count"] = len(financials or [])
        text = render(p["text"], {"as_of": as_of.isoformat(), "candidate": _candidate_block(cand),
                                  "extraction": json.dumps(extraction, ensure_ascii=False),
                                  "related": json.dumps(related or [], ensure_ascii=False),
                                  "financials": json.dumps(financials or [], ensure_ascii=False),
                                  "item_keys": ", ".join(item_keys(kind, cfg))})
        obj = extract_json(llm.complete(text))
        scores, details, errs = validate_assessment(obj, kind, cfg)
        if kind == "long":
            trl, trl_errs = parse_trl_level(obj.get("trl_level"))
            errs = errs + trl_errs
            result["extra"]["trl_level"] = trl
            result["extra"]["direction"] = parse_direction(obj.get("direction"))
        if errs:
            result["errors"] = errs
            return result
        if kind == "event":
            ev = obj.get("event") or {}
            result["event"] = ev
            if not str(ev.get("market_expectation", "") or "").strip():
                # 市場予想が確認できない → 分類は確定せず、乖離・織り込み度は根拠なしとして保留
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
    return result
