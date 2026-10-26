"""採点の検証・総合点計算・スコア帯。

原則: 根拠の無い項目は null のままにし、点数をでっち上げない。軸の中では
「全部揃わないと総合点を出さない・重み再配分はしない」を守る。

long は技術軸/事業軸の二軸で結論を出す。trl_level が確定して 3 以下
(前商業段階) の場合、事業項目は「未調査で不足」ではなく「段階的に未成立」
であり、事業軸は段階未達 (pre_stage) として評価対象外になる。その場合、
技術軸が揃えば技術軸の総合点で final とする (total_scope="technology")。
trl_level が未確定なら段階未達は適用せず、従来どおり全項目が必須になる。
"""

import math


TECH_ITEMS = ("technology_readiness", "technological_advantage")
PRE_STAGE_MAX_TRL = 3


def item_keys(kind, cfg):
    return list(cfg["scoring"][kind]["weights"].keys())


def validate_items(items, kind, cfg):
    """items: {key: score|None}。形式エラーの一覧を返す(null は許容)。"""
    errors = []
    keys = set(item_keys(kind, cfg))
    for k in keys - set(items):
        errors.append(f"missing_item:{k}")
    for k in set(items) - keys:
        errors.append(f"unknown_item:{k}")
    for k, v in items.items():
        if k not in keys or v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errors.append(f"not_a_number:{k}")
        elif math.isnan(v) or math.isinf(v) or v < 0 or v > 100:
            errors.append(f"out_of_range:{k}")
    return errors


def band_for(score, cfg):
    best = None
    for b in cfg["scoring"]["bands"]:
        if score >= b["min"]:
            best = b
    return {"label": best["label"], "description": best["description"]}


def _axis_total(items, weights):
    s = sum(weights.values())
    return round(sum(items[k] * w for k, w in weights.items()) / s, 1) if s else None


def compute(items, kind, cfg, trl_level=None):
    """-> {status: final|hold|error, total, band, missing, critical_missing, errors,
    axes, business_stage, total_scope}"""
    errors = validate_items(items, kind, cfg)
    base = {"axes": None, "business_stage": None, "total_scope": None}
    if errors:
        return {"status": "error", "total": None, "band": None, "missing": [],
                "critical_missing": [], "errors": errors, **base}
    sec = cfg["scoring"][kind]
    missing = [k for k in sec["weights"] if items[k] is None]
    critical = [k for k in sec["required"] if items[k] is None]
    if kind != "long":
        if missing:
            return {"status": "hold", "total": None, "band": None, "missing": missing,
                    "critical_missing": critical, "errors": [], **base}
        total = round(sum(items[k] * w for k, w in sec["weights"].items()) / 100.0, 1)
        return {"status": "final", "total": total, "band": band_for(total, cfg),
                "missing": [], "critical_missing": [], "errors": [], **base, "total_scope": "full"}
    weights = sec["weights"]
    tech_w = {k: weights[k] for k in TECH_ITEMS if k in weights}
    biz_w = {k: w for k, w in weights.items() if k not in tech_w}
    tech_missing = [k for k in tech_w if items[k] is None]
    biz_missing = [k for k in biz_w if items[k] is None]
    pre_stage = (isinstance(trl_level, int) and not isinstance(trl_level, bool)
                 and trl_level <= PRE_STAGE_MAX_TRL)
    axes = {
        "technology": {"status": "hold" if tech_missing else "final",
                       "total": None if tech_missing else _axis_total(items, tech_w),
                       "missing": tech_missing},
        "business": {"status": "pre_stage" if pre_stage else ("hold" if biz_missing else "final"),
                     "total": None if biz_missing else _axis_total(items, biz_w),
                     "missing": biz_missing},
    }
    if pre_stage:
        critical = [k for k in critical if k in tech_w]  # 事業側の必須項目は段階未達では不足に数えない
    out = {"missing": missing, "critical_missing": critical, "errors": [], "axes": axes,
           "business_stage": "pre_stage" if pre_stage else None, "total_scope": None}
    if not missing:
        total = round(sum(items[k] * w for k, w in weights.items()) / 100.0, 1)
        return {"status": "final", "total": total, "band": band_for(total, cfg),
                **out, "total_scope": "full"}
    if pre_stage and not tech_missing:
        total = axes["technology"]["total"]
        return {"status": "final", "total": total, "band": band_for(total, cfg),
                **out, "total_scope": "technology"}
    return {"status": "hold", "total": None, "band": None, **out}
