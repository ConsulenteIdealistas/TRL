"""追加分のテスト(一時エラーの扱い・抽出再利用・export)。Web ページ自体の構造チェックは test_web.py に分離。実行: python -m unittest test_site -v"""
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
from testutil import (AS_OF, CFG, EXTRACTION, LONG_KEYS, Mock,
                      StoreFixture, assessment, cfg_with, fetch_n,
                      make_llm_fn, raw)


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

    def test_fallback_chain_has_only_one_fallback(self):
        # X-2: 階段は主 → 代替1 のみ。両段とも一時的失敗なら例外になる
        # (かつての代替2 = FALLBACK_MODEL_2 の配管は除去済み)
        calls = []

        def http(url, **kw):
            for name in ("gemini-main", "gemini-sub"):
                if name in url:
                    calls.append(name)
                    raise T.HttpError("HTTP 503 for x", 503)
            raise AssertionError(url)
        g = T.Gemini(model="gemini-main", http=http, fallback_model="gemini-sub")
        self.assertEqual(g.fallback_models, ["gemini-sub"])
        self.assertEqual(g.fallback_model, "gemini-sub")  # 互換プロパティは1段目
        with self.assertRaises(T.HttpError):
            g.complete("p")
        self.assertEqual(calls, ["gemini-main", "gemini-sub"])

    def test_fallback_chain_stops_at_first_success(self):
        # 代替1 が応答すればそこで終わる
        calls = []

        def http(url, **kw):
            if "gemini-main" in url:
                calls.append("main")
                raise T.HttpError("HTTP 503 for x", 503)
            calls.append("sub")
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}).encode()
        g = T.Gemini(model="gemini-main", http=http, fallback_model="gemini-sub")
        self.assertEqual(g.complete("p"), "ok")
        self.assertEqual(calls, ["main", "sub"])

    def test_stage_breaker_skips_failed_main(self):
        # 主が落ち続けるとブレーカーで飛ばされ、代替1 へ直行する
        counts = {"gemini-main": 0, "gemini-sub": 0}

        def http(url, **kw):
            for name in ("gemini-main", "gemini-sub"):
                if name in url:
                    counts[name] += 1
                    if name == "gemini-main":
                        raise T.HttpError("HTTP 503 for x", 503)
                    return json.dumps({"candidates": [{"content": {"parts": [{"text": "fb"}]}}]}).encode()
            raise AssertionError(url)
        g = T.Gemini(model="gemini-main", http=http, fallback_model="gemini-sub")
        for _ in range(5):
            self.assertEqual(g.complete("p"), "fb")
        self.assertEqual(counts["gemini-main"], 1)  # 1回の失敗で見切られる
        self.assertEqual(counts["gemini-sub"], 5)

    def test_second_fallback_env_is_ignored(self):
        # X-2: 環境変数 TRL_LLM_FALLBACK_MODEL_2 はもう読まない (配管を除去)
        import os
        old = os.environ.get("TRL_LLM_FALLBACK_MODEL_2")
        os.environ["TRL_LLM_FALLBACK_MODEL_2"] = "gemini-env2"
        try:
            g = T.Gemini(model="gemini-main", http=lambda *a, **k: b"{}", fallback_model="gemini-sub")
            self.assertEqual(g.fallback_models, ["gemini-sub"])
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


class LlmCallHardeningTests(unittest.TestCase):
    """LLM は Gemini のみ (第2プロバイダと Anthropic は 2026-11-06 に廃止)。
    失敗時の挙動 (429 は再試行しない・候補ごとの総時間予算) を固定する。"""

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
    def gemini_ok(text="ok"):
        return json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}],
                           "usageMetadata": {"promptTokenCount": 10,
                                             "candidatesTokenCount": 3}}).encode()

    def test_provider_ignores_cloudflare_credentials(self):
        # 資格情報がそろっていても第2プロバイダは足さない (廃止済み)
        llm = T.get_provider("gemini", model="gemini-main",
                             http=lambda *a, **k: self.gemini_ok())
        self.assertIsInstance(llm, T.Gemini)
        self.assertEqual(llm.complete("p", purpose="extract", candidate_id=3), "ok")

    def test_removed_providers_are_rejected(self):
        for name in ("cloudflare", "anthropic"):
            with self.assertRaises(T.LLMError):
                T.get_provider(name, http=lambda *a, **k: b"{}")

    def test_gemini_http_call_disables_429_retry(self):
        seen = {}

        def http(url, **kw):
            seen.update(kw)
            return self.gemini_ok()
        T.Gemini(model="m", http=http).complete("p")
        self.assertIs(seen.get("retry_429"), False)

    def test_http_fetch_429_retry_flag(self):
        import urllib.error
        calls = []

        def opener(req, timeout=None):
            calls.append(1)
            raise urllib.error.HTTPError("https://x", 429, "too many", None, None)
        with self.assertRaises(T.HttpError):
            T.http_fetch("https://x", retries=3, retry_429=False,
                         opener=opener, sleep=lambda *_: None)
        self.assertEqual(len(calls), 1)  # 無効時は聞き直さない
        calls.clear()
        with self.assertRaises(T.HttpError):
            T.http_fetch("https://x", retries=3, opener=opener, sleep=lambda *_: None)
        self.assertEqual(len(calls), 4)  # 既定 (収集系) は従来どおり再試行する

    def test_candidate_budget_caps_deadline_and_expires(self):
        import time as _time
        seen = {}

        def http(url, **kw):
            seen.update(kw)
            return self.gemini_ok()
        g = T.Gemini(model="m", http=http)
        g.call_budget = 999  # 呼び出し予算より候補予算が小さい状況を作る
        g.set_candidate_budget(300)
        self.assertEqual(g.complete("p"), "ok")
        self.assertLessEqual(seen["deadline"], 300)
        self.assertGreater(seen["deadline"], 240)
        g.set_candidate_budget(0.05)
        _time.sleep(0.06)
        with self.assertRaises(T.HttpError) as ctx:
            g.complete("p")
        self.assertTrue(ctx.exception.deadline_exceeded)
        g.set_candidate_budget(None)
        self.assertEqual(g.complete("p"), "ok")

    def test_configure_llm_applies_settings(self):
        llm = T.get_provider("gemini", model="m", http=lambda *a, **k: b"{}")
        T.configure_llm(llm, cfg_with(timeout_sec=77, retries=5, call_budget_sec=99))
        self.assertEqual((llm.timeout, llm.retries, llm.call_budget), (77, 5, 99))
        fn = lambda rec: None
        llm.recorder = fn
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
        self.assertEqual(T.llm_settings(CFG)["retries"], 1)
        self.assertEqual(T.llm_settings(CFG)["candidate_budget_sec"], 300)

    def test_bad_llm_settings_rejected(self):
        for bad in ({"timeout_sec": 0}, {"retries": -1}, {"nope": 1}, {"timeout_sec": True}):
            c = copy.deepcopy(CFG)
            c["scoring"]["llm"] = bad
            with self.assertRaises(T.ConfigError):
                T.validate_config(c)

    def test_configure_llm_applies_settings(self):
        class P:
            timeout, retries = 120, 2
        p = T.configure_llm(P(), CFG)
        self.assertEqual((p.timeout, p.retries), (120, 1))
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


class ExportTests(StoreFixture):


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

    def test_audit_catalog_exported(self):
        # 監査カタログは export に同梱され、全28チェックが正本どおりに届く
        # (criteria.html の一覧と audit.js のチェック名の表示元)
        data = T.build_site_data(self.store, cfg_with(),
                                 datetime(2026, 10, 26, tzinfo=timezone.utc))
        cat = data["audit_catalog"]
        self.assertEqual(len(cat["checks"]), 28)
        by_id = {c["id"]: c for c in cat["checks"]}
        self.assertEqual(by_id["A1"]["name"], "原則2: 件数・期間の推測")
        self.assertIn("79", by_id["A15"]["threshold"])
        self.assertFalse(by_id["A5"]["implemented"])

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

    def test_v3_trace_fields_exported_and_legacy_safe(self):
        # 凍結バッチ: v3 世代の trl_levels と項目コードは export に素通しし、
        # 旧世代の評価 (どちらも無い) では欠落したまま壊れない
        data = self.build(Mock(make_llm_fn(long_scores={k: 65 for k in LONG_KEYS})), n=1)
        cand = data["candidates"][0]
        self.assertIsNone(cand["long"]["trl_levels"])  # 旧形式の評価には無い
        self.assertNotIn("basis_claim_ids", cand["long"]["items"]["technology_readiness"])
        cid = cand["id"]
        ev2 = {"kind": "long", "as_of": AS_OF.isoformat(), "status": "final", "total": 65.0,
               "band": {"label": "strong"},
               "items": {k: {"score": 65, "reason": "r",
                             "evidence": [{"ref": "https://example.org/x", "summary": "s",
                                           "kind": "fact"}],
                             "counter_evidence": [], "missing_info": []} for k in LONG_KEYS},
               "extraction": None, "event": None, "prompt_versions": {}, "config_version": "x",
               "config_hash": "x", "llm_provider": "gemini", "llm_model": "m", "input_hash": "h9",
               "errors": [],
               "extra": {"trl_level": {"level": 6, "reason": "r", "evidence": [], "missing_info": []},
                         "trl_levels": [{"subject": "主技術", "level": 6,
                                         "basis_claim_ids": ["C1"]}]}}
        ev2["items"]["technology_readiness"]["basis_claim_ids"] = ["C1"]
        ev2["items"]["technology_readiness"]["constraint_codes"] = ["single_source"]
        self.store.add_evaluation("r9", cid, ev2)
        data2 = T.build_site_data(self.store, cfg_with(), datetime(2026, 10, 26, tzinfo=timezone.utc))
        long2 = [c for c in data2["candidates"] if c["id"] == cid][0]["long"]
        self.assertEqual(long2["trl_levels"], [{"subject": "主技術", "level": 6,
                                                "basis_claim_ids": ["C1"]}])
        self.assertEqual(long2["items"]["technology_readiness"]["basis_claim_ids"], ["C1"])
        self.assertEqual(long2["items"]["technology_readiness"]["constraint_codes"],
                         ["single_source"])
        self.assertNotIn("constraint_codes", long2["items"]["monetization"])  # 非空のときだけ付く

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

    def test_config_includes_reference_items(self):
        # 参考項目 (規程第13条) は config に同梱され、重み・必須には現れない
        data = self.build(Mock(make_llm_fn()))
        self.assertEqual(data["config"]["reference_items"], ["management_execution"])
        self.assertNotIn("management_execution", data["config"]["long_weights"])

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

    def test_export_has_no_browser_summary_key(self):
        # 旧ブラウザ方式 (公開ページへのキー差し込み) の残骸が無いこと。
        # 要点は抽出型 (trlcore/summarize) に置き換えた。
        self.build(Mock(make_llm_fn()))
        out = Path(self.tmp.name) / "site"
        T.export_site(self.store, cfg_with(), out)
        detail = (out / "detail.html").read_text(encoding="utf-8")
        self.assertNotIn("__TRL_SUM_KEY__", detail)
        self.assertNotIn("generativelanguage", detail)


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

    def test_audit_attached_to_latest_long_evaluation(self):
        # 監査レビュー (段階A): 最新の long 評価に紐づく監査 (規則版・LLM 版)
        # が候補の audit キーに同梱され、監査の無い候補には付かない
        data = self.build(Mock(make_llm_fn(long_scores={k: 65 for k in LONG_KEYS})))
        target = next(c for c in data["candidates"] if c["long"])
        cid = target["id"]
        eid = self.store.latest_evaluation(cid, "long")["id"]
        self.store.add_audit_result({
            "evaluation_id": eid, "candidate_id": cid, "run_id": "aud-r1",
            "auditor_provider": "rules", "auditor_model": "rules/v1",
            "prompt_version": "v1", "prompt_sha": "rulesha",
            "flags": [
                {"check": "A1", "item": "technology_readiness", "verdict": "flag",
                 "rule": "tech_continuation_claim", "detail": "技術項目で継続の使用実績を主張"},
                {"check": "A7", "item": "monetization", "verdict": "gray",
                 "rule": None, "detail": "減点理由が漠然としている"}],
            "summary": "要確認1件・グレー1件"})
        self.store.add_audit_result({
            "evaluation_id": eid, "candidate_id": cid, "run_id": "aud-l1",
            "auditor_provider": "gemini", "auditor_model": "gemini-3.6-flash",
            "prompt_version": "v1", "prompt_sha": "llmsha",
            "flags": [{"check": "A5", "item": "monetization", "detail": "点と理由が食い違う"}],
            "summary": "整合に疑い"})
        data = T.build_site_data(self.store, cfg_with(), datetime(2026, 10, 26, tzinfo=timezone.utc))
        self.assertEqual(data["schema_version"], 3)  # 追加キーのみで版は上げない
        c = next(x for x in data["candidates"] if x["id"] == cid)
        schema = json.loads((T.ROOT / "web" / "schema.json").read_text(encoding="utf-8"))
        by_auditor = {a["auditor"]: a for a in c["audit"]}
        rules = by_auditor["rules/v1"]
        for k in schema["audit"]:
            self.assertIn(k, rules)
        self.assertEqual(rules["evaluation_id"], eid)
        self.assertEqual(rules["provider"], "rules")
        self.assertEqual(rules["version"], "v1")
        self.assertEqual(rules["summary"], "要確認1件・グレー1件")
        e0 = rules["entries"][0]
        for k in schema["audit_entry"]:
            self.assertIn(k, e0)
        self.assertEqual((e0["check"], e0["item"], e0["verdict"], e0["rule"]),
                         ("A1", "technology_readiness", "flag", "tech_continuation_claim"))
        self.assertEqual(rules["entries"][1]["verdict"], "gray")
        llm = by_auditor["gemini-3.6-flash"]
        # LLM 版の entries は verdict/rule を持たない → verdict は flag (要確認) 扱い、rule は None
        self.assertEqual(llm["entries"][0]["verdict"], "flag")
        self.assertIsNone(llm["entries"][0]["rule"])
        others = [x for x in data["candidates"] if x["id"] != cid]
        self.assertTrue(others)
        self.assertTrue(all("audit" not in x for x in others))

    def test_audit_event_attached_to_latest_event_evaluation(self):
        # イベント評価の監査は audit_event に同梱される (長期側の audit とは別枠)
        data = self.build(Mock(make_llm_fn(long_scores={k: 65 for k in LONG_KEYS})))
        target = next(c for c in data["candidates"] if c["long"])
        cid = target["id"]
        ev = {"kind": "event", "as_of": "2026-10-25", "status": "hold",
              "total": None, "band": None,
              "items": {}, "extraction": EXTRACTION, "event": None, "prompt_versions": {},
              "config_version": CFG["config_version"], "config_hash": CFG["config_hash"],
              "llm_provider": "mock", "llm_model": "mock-1",
              "input_hash": "h-ev", "errors": [], "extra": {}}
        eid = self.store.add_evaluation("r-ev", cid, ev)
        self.store.add_audit_result({
            "evaluation_id": eid, "candidate_id": cid, "run_id": "aud-ev",
            "auditor_provider": "rules", "auditor_model": "rules/v3",
            "prompt_version": "v3", "prompt_sha": "rulesha3",
            "flags": [{"check": "E2", "item": "not_priced_in", "verdict": "gray",
                       "rule": "reaction_unused_hold", "detail": "価格反応があるのに未判定"}],
            "summary": "グレー1件"})
        data = T.build_site_data(self.store, cfg_with(), datetime(2026, 10, 26, tzinfo=timezone.utc))
        c = next(x for x in data["candidates"] if x["id"] == cid)
        self.assertIn("audit_event", c)
        blk = c["audit_event"][0]
        self.assertEqual(blk["evaluation_id"], eid)
        self.assertEqual(blk["entries"][0]["check"], "E2")
        self.assertNotIn("audit", c)  # long 側に監査は無いので混ざらない

    def test_audit_on_superseded_evaluation_not_exported(self):
        # 監査が付いた評価より新しい long 評価が付いたら、その監査は
        # 最新評価への監査ではないので同梱しない (処分は表示中の評価へのもの)
        data = self.build(Mock(make_llm_fn(long_scores={k: 65 for k in LONG_KEYS})), n=1)
        cid = data["candidates"][0]["id"]
        eid = self.store.latest_evaluation(cid, "long")["id"]
        self.store.add_audit_result({
            "evaluation_id": eid, "candidate_id": cid, "run_id": "aud-r1",
            "auditor_provider": "rules", "auditor_model": "rules/v1",
            "prompt_version": "v1", "prompt_sha": "rulesha",
            "flags": [{"check": "A2", "item": "trl_level", "verdict": "flag",
                       "rule": "trl9_no_recurrence", "detail": "継続の証拠が見当たらない"}],
            "summary": ""})
        ev = {"kind": "long", "as_of": "2026-10-25", "status": "final",
              "total": 66.0, "band": {"label": "順調"},
              "items": assessment(LONG_KEYS, {k: 66 for k in LONG_KEYS})["items"],
              "extraction": EXTRACTION, "event": None, "prompt_versions": {},
              "config_version": CFG["config_version"], "config_hash": CFG["config_hash"],
              "llm_provider": "mock", "llm_model": "mock-1",
              "input_hash": "h-newer", "errors": [], "extra": {}}
        self.store.add_evaluation("r-new", cid, ev)
        data = T.build_site_data(self.store, cfg_with(), datetime(2026, 10, 26, tzinfo=timezone.utc))
        c = next(x for x in data["candidates"] if x["id"] == cid)
        self.assertNotIn("audit", c)


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
                     "entities", "related_news", "related_financials", "queue", "summary"}
        ev_keys = {"status", "total", "band", "as_of", "evaluated_at", "items",
                   "critical_missing", "errors", "trl_level", "trl_levels", "direction", "axes",
                   "business_stage", "total_scope", "llm_provider", "llm_model",
                   "prev", "hold_cause", "total_orig", "band_orig",
                   # 世代 (gen) と関門 (barriers) は表示専用の意図的な追加
                   "gen", "barriers",
                   # 付記イベント (annex、v5 世代) も意図的な追加
                   "annex"}
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

    def test_orphan_report_covers_newer_tables(self):
        # AC-4: 後から増えたテーブル群 (監査・レビュー・tier) も監視対象
        self.store.conn.execute(
            "INSERT INTO audit_results(evaluation_id, candidate_id) VALUES (999, 'ghost')")
        self.store.conn.execute(
            "INSERT INTO review_decisions(evaluation_id, audit_id) VALUES (999, 999)")
        self.store.conn.execute(
            "INSERT INTO evaluation_adjustments(evaluation_id) VALUES (999)")
        self.store.conn.execute(
            "INSERT INTO reeval_queue(candidate_id) VALUES ('ghost')")
        self.store.conn.execute(
            "INSERT INTO tier_snapshots(entity_id) VALUES ('ghost-entity')")
        self.store.conn.commit()
        out = self.store.orphan_report()
        # audit_results は evaluation_id と candidate_id の2参照とも孤児で合算2
        self.assertEqual(out["audit_results"], 2)
        # review_decisions は evaluation_id と audit_id の2参照とも孤児で合算2
        self.assertEqual(out["review_decisions"], 2)
        self.assertEqual(out["evaluation_adjustments"], 1)
        self.assertEqual(out["reeval_queue"], 1)
        self.assertEqual(out["tier_snapshots"], 1)

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
        # 保留の内訳は見出しと同じ母数 (display_status が hold のもの) だけを
        # 数える (評価 hold でも review 表示のものまで数えると内訳が合わない)
        self.assertIn('p.display_status === "hold" && p.long', app)
        self.assertIn("企業を特定できず", index)
        self.assertIn("根拠不足", index)
        # 採点基準へのリンクは一覧では検索と統計の間 (.pagelinks) に、
        # 詳細ではフッターに置く (ヘッダーはタイトル周りを空ける)
        self.assertIn('<a href="criteria.html">採点基準</a>', index)
        self.assertIn('<a href="criteria.html">採点基準（評価規程と重み）</a>', detail)
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

    def test_compare_candidates_unlimited_by_default(self):
        # TH-2: 既定は上限なし (全対象)。明示指定時のみ limit が効く
        cfg = self._baseline(3)
        all_ids = T.select_compare_candidates(self.store)
        self.assertEqual(len(all_ids), 3)
        self.assertEqual(T.select_compare_candidates(self.store, None), all_ids)
        self.assertEqual(len(T.select_compare_candidates(self.store, 2)), 2)
        res = T.run_compare(self.store, cfg, [("gemini/a", Mock(make_llm_fn(
            long_scores={k: 60 for k in LONG_KEYS})))],
            as_of=AS_OF, log=lambda *_: None)
        self.assertEqual(res["selected"], 3)  # run_compare の既定も上限なし

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
        # X-2: TRL_LLM_FALLBACK_MODEL_2 はもう読まないため、環境変数に
        # 残っていてもクライアントは生まれない
        self.assertEqual([label for label, _ in clients],
                         ["gemini/gemini-3.6-flash", "gemini/gemini-3.5-flash-lite"])
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


class SummaryExtractTests(unittest.TestCase):
    """抽出型の要点 (trlcore/summarize) の単体テスト。決定的であることが前提。"""

    STRUCTURED = (
        "BACKGROUND: Cancer therapy remains challenging for many patients worldwide today. "
        "METHODS: We enrolled 120 patients and administered the drug daily for six months. "
        "RESULTS: The response rate was 68 percent compared with 31 percent in the control group. "
        "RESULTS: Median survival improved from 8 months to 14 months with treatment. "
        "CONCLUSIONS: The drug significantly improves survival and response in this population.")

    def test_short_abstract_gets_no_summary(self):
        from trlcore import summarize
        self.assertIsNone(summarize.extract_summary(
            "T", "One sentence is here now. Two sentences follow this one. Three at this point. Four at last."))

    def test_structured_results_preferred(self):
        from trlcore import summarize
        got = summarize.extract_summary("Drug trial", self.STRUCTURED)
        self.assertIsNotNone(got)
        self.assertIn("68 percent", got)
        self.assertIn("significantly improves", got)

    def test_deterministic_and_from_original(self):
        from trlcore import summarize
        a = summarize.extract_summary("Drug trial", self.STRUCTURED)
        b = summarize.extract_summary("Drug trial", self.STRUCTURED)
        self.assertEqual(a, b)
        original = set()
        for _lab, body in summarize._split_sections(self.STRUCTURED):
            original.update(summarize._split_sentences(body, False))
        for s in summarize._split_sentences(a, False):
            self.assertIn(s, original)

    def test_japanese_abstract(self):
        from trlcore import summarize
        ja = ("本研究は固形がん患者に対する新薬の効果を検証した。"
              "対象は120名の患者で、6か月間毎日投与を行った。"
              "奏効率は68%であり、対照群の31%を大きく上回った。"
              "生存期間の中央値は8か月から14か月に延長した。"
              "副作用は概ね軽度であり、重篤な有害事象は認められなかった。"
              "以上より、本薬剤は有効な治療選択肢であると考えられる。")
        got = summarize.extract_summary("新薬の試験", ja)
        self.assertIsNotNone(got)
        self.assertIn("68%", got)


class GenBarrierExportTests(StoreFixture):
    """世代 (gen) と関門 (barriers) の export (ISSUES-2 の G-2、barriers/v1)。

    世代の正本は as_evaluated の config_version で、評価行の版は recalc で
    付け替わりうるため次善のフォールバックに留める。barriers は long
    final 評価にだけ build 側で付与する一次情報からの導出 (DESIGN-V2)。"""

    def _row(self, **kw):
        row = {"kind": "long", "status": "final", "total": 55.0,
               "band": "中程度・不確実", "as_of": "2026-11-01",
               "evaluated_at": "2026-11-01T00:00:00+00:00",
               "items_json": "{}", "errors_json": "[]",
               "extra_json": "{}", "prompt_versions_json": "{}",
               "config_version": "2026.11.4",
               "llm_provider": "gemini", "llm_model": "m"}
        row.update(kw)
        return row

    def test_gen_prefers_as_evaluated_over_row_version(self):
        # recalc で行の版が現行版に付け替わっていても、世代は当時の版を指す
        extra = {"as_evaluated": {"total": 50.0, "band": "中程度・不確実",
                                  "config_version": "2026.10.0"}}
        ev = self._row(extra_json=json.dumps(extra),
                       prompt_versions_json=json.dumps(
                           {"extract": {"name": "extract", "version": "v1"},
                            "long": {"version": "v2"}}))
        gen = site_mod._ev_for_site(ev, cfg_with())["gen"]
        self.assertEqual(gen["config"], "2026.10.0")
        self.assertEqual(gen["prompts"], {"extract": "v1", "long": "v2"})
        self.assertFalse(gen["current"])

    def test_gen_falls_back_to_row_version(self):
        gen = site_mod._ev_for_site(self._row(), cfg_with())["gen"]
        self.assertEqual(gen["config"], "2026.11.4")
        self.assertEqual(gen["prompts"], {})
        self.assertTrue(gen["current"])

    def test_gen_absent_without_any_version(self):
        ev = self._row(config_version=None)
        self.assertIsNone(site_mod._ev_for_site(ev, cfg_with())["gen"])

    def test_gen_survives_broken_prompt_versions(self):
        ev = self._row(prompt_versions_json="{broken")
        gen = site_mod._ev_for_site(ev, cfg_with())["gen"]
        self.assertEqual(gen["prompts"], {})

    def test_barriers_only_on_long_final_in_build(self):
        # _ev_for_site 単体では付けない (導出には DB の一次情報が要る)
        extra = {"trl_level": {"level": 6, "reason": "r"}}
        ev = self._row(extra_json=json.dumps(extra))
        self.assertNotIn("barriers", site_mod._ev_for_site(ev, cfg_with()))
        self.assertNotIn("phase", site_mod._ev_for_site(ev, cfg_with()))
        # build では long final に barriers が付く (ルール版つき)
        data = self.build(Mock(make_llm_fn()))
        longs = [c["long"] for c in data["candidates"] if c.get("long")]
        self.assertTrue(longs)
        for ev_out in longs:
            self.assertEqual(ev_out["barriers"]["v"], "barriers/v1")
            for k in ("devil_river", "valley_of_death", "darwinian_sea"):
                self.assertIn(ev_out["barriers"][k]["status"],
                              ("hit", "clear", "unknown", "na"))
            self.assertNotIn("phase", ev_out)

    def test_prev_gen_config_uses_same_source(self):
        extra = {"trl_level": {"level": 3},
                 "as_evaluated": {"total": 40.0, "band": "弱い",
                                  "config_version": "2026.09.0"}}
        prev = site_mod._prev_for_site(self._row(extra_json=json.dumps(extra)))
        self.assertEqual(prev["gen_config"], "2026.09.0")

    def test_build_attaches_current_gen(self):
        # パイプラインで作った評価には、現行世代として gen が付く
        data = self.build(Mock(make_llm_fn()))
        longs = [c["long"] for c in data["candidates"] if c.get("long")]
        self.assertTrue(longs)
        for ev in longs:
            self.assertIsNotNone(ev["gen"])
            self.assertEqual(ev["gen"]["config"], data["config"]["version"])
            self.assertTrue(ev["gen"]["current"])
            self.assertEqual(ev["gen"]["prompts"],
                             {"extract": "v3", "long": "v5"})
            self.assertIn("barriers", ev)
