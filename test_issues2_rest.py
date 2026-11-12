"""ISSUES-2 残件 (G-5 / N-1 / N-2 / O-2) のテスト。

実行: python -m unittest test_issues2_rest -v
S-4 (ESEF) のテストは test_esef.py 側にある。共通フィクスチャは testutil。
"""
import trl as T
from trl import *  # noqa

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from testutil import (ABSTRACT, AS_OF, CFG, EXTRACTION, LONG_KEYS, assessment,
                      cfg_with, fetch_n, item, make_llm_fn, raw)
from trlcore import evaluate as ev_ns

REF_KEY = "management_execution"
CUR_PROMPTS = {"extract": {"version": CFG["scoring"]["prompts"]["extract"]},
               "long": {"version": CFG["scoring"]["prompts"]["long"]}}


class StoreFixtureBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "trl.sqlite")
        self._seq = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _candidate(self, published="2026-10-10", status=None, eval_at=None,
                   updated=None, **raw_kw):
        self._seq += 1
        rec, problems = normalize(
            raw(sid=f"2610.{self._seq:05d}", published=published, **raw_kw),
            "2026-10-20T00:00:00+00:00")
        assert problems == []
        cid, _ = self.store.upsert_candidate(rec, "r0")
        self.store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
        if status:
            self.store.set_status(cid, status, eval_at=eval_at)
        if updated:
            self.store.conn.execute(
                "UPDATE candidates SET material_updated_at=? WHERE id=?",
                (updated, cid))
            self.store.conn.commit()
        return cid

    def _long_eval(self, cid, config=None, prompts=None, as_evaluated=True,
                   status="final"):
        ev = {"kind": "long", "as_of": "2026-09-20", "status": status,
              "total": 70.0, "band": {"label": "strong"},
              "items": {k: item(70) for k in LONG_KEYS},
              "extraction": EXTRACTION, "event": None,
              "prompt_versions": prompts if prompts is not None else CUR_PROMPTS,
              "config_version": config or CFG["config_version"],
              "config_hash": CFG["config_hash"], "llm_provider": "gemini",
              "llm_model": "m", "input_hash": f"h-{cid}-{self._seq}-{status}",
              "errors": [],
              "extra": ({"as_evaluated": {
                  "total": 70.0, "band": "strong",
                  "config_version": config or CFG["config_version"]}}
                  if as_evaluated else {})}
        return self.store.add_evaluation("r0", cid, ev)


class GenerationStampTests(StoreFixtureBase):
    """G-5 の土台: 候補ごとの最新 long 評価の世代印。"""

    def test_latest_long_generation_uses_as_evaluated(self):
        cid = self._candidate()
        self._long_eval(cid)  # 現行世代
        self._long_eval(cid, config="2026.11.1",
                        prompts={"extract": {"version": "v2"},
                                 "long": {"version": "v2"}})  # 新しい方が旧世代
        got = self.store.latest_long_eval_generations()
        self.assertEqual(got[cid]["config"], "2026.11.1")
        self.assertEqual(got[cid]["prompts"], {"extract": "v2", "long": "v2"})

    def test_ignores_event_error_and_missing(self):
        c_event = self._candidate()
        ev = {"kind": "event", "as_of": "2026-09-20", "status": "final",
              "total": 1.0, "band": {}, "items": {}, "extraction": {},
              "event": None, "prompt_versions": {}, "config_version": "x",
              "config_hash": "h", "llm_provider": "g", "llm_model": "m",
              "input_hash": f"h-ev-{c_event}", "errors": [], "extra": {}}
        self.store.add_evaluation("r0", c_event, ev)
        c_err = self._candidate()
        self._long_eval(c_err, status="error")
        c_none = self._candidate()
        got = self.store.latest_long_eval_generations()
        self.assertNotIn(c_event, got)
        self.assertNotIn(c_err, got)
        self.assertNotIn(c_none, got)

    def test_falls_back_to_row_config_without_as_evaluated(self):
        cid = self._candidate()
        self._long_eval(cid, config="2026.10.0", as_evaluated=False)
        got = self.store.latest_long_eval_generations()
        self.assertEqual(got[cid]["config"], "2026.10.0")


class GenerationSelectionTests(StoreFixtureBase):
    """G-5: 旧世代の再評価適格候補を上限の半分まで前面に出す。"""

    def _eligible(self, published, gen):
        cid = self._candidate(published=published, status="evaluated",
                              eval_at="2026-09-20T00:00:00+00:00",
                              updated="2026-10-19T00:00:00+00:00")
        if gen == "old":
            self._long_eval(cid, config="2026.11.1",
                            prompts={"extract": {"version": "v2"},
                                     "long": {"version": "v2"}})
        else:
            self._long_eval(cid)
        return cid

    def test_oldgen_fronted_within_half_cap(self):
        old15 = self._eligible("2026-10-15", "old")
        old14 = self._eligible("2026-10-14", "old")
        old13 = self._eligible("2026-10-13", "old")
        new16 = self._eligible("2026-10-16", "new")
        new12 = self._eligible("2026-10-12", "new")
        stats = {}
        sel = select_for_eval(self.store, CFG, AS_OF, 4, stats=stats)
        ids = [c["id"] for c in sel]
        # 前面は旧世代から上限の半分 (2件) まで。残りは通常順 (新しい順)
        self.assertEqual(ids, [old15, old14, new16, new12])
        self.assertNotIn(old13, ids)
        self.assertEqual(stats["oldgen_long_remaining"], 3)
        self.assertEqual(stats["oldgen_reeval_eligible"], 3)
        self.assertEqual(stats["oldgen_selected"], 2)

    def test_no_oldgen_keeps_published_order(self):
        a = self._eligible("2026-10-15", "new")
        b = self._eligible("2026-10-16", "new")
        stats = {}
        sel = select_for_eval(self.store, CFG, AS_OF, 4, stats=stats)
        self.assertEqual([c["id"] for c in sel], [b, a])
        self.assertEqual(stats["oldgen_long_remaining"], 0)

    def _stale(self, published, gen):
        # 材料更新が無い再評価候補 (TH-1 ゲートに通常は止まる形)
        cid = self._candidate(published=published, status="evaluated",
                              eval_at="2026-09-20T00:00:00+00:00")
        if gen == "old":
            self._long_eval(cid, config="2026.11.1",
                            prompts={"extract": {"version": "v2"},
                                     "long": {"version": "v2"}})
        else:
            self._long_eval(cid)
        return cid

    def test_oldgen_exempt_from_material_gate(self):
        # 世代の移行: 旧世代は材料更新が無くても適格になる。現行世代は
        # 従来どおりゲートで見送られ、見送り件数にも数えられる
        old = self._stale("2026-10-15", "old")
        new = self._stale("2026-10-16", "new")
        stats = {}
        sel = select_for_eval(self.store, CFG, AS_OF, 10, stats=stats)
        ids = [c["id"] for c in sel]
        self.assertIn(old, ids)
        self.assertNotIn(new, ids)
        self.assertEqual(stats["oldgen_long_remaining"], 1)
        self.assertEqual(stats["oldgen_reeval_eligible"], 1)
        self.assertEqual(stats["oldgen_selected"], 1)
        self.assertEqual(stats.get("reeval_skipped_no_material_update"), 1)

    def test_oldgen_fills_slots_beyond_front_half(self):
        # 前半枠 (上限の半分) を超えた旧世代も、他の適格候補が尽きれば
        # 残り枠で選ばれる (移行ペースは週予算の範囲で最大化される)
        olds = [self._stale(f"2026-10-{d:02d}", "old") for d in (15, 14, 13, 12)]
        fresh = self._candidate(published="2026-10-16", status="screened")
        stats = {}
        sel = select_for_eval(self.store, CFG, AS_OF, 5, stats=stats)
        self.assertEqual({c["id"] for c in sel}, set(olds) | {fresh})
        self.assertEqual(stats["oldgen_reeval_eligible"], 4)
        self.assertEqual(stats["oldgen_selected"], 4)

    def test_oldgen_reservation_gate_unchanged(self):
        # 予約 (TA-2) の起点ゲートは世代緩和の対象外: 処分直後の旧世代
        # 候補も、予約の間隔が経つまでは通常選定に戻らない
        cid = self._candidate(published="2026-10-15", status="hold",
                              eval_at="2026-09-01T00:00:00+00:00")
        self._long_eval(cid, config="2026.11.1",
                        prompts={"extract": {"version": "v2"},
                                 "long": {"version": "v2"}}, status="hold")
        self.store.enqueue_reeval("uuid-genmix-1", cid, "監査レビューでの予約")
        self.store.conn.execute(
            "UPDATE reeval_queue SET created_at='2026-10-17T00:00:00+00:00' "
            "WHERE candidate_id=?", (cid,))
        self.store.conn.commit()
        sel = select_for_eval(self.store, CFG, AS_OF, 10)
        self.assertNotIn(cid, [c["id"] for c in sel])


class FirstEvalSelectionTests(StoreFixtureBase):
    """F-1: 帰属可能な long 評価が無い企業の初回評価を上限の半分まで前面に出す。"""

    def _core_count(self):
        return sum(1 for e in CFG["entities"]["entities"]
                   if (e.get("tier") or "core") == "core")

    def test_attributable_entities_final_and_hold_only(self):
        a = self._candidate(status="screened")
        self.assertEqual(self.store.entities_with_attributable_long_eval(), set())
        self._long_eval(a, status="hold")
        self.assertEqual(self.store.entities_with_attributable_long_eval(),
                         {"astroscale"})
        b = self._candidate(status="screened",
                            affiliations=["ispace, inc."])
        self._long_eval(b, status="error")
        self.assertEqual(self.store.entities_with_attributable_long_eval(),
                         {"astroscale"})

    def test_first_eval_fronted_half_cap_and_one_per_company(self):
        a_new = self._candidate(published="2026-10-15", status="screened")
        a_old = self._candidate(published="2026-10-14", status="screened")
        i_new = self._candidate(published="2026-10-13", status="screened",
                                affiliations=["ispace, inc."])
        # 評価済み企業の再評価候補 (材料更新あり) は一般選定に回る
        s_cand = self._candidate(published="2026-10-16", status="evaluated",
                                 eval_at="2026-09-20T00:00:00+00:00",
                                 updated="2026-10-19T00:00:00+00:00",
                                 affiliations=["Synspective Inc."])
        self._long_eval(s_cand)
        stats = {}
        sel = select_for_eval(self.store, CFG, AS_OF, 4, stats=stats)
        ids = [c["id"] for c in sel]
        # 前面は初回評価から上限の半分 (2件) まで・1社1件。残りは一般、
        # 初回評価の溢れ (同じ企業の2件目) の順
        self.assertEqual(ids, [a_new, i_new, s_cand, a_old])
        self.assertEqual(stats["firsteval_eligible_companies"], 2)
        self.assertEqual(stats["firsteval_selected"], 3)
        self.assertEqual(stats["firsteval_companies_remaining"],
                         self._core_count() - 1)

    def test_first_eval_ahead_of_oldgen(self):
        # 初回評価は再評価 (旧世代の寄せを含む) より前に置く
        first = self._candidate(published="2026-10-10", status="screened")
        old = self._candidate(published="2026-10-16", status="evaluated",
                              eval_at="2026-09-20T00:00:00+00:00",
                              affiliations=["Synspective Inc."])
        self._long_eval(old, config="2026.11.1",
                        prompts={"extract": {"version": "v2"},
                                 "long": {"version": "v2"}})
        stats = {}
        sel = select_for_eval(self.store, CFG, AS_OF, 2, stats=stats)
        self.assertEqual([c["id"] for c in sel], [first, old])
        self.assertEqual(stats["firsteval_selected"], 1)
        self.assertEqual(stats["oldgen_selected"], 1)

    def test_multi_strong_candidate_not_fronted(self):
        # strong リンクが複数企業に跨る候補は、評価しても帰属可能な評価に
        # ならない (一意でない) ため前面の対象にしない
        both = self._candidate(published="2026-10-15", status="screened",
                               affiliations=["Astroscale Holdings Inc.",
                                             "ispace, inc."])
        stats = {}
        sel = select_for_eval(self.store, CFG, AS_OF, 4, stats=stats)
        self.assertEqual([c["id"] for c in sel], [both])
        self.assertEqual(stats["firsteval_eligible_companies"], 0)
        self.assertEqual(stats["firsteval_selected"], 0)
        self.assertEqual(stats["firsteval_companies_remaining"],
                         self._core_count())


class ReferenceItemTests(unittest.TestCase):
    """N-1: 参考項目の欠落は理由コードで記録し、状態を判定できる。"""

    def _details(self, items):
        obj = {"items": items, "next_sources": [], "reeval_conditions": []}
        _scores, details, _errors = ev_ns.validate_assessment(obj, "long", CFG)
        return details

    def test_omitted_reference_item_recorded_with_code(self):
        items = {k: item(70) for k in LONG_KEYS}
        d = self._details(items)[REF_KEY]
        self.assertIsNone(d["score"])
        self.assertEqual(d["missing_codes"], ["item_omitted"])
        self.assertEqual(d["missing_info"], [])  # 表示用の不足理由には混ぜない
        from trlcore.evaluate import reference_item_state
        self.assertEqual(reference_item_state(d), "omitted")

    def test_explicit_null_and_scored_states(self):
        from trlcore.evaluate import reference_item_state
        items = {k: item(70) for k in LONG_KEYS}
        items[REF_KEY] = item(None)
        self.assertEqual(reference_item_state(self._details(items)[REF_KEY]),
                         "explicit_null")
        items[REF_KEY] = item(55)
        details = self._details(items)
        self.assertEqual(reference_item_state(details[REF_KEY]), "scored")
        self.assertEqual(details[REF_KEY]["score"], 55)


class SubjectSpanTests(unittest.TestCase):
    """N-2: 原文に無い subject は claim から落とし、件数を返す。"""

    CAND = {"title": "New battery electrolyte study",
            "abstract": ABSTRACT, "fields": ["battery"]}

    def test_drops_subject_not_in_source(self):
        ext = {"facts": [
                   {"statement": "s1", "source": "u",
                    "subject": "battery electrolyte"},
                   {"statement": "s2", "source": "u",
                    "subject": "quantum computer"},
                   {"statement": "s3", "source": "u"}],
               "inferences": [], "forecasts": [], "hypotheses": [],
               "counter_points": [], "missing_info": []}
        out, stats = ev_ns.validate_subject_spans(ext, self.CAND)
        self.assertEqual(stats, {"claims": 3, "with_subject": 2, "dropped": 1})
        self.assertEqual(out["facts"][0]["subject"], "battery electrolyte")
        self.assertNotIn("subject", out["facts"][1])
        self.assertEqual(out["facts"][1]["statement"], "s2")  # claim は残る
        self.assertIs(ext["facts"][1].get("subject"), "quantum computer")  # 元は不変

    def test_casefold_and_whitespace_insensitive(self):
        ext = {"facts": [{"statement": "s", "source": "u",
                          "subject": "Battery   Electrolyte"}],
               "inferences": [], "forecasts": [], "hypotheses": [],
               "counter_points": [], "missing_info": []}
        out, stats = ev_ns.validate_subject_spans(ext, self.CAND)
        self.assertEqual(stats["dropped"], 0)
        self.assertIs(out, ext)  # 落とす対象が無ければコピーしない


class PipelineObservationTests(StoreFixtureBase):
    """N-1 / N-2 の実行 counts への集計。"""

    def test_counts_reference_and_subject(self):
        def fn(prompt):
            if "根拠抽出」担当" in prompt:
                return json.dumps({"facts": [
                    {"statement": "s1", "source": "u",
                     "subject": "battery electrolyte"},
                    {"statement": "s2", "source": "u",
                     "subject": "quantum computer"},
                    {"statement": "s3", "source": "u"}],
                    "inferences": [], "forecasts": [], "hypotheses": [],
                    "counter_points": [], "missing_info": []})
            if "長期成長評価」担当" in prompt:
                return json.dumps(assessment(LONG_KEYS))  # 参考項目は出さない
            raise AssertionError("unknown prompt")

        r = run_pipeline(self.store, cfg_with(), Mock(fn), as_of=AS_OF,
                         kinds=("long",), fetchers=fetch_n(1),
                         sources=("arxiv",), today=AS_OF, log=lambda *_: None)
        c = r["counts"]
        self.assertEqual(c["final"], 1)
        self.assertEqual(c["reference_item_states"],
                         {"scored": 0, "explicit_null": 0, "omitted": 1})
        self.assertEqual(c["subject_claims"], 3)
        self.assertEqual(c["subject_attached"], 1)
        self.assertEqual(c["subject_dropped"], 1)
        evrow = self.store.evaluations_for_run(r["run_id"], "long")[0]
        facts = json.loads(evrow["extraction_json"])["facts"]
        self.assertEqual(facts[0]["subject"], "battery electrolyte")
        self.assertNotIn("subject", facts[1])


class AuditReferenceExclusionTests(StoreFixtureBase):
    """N-1 監査側: 項目走査は参考項目を母数から外す。"""

    def _audit(self, ref_item):
        cid = self._candidate()
        self.store.set_entities(
            cid, [{"entity_id": "astroscale", "strength": "strong",
                   "matched_by": "title", "matched_alias": "Astroscale"}])
        items = {k: item(70) for k in LONG_KEYS}
        items[REF_KEY] = ref_item
        ev = {"kind": "long", "as_of": "2026-10-20", "status": "final",
              "total": 70.0, "band": {"label": "strong"}, "items": items,
              "extraction": EXTRACTION, "event": None, "prompt_versions": {},
              "config_version": CFG["config_version"],
              "config_hash": CFG["config_hash"], "llm_provider": "gemini",
              "llm_model": "m", "input_hash": f"h-audit-{cid}", "errors": [],
              "extra": {"trl_level": {"level": 8, "reason": "実証段階",
                                       "evidence": [], "missing_info": []}}}
        eid = self.store.add_evaluation("r0", cid, ev)
        row = dict(self.store.conn.execute(
            "SELECT * FROM evaluations WHERE id = ?", (eid,)).fetchone())
        inp = T.build_audit_input(self.store, row, CFG)
        self.assertEqual(inp["reference_items"], [REF_KEY])
        return T.audit_evaluation_rules(inp, T.load_rules()["rules"])

    def test_a7_ignores_reference_item(self):
        # 除外が無ければ A7 が flag になる形 (低得点+漠然フレーズ+不足なし)
        entries = self._audit({"score": 30, "reason": "根拠が乏しい",
                               "evidence": [{"ref": "https://example.org/x",
                                             "summary": "要約", "kind": "fact"}],
                               "counter_evidence": [], "missing_info": []})
        self.assertEqual([e for e in entries if e["check"] == "A7"], [])

    def test_a9_ignores_reference_item(self):
        entries = self._audit({"score": 90, "reason": "理由",
                               "evidence": [], "counter_evidence": ["反証"],
                               "missing_info": []})
        self.assertEqual([e for e in entries
                          if e["check"] == "A9" and e.get("item") == REF_KEY], [])


def _bars(n=130, jump_idx=100, jump=0.08):
    days, d = [], date(2026, 3, 2)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    closes, price = [], 100.0
    for i in range(n):
        price *= 1.004 if i % 2 == 0 else 0.997
        if i == jump_idx + 1:
            price *= 1 + jump
        closes.append(price)
    return [(d.isoformat(), c, 2000.0 if i in (jump_idx, jump_idx + 1) else 1000.0)
            for i, (d, c) in enumerate(zip(days, closes))], days


class EventFillTests(StoreFixtureBase):
    """O-2: event 充当の選定と、定期実行 (kinds 未指定) での発動。"""

    def _cfg(self):
        cfg = dict(CFG)
        cfg["entities"] = {"entities": [
            {"id": "acme", "name": "Acme", "ticker": "ACME", "market": "US",
             "tier": "core", "sector": "bio"},
            {"id": "notick", "name": "NoTick", "market": "US",
             "tier": "core", "sector": "bio"}]}
        cfg["sources"] = {"defaults": {"max_results": 5, "lookback_days": 30},
                          "queries": [{"name": "q", "arxiv": "a"}]}
        return cfg

    def _cand(self, entity, published, status="screened", **kw):
        self._seq += 1
        rec, problems = normalize(
            raw(sid=f"2610.{self._seq:05d}", published=published,
                title=f"{entity} の発表", affiliations=[], **kw),
            "2026-10-20T00:00:00+00:00")
        assert problems == []
        cid, _ = self.store.upsert_candidate(rec, "r0")
        self.store.set_entities(cid, [{"entity_id": entity, "strength": "strong",
                                       "matched_by": "title",
                                       "matched_alias": entity}])
        self.store.set_status(cid, status)
        return cid

    def _event_eval(self, cid, config=None, prompts=None, as_of="2026-09-20"):
        ev = {"kind": "event", "as_of": as_of, "status": "final",
              "total": 50.0, "band": {}, "items": {}, "extraction": {},
              "event": {},
              "prompt_versions": prompts if prompts is not None else {
                  "extract": {"version": CFG["scoring"]["prompts"]["extract"]},
                  "event": {"version": CFG["scoring"]["prompts"]["event"]}},
              "config_version": config or CFG["config_version"],
              "config_hash": "h", "llm_provider": "g", "llm_model": "m",
              "input_hash": f"h-ev-{cid}-{self._seq}-{config}",
              "errors": [],
              "extra": {"as_evaluated": {
                  "total": 50.0, "band": "x",
                  "config_version": config or CFG["config_version"]}}}
        return self.store.add_evaluation("r0", cid, ev)

    def test_select_event_fill(self):
        from trlcore.pipeline import select_event_fill
        cfg = self._cfg()
        bars, days = _bars()
        self.store.save_price_series("ACME", bars)
        as_of = days[-1]
        target = self._cand("acme", days[100].isoformat())
        done = self._cand("acme", days[100].isoformat())
        self._event_eval(done, as_of=days[110].isoformat())  # 現行世代で評価済み
        no_symbol = self._cand("notick", days[100].isoformat())
        got = select_event_fill(self.store, cfg, as_of, 5, set(),
                                log=lambda *_: None)
        self.assertEqual([c["id"] for c in got], [target])
        self.assertNotIn(no_symbol, [c["id"] for c in got])

    def test_select_event_fill_includes_oldgen(self):
        # 世代の移行: 最新 event 評価が旧世代の候補は、材料更新が無くても
        # 「評価済み」とみなさず充当の対象に残す (long の TH-1 緩和と同じ)
        from trlcore.pipeline import select_event_fill
        cfg = self._cfg()
        bars, days = _bars()
        self.store.save_price_series("ACME", bars)
        as_of = days[-1]
        old = self._cand("acme", days[100].isoformat())
        self._event_eval(old, config="2026.11.0",
                         prompts={"extract": {"version": "v2"},
                                  "event": {"version": "v2"}},
                         as_of=days[110].isoformat())
        got = select_event_fill(self.store, cfg, as_of, 5, set(),
                                log=lambda *_: None)
        self.assertEqual([c["id"] for c in got], [old])

    def test_pipeline_event_fill(self):
        cfg = self._cfg()
        bars, days = _bars()
        self.store.save_price_series("ACME", bars)
        as_of = days[-1]
        fresh = self._cand("acme", days[125].isoformat())
        # 評価済み (材料更新なし) で long 再評価は TH-1 ゲートに止まるが、
        # event は未評価 → 充当の対象になる
        old = self._cand("acme", days[100].isoformat(), status="evaluated")
        self.store.conn.execute(
            "UPDATE candidates SET last_eval_at=? WHERE id=?",
            (days[110].isoformat() + "T00:00:00+00:00", old))
        self.store.conn.commit()
        self._long_eval(old)
        llm = Mock(make_llm_fn())
        r = run_pipeline(self.store, cfg, llm, as_of=as_of, sources=("arxiv",),
                         kinds=("long",), max_eval=2, do_collect=False,
                         event_fill=True, today=as_of, log=lambda *_: None)
        c = r["counts"]
        self.assertEqual(c["selected"], 1)
        self.assertEqual(c["event_fill"], 1)
        evs = self.store.evaluations_for_run(r["run_id"], "event")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["candidate_id"], old)
        longs = self.store.evaluations_for_run(r["run_id"], "long")
        self.assertEqual([e["candidate_id"] for e in longs], [fresh])


if __name__ == "__main__":
    unittest.main()
