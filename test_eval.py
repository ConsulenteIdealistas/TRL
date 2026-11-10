"""評価・採点系 (trlcore/scoring.py・evaluate.py・prompts.py・market.py) のテスト。

実行: python -m unittest test_eval -v
test_trl.py から分離した。共通フィクスチャは testutil を参照する。
"""
import trl as T
from trl import *  # noqa

import copy
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from testutil import (AS_OF, CFG, EVENT_KEYS, EXTRACTION, LONG_KEYS,
                      assessment, ev, item, make_llm_fn, raw, scoring)


class ScoringTests(unittest.TestCase):
    def test_weights_sum_100(self):
        for k in ("long", "event"):
            self.assertEqual(sum(CFG["scoring"][k]["weights"].values()), 100)

    def test_bad_weights_rejected(self):
        bad = copy.deepcopy(CFG)
        bad["scoring"]["long"]["weights"]["monetization"] = 30
        with self.assertRaises(ConfigError):
            validate_config(bad)

    def test_total_computation(self):
        items = {k: 80 for k in LONG_KEYS}
        r = scoring.compute(items, "long", CFG)
        self.assertEqual((r["status"], r["total"]), ("final", 80.0))
        items = {"technology_readiness": 100, "technological_advantage": 0, "monetization": 0,
                 "market_growth": 0, "financial_sustainability": 0, "moat_durability": 0}
        self.assertEqual(scoring.compute(items, "long", CFG)["total"], 30.0)

    def test_null_means_hold_no_redistribution(self):
        items = {k: 90 for k in LONG_KEYS}
        items["monetization"] = None
        r = scoring.compute(items, "long", CFG)
        self.assertEqual(r["status"], "hold")
        self.assertIsNone(r["total"])
        self.assertEqual(r["critical_missing"], ["monetization"])

    def test_pre_stage_business_not_required(self):
        # TRL 2 で技術軸だけ揃えば、事業軸は段階未達として final (技術軸の総合点)
        items = {k: None for k in LONG_KEYS}
        items["technology_readiness"] = 80
        items["technological_advantage"] = 60
        r = scoring.compute(items, "long", CFG, trl_level=2)
        self.assertEqual(r["status"], "final")
        self.assertEqual(r["total_scope"], "technology")
        self.assertEqual(r["total"], 73.3)  # (80*30 + 60*15) / 45
        self.assertEqual(r["business_stage"], "pre_stage")
        self.assertEqual(r["axes"]["business"]["status"], "pre_stage")
        self.assertEqual(r["critical_missing"], [])  # monetization は不足に数えない

    def test_pre_stage_needs_trl_and_tech(self):
        items = {k: None for k in LONG_KEYS}
        items["technology_readiness"] = 80
        items["technological_advantage"] = 60
        # TRL が高い/未確定なら従来どおり事業項目が必須
        self.assertEqual(scoring.compute(items, "long", CFG, trl_level=5)["status"], "hold")
        self.assertEqual(scoring.compute(items, "long", CFG, trl_level=None)["status"], "hold")
        self.assertEqual(scoring.compute(items, "long", CFG)["status"], "hold")
        # 技術軸が欠けていれば TRL が低くても hold
        items["technology_readiness"] = None
        r = scoring.compute(items, "long", CFG, trl_level=1)
        self.assertEqual(r["status"], "hold")
        self.assertEqual(r["critical_missing"], ["technology_readiness"])

    def test_pre_stage_full_scores_still_full_total(self):
        items = {k: 80 for k in LONG_KEYS}
        r = scoring.compute(items, "long", CFG, trl_level=2)
        self.assertEqual((r["status"], r["total"], r["total_scope"]), ("final", 80.0, "full"))

    def test_event_hold_on_critical_missing(self):
        items = {k: 60 for k in EVENT_KEYS}
        items["not_priced_in"] = None
        r = scoring.compute(items, "event", CFG)
        self.assertEqual((r["status"], r["critical_missing"]), ("hold", ["not_priced_in"]))

    def test_range_validation(self):
        for bad in (-1, 100.5, "80", True, float("nan")):
            items = {k: 50 for k in LONG_KEYS}
            items["market_growth"] = bad
            self.assertEqual(scoring.compute(items, "long", CFG)["status"], "error", bad)
        items = {k: 0 for k in LONG_KEYS}
        self.assertEqual(scoring.compute(items, "long", CFG)["total"], 0.0)
        items = {k: 100 for k in LONG_KEYS}
        self.assertEqual(scoring.compute(items, "long", CFG)["total"], 100.0)

    def test_required_items_validated(self):
        items = {k: 50 for k in LONG_KEYS}
        del items["monetization"]
        self.assertIn("missing_item:monetization", scoring.validate_items(items, "long", CFG))
        items = {k: 50 for k in LONG_KEYS}
        items["bogus"] = 1
        self.assertIn("unknown_item:bogus", scoring.validate_items(items, "long", CFG))

    def test_bands(self):
        exp = [(0, "extremely_weak"), (19, "extremely_weak"), (20, "weak"), (39, "weak"), (40, "moderate_uncertain"),
               (59, "moderate_uncertain"), (59.5, "moderate_uncertain"), (60, "strong"), (79, "strong"),
               (80, "very_strong"), (100, "very_strong")]
        for s, label in exp:
            self.assertEqual(scoring.band_for(s, CFG)["label"], label, s)

    def test_reference_item_not_in_total_or_missing(self):
        # 参考項目 (経営陣・実行力) は総合点・missing に一切影響しない
        items = {k: 80 for k in LONG_KEYS}
        items["management_execution"] = 0
        r = scoring.compute(items, "long", CFG)
        self.assertEqual((r["status"], r["total"]), ("final", 80.0))
        items["management_execution"] = None  # null でも hold しない
        r = scoring.compute(items, "long", CFG)
        self.assertEqual((r["status"], r["total"]), ("final", 80.0))
        self.assertNotIn("management_execution", r["missing"])
        self.assertNotIn("management_execution", r["critical_missing"])

    def test_reference_item_range_validated(self):
        items = {k: 50 for k in LONG_KEYS}
        items["management_execution"] = 101
        self.assertIn("out_of_range:management_execution", scoring.validate_items(items, "long", CFG))
        items["management_execution"] = 50
        self.assertEqual(scoring.validate_items(items, "long", CFG), [])

    def test_reference_overlap_rejected(self):
        bad = copy.deepcopy(CFG)
        bad["scoring"]["long"]["reference"] = ["monetization"]
        with self.assertRaises(ConfigError):
            validate_config(bad)

    def test_long_and_event_not_averaged(self):
        # 2系統の結果は別々に返り、合成関数は存在しない
        self.assertFalse(hasattr(scoring, "combine"))


class DirectionTests(unittest.TestCase):
    """材料の方向性: 点数とは独立に追い風/逆風を判定し、サイトへバッジ表示する。"""

    def test_parse_direction(self):
        self.assertEqual(T.parse_direction({"direction": "negative", "reason": "治験が中止された"}),
                         {"direction": "negative", "reason": "治験が中止された"})
        self.assertEqual(T.parse_direction({"direction": "positive"})["direction"], "positive")
        self.assertIsNone(T.parse_direction({"direction": "bullish"}))  # 未知の値は捨てる
        self.assertIsNone(T.parse_direction({"direction": None}))
        self.assertIsNone(T.parse_direction(None))
        self.assertIsNone(T.parse_direction("positive"))

    def _eval_with_direction(self, direction_obj):
        cand = {"id": "c1", "title": "T", "abstract": "A" * 300, "url": "u", "source": "trials",
                "published": "2026-10-10", "fields": []}
        base = make_llm_fn(long_scores={k: 70 for k in LONG_KEYS})

        def fn(p):
            out = base(p)
            if "長期成長評価" in p:
                obj = json.loads(out)
                obj["direction"] = direction_obj
                return json.dumps(obj)
            return out

        return ev.evaluate_one(cand, "long", CFG, Mock(fn), AS_OF)

    def test_evaluate_one_records_direction(self):
        res = self._eval_with_direction({"direction": "negative", "reason": "第3相治験が中止された"})
        self.assertEqual(res["status"], "final")  # 全項目に点が付いても方向は negative であり得る
        self.assertEqual(res["extra"]["direction"],
                         {"direction": "negative", "reason": "第3相治験が中止された"})

    def test_invalid_direction_does_not_fail_evaluation(self):
        res = self._eval_with_direction({"direction": "strong_buy"})
        self.assertEqual(res["status"], "final")
        self.assertIsNone(res["extra"]["direction"])

    def test_site_data_includes_direction(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "trl.sqlite")
        rec, _ = normalize(raw(sid="2610.8888", title="Terminated trial study"),
                           "2026-10-20T00:00:00+00:00")
        cid, _ = store.upsert_candidate(rec, "r0")
        store.set_status(cid, "screened")
        res = self._eval_with_direction({"direction": "mixed", "reason": "進展と悪材料が混在"})
        store.add_evaluation("r0", cid, res)
        data = T.build_site_data(store, CFG)
        cand = [c for c in data["candidates"] if c["id"] == cid][0]
        self.assertEqual(cand["long"]["direction"], {"direction": "mixed", "reason": "進展と悪材料が混在"})
        store.close()

    def test_site_splits_awaiting_from_missing(self):
        from trlcore.site import _ev_for_site, _split_missing
        self.assertEqual(_split_missing(["結果待ち：第II相の最終結果", "価格の根拠", "結果待ち: 承認審査"]),
                         (["価格の根拠"], ["第II相の最終結果", "承認審査"]))
        ev = {"kind": "long", "status": "hold", "total": None, "band": None,
              "as_of": "2026-10-20", "evaluated_at": "2026-10-20T00:00:00+00:00",
              "items_json": json.dumps({"monetization": {"score": None, "reason": "",
                  "evidence": [], "counter_evidence": [],
                  "missing_info": ["価格設定の根拠", "結果待ち：第II相試験の最終結果"]}}),
              "extra_json": json.dumps({"trl_level": {"level": None, "reason": "",
                  "missing_info": ["結果待ち：主要評価項目の達成状況", "対象技術の特定"]}}),
              "errors_json": "[]"}
        out = _ev_for_site(ev, CFG)
        item = out["items"]["monetization"]
        self.assertEqual(item["missing_info"], ["価格設定の根拠"])
        self.assertEqual(item["awaiting_info"], ["第II相試験の最終結果"])
        self.assertEqual(out["trl_level"]["missing_info"], ["対象技術の特定"])
        self.assertEqual(out["trl_level"]["awaiting_info"], ["主要評価項目の達成状況"])


class PromptTests(unittest.TestCase):
    def test_render(self):
        self.assertEqual(render("a {{x}} b", {"x": "1"}), "a 1 b")
        with self.assertRaises(PromptError):
            render("{{x}} {{y}}", {"x": 1})

    def test_inserted_values_not_reexpanded(self):
        self.assertEqual(render("{{a}}", {"a": "{{b}}", "b": "BAD"}), "{{b}}")

    def test_real_prompts_load_and_have_versions(self):
        for n in ("extract", "long", "event"):
            p = load_prompt(n, CFG["scoring"]["prompts"][n])
            self.assertEqual(len(p["sha"]), 12)

    def test_long_prompt_contains_trl_definitions(self):
        text = load_prompt("long", CFG["scoring"]["prompts"]["long"])["text"]
        for anchor in ("TRL 1", "TRL 9", "実施済みだけを数える", "迷ったら切り下げる",
                       "セクター別の運用", "条件及び期限付き承認", "軌道上での技術実証",
                       "テープアウト", "PoC", "商用展開と TRL の分離",
                       "意図した性能・安定性", "再現性(規格・歩留まり)", "デザインウィン",
                       "公開ベンチマークのスコア公表だけでは", "熱真空試験に合格",
                       "実臨床での使用を通じて", "販売数量や売上は",
                       "推測で補ってはならない", "投資上の成長余地を推論してはならない",
                       "TRL 9 の確認事項"):
            self.assertIn(anchor, text)

    def test_long_prompt_contains_direction_guidance(self):
        text = load_prompt("long", CFG["scoring"]["prompts"]["long"])["text"]
        for anchor in ("材料の方向性", "positive", "negative", "mixed", "点数や TRL とは独立"):
            self.assertIn(anchor, text)

    def test_prompts_contain_score_anchors(self):
        # 6項目の帯域アンカー (何点に何が要るか) が欠けないこと
        text = load_prompt("long", CFG["scoring"]["prompts"]["long"])["text"]
        for anchor in ("[採点アンカー]", "technology_readiness", "ランウェイ", "59 を上限の目安",
                       "根拠の質が低い側の帯域"):
            self.assertIn(anchor, text)
        ev = load_prompt("event", CFG["scoring"]["prompts"]["event"])["text"]
        for anchor in ("[採点アンカー]", "catalyst_quality", "downside_resilience"):
            self.assertIn(anchor, ev)

    def test_long_prompt_missing_info_policy(self):
        # 原理的に資料に無い情報を不足として数えない方針と、結果待ちの分離規約
        text = load_prompt("long", CFG["scoring"]["prompts"]["long"])["text"]
        for anchor in ("[不足の扱い]", "原理的に含まれない種類の情報", "代理指標",
                       "結果待ち：", "将来予測が無いことだけを理由に null にしない"):
            self.assertIn(anchor, text)

    def test_long_prompt_contains_reference_item(self):
        # 規程第13条: 経営陣・実行力の参考項目 (総合点・必須判定に含めない)
        text = load_prompt("long", CFG["scoring"]["prompts"]["long"])["text"]
        for anchor in ("management_execution", "経営陣・実行力", "参考項目",
                       "総合点・軸別点・必須判定・不足判定には含めません"):
            self.assertIn(anchor, text)

    def test_long_prompt_observational_study_rule(self):
        # 規程第11条 (C-4 で B類として明文化) の観察研究の位置づけが同期していること
        text = load_prompt("long", CFG["scoring"]["prompts"]["long"])["text"]
        for anchor in ("市販後観察研究・レジストリは、その対象製品の TRL 9 の証拠になり得る",
                       "観察研究という研究形式自体が段階を下げることはない"):
            self.assertIn(anchor, text)

    def test_extract_prompt_contains_subject(self):
        # 監査要項④ (技術帰属): facts に subject を原文スパンのまま併記する指示
        text = load_prompt("extract", CFG["scoring"]["prompts"]["extract"])["text"]
        for anchor in ("subject", "原文に現れる形のまま", "帰属"):
            self.assertIn(anchor, text)

    def test_long_prompt_contains_low_band_procedure(self):
        # TRL 1〜3 が null に逃げないための判定手順・原則・具体例が欠けないこと
        text = load_prompt("long", CFG["scoring"]["prompts"]["long"])["text"]
        for anchor in ("[判定手順]", "原則として 1〜3 のいずれかに必ず位置づけられる",
                       "null は最後の手段", "1〜3 を否定する理由にはならない",
                       "[具体例]", "誤り率の閾値", "合成経路を提案"):
            self.assertIn(anchor, text)


class ValidationTests(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(extract_json('```json\n{"a":1}\n```'), {"a": 1})
        for bad in ("", "no json", "{broken", "[1,2]"):
            with self.assertRaises(LLMError):
                extract_json(bad)

    def test_extract_json_trailing_prose(self):
        # JSON の後ろに括弧を含む説明文が続いても先頭オブジェクトを読める
        self.assertEqual(extract_json('{"a": 1} 補足{注: あとで確認}'), {"a": 1})

    def test_valid_assessment(self):
        s, d, e = ev.validate_assessment(assessment(LONG_KEYS), "long", CFG)
        self.assertEqual(e, [])

    def test_bad_score_rejected(self):
        # 文字列・真偽値・NaN のスコアは検証エラーにする(表示側の注入経路を断つ)
        for bad in ("55", True, float("nan")):
            a = assessment(LONG_KEYS)
            a["items"]["market_growth"]["score"] = bad
            self.assertIn("bad_score:market_growth", ev.validate_assessment(a, "long", CFG)[2])

    def test_invalid_assessments(self):
        a = assessment(LONG_KEYS)
        del a["items"]["monetization"]
        self.assertIn("missing_item:monetization", ev.validate_assessment(a, "long", CFG)[2])
        a = assessment(LONG_KEYS)
        a["items"]["market_growth"]["evidence"] = []
        self.assertIn("evidence_required:market_growth", ev.validate_assessment(a, "long", CFG)[2])
        a = assessment(LONG_KEYS)
        a["items"]["market_growth"]["reason"] = ""
        self.assertIn("reason_required:market_growth", ev.validate_assessment(a, "long", CFG)[2])
        a = assessment(LONG_KEYS, {"market_growth": None})
        a["items"]["market_growth"]["missing_info"] = []
        self.assertIn("missing_info_required_for_null:market_growth", ev.validate_assessment(a, "long", CFG)[2])
        a = assessment(LONG_KEYS)
        a["items"]["market_growth"]["evidence"][0]["kind"] = "rumor"
        self.assertTrue(any(x.startswith("bad_evidence") for x in ev.validate_assessment(a, "long", CFG)[2]))
        self.assertEqual(ev.validate_assessment({"items": []}, "long", CFG)[2], ["items:must_be_object"])

    def test_extraction_validation(self):
        self.assertTrue(ev.validate_extraction({}))
        self.assertEqual(ev.validate_extraction({"facts": [{"statement": "x"}], "inferences": [], "forecasts": [],
                                                 "hypotheses": [], "counter_points": [], "missing_info": []}), [])

    def test_extraction_subject_validation(self):
        # 技術帰属 (監査要項④): subject は文字列なら受け付け、欠落も許容する
        base = {"facts": [{"statement": "x", "subject": "製品A"}], "inferences": [], "forecasts": [],
                "hypotheses": [], "counter_points": [], "missing_info": []}
        self.assertEqual(ev.validate_extraction(base), [])
        base["facts"][0]["subject"] = 123
        self.assertTrue(any("subject" in e for e in ev.validate_extraction(base)))

    def test_reference_item_assessment(self):
        a = assessment(LONG_KEYS)
        a["items"]["management_execution"] = item(70)
        s, d, e = ev.validate_assessment(a, "long", CFG)
        self.assertEqual(e, [])
        self.assertEqual(s["management_execution"], 70)
        # 参考項目が欠落していてもエラーにはせず null として記録する
        s, d, e = ev.validate_assessment(assessment(LONG_KEYS), "long", CFG)
        self.assertEqual(e, [])
        self.assertIsNone(s["management_execution"])
        self.assertIsNone(d["management_execution"]["score"])
        # 得点があるのに根拠が無い場合は採点項目と同じ規律でエラー
        a = assessment(LONG_KEYS)
        a["items"]["management_execution"] = item(70, with_evidence=False)
        self.assertIn("evidence_required:management_execution", ev.validate_assessment(a, "long", CFG)[2])


class EvaluateOneTests(unittest.TestCase):
    cand = {"id": "c1", "title": "T", "abstract": "A" * 300, "url": "u", "source": "arxiv",
            "published": "2026-10-10", "fields": []}

    def run_it(self, kind="long", **kw):
        return ev.evaluate_one(self.cand, kind, CFG, Mock(make_llm_fn(**kw)), AS_OF)

    def test_long_final(self):
        r = self.run_it(long_scores={k: 60 for k in LONG_KEYS})
        self.assertEqual((r["status"], r["total"], r["band"]["label"]), ("final", 60.0, "strong"))
        self.assertEqual(set(r["prompt_versions"]), {"extract", "long"})

    def _fn_with_reference(self, ref_value):
        def fn(prompt):
            if "長期成長評価" in prompt:
                obj = assessment(LONG_KEYS, {k: 60 for k in LONG_KEYS})
                if ref_value is not ...:
                    obj["items"]["management_execution"] = item(ref_value)
                return json.dumps(obj)
            return json.dumps(EXTRACTION)
        return fn

    def test_long_reference_item_scored_not_in_total(self):
        # 参考項目に高得点が付いても総合点は6項目だけで決まる
        r = ev.evaluate_one(self.cand, "long", CFG, Mock(self._fn_with_reference(90)), AS_OF)
        self.assertEqual((r["status"], r["total"]), ("final", 60.0))
        self.assertEqual(r["items"]["management_execution"]["score"], 90)

    def test_long_reference_item_null_or_absent_still_final(self):
        # 参考項目が null でも、出力自体が無くても、評価は確定する
        r = ev.evaluate_one(self.cand, "long", CFG, Mock(self._fn_with_reference(None)), AS_OF)
        self.assertEqual((r["status"], r["total"]), ("final", 60.0))
        self.assertIsNone(r["items"]["management_execution"]["score"])
        r = ev.evaluate_one(self.cand, "long", CFG, Mock(self._fn_with_reference(...)), AS_OF)
        self.assertEqual((r["status"], r["total"]), ("final", 60.0))
        self.assertIsNone(r["items"]["management_execution"]["score"])

    def _flaky_fn(self, calls, always_bad=False):
        def fn(prompt):
            if "根拠抽出」担当" in prompt:
                return json.dumps(EXTRACTION)
            calls["assess"] += 1
            a = assessment(LONG_KEYS, {k: 60 for k in LONG_KEYS})
            if always_bad or calls["assess"] == 1:
                del a["items"]["moat_durability"]
            return json.dumps(a)
        return fn

    def test_assessment_regenerated_once_on_missing_item(self):
        # 項目欠落などの検証エラーは、不足点を伝えて1回だけ再生成し、復旧すれば通常の評価にする
        calls = {"assess": 0}
        r = ev.evaluate_one(self.cand, "long", CFG, Mock(self._flaky_fn(calls)), AS_OF)
        self.assertEqual(r["status"], "final")
        self.assertEqual(calls["assess"], 2)
        self.assertEqual(r["extra"]["assess_attempts"], 2)

    def test_assessment_regeneration_gives_up_after_two(self):
        calls = {"assess": 0}
        r = ev.evaluate_one(self.cand, "long", CFG, Mock(self._flaky_fn(calls, always_bad=True)), AS_OF)
        self.assertEqual(r["status"], "error")
        self.assertIn("missing_item:moat_durability", r["errors"])
        self.assertEqual(calls["assess"], 2)

    def test_long_hold_on_null(self):
        r = self.run_it(long_scores={"monetization": None})
        self.assertEqual((r["status"], r["total"], r["critical_missing"]), ("hold", None, ["monetization"]))

    def test_invalid_ai_response_is_error(self):
        r = self.run_it(bad="not json at all")
        self.assertEqual(r["status"], "error")
        r = self.run_it(long_scores={"monetization": 150})
        self.assertEqual(r["status"], "error")
        self.assertIn("out_of_range:monetization", r["errors"])

    def test_llm_exception_is_contained(self):
        def boom(p):
            raise LLMError("api down")
        r = ev.evaluate_one(self.cand, "long", CFG, Mock(boom), AS_OF)
        self.assertEqual(r["status"], "error")

    def test_event_without_market_expectation_holds(self):
        r = self.run_it("event", event_scores={k: 90 for k in EVENT_KEYS},
                        event={"classification": 4, "market_expectation": ""})
        self.assertEqual(r["status"], "hold")
        self.assertIsNone(r["event"]["classification"])
        self.assertEqual(set(r["critical_missing"]), {"expectation_gap", "not_priced_in"})

    def test_event_with_market_expectation_final(self):
        r = self.run_it("event", event_scores={k: 70 for k in EVENT_KEYS},
                        event={"classification": 4, "market_expectation": "コンセンサスは赤字継続"})
        self.assertEqual((r["status"], r["total"], r["event"]["classification"]), ("final", 70.0, 4))

    def test_event_with_market_reaction_final_without_expectation(self):
        # 案A (2026-11-06): 市場予想が無くても、株価反応データがあれば
        # 乖離・織り込み度の採点を強制 null にせず生かす
        metrics = {"available": True, "anchor_date": "2026-10-19", "trading_date": "2026-10-19",
                   "reaction_pct": 8.0, "z": 2.5, "pre_drift_pct": 0.5,
                   "volume_ratio": 1.8, "sigma_pct": 2.2, "sigma_days": 60}
        r = ev.evaluate_one(self.cand, "event", CFG,
                            Mock(make_llm_fn(event_scores={k: 70 for k in EVENT_KEYS},
                                             event={"classification": 4, "market_expectation": ""})),
                            AS_OF, market_reaction=(metrics, "Argenx", "ARGX"))
        self.assertEqual((r["status"], r["total"], r["event"]["classification"]), ("final", 70.0, 4))
        self.assertEqual(r["extra"]["market_reaction"], metrics)

    def test_event_with_unavailable_reaction_still_holds(self):
        # 反応データが「利用不可」の場合は従来どおり保留 (根拠なしの採点を生かさない)
        metrics = {"available": False, "reason": "株価系列を取得できなかった", "anchor_date": "2026-10-19"}
        r = ev.evaluate_one(self.cand, "event", CFG,
                            Mock(make_llm_fn(event_scores={k: 70 for k in EVENT_KEYS},
                                             event={"classification": 4, "market_expectation": ""})),
                            AS_OF, market_reaction=(metrics, "Argenx", "ARGX"))
        self.assertEqual(r["status"], "hold")
        self.assertIsNone(r["event"]["classification"])

    def test_provider_requires_key(self):
        import os
        os.environ.pop("GEMINI_API_KEY", None)
        with self.assertRaises(LLMError):
            Gemini()

    def test_gemini_default_model_and_override(self):
        import os
        os.environ["GEMINI_API_KEY"] = "k" * 10
        saved = os.environ.pop("TRL_LLM_MODEL", None)
        try:
            self.assertEqual(Gemini().model, "gemini-3.8-flash")
            os.environ["TRL_LLM_MODEL"] = "gemini-x"
            self.assertEqual(Gemini().model, "gemini-x")
            self.assertEqual(Gemini("explicit").model, "explicit")
        finally:
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("TRL_LLM_MODEL", None)
            if saved:
                os.environ["TRL_LLM_MODEL"] = saved

    def test_gemini_response_parsing(self):
        import os
        os.environ["GEMINI_API_KEY"] = "k" * 10
        try:
            g = Gemini(http=lambda *a, **k: json.dumps({"candidates": [{"content": {"parts": [{"text": "yo"}]}}]}).encode())
            self.assertEqual(g.complete("p"), "yo")
            bad = Gemini(http=lambda *a, **k: b"{}")
            with self.assertRaises(LLMError):
                bad.complete("p")
        finally:
            del os.environ["GEMINI_API_KEY"]


class ScreenTests(unittest.TestCase):
    def c(self, **kw):
        d = {"published": "2026-10-10", "abstract": "A" * 300}
        d.update(kw)
        return d

    def test_screen(self):
        self.assertIsNone(screen(self.c(), AS_OF, CFG))
        self.assertEqual(screen(self.c(published=None), AS_OF, CFG), "no_published_date")
        self.assertEqual(screen(self.c(published="2026-10-25"), AS_OF, CFG), "published_after_as_of")
        self.assertEqual(screen(self.c(published="2026-10"), date(2026, 10, 15), CFG), "published_after_as_of")  # 月末まで未確定
        self.assertEqual(screen(self.c(published="2024-01-01"), AS_OF, CFG), "stale")
        self.assertEqual(screen(self.c(abstract="short"), AS_OF, CFG), "abstract_too_short")
        # ニュース/開示は info_type 別の低い閾値で通る (論文は従来どおり)
        self.assertIsNone(screen(self.c(abstract="x" * 40, info_type="news"), AS_OF, CFG))
        self.assertIsNone(screen(self.c(abstract="x" * 40, info_type="disclosure"), AS_OF, CFG))
        self.assertEqual(screen(self.c(abstract="x" * 40, info_type="paper"), AS_OF, CFG), "abstract_too_short")


class MarketReactionTests(unittest.TestCase):
    def _bars(self, n=130, jump_idx=100, jump=0.08, glitch_idx=None):
        from datetime import date, timedelta
        days, d = [], date(2026, 3, 2)
        while len(days) < n:
            if d.weekday() < 5:
                days.append(d)
            d += timedelta(days=1)
        closes, price = [], 100.0
        for i in range(n):
            price *= 1.004 if i % 2 == 0 else 0.997  # 一定だと MAD が 0 になるため揺らす
            if i == jump_idx + 1:
                price *= 1 + jump
            closes.append(price)
        if glitch_idx is not None:
            closes[glitch_idx] *= 1.45
            closes[glitch_idx + 1] /= 1.45
        return [(d.isoformat(), c, 2000.0 if i in (jump_idx, jump_idx + 1) else 1000.0)
                for i, (d, c) in enumerate(zip(days, closes))], days

    def test_reaction_metrics_basic(self):
        from trlcore import market as MK
        bars, days = self._bars()
        m = MK.reaction_metrics(bars, days[100].isoformat())
        self.assertTrue(m["available"])
        self.assertAlmostEqual(m["reaction_pct"], 8.2, delta=0.4)
        self.assertGreater(m["z"], 3)
        self.assertAlmostEqual(m["volume_ratio"], 2.0, delta=0.05)
        self.assertEqual(m["trading_date"], days[100].isoformat())

    def test_weekend_anchor_resolves_to_next_trading_day(self):
        from trlcore import market as MK
        from datetime import timedelta
        bars, days = self._bars()
        saturday = (days[100] - timedelta(days=2)).isoformat()  # 月曜の2日前 = 土曜
        m = MK.reaction_metrics(bars, saturday)
        self.assertTrue(m["available"])
        self.assertEqual(m["trading_date"], days[100].isoformat())

    def test_robust_sigma_survives_glitch(self):
        from trlcore import market as MK
        bars, days = self._bars(glitch_idx=50)
        m = MK.reaction_metrics(bars, days[100].isoformat())
        self.assertTrue(m["available"])
        self.assertLess(m["sigma_pct"], 1.0)   # +45% の外れ値に σ が壊されない
        self.assertGreater(m["z"], 3)

    def test_insufficient_history(self):
        from trlcore import market as MK
        bars, days = self._bars(n=10)
        m = MK.reaction_metrics(bars, days[5].isoformat())
        self.assertFalse(m["available"])

    def test_fetch_parsing_with_gmtoffset(self):
        from trlcore import market as MK
        from datetime import datetime, timezone
        ts = int(datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc).timestamp())  # JST では 10/20 00:00
        doc = {"chart": {"result": [{"meta": {"gmtoffset": 32400}, "timestamp": [ts],
                                      "indicators": {"quote": [{"close": [123.4], "volume": [555]}]}}]}}
        bars = MK.fetch_price_series("7203.T", http=lambda url, params=None, headers=None: json.dumps(doc))
        self.assertEqual(bars, [("2026-10-20", 123.4, 555.0)])

    def test_build_market_reaction_end_to_end(self):
        from trlcore import market as MK
        from datetime import date, datetime, timezone
        bars, days = self._bars()
        doc = {"chart": {"result": [{"meta": {"gmtoffset": 0},
                                      "timestamp": [int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp())
                                                    for d, _, _ in bars],
                                      "indicators": {"quote": [{"close": [c for _, c, _ in bars],
                                                                "volume": [v for _, _, v in bars]}]}}]}}
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")
        cfg = dict(CFG)
        cfg["entities"] = {"entities": [{"id": "acme", "name": "Acme", "ticker": "ACME",
                                         "market": "US", "tier": "core", "sector": "bio"}]}
        rec, _ = T.normalize(raw(source="prtimes", sid="M1", title="Acme の発表",
                                 published=days[100].isoformat()), T.now_iso())
        cid, _ = store.upsert_candidate(rec, "rX")
        store.set_entities(cid, [{"entity_id": "acme", "strength": "strong"}])
        cand = store.get_candidate(cid)
        metrics, name, symbol = MK.build_market_reaction(
            store, cfg, cand, AS_OF, http=lambda url, params=None, headers=None: json.dumps(doc),
            log=lambda *_: None)
        self.assertEqual((name, symbol), ("Acme", "ACME"))
        self.assertTrue(metrics["available"])
        text = MK.format_reaction(metrics, name, symbol, cand["source"])
        self.assertIn("標準化反応 z", text)
        self.assertIn("アンカーの強さ: 強", text)
        # 2回目はキャッシュされた系列で計算できる (系列が新しければ http を呼ばない)
        metrics2, _, _ = MK.build_market_reaction(
            store, cfg, cand, date(2026, 8, 30),
            http=lambda *a, **k: (_ for _ in ()).throw(AssertionError("再取得した")), log=lambda *_: None)
        self.assertTrue(metrics2["available"])
        store.close()
        tmp.cleanup()


class FreezeV3Tests(unittest.TestCase):
    """凍結バッチ (A1〜A10): v3 世代の構造化と旧世代との混在互換。"""

    cand = {"id": "c1", "title": "T", "abstract": "A" * 300, "url": "u", "source": "arxiv",
            "published": "2026-10-10", "fields": []}

    def test_prompts_are_v3(self):
        self.assertEqual(CFG["config_version"], "2026.11.2")
        self.assertEqual(CFG["scoring"]["prompts"],
                         {"extract": "v3", "long": "v3", "event": "v3"})

    def test_extract_v3_prompt_anchors(self):
        text = load_prompt("extract", "v3")["text"]
        for anchor in ("quote", "primary_subjects", "quantities", "identifiers",
                       "facets", "grounded", "取りこぼし防止", "経営陣の就任・退任"):
            self.assertIn(anchor, text)

    def test_long_v3_prompt_anchors(self):
        text = load_prompt("long", "v3")["text"]
        for anchor in ("trl_levels", "basis_claim_ids", "constraint_codes",
                       "deduction_codes", "missing_codes", "防衛・軍事技術",
                       "硬い上限ではない", "TAM", "原資は現預金に短期投資"):
            self.assertIn(anchor, text)

    def test_normalize_extraction_envelope(self):
        obj = {"facts": [{"statement": "第2相で主要評価項目を達成した",
                          "quote": "主要評価項目を達成", "source": "u",
                          "subject": "候補薬X", "subject_type": "substance",
                          "subject_role": "primary", "polarity": "positive",
                          "status": "completed", "event_date": "2026-09-01",
                          "quantities": [{"metric": "症例数", "value": "120", "unit": "例"}],
                          "identifiers": [{"type": "NCT", "value": "NCT01234567"}],
                          "facets": {"study": {"phase": "Phase 2"}}}],
               "inferences": [{"statement": "推論1", "subject_type": "bogus",
                               "status": "bogus"}],
               "forecasts": [], "hypotheses": [],
               "counter_points": [{"statement": "安全性の懸念", "grounded": True}],
               "missing_info": ["長期データ"],
               "primary_subjects": [" 候補薬X ", ""]}
        out = T.normalize_extraction(obj)
        f = out["facts"][0]
        self.assertEqual((f["id"], f["kind"]), ("C1", "fact"))
        self.assertEqual(f["quote"], "主要評価項目を達成")
        self.assertEqual(f["subject_type"], "substance")
        self.assertEqual(f["status"], "completed")
        self.assertEqual(f["quantities"][0]["value"], "120")
        self.assertEqual(f["identifiers"][0]["value"], "NCT01234567")
        self.assertEqual(f["facets"]["study"]["phase"], "Phase 2")
        inf = out["inferences"][0]
        self.assertEqual((inf["id"], inf["kind"]), ("C2", "inference"))
        self.assertNotIn("subject_type", inf)  # 語彙外は落とすだけ
        self.assertNotIn("status", inf)
        cp = out["counter_points"][0]
        self.assertEqual((cp["id"], cp["kind"], cp["grounded"]), ("C3", "counter", True))
        self.assertEqual(out["missing_info"], ["長期データ"])  # 文字列リストのまま
        self.assertEqual(out["primary_subjects"], ["候補薬X"])

    def test_normalize_extraction_legacy_shape(self):
        # 旧世代 (v2 以前) の抽出: subject のみの fact と文字列の counter_points
        legacy = {"facts": [{"statement": "f", "source": "u", "subject": "製品A"}],
                  "inferences": [], "forecasts": [], "hypotheses": [],
                  "counter_points": ["文字列の反証"], "missing_info": []}
        self.assertEqual(T.validate_extraction(legacy), [])
        out = T.normalize_extraction(legacy)
        self.assertEqual((out["facts"][0]["id"], out["facts"][0]["kind"]), ("C1", "fact"))
        self.assertEqual(out["facts"][0]["subject"], "製品A")
        self.assertEqual(out["counter_points"][0]["statement"], "文字列の反証")
        self.assertEqual(out["counter_points"][0]["kind"], "counter")
        self.assertEqual(out["primary_subjects"], [])

    def test_validate_extraction_counter_dict_needs_statement(self):
        obj = dict(EXTRACTION, counter_points=[{"quote": "引用のみ"}])
        self.assertTrue(any("counter_points" in e for e in T.validate_extraction(obj)))
        obj2 = dict(EXTRACTION, counter_points=["文字列は可", {"statement": "構造化も可"}])
        self.assertEqual(T.validate_extraction(obj2), [])

    def test_parse_trl_levels_lenient(self):
        v = [{"subject": "主技術", "level": 6, "environment": "実環境",
              "basis_claim_ids": ["C1"]},
             {"subject": "", "level": 3},            # subject 無しは捨てる
             {"subject": "副技術", "level": 99},     # 範囲外は捨てる
             "not-a-dict",
             {"subject": "第三技術", "level": None}]  # level null は残す
        out = T.parse_trl_levels(v)
        self.assertEqual([e["subject"] for e in out], ["主技術", "第三技術"])
        self.assertEqual(out[0]["basis_claim_ids"], ["C1"])
        self.assertIsNone(out[1]["level"])
        self.assertEqual(T.parse_trl_levels(None), [])
        self.assertEqual(T.parse_trl_levels({"subject": "x"}), [])

    def test_normalize_event_vocab(self):
        evv = T.normalize_event({"classification": 2, "status": "occurred",
                                 "date_window": {"start": "2026-11-01"},
                                 "expectation": {"value": "承認"}, "actual": "文字列"})
        self.assertEqual(evv["status"], "occurred")
        self.assertEqual(evv["date_window"], {"start": "2026-11-01"})
        self.assertNotIn("actual", evv)  # dict でない構造化値は落とす
        self.assertIsNone(T.normalize_event({"status": "bogus"})["status"])
        legacy = {"classification": 1, "market_expectation": "x"}
        self.assertEqual(T.normalize_event(dict(legacy)), legacy)  # 旧形式は素通し

    def test_item_trace_fields_roundtrip(self):
        obj = assessment(LONG_KEYS, {k: 60 for k in LONG_KEYS})
        it = obj["items"]["technology_readiness"]
        it["basis_claim_ids"] = ["C1", "C2"]
        it["constraint_codes"] = ["single_source", "unknown_code_xyz"]  # 未知コードも保存
        it["deduction_codes"] = ["no_repetition"]
        it["missing_codes"] = []
        scores, details, errors = T.validate_assessment(obj, "long", CFG)
        self.assertEqual(errors, [])
        d = details["technology_readiness"]
        self.assertEqual(d["basis_claim_ids"], ["C1", "C2"])
        self.assertEqual(d["constraint_codes"], ["single_source", "unknown_code_xyz"])
        self.assertEqual(d["deduction_codes"], ["no_repetition"])
        self.assertEqual(d["missing_codes"], [])  # 空リストも list として取り込む

    def test_item_trace_field_must_be_list(self):
        obj = assessment(LONG_KEYS, {k: 60 for k in LONG_KEYS})
        obj["items"]["monetization"]["constraint_codes"] = "single_source"
        _, _, errors = T.validate_assessment(obj, "long", CFG)
        self.assertIn("constraint_codes:must_be_list:monetization", errors)

    def test_evaluate_one_long_keeps_levels_and_codes(self):
        def fn(prompt):
            if "長期成長評価" not in prompt:
                return json.dumps(EXTRACTION)
            obj = assessment(LONG_KEYS, {k: 60 for k in LONG_KEYS})
            obj["items"]["technology_readiness"]["basis_claim_ids"] = ["C1"]
            obj["items"]["technology_readiness"]["constraint_codes"] = ["single_source"]
            obj["trl_level"] = {"level": 6, "reason": "実証済み",
                                "evidence": [{"ref": "https://example.org/x",
                                              "summary": "要約", "kind": "fact"}],
                                "basis_claim_ids": ["C1"]}
            obj["trl_levels"] = [{"subject": "主技術", "level": 6, "basis_claim_ids": ["C1"]},
                                 {"subject": "副技術", "level": 99}]
            return json.dumps(obj)
        r = ev.evaluate_one(self.cand, "long", CFG, Mock(fn), AS_OF)
        self.assertEqual(r["status"], "final")
        self.assertEqual(r["extra"]["trl_levels"],
                         [{"subject": "主技術", "level": 6, "basis_claim_ids": ["C1"]}])
        self.assertEqual(r["extra"]["trl_level"]["basis_claim_ids"], ["C1"])
        self.assertEqual(r["items"]["technology_readiness"]["constraint_codes"],
                         ["single_source"])
        self.assertEqual(r["prompt_versions"]["long"]["version"], "v3")
        self.assertEqual(r["prompt_versions"]["extract"]["version"], "v3")

    def test_evaluate_one_event_structured(self):
        r = ev.evaluate_one(self.cand, "event", CFG,
                            Mock(make_llm_fn(event_scores={k: 60 for k in EVENT_KEYS},
                                             event={"classification": 2,
                                                    "market_expectation": "市場は未織り込みと見る",
                                                    "status": "occurred",
                                                    "date_window": {"start": "2026-11-01",
                                                                    "end": "2027-04-30"},
                                                    "expectation": {"metric": "承認",
                                                                    "value": "承認取得"},
                                                    "actual": "文字列"})),
                            AS_OF)
        self.assertEqual(r["event"]["status"], "occurred")
        self.assertEqual(r["event"]["date_window"]["end"], "2027-04-30")
        self.assertEqual(r["event"]["expectation"]["value"], "承認取得")
        self.assertNotIn("actual", r["event"])
        self.assertEqual(r["prompt_versions"]["event"]["version"], "v3")


if __name__ == "__main__":
    unittest.main()
