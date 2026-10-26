"""追加分のテスト(一時エラーの扱い・抽出再利用・export・Webとの契約)。実行: python -m unittest test_site -v"""
import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import trl as T
from test_trl import AS_OF, CFG, EXTRACTION, LONG_KEYS, Mock, assessment, make_llm_fn, raw


def cfg_with(**llm):
    c = dict(CFG)
    c["scoring"] = copy.deepcopy(CFG["scoring"])
    c["scoring"]["llm"] = dict(T.LLM_DEFAULTS, **llm)
    c["sources"] = {"defaults": {"max_results": 5, "lookback_days": 30},
                    "queries": [{"name": "q", "arxiv": "a"}]}
    return c


def fetch_n(n):
    def arx(q, k, days, as_of):
        return [raw(sid=f"2610.{i:04d}", title=f"Paper {i}") for i in range(1, n + 1)]
    return {"arxiv": arx}


class RetryTests(unittest.TestCase):
    def test_retry_after_is_honored_and_capped(self):
        class H:
            def get(self, k, d=None):
                return {"Retry-After": "7"}.get(k, d)
        self.assertEqual(T._retry_after_seconds(H()), 7.0)

        class Big:
            def get(self, k, d=None):
                return "9999"
        self.assertEqual(T._retry_after_seconds(Big()), 60.0)
        self.assertIsNone(T._retry_after_seconds({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))
        self.assertIsNone(T._retry_after_seconds(None))

    def test_backoff_default_and_jitter_bounds(self):
        self.assertEqual(T.backoff_delay(1, rand=lambda: 0.0), 5)
        self.assertAlmostEqual(T.backoff_delay(2, rand=lambda: 1.0), 18.0)
        self.assertEqual(T.backoff_delay(1, retry_after=3.0), 3.0)

    def test_backoff_extends_for_later_attempts(self):
        # 503/429 の過負荷は数秒では回復しないため、3回目以降は間隔を延ばし上限60秒にする
        self.assertEqual(T.backoff_delay(3, rand=lambda: 0.0), 30)
        self.assertEqual(T.backoff_delay(4, rand=lambda: 0.0), 60)
        self.assertEqual(T.backoff_delay(9, rand=lambda: 0.0), 60)
        self.assertAlmostEqual(T.backoff_delay(4, rand=lambda: 1.0), 72.0)
        self.assertEqual(T.backoff_delay(4, retry_after=3.0), 3.0)  # Retry-After が常に優先

    def test_transient_classification(self):
        self.assertTrue(T.HttpError("x", 429).transient)
        self.assertTrue(T.HttpError("x", 503).transient)
        self.assertTrue(T.HttpError("x", None).transient)
        self.assertFalse(T.HttpError("x", 404).transient)
        self.assertFalse(T.HttpError("x", 401).transient)


class FallbackModelTests(unittest.TestCase):
    def setUp(self):
        import os
        self._env = dict(os.environ)
        os.environ["GEMINI_API_KEY"] = os.environ["ANTHROPIC_API_KEY"] = "k" * 10
        os.environ.pop("TRL_LLM_FALLBACK_MODEL", None)

    def tearDown(self):
        import os
        os.environ.clear()
        os.environ.update(self._env)

    def test_gemini_fallback_on_503(self):
        seen = []

        def http(url, **kw):
            seen.append(url)
            if "gemini-main" in url:
                raise T.HttpError("HTTP 503 for x", 503)
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "fb-ok"}]}}]}).encode()
        g = T.Gemini(model="gemini-main", http=http, fallback_model="gemini-sub")
        self.assertEqual(g.complete("p"), "fb-ok")
        self.assertEqual(len(seen), 2)

    def test_no_fallback_on_permanent_error(self):
        def http(url, **kw):
            raise T.HttpError("HTTP 404 for x", 404)
        g = T.Gemini(model="gemini-main", http=http, fallback_model="gemini-sub")
        with self.assertRaises(T.HttpError):
            g.complete("p")

    def test_no_fallback_when_unset_or_same_model(self):
        def http(url, **kw):
            raise T.HttpError("HTTP 503 for x", 503)
        self.assertIsNone(T.Gemini(model="m", http=http).fallback_model)
        self.assertIsNone(T.Gemini(model="m", http=http, fallback_model="m").fallback_model)
        with self.assertRaises(T.HttpError):
            T.Gemini(model="m", http=http).complete("p")

    def test_fallback_from_env(self):
        import os
        os.environ["TRL_LLM_FALLBACK_MODEL"] = "gemini-sub"
        self.assertEqual(T.Gemini(model="m", http=lambda *a, **k: b"{}").fallback_model, "gemini-sub")


class ReportLegacyQueueTests(unittest.TestCase):
    def test_report_hides_legacy_http_error_queue(self):
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d" / "trl.sqlite")
        try:
            cfg = cfg_with()
            T.run_pipeline(store, cfg, None, as_of=AS_OF, kinds=("long",), fetchers=fetch_n(1),
                           sources=("arxiv",), today=AS_OF, log=lambda *_: None)
            cid = store.conn.execute("SELECT id FROM candidates").fetchone()[0]
            store.upsert_queue(cid, "long", "evaluation_error: HttpError: HTTP 503 for x", [], [], [], "t", {})
            run_id = store.conn.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()[0]
            text = T.build_report(store, run_id)
            self.assertIn("レビューキュー（未解消 0件）", text)
            self.assertNotIn("HTTP 503 for x", text)
        finally:
            store.close()
            tmp.cleanup()


class LlmConfigTests(unittest.TestCase):
    def test_shipped_llm_settings_valid(self):
        self.assertEqual(T.llm_settings(CFG)["timeout_sec"], 120)

    def test_bad_llm_settings_rejected(self):
        for bad in ({"timeout_sec": 0}, {"retries": -1}, {"nope": 1}, {"timeout_sec": True}):
            c = copy.deepcopy(CFG)
            c["scoring"]["llm"] = bad
            with self.assertRaises(T.ConfigError):
                T.validate_config(c)

    def test_configure_llm_applies_to_providers_only(self):
        class P:
            timeout, retries = 120, 2
        p = T.configure_llm(P(), CFG)
        self.assertEqual((p.timeout, p.retries), (120, 3))
        self.assertIsNone(T.configure_llm(None, CFG))
        T.configure_llm(Mock(lambda p: ""), CFG)  # timeout 属性が無いMockは触らない


class TrlLevelTests(unittest.TestCase):
    EV = [{"ref": "u", "summary": "s", "kind": "fact"}]

    def test_absent_and_null(self):
        self.assertEqual(T.parse_trl_level(None), (None, []))
        out, errs = T.parse_trl_level({"level": None, "missing_info": ["x"]})
        self.assertEqual((out["level"], errs), (None, []))

    def test_valid_needs_reason_and_evidence(self):
        out, errs = T.parse_trl_level({"level": 4, "reason": "r", "evidence": self.EV})
        self.assertEqual((out["level"], errs), (4, []))
        _, errs = T.parse_trl_level({"level": 4, "reason": "", "evidence": []})
        self.assertIn("trl_level:reason_required", errs)
        self.assertIn("trl_level:evidence_required", errs)

    def test_invalid_levels(self):
        for lv in (0, 10, 4.5, "4", True):
            _, errs = T.parse_trl_level({"level": lv, "reason": "r", "evidence": self.EV})
            self.assertEqual(errs, ["trl_level.level:must_be_1_to_9_or_null"], lv)
        self.assertEqual(T.parse_trl_level("x")[1], ["trl_level:must_be_object"])


class PipelineResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "d" / "trl.sqlite"
        self.store = T.Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_p(self, llm, cfg, n=4, **kw):
        return T.run_pipeline(self.store, cfg, llm, as_of=AS_OF, kinds=("long",), fetchers=fetch_n(n),
                              sources=("arxiv",), today=AS_OF, log=lambda *_: None, **kw)

    def test_api_failures_are_not_stored_and_phase_stops_early(self):
        calls = {"n": 0}

        def fn(p):
            calls["n"] += 1
            raise T.HttpError("HTTP 503 for https://x", 503)
        r = self.run_p(Mock(fn), cfg_with(max_consecutive_failures=2))
        self.assertEqual(calls["n"], 2)  # 3件目以降は呼ばない
        self.assertEqual(r["llm_degraded"], "llm_consecutive_failures")
        self.assertEqual(r["counts"]["llm_infra_failed"], 2)
        self.assertEqual(r["counts"]["error"], 0)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0], 0)
        self.assertEqual(self.store.list_queue(), [])
        sts = {c["status"] for c in self.store.list_by_status(["screened", "error", "hold"])}
        self.assertEqual(sts, {"screened"})  # 未評価のまま
        self.assertEqual(r["counts"]["llm_degraded"], "llm_consecutive_failures")
        self.assertIn("LLM API失敗", T.build_report(self.store, r["run_id"]))

    def test_non_transient_error_stops_immediately(self):
        calls = {"n": 0}

        def fn(p):
            calls["n"] += 1
            raise T.HttpError("HTTP 404 for https://x", 404)
        r = self.run_p(Mock(fn), cfg_with(max_consecutive_failures=5))
        self.assertEqual(calls["n"], 1)
        self.assertEqual(r["llm_degraded"], "llm_fatal_error")

    def test_success_resets_consecutive_counter(self):
        seq = iter([True, False, True, False, False, False])
        base = make_llm_fn(long_scores={k: 70 for k in LONG_KEYS})

        def fn(p):
            if "根拠抽出」担当" in p:
                if next(seq):
                    raise T.HttpError("HTTP 429", 429)
            return base(p)
        r = self.run_p(Mock(fn), cfg_with(max_consecutive_failures=2), n=3)
        self.assertIsNone(r["llm_degraded"])
        self.assertEqual((r["counts"]["llm_infra_failed"], r["counts"]["final"]), (2, 1))

    def test_content_errors_are_still_stored(self):
        r = self.run_p(Mock(make_llm_fn(bad="not json")), cfg_with(), n=2)
        self.assertEqual(r["counts"]["error"], 2)
        self.assertEqual(r["counts"]["llm_infra_failed"], 0)
        self.assertEqual(len(self.store.list_queue()), 2)

    def test_phase_budget(self):
        base = make_llm_fn(long_scores={k: 70 for k in LONG_KEYS})
        r = self.run_p(Mock(base), cfg_with(phase_budget_sec=1000), n=3)
        self.assertEqual(r["counts"]["final"], 3)  # 予算内なら通常どおり
        self.assertIsNone(r["llm_degraded"])
        # 予算を超えたら打ち切る(経過時間を進めた時計で再現)
        import unittest.mock as um
        t = iter(range(0, 100000, 10))
        s2 = T.Store(Path(self.tmp.name) / "b" / "trl.sqlite")
        with um.patch.object(T.time, "monotonic", lambda: next(t)):
            r2 = T.run_pipeline(s2, cfg_with(phase_budget_sec=15), Mock(base), as_of=AS_OF, kinds=("long",),
                                fetchers=fetch_n(5), sources=("arxiv",), today=AS_OF, log=lambda *_: None)
        s2.close()
        self.assertEqual(r2["llm_degraded"], "phase_budget_exceeded")
        self.assertLess(r2["counts"]["final"], 5)

    def test_extraction_reused_after_scoring_failure(self):
        n = {"extract": 0, "long": 0}
        base = make_llm_fn(long_scores={k: 70 for k in LONG_KEYS})

        def fn(p):
            if "根拠抽出」担当" in p:
                n["extract"] += 1
            if "長期成長評価」担当" in p:
                n["long"] += 1
                if n["long"] == 1:
                    raise T.HttpError("HTTP 503", 503)
            return base(p)
        cfg = cfg_with(max_consecutive_failures=5)
        r1 = self.run_p(Mock(fn), cfg, n=1)
        self.assertEqual((n["extract"], r1["counts"]["llm_infra_failed"]), (1, 1))
        r2 = self.run_p(Mock(fn), cfg, n=1, do_collect=False)  # 同じ基準日で再実行
        self.assertEqual(n["extract"], 1)  # 抽出はキャッシュから再利用
        self.assertEqual(r2["counts"]["final"], 1)
        ev = self.store.latest_evaluation(self.store.conn.execute("SELECT id FROM candidates").fetchone()[0], "long")
        self.assertTrue(json.loads(ev["extra_json"])["extract_cached"])

    def test_legacy_infra_errors_are_requeued(self):
        # 旧版が API失敗を status=error で保存した候補は、retry指定なしでも再選択される
        self.run_p(None, cfg_with(), n=2)  # 収集のみ
        cid = self.store.conn.execute("SELECT id FROM candidates").fetchone()[0]
        c = self.store.get_candidate(cid)
        legacy = {"kind": "long", "as_of": AS_OF.isoformat(), "status": "error", "total": None, "band": None, "items": {},
                  "extraction": None, "event": None, "prompt_versions": {}, "config_version": "x", "config_hash": "x",
                  "llm_provider": "gemini", "llm_model": "m", "input_hash": "h",
                  "errors": ["HttpError: HTTP 503 for https://generativelanguage.googleapis.com/x"]}
        self.store.add_evaluation("r0", cid, legacy)
        self.store.set_status(cid, "error", retry_inc=True)
        self.store.conn.execute("UPDATE candidates SET retry_count=99 WHERE id=?", (cid,))
        self.store.conn.commit()
        self.assertTrue(self.store.only_infra_errors(cid))
        picked = [x["id"] for x in T.select_for_eval(self.store, CFG, AS_OF, 10)]
        self.assertIn(cid, picked)
        # 内容起因のエラーは従来どおり、明示的に許可されるまで選ばれない
        legacy["errors"] = ["LLMError: invalid JSON"]
        self.store.add_evaluation("r1", cid, legacy)
        self.assertFalse(self.store.only_infra_errors(cid))
        self.assertNotIn(cid, [x["id"] for x in T.select_for_eval(self.store, CFG, AS_OF, 10)])

    def test_migration_adds_extra_column_to_old_db(self):
        import sqlite3
        p = Path(self.tmp.name) / "old.sqlite"
        con = sqlite3.connect(p)
        con.executescript(T.SCHEMA)  # extra_json の無い旧スキーマを再現するため、列を作り直す
        con.executescript("DROP TABLE evaluations; CREATE TABLE evaluations(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                          "run_id TEXT, candidate_id TEXT, kind TEXT, as_of TEXT, evaluated_at TEXT, status TEXT, "
                          "total REAL, band TEXT, items_json TEXT, extraction_json TEXT, event_json TEXT, "
                          "prompt_versions_json TEXT, config_version TEXT, config_hash TEXT, llm_provider TEXT, "
                          "llm_model TEXT, input_hash TEXT, errors_json TEXT);")
        con.close()
        s2 = T.Store(p)  # 既存DBでも例外なく開ける
        cols = {r[1] for r in s2.conn.execute("PRAGMA table_info(evaluations)")}
        self.assertIn("extra_json", cols)
        s2.close()


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = T.Store(Path(self.tmp.name) / "d" / "trl.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def build(self, llm, n=3):
        cfg = cfg_with()
        T.run_pipeline(self.store, cfg, llm, as_of=AS_OF, kinds=("long",), fetchers=fetch_n(n), sources=("arxiv",),
                       today=AS_OF, log=lambda *_: None)
        return T.build_site_data(self.store, cfg, datetime(2026, 10, 26, tzinfo=timezone.utc))

    def test_statuses_derived_from_data(self):
        # 必須項目(monetization)が欠ける → hold / 必須は揃い任意項目だけ欠ける → review / 全部揃う → final
        order = iter([{"monetization": None}, {"market_growth": None}, {k: 65 for k in LONG_KEYS}])
        cache = {}

        def fn(p):
            if "根拠抽出」担当" in p:
                return json.dumps(EXTRACTION)
            key = p
            if key not in cache:
                cache[key] = next(order)
            return json.dumps(assessment(LONG_KEYS, cache[key]))
        data = self.build(Mock(fn))
        got = sorted(c["display_status"] for c in data["candidates"])
        self.assertEqual(got, ["final", "hold", "review"])

    def test_pending_rejected_and_legacy_api_errors(self):
        data = self.build(None)  # LLM未設定 → すべて未評価
        self.assertEqual({c["display_status"] for c in data["candidates"]}, {"pending"})

    def test_contract_matches_web_schema(self):
        base = make_llm_fn(long_scores={"monetization": None})
        data = self.build(Mock(base))
        schema = json.loads((T.ROOT / "web" / "schema.json").read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], schema["schema_version"])
        for k in schema["top_level"]:
            self.assertIn(k, data)
        for k in schema["config"]:
            self.assertIn(k, data["config"])
        for c in data["candidates"]:
            for k in schema["candidate"]:
                self.assertIn(k, c)
            self.assertIn(c["display_status"], schema["display_status"])
            if c["long"]:
                for k in schema["evaluation"]:
                    self.assertIn(k, c["long"])
                for it in c["long"]["items"].values():
                    for k in schema["item"]:
                        self.assertIn(k, it)
        self.assertEqual(set(data["config"]["item_groups"]["technology"]), set(T.TECH_ITEMS))
        json.dumps(data, ensure_ascii=False)  # シリアライズ可能

    def test_export_writes_site_without_schema_file(self):
        self.build(Mock(make_llm_fn()))
        out = Path(self.tmp.name) / "site"
        T.export_site(self.store, cfg_with(), out)
        names = {p.name for p in out.iterdir()}
        self.assertIn("data.json", names)
        self.assertIn("index.html", names)
        self.assertNotIn("schema.json", names)
        self.assertEqual(json.loads((out / "data.json").read_text(encoding="utf-8"))["schema_version"], 1)

    def test_trl_level_flows_to_export_and_not_inferred_from_score(self):
        def fn(p):
            if "根拠抽出」担当" in p:
                return json.dumps(EXTRACTION)
            o = assessment(LONG_KEYS, {k: 70 for k in LONG_KEYS})
            o["trl_level"] = {"level": 5, "reason": "r", "evidence": [{"ref": "u", "summary": "s", "kind": "fact"}]}
            return json.dumps(o)
        data = self.build(Mock(fn), n=1)
        self.assertEqual(data["candidates"][0]["long"]["trl_level"]["level"], 5)
        # 別DBで、trl_level を返さないAI応答(v1相当)。スコアが高くても trl_level は null のまま
        other = T.Store(Path(self.tmp.name) / "other" / "trl.sqlite")
        cfg = cfg_with()
        T.run_pipeline(other, cfg, Mock(make_llm_fn(long_scores={k: 90 for k in LONG_KEYS})), as_of=AS_OF,
                       kinds=("long",), fetchers=fetch_n(1), sources=("arxiv",), today=AS_OF, log=lambda *_: None)
        data2 = T.build_site_data(other, cfg)
        other.close()
        for c in data2["candidates"]:
            self.assertIsNone(c["long"]["trl_level"])  # スコアから換算しない

    def test_legacy_http_error_queue_entries_hidden(self):
        cfg = cfg_with()
        T.run_pipeline(self.store, cfg, None, as_of=AS_OF, kinds=("long",), fetchers=fetch_n(1), sources=("arxiv",),
                       today=AS_OF, log=lambda *_: None)
        cid = self.store.conn.execute("SELECT id FROM candidates").fetchone()[0]
        self.store.upsert_queue(cid, "long", "evaluation_error: HttpError: HTTP 429 for x", [], [], [], "t", {})
        data = T.build_site_data(self.store, cfg)
        self.assertEqual(data["candidates"][0]["queue"], {})


if __name__ == "__main__":
    unittest.main()
