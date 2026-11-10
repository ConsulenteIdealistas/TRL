"""監査 (trlcore/audit.py・監査規則・audit_positive) のテスト。

実行: python -m unittest test_audit -v
test_trl.py から分離した。共通フィクスチャは testutil を参照する。
"""
import copy
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import trl as T
from trl import *  # noqa
from testutil import AS_OF, CFG, EXTRACTION, LONG_KEYS, ev, item, raw, trials


class AuditFixTests(unittest.TestCase):
    def test_yahoo_quarters_are_distinct_candidates(self):
        # Yahoo の URL は銘柄ページ(四半期で不変)。キーに使うと四半期レコードが
        # 初回分へ統合され新しい財務数値が取り込まれないため、source_id をキーにする
        kw = dict(url="https://finance.yahoo.co.jp/quote/TEST", info_type="disclosure")
        q1 = raw(source="yahoo", sid="TEST-2026-06-30", title="TEST 財務情報(2026-06-30 四半期)",
                 published="2026-06-30", abstract="TESTの財務数値: 売上高 100", **kw)
        q2 = raw(source="yahoo", sid="TEST-2026-09-30", title="TEST 財務情報(2026-09-30 四半期)",
                 published="2026-09-30", abstract="TESTの財務数値: 売上高 200", **kw)
        r1, _ = normalize(q1, "2026-10-29T00:00:00+00:00")
        r2, _ = normalize(q2, "2026-10-29T00:00:00+00:00")
        self.assertEqual(r1["keys"][0], "srcid:yahoo:TEST-2026-06-30")
        self.assertNotEqual(r1["keys"][0], r2["keys"][0])
        with tempfile.TemporaryDirectory() as td:
            store = T.Store(Path(td) / "d.sqlite")
            c1, n1 = store.upsert_candidate(r1, "run1")
            c2, n2 = store.upsert_candidate(r2, "run1")
            self.assertTrue(n1 and n2)
            self.assertNotEqual(c1, c2)
            self.assertIn("200", store.get_candidate(c2)["abstract"])
            store.close()

    def test_edgar_quarters_are_distinct_candidates(self):
        # EDGAR の URL は企業ページ (四半期で不変)。Yahoo と同じく source_id をキーに
        # しないと、新しい四半期の財務が初回分へ統合されて永久に取り込まれない
        kw = dict(url="https://www.sec.gov/edgar/browse/?CIK=1819994", info_type="disclosure")
        q1 = raw(source="edgar", sid="RKLB-2026-06-30", title="RKLB 財務情報(2026-06-30 四半期)",
                 published="2026-06-30", abstract="RKLBの財務数値(SEC EDGAR): 売上高 100", **kw)
        q2 = raw(source="edgar", sid="RKLB-2026-09-30", title="RKLB 財務情報(2026-09-30 四半期)",
                 published="2026-09-30", abstract="RKLBの財務数値(SEC EDGAR): 売上高 200", **kw)
        r1, _ = normalize(q1, "2026-10-29T00:00:00+00:00")
        r2, _ = normalize(q2, "2026-10-29T00:00:00+00:00")
        self.assertEqual(r1["keys"][0], "srcid:edgar:RKLB-2026-06-30")
        self.assertNotEqual(r1["keys"][0], r2["keys"][0])
        with tempfile.TemporaryDirectory() as td:
            store = T.Store(Path(td) / "d.sqlite")
            c1, n1 = store.upsert_candidate(r1, "run1")
            c2, n2 = store.upsert_candidate(r2, "run1")
            self.assertTrue(n1 and n2)
            self.assertNotEqual(c1, c2)
            store.close()

class AuditFix2Tests(unittest.TestCase):
    """2026-10-29 監査の低優先修正の固定。"""

    def test_input_hash_stable_within_freshness_bucket(self):
        cand = {"title": "T", "abstract": "A", "url": "https://x", "published": "2026-10-01"}
        h1 = T.input_hash(cand, date(2026, 10, 20))
        h2 = T.input_hash(cand, date(2026, 10, 25))  # 同じ fresh_30d バケット
        h3 = T.input_hash(cand, date(2027, 1, 1))    # older バケット
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)

    def test_response_shape_error_is_infra_failure(self):
        from trlcore.errors import ResponseShapeError
        cand = {"id": "c_x", "title": "T", "abstract": "A", "url": "https://x",
                "source": "arxiv", "published": "2026-10-10", "fields": []}

        def fn(_p):
            raise ResponseShapeError("unexpected Gemini response shape")
        res = ev.evaluate_one(cand, "long", CFG, Mock(fn), AS_OF)
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["infra_failure"], {"transient": True, "status": None})

    def test_retry_count_resets_on_success(self):
        with tempfile.TemporaryDirectory() as td:
            store = T.Store(Path(td) / "d.sqlite")
            rec, _ = normalize(raw(sid="2610.9701"), "2026-10-20T00:00:00+00:00")
            cid, _ = store.upsert_candidate(rec, "r0")
            store.set_status(cid, "error", retry_inc=True)
            store.set_status(cid, "error", retry_inc=True)
            self.assertEqual(store.get_candidate(cid)["retry_count"], 2)
            store.set_status(cid, "hold", retry_reset=True)
            self.assertEqual(store.get_candidate(cid)["retry_count"], 0)
            store.close()

    def test_tags_must_be_list_of_strings(self):
        base = {"entities": [{"id": "x_co", "name": "X Corp"}]}
        T.validate_entities(base)
        ok = {"entities": [{"id": "x_co", "name": "X Corp", "tags": ["photonics"]}]}
        T.validate_entities(ok)
        bad = {"entities": [{"id": "x_co", "name": "X Corp", "tags": "photonics"}]}
        with self.assertRaises(T.ConfigError):
            T.validate_entities(bad)

    def test_stage_validation(self):
        base = {"entities": [{"id": "x_co", "name": "X Corp", "stage": "demo"}]}
        T.validate_entities(base)
        bad = {"entities": [{"id": "x_co", "name": "X Corp", "stage": "growth"}]}
        with self.assertRaises(T.ConfigError):
            T.validate_entities(bad)

    def test_market_map_guard(self):
        from trlcore.config import _check_market_maps
        _check_market_maps("US", "x_co")
        with self.assertRaises(T.ConfigError):
            _check_market_maps("XX", "x_co")

    def test_edgar_operating_cf_ytd(self):
        import copy
        from trlcore.sources import edgar
        facts = {"entityName": "CF Corp", "facts": {"us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                {"start": "2026-04-01", "end": "2026-06-30", "val": 100_000_000, "form": "10-Q", "filed": "2026-08-01"},
                {"start": "2025-01-01", "end": "2025-12-31", "val": 400_000_000, "form": "10-K", "filed": "2026-03-01"}]}},
            "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
                {"start": "2026-01-01", "end": "2026-06-30", "val": -30_000_000, "form": "10-Q", "filed": "2026-08-01"},
                {"start": "2025-01-01", "end": "2025-12-31", "val": -120_000_000, "form": "10-K", "filed": "2026-03-01"}]}},
        }}}
        text, period, kind = edgar.parse_company_facts(facts, date(2026, 10, 29))
        self.assertIn("営業キャッシュフロー(期首累計) △30百万米ドル", text)
        self.assertIn("営業キャッシュフロー △120百万米ドル", text)  # 通期分も出す

    def test_trials_pagination(self):
        def study(nct, upd):
            return {"protocolSection": {
                "identificationModule": {"nctId": nct, "officialTitle": f"Study {nct}"},
                "descriptionModule": {"briefSummary": "summary"},
                "statusModule": {"overallStatus": "RECRUITING",
                                 "lastUpdatePostDateStruct": {"date": upd}},
                "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Astroscale"}}}}
        calls = []

        def fake_http(url, params=None, **kw):
            calls.append(dict(params or {}))
            if params.get("pageToken"):
                return json.dumps({"studies": [study("NCT002", "2026-10-15")]})
            return json.dumps({"studies": [study("NCT001", "2026-10-16")], "nextPageToken": "tok2"})
        out = trials.fetch("Astroscale", 20, 180, AS_OF, http=fake_http)
        self.assertEqual([r["source_id"] for r in out], ["NCT001", "NCT002"])
        self.assertEqual(calls[1].get("pageToken"), "tok2")

    def test_translate_redoes_legacy_translation_without_source(self):
        # 出典記録の無い古い訳 (translation_src が空) は、訳文があっても訳し直して出典を付ける
        import os
        from unittest import mock as um
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")
        rec, _ = T.normalize(raw(), T.now_iso())
        cid, _ = store.upsert_candidate(rec, "rX")
        store.conn.execute(
            "INSERT INTO evaluations(run_id, candidate_id, kind, status) VALUES('rX', ?, 'long', 'ok')", (cid,))
        store.conn.commit()
        store.set_translation(cid, "出典なしの旧訳タイトル", "出典なしの旧訳抄録")  # by を渡さない = 記録なし
        self.assertIsNone(store.get_candidate(cid)["translation_src"])

        def g_http(url, params=None, headers=None, data=None, **kw):
            qs = json.loads(data)["q"]
            return json.dumps({"data": {"translations": [{"translatedText": "新訳:" + q} for q in qs]}})
        with um.patch.dict(os.environ, {"GOOGLE_TRANSLATE_API_KEY": "gk"}, clear=False):
            n, _stats = T.translate_display_set(store, CFG, AS_OF, log=lambda *_: None, http=g_http)
        self.assertEqual(n, 1)
        c = store.get_candidate(cid)
        self.assertTrue(c["title_ja"].startswith("新訳:"))
        self.assertEqual(json.loads(c["translation_src"]), {"title": "google", "abstract": "google"})
        store.close()

    def test_translate_sends_only_wanted_fields(self):
        import os
        from unittest import mock as um
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")
        rec, _ = T.normalize(raw(), T.now_iso())
        cid, _ = store.upsert_candidate(rec, "rX")
        store.conn.execute(
            "INSERT INTO evaluations(run_id, candidate_id, kind, status) VALUES('rX', ?, 'long', 'ok')", (cid,))
        store.conn.commit()
        store.set_translation(cid, "既訳タイトル", None, title_by="google")  # 出典つきで訳済み → 抄録だけ送る
        sent = []

        def g_http(url, params=None, headers=None, data=None, **kw):
            qs = json.loads(data)["q"]
            sent.extend(qs)
            return json.dumps({"data": {"translations": [{"translatedText": "訳:" + q} for q in qs]}})
        with um.patch.dict(os.environ, {"GOOGLE_TRANSLATE_API_KEY": "gk"}, clear=False):
            n, stats = T.translate_display_set(store, CFG, AS_OF, log=lambda *_: None, http=g_http)
        self.assertEqual(n, 1)
        self.assertEqual(sent, [rec["abstract"]])
        self.assertEqual(stats, {"google": len(rec["abstract"])})
        store.close()

class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "trl.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _mk_eval(self, title="監査対象の論文", trl_level=9, status="final", run_id="r0"):
        rec, problems = normalize(raw(title=title), "2026-10-20T00:00:00+00:00")
        self.assertEqual(problems, [])
        cid, _ = self.store.upsert_candidate(rec, run_id)
        self.store.set_entities(cid, [{"entity_id": "astroscale", "strength": "strong",
                                       "matched_by": "title", "matched_alias": "Astroscale"}])
        ev = {"kind": "long", "as_of": "2026-10-20", "status": status,
              "total": 70.0, "band": {"label": "strong"},
              "items": {k: item(70) for k in LONG_KEYS},
              "extraction": EXTRACTION, "event": None, "prompt_versions": {},
              "config_version": CFG["config_version"], "config_hash": CFG["config_hash"],
              "llm_provider": "gemini", "llm_model": "gemini-3.5-flash-lite",
              "input_hash": f"h-{title}-{run_id}", "errors": [],
              "extra": {"trl_level": {"level": trl_level, "reason": "運用実績あり",
                                       "evidence": [], "missing_info": []}}}
        self.store.add_evaluation(run_id, cid, ev)
        return cid, self.store.latest_evaluation(cid, "long")["id"]

    def test_config_hash_ignores_audit_prompt(self):
        # 監査プロンプトは scoring の prompts に登録されず、評価の世代印
        # (config_hash) は audit.v1.md の有無・内容で一切変わらない
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for name in ("scoring.json", "sources.json", "extract.v1.md",
                         "long.v1.md", "event.v1.md"):
                (d / name).write_bytes((T.ROOT / name).read_bytes())
            h1 = load_config(d)["config_hash"]  # audit.v1.md が無くても読める
            (d / "audit.v1.md").write_text("# 監査側ファイルのダミー\n", encoding="utf-8")
            self.assertEqual(load_config(d)["config_hash"], h1)  # 置いても不変
            (d / "audit.v1.md").write_text("# 監査側ファイルのダミー\n追記\n", encoding="utf-8")
            self.assertEqual(load_config(d)["config_hash"], h1)  # 本文の改訂でも不変
            (d / "long.v1.md").write_text(
                (d / "long.v1.md").read_text(encoding="utf-8") + "\n追記\n", encoding="utf-8")
            self.assertNotEqual(load_config(d)["config_hash"], h1)  # 評価側は従来どおり変化

    def test_build_audit_input(self):
        cid, eid = self._mk_eval()
        row = dict(self.store.conn.execute(
            "SELECT * FROM evaluations WHERE id = ?", (eid,)).fetchone())
        inp = T.build_audit_input(self.store, row, CFG)
        self.assertEqual(inp["title"], "監査対象の論文")
        self.assertEqual(inp["sector"], "space")
        self.assertEqual(inp["companies"][0]["name"], "Astroscale Holdings Inc.")
        self.assertEqual(inp["items"]["technology_readiness"]["score"], 70)
        self.assertEqual(inp["trl_level"]["level"], 9)
        self.assertEqual(inp["status"], "final")
        self.assertEqual(inp["critical_missing"], [])

    def test_select_unaudited(self):
        _, eid1 = self._mk_eval(title="論文1")
        _, eid2 = self._mk_eval(title="論文2")
        sha = "sha-v1"  # 冪等キーの一部としての任意の版ハッシュ (規則側も同じ仕組みを使う)
        self.assertEqual({r["id"] for r in T.select_unaudited(self.store, sha, "m", 10)},
                         {eid1, eid2})
        self.store.add_audit_result({"evaluation_id": eid1, "candidate_id": "c",
                                     "run_id": "aud-x", "auditor_model": "m",
                                     "prompt_sha": sha, "flags": [], "summary": ""})
        # 同じ sha+model では監査済みは外れる。sha かモデルが違えば再び対象になる
        self.assertEqual([r["id"] for r in T.select_unaudited(self.store, sha, "m", 10)], [eid2])
        self.assertEqual({r["id"] for r in T.select_unaudited(self.store, "othersha", "m", 10)},
                         {eid1, eid2})
        self.assertEqual({r["id"] for r in T.select_unaudited(self.store, sha, "other", 10)},
                         {eid1, eid2})

    def test_add_audit_result_idempotent(self):
        _, eid = self._mk_eval()
        row = {"evaluation_id": eid, "candidate_id": "c", "run_id": "aud-y",
               "auditor_provider": "gemini", "auditor_model": "m", "prompt_version": "v1",
               "prompt_sha": "sha1", "flags": [], "summary": "s"}
        self.assertTrue(self.store.add_audit_result(row))
        self.assertFalse(self.store.add_audit_result(row))  # 同じ冪等キーは二重記録しない
        self.assertTrue(self.store.add_audit_result(dict(row, prompt_sha="sha2")))
        self.assertEqual(len(self.store.audit_results("aud-y")), 2)

    def test_audit_report_smoke(self):
        cid, eid = self._mk_eval()
        self.store.add_audit_result(
            {"evaluation_id": eid, "candidate_id": cid, "run_id": "aud-z",
             "auditor_provider": "gemini", "auditor_model": "gemini-3.6-flash",
             "prompt_version": "v1", "prompt_sha": "sha",
             "flags": [{"check": "A5", "item": "monetization", "detail": "点と理由が食い違う"}],
             "summary": "整合に疑い"})
        text = T.build_audit_report(self.store, "aud-z")
        self.assertIn("# 監査レポート aud-z", text)
        self.assertIn("監査対象の論文", text)
        self.assertIn("A5", text)
        self.assertIn("点と理由が食い違う", text)

class AuditRulesTests(unittest.TestCase):
    """規則エンジン (既定の監査) の単体テスト。実レコードの形は AuditTests と同じ流儀。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "trl.sqlite")
        self.rules = T.load_rules()["rules"]
        self.n = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _item(self, score=70, reason="理由", evidence=None, missing=None):
        return {"score": score, "reason": reason,
                "evidence": evidence if evidence is not None else
                [{"ref": "https://example.org/x", "summary": "要約", "kind": "fact"}],
                "counter_evidence": [], "missing_info": missing or []}

    def _mk_eval(self, title, entity_id="astroscale", items=None, trl=None,
                 extraction=None):
        self.n += 1
        sid = f"2610.200{self.n:02d}"
        rec, problems = normalize(raw(source="arxiv", sid=sid, title=title),
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
              "extraction": extraction if extraction is not None else EXTRACTION,
              "event": None, "prompt_versions": {},
              "config_version": CFG["config_version"], "config_hash": CFG["config_hash"],
              "llm_provider": "gemini", "llm_model": "gemini-3.5-flash-lite",
              "input_hash": f"h-{sid}", "errors": [],
              "extra": {"trl_level": trl if trl is not None else
                        {"level": 8, "reason": "実証段階", "evidence": [], "missing_info": []}}}
        self.store.add_evaluation("r0", cid, ev)
        return cid, self.store.latest_evaluation(cid, "long")["id"]

    def _audit_one(self, eid):
        row = dict(self.store.conn.execute(
            "SELECT * FROM evaluations WHERE id = ?", (eid,)).fetchone())
        inp = T.build_audit_input(self.store, row, CFG)
        return T.audit_evaluation_rules(inp, self.rules)

    @staticmethod
    def _only(entries, check):
        return [e for e in entries if e["check"] == check]

    def test_load_rules(self):
        r = T.load_rules()
        self.assertEqual(r["version"], "v4.1")
        self.assertEqual(len(r["sha"]), 12)
        self.assertEqual(r["sha"], T.load_rules()["sha"])  # 同じファイルでは安定
        # A5 は規則化しない (対象外)。それ以外とポジティブ監査 P1〜P6 が載っている
        self.assertEqual(set(r["rules"]["checks"]),
                         {"A1", "A2", "A3", "A4", "A6", "A7", "A8", "A9",
                          "A10", "A11", "A12", "A13", "A14", "A15",
                          "E1", "E2",
                          "P1", "P2", "P3", "P4", "P5", "P6"})

    def test_a1_flag(self):
        _, eid = self._mk_eval("A1フラグ", items={
            "technology_readiness": self._item(
                reason="複数のミッションで継続的に使用されている")})
        a1 = self._only(self._audit_one(eid), "A1")
        self.assertEqual(len(a1), 1)
        self.assertEqual(a1[0]["verdict"], "flag")
        self.assertEqual(a1[0]["item"], "technology_readiness")
        self.assertIn("rule", a1[0])

    def test_a1_white_rescue(self):
        ext = {"facts": [{"statement": "過去3回の打上げで運用し、2年間継続した",
                          "source": "u"}],
               "inferences": [], "forecasts": [], "hypotheses": [],
               "counter_points": [], "missing_info": []}
        _, eid = self._mk_eval("A1救済", extraction=ext, items={
            "technology_readiness": self._item(
                reason="複数のミッションで継続的に使用されている")})
        # 根拠側に回数・期間の明記があるため、フラグにもグレーにもならない
        self.assertEqual(self._only(self._audit_one(eid), "A1"), [])

    def test_a1_gray_for_generic_claim(self):
        _, eid = self._mk_eval("A1グレー", items={
            "technology_readiness": self._item(
                reason="複数のコホートで評価が進んでいる")})
        a1 = self._only(self._audit_one(eid), "A1")
        self.assertEqual(len(a1), 1)
        self.assertEqual(a1[0]["verdict"], "gray")  # 治験デザイン語は確定違反にしない

    def test_a2_flag_pass_and_out_of_scope(self):
        trl9 = {"level": 9, "reason": "実用化済み", "evidence": [], "missing_info": []}
        _, eid = self._mk_eval("A2フラグ", trl=trl9)
        a2 = self._only(self._audit_one(eid), "A2")
        self.assertEqual(len(a2), 1)
        self.assertEqual(a2[0]["verdict"], "flag")

        ext = {"facts": [{"statement": "複数ミッション成功の実績がある", "source": "u"}],
               "inferences": [], "forecasts": [], "hypotheses": [],
               "counter_points": [], "missing_info": []}
        _, eid2 = self._mk_eval("A2通過", trl=trl9, extraction=ext)
        self.assertEqual(self._only(self._audit_one(eid2), "A2"), [])

        _, eid3 = self._mk_eval("A2対象外", trl={"level": 8, "reason": "実証段階",
                                                "evidence": [], "missing_info": []})
        self.assertEqual(self._only(self._audit_one(eid3), "A2"), [])

    def test_a4_recorded_as_gray(self):
        _, eid = self._mk_eval("A4グレー", items={
            "technology_readiness": self._item(
                reason="承認を取得し販売を開始した",
                evidence=[{"ref": "https://example.org/x", "summary": "売上が伸びている",
                           "kind": "fact"}])})
        a4 = self._only(self._audit_one(eid), "A4")
        self.assertEqual(len(a4), 1)
        self.assertEqual(a4[0]["verdict"], "gray")  # 初版はフラグにしない (mode=gray)

        _, eid2 = self._mk_eval("A4救済", items={
            "technology_readiness": self._item(
                reason="臨床試験で有効性を確認し承認を取得した",
                evidence=[{"ref": "https://example.org/x", "summary": "症例データあり",
                           "kind": "fact"}])})
        self.assertEqual(self._only(self._audit_one(eid2), "A4"), [])

    def test_a6_flag(self):
        _, eid = self._mk_eval("A6フラグ", items={
            "monetization": self._item(missing=["株価の推移が不明"])})
        a6 = self._only(self._audit_one(eid), "A6")
        self.assertEqual(len(a6), 1)
        self.assertEqual(a6[0]["verdict"], "flag")
        self.assertEqual(a6[0]["item"], "monetization")

    def test_a7_flag_gray_and_none(self):
        _, eid = self._mk_eval("A7フラグ", items={
            "moat_durability": self._item(score=30, reason="根拠が乏しい")})
        a7 = self._only(self._audit_one(eid), "A7")
        self.assertEqual(len(a7), 1)
        self.assertEqual(a7[0]["verdict"], "flag")

        # 本文に具体的な名指し (特許・期間) があれば確定形にはならずグレー
        _, eid2 = self._mk_eval("A7名指し", items={
            "moat_durability": self._item(
                score=30, reason="根拠が乏しいが、特許の残存期間には言及がある")})
        a7b = self._only(self._audit_one(eid2), "A7")
        self.assertEqual(len(a7b), 1)
        self.assertEqual(a7b[0]["verdict"], "gray")

        # missing_info があれば確定形にはならずグレー
        _, eid3 = self._mk_eval("A7不足あり", items={
            "moat_durability": self._item(score=30, reason="証拠が弱い",
                                          missing=["追加の比較データ"])})
        a7c = self._only(self._audit_one(eid3), "A7")
        self.assertEqual(len(a7c), 1)
        self.assertEqual(a7c[0]["verdict"], "gray")

        # 漠然フレーズが無ければ低得点でも何も付かない
        _, eid4 = self._mk_eval("A7なし", items={
            "moat_durability": self._item(score=30, reason="競合との差別化が弱い")})
        self.assertEqual(self._only(self._audit_one(eid4), "A7"), [])

    def _inp(self, **over):
        """監査要項チェック (A8〜A14) 用の手組み入力。既存チェックに
        当たらない素直な形を既定にし、テストごとに差分だけ上書きする。"""
        base = {"status": "final", "total": 70.0, "band": "strong",
                "sector": "space", "critical_missing": [], "trl_level": None,
                "items": {k: self._item() for k in LONG_KEYS},
                "extraction": {"counter_points": ["反証あり"], "facts": []},
                "published": "2026-10-01", "as_of": "2026-10-20",
                "expected_total": 70.0, "expected_band": "strong",
                "companies": [{"name": "X", "sector": "space",
                               "strength": "strong"}]}
        base.update(over)
        return base

    def test_a8_flag_and_rescue(self):
        inp = self._inp(extraction={"counter_points": [], "facts": []})
        for it in inp["items"].values():
            it["counter_evidence"] = []
        a8 = self._only(T.audit_evaluation_rules(inp, self.rules), "A8")
        self.assertEqual(len(a8), 1)
        self.assertEqual(a8[0]["verdict"], "flag")
        # 反証が抽出側にあれば拾わない。総合点が低ければ対象外
        inp2 = self._inp()
        self.assertEqual(self._only(T.audit_evaluation_rules(inp2, self.rules), "A8"), [])
        inp3 = self._inp(total=50.0, extraction={"counter_points": [], "facts": []})
        self.assertEqual(self._only(T.audit_evaluation_rules(inp3, self.rules), "A8"), [])

    def test_a9_flag(self):
        inp = self._inp()
        inp["items"]["monetization"] = self._item(score=85, evidence=[])
        a9 = self._only(T.audit_evaluation_rules(inp, self.rules), "A9")
        self.assertEqual(len(a9), 1)
        self.assertEqual(a9[0]["item"], "monetization")

    def test_a10_flag(self):
        inp = self._inp(published="2026-11-01")
        a10 = self._only(T.audit_evaluation_rules(inp, self.rules), "A10")
        self.assertEqual(len(a10), 1)
        inp2 = self._inp()
        self.assertEqual(self._only(T.audit_evaluation_rules(inp2, self.rules), "A10"), [])

    def test_a11_gray_and_rescue(self):
        inp = self._inp()
        inp["items"]["monetization"] = self._item(
            reason="売上は前年比35.5%増と好調",
            evidence=[{"ref": "https://example.org/x", "summary": "売上は増加した",
                       "kind": "fact"}])
        a11 = self._only(T.audit_evaluation_rules(inp, self.rules), "A11")
        self.assertEqual(len(a11), 1)
        self.assertEqual(a11[0]["verdict"], "gray")
        inp2 = self._inp()
        inp2["items"]["monetization"] = self._item(
            reason="売上は前年比35.5%増と好調",
            evidence=[{"ref": "https://example.org/x",
                       "summary": "売上は前年比35.5%増", "kind": "fact"}])
        self.assertEqual(self._only(T.audit_evaluation_rules(inp2, self.rules), "A11"), [])

    def test_a12_flag(self):
        inp = self._inp()
        inp["items"]["monetization"] = self._item(
            evidence=[{"summary": "出典なし", "kind": "fact"}])
        a12 = self._only(T.audit_evaluation_rules(inp, self.rules), "A12")
        self.assertEqual(len(a12), 1)
        self.assertEqual(a12[0]["verdict"], "flag")

    def test_a13_flag_and_match(self):
        inp = self._inp(expected_total=50.0)
        a13 = self._only(T.audit_evaluation_rules(inp, self.rules), "A13")
        self.assertEqual(len(a13), 1)
        inp2 = self._inp()
        self.assertEqual(self._only(T.audit_evaluation_rules(inp2, self.rules), "A13"), [])

    def test_a14_gray(self):
        inp = self._inp(companies=[{"name": "Y", "sector": "space",
                                     "strength": "weak"}])
        a14 = self._only(T.audit_evaluation_rules(inp, self.rules), "A14")
        self.assertEqual(len(a14), 1)
        self.assertEqual(a14[0]["verdict"], "gray")
        inp2 = self._inp()
        self.assertEqual(self._only(T.audit_evaluation_rules(inp2, self.rules), "A14"), [])

    def test_a15_cap_observation(self):
        trl9 = {"level": 9, "reason": "実用化済み", "evidence": [], "missing_info": []}
        inp = self._inp(total=85.0, trl_level=trl9)
        entries = T.audit_evaluation_rules(inp, self.rules)
        self.assertEqual(len(self._only(entries, "A2")), 1)  # 前提: A2 が確定
        a15 = self._only(entries, "A15")
        self.assertEqual(len(a15), 1)
        self.assertEqual(a15[0]["verdict"], "gray")
        # 上限以下なら A2 が確定しても上限観測は付かない
        inp2 = self._inp(total=70.0, trl_level=trl9)
        self.assertEqual(self._only(T.audit_evaluation_rules(inp2, self.rules), "A15"), [])

    def test_e1_flag(self):
        inp = self._inp(kind="event")
        inp["items"]["not_priced_in"] = self._item(score=70)
        inp["market_reaction"] = {"available": True, "z": 2.5, "reaction_pct": 12.3}
        e1 = self._only(T.audit_evaluation_rules(inp, self.rules), "E1")
        self.assertEqual(len(e1), 1)
        self.assertEqual(e1[0]["verdict"], "flag")
        inp["market_reaction"] = {"available": True, "z": 0.5, "reaction_pct": 1.0}
        self.assertEqual(self._only(T.audit_evaluation_rules(inp, self.rules), "E1"), [])

    def test_e2_gray(self):
        inp = self._inp(kind="event", status="hold")
        inp["items"]["not_priced_in"] = self._item(score=None)
        inp["market_reaction"] = {"available": True, "z": 1.2, "reaction_pct": 2.7}
        e2 = self._only(T.audit_evaluation_rules(inp, self.rules), "E2")
        self.assertEqual(len(e2), 1)
        self.assertEqual(e2[0]["verdict"], "gray")
        # 反応データが無い旧評価では静か
        inp["market_reaction"] = None
        self.assertEqual(self._only(T.audit_evaluation_rules(inp, self.rules), "E2"), [])

    def test_p1_flag_gray_plan_and_unknown_sector(self):
        trl8 = {"level": 8, "reason": "実証段階", "evidence": [], "missing_info": []}
        hit = [{"ref": "https://example.org/x", "kind": "fact",
                "summary": "複数ミッション成功を重ね、通算3回の打ち上げに成功している"}]
        # 確認語彙と同じ文に回数・期間の明記 → 要確認
        inp = self._inp(trl_level=trl8)
        inp["items"]["technology_readiness"] = self._item(evidence=hit)
        p1 = self._only(T.audit_evaluation_rules(inp, self.rules), "P1")
        self.assertEqual(len(p1), 1)
        self.assertEqual(p1[0]["verdict"], "flag")
        self.assertEqual(p1[0]["rule"], "confirm_by_sector:space")
        # 回数・期間が別の文にある → グレー
        inp = self._inp(trl_level=trl8)
        inp["items"]["technology_readiness"] = self._item(evidence=[
            {"ref": "https://example.org/x", "kind": "fact",
             "summary": "ミッション成功を収めた。"},
            {"ref": "https://example.org/x", "kind": "fact",
             "summary": "打ち上げは通算3回を記録した。"}])
        p1 = self._only(T.audit_evaluation_rules(inp, self.rules), "P1")
        self.assertEqual(len(p1), 1)
        self.assertEqual(p1[0]["verdict"], "gray")
        # 計画・予定の文に当たった語彙は数えない (第10条1)
        inp = self._inp(trl_level=trl8)
        inp["items"]["technology_readiness"] = self._item(evidence=[
            {"ref": "https://example.org/x", "kind": "fact",
             "summary": "ミッション成功を目指す計画で、3回の打ち上げを予定している"}])
        self.assertEqual(self._only(T.audit_evaluation_rules(inp, self.rules), "P1"), [])
        # セクター別の語彙が引けない場合は束ね判定をせず発火しない
        inp = self._inp(trl_level=trl8, sector="mining")
        inp["items"]["technology_readiness"] = self._item(evidence=hit)
        self.assertEqual(self._only(T.audit_evaluation_rules(inp, self.rules), "P1"), [])

    def test_p1_design_cap_and_priority(self):
        trl8 = {"level": 8, "reason": "実証段階", "evidence": [], "missing_info": []}
        # 試験デザイン・目的の文に確認語彙と回数が同居しても flag には
        # しない (判定保留に上限)。v4.1 の校正 (試験目的文へのヒット対策)
        inp = self._inp(trl_level=trl8)
        inp["items"]["technology_readiness"] = self._item(evidence=[
            {"ref": "https://example.org/x", "kind": "fact",
             "summary": "複数ミッション成功を評価する観察研究であり、通算3回を対象とする。"}])
        p1 = self._only(T.audit_evaluation_rules(inp, self.rules), "P1")
        self.assertEqual(len(p1), 1)
        self.assertEqual(p1[0]["verdict"], "gray")
        self.assertIn("試験デザイン・目的の記述のため判定保留", p1[0]["detail"])
        # デザインでない文に確認語彙と回数が同居すれば従来どおり flag
        inp = self._inp(trl_level=trl8)
        inp["items"]["technology_readiness"] = self._item(evidence=[
            {"ref": "https://example.org/x", "kind": "fact",
             "summary": "複数ミッション成功を重ね、通算3回の打ち上げに成功している。"}])
        p1 = self._only(T.audit_evaluation_rules(inp, self.rules), "P1")
        self.assertEqual(len(p1), 1)
        self.assertEqual(p1[0]["verdict"], "flag")
        # デザインのヒットが先にあっても、後ろの非デザインのヒットを
        # 優先して判定する (デザイン候補に引きずられない)
        inp = self._inp(trl_level=trl8)
        inp["items"]["technology_readiness"] = self._item(evidence=[
            {"ref": "https://example.org/x", "kind": "fact",
             "summary": "複数ミッション成功を評価する観察研究であり、通算3回を対象とする。"},
            {"ref": "https://example.org/x", "kind": "fact",
             "summary": "複数ミッション成功を重ね、通算5回の打ち上げに成功している。"}])
        p1 = self._only(T.audit_evaluation_rules(inp, self.rules), "P1")
        self.assertEqual(len(p1), 1)
        self.assertEqual(p1[0]["verdict"], "flag")
        self.assertIn("5回", p1[0]["detail"])

    def test_p2_gray_and_threshold(self):
        facts = [{"statement": f"事実{i}", "source": f"s{i % 2}"} for i in range(5)]
        inp = self._inp(status="hold",
                        extraction={"facts": facts, "counter_points": []})
        p2 = self._only(T.audit_evaluation_rules(inp, self.rules), "P2")
        self.assertEqual(len(p2), 1)
        self.assertEqual(p2[0]["verdict"], "gray")
        # 事実が閾値 (5件) 未満なら拾わない
        inp = self._inp(status="hold",
                        extraction={"facts": facts[:4], "counter_points": []})
        self.assertEqual(self._only(T.audit_evaluation_rules(inp, self.rules), "P2"), [])
        # 企業リンクが無い保留は『企業未特定』で設計どおりなので対象外
        inp = self._inp(status="hold", companies=[],
                        extraction={"facts": facts, "counter_points": []})
        self.assertEqual(self._only(T.audit_evaluation_rules(inp, self.rules), "P2"), [])

    def test_p2_pool_sources(self):
        # v4.1: 出典は facts の source と材料プールの出典識別子の和集合で数える。
        # facts が単一出典でも、プールに複数の出典があれば発火する
        facts = [{"statement": f"事実{i}", "source": "u1"} for i in range(5)]
        inp = self._inp(status="hold",
                        extraction={"facts": facts, "counter_points": []},
                        pool_sources=["媒体A", "yahoo"])
        p2 = self._only(T.audit_evaluation_rules(inp, self.rules), "P2")
        self.assertEqual(len(p2), 1)
        self.assertEqual(p2[0]["verdict"], "gray")
        self.assertIn("材料プール", p2[0]["detail"])
        self.assertIn("媒体A", p2[0]["detail"])
        # プールが無ければ facts の単一出典だけでは発火しない
        inp = self._inp(status="hold",
                        extraction={"facts": facts, "counter_points": []},
                        pool_sources=[])
        self.assertEqual(self._only(T.audit_evaluation_rules(inp, self.rules), "P2"), [])
        # pool_sources キー自体が無い旧い入力でも壊れない
        inp = self._inp(status="hold",
                        extraction={"facts": facts, "counter_points": []})
        self.assertEqual(self._only(T.audit_evaluation_rules(inp, self.rules), "P2"), [])

    def test_p3_flag_gray_and_rescue(self):
        def fact_ev(text):
            return {"ref": "https://example.org/x", "summary": text, "kind": "fact"}
        low = {"score": 30, "reason": "売上が伸び悩んでいる",
               "evidence": [fact_ev("売上は120億円だった"), fact_ev("受注は45件"),
                            fact_ev("出荷は300台")],
               "counter_evidence": [], "missing_info": []}
        inp = self._inp()
        inp["items"]["monetization"] = low
        p3 = self._only(T.audit_evaluation_rules(inp, self.rules), "P3")
        self.assertEqual(len(p3), 1)
        self.assertEqual(p3[0]["verdict"], "flag")
        self.assertEqual(p3[0]["item"], "monetization")
        # 数値を含む fact 根拠が2件ならグレー
        inp = self._inp()
        inp["items"]["monetization"] = dict(low, evidence=low["evidence"][:2])
        p3 = self._only(T.audit_evaluation_rules(inp, self.rules), "P3")
        self.assertEqual(len(p3), 1)
        self.assertEqual(p3[0]["verdict"], "gray")
        # 不足情報が記録されていれば低得点には説明があるので拾わない
        inp = self._inp()
        inp["items"]["monetization"] = dict(low, missing_info=["四半期の内訳"])
        self.assertEqual(self._only(T.audit_evaluation_rules(inp, self.rules), "P3"), [])

    def test_p4_flag(self):
        inp = self._inp(critical_missing=["monetization"])
        inp["items"]["monetization"] = {
            "score": None, "reason": "",
            "evidence": [{"ref": "https://example.org/x", "summary": "根拠あり",
                          "kind": "fact"}],
            "counter_evidence": [], "missing_info": []}
        p4 = self._only(T.audit_evaluation_rules(inp, self.rules), "P4")
        self.assertEqual(len(p4), 1)
        self.assertEqual(p4[0]["verdict"], "flag")
        # 不足情報が記録されていれば採点漏れではない
        inp["items"]["monetization"]["missing_info"] = ["決算の内訳"]
        self.assertEqual(self._only(T.audit_evaluation_rules(inp, self.rules), "P4"), [])

    def test_p5_gray(self):
        null_item = {"score": None, "reason": "", "evidence": [],
                     "counter_evidence": [], "missing_info": []}
        inp = self._inp(critical_missing=["monetization"])
        inp["items"]["monetization"] = null_item
        p5 = self._only(T.audit_evaluation_rules(inp, self.rules), "P5")
        self.assertEqual(len(p5), 1)
        self.assertEqual(p5[0]["verdict"], "gray")
        # strong が一意に定まらなければ対象外 (第5条2)
        inp = self._inp(critical_missing=["monetization"], companies=[
            {"name": "X", "sector": "space", "strength": "strong"},
            {"name": "Y", "sector": "space", "strength": "strong"}])
        inp["items"]["monetization"] = null_item
        self.assertEqual(self._only(T.audit_evaluation_rules(inp, self.rules), "P5"), [])

    def test_p6_flag_and_no_reaction(self):
        inp = self._inp(kind="event")
        inp["items"]["not_priced_in"] = self._item(score=30)
        inp["market_reaction"] = {"available": True, "z": 0.2, "reaction_pct": 0.4}
        p6 = self._only(T.audit_evaluation_rules(inp, self.rules), "P6")
        self.assertEqual(len(p6), 1)
        self.assertEqual(p6[0]["verdict"], "flag")
        # 反応データが無ければ判定材料が無いので拾わない
        inp["market_reaction"] = None
        self.assertEqual(self._only(T.audit_evaluation_rules(inp, self.rules), "P6"), [])
        inp["market_reaction"] = {"available": False, "z": None, "reaction_pct": None}
        self.assertEqual(self._only(T.audit_evaluation_rules(inp, self.rules), "P6"), [])

    def test_build_input_new_fields(self):
        _, eid = self._mk_eval("新規フィールド")
        row = dict(self.store.conn.execute(
            "SELECT * FROM evaluations WHERE id = ?", (eid,)).fetchone())
        inp = T.build_audit_input(self.store, row, CFG)
        self.assertEqual(inp["as_of"], "2026-10-20")
        self.assertIsNotNone(inp["published"])
        # 全項目70点のフィクスチャは再導出でも70.0になる (A13が黙る形)
        self.assertEqual(inp["expected_total"], 70.0)
        self.assertEqual(inp["expected_band"], "strong")
        self.assertEqual(inp["companies"][0]["strength"], "strong")

    def test_run_audit_rules_idempotent_and_rerun_on_change(self):
        self._mk_eval("規則対象1", items={
            "monetization": self._item(missing=["株価の推移が不明"])})
        self._mk_eval("規則対象2")
        res1 = T.run_audit_rules(self.store, CFG, limit=10, log=lambda *_: None)
        self.assertEqual(res1["audited"], 2)
        self.assertGreaterEqual(res1["flags"], 1)
        rows = self.store.audit_results(res1["batch"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["auditor_model"], "rules/v4.1")
        self.assertEqual(rows[0]["prompt_sha"], T.load_rules()["sha"])
        # 同じ規則 (同じ sha) では再監査しない
        res2 = T.run_audit_rules(self.store, CFG, limit=10, log=lambda *_: None)
        self.assertEqual(res2["audited"], 0)
        # 規則ファイルが変われば (sha が変われば) 再び対象になる
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            data = json.loads((T.ROOT / "audit_rules.json").read_text(encoding="utf-8"))
            data["checks"]["A6"]["black"][0]["note"] += " (改訂)"
            (d / "audit_rules.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
            res3 = T.run_audit_rules(self.store, CFG, limit=10,
                                     log=lambda *_: None, rules_dir=d)
            self.assertEqual(res3["audited"], 2)

    def test_rules_report(self):
        self._mk_eval("報告フラグ", items={
            "monetization": self._item(missing=["株価の推移が不明"])})
        self._mk_eval("報告グレー", items={
            "technology_readiness": self._item(
                reason="承認を取得し販売を開始した",
                evidence=[{"ref": "https://example.org/x", "summary": "売上が伸びている",
                           "kind": "fact"}])})
        res = T.run_audit_rules(self.store, CFG, limit=10, log=lambda *_: None)
        text = T.build_audit_report(self.store, res["batch"])
        self.assertIn("# 監査レポート", text)
        self.assertIn("要確認あり 1件", text)
        self.assertIn("グレーあり 1件", text)
        self.assertIn("## グレー一覧", text)
        self.assertIn("報告フラグ", text)
        self.assertIn("A6", text)
        self.assertIn("A4", text)
