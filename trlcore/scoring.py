"""採点の検証・総合点計算・スコア帯。nullを含む場合は総合点を確定せず hold にする(重み再配分はしない)。"""

import math


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


def compute(items, kind, cfg):
    """-> {status: final|hold|error, total, band, missing, critical_missing, errors}"""
    errors = validate_items(items, kind, cfg)
    if errors:
        return {"status": "error", "total": None, "band": None, "missing": [],
                "critical_missing": [], "errors": errors}
    sec = cfg["scoring"][kind]
    missing = [k for k in sec["weights"] if items[k] is None]
    critical = [k for k in sec["required"] if items[k] is None]
    if missing:
        return {"status": "hold", "total": None, "band": None, "missing": missing,
                "critical_missing": critical, "errors": []}
    total = round(sum(items[k] * w for k, w in sec["weights"].items()) / 100.0, 1)
    return {"status": "final", "total": total, "band": band_for(total, cfg),
            "missing": [], "critical_missing": [], "errors": []}
