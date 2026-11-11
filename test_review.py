"""監査レビュー段階B (review_inbox.json の取り込み・再評価キュー・
TRL 調整の export 反映) のテスト。

実行: python -m unittest test_review -v
設計: REVIEW_DESIGN.md (監査レビューページ+調整レイヤー) の段階B。
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import trl as T
from trl import *  # noqa
from trlcore import pipeline as pipeline_mod
from testutil import (AS_OF, CFG, EXTRACTION, LONG_KEYS, Mock, cfg_with,
                      fetch_n, item, make_llm_fn, raw)

TECH = ("technology_readiness", "technological_advantage")


class ReviewFixture(unittest.TestCase):
    """評価 (TRL つき) を直接作るフィクスチャ。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "trl.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _mk_eval(self, title="レビュー対象の論文", trl_level=9, tech=80, biz=40,
                 biz_missing=False, run_id="r0"):
        self._seq = getattr(self, "_seq", 0) + 1
        rec, problems = normalize(raw(title=title, sid=f"2610.{self._seq:05d}"),
                                  "2026-10-20T00:00:00+00:00")
        self.assertEqual(problems, [])
        cid, _ = self.store.upsert_candidate(rec, run_id)
        self.store.set_entities(cid, [{"entity_id": "astroscale", "strength": "strong",
                                       "matched_by": "title", "matched_alias": "Astroscale"}])
        scores = {k: (tech if k in TECH else (None if biz_missing else biz))
                  for k in LONG_KEYS}
        out = T.compute(scores, "long", CFG, trl_level=trl_level)
        ev = {"kind": "long", "as_of": "2026-10-20", "status": out["status"],
              "total": out["total"], "band": out["band"],
              "items": {k: item(s) for k, s in scores.items()},
              "extraction": EXTRACTION, "event": None, "prompt_versions": {},
              "config_version": CFG["config_version"], "config_hash": CFG["config_hash"],
              "llm_provider": "gemini", "llm_model": "gemini-3.5-flash-lite",
              "input_hash": f"h-{title}-{run_id}", "errors": [],
              "extra": {"trl_level": {"level": trl_level, "reason": "根拠あり",
                                       "evidence": [], "missing_info": []},
                        "axes": out["axes"], "business_stage": out["business_stage"],
                        "total_scope": out["total_scope"]}}
        self.store.add_evaluation(run_id, cid, ev)
        return cid, self.store.latest_evaluation(cid, "long")["id"]

    def _mk_audit(self, cid, eid, model="rules/v4.1"):
        self.store.add_audit_result({
            "evaluation_id": eid, "candidate_id": cid, "run_id": "ra",
            "auditor_provider": "rules", "auditor_model": model,
            "prompt_version": "v4.1", "prompt_sha": "sha-rules",
            "flags": [{"check": "A2", "item": "trl_level",
                       "detail": "TRL 9 の継続証拠が無い", "verdict": "flag",
                       "rule": "trl9_continuation"}],
            "summary": "要確認1件"})
        row = self.store.conn.execute(
            "SELECT id FROM audit_results WHERE evaluation_id=?", (eid,)).fetchone()
        return row[0]


class ImportDecisionsTests(ReviewFixture):
    def _inbox(self, **kw):
        d = {"id": "uuid-d1", "evaluation_id": kw.get("eid"), "check": "A2",
             "item": "trl_level", "auditor": "rules/v4.1",
             "disposition": kw.get("disposition", "confirmed"),
             "note": kw.get("note", "材料を確認した"),
             "decided_at": "2026-11-10T00:00:00+00:00"}
        return {"version": 1, "decisions": [d], "reeval_queue": [], "adjustments": []}

    def test_import_records_decision_with_audit_id(self):
        cid, eid = self._mk_eval()
        audit_id = self._mk_audit(cid, eid)
        out = T.import_inbox(self.store, self._inbox(eid=eid))
        self.assertEqual(out["decisions"], 1)
        rows = self.store.conn.execute("SELECT * FROM review_decisions").fetchall()
        self.assertEqual(len(rows), 1)
        r = dict(rows[0])
        self.assertEqual(r["source_id"], "uuid-d1")
        self.assertEqual(r["evaluation_id"], eid)
        self.assertEqual(r["audit_id"], audit_id)
        self.assertEqual(r["disposition"], "confirmed")

    def test_import_is_idempotent(self):
        cid, eid = self._mk_eval()
        self._mk_audit(cid, eid)
        inbox = self._inbox(eid=eid)
        T.import_inbox(self.store, inbox)
        out = T.import_inbox(self.store, inbox)
        self.assertEqual(out["decisions"], 0)
        self.assertEqual(out["decisions_dup"], 1)
        n = self.store.conn.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0]
        self.assertEqual(n, 1)

    def test_changed_disposition_supersedes_with_new_row(self):
        # 同じ UUID で処分を変えた再送は、新しい行として足され最新が有効になる
        cid, eid = self._mk_eval()
        self._mk_audit(cid, eid)
        T.import_inbox(self.store, self._inbox(eid=eid))
        out = T.import_inbox(self.store, self._inbox(eid=eid, disposition="watch"))
        self.assertEqual(out["decisions"], 1)
        rows = self.store.conn.execute(
            "SELECT disposition FROM review_decisions ORDER BY id").fetchall()
        self.assertEqual([r[0] for r in rows], ["confirmed", "watch"])

    def test_decision_without_matching_audit_still_recorded(self):
        cid, eid = self._mk_eval()  # 監査を作らない
        out = T.import_inbox(self.store, self._inbox(eid=eid))
        self.assertEqual(out["decisions"], 1)
        r = self.store.conn.execute("SELECT audit_id FROM review_decisions").fetchone()
        self.assertIsNone(r[0])

    def test_invalid_decisions_are_skipped(self):
        cid, eid = self._mk_eval()
        out = T.import_inbox(self.store, self._inbox(eid=eid, disposition="unknown"))
        self.assertEqual(out["skipped"], 1)
        out = T.import_inbox(self.store, self._inbox(eid=99999))
        self.assertEqual(out["skipped"], 1)
        n = self.store.conn.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0]
        self.assertEqual(n, 0)


class ImportAdjustmentsTests(ReviewFixture):
    def _inbox(self, eid, **kw):
        a = {"id": kw.get("id", "uuid-a1"), "evaluation_id": eid, "field": "trl_level",
             "from_value": kw.get("from_value", 9), "to_value": kw.get("to_value", 8),
             "reason": kw.get("reason", "継続運用の根拠が無いため")}
        return {"version": 1, "decisions": [], "reeval_queue": [], "adjustments": [a]}

    def test_import_adjustment_and_idempotency(self):
        cid, eid = self._mk_eval()
        inbox = self._inbox(eid)
        out = T.import_inbox(self.store, inbox)
        self.assertEqual(out["adjustments"], 1)
        out = T.import_inbox(self.store, inbox)
        self.assertEqual(out["adjustments_dup"], 1)
        adj = self.store.latest_adjustment(eid)
        self.assertEqual((adj["from_value"], adj["to_value"]), (9, 8))

    def test_adjustment_requires_reason_and_valid_level(self):
        cid, eid = self._mk_eval()
        for kw in ({"reason": "  "}, {"to_value": 10}, {"to_value": "8"}, {"to_value": 0}):
            out = T.import_inbox(self.store, self._inbox(eid, **kw))
            self.assertEqual(out["skipped"], 1, kw)
        self.assertIsNone(self.store.latest_adjustment(eid))

    def test_adjustment_for_undecided_trl_is_skipped(self):
        cid, eid = self._mk_eval(trl_level=None)
        out = T.import_inbox(self.store, self._inbox(eid, from_value=0, to_value=8))
        self.assertEqual(out["skipped"], 1)
        self.assertIsNone(self.store.latest_adjustment(eid))


class AdjustedViewTests(ReviewFixture):
    def _adjust(self, eid, to_value, reason="根拠の読み替え", sid="uuid-a1"):
        return T.import_inbox(self.store, {"version": 1, "decisions": [],
                                           "reeval_queue": [], "adjustments": [
            {"id": sid, "evaluation_id": eid, "field": "trl_level",
             "from_value": 9, "to_value": to_value, "reason": reason}]})

    def test_adjusted_view_recomputes_from_items(self):
        # 事業項目が欠けた hold (TRL 9) を TRL 3 に読み替えると、前商業段階
        # として技術軸 (80) だけで final に再導出される。項目点は元のまま
        cid, eid = self._mk_eval(trl_level=9, tech=80, biz_missing=True)
        ev_row = self.store.latest_evaluation(cid, "long")
        self.assertEqual(ev_row["status"], "hold")
        self.assertIsNone(ev_row["total"])
        self._adjust(eid, 3)
        view = T.adjusted_view(self.store, CFG, ev_row)
        self.assertIsNotNone(view)
        self.assertEqual((view["from_value"], view["to_value"]), (9, 3))
        self.assertEqual(view["total"], 80.0)
        self.assertEqual(view["total_scope"], "technology")
        self.assertEqual(view["status"], "final")
        self.assertEqual(view["reason"], "根拠の読み替え")

    def test_original_evaluation_is_not_rewritten(self):
        cid, eid = self._mk_eval(trl_level=9)
        before = self.store.latest_evaluation(cid, "long")
        self._adjust(eid, 8)
        T.adjusted_view(self.store, CFG, before)
        after = self.store.latest_evaluation(cid, "long")
        self.assertEqual(after["total"], before["total"])
        self.assertEqual(after["band"], before["band"])
        self.assertEqual(after["extra_json"], before["extra_json"])
        self.assertEqual(after["items_json"], before["items_json"])

    def test_cancellation_by_row_with_original_value(self):
        # 取り消しは「最新の調整行の to_value = 元値」で表す (設計書3節)
        cid, eid = self._mk_eval(trl_level=9)
        ev_row = self.store.latest_evaluation(cid, "long")
        self._adjust(eid, 8)
        self.assertIsNotNone(T.adjusted_view(self.store, CFG, ev_row))
        out = self._adjust(eid, 9, reason="取り消し", sid="uuid-a1")
        self.assertEqual(out["adjustments"], 1)  # 同じ UUID の内容変更として足される
        self.assertIsNone(T.adjusted_view(self.store, CFG, ev_row))

    def test_no_adjustment_no_view(self):
        cid, eid = self._mk_eval(trl_level=9)
        ev_row = self.store.latest_evaluation(cid, "long")
        self.assertIsNone(T.adjusted_view(self.store, CFG, ev_row))

    def test_export_carries_adjustment_and_keeps_original(self):
        cid, eid = self._mk_eval(trl_level=9, tech=80, biz_missing=True)
        other_cid, _ = self._mk_eval(title="調整の無い論文", trl_level=7)
        self.assertNotEqual(cid, other_cid)
        self._adjust(eid, 3)
        data = T.build_site_data(self.store, CFG, datetime(2026, 10, 26, tzinfo=timezone.utc))
        by_id = {c["id"]: c for c in data["candidates"]}
        long_ev = by_id[cid]["long"]
        self.assertIn("adjustment", long_ev)
        self.assertEqual(long_ev["adjustment"]["to_value"], 3)
        self.assertEqual(long_ev["adjustment"]["total"], 80.0)
        # 元の値は書き換えられていない (併記のため両方残る)
        self.assertIsNone(long_ev["total"])
        self.assertEqual(long_ev["trl_level"]["level"], 9)
        self.assertNotIn("adjustment", by_id[other_cid]["long"])


class CrossDeviceExportTests(ReviewFixture):
    """処分・予約・調整の端末またぎ: DB の状態が export 経由で全端末へ
    配られることと、処分の取り消し (cleared 墓標) が伝わること。"""

    def _decide(self, eid, disposition="confirmed", sid="uuid-d1", note="材料を確認した"):
        return T.import_inbox(self.store, {"version": 1, "reeval_queue": [],
                                           "adjustments": [], "decisions": [
            {"id": sid, "evaluation_id": eid, "check": "A2", "item": "trl_level",
             "auditor": "rules/v4.1", "disposition": disposition, "note": note,
             "decided_at": "2026-11-10T00:00:00+00:00"}]})

    def test_effective_decisions_latest_row_wins(self):
        cid, eid = self._mk_eval()
        self._mk_audit(cid, eid)
        self._decide(eid)
        eff = self.store.effective_review_decisions()
        rec = eff[(eid, "A2", "trl_level")]
        self.assertEqual(rec["disposition"], "confirmed")
        self.assertEqual(rec["source_id"], "uuid-d1")
        # 同じ UUID で処分を変えた再送が最新の有効状態になる
        self._decide(eid, disposition="watch")
        eff = self.store.effective_review_decisions()
        self.assertEqual(eff[(eid, "A2", "trl_level")]["disposition"], "watch")

    def test_cleared_tombstone_reverts_to_undisposed(self):
        cid, eid = self._mk_eval()
        self._mk_audit(cid, eid)
        self._decide(eid, disposition="false_positive")
        self.assertIn((eid, "A2", "trl_level"), self.store.effective_review_decisions())
        out = self._decide(eid, disposition="cleared")
        self.assertEqual(out["decisions"], 1)  # 墓標も1件として取り込まれる
        self.assertNotIn((eid, "A2", "trl_level"), self.store.effective_review_decisions())
        # 墓標の再送は重複として弾かれる (行は増えない)
        out = self._decide(eid, disposition="cleared")
        self.assertEqual(out["decisions_dup"], 1)
        n = self.store.conn.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0]
        self.assertEqual(n, 2)

    def test_export_carries_decisions_on_audit_blocks(self):
        cid, eid = self._mk_eval()
        self._mk_audit(cid, eid)
        other_cid, other_eid = self._mk_eval(title="処分の無い論文")
        self._mk_audit(other_cid, other_eid)
        self._decide(eid, disposition="watch", note="次回を見る")
        data = T.build_site_data(self.store, CFG, datetime(2026, 10, 26, tzinfo=timezone.utc))
        by_id = {c["id"]: c for c in data["candidates"]}
        blocks = by_id[cid]["audit"]
        self.assertEqual(len(blocks), 1)
        ds = blocks[0]["decisions"]
        self.assertEqual(len(ds), 1)
        self.assertEqual((ds[0]["check"], ds[0]["item"]), ("A2", "trl_level"))
        self.assertEqual(ds[0]["disposition"], "watch")
        self.assertEqual(ds[0]["note"], "次回を見る")
        self.assertEqual(ds[0]["source_id"], "uuid-d1")
        # 処分の無い評価のブロックでは decisions は空の一覧 (常にあるキー)
        self.assertEqual(by_id[other_cid]["audit"][0]["decisions"], [])

    def test_export_marks_open_reeval_booked(self):
        cid, eid = self._mk_eval()
        other_cid, _ = self._mk_eval(title="予約の無い論文")
        T.import_inbox(self.store, {"version": 1, "decisions": [], "adjustments": [],
                                    "reeval_queue": [{"id": "uuid-q1", "candidate_id": cid}]})
        data = T.build_site_data(self.store, CFG, datetime(2026, 10, 26, tzinfo=timezone.utc))
        by_id = {c["id"]: c for c in data["candidates"]}
        self.assertTrue(by_id[cid]["reeval_booked"])
        self.assertNotIn("reeval_booked", by_id[other_cid])

    def test_adjusted_view_carries_source_id(self):
        # 監査ページが DB 側の調整を種として取り込むため、調整の出所
        # (source_id) が export の adjustment に同梱されること
        cid, eid = self._mk_eval(trl_level=9)
        ev_row = self.store.latest_evaluation(cid, "long")
        T.import_inbox(self.store, {"version": 1, "decisions": [], "reeval_queue": [],
                                    "adjustments": [
            {"id": "uuid-a9", "evaluation_id": eid, "field": "trl_level",
             "from_value": 9, "to_value": 8, "reason": "根拠の読み替え"}]})
        view = T.adjusted_view(self.store, CFG, ev_row)
        self.assertEqual(view["source_id"], "uuid-a9")


class ReevalQueueTests(ReviewFixture):
    def test_import_reeval_and_dedup(self):
        cid, eid = self._mk_eval()
        inbox = {"version": 1, "decisions": [], "adjustments": [],
                 "reeval_queue": [{"id": "uuid-q1", "candidate_id": cid}]}
        out = T.import_inbox(self.store, inbox)
        self.assertEqual(out["reeval"], 1)
        out = T.import_inbox(self.store, inbox)
        self.assertEqual(out["reeval_dup"], 1)
        pending = self.store.pending_reeval()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["candidate_id"], cid)

    def test_import_reeval_unknown_candidate_skipped(self):
        out = T.import_inbox(self.store, {"version": 1, "decisions": [], "adjustments": [],
                                          "reeval_queue": [{"id": "uuid-q9",
                                                            "candidate_id": "c_missing"}]})
        self.assertEqual(out["skipped"], 1)
        self.assertEqual(self.store.pending_reeval(), [])


class ReevalQueuePipelineTests(unittest.TestCase):
    """パイプライン選定との統合: 予約は上限の内数・最優先で、実際に
    再評価されたら消化済みになる。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "trl.sqlite")
        self.cfg = cfg_with()
        self.llm = Mock(make_llm_fn())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _run(self, max_eval=1, llm="default"):
        return T.run_pipeline(
            self.store, self.cfg, self.llm if llm == "default" else llm,
            as_of=AS_OF, sources=("arxiv",), kinds=("long",), max_eval=max_eval,
            fetchers=fetch_n(3), today=AS_OF, log=lambda *_: None)

    def _eval_count(self, cid):
        return self.store.conn.execute(
            "SELECT COUNT(*) FROM evaluations WHERE candidate_id=?", (cid,)).fetchone()[0]

    def _evaluated_cid(self):
        row = self.store.conn.execute(
            "SELECT candidate_id FROM evaluations LIMIT 1").fetchone()
        return row[0] if row else None

    def test_queue_is_prioritized_within_limit_and_marked_done(self):
        self._run(max_eval=1)
        cid = self._evaluated_cid()
        self.assertIsNotNone(cid)
        others = [r[0] for r in self.store.conn.execute(
            "SELECT id FROM candidates WHERE id != ?", (cid,))]
        self.assertEqual(len(others), 2)
        # 評価済み候補は通常なら14日ゲートで選ばれない。予約で選定させる
        self.store.enqueue_reeval("uuid-q1", cid, "監査レビューでの予約")
        res = self._run(max_eval=1)
        self.assertEqual(res["counts"].get("reeval_queue"), 1)
        self.assertEqual(res["counts"]["selected"], 1)  # 上限の内数 (別枠にしない)
        self.assertEqual(self._eval_count(cid), 2)      # 予約候補が再評価された
        for o in others:
            self.assertEqual(self._eval_count(o), 0)    # 新規候補は今回選ばれていない
        self.assertEqual(self.store.pending_reeval(), [])  # 消化済み
        # 消化後は通常のゲートに戻る: 次の実行では新規候補が選ばれる
        res = self._run(max_eval=1)
        self.assertNotIn("reeval_queue", res["counts"])
        self.assertEqual(self._eval_count(cid), 2)
        self.assertEqual(sum(self._eval_count(o) for o in others), 1)

    def test_queue_stays_pending_without_llm(self):
        self._run(max_eval=1)
        cid = self._evaluated_cid()
        self.store.enqueue_reeval("uuid-q1", cid, "監査レビューでの予約")
        self._run(max_eval=1, llm=None)
        self.assertEqual(self._eval_count(cid), 1)  # 評価は増えていない
        self.assertEqual(len(self.store.pending_reeval()), 1)  # 予約は残る

    def test_pipeline_imports_inbox_file_at_start(self):
        self._run(max_eval=1)
        cid = self._evaluated_cid()
        root = Path(self.tmp.name) / "repo"
        root.mkdir()
        (root / "review_inbox.json").write_text(json.dumps({
            "version": 1, "decisions": [], "adjustments": [],
            "reeval_queue": [{"id": "uuid-q1", "candidate_id": cid}]}), encoding="utf-8")
        with mock.patch.object(pipeline_mod, "ROOT", root):
            res = self._run(max_eval=1)
        self.assertEqual(res["counts"].get("review_import"), {"reeval": 1})
        self.assertEqual(self._eval_count(cid), 2)
        self.assertEqual(self.store.pending_reeval(), [])

    def test_pipeline_survives_broken_inbox(self):
        root = Path(self.tmp.name) / "repo"
        root.mkdir()
        (root / "review_inbox.json").write_text("{壊れた JSON", encoding="utf-8")
        logs = []
        with mock.patch.object(pipeline_mod, "ROOT", root):
            res = T.run_pipeline(
                self.store, self.cfg, self.llm, as_of=AS_OF, sources=("arxiv",),
                kinds=("long",), max_eval=1, fetchers=fetch_n(3), today=AS_OF,
                log=logs.append)
        self.assertFalse(res["failed"])
        self.assertNotIn("review_import", res["counts"])
        self.assertTrue(any("review_inbox" in str(x) for x in logs))


class StageBWiringTests(unittest.TestCase):
    """ページ側の配線: 詳細ページの併記表示と、監査ページの取り消しの形。"""

    def test_detail_page_shows_adjustment_with_original(self):
        html = (T.ROOT / "web" / "detail.html").read_text(encoding="utf-8")
        self.assertIn("LL.adjustment", html)
        self.assertIn("読み替えています", html)
        self.assertIn("元の総合点は", html)

    def test_audit_page_cancel_is_a_record_not_a_delete(self):
        js = (T.ROOT / "web" / "audit.js").read_text(encoding="utf-8")
        self.assertIn("activeAdj", js)
        self.assertIn("rec.to_value = rec.from_value", js)
        self.assertNotIn("delete review.adjustments[clrBtn.dataset.adjclear]", js)


if __name__ == "__main__":
    unittest.main()
