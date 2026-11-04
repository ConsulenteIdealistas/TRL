"""追加分のテスト(一時エラーの扱い・抽出再利用・export・Webとの契約)。実行: python -m unittest test_site -v"""
import copy
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import trl as T
from trlcore import site as site_mod
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
        # 15→30秒 (旧 5→15→30 より一段長めに置く)
        self.assertEqual(T.backoff_delay(1, rand=lambda: 0.0), 15)
        self.assertAlmostEqual(T.backoff_delay(2, rand=lambda: 1.0), 36.0)
        self.assertEqual(T.backoff_delay(1, retry_after=3.0), 3.0)

    def test_backoff_extends_for_later_attempts(self):
        # 試行2〜3は30秒、4回目以降は60秒を上限にする
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

    def test_llm_call_recorder_captures_each_attempt(self):
        # 計装: モデル試行ごとに目的・実モデル・成否・トークンが記録される
        def http(url, **kw):
            if "gemini-main" in url:
                raise T.HttpError("HTTP 503 for x", 503)
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "fb-ok"}]}}],
                               "usageMetadata": {"promptTokenCount": 111,
                                                 "candidatesTokenCount": 22}}).encode()
        g = T.Gemini(model="gemini-main", http=http, fallback_model="gemini-sub")
        recs = []
        g.recorder = recs.append
        out = g.complete("prompt-text", purpose="assess", candidate_id="c_x")
        self.assertEqual(out, "fb-ok")
        self.assertEqual(len(recs), 2)
        self.assertEqual((recs[0]["model"], recs[0]["ok"]), ("gemini-main", False))
        self.assertIn("HttpError", recs[0]["error"])
        self.assertEqual((recs[1]["model"], recs[1]["ok"]), ("gemini-sub", True))
        self.assertEqual((recs[1]["tokens_in"], recs[1]["tokens_out"]), (111, 22))
        self.assertEqual((recs[1]["purpose"], recs[1]["candidate_id"]), ("assess", "c_x"))
        self.assertEqual(recs[1]["chars_in"], len("prompt-text"))
        self.assertEqual(recs[1]["chars_out"], len("fb-ok"))
        self.assertGreaterEqual(recs[1]["seconds"], 0)

    def test_llm_recorder_failure_does_not_break_call(self):
        def ok_http(url, **kw):
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}).encode()
        g = T.Gemini(model="m", http=ok_http)

        def bad_recorder(rec):
            raise RuntimeError("recorder down")
        g.recorder = bad_recorder
        self.assertEqual(g.complete("p"), "ok")

    def test_model_breaker_skips_main_after_first_failure(self):
        # 主モデルが1回失敗したら、その実行の間は主を試さず代替へ直行する
        # (実測: 初失敗後の成功は 25回中1回。不調は時間帯で固まるため)
        main_calls = []

        def http(url, **kw):
            if "gemini-main" in url:
                main_calls.append(url)
                raise T.HttpError("HTTP 503 for x", 503)
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "fb"}]}}]}).encode()
        g = T.Gemini(model="gemini-main", http=http, fallback_model="gemini-sub")
        recs = []
        g.recorder = recs.append
        for _ in range(5):
            self.assertEqual(g.complete("p", purpose="assess"), "fb")
        self.assertEqual(len(main_calls), 1)  # 2回目以降は主を試していない
        skipped = [r for r in recs if r.get("main_skipped")]
        self.assertEqual(len(skipped), 4)
        self.assertTrue(all(r["model"] == "gemini-sub" for r in skipped))
        self.assertEqual(g.last_model, "gemini-sub")

    def test_model_breaker_keeps_main_while_it_succeeds(self):
        # 主モデルが成功し続ける限り、ブレーカーは開かず主が使われ続ける
        main_calls = []

        def http(url, **kw):
            if "gemini-main" in url:
                main_calls.append(url)
                return json.dumps({"candidates": [{"content": {"parts": [{"text": "main-ok"}]}}]}).encode()
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "fb"}]}}]}).encode()
        g = T.Gemini(model="gemini-main", http=http, fallback_model="gemini-sub")
        outs = [g.complete("p") for _ in range(4)]
        self.assertEqual(len(main_calls), 4)  # 全コールで主が試されている
        self.assertTrue(all(o == "main-ok" for o in outs))
        self.assertEqual(g.last_model, "gemini-main")

    def test_fallback_chain_reaches_second_fallback(self):
        # 主 → 代替1 が両方とも一時的失敗なら、代替2 で応答する
        calls = []

        def http(url, **kw):
            for name in ("gemini-sub2", "gemini-main", "gemini-sub"):
                if name in url:
                    calls.append(name)
                    if name != "gemini-sub2":
                        raise T.HttpError("HTTP 503 for x", 503)
                    return json.dumps({"candidates": [{"content": {"parts": [{"text": "fb2"}]}}]}).encode()
            raise AssertionError(url)
        g = T.Gemini(model="gemini-main", http=http, fallback_model="gemini-sub",
                     fallback_model_2="gemini-sub2")
        self.assertEqual(g.complete("p"), "fb2")
        self.assertEqual(calls, ["gemini-main", "gemini-sub", "gemini-sub2"])
        self.assertEqual(g.last_model, "gemini-sub2")
        self.assertEqual(g.fallback_models, ["gemini-sub", "gemini-sub2"])
        self.assertEqual(g.fallback_model, "gemini-sub")  # 互換プロパティは1段目

    def test_fallback_chain_stops_at_first_success(self):
        # 代替1 が応答すれば代替2 は呼ばない
        calls = []

        def http(url, **kw):
            if "gemini-main" in url:
                calls.append("main")
                raise T.HttpError("HTTP 503 for x", 503)
            calls.append("sub2" if "gemini-sub2" in url else "sub")
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}).encode()
        g = T.Gemini(model="gemini-main", http=http, fallback_model="gemini-sub",
                     fallback_model_2="gemini-sub2")
        self.assertEqual(g.complete("p"), "ok")
        self.assertEqual(calls, ["main", "sub"])

    def test_stage_breaker_skips_failed_fallback(self):
        # 主と代替1 が落ち続けると両段ともブレーカーで飛ばされ、代替2 へ直行する
        counts = {"gemini-main": 0, "gemini-sub": 0, "gemini-sub2": 0}

        def http(url, **kw):
            for name in ("gemini-sub2", "gemini-main", "gemini-sub"):
                if name in url:
                    counts[name] += 1
                    if name != "gemini-sub2":
                        raise T.HttpError("HTTP 503 for x", 503)
                    return json.dumps({"candidates": [{"content": {"parts": [{"text": "fb2"}]}}]}).encode()
            raise AssertionError(url)
        g = T.Gemini(model="gemini-main", http=http, fallback_model="gemini-sub",
                     fallback_model_2="gemini-sub2")
        for _ in range(5):
            self.assertEqual(g.complete("p"), "fb2")
        self.assertEqual(counts["gemini-main"], 1)  # 1回の失敗で見切られる
        self.assertEqual(counts["gemini-sub"], 1)
        self.assertEqual(counts["gemini-sub2"], 5)

    def test_second_fallback_from_env(self):
        # 環境変数 TRL_LLM_FALLBACK_MODEL_2 からも2段目を読める
        import os
        old = os.environ.get("TRL_LLM_FALLBACK_MODEL_2")
        os.environ["TRL_LLM_FALLBACK_MODEL_2"] = "gemini-env2"
        try:
            g = T.Gemini(model="gemini-main", http=lambda *a, **k: b"{}", fallback_model="gemini-sub")
            self.assertEqual(g.fallback_models, ["gemini-sub", "gemini-env2"])
        finally:
            if old is None:
                os.environ.pop("TRL_LLM_FALLBACK_MODEL_2", None)
            else:
                os.environ["TRL_LLM_FALLBACK_MODEL_2"] = old

    def test_last_model_tracking(self):
        def ok_http(url, **kw):
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}).encode()
        g = T.Gemini(model="gemini-main", http=ok_http, fallback_model="gemini-sub")
        self.assertIsNone(g.last_model)
        g.complete("p")
        self.assertEqual(g.last_model, "gemini-main")

        def flaky_http(url, **kw):
            if "gemini-main" in url:
                raise T.HttpError("HTTP 503 for x", 503)
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "fb"}]}}]}).encode()
        g2 = T.Gemini(model="gemini-main", http=flaky_http, fallback_model="gemini-sub")
        g2.complete("p")
        self.assertEqual(g2.last_model, "gemini-sub")

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


class SecondProviderTests(unittest.TestCase):
    """第2プロバイダ (Cloudflare Workers AI) とプロバイダ階段の検証。"""

    KEYS = ("GEMINI_API_KEY", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "TRL_LLM_CF_MODEL")

    def setUp(self):
        import os
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        os.environ["GEMINI_API_KEY"] = "g-key"
        os.environ["CLOUDFLARE_ACCOUNT_ID"] = "acct-1"
        os.environ["CLOUDFLARE_API_TOKEN"] = "cf-token"

    def tearDown(self):
        import os
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @staticmethod
    def cf_ok(text="cf-ok"):
        return json.dumps({"choices": [{"message": {"content": text}}],
                           "usage": {"prompt_tokens": 31, "completion_tokens": 7}}).encode()

    def test_cloudflare_request_and_parsing(self):
        seen = {}

        def http(url, **kw):
            seen.update(url=url, kw=kw)
            return self.cf_ok()
        p = T.get_provider("cloudflare", http=http)
        self.assertEqual(p.name, "cloudflare")
        self.assertEqual(p.model, "@cf/google/gemma-4-26b-a4b-it")
        recs = []
        p.recorder = recs.append
        self.assertEqual(p.complete("プロンプト", purpose="assess", candidate_id=9), "cf-ok")
        self.assertEqual(seen["url"],
                         "https://api.cloudflare.com/client/v4/accounts/acct-1/ai/v1/chat/completions")
        self.assertEqual(seen["kw"]["headers"]["authorization"], "Bearer cf-token")
        body = json.loads(seen["kw"]["data"])
        self.assertEqual(body["model"], "@cf/google/gemma-4-26b-a4b-it")
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][1]["content"], "プロンプト")
        self.assertEqual(p.last_model, "@cf/google/gemma-4-26b-a4b-it")
        self.assertEqual(len(recs), 1)
        self.assertEqual((recs[0]["provider"], recs[0]["tokens_in"], recs[0]["tokens_out"]),
                         ("cloudflare", 31, 7))

    def test_cloudflare_model_from_env(self):
        import os
        os.environ["TRL_LLM_CF_MODEL"] = "@cf/zai-org/glm-4.7-flash"
        p = T.get_provider("cloudflare", http=lambda *a, **k: self.cf_ok())
        self.assertEqual(p.model, "@cf/zai-org/glm-4.7-flash")

    def test_cloudflare_requires_both_credentials(self):
        import os
        os.environ.pop("CLOUDFLARE_API_TOKEN")
        with self.assertRaises(T.LLMError):
            T.get_provider("cloudflare", http=lambda *a, **k: b"{}")
        os.environ["CLOUDFLARE_API_TOKEN"] = "cf-token"
        os.environ.pop("CLOUDFLARE_ACCOUNT_ID")
        with self.assertRaises(T.LLMError):
            T.get_provider("cloudflare", http=lambda *a, **k: b"{}")

    def test_chain_falls_through_to_cloudflare(self):
        def http(url, **kw):
            if "api.cloudflare.com" in url:
                return self.cf_ok("from-cf")
            raise T.HttpError("HTTP 503 for x", 503)
        llm = T.get_provider("gemini", model="gemini-main", http=http)
        self.assertIsInstance(llm, T.ProviderChain)
        self.assertEqual(llm.name, "gemini+cloudflare")
        self.assertEqual(llm.model, "gemini-main")
        recs = []
        llm.recorder = recs.append
        self.assertEqual(llm.complete("p", purpose="extract", candidate_id=3), "from-cf")
        self.assertEqual(llm.last_model, "@cf/google/gemma-4-26b-a4b-it")
        self.assertEqual([r["provider"] for r in recs], ["gemini", "cloudflare"])
        self.assertEqual([r["ok"] for r in recs], [False, True])

    def test_chain_absent_without_cf_env(self):
        import os
        os.environ.pop("CLOUDFLARE_ACCOUNT_ID")
        os.environ.pop("CLOUDFLARE_API_TOKEN")
        llm = T.get_provider("gemini", model="m", http=lambda *a, **k: b"{}")
        self.assertIsInstance(llm, T.Gemini)
        # 片方だけの設定でも階段は組まない (従来どおり第1プロバイダ単独)
        os.environ["CLOUDFLARE_API_TOKEN"] = "cf-token"
        llm = T.get_provider("gemini", model="m", http=lambda *a, **k: b"{}")
        self.assertIsInstance(llm, T.Gemini)

    def test_chain_stops_on_permanent_error(self):
        cf_calls = []

        def http(url, **kw):
            if "api.cloudflare.com" in url:
                cf_calls.append(url)
                return self.cf_ok()
            raise T.HttpError("HTTP 404 for x", 404)
        llm = T.get_provider("gemini", model="gemini-main", http=http)
        with self.assertRaises(T.HttpError):
            llm.complete("p")
        self.assertEqual(cf_calls, [])

    def test_chain_stops_on_deadline_exceeded(self):
        cf_calls = []

        def http(url, **kw):
            if "api.cloudflare.com" in url:
                cf_calls.append(url)
                return self.cf_ok()
            raise T.HttpError("deadline", None, deadline_exceeded=True)
        llm = T.get_provider("gemini", model="gemini-main", http=http)
        with self.assertRaises(T.HttpError):
            llm.complete("p")
        self.assertEqual(cf_calls, [])

    def test_pipeline_evaluates_via_cloudflare_when_gemini_down(self):
        # Gemini が全段落ちたら Cloudflare で評価が完走し、来歴 (llm_provider /
        # llm_model) と llm_calls に実際に答えた側が残ることを run 経由で固定する
        fn = make_llm_fn(long_scores={k: 65 for k in LONG_KEYS})

        def http(url, **kw):
            if "api.cloudflare.com" in url:
                prompt = json.loads(kw["data"])["messages"][1]["content"]
                return self.cf_ok(fn(prompt))
            raise T.HttpError("HTTP 503 for x", 503)
        tmp = tempfile.TemporaryDirectory()
        try:
            store = T.Store(Path(tmp.name) / "d" / "trl.sqlite")
            llm = T.get_provider("gemini", model="gemini-main", http=http)
            r = T.run_pipeline(store, cfg_with(), llm, as_of=AS_OF, kinds=("long",),
                               fetchers=fetch_n(2), today=AS_OF, log=lambda *_: None)
            self.assertEqual(r["counts"]["final"], 2)
            rows = store.llm_calls(r["run_id"])
            self.assertTrue(any(row["provider"] == "cloudflare" and row["ok"] for row in rows))
            self.assertTrue(any(row["provider"] == "gemini" and not row["ok"] for row in rows))
            evs = store.evaluations_for_run(r["run_id"], "long")
            self.assertTrue(all(e["llm_model"] == "@cf/google/gemma-4-26b-a4b-it" for e in evs))
            self.assertTrue(all(e["llm_provider"] == "gemini+cloudflare" for e in evs))
            store.close()
        finally:
            tmp.cleanup()

    def test_chain_settings_and_recorder_fanout(self):
        llm = T.get_provider("gemini", model="m", http=lambda *a, **k: b"{}")
        T.configure_llm(llm, cfg_with(timeout_sec=77, retries=5, call_budget_sec=99))
        for p in llm.providers:
            self.assertEqual((p.timeout, p.retries, p.call_budget), (77, 5, 99))
        fn = lambda rec: None
        llm.recorder = fn
        self.assertTrue(all(p.recorder is fn for p in llm.providers))
        self.assertIs(llm.recorder, fn)


class SiteConsistencyTests(unittest.TestCase):
    def test_error_evaluation_direction_is_dropped(self):
        # error 評価の方向性はサイトに出さない (一覧・詳細の共通ルールを site 側で担保)
        from trlcore.site import _ev_for_site
        ev = {"kind": "long", "status": "error", "total": None, "band": None, "as_of": "2026-10-20",
              "evaluated_at": "2026-10-20T00:00:00Z", "items_json": "{}", "errors_json": "[\"x\"]",
              "extra_json": json.dumps({"direction": {"direction": "positive", "reason": "r"}})}
        self.assertIsNone(_ev_for_site(ev, CFG)["direction"])
        ev["status"] = "hold"
        self.assertEqual(_ev_for_site(ev, CFG)["direction"]["direction"], "positive")


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

    def test_parallel_eval_processes_all_candidates(self):
        import threading
        import time as time_mod
        base = make_llm_fn(long_scores={k: 70 for k in LONG_KEYS})
        lock = threading.Lock()
        calls = {"n": 0}

        def fn(p):
            with lock:
                calls["n"] += 1
            time_mod.sleep(0.25)
            return base(p)
        t0 = time_mod.monotonic()
        r = self.run_p(Mock(fn), cfg_with(eval_workers=3), n=4, max_eval=4)
        elapsed = time_mod.monotonic() - t0
        self.assertEqual(r["counts"]["final"], 4)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0], 4)
        # 直列なら 4候補×2コール×0.25秒 = 2秒。3並列バッチならその半分未満に収まる
        self.assertLess(elapsed, 1.6)

    def test_parallel_batch_stops_after_infra_failures(self):
        import threading
        lock = threading.Lock()
        calls = {"n": 0}

        def fn(p):
            with lock:
                calls["n"] += 1
            raise T.HttpError("HTTP 503 for https://x", 503)
        r = self.run_p(Mock(fn), cfg_with(eval_workers=3, max_consecutive_failures=2), n=5, max_eval=5)
        self.assertEqual(calls["n"], 3)  # 最初のバッチ(3件)までで止まる。残り2件は呼ばない
        self.assertEqual(r["llm_degraded"], "llm_consecutive_failures")
        self.assertEqual(r["counts"]["llm_infra_failed"], 2)  # 保存側の判定は候補順どおり
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0], 0)

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

    def test_prev_evaluation_diff_exported(self):
        # 前回比 (Issue 1): 再評価された候補には直前の比較可能な評価の要約が付く
        data = self.build(Mock(make_llm_fn(long_scores={k: 65 for k in LONG_KEYS})), n=1)
        cand = data["candidates"][0]
        self.assertIsNone(cand["long"]["prev"])  # 初回評価には前回が無い
        cid = cand["id"]
        ev2 = {"kind": "long", "as_of": AS_OF.isoformat(), "status": "hold", "total": None, "band": None,
               "items": {"technology_readiness": {"score": 50, "reason": "", "evidence": [],
                                                  "counter_evidence": [], "missing_info": []}},
               "extraction": None, "event": None, "prompt_versions": {}, "config_version": "x",
               "config_hash": "x", "llm_provider": "gemini", "llm_model": "m", "input_hash": "h2",
               "errors": [], "extra": {"trl_level": {"level": 5, "reason": "r", "evidence": [], "missing_info": []}}}
        self.store.add_evaluation("r9", cid, ev2)
        data2 = T.build_site_data(self.store, cfg_with(), datetime(2026, 10, 26, tzinfo=timezone.utc))
        cand2 = [c for c in data2["candidates"] if c["id"] == cid][0]
        prev = cand2["long"]["prev"]
        self.assertIsNotNone(prev)
        self.assertEqual(prev["total"], 65)
        self.assertEqual(prev["status"], "final")
        self.assertEqual(prev["items"]["technology_readiness"], 65)
        # 最新が error のときは前回比を付けない (error は採点結果ではない)
        ev3 = dict(ev2, status="error", errors=["LLMError: invalid JSON"], input_hash="h3")
        self.store.add_evaluation("r10", cid, ev3)
        data3 = T.build_site_data(self.store, cfg_with(), datetime(2026, 10, 26, tzinfo=timezone.utc))
        cand3 = [c for c in data3["candidates"] if c["id"] == cid][0]
        self.assertNotIn("prev", cand3["long"])

    def test_hold_cause_split_exported(self):
        # 保留の主因の内訳 (Issue 2 の表示策): 企業を特定できないケースと、
        # 特定できているが根拠が無いケースを、理由文の明記から切り分ける
        data = self.build(Mock(make_llm_fn(long_scores={k: 65 for k in LONG_KEYS})), n=1)
        cid = data["candidates"][0]["id"]

        def hold_ev(missing, reason):
            return {"kind": "long", "as_of": AS_OF.isoformat(), "status": "hold", "total": None, "band": None,
                    "items": {"monetization": {"score": None, "reason": reason, "evidence": [],
                                               "counter_evidence": [], "missing_info": [missing]}},
                    "extraction": None, "event": None, "prompt_versions": {}, "config_version": "x",
                    "config_hash": "x", "llm_provider": "gemini", "llm_model": "m", "input_hash": "h",
                    "errors": [], "extra": {}}

        def cause():
            d = T.build_site_data(self.store, cfg_with(), datetime(2026, 10, 26, tzinfo=timezone.utc))
            return [c for c in d["candidates"] if c["id"] == cid][0]["long"].get("hold_cause")

        self.store.add_evaluation("r9", cid, hold_ev("対象企業の特定および商用化・収益実績に関する情報",
                                                     "対象企業が特定されていないため評価不可。"))
        self.assertEqual(cause(), "unidentified")
        self.store.add_evaluation("r10", cid, hold_ev("製品化や販売に関する受注・売上実績",
                                                      "前商業段階であり、商用化実績を示す情報が含まれていない。"))
        self.assertEqual(cause(), "no_evidence")

    def test_news_candidates_hidden_from_site(self):
        self.build(None, n=1)
        rec, _ = T.normalize({"source": "gnews", "source_id": "z1", "title": "アストロスケール news",
                              "abstract": "x" * 60, "url": "https://example.org/z1",
                              "published": "2026-10-19", "fields": ["日本経済新聞"],
                              "info_type": "news"}, T.now_iso())
        self.store.upsert_candidate(rec, "rX")
        data = T.build_site_data(self.store, cfg_with(), datetime(2026, 10, 26, tzinfo=timezone.utc))
        self.assertTrue(data["candidates"])
        self.assertTrue(all(c["source"] != "gnews" for c in data["candidates"]))

    def test_entities_carry_market_and_sector(self):
        # 国・分野の絞り込み/並び替え用に、関連企業へ market と sector を載せる
        data = self.build(None, n=1)
        ents = [e for c in data["candidates"] for e in c["entities"]]
        self.assertTrue(ents)
        self.assertTrue(all(e["market"] == "JP" and e["sector"] == "space" for e in ents))

    def test_entities_carry_stage(self):
        # 企業の段階 (stage) が書き出しにも載る (表示・分析専用)
        cfg = cfg_with()
        cfg["entities"] = copy.deepcopy(cfg["entities"])
        for e in cfg["entities"]["entities"]:
            e["stage"] = "demo"
        T.run_pipeline(self.store, cfg, None, as_of=AS_OF, kinds=("long",),
                       fetchers=fetch_n(1), sources=("arxiv",), log=lambda *a: None)
        data = T.build_site_data(self.store, cfg, datetime(2026, 10, 26, tzinfo=timezone.utc))
        ents = [e for c in data["candidates"] for e in c["entities"]]
        self.assertTrue(ents)
        self.assertTrue(all(e["stage"] == "demo" for e in ents))

    def test_entities_carry_tags(self):
        # 二層化: 辞書の tags (フォトニクス/医療機器など) が書き出しにも載る
        cfg = cfg_with()
        cfg["entities"] = copy.deepcopy(cfg["entities"])
        for e in cfg["entities"]["entities"]:
            e["tags"] = ["photonics"]
        T.run_pipeline(self.store, cfg, None, as_of=AS_OF, kinds=("long",),
                       fetchers=fetch_n(1), sources=("arxiv",), log=lambda *a: None)
        data = T.build_site_data(self.store, cfg, datetime(2026, 10, 26, tzinfo=timezone.utc))
        ents = [e for c in data["candidates"] for e in c["entities"]]
        self.assertTrue(ents)
        self.assertTrue(all(e["tags"] == ["photonics"] for e in ents))

    def test_translation_exported(self):
        data = self.build(None, n=1)
        cid = data["candidates"][0]["id"]
        self.assertIsNone(data["candidates"][0]["title_ja"])
        self.assertIsNone(data["candidates"][0]["translation_src"])
        self.store.set_translation(cid, "邦題", "邦文抄録", title_by="deepl", abstract_by="azure")
        data = T.build_site_data(self.store, cfg_with(), datetime(2026, 10, 26, tzinfo=timezone.utc))
        got = next(c for c in data["candidates"] if c["id"] == cid)
        self.assertEqual((got["title_ja"], got["abstract_ja"]), ("邦題", "邦文抄録"))
        self.assertEqual(got["translation_src"], {"title": "deepl", "abstract": "azure"})

    def test_pending_rejected_and_legacy_api_errors(self):
        data = self.build(None)  # LLM未設定 → すべて未評価
        self.assertEqual({c["display_status"] for c in data["candidates"]}, {"pending"})

    def test_export_limits_pending_to_next_up(self):
        # 未評価は「次に評価される上位10件」だけを載せ、next_up を付ける (在庫の全件表示はしない)
        self.build(None, n=1)
        for i in range(15):
            rec, _ = T.normalize(raw(sid=f"2610.55{i:02d}", title=f"Queued paper {i}",
                                     published="2026-10-19"), T.now_iso())
            cid, _ = self.store.upsert_candidate(rec, "rX")
            self.store.set_entities(cid, T.link_entities(rec, CFG.get("entities", {})))
            self.store.set_status(cid, "screened")
        data = T.build_site_data(self.store, cfg_with(), datetime(2026, 10, 26, tzinfo=timezone.utc))
        pend = [c for c in data["candidates"] if c["display_status"] == "pending"]
        self.assertEqual(len(pend), 10)
        self.assertTrue(all(c["next_up"] for c in pend))

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
        self.assertIn("detail.html", names)
        self.assertNotIn("schema.json", names)
        self.assertEqual(json.loads((out / "data.json").read_text(encoding="utf-8"))["schema_version"], 3)

    def test_search_suggest_is_touch_friendly(self):
        # スマホでサジェストが効くための配線: タップは pointerdown、候補は入力欄の直下、
        # 入力欄は iOS が拡大しない 16px。どれか欠けると実機でタップがずれる/候補が離れる。
        html = (T.ROOT / "web" / "index.html").read_text(encoding="utf-8")
        js = (T.ROOT / "web" / "app.js").read_text(encoding="utf-8")
        css = (T.ROOT / "web" / "style.css").read_text(encoding="utf-8")
        self.assertIn('class="qin"', html)
        self.assertIn('addEventListener("pointerdown"', js)
        self.assertIn("compositionend", js)
        # 変換を確定しても候補が消えたままにならないこと (確定後に再表示、blur の消去タイマーは解除可能に)
        self.assertIn("compositionstart", js)
        self.assertIn("sugHideTimer", js)
        self.assertIn('addEventListener("focus"', js)
        self.assertIn(".qin{position:relative", css)
        self.assertIn("font-size:16px", css)
        # 候補リストがヒーローの下端で切り取られないこと (overflow:hidden があると
        # 絶対配置の候補がヒーローの箱の外側で見えなくなる)
        import re
        hero = re.search(r"\.hero\{([^}]*)\}", css).group(1)
        self.assertNotIn("overflow:hidden", hero)

    def test_pre_stage_final_flows_to_site(self):
        def fn(p):
            if "根拠抽出」担当" in p:
                return json.dumps(EXTRACTION)
            o = assessment(LONG_KEYS, {"technology_readiness": 80, "technological_advantage": 60,
                                       "monetization": None, "market_growth": None,
                                       "financial_sustainability": None, "moat_durability": None})
            o["trl_level"] = {"level": 2, "reason": "r", "evidence": [{"ref": "u", "summary": "s", "kind": "fact"}]}
            return json.dumps(o)
        data = self.build(Mock(fn), n=1)
        c = data["candidates"][0]
        self.assertEqual(c["display_status"], "final")
        self.assertEqual(c["long"]["total"], 73.3)
        self.assertEqual(c["long"]["total_scope"], "technology")
        self.assertEqual(c["long"]["business_stage"], "pre_stage")

    def test_llm_model_flows_to_export(self):
        # 評価の来歴 (実際に採点したモデル) をサイトでも判別できるよう export に載せる
        data = self.build(Mock(make_llm_fn(long_scores={k: 65 for k in LONG_KEYS})))
        evs = [c["long"] for c in data["candidates"] if c["long"]]
        self.assertTrue(evs)
        self.assertTrue(all(e["llm_model"] == "mock-1" for e in evs))
        self.assertTrue(all(e["llm_provider"] == "mock" for e in evs))
        # 一覧カードと詳細 (ドロワー・専用ページ) の両方がモデル名を表示する
        app_js = (T.ROOT / "web" / "app.js").read_text(encoding="utf-8")
        detail = (T.ROOT / "web" / "detail.html").read_text(encoding="utf-8")
        self.assertIn("llm_model", app_js)
        self.assertIn("llm_model", detail)

    def test_stats_eight_boxes_summary_and_next_up(self):
        # 統計8枠化: 除外は掲載候補に含まれないため summary で別集計し、
        # 次回評価予定は候補の next_up フラグから数える (表示側)
        data = self.build(Mock(make_llm_fn(long_scores={k: 65 for k in LONG_KEYS})))
        self.assertIn("summary", data)
        self.assertEqual(data["summary"]["rejected"], 0)
        rec, _ = T.normalize(raw(sid="2610.9999", title="Rejected paper", published="2026-10-19"),
                             T.now_iso())
        cid, _ = self.store.upsert_candidate(rec, "rX")
        self.store.set_status(cid, "rejected")
        data = T.build_site_data(self.store, cfg_with(), datetime(2026, 10, 26, tzinfo=timezone.utc))
        self.assertEqual(data["summary"]["rejected"], 1)
        # 除外も理由つきで掲載する (状態フィルタで選んだときだけ一覧に出る)
        rej = [c for c in data["candidates"] if c["id"] == cid]
        self.assertEqual(len(rej), 1)
        self.assertEqual(rej[0]["display_status"], "rejected")
        self.assertIn("screen_reason", rej[0])
        html = (T.ROOT / "web" / "index.html").read_text(encoding="utf-8")
        # 次回評価予定は独立枠にせず未評価枠の中に併記し、8枠目は TRL判定済み
        self.assertIn('id="c4n"', html)
        self.assertIn("うち次回予定", html)
        self.assertIn("TRL判定済み", html)
        self.assertIn('id="c6"', html)  # TRL判定済み
        self.assertIn('id="c7"', html)  # 除外
        app = (T.ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('set("#c7", n(["rejected"]))', app)  # 要素欠けに強い set() 経由で書く
        self.assertIn("screen_reason", app)
        self.assertIn("c4n", app)
        self.assertIn("lvl(p) != null", app)

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


class ZeroRiskBatchTests(unittest.TestCase):
    """ゼロリスク改善バッチ (完全性・機密性・ACID・観測) のテスト。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = T.Store(Path(self.tmp.name) / "d" / "trl.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def build(self, n=1):
        cfg = cfg_with()
        T.run_pipeline(self.store, cfg, Mock(make_llm_fn(long_scores={k: 65 for k in LONG_KEYS})),
                       as_of=AS_OF, kinds=("long",), fetchers=fetch_n(n), sources=("arxiv",),
                       today=AS_OF, log=lambda *_: None)
        return T.build_site_data(self.store, cfg, datetime(2026, 10, 26, tzinfo=timezone.utc))

    def test_export_keys_stay_within_whitelist(self):
        # 公開 data.json に内部情報が混ざらないよう、候補と評価のキーを固定する。
        # 新しいキーを足すときはこのホワイトリストも更新し、意図的な追加にする
        data = self.build()
        cand_keys = {"id", "title", "source", "published", "url", "abstract", "fields",
                     "title_ja", "abstract_ja", "translation_src", "screen_reason", "retrieved_at",
                     "display_status", "eval_count", "next_up", "long", "event",
                     "entities", "related_news", "related_financials", "queue"}
        ev_keys = {"status", "total", "band", "as_of", "evaluated_at", "items",
                   "critical_missing", "errors", "trl_level", "direction", "axes",
                   "business_stage", "total_scope", "llm_provider", "llm_model",
                   "prev", "hold_cause", "total_orig", "band_orig"}
        for c in data["candidates"]:
            self.assertLessEqual(set(c), cand_keys)
            if c["long"]:
                self.assertLessEqual(set(c["long"]), ev_keys)

    def test_eval_count_counts_scored_long_evaluations(self):
        data = self.build()
        cand = data["candidates"][0]
        self.assertEqual(cand["eval_count"], 1)  # 初回の final が1回分
        ev2 = {"kind": "long", "as_of": AS_OF.isoformat(), "status": "hold", "total": None, "band": None,
               "items": {}, "extraction": None, "event": None, "prompt_versions": {},
               "config_version": "x", "config_hash": "x", "llm_provider": "gemini",
               "llm_model": "m", "input_hash": "h2", "errors": [], "extra": {}}
        self.store.add_evaluation("r9", cand["id"], ev2)
        ev3 = dict(ev2, status="error", errors=["LLMError: invalid JSON"], input_hash="h3")
        self.store.add_evaluation("r10", cand["id"], ev3)
        data2 = T.build_site_data(self.store, cfg_with(), datetime(2026, 10, 26, tzinfo=timezone.utc))
        cand2 = [c for c in data2["candidates"] if c["id"] == cand["id"]][0]
        self.assertEqual(cand2["eval_count"], 2)  # error は数えない

    def test_error_strings_redacted_in_export(self):
        row = {"kind": "long", "extra_json": "{}", "status": "error", "total": None,
               "band": None, "as_of": "2026-10-20", "evaluated_at": "2026-10-20T00:00:00+00:00",
               "items_json": "{}",
               "errors_json": json.dumps(["HttpError: https://x.test/?key=ABC12345 secret=SECRET_DUMMY_123"])}
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "SECRET_DUMMY_123"}):
            out = T._ev_for_site(row, CFG)
        self.assertEqual(len(out["errors"]), 1)
        self.assertNotIn("SECRET_DUMMY_123", out["errors"][0])
        self.assertNotIn("ABC12345", out["errors"][0])
        self.assertIn("***", out["errors"][0])

    def test_orphan_report_detects_dangling_rows(self):
        self.store.conn.execute(
            "INSERT INTO identifiers(key, candidate_id) VALUES ('k1', 'ghost')")
        self.store.conn.execute(
            "INSERT INTO evaluations(candidate_id, kind) VALUES ('ghost', 'long')")
        self.store.conn.commit()
        self.assertEqual(self.store.orphan_report(), {"identifiers": 1, "evaluations": 1})

    def test_defer_commits_bundles_and_rolls_back(self):
        rec, _ = T.normalize(raw(sid="2610.7777", title="Tx paper"), T.now_iso())
        with self.assertRaises(RuntimeError):
            with self.store.defer_commits():
                cid, _ = self.store.upsert_candidate(rec, "rX")
                raise RuntimeError("boom")
        n = self.store.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        self.assertEqual(n, 0)  # 途中終了では部分的に残らない
        with self.store.defer_commits():
            self.store.upsert_candidate(rec, "rX")
        n = self.store.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        self.assertEqual(n, 1)

    def test_export_guard_blocks_broken_output(self):
        self.build()
        out_dir = Path(self.tmp.name) / "site"
        with mock.patch.object(site_mod, "build_site_data", return_value={"candidates": []}):
            with self.assertRaises(RuntimeError):
                T.export_site(self.store, cfg_with(), out_dir)
        self.assertFalse((out_dir / "data.json").exists())

    def test_copy_buttons_for_original_and_translation(self):
        # 原文・日本語訳のコピーはブラウザ内だけで完結する (一覧ドロワーと詳細の両方)
        app = (T.ROOT / "web" / "app.js").read_text(encoding="utf-8")
        detail = (T.ROOT / "web" / "detail.html").read_text(encoding="utf-8")
        for text in (app, detail):
            self.assertIn("原文をコピー", text)
            self.assertIn("日本語訳をコピー", text)
            self.assertIn("出典をコピー", text)
            self.assertIn("navigator.clipboard", text)

    def test_personal_view_features(self):
        # 自分の表示 (新着バッジ・注目ピン・カードからの直接コピー) と
        # 評価まるごとコピー。状態は端末内 (localStorage) だけで完結し、
        # 注目ピンは共有リンク (?pins=) で別端末へ持ち運べる
        app = (T.ROOT / "web" / "app.js").read_text(encoding="utf-8")
        detail = (T.ROOT / "web" / "detail.html").read_text(encoding="utf-8")
        index = (T.ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("trl-last-visit", app)
        self.assertIn("新着", app)
        self.assertIn("trl-pins", app)
        self.assertIn("data-pin", app)
        self.assertIn("?pins=", app)
        # 新着の基準時刻も同じ共有リンク (?seen=) で持ち運べる (既読の統合)
        self.assertIn("seen=", app)
        self.assertIn("data-ccite", app)
        self.assertIn("data-clink", app)
        self.assertIn("自分の表示", index)
        self.assertIn("注目・新着の共有リンクをコピー", index)
        for text in (app, detail):
            self.assertIn("評価をコピー", text)
            self.assertIn("evalText", text)

    def test_quiet_zero_source_recorded(self):
        cfg = cfg_with()
        res = T.run_pipeline(self.store, cfg, None, as_of=AS_OF, kinds=("long",),
                             fetchers={"arxiv": lambda q, k, days, as_of: []}, sources=("arxiv",),
                             today=AS_OF, log=lambda *_: None)
        counts = json.loads(self.store.conn.execute(
            "SELECT counts_json FROM runs WHERE run_id = ?", (res["run_id"],)).fetchone()[0])
        self.assertEqual(counts.get("quiet_zero_sources"), ["arxiv"])
        # 取得がある通常の実行では記録されない
        data = self.build()  # noqa: F841 (実行そのものが目的)
        rid = self.store.conn.execute(
            "SELECT run_id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        counts2 = json.loads(self.store.conn.execute(
            "SELECT counts_json FROM runs WHERE run_id = ?", (rid,)).fetchone()[0])
        self.assertNotIn("quiet_zero_sources", counts2)


class ShallowDepthTests(unittest.TestCase):
    """浅い日 (depth=shallow): 発見系ソースの取得窓だけを切り詰める。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = T.Store(Path(self.tmp.name) / "d" / "trl.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _run(self, depth):
        cfg = copy.deepcopy(CFG)
        seen = {}

        def rec(name):
            def f(q, n, days, as_of):
                seen.setdefault(name, set()).add(days)
                return []
            return f
        fetchers = {s: rec(s) for s in ("pubmed", "trials", "yahoo", "edgar")}
        T.run_pipeline(self.store, cfg, Mock(make_llm_fn()), as_of=AS_OF,
                       sources=("pubmed", "trials", "yahoo", "edgar"),
                       fetchers=fetchers, today=AS_OF, log=lambda *_: None,
                       depth=depth)
        return seen

    def test_shallow_caps_discovery_windows_only(self):
        seen = self._run("shallow")
        self.assertEqual(seen.get("pubmed"), {4})
        self.assertEqual(seen.get("trials"), {4})
        self.assertEqual(seen.get("yahoo"), {400})  # 財務系は窓を触らない
        self.assertEqual(seen.get("edgar"), {400})

    def test_deep_keeps_configured_windows(self):
        seen = self._run("deep")
        self.assertIn(365, seen.get("pubmed", set()))
        self.assertNotIn(4, seen.get("pubmed", set()))
        self.assertEqual(seen.get("trials"), {180})


class CompareModelsTests(unittest.TestCase):
    """モデル比較 (compare): 同じ候補・同じ抽出でモデル別の採点を比べる。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = T.Store(Path(self.tmp.name) / "d" / "trl.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _baseline(self, n=2):
        cfg = cfg_with()
        T.run_pipeline(self.store, cfg, Mock(make_llm_fn(long_scores={k: 65 for k in LONG_KEYS})),
                       as_of=AS_OF, kinds=("long",), fetchers=fetch_n(n), sources=("arxiv",),
                       today=AS_OF, log=lambda *_: None)
        return cfg

    def test_compare_scores_same_candidates_with_each_model(self):
        cfg = self._baseline(2)
        hi = Mock(make_llm_fn(long_scores={k: 65 for k in LONG_KEYS}))
        lo = Mock(make_llm_fn(long_scores={k: 40 for k in LONG_KEYS}))
        res = T.run_compare(self.store, cfg, [("gemini/hi", hi), ("gemini/lo-lite", lo)],
                            as_of=AS_OF, log=lambda *_: None)
        rows = self.store.compare_results(res["batch"])
        self.assertEqual(len(rows), 4)  # 2候補 × 2モデル
        for r in rows:
            self.assertEqual(r["baseline_status"], "final")
            self.assertEqual(r["baseline_total"], 65)
        self.assertTrue(all(r["total"] == 65 for r in rows if r["label"] == "gemini/hi"))
        self.assertTrue(all(r["total"] == 40 for r in rows if r["label"] == "gemini/lo-lite"))
        # 比較は本体の評価に触れない
        n_ev = self.store.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
        self.assertEqual(n_ev, 2)
        report = T.build_compare_report(self.store, res["batch"])
        self.assertIn("gemini/hi", report)
        self.assertIn("状態一致", report)

    def test_compare_extracts_for_candidates_without_baseline(self):
        cfg = self._baseline(1)
        rec, _ = T.normalize(raw(sid="2610.5555", title="Fresh paper"), T.now_iso())
        cid, _ = self.store.upsert_candidate(rec, "rX")
        self.store.set_status(cid, "screened")
        a = Mock(make_llm_fn(long_scores={k: 60 for k in LONG_KEYS}))
        lite = Mock(make_llm_fn(long_scores={k: 60 for k in LONG_KEYS}))
        res = T.run_compare(self.store, cfg, [("gemini/a", a), ("gemini/b-lite", lite)],
                            as_of=AS_OF, log=lambda *_: None)
        rows = [r for r in self.store.compare_results(res["batch"]) if r["candidate_id"] == cid]
        self.assertEqual(len(rows), 2)  # 抽出を共有して両モデルで採点できた
        self.assertIsNone(rows[0]["baseline_status"])

    def test_compare_records_actual_model_and_flags_mismatch(self):
        # 名札と実際に応答したモデルを照合できる (フォールバック混入の検出)
        cfg = self._baseline(2)
        good = Mock(make_llm_fn(long_scores={k: 60 for k in LONG_KEYS}))
        res = T.run_compare(self.store, cfg, [("mock/mock-1", good)],
                            as_of=AS_OF, log=lambda *_: None)
        rows = self.store.compare_results(res["batch"])
        self.assertTrue(rows)
        self.assertTrue(all(r["actual_model"] == "mock/mock-1" for r in rows))
        self.assertNotIn("名札と実際に応答したモデルが異なる",
                         T.build_compare_report(self.store, res["batch"]))
        bad = Mock(make_llm_fn(long_scores={k: 60 for k in LONG_KEYS}))
        res2 = T.run_compare(self.store, cfg, [("gemini/not-the-model", bad)],
                             as_of=AS_OF, log=lambda *_: None)
        rows2 = self.store.compare_results(res2["batch"])
        self.assertTrue(all(r["actual_model"] == "mock/mock-1" for r in rows2))
        self.assertIn("名札と実際に応答したモデルが異なる",
                      T.build_compare_report(self.store, res2["batch"]))

    def test_compare_budget_is_sliced_per_model(self):
        # 遅いモデルが全体の予算を食わず、速いモデルは自分の取り分で全件測れる
        cfg = self._baseline(3)
        import time as _time
        base_fn = make_llm_fn(long_scores={k: 60 for k in LONG_KEYS})

        def slow_fn(prompt):
            _time.sleep(0.5)
            return base_fn(prompt)

        res = T.run_compare(self.store, cfg,
                            [("gemini/slow", Mock(slow_fn)), ("gemini/fast", Mock(base_fn))],
                            as_of=AS_OF, budget_sec=0.6, log=lambda *_: None)
        rows = self.store.compare_results(res["batch"])
        n_slow = sum(1 for r in rows if r["label"] == "gemini/slow")
        n_fast = sum(1 for r in rows if r["label"] == "gemini/fast")
        self.assertEqual(n_fast, 3)
        self.assertLessEqual(n_slow, 1)

    def test_compare_clients_have_no_fallback_ladder(self):
        # 本番の環境変数が残っていても比較クライアントは階段を持たない
        # (持つと名札と違うモデルが応答し、初回実行のように比較が汚染される)
        import os
        from unittest import mock as umock
        from trlcore import cli as trl_cli
        env = {"GEMINI_API_KEY": "dummy", "TRL_LLM_MODEL": "gemini-3.6-flash",
               "TRL_LLM_FALLBACK_MODEL": "gemini-3.5-flash-lite",
               "TRL_LLM_FALLBACK_MODEL_2": "gemini-3.5-flash",
               "CLOUDFLARE_ACCOUNT_ID": "", "CLOUDFLARE_API_TOKEN": ""}
        with umock.patch.dict(os.environ, env):
            clients = trl_cli._compare_clients(cfg_with())
        self.assertEqual([label for label, _ in clients],
                         ["gemini/gemini-3.6-flash", "gemini/gemini-3.5-flash-lite",
                          "gemini/gemini-3.5-flash"])
        for _label, client in clients:
            self.assertEqual(client.fallback_models, [])


class SiteScoreCoercionTests(unittest.TestCase):
    def test_string_score_is_exported_as_none(self):
        # 旧データ等で items に文字列スコアが残っていても、サイト側へは数値以外を出さない
        ev_row = {"kind": "long", "extra_json": "{}", "status": "error", "total": None,
                  "band": None, "as_of": "2026-10-20", "evaluated_at": "2026-10-20T00:00:00+00:00",
                  "errors_json": "[]",
                  "items_json": json.dumps({"technology_readiness": {
                      "score": "<b>99</b>", "reason": "r", "evidence": [],
                      "counter_evidence": [], "missing_info": []}})}
        out = T._ev_for_site(ev_row, CFG)
        self.assertIsNone(out["items"]["technology_readiness"]["score"])


if __name__ == "__main__":
    unittest.main()
