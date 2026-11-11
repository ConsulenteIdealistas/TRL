"""保存済み評価の総合点・帯域の再計算 (規程の A2 類変更への対応)。

重みや帯域のような「集計だけの変更」は、項目点を作り直さずに保存済みの
項目点から総合点を計算し直すことで反映できる。項目点・TRL・方向性には
触れない。書き換えの対象は「総合点か帯域が実際に変わる」評価だけに限る
(ISSUES-2 の G-1): 版 (config_version) の差だけでは書き込まない。版の差
だけの評価を毎回書き換えると、旧世代の評価行が現行世代のラベルを被り、
行内の prompt_versions と矛盾する (世代の正本は as_evaluated と
prompt_versions であって、評価行の config_version ではない)。
実際に点が変わって書き換える評価は config_version / config_hash を
現行世代へ揃え、extra_json に recalculated (変更前バージョン・旧総合点・
旧帯域・日時) の来歴を残す (再計算の事実の記録はこの extra 側に寄せ、
専用の列は足さない)。再計算で final にならない評価 (項目点の欠け等、
通常は起こらない) は異常として数え、書き換えない。
"""

import json
from datetime import datetime, timezone
from .scoring import compute


def recalculate_evaluations(store, cfg, apply=False):
    """-> {"checked", "changed", "anomalies", "details"}。
    apply=False なら書き換えず、変わる内容の報告だけを行う。"""
    cur_version = cfg["config_version"]
    cur_hash = cfg["config_hash"]
    details, changed, anomalies = [], 0, 0
    rows = store.final_evaluations()
    for row in rows:
        raw_items = json.loads(row["items_json"] or "{}")
        if isinstance(raw_items, dict):
            # 保存形式は {項目キー: {"score": ...}} の辞書
            items = {k: (v or {}).get("score") for k, v in raw_items.items()}
        else:
            items = {it["key"]: it.get("score") for it in raw_items}
        extra = json.loads(row["extra_json"] or "{}")
        trl = (extra.get("trl_level") or {}).get("level")
        out = compute(items, row["kind"], cfg, trl if row["kind"] == "long" else None)
        if out["status"] != "final":
            anomalies += 1
            continue
        new_band = (out["band"] or {}).get("label")
        # G-1: 変更の判定は総合点・帯域の変化だけ。版の差は含めない
        differs = (out["total"] != row["total"] or new_band != row["band"])
        if not differs:
            continue
        changed += 1
        details.append({"id": row["id"], "candidate_id": row["candidate_id"],
                        "old_total": row["total"], "new_total": out["total"],
                        "old_band": row["band"], "new_band": new_band,
                        "old_version": row["config_version"]})
        if apply:
            if "as_evaluated" not in extra:
                # 非破壊の固定が無い旧評価は、既存の来歴 (recalculated) が
                # あればその旧値から、無ければ現在の値 (= 当時の値) から
                # 評価当時の総合点を復元して固定する。これをしないと、
                # 2回目の再計算で当時の値が来歴ごと上書きされて消える
                prev = extra.get("recalculated") or {}
                extra["as_evaluated"] = {
                    "total": prev.get("old_total", row["total"]),
                    "band": prev.get("old_band", row["band"]),
                    "config_version": prev.get("from_version", row["config_version"])}
            extra["recalculated"] = {
                "from_version": row["config_version"], "to_version": cur_version,
                "old_total": row["total"], "old_band": row["band"],
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            store.update_evaluation_recalc(row["id"], out["total"], new_band,
                                           cur_version, cur_hash, extra)
    return {"checked": len(rows), "changed": changed, "anomalies": anomalies,
            "details": details}
