"""tier 導出レイヤー (trlcore/tiers.py、CRITERIA.md §4/§6) のテスト。

実行: python -m unittest test_tiers -v
導出は辞書 (core/watch/reference) を変えない別層で、基礎 core の企業は
(a) 最新の帰属可能な long 評価、(b) strong リンクの一意性、(c) 最新監査の
未処分の要確認なし、の3条件を全て満たせば Core、欠ければ Conditional。
"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import trl as T
from trl import *  # noqa
from testutil import CFG, cfg_with, raw


ENTS = {"entities": [
    {"id": "acme", "name": "Acme", "tier": "core"},
    {"id": "beta", "name": "Beta", "tier": "core"},
    {"id": "wco", "name": "Watch Co", "tier": "watch"},
    {"id": "rco", "name": "Ref Co", "tier": "reference"},
]}


def ev_info(**kw):
    d = {"evaluation_id": 7, "candidate_id": "c1",
         "strong_entity_ids": {"acme"}, "open_flags": []}
    d.update(kw)
    return d


class DeriveTierPureTests(unittest.TestCase):
    """純粋関数 derive_tier の条件分岐。"""

    def test_watch_is_look_reference_is_see(self):
        self.assertEqual(T.derive_tier("wco", "watch", None)["derived_tier"], "look")
        self.assertEqual(T.derive_tier("rco", "reference", None)["derived_tier"], "see")

    def test_core_all_conditions_met(self):
        r = T.derive_tier("acme", "core", ev_info())
        self.assertEqual(r["derived_tier"], "core")
        self.assertEqual(r["evaluation_id"], 7)
        self.assertTrue(all(x["ok"] for x in r["reasons"]))

    def test_core_no_evaluation_is_conditional(self):
        r = T.derive_tier("acme", "core", None)
        self.assertEqual(r["derived_tier"], "conditional")
        by_rule = {x["rule"]: x for x in r["reasons"]}
        self.assertFalse(by_rule["latest_long_eval"]["ok"])

    def test_core_attribution_not_unique(self):
        r = T.derive_tier("acme", "core",
                          ev_info(strong_entity_ids={"acme", "beta"}))
        self.assertEqual(r["derived_tier"], "conditional")
        by_rule = {x["rule"]: x for x in r["reasons"]}
        self.assertTrue(by_rule["latest_long_eval"]["ok"])
        self.assertFalse(by_rule["attribution"]["ok"])
        self.assertIn("beta", by_rule["attribution"]["detail"])

    def test_core_open_flag_is_conditional(self):
        r = T.derive_tier("acme", "core", ev_info(open_flags=[("A8", "monetization")]))
        self.assertEqual(r["derived_tier"], "conditional")
        by_rule = {x["rule"]: x for x in r["reasons"]}
        self.assertFalse(by_rule["audit"]["ok"])
        self.assertIn("A8", by_rule["audit"]["detail"])


class TierFixture(unittest.TestCase):
    """DB を使った導出 (compute_derived_tiers) のフィクスチャ。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "trl.sqlite")
        self._seq = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _candidate(self, links, title="対象の論文"):
        self._seq += 1
        rec, problems = normalize(raw(title=title, sid=f"2610.{self._seq:05d}"),
                                  "2026-10-20T00:00:00+00:00")
        assert problems == []
        cid, _ = self.store.upsert_candidate(rec, "r0")
        self.store.set_entities(cid, [
            {"entity_id": eid, "strength": strength, "matched_by": "title",
             "matched_alias": eid} for eid, strength in links])
        return cid

    def _evaluation(self, cid, kind="long", status="final"):
        ev = {"kind": kind, "as_of": "2026-10-20", "status": status,
              "total": 80.0, "band": {"label": "A"}, "items": {},
              "extraction": {}, "event": None, "prompt_versions": {},
              "config_version": CFG["config_version"],
              "config_hash": CFG["config_hash"], "llm_provider": "gemini",
              "llm_model": "m", "input_hash": f"h-{cid}-{kind}-{self._seq}-{id(self)}",
              "errors": [], "extra": {}}
        ev_id = self.store.add_evaluation("r0", cid, ev)
        return ev_id

    def _audit(self, cid, ev_id, flags, provider="rules", model="rules/v4.1",
               sha="sha-1"):
        self.store.add_audit_result({
            "evaluation_id": ev_id, "candidate_id": cid, "run_id": "ra",
            "auditor_provider": provider, "auditor_model": model,
            "prompt_version": "v1", "prompt_sha": sha, "flags": flags,
            "summary": ""})

    def _decision(self, ev_id, disposition, check="A8", item="monetization",
                  source_id=None):
        self._seq += 1
        self.store.add_review_decision({
            "source_id": source_id or f"src-{self._seq}", "evaluation_id": ev_id,
            "audit_id": None, "check": check, "item": item,
            "auditor": "rules/v4.1", "disposition": disposition, "note": "",
            "decided_at": "2026-11-10T00:00:00+00:00"})

    def derive(self):
        return T.compute_derived_tiers(self.store, ENTS)


class ComputeDerivedTiersTests(TierFixture):
    def test_clean_company_is_core(self):
        cid = self._candidate([("acme", "strong")])
        self._evaluation(cid)
        out = self.derive()
        self.assertEqual(out["acme"]["derived_tier"], "core")
        self.assertEqual(out["wco"]["derived_tier"], "look")
        self.assertEqual(out["rco"]["derived_tier"], "see")
        self.assertEqual(out["beta"]["derived_tier"], "conditional")  # 未評価

    def test_open_flag_makes_conditional(self):
        cid = self._candidate([("acme", "strong")])
        ev_id = self._evaluation(cid)
        self._audit(cid, ev_id, [{"check": "A8", "item": "monetization",
                                  "verdict": "flag"}])
        self.assertEqual(self.derive()["acme"]["derived_tier"], "conditional")

    def test_gray_does_not_drop(self):
        cid = self._candidate([("acme", "strong")])
        ev_id = self._evaluation(cid)
        self._audit(cid, ev_id, [{"check": "A14", "item": "", "verdict": "gray"}])
        self.assertEqual(self.derive()["acme"]["derived_tier"], "core")

    def test_api_flag_makes_conditional(self):
        cid = self._candidate([("acme", "strong")])
        ev_id = self._evaluation(cid)
        self._audit(cid, ev_id, [{"check": "CT1", "item": "NCT00000000",
                                  "verdict": "flag"}],
                    provider="api", model="api/v1", sha="sha-api")
        self.assertEqual(self.derive()["acme"]["derived_tier"], "conditional")

    def test_llm_audit_is_ignored(self):
        # LLM 監査は廃止済み。過去行が残っていても層を左右しない
        cid = self._candidate([("acme", "strong")])
        ev_id = self._evaluation(cid)
        self._audit(cid, ev_id, [{"check": "X", "item": "", "verdict": "flag"}],
                    provider="gemini", model="gemini-3.6-flash", sha="sha-llm")
        self.assertEqual(self.derive()["acme"]["derived_tier"], "core")

    def test_false_positive_disposition_restores_core(self):
        cid = self._candidate([("acme", "strong")])
        ev_id = self._evaluation(cid)
        self._audit(cid, ev_id, [{"check": "A8", "item": "monetization",
                                  "verdict": "flag"}])
        self._decision(ev_id, "false_positive")
        self.assertEqual(self.derive()["acme"]["derived_tier"], "core")

    def test_confirmed_and_watch_dispositions_still_count(self):
        for disp in ("confirmed", "watch"):
            cid = self._candidate([("acme", "strong")], title=f"論文 {disp}")
            ev_id = self._evaluation(cid)
            self._audit(cid, ev_id, [{"check": "A8", "item": "monetization",
                                      "verdict": "flag"}])
            self._decision(ev_id, disp)
            self.assertEqual(self.derive()["acme"]["derived_tier"], "conditional",
                             disp)
            # 次の周回に備えて状態を戻す (同じ acme を使い回すため)
            self.store.conn.execute("DELETE FROM review_decisions")
            self.store.conn.execute("DELETE FROM audit_results")
            self.store.conn.commit()

    def test_latest_disposition_wins(self):
        cid = self._candidate([("acme", "strong")])
        ev_id = self._evaluation(cid)
        self._audit(cid, ev_id, [{"check": "A8", "item": "monetization",
                                  "verdict": "flag"}])
        self._decision(ev_id, "false_positive", source_id="d1")
        self._decision(ev_id, "confirmed", source_id="d1")
        self.assertEqual(self.derive()["acme"]["derived_tier"], "conditional")

    def test_newer_audit_generation_supersedes(self):
        cid = self._candidate([("acme", "strong")])
        ev_id = self._evaluation(cid)
        self._audit(cid, ev_id, [{"check": "A8", "item": "monetization",
                                  "verdict": "flag"}], sha="sha-old")
        self._audit(cid, ev_id, [], model="rules/v4.1", sha="sha-new")
        self.assertEqual(self.derive()["acme"]["derived_tier"], "core")

    def test_only_latest_evaluation_counts(self):
        # 古い評価の flag は最新評価には効かない (D-7)
        cid = self._candidate([("acme", "strong")])
        old_id = self._evaluation(cid)
        self._audit(cid, old_id, [{"check": "A8", "item": "monetization",
                                   "verdict": "flag"}])
        self._evaluation(cid)  # 新しい評価 (監査なし)
        self.assertEqual(self.derive()["acme"]["derived_tier"], "core")
        # 逆向き: 最新の評価に flag があれば落ちる
        cid2 = self._candidate([("beta", "strong")], title="Beta の論文")
        self._evaluation(cid2)
        new_id = self._evaluation(cid2)
        self._audit(cid2, new_id, [{"check": "A2", "item": "trl_level",
                                    "verdict": "flag"}])
        self.assertEqual(self.derive()["beta"]["derived_tier"], "conditional")

    def test_event_only_is_conditional(self):
        cid = self._candidate([("acme", "strong")])
        self._evaluation(cid, kind="event")
        r = self.derive()["acme"]
        self.assertEqual(r["derived_tier"], "conditional")
        by_rule = {x["rule"]: x for x in r["reasons"]}
        self.assertFalse(by_rule["latest_long_eval"]["ok"])

    def test_shared_strong_link_is_not_unique(self):
        cid = self._candidate([("acme", "strong"), ("beta", "strong")])
        self._evaluation(cid)
        out = self.derive()
        self.assertEqual(out["acme"]["derived_tier"], "conditional")
        self.assertEqual(out["beta"]["derived_tier"], "conditional")

    def test_weak_link_only_is_not_attributable(self):
        cid = self._candidate([("acme", "weak"), ("beta", "strong")])
        self._evaluation(cid)
        out = self.derive()
        self.assertEqual(out["acme"]["derived_tier"], "conditional")
        self.assertEqual(out["beta"]["derived_tier"], "core")

    def test_hold_evaluation_counts_as_evaluation(self):
        cid = self._candidate([("acme", "strong")])
        self._evaluation(cid, status="hold")
        self.assertEqual(self.derive()["acme"]["derived_tier"], "core")


class DistributionTests(unittest.TestCase):
    def _dist(self, tiers):
        return T.tier_distribution(
            {f"e{i}": {"derived_tier": t} for i, t in enumerate(tiers)})

    def test_counts_shares_and_no_warning_at_targets(self):
        d = self._dist(["core"] * 20 + ["conditional"] * 30
                       + ["look"] * 40 + ["see"] * 10)
        self.assertEqual(d["total"], 100)
        self.assertEqual(d["counts"], {"core": 20, "conditional": 30,
                                       "look": 40, "see": 10})
        self.assertEqual(d["shares"]["core"], 20.0)
        self.assertEqual(d["warnings"], [])
        self.assertEqual(d["version"], "tiers/v1")

    def test_warning_beyond_five_points(self):
        d = self._dist(["core"] * 26 + ["conditional"] * 24
                       + ["look"] * 40 + ["see"] * 10)
        warned = {w["tier"] for w in d["warnings"]}
        self.assertEqual(warned, {"core", "conditional"})
        core_w = [w for w in d["warnings"] if w["tier"] == "core"][0]
        self.assertEqual(core_w["diff_pt"], 6.0)

    def test_exactly_five_points_is_within_tolerance(self):
        d = self._dist(["core"] * 25 + ["conditional"] * 25
                       + ["look"] * 40 + ["see"] * 10)
        self.assertEqual(d["warnings"], [])


class SnapshotTests(TierFixture):
    def test_record_and_history(self):
        derived = self.derive()
        n = self.store.record_tier_snapshot(derived, "2026-11-10T00:00:00+00:00",
                                            T.TIER_RULES_VERSION)
        self.assertEqual(n, len(ENTS["entities"]))
        hist = self.store.tier_snapshot_history("acme")
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["derived_tier"], "conditional")
        self.assertEqual(hist[0]["rule_version"], "tiers/v1")
        self.assertTrue(hist[0]["reasons"])
        # append-only: 2回目の記録で履歴が積み上がる (上書きされない)
        self.store.record_tier_snapshot(derived, "2026-11-11T00:00:00+00:00",
                                        T.TIER_RULES_VERSION)
        self.assertEqual(len(self.store.tier_snapshot_history("acme")), 2)

    def test_export_records_snapshot_and_summary(self):
        cfg = cfg_with()
        out = Path(self.tmp.name) / "site"
        data = T.export_site(self.store, cfg, out,
                             now=datetime(2026, 11, 10, tzinfo=timezone.utc))
        tiers = data["summary"]["tiers"]
        total = len(cfg["entities"]["entities"])
        self.assertEqual(tiers["total"], total)
        # 空 DB では評価が無いので core 層は 0、基礎 core は全社 Conditional
        base_core = sum(1 for e in cfg["entities"]["entities"]
                        if (e.get("tier") or "core") == "core")
        self.assertEqual(tiers["counts"]["core"], 0)
        self.assertEqual(tiers["counts"]["conditional"], base_core)
        hist = self.store.tier_snapshot_history()
        self.assertEqual(len(hist), total)
        # watch_companies と候補側の双方に derived_tier が付く
        self.assertTrue(all("derived_tier" in w
                            for w in data["watch_companies"]))
        self.assertEqual({w["derived_tier"] for w in data["watch_companies"]},
                         {"look", "see"})


if __name__ == "__main__":
    unittest.main()
