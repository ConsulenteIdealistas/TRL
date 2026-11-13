"""スイス収集 (EQS / GlobeNewswire) のテスト (外部サイトに依存しない)。
実行: python -m unittest test_swiss -v"""
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import trl as T
from trlcore.normalize import normalize
from trlcore.sources import swiss

AS_OF = date(2026, 11, 10)

EQS_SEARCH_HTML = """
<html><body>
<a href="/news/ad-hoc/lonza-half-year-results-2026/123e4567-e89b-12d3-a456-426614174000_en">Lonza Half-Year Results 2026</a>
<a href="/news/corporate/other-company-half-year-results/123e4567-e89b-12d3-a456-426614174001_en">Other Company Half-Year Results</a>
<a href="/news/corporate/lonza-agm-invitation/123e4567-e89b-12d3-a456-426614174002_en">Lonza Annual General Meeting Invitation</a>
<a href="/company/lonza/">Lonza company page</a>
</body></html>
"""


def eqs_article(isin="CH0013841017", news_id="2987654", day="22.07.2026"):
    return f"""
<html><head><title>Lonza Half-Year Results 2026</title></head><body>
<p>{day} / 07:00 CET/CEST Dissemination of an Ad hoc announcement.</p>
<p>Basel, Switzerland, 22 July 2026 - Lonza announced sales of CHF 3,374 million in the first half.</p>
<p>EBIT was CHF 789 million and profit for the period was CHF 572 million.</p>
<table><tr><td>Company:</td><td>Lonza Group AG</td></tr>
<tr><td>ISIN:</td><td>{isin}</td></tr>
<tr><td>EQS News ID:</td><td>{news_id}</td></tr></table>
</body></html>
"""


GNW_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/elements/1.1/">
<title>Basilea Pharmaceutica International Ltd, Allschwil</title>
<entry>
<title>Basilea reports strong 2025 full-year results</title>
<link rel="alternate" href="https://www.globenewswire.com/news-release/2026/02/17/3238936/0/en/Basilea-reports-strong-2025-full-year-results.html"/>
<published>2026-02-17T06:15:00Z</published>
<summary type="html">&lt;p&gt;Allschwil, Switzerland, February 17, 2026 - Total revenue was CHF 232.4 million in 2025. Operating profit amounted to CHF 51.5 million and net profit to CHF 40.2 million.&lt;/p&gt;</summary>
<dc:identifier>3238936</dc:identifier>
<dc:subject>Earnings Releases and Operating Results</dc:subject>
</entry>
<entry>
<title>Basilea erzielt starke Ergebnisse im Geschäftsjahr 2025</title>
<link rel="alternate" href="https://www.globenewswire.com/news-release/2026/02/17/3238936/0/de/Basilea-erzielt-starke-Ergebnisse.html"/>
<published>2026-02-17T06:15:00Z</published>
<summary type="html">&lt;p&gt;Der Gesamtumsatz betrug CHF 232.4 Millionen.&lt;/p&gt;</summary>
<dc:identifier>3238936</dc:identifier>
<dc:subject>Earnings Releases and Operating Results</dc:subject>
</entry>
<entry>
<title>Basilea joins research consortium</title>
<link rel="alternate" href="https://www.globenewswire.com/news-release/2026/01/13/3217461/0/en/Basilea-joins-consortium.html"/>
<published>2026-01-13T06:15:00Z</published>
<summary type="html">&lt;p&gt;Basilea joins a research consortium.&lt;/p&gt;</summary>
<dc:identifier>3217461</dc:identifier>
<dc:subject>Company Announcement</dc:subject>
</entry>
</feed>
"""

# rss ホスト側フィードの実形式 (2026-11-13 実測): dc は DCMI 本家の
# 名前空間、本文は summary でなく content にリード文のみが入る
GNW_FEED_RSS = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns:dc="http://dublincore.org/documents/dcmi-namespace/" xmlns="http://www.w3.org/2005/Atom">
<title>Molecular Partners AG</title>
<entry>
<title>Molecular Partners Reports Q3 2026 Financial Results</title>
<link rel="alternate" href="https://www.globenewswire.com/news-release/2026/10/29/3390914/0/en/molecular-partners-reports-q3-2026-financial-results.html"/>
<published>2026-10-29T06:00:00Z</published>
<content type="html"><![CDATA[<p>ZURICH, Oct. 29, 2026 (GLOBE NEWSWIRE) -- Molecular Partners AG today announced financial results for the third quarter of 2026.</p>]]></content>
<dc:identifier>3390914</dc:identifier>
<dc:subject>Earnings Releases and Operating Results</dc:subject>
</entry>
<entry>
<title>Molecular Partners meldet Ergebnisse für das dritte Quartal 2026</title>
<link rel="alternate" href="https://www.globenewswire.com/news-release/2026/10/29/3390914/0/de/molecular-partners-meldet-ergebnisse.html"/>
<published>2026-10-29T06:00:00Z</published>
<content type="html"><![CDATA[<p>ZÜRICH, 29. Oktober 2026 (GLOBE NEWSWIRE) -- Molecular Partners AG gab heute die Finanzergebnisse bekannt.</p>]]></content>
<dc:identifier>3390914</dc:identifier>
<dc:subject>Earnings Releases and Operating Results</dc:subject>
</entry>
<entry>
<title>Molecular Partners to Present at a Healthcare Conference</title>
<link rel="alternate" href="https://www.globenewswire.com/news-release/2026/11/05/3395000/0/en/molecular-partners-to-present.html"/>
<published>2026-11-05T06:00:00Z</published>
<content type="html"><![CDATA[<p>Molecular Partners will present at a conference.</p>]]></content>
<dc:identifier>3395000</dc:identifier>
<dc:subject>Conferences</dc:subject>
</entry>
</feed>
"""

# 記事ページ: 本文コンテナの外 (ナビ) にも金額文を置き、本文だけが
# 拾われることをテストで担保する
GNW_ARTICLE_HTML = """
<html><body>
<div class="nav"><p>Other Corp revenue was CHF 999 million last year.</p></div>
<div class="main-body-container article-body" id="main-body-container">
<p>ZURICH, Oct. 29, 2026 -- Molecular Partners announced its Q3 results.</p>
<div><p>The Company's cash and cash equivalents were CHF 57.5 million as of September 30, 2026.</p></div>
<p>Total revenue was CHF 12.1 million in the third quarter.</p>
</div>
<div class="footer"><p>Another Corp profit was CHF 888 million.</p></div>
</body></html>
"""


class SwissParseTests(unittest.TestCase):
    def test_eqs_search_extracts_article_links(self):
        hits = swiss.parse_eqs_search(EQS_SEARCH_HTML)
        self.assertEqual(len(hits), 3)
        self.assertTrue(hits[0]["url"].endswith("426614174000_en"))
        self.assertEqual(hits[0]["title"], "Lonza Half-Year Results 2026")

    def test_eqs_article_footer(self):
        a = swiss.parse_eqs_article(eqs_article())
        self.assertEqual(a["isin"], "CH0013841017")
        self.assertEqual(a["news_id"], "2987654")
        self.assertEqual(a["published"], "2026-07-22")

    def test_money_sentences(self):
        text = ("Sales grew strongly in the period. Net sales were EUR 440.5 million, "
                "up 6 percent. The margin was 13.1 percent. Cash was CHF 89 million.")
        s = swiss.extract_money_sentences(text)
        self.assertEqual(len(s), 2)
        self.assertIn("EUR 440.5 million", s[0])
        self.assertEqual(swiss.extract_money_sentences("No figures here."), [])

    def test_results_title(self):
        self.assertTrue(swiss.is_results_title("Lonza Half-Year Results 2026"))
        self.assertTrue(swiss.is_results_title("Kardex: Umsatz im Halbjahr gestiegen"))
        self.assertFalse(swiss.is_results_title("Lonza Annual General Meeting Invitation"))
        self.assertFalse(swiss.is_results_title("Comet appoints new CFO"))


class SwissFetchTests(unittest.TestCase):
    def test_fetch_eqs_identifies_by_isin(self):
        articles = {
            "426614174000": eqs_article(),
            "426614174001": eqs_article(isin="CH9999999999", news_id="111"),
        }

        def fake_http(url, params=None, headers=None, retries=None):
            if "search-results" in url:
                return EQS_SEARCH_HTML.encode()
            for key, body in articles.items():
                if key in url:
                    return body.encode()
            raise AssertionError(f"unexpected url: {url}")

        out = swiss.fetch_eqs("lonza", 5, 400, AS_OF, http=fake_http)
        self.assertEqual(len(out), 1)  # 他社 ISIN の記事と AGM は落ちる
        self.assertEqual(out[0]["source"], "eqs")
        self.assertEqual(out[0]["source_id"], "2987654")
        self.assertEqual(out[0]["published"], "2026-07-22")
        self.assertIn("財務数値(EQS)", out[0]["abstract"])
        self.assertIn("CHF 3,374 million", out[0]["abstract"])
        rec, problems = normalize(out[0], "2026-11-10T00:00:00+00:00")
        self.assertIsNotNone(rec, problems)

    def test_fetch_eqs_search_failure_propagates(self):
        def bad_http(url, params=None, headers=None, retries=None):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            swiss.fetch_eqs("lonza", 5, 400, AS_OF, http=bad_http)

    def test_fetch_gnw_feed(self):
        def fake_http(url, params=None, headers=None, retries=None):
            self.assertIn("atomfeed/organization", url)
            return GNW_FEED.encode()

        out = swiss.fetch_gnw("basilea", 5, 400, AS_OF, http=fake_http)
        self.assertEqual(len(out), 1)  # de 版は畳まれ、非決算は落ちる
        self.assertEqual(out[0]["source"], "gnw")
        self.assertEqual(out[0]["source_id"], "3238936")
        self.assertIn("/en/", out[0]["url"])
        self.assertIn("財務数値(GlobeNewswire)", out[0]["abstract"])
        self.assertIn("CHF 232.4 million", out[0]["abstract"])
        rec, problems = normalize(out[0], "2026-11-10T00:00:00+00:00")
        self.assertIsNotNone(rec, problems)

    def test_parse_gnw_rss_feed(self):
        items = swiss.parse_gnw_feed(GNW_FEED_RSS)
        self.assertEqual(len(items), 2)  # de 版は identifier で畳まれる
        en = items[0]
        self.assertEqual(en["identifier"], "3390914")
        self.assertIn("/en/", en["link"])
        self.assertEqual(en["subjects"],
                         ["Earnings Releases and Operating Results"])
        self.assertIn("today announced", en["summary"])  # content へ fallback

    def test_fetch_gnw_rss_article_body(self):
        calls = []

        def fake_http(url, params=None, headers=None, retries=None):
            calls.append(url)
            if "atomfeed/organization" in url:
                return GNW_FEED_RSS.encode()
            return GNW_ARTICLE_HTML.encode()

        out = swiss.fetch_gnw("molecular_partners", 5, 400, AS_OF,
                              http=fake_http)
        self.assertEqual(len(out), 1)
        self.assertIn("CHF 57.5 million", out[0]["abstract"])
        self.assertNotIn("CHF 999 million", out[0]["abstract"])  # 本文の外は読まない
        self.assertNotIn("CHF 888 million", out[0]["abstract"])
        self.assertTrue(out[0]["url"].startswith(
            "https://www.globenewswire.com/"))  # 記録 URL は正規側
        article_calls = [u for u in calls if "news-release" in u]
        self.assertEqual(len(article_calls), 1)
        self.assertTrue(article_calls[0].startswith(
            "https://rss.globenewswire.com/"))  # 取得だけ rss 側

    def test_fetch_gnw_article_failure_raises(self):
        def fake_http(url, params=None, headers=None, retries=None):
            if "atomfeed/organization" in url:
                return GNW_FEED_RSS.encode()
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            swiss.fetch_gnw("molecular_partners", 5, 400, AS_OF,
                            http=fake_http)


def swiss_raw(wire, entity_id):
    marker = "EQS" if wire == "eqs" else "GlobeNewswire"
    return {"source": wire, "source_id": f"{entity_id}-2026-07-22",
            "title": f"{entity_id} Half-Year Results 2026",
            "abstract": f"{entity_id}の財務数値({marker}): 売上高 CHF 100 million。",
            "url": f"https://example.org/{wire}/{entity_id}",
            "published": "2026-07-22", "fields": [marker, "財務"],
            "affiliations": [], "info_type": "disclosure"}


ENTITIES = [
    {"id": "lonza", "name": "Lonza Group AG", "tier": "watch", "market": "CH", "ticker": "LONN"},
    {"id": "basilea", "name": "Basilea Pharmaceutica AG", "tier": "core", "market": "CH", "ticker": "BSLN"},
    {"id": "newch", "name": "New Swiss Co", "tier": "core", "market": "CH", "ticker": "NEW"},
    {"id": "jpco", "name": "JP Co", "tier": "core", "market": "JP", "ticker": "1000"},
]


def make_cfg(entities=ENTITIES):
    return {"sources": {"defaults": {"max_results": 5, "lookback_days": 90}, "queries": []},
            "entities": {"entities": entities},
            "config_hash": "testhash",
            "scoring": {"screening": {"collect_budget_sec": 1200}}}


class SwissCollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = T.Store(Path(self.tmp.name) / "d.sqlite")
        self.calls = []

        def make(wire):
            def fake(entity_id, max_results, lookback, as_of):
                self.calls.append((wire, entity_id))
                return [swiss_raw(wire, entity_id)]
            return fake

        self.fetchers = {"eqs": make("eqs"), "gnw": make("gnw")}
        self.counts = {k: 0 for k in ("fetched", "new", "duplicates", "invalid",
                                      "sources_ok", "sources_failed")}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_collect(self):
        # ゲート (swiss_enabled) を明示的に開けて collect を回す
        with mock.patch.dict(os.environ, {"TRL_SWISS_DISABLE": ""}):
            T.collect(self.store, make_cfg(), "run1", AS_OF, ("pubmed",), {},
                      self.counts, lambda *a: None, swiss_fetchers=self.fetchers)

    def test_collect_skipped_when_disabled(self):
        with mock.patch.dict(os.environ, {"TRL_SWISS_DISABLE": "1"}):
            T.collect(self.store, make_cfg(), "run1", AS_OF, ("pubmed",), {},
                      self.counts, lambda *a: None, swiss_fetchers=self.fetchers)
        self.assertNotIn("swiss_targets", self.counts)
        self.assertEqual(self.store.count_candidates(), 0)

    def test_collect_records_both_wires(self):
        self.run_collect()
        # lonza は EQS のみ (GNW 未確認)、basilea は両ワイヤ、newch は対応表外
        self.assertEqual(sorted(self.calls),
                         [("eqs", "basilea"), ("eqs", "lonza"), ("gnw", "basilea")])
        self.assertEqual(self.counts["swiss_targets"], 2)
        self.assertEqual(self.counts["swiss_gnw_unmapped"], 1)
        self.assertEqual(self.counts["swiss_skipped_unmapped"], 1)
        rows = [r for r in self.store.run_sources("run1")
                if r["source"] in ("eqs", "gnw")]
        self.assertEqual({(r["source"], r["query"]) for r in rows},
                         {("eqs", "lonza-eqs"), ("eqs", "basilea-eqs"),
                          ("gnw", "basilea-gnw")})
        self.assertTrue(all(r["ok"] and r["fetched"] == 1 for r in rows))
        cov = self.store.financial_sources_by_entity()
        self.assertEqual(cov["lonza"], {"eqs"})
        self.assertEqual(cov["basilea"], {"eqs", "gnw"})

    def test_recent_success_rests_but_zero_fetch_does_not(self):
        self.store.conn.execute(
            "INSERT INTO runs(run_id, started_at) VALUES('recent-run', '2026-11-05T00:00:00+00:00')")
        self.store.conn.execute(
            "INSERT INTO run_sources VALUES('recent-run', 'eqs', 'lonza-eqs', 1, 0, 1, 0, 1, NULL, 0.1)")
        self.store.conn.execute(
            "INSERT INTO run_sources VALUES('recent-run', 'eqs', 'basilea-eqs', 0, 0, 0, 0, 1, NULL, 0.1)")
        self.store.conn.commit()
        self.run_collect()
        self.assertNotIn(("eqs", "lonza"), self.calls)      # 5日前・取得あり → 休止
        self.assertIn(("eqs", "basilea"), self.calls)       # 5日前・取得0件 → 休まない

    def test_failure_is_recorded_not_swallowed(self):
        def bad(entity_id, max_results, lookback, as_of):
            raise RuntimeError("boom")

        self.fetchers = {"eqs": bad, "gnw": bad}
        self.run_collect()
        rows = [r for r in self.store.run_sources("run1")
                if r["source"] in ("eqs", "gnw")]
        self.assertTrue(rows)
        self.assertTrue(all(r["ok"] == 0 for r in rows))
        self.assertTrue(all("boom" in (r["error"] or "") for r in rows))


if __name__ == "__main__":
    unittest.main()
