"""ROIC.ai 財務補完収集のテスト (外部APIに依存しない)。
実行: python -m unittest test_roic -v"""
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
from trlcore.sources import roic

AS_OF = date(2026, 10, 20)

IS_ROWS = [
    {"period_end_date": "2026-03-31", "fiscal_year": 2026, "currency": "JPY",
     "is_sales_revenue_turnover": 12_345_000_000, "is_oper_income": 1_234_000_000,
     "is_net_income": 987_000_000},
    {"period_end_date": "2025-03-31", "fiscal_year": 2025, "currency": "JPY",
     "is_sales_revenue_turnover": 10_000_000_000, "is_oper_income": 900_000_000,
     "is_net_income": 700_000_000},
]
BS_ROWS = [
    {"period_end_date": "2026-03-31", "fiscal_year": 2026, "currency": "JPY",
     "bs_cash_near_cash_item": 2_000_000_000, "bs_tot_asset": 20_000_000_000,
     "bs_total_equity": 9_000_000_000},
    {"period_end_date": "2025-03-31", "fiscal_year": 2025, "currency": "JPY",
     "bs_cash_near_cash_item": 1_800_000_000, "bs_tot_asset": 18_000_000_000,
     "bs_total_equity": 8_100_000_000},
]
CF_ROWS = [
    {"period_end_date": "2026-03-31", "fiscal_year": 2026, "currency": "JPY",
     "cf_cash_from_oper": 1_500_000_000, "cf_free_cash_flow": 900_000_000},
]


def make_http(is_rows=IS_ROWS, bs_rows=BS_ROWS, cf_rows=CF_ROWS, search_rows=None):
    calls = []

    def fake_http(url, params=None, headers=None, retries=None):
        calls.append((url, params, headers))
        if "/tickers/search" in url:
            return json.dumps({"data": search_rows or []}).encode()
        if "/income-statement/" in url:
            return json.dumps({"data": is_rows}).encode()
        if "/balance-sheet/" in url:
            return json.dumps({"data": bs_rows}).encode()
        if "/cash-flow/" in url:
            return json.dumps({"data": cf_rows}).encode()
        raise AssertionError(f"unexpected url: {url}")

    fake_http.calls = calls
    return fake_http


class RoicFetcherTests(unittest.TestCase):
    def setUp(self):
        self._throttle = roic._throttle
        roic._throttle = lambda **kw: None  # テストでは実時間の間隔を待たない
        self._env = mock.patch.dict(os.environ, {"ROIC_API_KEY": "test-key"})
        self._env.start()

    def tearDown(self):
        roic._throttle = self._throttle
        self._env.stop()

    def test_symbol_mapping(self):
        self.assertEqual(roic.roic_symbol({"market": "JP", "ticker": "3174"}), "TYO:3174")
        self.assertEqual(roic.roic_symbol({"market": "DE", "ticker": "SAP"}), "XETR:SAP")
        self.assertEqual(roic.roic_symbol({"market": "DK", "ticker": "NOVO-B"}), "CPH:NOVO-B")
        self.assertEqual(roic.roic_symbol({"market": "GB", "ticker": "OXB"}), "LSE:OXB")
        # US は取引所が決まらないため裸ティッカー (取得時に検索で解決)
        self.assertEqual(roic.roic_symbol({"market": "US", "ticker": "aapl"}), "AAPL")
        # 変換できない市場・ティッカーなしは None
        self.assertIsNone(roic.roic_symbol({"market": "IN", "ticker": "RELIANCE"}))
        self.assertIsNone(roic.roic_symbol({"market": "JP", "ticker": ""}))
        self.assertIsNone(roic.roic_symbol({"market": "JP"}))
        # 16市場すべてに変換先がある
        for m in ("JP", "US", "GB", "DE", "CH", "CA", "AU", "SE", "DK", "NO",
                  "FI", "NL", "BE", "FR", "ES", "IT"):
            self.assertIsNotNone(roic.roic_symbol({"market": m, "ticker": "X"}), m)

    def test_fetch_normalizes_two_annual_periods(self):
        http = make_http()
        recs = roic.fetch_roic("TYO:3174", 5, 400, date(2026, 4, 15), http=http)
        self.assertEqual(len(recs), 2)
        r = recs[0]
        self.assertEqual(r["source"], "roic")
        self.assertEqual(r["source_id"], "TYO:3174-2026-03-31")
        self.assertEqual(r["published"], "2026-03-31")
        self.assertEqual(r["info_type"], "disclosure")
        self.assertIn("財務数値(ROIC.ai)", r["abstract"])
        self.assertIn("売上高 12,345百万円", r["abstract"])
        self.assertIn("純利益 987百万円", r["abstract"])
        self.assertIn("営業キャッシュフロー 1,500百万円", r["abstract"])
        self.assertIn("総資産 20,000百万円", r["abstract"])
        self.assertIn("自己資本比率 45.0%", r["abstract"])
        self.assertEqual(recs[1]["source_id"], "TYO:3174-2025-03-31")
        # 認証は Bearer ヘッダのみで、キーを URL に載せない
        for _url, _params, headers in http.calls:
            self.assertEqual(headers["Authorization"], "Bearer test-key")
        # 正規化すると srcid キーになり、期間ごとに別候補になる
        n1, _ = normalize(recs[0], "2026-04-15T00:00:00+00:00")
        n2, _ = normalize(recs[1], "2026-04-15T00:00:00+00:00")
        self.assertEqual(n1["keys"][0], "srcid:roic:TYO:3174-2026-03-31")
        self.assertNotEqual(n1["keys"][0], n2["keys"][0])

    def test_fetch_drops_periods_outside_lookback(self):
        http = make_http()
        recs = roic.fetch_roic("TYO:3174", 5, 400, AS_OF, http=http)
        # AS_OF (2026-10-20) から400日より前の 2025-03-31 は材料にしない
        self.assertEqual([r["published"] for r in recs], ["2026-03-31"])

    def test_fetch_404_is_empty_not_error(self):
        def http404(url, params=None, headers=None, retries=None):
            raise HttpError("HTTP 404", 404, None)
        self.assertEqual(roic.fetch_roic("TYO:9999", 5, 400, AS_OF, http=http404), [])

    def test_fetch_http_error_propagates(self):
        def http500(url, params=None, headers=None, retries=None):
            raise HttpError("HTTP 500", 500, None)
        with self.assertRaises(HttpError):
            roic.fetch_roic("TYO:3174", 5, 400, AS_OF, http=http500)

    def test_fetch_without_key_raises(self):
        self._env.stop()
        try:
            self.assertFalse(roic.roic_enabled())
            with self.assertRaises(RuntimeError):
                roic.fetch_roic("TYO:3174", 5, 400, AS_OF, http=make_http())
        finally:
            self._env.start()

    def test_us_symbol_resolution(self):
        search_rows = [
            {"symbol": "NASDAQ:XYZW", "primary_symbol": "XYZW", "exchange": "NASDAQ",
             "is_primary": True, "status": "listed"},
            {"symbol": "NYSE:XYZ", "primary_symbol": "XYZ", "exchange": "NYSE",
             "is_primary": True, "status": "listed"},
        ]
        http = make_http(search_rows=search_rows)
        recs = roic.fetch_roic("XYZ", 5, 400, date(2026, 4, 15), http=http)
        self.assertEqual(len(recs), 2)
        self.assertTrue(recs[0]["source_id"].startswith("NYSE:XYZ-"))
        stmt_urls = [u for u, _p, _h in http.calls if "/fundamental/" in u]
        self.assertTrue(all("NYSE:XYZ" in u for u in stmt_urls))

    def test_us_unresolvable_is_empty(self):
        http = make_http(search_rows=[])
        self.assertEqual(roic.fetch_roic("NOSUCH", 5, 400, AS_OF, http=http), [])

    def test_throttle_interval(self):
        roic._throttle = self._throttle  # 実物で検証する
        roic._last_call[0] = 0.0
        sleeps = []
        now = [1000.0]
        roic._throttle(sleep=sleeps.append, now=lambda: now[0])
        self.assertEqual(sleeps, [])  # 初回は待たない
        now[0] = 1005.0
        roic._throttle(sleep=sleeps.append, now=lambda: now[0])
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], roic.MIN_INTERVAL_SEC - 5.0)
        # 無料枠 5回/分を超えない間隔であること
        self.assertGreaterEqual(roic.MIN_INTERVAL_SEC, 60.0 / 5)


def fin_raw(source, sid, marker_label, published="2026-03-31"):
    return {"source": source, "source_id": sid,
            "title": f"T 財務情報({published} 通期)",
            "abstract": f"Tの財務数値({marker_label}): 直近通期({published}まで) 売上高 100百万円。",
            "url": f"https://example.org/{sid}", "published": published,
            "fields": ["財務"], "affiliations": [], "info_type": "disclosure"}


def roic_raw(symbol):
    return fin_raw("roic", f"{symbol}-2026-03-31", "ROIC.ai")


ENTITIES = [
    {"id": "a_zero", "name": "A Corp", "tier": "core", "market": "JP", "ticker": "1000"},
    {"id": "b_yahoo", "name": "B Corp", "tier": "core", "market": "JP", "ticker": "2000"},
    {"id": "c_watch", "name": "C Corp", "tier": "watch", "market": "JP", "ticker": "3000"},
    {"id": "d_edgar", "name": "D Corp", "tier": "core", "market": "JP", "ticker": "4000"},
    {"id": "e_unmapped", "name": "E Corp", "tier": "core", "market": "XX", "ticker": "5000"},
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


class RoicCollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = T.Store(Path(self.tmp.name) / "d.sqlite")
        self.calls = []

        def fake_fetcher(symbol, max_results, lookback, as_of):
            self.calls.append(symbol)
            return [roic_raw(symbol)]

        self.fetcher = fake_fetcher
        self.counts = {k: 0 for k in ("fetched", "new", "duplicates", "invalid",
                                       "sources_ok", "sources_failed")}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_collect(self, entities=ENTITIES):
        with mock.patch.dict(os.environ, {"ROIC_API_KEY": "test-key"}):
            T.collect(self.store, make_cfg(entities), "run1", AS_OF, ("pubmed",), {},
                      self.counts, lambda *a: None, roic_fetcher=self.fetcher)

    def test_priority_zero_first_then_yahoo_only(self):
        seed_financial(self.store, "b_yahoo", "yahoo", "2000-2026-03-31", "Yahoo Finance")
        seed_financial(self.store, "d_edgar", "edgar", "4000-2026-03-31", "SEC EDGAR")
        self.run_collect()
        # 財務ゼロ (a) → Yahoo のみ (b) の順。watch・裏取りあり・変換不可は対象外
        self.assertEqual(self.calls, ["TYO:1000", "TYO:2000"])
        self.assertEqual(self.counts["roic_targets"], 2)
        self.assertEqual(self.counts["roic_skipped_unmapped"], 1)
        self.assertEqual(self.counts["sources_ok"], 2)
        cov = self.store.financial_sources_by_entity()
        self.assertEqual(cov["a_zero"], {"roic"})
        self.assertEqual(cov["b_yahoo"], {"yahoo", "roic"})
        rows = [r for r in self.store.run_sources("run1") if r["source"] == "roic"]
        self.assertEqual({r["query"] for r in rows}, {"a_zero-roic", "b_yahoo-roic"})
        self.assertTrue(all(r["ok"] and r["fetched"] == 1 for r in rows))

    def test_recent_success_rests(self):
        self.store.conn.execute(
            "INSERT INTO runs(run_id, started_at) VALUES('recent-run', '2026-10-15T00:00:00+00:00')")
        self.store.conn.execute(
            "INSERT INTO run_sources VALUES('recent-run', 'roic', 'a_zero-roic', 1, 0, 1, 0, 1, NULL, 0.1)")
        self.store.conn.commit()
        self.run_collect()
        self.assertNotIn("TYO:1000", self.calls)  # 5日前の成功 → 30日休止
        self.assertIn("TYO:2000", self.calls)
        self.assertEqual(self.counts["roic_skipped_rest"], 1)

    def test_zero_fetch_success_does_not_rest(self):
        # ok でも fetched=0 の空振りは成功として数えない (英国 LON の罠:
        # 「成功・0件」で30日休止に閉じ込められていた)
        self.store.conn.execute(
            "INSERT INTO runs(run_id, started_at) VALUES('recent-run', '2026-10-15T00:00:00+00:00')")
        self.store.conn.execute(
            "INSERT INTO run_sources VALUES('recent-run', 'roic', 'a_zero-roic', 0, 0, 0, 0, 1, NULL, 0.1)")
        self.store.conn.commit()
        self.run_collect()
        self.assertIn("TYO:1000", self.calls)  # 5日前の空振り → 休止せず再試行
        self.assertEqual(self.counts["roic_skipped_rest"], 0)

    def test_disabled_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            T.collect(self.store, make_cfg(), "run1", AS_OF, ("pubmed",), {},
                      self.counts, lambda *a: None, roic_fetcher=self.fetcher)
        self.assertEqual(self.calls, [])
        self.assertNotIn("roic_targets", self.counts)

    def test_zero_result_is_recorded(self):
        def empty_fetcher(symbol, max_results, lookback, as_of):
            self.calls.append(symbol)
            return []

        self.fetcher = empty_fetcher
        ents = [ENTITIES[0]]
        self.run_collect(entities=ents)
        rows = [r for r in self.store.run_sources("run1") if r["source"] == "roic"]
        self.assertEqual(len(rows), 1)
        # 全クエリ成功で0件 = quiet_zero_sources の導出条件を満たす形で残る
        self.assertEqual((rows[0]["ok"], rows[0]["fetched"]), (1, 0))

    def test_failure_is_recorded_not_swallowed(self):
        def bad_fetcher(symbol, max_results, lookback, as_of):
            raise RuntimeError("boom")

        self.fetcher = bad_fetcher
        self.run_collect(entities=[ENTITIES[0]])
        rows = [r for r in self.store.run_sources("run1") if r["source"] == "roic"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ok"], 0)
        self.assertIn("boom", rows[0]["error"])
        self.assertEqual(self.counts["sources_failed"], 1)

    def test_per_run_cap_defers_rest(self):
        ents = [{"id": f"z{i:02d}", "name": f"Z{i}", "tier": "core",
                 "market": "JP", "ticker": f"{1000 + i}"} for i in range(12)]
        self.run_collect(entities=ents)
        self.assertEqual(len(self.calls), 10)
        self.assertEqual(self.counts["roic_deferred"], 2)


if __name__ == "__main__":
    unittest.main()
