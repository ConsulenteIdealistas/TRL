"""ESEF (filings.xbrl.org) 財務収集のテスト (外部APIに依存しない)。
実行: python -m unittest test_esef -v"""
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
from trlcore.sources import esef

AS_OF = date(2026, 2, 1)
LEI = "549300XO4L9XNOCFCZ84"


def fact(concept, value, period, unit="iso4217:EUR", extra=None):
    dims = {"concept": f"ifrs-full:{concept}", "entity": f"scheme:{LEI}",
            "period": period, "unit": unit}
    if extra:
        dims.update(extra)
    return {"value": str(value), "dimensions": dims}


def doc_2025(revenue=500_000_000):
    facts = {
        "f1": fact("Revenue", revenue, "2025-01-01T00:00:00/2026-01-01T00:00:00"),
        "f2": fact("Revenue", 400_000_000, "2024-01-01T00:00:00/2025-01-01T00:00:00"),
        # 半期 (年次でない) は通期として採らない
        "f3": fact("Revenue", 200_000_000, "2025-01-01T00:00:00/2025-07-01T00:00:00"),
        # 追加次元 (セグメント内訳) は集計値ではないので採らない
        "f4": fact("Revenue", 999_000_000, "2025-01-01T00:00:00/2026-01-01T00:00:00",
                   extra={"ifrs-full:ComponentsOfEquityAxis": "ifrs-full:EquityMember"}),
        # 同一 fact の再掲は重複として1つに寄せる
        "f5": fact("Revenue", revenue, "2025-01-01T00:00:00/2026-01-01T00:00:00"),
        "f6": fact("ProfitLossFromOperatingActivities", 50_000_000, "2025-01-01T00:00:00/2026-01-01T00:00:00"),
        "f7": fact("ProfitLoss", 40_000_000, "2025-01-01T00:00:00/2026-01-01T00:00:00"),
        "f8": fact("ProfitLossAttributableToOwnersOfParent", 39_000_000, "2025-01-01T00:00:00/2026-01-01T00:00:00"),
        "f9": fact("CashFlowsFromUsedInOperatingActivities", 60_000_000, "2025-01-01T00:00:00/2026-01-01T00:00:00"),
        "f10": fact("CashAndCashEquivalents", 120_000_000, "2026-01-01T00:00:00"),
        "f11": fact("Assets", 900_000_000, "2026-01-01T00:00:00"),
        "f12": fact("Equity", 450_000_000, "2026-01-01T00:00:00"),
    }
    return {"facts": facts}


def doc_2024():
    return {"facts": {
        "g1": fact("Revenue", 400_000_000, "2024-01-01T00:00:00/2025-01-01T00:00:00"),
        "g2": fact("Assets", 800_000_000, "2025-01-01T00:00:00"),
    }}


FILINGS = {"meta": {"count": 3}, "data": [
    {"attributes": {"period_end": "2025-12-31", "date_added": "2026-05-01 00:00:00",
                    "json_url": f"/{LEI}/2025-12-31/ESEF/SE/0/x-2025-12-31-sv.json"}},
    {"attributes": {"period_end": "2025-12-31", "date_added": "2026-04-01 00:00:00",
                    "json_url": f"/{LEI}/2025-12-31/ESEF/SE/0/x-2025-12-31-en.json"}},
    {"attributes": {"period_end": "2024-12-31", "date_added": "2025-05-01 00:00:00",
                    "json_url": f"/{LEI}/2024-12-31/ESEF/SE/0/x-2024-12-31-en.json"}},
]}


def make_http(filings=FILINGS):
    calls = []

    def fake_http(url, params=None, headers=None, retries=None):
        calls.append((url, headers))
        if "/api/entities/" in url:
            return json.dumps(filings).encode()
        if url.endswith("-en.json") and "2025-12-31" in url:
            return json.dumps(doc_2025()).encode()
        if url.endswith("-sv.json"):
            return json.dumps(doc_2025(revenue=111_000_000)).encode()
        if "2024-12-31" in url:
            return json.dumps(doc_2024()).encode()
        raise AssertionError(f"unexpected url: {url}")

    fake_http.calls = calls
    return fake_http


class EsefFetcherTests(unittest.TestCase):
    def setUp(self):
        self._throttle = esef._throttle
        esef._throttle = lambda **kw: None  # テストでは実時間の間隔を待たない

    def tearDown(self):
        esef._throttle = self._throttle

    def test_fetch_two_periods_prefers_english(self):
        recs = esef.fetch_esef(LEI, 5, 400, AS_OF, http=make_http())
        self.assertEqual(len(recs), 2)
        r = recs[0]
        self.assertEqual(r["source"], "esef")
        self.assertEqual(r["source_id"], f"{LEI}-2025-12-31")
        self.assertEqual(r["published"], "2025-12-31")
        self.assertEqual(r["info_type"], "disclosure")
        self.assertIn("財務数値(ESEF XBRL)", r["abstract"])
        # 英語版 (売上 500) がスウェーデン語版 (111) より優先される
        self.assertIn("売上高 500百万ユーロ", r["abstract"])
        self.assertIn("営業利益 50百万ユーロ", r["abstract"])
        self.assertIn("純利益 40百万ユーロ", r["abstract"])  # ProfitLoss を帰属分より優先
        self.assertIn("営業キャッシュフロー 60百万ユーロ", r["abstract"])
        self.assertIn("現金及び現金同等物 120百万ユーロ", r["abstract"])
        self.assertIn("総資産 900百万ユーロ", r["abstract"])
        self.assertIn("自己資本比率 50.0%", r["abstract"])
        self.assertTrue(r["url"].endswith("-en.json"))
        self.assertEqual(recs[1]["source_id"], f"{LEI}-2024-12-31")
        self.assertIn("売上高 400百万ユーロ", recs[1]["abstract"])

    def test_parse_ignores_segments_and_half_year(self):
        text, period = esef.parse_xbrl_json(doc_2025())
        self.assertEqual(period, date(2025, 12, 31))
        self.assertIn("売上高 500百万ユーロ", text)
        self.assertNotIn("999", text)
        self.assertNotIn("200百万ユーロ", text)

    def test_parse_without_flows_is_empty(self):
        doc = {"facts": {"h1": fact("Assets", 100, "2026-01-01T00:00:00")}}
        self.assertEqual(esef.parse_xbrl_json(doc), ("", None))

    def test_entity_not_found_is_empty(self):
        def http404(url, params=None, headers=None, retries=None):
            raise HttpError("not found", status=404)
        self.assertEqual(esef.fetch_esef(LEI, 5, 400, AS_OF, http=http404), [])

    def test_http_error_propagates(self):
        def http500(url, params=None, headers=None, retries=None):
            raise HttpError("server error", status=500)
        with self.assertRaises(HttpError):
            esef.fetch_esef(LEI, 5, 400, AS_OF, http=http500)

    def test_no_filings_is_empty(self):
        http = make_http(filings={"meta": {"count": 0}, "data": []})
        self.assertEqual(esef.fetch_esef(LEI, 5, 400, AS_OF, http=http), [])


def fin_raw(source, sid, marker_label, published="2025-12-31"):
    return {"source": source, "source_id": sid,
            "title": f"T 財務情報({published} 通期)",
            "abstract": f"Tの財務数値({marker_label}): 直近通期({published}まで) 売上高 100百万ユーロ。",
            "url": f"https://example.org/{sid}", "published": published,
            "fields": ["財務"], "affiliations": [], "info_type": "disclosure"}


def esef_raw(lei):
    return fin_raw("esef", f"{lei}-2025-12-31", "ESEF XBRL")


LEI_B, LEI_C = "11111111111111111111", "22222222222222222222"
ENTITIES = [
    {"id": "a_zero", "name": "A AB", "tier": "core", "market": "SE", "ticker": "A", "lei": LEI},
    {"id": "b_yahoo", "name": "B plc", "tier": "core", "market": "GB", "ticker": "B", "lei": LEI_B},
    {"id": "c_other", "name": "C SA", "tier": "core", "market": "FR", "ticker": "C", "lei": LEI_C},
    {"id": "d_nolei", "name": "D AG", "tier": "core", "market": "DE", "ticker": "D"},
    {"id": "e_watch", "name": "E AB", "tier": "watch", "market": "SE", "ticker": "E", "lei": LEI},
]


def make_cfg(entities=ENTITIES):
    return {"sources": {"defaults": {"max_results": 5, "lookback_days": 90}, "queries": []},
            "entities": {"entities": entities},
            "config_hash": "testhash",
            "scoring": {"screening": {"collect_budget_sec": 1200}}}


def seed_financial(store, entity_id, source, sid, label):
    rec, problems = normalize(fin_raw(source, sid, label), "2026-01-05T00:00:00+00:00")
    assert rec is not None, problems
    cid, _ = store.upsert_candidate(rec, "seed-run")
    store.set_entities(cid, [{"entity_id": entity_id, "strength": "strong",
                              "matched_by": "test", "matched_alias": ""}])
    return cid


class EsefCollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = T.Store(Path(self.tmp.name) / "d.sqlite")
        self.calls = []

        def fake_fetcher(lei, max_results, lookback, as_of):
            self.calls.append(lei)
            return [esef_raw(lei)]

        self.fetcher = fake_fetcher
        self.counts = {k: 0 for k in ("fetched", "new", "duplicates", "invalid",
                                      "sources_ok", "sources_failed")}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_collect(self, entities=ENTITIES):
        # testutil を import した他モジュールが無効化している場合に備え、
        # ここでは明示的に有効化する (全件実行との順序依存を避ける)
        with mock.patch.dict(os.environ, {"TRL_ESEF_DISABLE": ""}):
            T.collect(self.store, make_cfg(entities), "run1", AS_OF, ("pubmed",), {},
                      self.counts, lambda *a: None, esef_fetcher=self.fetcher)

    def test_disabled_by_env_gate(self):
        with mock.patch.dict(os.environ, {"TRL_ESEF_DISABLE": "1"}):
            T.collect(self.store, make_cfg(), "run1", AS_OF, ("pubmed",), {},
                      self.counts, lambda *a: None, esef_fetcher=self.fetcher)
        self.assertEqual(self.calls, [])
        self.assertNotIn("esef_targets", self.counts)

    def test_priority_and_targets(self):
        seed_financial(self.store, "b_yahoo", "yahoo", "B-2025-12-31", "Yahoo Finance")
        seed_financial(self.store, "c_other", "edgar", "C-2025-12-31", "SEC EDGAR")
        self.run_collect()
        # 財務ゼロ → Yahoo のみ → その他の順。LEI の無いコアと watch は対象外
        self.assertEqual(self.calls, [LEI, LEI_B, LEI_C])
        self.assertEqual(self.counts["esef_targets"], 3)
        self.assertEqual(self.counts["esef_skipped_no_lei"], 1)
        cov = self.store.financial_sources_by_entity()
        self.assertEqual(cov["a_zero"], {"esef"})
        self.assertEqual(cov["c_other"], {"edgar", "esef"})
        rows = [r for r in self.store.run_sources("run1") if r["source"] == "esef"]
        self.assertEqual({r["query"] for r in rows},
                         {"a_zero-esef", "b_yahoo-esef", "c_other-esef"})
        self.assertTrue(all(r["ok"] and r["fetched"] == 1 for r in rows))

    def _seed_run_source(self, fetched, run_id="recent-run", started="2026-01-20T00:00:00+00:00"):
        self.store.conn.execute(
            "INSERT INTO runs(run_id, started_at) VALUES(?, ?)", (run_id, started))
        self.store.conn.execute(
            "INSERT INTO run_sources VALUES(?, 'esef', 'a_zero-esef', ?, 0, 0, 0, 1, NULL, 0.1)",
            (run_id, fetched))
        self.store.conn.commit()

    def test_recent_success_rests(self):
        self._seed_run_source(fetched=1)
        self.run_collect()
        self.assertNotIn(LEI, self.calls)  # 12日前の成功 (取得あり) → 30日休止
        self.assertEqual(self.counts["esef_skipped_rest"], 1)

    def test_zero_fetch_success_does_not_rest(self):
        # ok でも fetched=0 の空振りは成功として数えない (fxo 未収録の企業を
        # 「成功・0件」で休止に閉じ込めない)
        self._seed_run_source(fetched=0)
        self.run_collect()
        self.assertIn(LEI, self.calls)
        self.assertEqual(self.counts["esef_skipped_rest"], 0)

    def test_zero_result_is_recorded(self):
        def empty_fetcher(lei, max_results, lookback, as_of):
            self.calls.append(lei)
            return []

        self.fetcher = empty_fetcher
        self.run_collect(entities=[ENTITIES[0]])
        rows = [r for r in self.store.run_sources("run1") if r["source"] == "esef"]
        self.assertEqual(len(rows), 1)
        # 全クエリ成功で0件 = quiet_zero_sources の導出条件を満たす形で残る
        self.assertEqual((rows[0]["ok"], rows[0]["fetched"]), (1, 0))

    def test_failure_is_recorded_not_swallowed(self):
        def bad_fetcher(lei, max_results, lookback, as_of):
            raise RuntimeError("boom")

        self.fetcher = bad_fetcher
        self.run_collect(entities=[ENTITIES[0]])
        rows = [r for r in self.store.run_sources("run1") if r["source"] == "esef"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ok"], 0)
        self.assertIn("boom", rows[0]["error"])
        self.assertEqual(self.counts["sources_failed"], 1)

    def test_per_run_cap_defers_rest(self):
        ents = [{"id": f"z{i:02d}", "name": f"Z{i}", "tier": "core", "market": "SE",
                 "ticker": f"Z{i}", "lei": f"{i:020d}"} for i in range(12)]
        self.run_collect(entities=ents)
        self.assertEqual(len(self.calls), 10)
        self.assertEqual(self.counts["esef_deferred"], 2)


class CompletenessTests(unittest.TestCase):
    """S-4: 完全性の明示と、期間終了日を混ぜないセット組み。"""

    def test_full_doc_has_no_missing(self):
        text, period, info = esef.parse_xbrl_json_full(doc_2025())
        self.assertEqual(info["missing"], [])
        self.assertEqual(len(info["found"]), 7)
        self.assertEqual(info["currencies"], ["EUR"])
        self.assertEqual(info["instant_period"], "2025-12-31")
        self.assertNotIn("欠落項目", text)

    def test_other_period_items_are_not_merged(self):
        # 営業利益は 2024 年分 (999) しか無い。2025 年のセットへ期の違う
        # 項目を混ぜず、欠落として明示する
        doc = doc_2025()
        doc["facts"]["f6"] = fact("ProfitLossFromOperatingActivities",
                                  999_000_000,
                                  "2024-01-01T00:00:00/2025-01-01T00:00:00")
        text, period, info = esef.parse_xbrl_json_full(doc)
        self.assertEqual(period.isoformat(), "2025-12-31")
        self.assertNotIn("999", text)
        self.assertIn("営業利益", info["missing"])
        self.assertIn("欠落項目: 営業利益", text)

    def test_ratio_requires_same_date_pair(self):
        # 純資産は 2025 年末、総資産は 2024 年末のみ → 2025 年末のセットに
        # 総資産は入らず、同日ペアが無いので比率も計算しない
        doc = doc_2025()
        doc["facts"]["f11"] = fact("Assets", 800_000_000, "2025-01-01T00:00:00")
        text, _period, info = esef.parse_xbrl_json_full(doc)
        self.assertNotIn("自己資本比率", text)
        self.assertIn("総資産", info["missing"])
        # instant が全て 2024 年末に揃えば、その日付のセットで比率が出る
        doc2 = doc_2025()
        doc2["facts"]["f10"] = fact("CashAndCashEquivalents", 100_000_000,
                                    "2025-01-01T00:00:00")
        doc2["facts"]["f11"] = fact("Assets", 800_000_000, "2025-01-01T00:00:00")
        doc2["facts"]["f12"] = fact("Equity", 400_000_000, "2025-01-01T00:00:00")
        text2, _p2, info2 = esef.parse_xbrl_json_full(doc2)
        self.assertIn("自己資本比率 50.0%", text2)
        self.assertEqual(info2["instant_period"], "2024-12-31")

    def test_currency_mix_is_noted(self):
        doc = doc_2025()
        doc["facts"]["f11"] = fact("Assets", 900_000_000, "2026-01-01T00:00:00",
                                   unit="iso4217:USD")
        text, _period, info = esef.parse_xbrl_json_full(doc)
        self.assertEqual(info["currencies"], ["EUR", "USD"])
        self.assertIn("通貨混在: EUR・USD", text)
        self.assertIn("総資産 900百万米ドル", text)
        # 通貨が違うペアからは自己資本比率を計算しない
        self.assertNotIn("自己資本比率", text)

    def test_fetch_record_carries_completeness(self):
        partial = {"facts": {"p1": fact(
            "Revenue", 500_000_000, "2025-01-01T00:00:00/2026-01-01T00:00:00")}}

        def fake_http(url, params=None, headers=None, retries=None):
            if "/api/entities/" in url:
                return json.dumps(FILINGS).encode()
            if "2025-12-31" in url:
                return json.dumps(partial).encode()
            if "2024-12-31" in url:
                return json.dumps(doc_2024()).encode()
            raise AssertionError(f"unexpected url: {url}")

        recs = esef.fetch_esef(LEI, 5, 400, AS_OF, http=fake_http)
        self.assertEqual(len(recs), 2)
        comp = recs[0]["completeness"]
        self.assertEqual(comp["found"], ["売上高"])
        self.assertIn("純資産", comp["missing"])
        self.assertIn("欠落項目", recs[0]["abstract"])


if __name__ == "__main__":
    unittest.main()
