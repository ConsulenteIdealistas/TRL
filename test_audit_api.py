"""API 照合 (trlcore/audit_api.py) のテスト。

実行: python -m unittest test_audit_api -v
test_trl.py から分離した。共通フィクスチャは testutil を参照する。
"""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import trl as T
from trl import *  # noqa
from testutil import CFG, LONG_KEYS, ev, item, raw

from trlcore import audit_api as API


class AuditApiTests(unittest.TestCase):
    """API 照合層 (trlcore/audit_api) の単体テスト。ネットワークは
    fetch 引数の差し替えでモックし、実 API には触れない。"""

    NCT = "NCT00000001"
    CT_URL = f"https://clinicaltrials.gov/study/{NCT}"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "trl.sqlite")
        self.n = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _mk_eval(self, title, entity_id="idorsia", url=None, doi=None,
                 items=None, extraction=None):
        self.n += 1
        sid = f"2610.300{self.n:02d}"
        rec, problems = normalize(
            raw(source="arxiv", sid=sid, title=title, doi=doi,
                url=url or f"https://example.org/{sid}"),
            "2026-10-20T00:00:00+00:00")
        self.assertEqual(problems, [])
        cid, _ = self.store.upsert_candidate(rec, "r0")
        self.store.set_entities(cid, [{"entity_id": entity_id, "strength": "strong",
                                       "matched_by": "title", "matched_alias": "x"}])
        full = {k: item(70) for k in LONG_KEYS}
        for k, v in (items or {}).items():
            full[k] = v
        ev = {"kind": "long", "as_of": "2026-10-20", "status": "final",
              "total": 70.0, "band": {"label": "strong"}, "items": full,
              "extraction": extraction if extraction is not None else
              {"facts": [{"statement": "事実", "source": "https://example.org/x"}],
               "inferences": [], "forecasts": [], "hypotheses": [],
               "counter_points": [], "missing_info": []},
              "event": None, "prompt_versions": {},
              "config_version": CFG["config_version"], "config_hash": CFG["config_hash"],
              "llm_provider": "gemini", "llm_model": "gemini-3.5-flash-lite",
              "input_hash": f"h-{sid}", "errors": [],
              "extra": {"trl_level": {"level": 8, "reason": "実証段階",
                                       "evidence": [], "missing_info": []}}}
        self.store.add_evaluation("r0", cid, ev)
        return cid, self.store.latest_evaluation(cid, "long")["id"]

    @staticmethod
    def _study(status="RECRUITING", sponsor="Idorsia Pharmaceuticals Ltd."):
        return {"protocolSection": {
            "identificationModule": {"nctId": AuditApiTests.NCT,
                                     "briefTitle": "Study"},
            "statusModule": {"overallStatus": status,
                             "lastUpdatePostDateStruct": {"date": "2026-09-01"}},
            "sponsorCollaboratorsModule": {
                "leadSponsor": {"name": sponsor, "class": "INDUSTRY"}},
            "designModule": {"enrollmentInfo": {"count": 120, "type": "ACTUAL"}}}}

    @staticmethod
    def _fetch(mapping):
        def fetch(url):
            for key, val in mapping.items():
                if key in url:
                    if isinstance(val, Exception):
                        raise val
                    return val
            raise API.FetchFailed(f"unexpected url: {url}")
        return fetch

    def _run(self, fetch):
        return T.run_audit_api(self.store, CFG, limit=10, log=lambda *_: None,
                               fetch=fetch, sleep=lambda *_: None)

    def _entries(self, batch):
        rows = self.store.audit_results(batch)
        return rows, [f for r in rows for f in r["flags"]]

    def test_ct1_flag_terminated_unrecorded(self):
        self._mk_eval("中止が未記録の治験", url=self.CT_URL)
        res = self._run(self._fetch({self.NCT: (200, self._study(status="TERMINATED"))}))
        self.assertEqual((res["audited"], res["flags"]), (1, 1))
        rows, entries = self._entries(res["batch"])
        self.assertEqual(rows[0]["auditor_provider"], "api")
        self.assertEqual(rows[0]["auditor_model"], "api/v1")
        e = entries[0]
        self.assertEqual((e["check"], e["verdict"], e["rule"]),
                         ("CT1", "flag", "status:TERMINATED"))
        self.assertEqual(e["source_api"], "clinicaltrials.gov")
        self.assertEqual(e["snapshot"]["status"], "TERMINATED")
        self.assertEqual(e["snapshot"]["sponsor"], "Idorsia Pharmaceuticals Ltd.")

    def test_ct1_no_flag_when_recorded(self):
        ext = {"facts": [], "inferences": [], "forecasts": [], "hypotheses": [],
               "counter_points": ["研究が中止（TERMINATED）されているため、"
                                  "十分な評価が得られていない"],
               "missing_info": []}
        self._mk_eval("中止が記録済みの治験", url=self.CT_URL, extraction=ext)
        res = self._run(self._fetch({self.NCT: (200, self._study(status="TERMINATED"))}))
        _, entries = self._entries(res["batch"])
        self.assertEqual(entries, [])

    def test_ct1_no_flag_when_active(self):
        self._mk_eval("継続中の治験", url=self.CT_URL)
        res = self._run(self._fetch({self.NCT: (200, self._study(status="RECRUITING"))}))
        _, entries = self._entries(res["batch"])
        self.assertEqual(entries, [])

    def test_ct1_flag_on_404(self):
        self._mk_eval("実在しない治験", url=self.CT_URL)
        res = self._run(self._fetch({self.NCT: (404, None)}))
        _, entries = self._entries(res["batch"])
        self.assertEqual([(e["check"], e["verdict"], e["rule"]) for e in entries],
                         [("CT1", "flag", "ctgov-not-found")])

    def test_ct2_gray_on_sponsor_mismatch(self):
        self._mk_eval("スポンサー不一致", url=self.CT_URL)
        res = self._run(self._fetch({self.NCT: (200, self._study(
            status="COMPLETED", sponsor="University of Oxford"))}))
        _, entries = self._entries(res["batch"])
        self.assertEqual([(e["check"], e["verdict"]) for e in entries],
                         [("CT2", "gray")])

    def test_ct2_no_gray_on_name_variant(self):
        # 設定上の企業名は "Idorsia Ltd"、スポンサーは法人格つきの表記ゆれ
        self._mk_eval("スポンサー表記ゆれ", url=self.CT_URL)
        res = self._run(self._fetch({self.NCT: (200, self._study(
            status="COMPLETED", sponsor="Idorsia Pharmaceuticals Ltd."))}))
        _, entries = self._entries(res["batch"])
        self.assertEqual(entries, [])

    def test_cr1_flag_on_404(self):
        # Crossref 404 かつ DOI ハンドルでも未登録 (responseCode=100)
        self._mk_eval("実在しない DOI", doi="10.9999/fake.1")
        res = self._run(self._fetch(
            {"api.crossref.org": (404, None),
             "api/handles": (200, {"responseCode": 100})}))
        _, entries = self._entries(res["batch"])
        self.assertEqual([(e["check"], e["verdict"], e["item"]) for e in entries],
                         [("CR1", "flag", "candidate")])
        self.assertEqual(entries[0]["source_api"], "crossref")
        self.assertEqual(entries[0]["snapshot"]["handle_response_code"], 100)

    def test_cr1_no_flag_when_registered_elsewhere(self):
        # Crossref には無いが DOI ハンドルにはある = DataCite 等の
        # 他レジストリ登録分 (PANGAEA 型) は実在として扱う
        self._mk_eval("他レジストリの DOI", doi="10.1594/PANGAEA.989871")
        res = self._run(self._fetch(
            {"api.crossref.org": (404, None),
             "api/handles": (200, {"responseCode": 1})}))
        _, entries = self._entries(res["batch"])
        self.assertEqual(entries, [])

    def test_cr1_no_flag_on_200(self):
        self._mk_eval("実在する DOI", doi="10.1000/real.1")
        res = self._run(self._fetch(
            {"10.1000": (200, {"message": {"DOI": "10.1000/real.1"}})}))
        _, entries = self._entries(res["batch"])
        self.assertEqual(entries, [])
        self.assertEqual(res["doi_refs"], 1)

    def test_cr1_item_attribution_from_evidence(self):
        it = item(70)
        it["evidence"] = [{"ref": "https://doi.org/10.9999/fake.2",
                           "summary": "要約", "kind": "fact"}]
        self._mk_eval("根拠に DOI", items={"technology_readiness": it})
        res = self._run(self._fetch(
            {"api.crossref.org": (404, None),
             "api/handles": (200, {"responseCode": 100})}))
        _, entries = self._entries(res["batch"])
        self.assertEqual([(e["check"], e["item"]) for e in entries],
                         [("CR1", "technology_readiness")])

    def test_ct2_no_gray_on_concatenated_variant(self):
        # スポンサー表記 "ModernaTX" (連結形) と企業名 "Moderna, Inc." は
        # トークンの接頭辞一致で同一視する
        self._mk_eval("スポンサー連結形", entity_id="moderna", url=self.CT_URL)
        res = self._run(self._fetch({self.NCT: (200, self._study(
            status="COMPLETED", sponsor="ModernaTX, Inc."))}))
        _, entries = self._entries(res["batch"])
        self.assertEqual(entries, [])

    def test_fetch_failure_is_skipped_and_retried(self):
        self._mk_eval("通信失敗", url=self.CT_URL)
        res = self._run(self._fetch({self.NCT: API.FetchFailed("boom")}))
        self.assertEqual((res["audited"], res["skipped"]), (0, 1))
        n = self.store.conn.execute(
            "SELECT COUNT(*) FROM audit_results WHERE auditor_provider='api'"
        ).fetchone()[0]
        self.assertEqual(n, 0)  # 未実施は記録しない
        res2 = self._run(self._fetch({self.NCT: (200, self._study())}))
        self.assertEqual(res2["audited"], 1)  # 次回に再試行される

    def test_freshness_window(self):
        self._mk_eval("鮮度", url=self.CT_URL)
        fetch = self._fetch({self.NCT: (200, self._study())})
        self.assertEqual(self._run(fetch)["audited"], 1)
        self.assertEqual(self._run(fetch)["audited"], 0)  # 30日以内は対象外
        old = (datetime.now(timezone.utc) - timedelta(days=31)) \
            .isoformat(timespec="seconds")
        self.store.conn.execute(
            "UPDATE audit_results SET created_at = ? WHERE auditor_provider='api'",
            (old,))
        self.store.conn.commit()
        self.assertEqual(self._run(fetch)["audited"], 1)  # 30日超で再照合
