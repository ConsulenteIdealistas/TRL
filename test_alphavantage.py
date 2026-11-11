"""Alpha Vantage 低速キュー財務補完収集のテスト (外部APIに依存しない)。
実行: python -m unittest test_alphavantage -v"""
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import trl as T
from trlcore.http import HttpError
from trlcore.normalize import normalize
from trlcore.sources import alphavantage as av

AS_OF = date(2026, 10, 20)

IS_DATA = {
    "symbol": "ONT.LON",
    "quarterlyReports": [
        {"fiscalDateEnding": "2026-06-30", "reportedCurrency": "GBP",
         "totalRevenue": "150000000", "operatingIncome": "20000000",
         "netIncome": "10000000"},
        {"fiscalDateEnding": "2026-03-31", "reportedCurrency": "GBP",
         "totalRevenue": "120000000", "operatingIncome": "None",
         "netIncome": "5000000"},
        # 400日の窓より古い期間は材料にしない
        {"fiscalDateEnding": "2025-03-31", "reportedCurrency": "GBP",
         "totalRevenue": "90000000", "operatingIncome": "8000000",
         "netIncome": "4000000"},
    ],
    "annualReports": [],
}
BS_DATA = {
    "symbol": "ONT.LON",
    "quarterlyReports": [
        {"fiscalDateEnding": "2026-06-30", "reportedCurrency": "GBP",
         "cashAndCashEquivalentsAtCarryingValue": "300000000",
         "totalAssets": "1000000000", "totalShareholderEquity": "600000000"},
        {"fiscalDateEnding": "2026-03-31", "reportedCurrency": "GBP",
         "cashAndCashEquivalentsAtCarryingValue": "280000000",
         "totalAssets": "950000000", "totalShareholderEquity": "570000000"},
    ],
}
CF_DATA = {
    "symbol": "ONT.LON",
    "quarterlyReports": [
        {"fiscalDateEnding": "2026-06-30", "reportedCurrency": "GBP",
         "operatingCashflow": "25000000", "capitalExpenditures": "5000000"},
        {"fiscalDateEnding": "2026-03-31", "reportedCurrency": "GBP",
         "operatingCashflow": "18000000", "capitalExpenditures": "4000000"},
    ],
}


def make_http(is_data=IS_DATA, bs_data=BS_DATA, cf_data=CF_DATA):
    calls = []

    def fake_http(url, params=None, headers=None, retries=None):
        calls.append((url, params, headers, retries))
        fn = (params or {}).get("function")
        table = {"INCOME_STATEMENT": is_data, "BALANCE_SHEET": bs_data,
                 "CASH_FLOW": cf_data}.get(fn)
        if table is None:
            raise AssertionError(f"unexpected params: {params}")
        return json.dumps(table).encode()

    fake_http.calls = calls
    return fake_http


class AlphaVantageFetcherTests(unittest.TestCase):
    def setUp(self):
        self._throttle = av._throttle
        av._throttle = lambda **kw: None
        self._env = mock.patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "test-key"})
        self._env.start()

    def tearDown(self):
        av._throttle = self._throttle
        self._env.stop()

    def test_symbol_mapping(self):
        self.assertEqual(av.alphavantage_symbol({"market": "GB", "ticker": "ont"}), "ONT.LON")
        self.assertEqual(av.alphavantage_symbol({"market": "FR", "ticker": "AIR"}), "AIR.PAR")
        self.assertEqual(av.alphavantage_symbol({"market": "AU", "ticker": "TLX"}), "TLX.AX")
        # market 未指定は辞書の既定 (JP)
        self.assertEqual(av.alphavantage_symbol({"ticker": "186A"}), "186A.TYO")
        self.assertEqual(av.alphavantage_symbol({"market": "JP", "ticker": "3174"}), "3174.TYO")
        # SimFin 圏内 (US/DE/CA) はバルクに任せるため対象外
        self.assertIsNone(av.alphavantage_symbol({"market": "US", "ticker": "AAPL"}))
        self.assertIsNone(av.alphavantage_symbol({"market": "DE", "ticker": "SAP"}))
        self.assertIsNone(av.alphavantage_symbol({"market": "CA", "ticker": "MDA"}))
        # 接尾辞表に無い市場・ティッカーなしは None (呼び出し側で対象外として記録)
        self.assertIsNone(av.alphavantage_symbol({"market": "IN", "ticker": "RELIANCE"}))
        self.assertIsNone(av.alphavantage_symbol({"market": "GB", "ticker": ""}))
        self.assertIsNone(av.alphavantage_symbol({"market": "GB"}))
        # コア企業がいる SimFin 圏外の市場はすべて変換できる
        for m in ("JP", "GB", "FR", "AU", "SE", "CH", "FI", "DK", "NL", "NO",
                  "ES", "BE", "IT"):
            self.assertIsNotNone(av.alphavantage_symbol({"market": m, "ticker": "X"}), m)

    def test_fetch_builds_quarterly_records(self):
        http = make_http()
        out = av.fetch_alphavantage("ONT.LON", 5, 400, AS_OF, http=http)
        # 窓内 (400日) の直近2四半期のみ (2025-03-31 は窓外)
        self.assertEqual([r["published"] for r in out], ["2026-06-30", "2026-03-31"])
        r = out[0]
        self.assertEqual(r["source"], "alphavantage")
        self.assertEqual(r["source_id"], "ONT.LON-2026-06-30")
        self.assertEqual(r["info_type"], "disclosure")
        self.assertIn("財務数値(Alpha Vantage)", r["abstract"])
        self.assertIn("直近四半期(2026-06-30まで)", r["abstract"])
        self.assertIn("売上高 150百万英ポンド", r["abstract"])
        self.assertIn("純利益 10百万英ポンド", r["abstract"])
        self.assertIn("営業キャッシュフロー 25百万英ポンド", r["abstract"])
        # FCF = 営業CF 25 − 設備投資 5 = 20
        self.assertIn("フリーキャッシュフロー 20百万英ポンド", r["abstract"])
        self.assertIn("総資産 1,000百万英ポンド", r["abstract"])
        self.assertIn("自己資本比率 60.0%", r["abstract"])
        # 欠損は文字列 "None" で返るため、項目ごと落とす (2026-03-31 の営業利益)
        self.assertNotIn("営業利益", out[1]["abstract"])
        self.assertIn("売上高 120百万英ポンド", out[1]["abstract"])
        # 取得は1社3リクエスト (3表)。キーは API の仕様上クエリに入る
        self.assertEqual(len(http.calls), 3)
        self.assertEqual({p["function"] for _u, p, _h, _r in http.calls},
                         {"INCOME_STATEMENT", "BALANCE_SHEET", "CASH_FLOW"})
        for _url, params, _headers, retries in http.calls:
            self.assertEqual(params["symbol"], "ONT.LON")
            self.assertEqual(params["apikey"], "test-key")
            self.assertEqual(retries, 0)
        # 正規化すると srcid キーになり、期間ごとに別候補になる
        n1, _ = normalize(out[0], "2026-10-20T00:00:00+00:00")
        n2, _ = normalize(out[1], "2026-10-20T00:00:00+00:00")
        self.assertEqual(n1["keys"][0], "srcid:alphavantage:ONT.LON-2026-06-30")
        self.assertNotEqual(n1["keys"][0], n2["keys"][0])

    def test_fetch_unknown_symbol_raises(self):
        # S-3: シンボルが提供元に無い (Error Message) 場合は握りつぶして
        # 成功・0件にせず、例外として上げる (呼び出し側で ok=0 と記録する)
        http = make_http(is_data={"Error Message": "Invalid API call."},
                         bs_data={"Error Message": "Invalid API call."},
                         cf_data={"Error Message": "Invalid API call."})
        with self.assertRaises(Exception) as cm:
            av.fetch_alphavantage("NOSUCH.LON", 5, 400, AS_OF, http=http)
        self.assertIn("Invalid API call", str(cm.exception))
        self.assertEqual(len(http.calls), 1)  # 損益で失敗が確定し、残りは聞かない

    def test_fetch_missing_balance_sheet_still_uses_other_tables(self):
        http = make_http(bs_data={"Error Message": "Invalid API call."})
        out = av.fetch_alphavantage("ONT.LON", 5, 400, AS_OF, http=http)
        self.assertEqual(len(out), 2)
        self.assertIn("売上高 150百万英ポンド", out[0]["abstract"])
        self.assertNotIn("貸借対照表", out[0]["abstract"])

    def test_fetch_rate_limit_note_raises(self):
        # 枠超過は HTTP 200 の本文で通知される。成功・0件で握りつぶさない
        http = make_http(is_data={"Note": "Thank you for using Alpha Vantage! "
                                          "Our standard API rate limit is 25 requests per day."})
        with self.assertRaises(av.RateLimitError):
            av.fetch_alphavantage("ONT.LON", 5, 400, AS_OF, http=http)
        http = make_http(is_data={"Information": "We have detected your API key "
                                                 "and reached the daily limit."})
        with self.assertRaises(av.RateLimitError):
            av.fetch_alphavantage("ONT.LON", 5, 400, AS_OF, http=http)

    def test_fetch_http_error_propagates(self):
        def http500(url, params=None, headers=None, retries=None):
            raise HttpError("HTTP 500", 500, None)
        with self.assertRaises(HttpError):
            av.fetch_alphavantage("ONT.LON", 5, 400, AS_OF, http=http500)

    def test_fetch_without_key_raises(self):
        self._env.stop()
        try:
            self.assertFalse(av.alphavantage_enabled())
            with self.assertRaises(RuntimeError):
                av.fetch_alphavantage("ONT.LON", 5, 400, AS_OF, http=make_http())
        finally:
            self._env.start()

    def test_last_fetch_requests_counts_issued_requests(self):
        # 当日枠の消費計数は「試行 × 3」ではなく実際に発行したリクエスト数
        # 成功は 3表 = 3回
        av.fetch_alphavantage("ONT.LON", 5, 400, AS_OF, http=make_http())
        self.assertEqual(av.last_fetch_requests(), 3)
        # 損益表だけが無い場合は、ある表だけで材料にするが発行は 3回
        av.fetch_alphavantage("ONT.LON", 5, 400, AS_OF,
                              http=make_http(bs_data={"Error Message": "Invalid API call."}))
        self.assertEqual(av.last_fetch_requests(), 3)
        # シンボル自体が無い場合は損益の 1回で打ち切る (×3 と数えない)
        with self.assertRaises(Exception):
            av.fetch_alphavantage("NOSUCH.LON", 5, 400, AS_OF,
                                  http=make_http(is_data={"Error Message": "Invalid API call."}))
        self.assertEqual(av.last_fetch_requests(), 1)
        # 枠超過の応答も、発行した 1回分は消費として数える
        with self.assertRaises(av.RateLimitError):
            av.fetch_alphavantage("ONT.LON", 5, 400, AS_OF,
                                  http=make_http(is_data={"Note": "rate limit"}))
        self.assertEqual(av.last_fetch_requests(), 1)
        # 通信失敗も発行した分だけ数える
        def http500(url, params=None, headers=None, retries=None):
            raise HttpError("HTTP 500", 500, None)
        with self.assertRaises(HttpError):
            av.fetch_alphavantage("ONT.LON", 5, 400, AS_OF, http=http500)
        self.assertEqual(av.last_fetch_requests(), 1)

    def test_last_fetch_requests_zero_when_no_request_issued(self):
        # キー未設定はリクエスト自体を発行しないため 0
        self._env.stop()
        try:
            os.environ.pop("ALPHA_VANTAGE_API_KEY", None)
            with self.assertRaises(RuntimeError):
                av.fetch_alphavantage("ONT.LON", 5, 400, AS_OF, http=make_http())
            self.assertEqual(av.last_fetch_requests(), 0)
        finally:
            self._env.start()

    def test_quota_arithmetic(self):
        # 枠の定数は提供元の 25回/日 から導出される (恣意的な数字を置かない)
        self.assertEqual(av.DAILY_REQUEST_LIMIT, 25)
        self.assertEqual(av.REQUESTS_PER_COMPANY, 3)
        self.assertEqual(av.MAX_COMPANIES_PER_DAY, 8)
        self.assertLessEqual(av.MAX_COMPANIES_PER_DAY * av.REQUESTS_PER_COMPANY,
                             av.DAILY_REQUEST_LIMIT)
        self.assertGreater((av.MAX_COMPANIES_PER_DAY + 1) * av.REQUESTS_PER_COMPANY,
                           av.DAILY_REQUEST_LIMIT)

    def test_throttle_interval_matches_per_minute_cap(self):
        # 分単位の上限 (5回/分前提) を超えない間隔であること
        self.assertGreaterEqual(av.MIN_INTERVAL_SEC, 60.0 / 5)


def fin_raw(source, sid, marker_label, published="2026-03-31"):
    return {"source": source, "source_id": sid,
            "title": f"T 財務情報({published} 通期)",
            "abstract": f"Tの財務数値({marker_label}): 直近通期({published}まで) 売上高 100百万円。",
            "url": f"https://example.org/{sid}", "published": published,
            "fields": ["財務"], "affiliations": [], "info_type": "disclosure"}


def av_raw(symbol):
    return {"source": "alphavantage", "source_id": f"{symbol}-2026-06-30",
            "title": f"{symbol} 財務情報(2026-06-30 四半期)",
            "abstract": f"{symbol}の財務数値(Alpha Vantage): 直近四半期(2026-06-30まで) 売上高 100百万英ポンド。",
            "url": "https://www.alphavantage.co/", "published": "2026-06-30",
            "fields": ["Alpha Vantage", "財務"], "affiliations": [], "info_type": "disclosure"}


ENTITIES = [
    {"id": "h_yahoo", "name": "H Corp", "tier": "core", "market": "JP", "ticker": "2000"},
    {"id": "g_zero", "name": "G Corp", "tier": "core", "market": "GB", "ticker": "GGG"},
    {"id": "i_us", "name": "I Corp", "tier": "core", "market": "US", "ticker": "III"},
    {"id": "j_watch", "name": "J Corp", "tier": "watch", "market": "GB", "ticker": "JJJ"},
    {"id": "k_edgar", "name": "K Corp", "tier": "core", "market": "GB", "ticker": "KKK"},
    {"id": "l_de", "name": "L Corp", "tier": "core", "market": "DE", "ticker": "LLL"},
    {"id": "m_fr", "name": "M Corp", "tier": "core", "market": "FR", "ticker": "MMM"},
]


def make_cfg(entities=ENTITIES):
    return {"sources": {"defaults": {"max_results": 5, "lookback_days": 90}, "queries": []},
            "entities": {"entities": entities},
            "config_hash": "testhash",
            "scoring": {"screening": {"collect_budget_sec": 1200}}}


def seed_financial(store, entity_id, source, sid, label):
    rec, problems = normalize(fin_raw(source, sid, label), "2026-04-01T00:00:00+00:00")
    assert rec is not None, problems
    cid, _ = store.upsert_candidate(rec, "seed-run")
    store.set_entities(cid, [{"entity_id": entity_id, "strength": "strong",
                              "matched_by": "test", "matched_alias": ""}])
    return cid


def seed_attempts(store, run_id, started_at, source, n, fetched=1):
    store.conn.execute(
        "INSERT INTO runs(run_id, started_at) VALUES(?, ?)", (run_id, started_at))
    for i in range(n):
        store.conn.execute(
            "INSERT INTO run_sources VALUES(?, ?, ?, ?, 0, 0, 0, 1, NULL, 0.1)",
            (run_id, source, f"other{i}-{source}", fetched))
    store.conn.commit()


class AlphaVantageCollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = T.Store(Path(self.tmp.name) / "d.sqlite")
        self.calls = []

        def fake_fetcher(symbol, max_results, lookback, as_of):
            self.calls.append(symbol)
            return [av_raw(symbol)]

        self.fetcher = fake_fetcher
        self.counts = {k: 0 for k in ("fetched", "new", "duplicates", "invalid",
                                       "sources_ok", "sources_failed")}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_collect(self, entities=ENTITIES):
        env = {"ALPHA_VANTAGE_API_KEY": "test-key", "ROIC_API_KEY": "", "SIMFIN_API_KEY": ""}
        with mock.patch.dict(os.environ, env):
            T.collect(self.store, make_cfg(entities), "run1", AS_OF, ("pubmed",), {},
                      self.counts, lambda *a: None, alphavantage_fetcher=self.fetcher)

    def test_targets_priority_and_scope(self):
        seed_financial(self.store, "h_yahoo", "yahoo", "2000-2026-03-31", "Yahoo Finance")
        seed_financial(self.store, "k_edgar", "edgar", "KKK-2026-03-31", "SEC EDGAR")
        self.run_collect()
        # 財務ゼロ (GB・FR) → Yahoo のみ (JP) の順。watch・裏取りありは対象外、
        # SimFin 圏内 (US・DE) は unmapped に記録
        self.assertEqual(self.calls, ["GGG.LON", "MMM.PAR", "2000.TYO"])
        self.assertEqual(self.counts["alphavantage_targets"], 3)
        self.assertEqual(self.counts["alphavantage_skipped_unmapped"], 2)
        self.assertEqual(self.counts["alphavantage_allowed"], 8)
        self.assertEqual(self.counts["sources_ok"], 3)
        cov = self.store.financial_sources_by_entity()
        self.assertEqual(cov["g_zero"], {"alphavantage"})
        self.assertEqual(cov["h_yahoo"], {"yahoo", "alphavantage"})
        rows = [r for r in self.store.run_sources("run1") if r["source"] == "alphavantage"]
        self.assertEqual({r["query"] for r in rows},
                         {"g_zero-alphavantage", "h_yahoo-alphavantage", "m_fr-alphavantage"})
        self.assertTrue(all(r["ok"] and r["fetched"] == 1 for r in rows))

    def test_daily_cap_defers_rest(self):
        ents = [{"id": f"z{i:02d}", "name": f"Z{i}", "tier": "core",
                 "market": "GB", "ticker": f"Z{i}"} for i in range(10)]
        self.run_collect(entities=ents)
        # 25回/日 ÷ 3リクエスト = 8社まで。残り2社は失敗にせず次回へ回す
        self.assertEqual(len(self.calls), 8)
        self.assertEqual(self.counts["alphavantage_deferred"], 2)
        rows = [r for r in self.store.run_sources("run1") if r["source"] == "alphavantage"]
        self.assertEqual(len(rows), 8)

    def test_same_day_prior_attempts_consume_quota(self):
        # 同日の先行実行が5社分 (15リクエスト) を消費済みなら、残枠は
        # 10リクエスト = 3社分だけ (枠の管理は run_sources の記録に寄せる)
        seed_attempts(self.store, "today-run", "2026-10-20T01:00:00+00:00",
                      "alphavantage", 5)
        ents = [{"id": f"z{i:02d}", "name": f"Z{i}", "tier": "core",
                 "market": "GB", "ticker": f"Z{i}"} for i in range(5)]
        self.run_collect(entities=ents)
        self.assertEqual(self.counts["alphavantage_used_today"], 15)
        self.assertEqual(self.counts["alphavantage_allowed"], 3)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.counts["alphavantage_deferred"], 2)

    def test_same_day_prior_requests_counted_from_run_counts(self):
        # 先行 run が counts に実リクエスト数 (7回) を残していれば、試行行数
        # × 3 ではなく実数で数える (同じ run に試行行が 5行 = 15回相当
        # あっても、記録された実数が優先される)
        self.store.conn.execute(
            "INSERT INTO runs(run_id, started_at, counts_json) VALUES(?, ?, ?)",
            ("today-run", "2026-10-20T01:00:00+00:00",
             json.dumps({"alphavantage_requests": 7})))
        for i in range(5):
            self.store.conn.execute(
                "INSERT INTO run_sources VALUES('today-run', 'alphavantage', ?, "
                "1, 0, 0, 0, 1, NULL, 0.1)", (f"other{i}-alphavantage",))
        self.store.conn.commit()
        ents = [{"id": f"z{i:02d}", "name": f"Z{i}", "tier": "core",
                 "market": "GB", "ticker": f"Z{i}"} for i in range(5)]
        self.run_collect(entities=ents)
        self.assertEqual(self.counts["alphavantage_used_today"], 7)
        self.assertEqual(self.counts["alphavantage_allowed"], 6)  # (25 − 7) // 3
        self.assertEqual(len(self.calls), 5)

    def test_recent_success_rests(self):
        seed_attempts(self.store, "recent-run", "2026-10-15T00:00:00+00:00",
                      "alphavantage", 0)
        self.store.conn.execute(
            "INSERT INTO run_sources VALUES('recent-run', 'alphavantage', "
            "'g_zero-alphavantage', 1, 0, 1, 0, 1, NULL, 0.1)")
        self.store.conn.commit()
        self.run_collect()
        self.assertNotIn("GGG.LON", self.calls)  # 5日前の成功 (fetched>0) → 30日休止
        self.assertEqual(self.counts["alphavantage_skipped_rest"], 1)

    def test_zero_fetch_success_does_not_rest(self):
        # ok でも fetched=0 の空振りは成功として数えない (ROIC の規則)。
        # 接尾辞の誤り等で0件の社を休止に閉じ込めない
        seed_attempts(self.store, "recent-run", "2026-10-15T00:00:00+00:00",
                      "alphavantage", 0)
        self.store.conn.execute(
            "INSERT INTO run_sources VALUES('recent-run', 'alphavantage', "
            "'g_zero-alphavantage', 0, 0, 0, 0, 1, NULL, 0.1)")
        self.store.conn.commit()
        self.run_collect()
        self.assertIn("GGG.LON", self.calls)
        self.assertEqual(self.counts["alphavantage_skipped_rest"], 0)

    def test_disabled_without_key(self):
        env = {"ROIC_API_KEY": "", "SIMFIN_API_KEY": ""}
        with mock.patch.dict(os.environ, env, clear=True):
            T.collect(self.store, make_cfg(), "run1", AS_OF, ("pubmed",), {},
                      self.counts, lambda *a: None, alphavantage_fetcher=self.fetcher)
        self.assertEqual(self.calls, [])
        self.assertNotIn("alphavantage_targets", self.counts)

    def test_zero_result_is_recorded(self):
        self.fetcher = lambda symbol, max_results, lookback, as_of: []
        self.run_collect(entities=[ENTITIES[1]])  # g_zero のみ
        rows = [r for r in self.store.run_sources("run1") if r["source"] == "alphavantage"]
        self.assertEqual(len(rows), 1)
        # 全クエリ成功で0件 = quiet_zero_sources の導出条件を満たす形で残る
        self.assertEqual((rows[0]["ok"], rows[0]["fetched"]), (1, 0))

    def test_symbol_error_recorded_as_failure(self):
        # S-3: シンボル不一致 (Error Message) は fetcher が例外として上げ、
        # run_sources には失敗 (ok=0) として残る (成功・0件にしない)
        def bad_symbol(symbol, max_results, lookback, as_of):
            self.calls.append(symbol)
            raise Exception("Invalid API call. (symbol error)")

        self.fetcher = bad_symbol
        self.run_collect(entities=[ENTITIES[1]])  # g_zero のみ
        rows = [r for r in self.store.run_sources("run1") if r["source"] == "alphavantage"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["ok"], rows[0]["fetched"]), (0, 0))
        self.assertEqual(self.counts["sources_failed"], 1)

    def test_rate_limit_stops_queue_and_defers_rest(self):
        def limited(symbol, max_results, lookback, as_of):
            self.calls.append(symbol)
            raise av.RateLimitError("daily limit reached")

        self.fetcher = limited
        ents = [ENTITIES[1], ENTITIES[6],
                {"id": "n_se", "name": "N Corp", "tier": "core", "market": "SE", "ticker": "NNN"}]
        self.run_collect(entities=ents)
        # 枠超過は当日に回復しないため、最初の1社で止める。残りは失敗として
        # 記録せず、次回のキューに残す (deferred)
        self.assertEqual(len(self.calls), 1)
        rows = [r for r in self.store.run_sources("run1") if r["source"] == "alphavantage"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ok"], 0)
        self.assertIn("RateLimitError", rows[0]["error"])
        self.assertEqual(self.counts["sources_failed"], 1)
        self.assertEqual(self.counts["alphavantage_deferred"], 2)

    def test_failure_is_recorded_and_queue_continues(self):
        def flaky(symbol, max_results, lookback, as_of):
            self.calls.append(symbol)
            if symbol == "GGG.LON":
                raise RuntimeError("boom")
            return [av_raw(symbol)]

        self.fetcher = flaky
        self.run_collect(entities=[ENTITIES[1], ENTITIES[6]])  # GB → FR
        self.assertEqual(self.calls, ["GGG.LON", "MMM.PAR"])
        rows = {r["query"]: r for r in self.store.run_sources("run1")
                if r["source"] == "alphavantage"}
        self.assertEqual(rows["g_zero-alphavantage"]["ok"], 0)
        self.assertIn("boom", rows["g_zero-alphavantage"]["error"])
        self.assertEqual(rows["m_fr-alphavantage"]["ok"], 1)
        self.assertEqual(self.counts["sources_failed"], 1)
        self.assertEqual(self.counts["sources_ok"], 1)


if __name__ == "__main__":
    unittest.main()
