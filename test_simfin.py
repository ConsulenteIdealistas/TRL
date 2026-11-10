"""SimFin バルク財務補完収集のテスト (外部APIに依存しない)。
実行: python -m unittest test_simfin -v"""
import io
import os
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock

import trl as T
from trlcore.http import HttpError
from trlcore.normalize import normalize
from trlcore.sources import simfin

AS_OF = date(2026, 10, 20)

INC_HEAD = ("Ticker;SimFinId;Currency;Fiscal Year;Fiscal Period;Report Date;"
            "Publish Date;Restated Date;Revenue;Operating Income (Loss);"
            "Net Income;Net Income (Common)")
BAL_HEAD = ("Ticker;SimFinId;Currency;Fiscal Year;Fiscal Period;Report Date;"
            "Publish Date;Restated Date;"
            "Cash, Cash Equivalents & Short Term Investments;Total Assets;Total Equity")
CF_HEAD = ("Ticker;SimFinId;Currency;Fiscal Year;Fiscal Period;Report Date;"
           "Publish Date;Restated Date;Net Cash from Operating Activities;"
           "Change in Fixed Assets & Intangibles")

INC_ROWS = [
    "XYZ;1;USD;2026;Q2;2026-06-30;2026-08-10;2026-08-10;40000000000;4000000000;3000000000;2800000000",
    "XYZ;1;USD;2026;Q1;2026-03-31;2026-05-12;2026-05-12;38000000000;3600000000;2600000000;2400000000",
    "XYZ;1;USD;2025;Q1;2025-03-31;2025-05-12;2025-05-12;30000000000;2000000000;1500000000;1400000000",
    # Publish Date が as_of より後 = 当時は知り得なかった期間は材料にしない
    "XYZ;1;USD;2026;Q3;2026-09-30;2026-11-15;2026-11-15;41000000000;4100000000;3100000000;2900000000",
    "ABC;2;USD;2026;Q2;2026-06-30;2026-08-10;2026-08-10;1000000000;100000000;90000000;80000000",
]
BAL_ROWS = [
    "XYZ;1;USD;2026;Q2;2026-06-30;2026-08-10;2026-08-10;8000000000;50000000000;20000000000",
    "XYZ;1;USD;2026;Q1;2026-03-31;2026-05-12;2026-05-12;7000000000;47000000000;18800000000",
]
CF_ROWS = [
    "XYZ;1;USD;2026;Q2;2026-06-30;2026-08-10;2026-08-10;6000000000;-1000000000",
    "XYZ;1;USD;2026;Q1;2026-03-31;2026-05-12;2026-05-12;5500000000;-900000000",
]


def csv_bytes(head, rows):
    return ("\n".join([head] + rows) + "\n").encode()


def make_http(inc=INC_ROWS, bal=BAL_ROWS, cf=CF_ROWS):
    calls = []

    def fake_http(url, params=None, headers=None, retries=None):
        calls.append((url, params, headers, retries))
        ds = (params or {}).get("dataset")
        if ds == "income":
            return csv_bytes(INC_HEAD, inc)
        if ds == "balance":
            return csv_bytes(BAL_HEAD, bal)
        if ds == "cashflow":
            return csv_bytes(CF_HEAD, cf)
        raise AssertionError(f"unexpected params: {params}")

    fake_http.calls = calls
    return fake_http


class SimFinFetcherTests(unittest.TestCase):
    def setUp(self):
        self._throttle = simfin._throttle
        simfin._throttle = lambda **kw: None
        self._env = mock.patch.dict(os.environ, {"SIMFIN_API_KEY": "test-key"})
        self._env.start()

    def tearDown(self):
        simfin._throttle = self._throttle
        self._env.stop()

    def test_market_and_ticker_mapping(self):
        self.assertEqual(simfin.simfin_market({"market": "US", "ticker": "XYZ"}), "us")
        self.assertEqual(simfin.simfin_market({"market": "DE", "ticker": "SAP"}), "de")
        self.assertEqual(simfin.simfin_market({"market": "CA", "ticker": "MDA"}), "ca")
        # カバー範囲外・ティッカーなしは None (呼び出し側で対象外として記録)
        self.assertIsNone(simfin.simfin_market({"market": "JP", "ticker": "3174"}))
        self.assertIsNone(simfin.simfin_market({"market": "GB", "ticker": "OXB"}))
        self.assertIsNone(simfin.simfin_market({"market": "US", "ticker": ""}))
        self.assertIsNone(simfin.simfin_market({"market": "US"}))
        self.assertEqual(simfin.simfin_ticker({"ticker": "brk.b"}), "BRK-B")
        self.assertIsNone(simfin.simfin_ticker({"ticker": ""}))

    def test_parse_bulk_csv_semicolon(self):
        rows = simfin.parse_bulk_csv(csv_bytes(INC_HEAD, INC_ROWS))
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["Ticker"], "XYZ")
        self.assertEqual(rows[0]["Revenue"], "40000000000")

    def test_parse_bulk_csv_zip_and_comma(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("us-income-ttm.csv", csv_bytes(INC_HEAD, INC_ROWS[:1]))
        rows = simfin.parse_bulk_csv(buf.getvalue())
        self.assertEqual(len(rows), 1)
        rows = simfin.parse_bulk_csv(b"Ticker,Revenue\nXYZ,123\n")
        self.assertEqual(rows, [{"Ticker": "XYZ", "Revenue": "123"}])
        self.assertEqual(simfin.parse_bulk_csv(b""), [])

    def test_fetch_bulk_builds_ttm_records(self):
        http = make_http()
        out = simfin.fetch_simfin_bulk("us", ["XYZ"], 400, AS_OF, http=http)
        self.assertEqual(sorted(out), ["XYZ"])  # 対象外の ABC は含まない
        recs = out["XYZ"]
        # 400日の窓内・公表済みの直近2期間 (2025-03-31 は窓外、
        # 2026-09-30 は Publish Date が as_of より後で除外)
        self.assertEqual([r["published"] for r in recs], ["2026-06-30", "2026-03-31"])
        r = recs[0]
        self.assertEqual(r["source"], "simfin")
        self.assertEqual(r["source_id"], "US:XYZ-2026-06-30")
        self.assertEqual(r["info_type"], "disclosure")
        self.assertIn("財務数値(SimFin)", r["abstract"])
        self.assertIn("直近TTM(2026-06-30まで、2026 Q2)", r["abstract"])
        self.assertIn("売上高 40,000百万米ドル", r["abstract"])
        # 純利益は Net Income (Common) を優先 (2,800)
        self.assertIn("純利益 2,800百万米ドル", r["abstract"])
        self.assertIn("営業キャッシュフロー 6,000百万米ドル", r["abstract"])
        # FCF = 営業CF 6,000 + 設備投資の現金増減 -1,000 = 5,000
        self.assertIn("フリーキャッシュフロー 5,000百万米ドル", r["abstract"])
        self.assertIn("総資産 50,000百万米ドル", r["abstract"])
        self.assertIn("自己資本比率 40.0%", r["abstract"])
        # 取得は市場あたり3ファイル (income/ttm, balance/quarterly, cashflow/ttm)
        got = {(p["dataset"], p["variant"], p["market"]) for _u, p, _h, _r in http.calls}
        self.assertEqual(got, {("income", "ttm", "us"), ("balance", "quarterly", "us"),
                               ("cashflow", "ttm", "us")})
        for _url, params, headers, retries in http.calls:
            self.assertEqual(headers["Authorization"], "api-key test-key")
            self.assertEqual(retries, 0)
            self.assertNotIn("test-key", str(params))  # キーを URL に載せない
        # 正規化すると srcid キーになり、期間ごとに別候補になる
        n1, _ = normalize(recs[0], "2026-10-20T00:00:00+00:00")
        n2, _ = normalize(recs[1], "2026-10-20T00:00:00+00:00")
        self.assertEqual(n1["keys"][0], "srcid:simfin:US:XYZ-2026-06-30")
        self.assertNotEqual(n1["keys"][0], n2["keys"][0])

    def test_fetch_ticker_not_in_bulk_is_absent(self):
        out = simfin.fetch_simfin_bulk("us", ["NOSUCH"], 400, AS_OF, http=make_http())
        self.assertEqual(out, {})

    def test_fetch_unknown_market_downloads_nothing(self):
        http = make_http()
        self.assertEqual(simfin.fetch_simfin_bulk("jp", ["XYZ"], 400, AS_OF, http=http), {})
        self.assertEqual(http.calls, [])

    def test_fetch_http_error_propagates(self):
        def http500(url, params=None, headers=None, retries=None):
            raise HttpError("HTTP 500", 500, None)
        with self.assertRaises(HttpError):
            simfin.fetch_simfin_bulk("us", ["XYZ"], 400, AS_OF, http=http500)

    def test_fetch_without_key_raises(self):
        self._env.stop()
        try:
            self.assertFalse(simfin.simfin_enabled())
            with self.assertRaises(RuntimeError):
                simfin.fetch_simfin_bulk("us", ["XYZ"], 400, AS_OF, http=make_http())
        finally:
            self._env.start()

    def test_throttle_interval_matches_web_api_limit(self):
        # Web-API の上限 2回/秒を超えない間隔であること
        self.assertGreaterEqual(simfin.MIN_INTERVAL_SEC, 0.5)


def fin_raw(source, sid, marker_label, published="2026-03-31"):
    return {"source": source, "source_id": sid,
            "title": f"T 財務情報({published} 通期)",
            "abstract": f"Tの財務数値({marker_label}): 直近通期({published}まで) 売上高 100百万円。",
            "url": f"https://example.org/{sid}", "published": published,
            "fields": ["財務"], "affiliations": [], "info_type": "disclosure"}


def simfin_raw(ticker):
    return {"source": "simfin", "source_id": f"US:{ticker}-2026-06-30",
            "title": f"{ticker} 財務情報(2026-06-30 TTM)",
            "abstract": f"{ticker}の財務数値(SimFin): 直近TTM(2026-06-30まで) 売上高 100百万米ドル。",
            "url": "https://www.simfin.com/en/prices/", "published": "2026-06-30",
            "fields": ["SimFin", "財務"], "affiliations": [], "info_type": "disclosure"}


ENTITIES = [
    {"id": "a_zero", "name": "A Corp", "tier": "core", "market": "US", "ticker": "AAA"},
    {"id": "b_yahoo", "name": "B Corp", "tier": "core", "market": "DE", "ticker": "BBB"},
    {"id": "c_watch", "name": "C Corp", "tier": "watch", "market": "US", "ticker": "CCC"},
    {"id": "d_edgar", "name": "D Corp", "tier": "core", "market": "US", "ticker": "DDD"},
    {"id": "e_unmapped", "name": "E Corp", "tier": "core", "market": "JP", "ticker": "5000"},
    {"id": "f_ca", "name": "F Corp", "tier": "core", "market": "CA", "ticker": "FFF"},
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


class SimFinCollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = T.Store(Path(self.tmp.name) / "d.sqlite")
        self.calls = []

        def fake_fetcher(market, tickers, lookback, as_of):
            self.calls.append((market, list(tickers)))
            return {t: [simfin_raw(t)] for t in tickers}

        self.fetcher = fake_fetcher
        self.counts = {k: 0 for k in ("fetched", "new", "duplicates", "invalid",
                                       "sources_ok", "sources_failed")}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_collect(self, entities=ENTITIES):
        with mock.patch.dict(os.environ, {"SIMFIN_API_KEY": "test-key"}):
            T.collect(self.store, make_cfg(entities), "run1", AS_OF, ("pubmed",), {},
                      self.counts, lambda *a: None, simfin_fetcher=self.fetcher)

    def test_targets_grouped_by_market(self):
        seed_financial(self.store, "b_yahoo", "yahoo", "BBB-2026-03-31", "Yahoo Finance")
        seed_financial(self.store, "d_edgar", "edgar", "DDD-2026-03-31", "SEC EDGAR")
        self.run_collect()
        # 市場ごとに1回だけ呼ばれる (社ごとには落とさない)。
        # watch・裏取りありは対象外、カバー外 (JP) は unmapped に記録
        self.assertEqual(self.calls, [("ca", ["FFF"]), ("de", ["BBB"]), ("us", ["AAA"])])
        self.assertEqual(self.counts["simfin_targets"], 3)
        self.assertEqual(self.counts["simfin_skipped_unmapped"], 1)
        self.assertEqual(self.counts["sources_ok"], 3)
        cov = self.store.financial_sources_by_entity()
        self.assertEqual(cov["a_zero"], {"simfin"})
        self.assertEqual(cov["b_yahoo"], {"yahoo", "simfin"})
        self.assertEqual(cov["f_ca"], {"simfin"})
        rows = [r for r in self.store.run_sources("run1") if r["source"] == "simfin"]
        self.assertEqual({r["query"] for r in rows},
                         {"a_zero-simfin", "b_yahoo-simfin", "f_ca-simfin"})
        self.assertTrue(all(r["ok"] and r["fetched"] == 1 for r in rows))

    def test_recent_success_rests(self):
        self.store.conn.execute(
            "INSERT INTO runs(run_id, started_at) VALUES('recent-run', '2026-10-15T00:00:00+00:00')")
        self.store.conn.execute(
            "INSERT INTO run_sources VALUES('recent-run', 'simfin', 'a_zero-simfin', 1, 0, 1, 0, 1, NULL, 0.1)")
        self.store.conn.commit()
        self.run_collect()
        us = [c for c in self.calls if c[0] == "us"]
        # 5日前の成功 (fetched>0) → AAA は30日休止。財務ゼロの DDD だけが残る
        self.assertEqual(us, [("us", ["DDD"])])
        self.assertEqual(self.counts["simfin_skipped_rest"], 1)

    def test_zero_fetch_success_does_not_rest(self):
        # ok でも fetched=0 の空振りは成功として数えない (ROIC の規則)。
        # バルクに無い・遅延で期間が無い社を休止に閉じ込めない
        self.store.conn.execute(
            "INSERT INTO runs(run_id, started_at) VALUES('recent-run', '2026-10-15T00:00:00+00:00')")
        self.store.conn.execute(
            "INSERT INTO run_sources VALUES('recent-run', 'simfin', 'a_zero-simfin', 0, 0, 0, 0, 1, NULL, 0.1)")
        self.store.conn.commit()
        self.run_collect()
        self.assertIn(("us", ["AAA", "DDD"]), self.calls)  # AAA も休止せず再試行
        self.assertEqual(self.counts["simfin_skipped_rest"], 0)

    def test_disabled_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            T.collect(self.store, make_cfg(), "run1", AS_OF, ("pubmed",), {},
                      self.counts, lambda *a: None, simfin_fetcher=self.fetcher)
        self.assertEqual(self.calls, [])
        self.assertNotIn("simfin_targets", self.counts)

    def test_zero_result_is_recorded(self):
        self.fetcher = lambda market, tickers, lookback, as_of: {}
        self.run_collect(entities=[ENTITIES[0]])
        rows = [r for r in self.store.run_sources("run1") if r["source"] == "simfin"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["ok"], rows[0]["fetched"]), (1, 0))

    def test_market_failure_is_recorded_for_all_its_targets(self):
        def bad_fetcher(market, tickers, lookback, as_of):
            raise RuntimeError("boom")
        self.fetcher = bad_fetcher
        self.run_collect(entities=[ENTITIES[0], ENTITIES[5]])  # US と CA
        rows = [r for r in self.store.run_sources("run1") if r["source"] == "simfin"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["ok"] == 0 for r in rows))
        self.assertTrue(all("boom" in r["error"] for r in rows))
        self.assertEqual(self.counts["sources_failed"], 2)


if __name__ == "__main__":
    unittest.main()
