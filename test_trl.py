"""自動テスト(外部APIに依存しない)。実行: python -m unittest test_trl -v"""
import trl as T
from trl import *  # noqa
from types import SimpleNamespace as NS

scoring = NS(compute=T.compute, validate_items=T.validate_items, band_for=T.band_for, item_keys=T.item_keys)
freshness = NS(parse_date=T.parse_date, days_since=T.days_since, freshness_info=T.freshness_info)
http = NS(fetch=T.http_fetch, redact=T.redact, HttpError=T.HttpError)
pubmed = NS(parse_efetch=T.parse_efetch, fetch=T.pubmed_fetch)
arxiv = NS(parse_feed=T.parse_feed, fetch=T.arxiv_fetch)
gnews = NS(parse_feed=T.gnews_parse_feed, fetch=T.gnews_fetch)
prtimes = NS(fetch=T.prtimes_fetch)
trials = NS(parse=T.parse_studies, fetch=T.trials_fetch)
edinet = NS(parse=T.edinet_parse_results, fetch=T.edinet_fetch, sec_code=T.sec_code,
            parse_facts_csv=T.edinet_parse_facts_csv, parse_facts_xbrl=T.edinet_parse_facts_xbrl,
            select_financials=T.edinet_select_financials, format_financials=T.edinet_format_financials)
yahoo = NS(fetch=T.yahoo_fetch, parse=T.yahoo_parse_quote_summary)
openalex = NS(parse=T.openalex_parse_works, fetch=T.openalex_fetch)
ev = NS(validate_assessment=T.validate_assessment, validate_extraction=T.validate_extraction, evaluate_one=T.evaluate_one)

import json
from datetime import date


CFG = load_config()
AS_OF = date(2026, 10, 20)
LONG_KEYS = list(CFG["scoring"]["long"]["weights"])
EVENT_KEYS = list(CFG["scoring"]["event"]["weights"])
ABSTRACT = "This study reports a new battery electrolyte with improved stability and cycle life. " * 4


def raw(source="arxiv", sid="2610.00001", title="Paper A", doi=None, published="2026-10-10", abstract=ABSTRACT, **kw):
    d = {"source": source, "source_id": sid, "title": title, "abstract": abstract, "doi": doi,
         "published": published, "url": f"https://example.org/{sid}", "fields": ["x"],
         "affiliations": ["Astroscale Holdings Inc."]}
    if source == "arxiv":
        d["arxiv_id"] = sid
    if source == "pubmed":
        d["pmid"] = sid
    d.update(kw)
    return d


def item(score, with_evidence=True):
    if score is None:
        return {"score": None, "reason": "", "evidence": [], "counter_evidence": [], "missing_info": ["情報不足"]}
    return {"score": score, "reason": "理由", "evidence": [{"ref": "https://example.org/x", "summary": "要約", "kind": "fact"}] if with_evidence else [],
            "counter_evidence": ["反証"], "missing_info": []}


def assessment(keys, scores=None, event=None):
    scores = scores or {}
    obj = {"items": {k: item(scores.get(k, 70)) for k in keys}, "next_sources": ["IR"], "reeval_conditions": ["決算後"]}
    if event is not None:
        obj["event"] = event
    return obj


EXTRACTION = {"facts": [{"statement": "f", "source": "u"}], "inferences": [], "forecasts": [], "hypotheses": [],
              "counter_points": [], "missing_info": []}


def make_llm_fn(long_scores=None, event=None, event_scores=None, bad=None):
    def fn(prompt):
        if "根拠抽出」担当" in prompt:
            return json.dumps(EXTRACTION)
        if "長期成長評価」担当" in prompt:
            if bad:
                return bad
            return json.dumps(assessment(LONG_KEYS, long_scores))
        if "イベント評価」担当" in prompt:
            return json.dumps(assessment(EVENT_KEYS, event_scores, event or {"classification": 2, "market_expectation": ""}))
        raise AssertionError("unknown prompt")
    return fn

import unittest

import copy


class ScoringTests(unittest.TestCase):
    def test_weights_sum_100(self):
        for k in ("long", "event"):
            self.assertEqual(sum(CFG["scoring"][k]["weights"].values()), 100)

    def test_bad_weights_rejected(self):
        bad = copy.deepcopy(CFG)
        bad["scoring"]["long"]["weights"]["monetization"] = 30
        with self.assertRaises(ConfigError):
            validate_config(bad)

    def test_total_computation(self):
        items = {k: 80 for k in LONG_KEYS}
        r = scoring.compute(items, "long", CFG)
        self.assertEqual((r["status"], r["total"]), ("final", 80.0))
        items = {"technology_readiness": 100, "technological_advantage": 0, "monetization": 0,
                 "market_growth": 0, "financial_sustainability": 0, "moat_durability": 0}
        self.assertEqual(scoring.compute(items, "long", CFG)["total"], 25.0)

    def test_null_means_hold_no_redistribution(self):
        items = {k: 90 for k in LONG_KEYS}
        items["monetization"] = None
        r = scoring.compute(items, "long", CFG)
        self.assertEqual(r["status"], "hold")
        self.assertIsNone(r["total"])
        self.assertEqual(r["critical_missing"], ["monetization"])

    def test_pre_stage_business_not_required(self):
        # TRL 2 で技術軸だけ揃えば、事業軸は段階未達として final (技術軸の総合点)
        items = {k: None for k in LONG_KEYS}
        items["technology_readiness"] = 80
        items["technological_advantage"] = 60
        r = scoring.compute(items, "long", CFG, trl_level=2)
        self.assertEqual(r["status"], "final")
        self.assertEqual(r["total_scope"], "technology")
        self.assertEqual(r["total"], 72.5)  # (80*25 + 60*15) / 40
        self.assertEqual(r["business_stage"], "pre_stage")
        self.assertEqual(r["axes"]["business"]["status"], "pre_stage")
        self.assertEqual(r["critical_missing"], [])  # monetization は不足に数えない

    def test_pre_stage_needs_trl_and_tech(self):
        items = {k: None for k in LONG_KEYS}
        items["technology_readiness"] = 80
        items["technological_advantage"] = 60
        # TRL が高い/未確定なら従来どおり事業項目が必須
        self.assertEqual(scoring.compute(items, "long", CFG, trl_level=5)["status"], "hold")
        self.assertEqual(scoring.compute(items, "long", CFG, trl_level=None)["status"], "hold")
        self.assertEqual(scoring.compute(items, "long", CFG)["status"], "hold")
        # 技術軸が欠けていれば TRL が低くても hold
        items["technology_readiness"] = None
        r = scoring.compute(items, "long", CFG, trl_level=1)
        self.assertEqual(r["status"], "hold")
        self.assertEqual(r["critical_missing"], ["technology_readiness"])

    def test_pre_stage_full_scores_still_full_total(self):
        items = {k: 80 for k in LONG_KEYS}
        r = scoring.compute(items, "long", CFG, trl_level=2)
        self.assertEqual((r["status"], r["total"], r["total_scope"]), ("final", 80.0, "full"))

    def test_event_hold_on_critical_missing(self):
        items = {k: 60 for k in EVENT_KEYS}
        items["not_priced_in"] = None
        r = scoring.compute(items, "event", CFG)
        self.assertEqual((r["status"], r["critical_missing"]), ("hold", ["not_priced_in"]))

    def test_range_validation(self):
        for bad in (-1, 100.5, "80", True, float("nan")):
            items = {k: 50 for k in LONG_KEYS}
            items["market_growth"] = bad
            self.assertEqual(scoring.compute(items, "long", CFG)["status"], "error", bad)
        items = {k: 0 for k in LONG_KEYS}
        self.assertEqual(scoring.compute(items, "long", CFG)["total"], 0.0)
        items = {k: 100 for k in LONG_KEYS}
        self.assertEqual(scoring.compute(items, "long", CFG)["total"], 100.0)

    def test_required_items_validated(self):
        items = {k: 50 for k in LONG_KEYS}
        del items["monetization"]
        self.assertIn("missing_item:monetization", scoring.validate_items(items, "long", CFG))
        items = {k: 50 for k in LONG_KEYS}
        items["bogus"] = 1
        self.assertIn("unknown_item:bogus", scoring.validate_items(items, "long", CFG))

    def test_bands(self):
        exp = [(0, "extremely_weak"), (19, "extremely_weak"), (20, "weak"), (39, "weak"), (40, "moderate_uncertain"),
               (59, "moderate_uncertain"), (59.5, "moderate_uncertain"), (60, "strong"), (79, "strong"),
               (80, "very_strong"), (100, "very_strong")]
        for s, label in exp:
            self.assertEqual(scoring.band_for(s, CFG)["label"], label, s)

    def test_long_and_event_not_averaged(self):
        # 2系統の結果は別々に返り、合成関数は存在しない
        self.assertFalse(hasattr(scoring, "combine"))



import io
import os
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path


PUBMED_XML = b"""<?xml version="1.0"?><PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>111</PMID>
<Article><Journal><JournalIssue><PubDate><Year>2026</Year><Month>Oct</Month></PubDate></JournalIssue></Journal>
<ArticleTitle>Gene <i>therapy</i> trial</ArticleTitle><Abstract><AbstractText Label="BACKGROUND">Bg text.</AbstractText>
<AbstractText>Plain.</AbstractText></Abstract></Article><MeshHeadingList><MeshHeading><DescriptorName>Genetic Therapy</DescriptorName></MeshHeading></MeshHeadingList></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="pubmed">111</ArticleId><ArticleId IdType="doi">10.1/ABC</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>"""
ARXIV_XML = b"""<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><entry>
<id>http://arxiv.org/abs/2610.01234v2</id><published>2026-10-05T10:00:00Z</published><title>Solid  state
 battery</title><summary> Abstract here. </summary><category term="cond-mat.mtrl-sci"/><arxiv:doi>10.1/abc</arxiv:doi></entry></feed>"""


class FreshnessTests(unittest.TestCase):
    def test_parse_and_days(self):
        self.assertEqual(freshness.parse_date("2026-10-10"), date(2026, 10, 10))
        self.assertEqual(freshness.parse_date("2026-10"), date(2026, 10, 1))
        self.assertEqual(freshness.parse_date("2026-10", end=True), date(2026, 10, 31))
        self.assertIsNone(freshness.parse_date("2026-13-01"))
        self.assertIsNone(freshness.parse_date("garbage"))
        self.assertEqual(freshness.days_since("2026-10-10", AS_OF), 10)

    def test_buckets_depend_on_as_of(self):
        self.assertEqual(freshness.freshness_info("2026-10-10", AS_OF)["bucket"], "fresh_30d")
        self.assertEqual(freshness.freshness_info("2026-10-10", date(2027, 6, 1))["bucket"], "older")
        self.assertEqual(freshness.freshness_info("2026-10-25", AS_OF)["bucket"], "future")
        self.assertEqual(freshness.freshness_info(None, AS_OF)["bucket"], "unknown")


class NormalizeTests(unittest.TestCase):
    def test_normalize_ok_and_fatal(self):
        rec, p = normalize(raw(doi="https://doi.org/10.1/ABC"), "t")
        self.assertEqual(rec["keys"][0], "doi:10.1/abc")
        self.assertIsNone(normalize(raw(title="  "), "t")[0])
        self.assertIsNone(normalize({"title": "x"}, "t")[0])
        rec, p = normalize(raw(published="bad"), "t")
        self.assertIsNone(rec["published"])
        self.assertIn("bad_published_date", p)


class SourceParseTests(unittest.TestCase):
    def test_pubmed(self):
        r = pubmed.parse_efetch(PUBMED_XML)[0]
        self.assertEqual((r["pmid"], r["doi"], r["published"]), ("111", "10.1/ABC", "2026-10"))
        self.assertEqual(r["title"], "Gene therapy trial")
        self.assertIn("BACKGROUND: Bg text.", r["abstract"])

    def test_arxiv(self):
        r = arxiv.parse_feed(ARXIV_XML)[0]
        self.assertEqual((r["arxiv_id"], r["published"], r["doi"]), ("2610.01234", "2026-10-05", "10.1/abc"))

    def test_pubmed_fetch_uses_as_of_window(self):
        calls = []

        def fake(url, params=None, **kw):
            calls.append(params)
            return b'{"esearchresult":{"idlist":["111"]}}' if "esearch" in url else PUBMED_XML
        out = pubmed.fetch("q", 5, 30, AS_OF, http=fake, sleep=lambda s: None)
        self.assertEqual(len(out), 1)
        self.assertEqual(calls[0]["maxdate"], "2026/10/20")
        self.assertEqual(calls[0]["mindate"], "2026/09/20")


GNEWS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>news</title>
<item><title>Astroscale wins a debris removal contract - Example News</title>
<link>https://news.google.com/rss/articles/AAA</link>
<pubDate>Sun, 18 Oct 2026 01:00:00 GMT</pubDate>
<description>&lt;a href="https://example.com"&gt;Astroscale wins a debris removal contract&lt;/a&gt;</description>
<source url="https://example.com">Example News</source></item>
<item><title>Old news about Astroscale funding</title>
<link>https://news.google.com/rss/articles/BBB</link>
<pubDate>Sat, 01 Aug 2026 01:00:00 GMT</pubDate>
<description>Old news about Astroscale funding</description>
<source url="https://example.com">Example News</source></item>
<item><title>Astroscale column republished by an aggregator</title>
<link>https://news.google.com/rss/articles/CCC</link>
<pubDate>Mon, 19 Oct 2026 01:00:00 GMT</pubDate>
<description>Astroscale column</description>
<source url="https://biggo.example">BigGo Finance</source></item>
</channel></rss>"""

TRIALS_DATA = {"studies": [{
    "protocolSection": {
        "identificationModule": {"nctId": "NCT00000001", "officialTitle": "Phase 1 study of PD-1",
                                 "briefTitle": "PD-1 study"},
        "descriptionModule": {"briefSummary": "A first-in-human study. " * 12,
                              "detailedDescription": "Dose escalation in patients."},
        "statusModule": {"overallStatus": "RECRUITING",
                         "lastUpdatePostDateStruct": {"date": "2026-10-01"},
                         "startDateStruct": {"date": "2026-05-01"}},
        "designModule": {"phases": ["PHASE1"]},
        "conditionsModule": {"conditions": ["Cancer"]},
        "armsInterventionsModule": {"interventions": [{"name": "PD-1"}]},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "PeptiDream Inc."}},
    }}]}

EDINET_RESULTS = [
    {"docID": "S100AAAA", "edinetCode": "E00001", "secCode": "93480", "filerName": "株式会社ispace",
     "docDescription": "臨時報告書", "submitDateTime": "2026-10-15 15:00", "periodStart": None,
     "periodEnd": None, "withdrawalStatus": "0"},
    {"docID": "S100BBBB", "edinetCode": "E00002", "secCode": "99990", "filerName": "別会社",
     "docDescription": "四半期報告書", "submitDateTime": "2026-10-15 15:00", "withdrawalStatus": "0"},
    {"docID": "S100CCCC", "edinetCode": "E00001", "secCode": "93480", "filerName": "株式会社ispace",
     "docDescription": "取下げ", "submitDateTime": "2026-10-14 15:00", "withdrawalStatus": "1"},
]


class NewSourceParseTests(unittest.TestCase):
    def test_gnews(self):
        items = gnews.parse_feed(GNEWS_XML)
        self.assertEqual(len(items), 2)  # BigGo の転載コラムは媒体フィルタで除外
        r = items[0]
        self.assertEqual((r["source"], r["info_type"], r["published"]), ("gnews", "news", "2026-10-18"))
        self.assertIn("配信元: Example News", r["abstract"])
        self.assertTrue(r["url"].startswith("https://news.google.com/"))

    def test_gnews_fetch_window(self):
        out = gnews.fetch("アストロスケール", 20, 30, AS_OF, http=lambda url, params=None, **kw: GNEWS_XML)
        self.assertEqual(len(out), 1)  # 2026-08-01 は lookback 30 日の外

    def test_gnews_fetch_with_locale_dict(self):
        calls = []

        def fake(url, params=None, **kw):
            calls.append(params)
            return GNEWS_XML

        q = {"q": "Rocket Lab (contract OR launch)", "hl": "en", "gl": "US", "ceid": "US:en"}
        out = gnews.fetch(q, 20, 30, AS_OF, http=fake)
        self.assertEqual(calls[0]["hl"], "en")
        self.assertEqual(calls[0]["gl"], "US")
        self.assertIn("Rocket Lab", calls[0]["q"])
        self.assertEqual(len(out), 1)

    def test_prtimes_fetch_uses_site_filter(self):
        calls = []

        def fake(url, params=None, **kw):
            calls.append(params)
            return GNEWS_XML
        out = prtimes.fetch("Example", 20, 30, AS_OF, http=fake)  # 固定XMLの見出し語で厳密フィルタを通す
        self.assertIn("site:prtimes.jp", calls[0]["q"])
        self.assertTrue(out)
        self.assertTrue(all(r["source"] == "prtimes" for r in out))

    def test_openalex_parse_and_fetch(self):
        data = {"results": [{
            "id": "https://openalex.org/W123", "doi": "https://doi.org/10.1000/xyz",
            "title": "On-orbit demonstration of a debris removal system",
            "publication_date": "2026-09-15",
            "abstract_inverted_index": {"On-orbit": [0], "demonstration": [1], "results": [2]},
            "authorships": [{"institutions": [{"display_name": "Astroscale"}]},
                            {"institutions": [{"display_name": "University of Tokyo"},
                                              {"display_name": "Astroscale"}]}],
            "primary_location": {"source": {"display_name": "Acta Astronautica"}},
            "type": "article"}]}
        r = openalex.parse(data)[0]
        self.assertEqual((r["source"], r["source_id"], r["info_type"]), ("openalex", "W123", "paper"))
        self.assertEqual(r["abstract"], "On-orbit demonstration results")
        self.assertEqual(r["affiliations"], ["Astroscale", "University of Tokyo"])
        self.assertEqual(r["doi"], "10.1000/xyz")
        self.assertEqual(r["fields"], ["Acta Astronautica"])
        seen = {}
        def stub(url, params=None, **kw):
            seen.update(params or {})
            return json.dumps(data)
        out = openalex.fetch("I999", 5, 365, AS_OF, http=stub)
        self.assertEqual(len(out), 1)
        # フィルタのフィールド名は複数形 institutions(単数形はAPIが拒否する)
        self.assertIn("authorships.institutions.id:I999", seen.get("filter", ""))

    def test_translation(self):
        self.assertTrue(T.needs_translation("High-throughput actinic mask inspection for EUV lithography"))
        self.assertFalse(T.needs_translation("月面探査ローバーの熱制御の設計と実装"))
        self.assertFalse(T.needs_translation("AI"))
        # Google: キーはヘッダーで送り、応答の HTML エスケープは戻す
        seen = {}
        def g_http(url, params=None, headers=None, data=None, **kw):
            seen["headers"] = headers or {}
            qs = json.loads(data)["q"]
            return json.dumps({"data": {"translations": [{"translatedText": "訳&amp;:" + q} for q in qs]}})
        out = T.google_translate(["Title"], "gkey", http=g_http)
        self.assertEqual(out, ["訳&:Title"])
        self.assertEqual(seen["headers"].get("X-Goog-Api-Key"), "gkey")

    def test_translate_display_set(self):
        # 最終段の Google で訳し、出典に google が記録される
        import os
        from unittest import mock as um
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")
        rec, _ = T.normalize(raw(), T.now_iso())
        cid, _ = store.upsert_candidate(rec, "rX")
        store.conn.execute(
            "INSERT INTO evaluations(run_id, candidate_id, kind, status) VALUES('rX', ?, 'long', 'ok')", (cid,))
        store.conn.commit()

        def g_http(url, params=None, headers=None, data=None, **kw):
            qs = json.loads(data)["q"]
            return json.dumps({"data": {"translations": [{"translatedText": "訳:" + q} for q in qs]}})
        with um.patch.dict(os.environ, {"GOOGLE_TRANSLATE_API_KEY": "gk"}, clear=False):
            n = T.translate_display_set(store, CFG, AS_OF, log=lambda *_: None, http=g_http)
            self.assertEqual(n, 1)
            c = store.get_candidate(cid)
            self.assertTrue(c["title_ja"].startswith("訳:"))
            self.assertEqual(json.loads(c["translation_src"]), {"title": "google", "abstract": "google"})
            # 2回目は未訳が無いので 0 件
            self.assertEqual(T.translate_display_set(store, CFG, AS_OF, log=lambda *_: None, http=g_http), 0)
        store.close()
        tmp.cleanup()

    def test_mt_providers_and_fallback(self):
        import os
        from unittest import mock as um
        # DeepL: 無料キーは api-free ホスト、応答は入力と同じ並びで返る
        seen = {}
        def deepl_http(url, params=None, headers=None, data=None, **kw):
            seen["url"] = url
            texts = json.loads(data)["text"]
            return json.dumps({"translations": [{"text": "訳:" + t} for t in texts]})
        out = T.deepl_translate(["Title A", "Abstract A"], "key:fx", http=deepl_http)
        self.assertEqual(out, ["訳:Title A", "訳:Abstract A"])
        self.assertIn("api-free.deepl.com", seen["url"])
        # Azure: 配列形式の応答を展開する
        def azure_http(url, params=None, headers=None, data=None, **kw):
            return json.dumps([{"translations": [{"text": "訳:" + d["Text"]}]} for d in json.loads(data)])
        self.assertEqual(T.azure_translate(["X"], "k", http=azure_http), ["訳:X"])
        # 経路の優先順位: 本命 Azure が失敗したら DeepL、それも無ければ LLM。出典も記録される
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")
        rec, _ = T.normalize(raw(), T.now_iso())
        cid, _ = store.upsert_candidate(rec, "rX")
        store.conn.execute(
            "INSERT INTO evaluations(run_id, candidate_id, kind, status) VALUES('rX', ?, 'long', 'ok')", (cid,))
        store.conn.commit()

        def flaky_http(url, params=None, headers=None, data=None, **kw):
            if "microsofttranslator" in url:
                raise RuntimeError("azure down")
            return deepl_http(url, params=params, headers=headers, data=data)
        env = {"DEEPL_API_KEY": "k:fx", "AZURE_TRANSLATOR_KEY": "k2"}
        with um.patch.dict(os.environ, env, clear=False):
            n = T.translate_display_set(store, CFG, AS_OF, log=lambda *_: None, http=flaky_http)
        self.assertEqual(n, 1)
        c = store.get_candidate(cid)
        self.assertTrue(c["title_ja"].startswith("訳:"))
        self.assertEqual(json.loads(c["translation_src"]), {"title": "deepl", "abstract": "deepl"})
        store.close()
        tmp.cleanup()

    def test_trials(self):
        r = trials.parse(TRIALS_DATA)[0]
        self.assertEqual((r["source"], r["info_type"], r["published"]), ("trials", "trial", "2026-10-01"))
        self.assertEqual(r["url"], "https://clinicaltrials.gov/study/NCT00000001")
        self.assertIn("Status: RECRUITING", r["abstract"])
        self.assertEqual(r["affiliations"], ["PeptiDream Inc."])

    def test_trials_results_posted(self):
        data = json.loads(json.dumps(TRIALS_DATA))
        data["studies"][0]["hasResults"] = True
        r = trials.parse(data)[0]
        self.assertIn("Results: Posted", r["abstract"])

    def test_edinet_sec_code_and_parse(self):
        self.assertEqual(edinet.sec_code("9348"), "93480")
        self.assertEqual(edinet.sec_code("186A"), "186A0")
        out = edinet.parse(EDINET_RESULTS, "9348")
        self.assertEqual(len(out), 1)  # 他社と取下げを除外
        self.assertEqual(out[0]["info_type"], "disclosure")
        self.assertIn("臨時報告書", out[0]["abstract"])

    def test_edinet_without_key_fetches_nothing(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EDINET_API_KEY", None)
            called = []
            out = edinet.fetch("9348", 20, 30, AS_OF, http=lambda *a, **k: called.append(a) or b"{}")
        self.assertEqual(out, [])
        self.assertEqual(called, [])


def fin_csv_bytes():
    """EDINET type=5 の CSV 形式 (UTF-16・タブ区切り・全項目クォート) の最小再現。"""
    header = ["要素ID", "項目名", "コンテキストID", "相対年度", "連結・個別", "期間・時点", "ユニットID", "通貨", "単位", "値"]
    rows = [
        ["jpsps:NetSalesSummaryOfBusinessResults", "売上高", "CurrentYearDuration", "当期", "連結", "期間", "JPY", "JPY", "円", "12345000000"],
        ["jpsps:NetSalesSummaryOfBusinessResults", "売上高", "PriorYearDuration", "前期", "連結", "期間", "JPY", "JPY", "円", "10000000000"],
        ["jpsps:NetSalesSummaryOfBusinessResults", "売上高", "CurrentYearDuration_NonConsolidatedMember", "当期", "個別", "期間", "JPY", "JPY", "円", "99999000000"],
        ["jpsps:OperatingIncomeLossSummaryOfBusinessResults", "営業利益", "CurrentYearDuration", "当期", "連結", "期間", "JPY", "JPY", "円", "-500000000"],
        ["jpsps:CashAndCashEquivalentsSummaryOfBusinessResults", "現金及び現金同等物", "CurrentYearInstant", "当期", "連結", "時点", "JPY", "JPY", "円", "30000000000"],
        ["jpsps:EquityToAssetRatioSummaryOfBusinessResults", "自己資本比率", "CurrentYearInstant", "当期", "連結", "時点", "pure", "", "", "0.512"],
    ]
    lines = ["\t".join(f'"{c}"' for c in header)] + ["\t".join(f'"{c}"' for c in r) for r in rows]
    return ("\r\n".join(lines) + "\r\n").encode("utf-16")


FIN_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.w3.org/2003/xbrl/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
  xmlns:jpsps="http://example.jp/jpsps" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<xbrli:context id="CurrentYearDuration"><xbrli:entity><xbrli:identifier scheme="s">E00001</xbrli:identifier></xbrli:entity>
<xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2027-03-31</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:context id="CurrentYearInstant"><xbrli:entity><xbrli:identifier scheme="s">E00001</xbrli:identifier></xbrli:entity>
<xbrli:period><xbrli:instant>2027-03-31</xbrli:instant></xbrli:period></xbrli:context>
<xbrli:context id="CurrentYearInstant_NonConsolidatedMember"><xbrli:entity><xbrli:identifier scheme="s">E00001</xbrli:identifier>
<xbrli:segment><xbrldi:explicitMember dimension="d">jpcrp:NonConsolidatedMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
<xbrli:period><xbrli:instant>2027-03-31</xbrli:instant></xbrli:period></xbrli:context>
<jpsps:NetSalesSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPY" decimals="-6">12345000000</jpsps:NetSalesSummaryOfBusinessResults>
<jpsps:NetAssetsSummaryOfBusinessResults contextRef="CurrentYearInstant" unitRef="JPY" decimals="-6">8000000000</jpsps:NetAssetsSummaryOfBusinessResults>
<jpsps:NetAssetsSummaryOfBusinessResults contextRef="CurrentYearInstant_NonConsolidatedMember" unitRef="JPY" decimals="-6">9999999999</jpsps:NetAssetsSummaryOfBusinessResults>
</xbrli:xbrl>"""


class EdinetFinancialTests(unittest.TestCase):
    def test_csv_facts_select_format(self):
        facts = edinet.parse_facts_csv(fin_csv_bytes())
        sel = edinet.select_financials(facts)
        self.assertEqual(sel["売上高"]["当期"], 12345000000.0)  # 個別の大きな値でなく連結を選ぶ
        self.assertEqual(sel["売上高"]["前期"], 10000000000.0)
        text = edinet.format_financials(sel)
        self.assertIn("売上高 12,345百万円(前期 10,000百万円)", text)
        self.assertIn("営業利益 △500百万円", text)
        self.assertIn("現金及び現金同等物 30,000百万円", text)
        self.assertIn("自己資本比率 51.2%", text)

    def test_xbrl_facts_fallback(self):
        facts = edinet.parse_facts_xbrl(FIN_XML)
        sel = edinet.select_financials(facts)
        self.assertEqual(sel["売上高"]["当期"], 12345000000.0)
        self.assertEqual(sel["純資産"]["当期"], 8000000000.0)  # 非連結メンバーは選ばない

    def test_fetch_enriches_financial_doc(self):
        import os
        from unittest import mock
        from trlcore.sources import edinet as edinet_mod
        edinet_mod._day_cache.clear()
        edinet_mod._doc_cache.clear()
        fin_doc = {"docID": "S100FIN", "edinetCode": "E00001", "secCode": "93480", "filerName": "株式会社ispace",
                   "docDescription": "第2四半期報告書", "docTypeCode": "140", "xbrlFlag": "1",
                   "submitDateTime": "2026-10-19 15:00", "periodStart": "2026-04-01",
                   "periodEnd": "2026-09-30", "withdrawalStatus": "0"}

        def fake_http(url, params=None, headers=None, **kw):
            if url.endswith("documents.json"):
                if params.get("date") == "2026-10-19":
                    return json.dumps({"results": [fin_doc]}).encode()
                return json.dumps({"results": []}).encode()
            if "/documents/S100FIN" in url:
                return fin_csv_bytes()
            raise AssertionError(url)

        with mock.patch.dict(os.environ, {"EDINET_API_KEY": "test-key"}):
            out = edinet.fetch("9348", 20, 30, AS_OF, http=fake_http, sleep=lambda *_: None)
        self.assertEqual(len(out), 1)
        self.assertIn("財務数値(EDINET XBRL)", out[0]["abstract"])
        self.assertIn("売上高 12,345百万円", out[0]["abstract"])

    def test_fetch_financial_failure_falls_back_to_metadata(self):
        import os
        from unittest import mock
        from trlcore.sources import edinet as edinet_mod
        edinet_mod._day_cache.clear()
        edinet_mod._doc_cache.clear()
        fin_doc = {"docID": "S100FIN", "edinetCode": "E00001", "secCode": "93480", "filerName": "株式会社ispace",
                   "docDescription": "第2四半期報告書", "docTypeCode": "140", "xbrlFlag": "1",
                   "submitDateTime": "2026-10-19 15:00", "periodStart": "2026-04-01",
                   "periodEnd": "2026-09-30", "withdrawalStatus": "0"}

        def fake_http(url, params=None, headers=None, **kw):
            if url.endswith("documents.json"):
                if params.get("date") == "2026-10-19":
                    return json.dumps({"results": [fin_doc]}).encode()
                return json.dumps({"results": []}).encode()
            raise RuntimeError("HTTP 500")

        with mock.patch.dict(os.environ, {"EDINET_API_KEY": "test-key"}):
            out = edinet.fetch("9348", 20, 30, AS_OF, http=fake_http, sleep=lambda *_: None)
        self.assertEqual(len(out), 1)
        self.assertNotIn("財務数値", out[0]["abstract"])


YAHOO_SUMMARY = {
    "quoteSummary": {"result": [{
        "incomeStatementHistoryQuarterly": {"incomeStatementHistory": [
            {"endDate": {"raw": 1782777600, "fmt": "2026-06-30"},
             "totalRevenue": {"raw": 168000000}, "operatingIncome": {"raw": -5500000000},
             "netIncome": {"raw": -6336000000}}]},
        "incomeStatementHistory": {"incomeStatementHistory": [
            {"endDate": {"raw": 1743379200, "fmt": "2025-03-31"},
             "totalRevenue": {"raw": 1000000000}, "netIncome": {"raw": -9000000000}}]},
        "balanceSheetHistory": {"balanceSheetStatements": [
            {"endDate": {"raw": 1743379200, "fmt": "2025-03-31"},
             "cash": {"raw": 25700000000}, "totalAssets": {"raw": 40000000000},
             "totalStockholderEquity": {"raw": 10000000000},
             "shortLongTermDebt": {"raw": 5000000000}, "longTermDebt": {"raw": 24760000000}}]},
        "cashflowStatementHistoryQuarterly": {"cashflowStatements": [
            {"endDate": {"raw": 1782777600, "fmt": "2026-06-30"},
             "totalCashFromOperatingActivities": {"raw": -4000000000}}]},
        "financialData": {"totalCash": {"raw": 25700000000}, "totalDebt": {"raw": 29760000000},
                          "currentRatio": {"raw": 3.89}},
        "quoteType": {"longName": "ispace, Inc.", "shortName": "ispace"},
    }], "error": None}}


class YahooFinancialTests(unittest.TestCase):
    def setUp(self):
        from trlcore.sources import yahoo as yahoo_mod
        s = yahoo_mod._sess()
        s["crumb"] = None
        s["opener"] = None
        s["ua"] = None

    def test_session_ua_from_pool_and_stable(self):
        from trlcore.sources import yahoo as yahoo_mod
        ua1 = yahoo_mod._session_ua()
        self.assertIn(ua1, yahoo_mod._UA_POOL)
        self.assertEqual(yahoo_mod._session_ua(), ua1)  # セッション内では変わらない

    def test_parse_quote_summary(self):
        text, period, name = yahoo.parse(YAHOO_SUMMARY)
        self.assertEqual(period, "2026-06-30")
        self.assertEqual(name, "ispace, Inc.")
        self.assertIn("売上高 168百万円", text)
        self.assertIn("営業利益 △5,500百万円", text)
        self.assertIn("自己資本比率 25.0%", text)
        self.assertIn("現預金 25,700百万円", text)
        self.assertIn("流動比率 3.89", text)

    def test_parse_quote_summary_foreign_currency(self):
        import copy
        data = copy.deepcopy(YAHOO_SUMMARY)
        data["quoteSummary"]["result"][0]["price"] = {"currency": "USD"}
        text, _, _ = yahoo.parse(data)
        self.assertIn("売上高 168百万米ドル", text)
        self.assertNotIn("百万円", text)

    def test_fetch_builds_disclosure_candidate(self):
        def fake_http(url, params=None, headers=None, **kw):
            if "getcrumb" in url:
                return b"crumb123"
            if "fc.yahoo.com" in url:
                raise RuntimeError("404")
            if "quoteSummary" in url:
                return json.dumps(YAHOO_SUMMARY).encode()
            raise AssertionError(url)

        out = yahoo.fetch("9348.T", 5, 400, AS_OF, http=fake_http, sleep=lambda *_: None)
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertEqual((r["source"], r["info_type"], r["source_id"]), ("yahoo", "disclosure", "9348-2026-06-30"))
        self.assertIn("財務数値(Yahoo Finance)", r["abstract"])
        self.assertEqual(r["affiliations"], ["ispace, Inc."])

    def test_fetch_stale_period_returns_nothing(self):
        def fake_http(url, params=None, headers=None, **kw):
            if "getcrumb" in url:
                return b"crumb123"
            if "fc.yahoo.com" in url:
                raise RuntimeError("404")
            return json.dumps(YAHOO_SUMMARY).encode()

        out = yahoo.fetch("9348.T", 5, 400, date(2028, 1, 1), http=fake_http, sleep=lambda *_: None)
        self.assertEqual(out, [])


class FinancialPoolTests(unittest.TestCase):
    """財務根拠プール: EDINET 開示の財務数値を同一企業の評価へ同梱する。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "d" / "trl.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def _add(self, store, r):
        rec, _ = normalize(r, "2026-10-20T00:00:00+00:00")
        cid, _ = store.upsert_candidate(rec, "r0")
        store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
        store.set_status(cid, "screened")
        return cid

    def test_related_financials_pool(self):
        store = Store(self.path)
        paper = self._add(store, raw(sid="2610.7777", title="Debris removal study",
                                     affiliations=["Astroscale Holdings Inc."]))
        fin = {"source": "edinet", "source_id": "S100FIN", "title": "アストロスケール 第2四半期報告書",
               "abstract": "EDINETに第2四半期報告書を提出した。財務数値(EDINET XBRL): 売上高 1,234百万円、現金及び現金同等物 30,000百万円。",
               "url": "https://example.org/fin", "published": "2026-10-15",
               "fields": ["EDINET", "四半期報告書"], "affiliations": ["Astroscale Holdings Inc."],
               "info_type": "disclosure"}
        self._add(store, fin)
        yf = {"source": "yahoo", "source_id": "186A-2026-06-30", "title": "Astroscale Holdings Inc. 財務情報",
              "abstract": "Astroscale Holdings Inc.(186A)の財務数値(Yahoo Finance): 直近四半期 売上高 1,200百万円。",
              "url": "https://example.org/yf", "published": "2026-06-30",
              "fields": ["Yahoo Finance", "財務"], "affiliations": ["Astroscale Holdings Inc."],
              "info_type": "disclosure"}
        self._add(store, yf)
        plain = dict(fin, source_id="S100PLAIN", title="アストロスケール 臨時報告書",
                     abstract="EDINETに臨時報告書を提出した。")
        self._add(store, plain)
        rel = store.related_financials(paper, AS_OF)
        self.assertEqual(len(rel), 2)  # EDINET と Yahoo の両方が対象。数値マーカーの無い開示は入らない
        self.assertTrue(all("財務数値(" in r["text"] for r in rel))
        store.close()

    def test_evaluate_one_includes_financials(self):
        cand = {"id": "c1", "title": "T", "abstract": "A" * 300, "url": "u", "source": "trials",
                "published": "2026-10-10", "fields": []}
        seen = []
        base = make_llm_fn(long_scores={k: 70 for k in LONG_KEYS})

        def fn(p):
            seen.append(p)
            return base(p)

        fins = [{"title": "X 四半期報告書", "published": "2026-10-15", "url": "https://example.org/f",
                 "text": "財務数値(EDINET XBRL): 売上高 1,234百万円、現金及び現金同等物 30,000百万円。"}]
        res = ev.evaluate_one(cand, "long", CFG, Mock(fn), AS_OF, financials=fins)
        self.assertEqual(res["extra"]["related_financials_count"], 1)
        self.assertTrue(any("関連財務根拠" in p and "30,000百万円" in p for p in seen))


class DirectionTests(unittest.TestCase):
    """材料の方向性: 点数とは独立に追い風/逆風を判定し、サイトへバッジ表示する。"""

    def test_parse_direction(self):
        self.assertEqual(T.parse_direction({"direction": "negative", "reason": "治験が中止された"}),
                         {"direction": "negative", "reason": "治験が中止された"})
        self.assertEqual(T.parse_direction({"direction": "positive"})["direction"], "positive")
        self.assertIsNone(T.parse_direction({"direction": "bullish"}))  # 未知の値は捨てる
        self.assertIsNone(T.parse_direction({"direction": None}))
        self.assertIsNone(T.parse_direction(None))
        self.assertIsNone(T.parse_direction("positive"))

    def _eval_with_direction(self, direction_obj):
        cand = {"id": "c1", "title": "T", "abstract": "A" * 300, "url": "u", "source": "trials",
                "published": "2026-10-10", "fields": []}
        base = make_llm_fn(long_scores={k: 70 for k in LONG_KEYS})

        def fn(p):
            out = base(p)
            if "長期成長評価" in p:
                obj = json.loads(out)
                obj["direction"] = direction_obj
                return json.dumps(obj)
            return out

        return ev.evaluate_one(cand, "long", CFG, Mock(fn), AS_OF)

    def test_evaluate_one_records_direction(self):
        res = self._eval_with_direction({"direction": "negative", "reason": "第3相治験が中止された"})
        self.assertEqual(res["status"], "final")  # 全項目に点が付いても方向は negative であり得る
        self.assertEqual(res["extra"]["direction"],
                         {"direction": "negative", "reason": "第3相治験が中止された"})

    def test_invalid_direction_does_not_fail_evaluation(self):
        res = self._eval_with_direction({"direction": "strong_buy"})
        self.assertEqual(res["status"], "final")
        self.assertIsNone(res["extra"]["direction"])

    def test_site_data_includes_direction(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "trl.sqlite")
        rec, _ = normalize(raw(sid="2610.8888", title="Terminated trial study"),
                           "2026-10-20T00:00:00+00:00")
        cid, _ = store.upsert_candidate(rec, "r0")
        store.set_status(cid, "screened")
        res = self._eval_with_direction({"direction": "mixed", "reason": "進展と悪材料が混在"})
        store.add_evaluation("r0", cid, res)
        data = T.build_site_data(store, CFG)
        cand = [c for c in data["candidates"] if c["id"] == cid][0]
        self.assertEqual(cand["long"]["direction"], {"direction": "mixed", "reason": "進展と悪材料が混在"})
        store.close()

    def test_site_splits_awaiting_from_missing(self):
        from trlcore.site import _ev_for_site, _split_missing
        self.assertEqual(_split_missing(["結果待ち：第II相の最終結果", "価格の根拠", "結果待ち: 承認審査"]),
                         (["価格の根拠"], ["第II相の最終結果", "承認審査"]))
        ev = {"kind": "long", "status": "hold", "total": None, "band": None,
              "as_of": "2026-10-20", "evaluated_at": "2026-10-20T00:00:00+00:00",
              "items_json": json.dumps({"monetization": {"score": None, "reason": "",
                  "evidence": [], "counter_evidence": [],
                  "missing_info": ["価格設定の根拠", "結果待ち：第II相試験の最終結果"]}}),
              "extra_json": json.dumps({"trl_level": {"level": None, "reason": "",
                  "missing_info": ["結果待ち：主要評価項目の達成状況", "対象技術の特定"]}}),
              "errors_json": "[]"}
        out = _ev_for_site(ev, CFG)
        item = out["items"]["monetization"]
        self.assertEqual(item["missing_info"], ["価格設定の根拠"])
        self.assertEqual(item["awaiting_info"], ["第II相試験の最終結果"])
        self.assertEqual(out["trl_level"]["missing_info"], ["対象技術の特定"])
        self.assertEqual(out["trl_level"]["awaiting_info"], ["主要評価項目の達成状況"])


class DedupeStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.sqlite"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def up(self, **kw):
        rec, _ = normalize(raw(**kw), "t")
        return self.store.upsert_candidate(rec)

    def test_trials_upsert_updates_abstract_on_change(self):
        rec1, _ = normalize(trials.parse(TRIALS_DATA)[0], "2026-10-20T00:00:00+00:00")
        cid, is_new = self.store.upsert_candidate(rec1, "r0")
        self.assertTrue(is_new)
        data = json.loads(json.dumps(TRIALS_DATA))
        data["studies"][0]["hasResults"] = True
        data["studies"][0]["protocolSection"]["statusModule"]["overallStatus"] = "COMPLETED"
        rec2, _ = normalize(trials.parse(data)[0], "2026-10-21T00:00:00+00:00")
        cid2, is_new2 = self.store.upsert_candidate(rec2, "r1")
        self.assertEqual((cid2, is_new2), (cid, False))
        cur = self.store.get_candidate(cid)
        self.assertIn("Results: Posted", cur["abstract"])
        self.assertIn("Status: COMPLETED", cur["abstract"])
        self.assertTrue(cur["material_updated_at"])

    def test_non_trials_upsert_keeps_abstract(self):
        cid, _ = self.up(sid="2610.7777")
        rec2, _ = normalize(raw(sid="2610.7777", abstract="書き換えた要旨です。 " * 5), "t2")
        self.store.upsert_candidate(rec2)
        cur = self.store.get_candidate(cid)
        self.assertNotIn("書き換えた要旨", cur["abstract"])
        self.assertFalse(cur["material_updated_at"])

    def test_same_id_not_duplicated(self):
        a, n1 = self.up()
        b, n2 = self.up()
        self.assertEqual((a, n1, n2), (b, True, False))
        self.assertEqual(self.store.count_candidates(), 1)

    def test_merge_across_sources_by_doi(self):
        a, _ = self.up(source="pubmed", sid="111", doi="10.1/abc")
        b, new = self.up(source="arxiv", sid="2610.9", doi="https://doi.org/10.1/ABC", title="Different title")
        self.assertEqual(a, b)
        self.assertFalse(new)

    def test_similar_titles_not_merged(self):
        a, _ = self.up(sid="1", title="Same Title")
        b, new = self.up(sid="2", title="Same Title")
        self.assertNotEqual(a, b)
        self.assertTrue(new)

    def test_missing_fields_filled_on_merge(self):
        a, _ = self.up(sid="1", abstract="")
        self.up(sid="1", abstract="now present")
        self.assertEqual(self.store.get_candidate(a)["abstract"], "now present")

    def test_persistence_across_reopen(self):
        a, _ = self.up()
        self.store.upsert_queue(a, "long", "r", ["m"], ["IR"], ["c"], "t", {"x": 1})
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get_candidate(a)["title"], "Paper A")
        q = self.store.list_queue()
        self.assertEqual((len(q), q[0]["missing"], q[0]["next_sources"]), (1, ["m"], ["IR"]))
        self.up()  # 再取得しても増えない
        self.assertEqual(self.store.count_candidates(), 1)
        self.store.resolve_queue(a, "long")
        self.assertEqual(self.store.list_queue(), [])


class PromptTests(unittest.TestCase):
    def test_render(self):
        self.assertEqual(render("a {{x}} b", {"x": "1"}), "a 1 b")
        with self.assertRaises(PromptError):
            render("{{x}} {{y}}", {"x": 1})

    def test_inserted_values_not_reexpanded(self):
        self.assertEqual(render("{{a}}", {"a": "{{b}}", "b": "BAD"}), "{{b}}")

    def test_real_prompts_load_and_have_versions(self):
        for n in ("extract", "long", "event"):
            p = load_prompt(n, CFG["scoring"]["prompts"][n])
            self.assertEqual(len(p["sha"]), 12)

    def test_long_prompt_contains_trl_definitions(self):
        text = load_prompt("long", CFG["scoring"]["prompts"]["long"])["text"]
        for anchor in ("TRL 1", "TRL 9", "実施済みだけを数える", "迷ったら切り下げる",
                       "セクター別の運用", "条件及び期限付き承認", "軌道上での技術実証",
                       "テープアウト", "PoC", "商用展開と TRL の分離",
                       "意図した性能・安定性", "再現性(規格・歩留まり)", "デザインウィン",
                       "公開ベンチマークのスコア公表だけでは", "熱真空試験に合格",
                       "実臨床での使用を通じて", "販売数量や売上は",
                       "推測で補ってはならない", "投資上の成長余地を推論してはならない",
                       "TRL 9 の確認事項"):
            self.assertIn(anchor, text)

    def test_long_prompt_contains_direction_guidance(self):
        text = load_prompt("long", CFG["scoring"]["prompts"]["long"])["text"]
        for anchor in ("材料の方向性", "positive", "negative", "mixed", "点数や TRL とは独立"):
            self.assertIn(anchor, text)

    def test_prompts_contain_score_anchors(self):
        # 6項目の帯域アンカー (何点に何が要るか) が欠けないこと
        text = load_prompt("long", CFG["scoring"]["prompts"]["long"])["text"]
        for anchor in ("[採点アンカー]", "technology_readiness", "ランウェイ", "59 を上限の目安",
                       "根拠の質が低い側の帯域"):
            self.assertIn(anchor, text)
        ev = load_prompt("event", CFG["scoring"]["prompts"]["event"])["text"]
        for anchor in ("[採点アンカー]", "catalyst_quality", "downside_resilience"):
            self.assertIn(anchor, ev)

    def test_long_prompt_missing_info_policy(self):
        # 原理的に資料に無い情報を不足として数えない方針と、結果待ちの分離規約
        text = load_prompt("long", CFG["scoring"]["prompts"]["long"])["text"]
        for anchor in ("[不足の扱い]", "原理的に含まれない種類の情報", "代理指標",
                       "結果待ち：", "将来予測が無いことだけを理由に null にしない"):
            self.assertIn(anchor, text)

    def test_long_prompt_contains_low_band_procedure(self):
        # TRL 1〜3 が null に逃げないための判定手順・原則・具体例が欠けないこと
        text = load_prompt("long", CFG["scoring"]["prompts"]["long"])["text"]
        for anchor in ("[判定手順]", "原則として 1〜3 のいずれかに必ず位置づけられる",
                       "null は最後の手段", "1〜3 を否定する理由にはならない",
                       "[具体例]", "誤り率の閾値", "合成経路を提案"):
            self.assertIn(anchor, text)


class HttpTests(unittest.TestCase):
    def test_retry_then_success(self):
        seq = [urllib.error.HTTPError("u", 503, "x", {}, None), io.BytesIO(b"ok")]

        def opener(req, timeout):
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        self.assertEqual(http.fetch("http://x", opener=opener, sleep=lambda s: None), b"ok")

    def test_no_retry_on_4xx_and_error_hides_key(self):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "x", {}, None)
        with self.assertRaises(http.HttpError) as cm:
            http.fetch("http://x/api", params={"api_key": "SECRETSECRET"}, opener=opener, sleep=lambda s: None)
        self.assertNotIn("SECRETSECRET", str(cm.exception))
        self.assertEqual(cm.exception.status, 401)

    def test_gives_up_after_retries(self):
        n = []

        def opener(req, timeout):
            n.append(1)
            raise urllib.error.URLError("down")
        with self.assertRaises(http.HttpError):
            http.fetch("http://x", retries=2, opener=opener, sleep=lambda s: None)
        self.assertEqual(len(n), 3)

    def test_redact_env_secret(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-abcdef123456"
        try:
            self.assertNotIn("sk-abcdef123456", http.redact("fail sk-abcdef123456 here"))
        finally:
            del os.environ["ANTHROPIC_API_KEY"]




class BomTests(unittest.TestCase):
    def test_bom_files_load(self):
        d = Path(tempfile.mkdtemp())
        (d / "long.v9.md").write_bytes(b"\xef\xbb\xbf{{x}} \xe3\x81\x82")
        p = load_prompt("long", "v9", d)
        self.assertEqual(render(p["text"], {"x": "1"}), "1 あ")
        for f in ("scoring", "sources"):
            (d / f"{f}.json").write_bytes(b"\xef\xbb\xbf" + (T.ROOT / f"{f}.json").read_bytes().lstrip(b"\xef\xbb\xbf"))
        self.assertEqual(load_config(d)["config_version"], CFG["config_version"])

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path



class ValidationTests(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(extract_json('```json\n{"a":1}\n```'), {"a": 1})
        for bad in ("", "no json", "{broken", "[1,2]"):
            with self.assertRaises(LLMError):
                extract_json(bad)

    def test_valid_assessment(self):
        s, d, e = ev.validate_assessment(assessment(LONG_KEYS), "long", CFG)
        self.assertEqual(e, [])

    def test_invalid_assessments(self):
        a = assessment(LONG_KEYS)
        del a["items"]["monetization"]
        self.assertIn("missing_item:monetization", ev.validate_assessment(a, "long", CFG)[2])
        a = assessment(LONG_KEYS)
        a["items"]["market_growth"]["evidence"] = []
        self.assertIn("evidence_required:market_growth", ev.validate_assessment(a, "long", CFG)[2])
        a = assessment(LONG_KEYS)
        a["items"]["market_growth"]["reason"] = ""
        self.assertIn("reason_required:market_growth", ev.validate_assessment(a, "long", CFG)[2])
        a = assessment(LONG_KEYS, {"market_growth": None})
        a["items"]["market_growth"]["missing_info"] = []
        self.assertIn("missing_info_required_for_null:market_growth", ev.validate_assessment(a, "long", CFG)[2])
        a = assessment(LONG_KEYS)
        a["items"]["market_growth"]["evidence"][0]["kind"] = "rumor"
        self.assertTrue(any(x.startswith("bad_evidence") for x in ev.validate_assessment(a, "long", CFG)[2]))
        self.assertEqual(ev.validate_assessment({"items": []}, "long", CFG)[2], ["items:must_be_object"])

    def test_extraction_validation(self):
        self.assertTrue(ev.validate_extraction({}))
        self.assertEqual(ev.validate_extraction({"facts": [{"statement": "x"}], "inferences": [], "forecasts": [],
                                                 "hypotheses": [], "counter_points": [], "missing_info": []}), [])


class EvaluateOneTests(unittest.TestCase):
    cand = {"id": "c1", "title": "T", "abstract": "A" * 300, "url": "u", "source": "arxiv",
            "published": "2026-10-10", "fields": []}

    def run_it(self, kind="long", **kw):
        return ev.evaluate_one(self.cand, kind, CFG, Mock(make_llm_fn(**kw)), AS_OF)

    def test_long_final(self):
        r = self.run_it(long_scores={k: 60 for k in LONG_KEYS})
        self.assertEqual((r["status"], r["total"], r["band"]["label"]), ("final", 60.0, "strong"))
        self.assertEqual(set(r["prompt_versions"]), {"extract", "long"})

    def test_long_hold_on_null(self):
        r = self.run_it(long_scores={"monetization": None})
        self.assertEqual((r["status"], r["total"], r["critical_missing"]), ("hold", None, ["monetization"]))

    def test_invalid_ai_response_is_error(self):
        r = self.run_it(bad="not json at all")
        self.assertEqual(r["status"], "error")
        r = self.run_it(long_scores={"monetization": 150})
        self.assertEqual(r["status"], "error")
        self.assertIn("out_of_range:monetization", r["errors"])

    def test_llm_exception_is_contained(self):
        def boom(p):
            raise LLMError("api down")
        r = ev.evaluate_one(self.cand, "long", CFG, Mock(boom), AS_OF)
        self.assertEqual(r["status"], "error")

    def test_event_without_market_expectation_holds(self):
        r = self.run_it("event", event_scores={k: 90 for k in EVENT_KEYS},
                        event={"classification": 4, "market_expectation": ""})
        self.assertEqual(r["status"], "hold")
        self.assertIsNone(r["event"]["classification"])
        self.assertEqual(set(r["critical_missing"]), {"expectation_gap", "not_priced_in"})

    def test_event_with_market_expectation_final(self):
        r = self.run_it("event", event_scores={k: 70 for k in EVENT_KEYS},
                        event={"classification": 4, "market_expectation": "コンセンサスは赤字継続"})
        self.assertEqual((r["status"], r["total"], r["event"]["classification"]), ("final", 70.0, 4))

    def test_provider_requires_key(self):
        import os
        for v in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
            os.environ.pop(v, None)
        with self.assertRaises(LLMError):
            Anthropic()
        with self.assertRaises(LLMError):
            Gemini()

    def test_gemini_default_model_and_override(self):
        import os
        os.environ["GEMINI_API_KEY"] = "k" * 10
        saved = os.environ.pop("TRL_LLM_MODEL", None)
        try:
            self.assertEqual(Gemini().model, "gemini-3.8-flash")
            os.environ["TRL_LLM_MODEL"] = "gemini-x"
            self.assertEqual(Gemini().model, "gemini-x")
            self.assertEqual(Gemini("explicit").model, "explicit")
        finally:
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("TRL_LLM_MODEL", None)
            if saved:
                os.environ["TRL_LLM_MODEL"] = saved

    def test_anthropic_gemini_response_parsing(self):
        import os
        os.environ["ANTHROPIC_API_KEY"] = os.environ["GEMINI_API_KEY"] = "k" * 10
        try:
            a = Anthropic(http=lambda *a, **k: json.dumps({"content": [{"type": "text", "text": "hi"}]}).encode())
            self.assertEqual(a.complete("p"), "hi")
            g = Gemini(http=lambda *a, **k: json.dumps({"candidates": [{"content": {"parts": [{"text": "yo"}]}}]}).encode())
            self.assertEqual(g.complete("p"), "yo")
            bad = Gemini(http=lambda *a, **k: b"{}")
            with self.assertRaises(LLMError):
                bad.complete("p")
        finally:
            del os.environ["ANTHROPIC_API_KEY"], os.environ["GEMINI_API_KEY"]


class ScreenTests(unittest.TestCase):
    def c(self, **kw):
        d = {"published": "2026-10-10", "abstract": "A" * 300}
        d.update(kw)
        return d

    def test_screen(self):
        self.assertIsNone(screen(self.c(), AS_OF, CFG))
        self.assertEqual(screen(self.c(published=None), AS_OF, CFG), "no_published_date")
        self.assertEqual(screen(self.c(published="2026-10-25"), AS_OF, CFG), "published_after_as_of")
        self.assertEqual(screen(self.c(published="2026-10"), date(2026, 10, 15), CFG), "published_after_as_of")  # 月末まで未確定
        self.assertEqual(screen(self.c(published="2024-01-01"), AS_OF, CFG), "stale")
        self.assertEqual(screen(self.c(abstract="short"), AS_OF, CFG), "abstract_too_short")
        # ニュース/開示は info_type 別の低い閾値で通る (論文は従来どおり)
        self.assertIsNone(screen(self.c(abstract="x" * 40, info_type="news"), AS_OF, CFG))
        self.assertIsNone(screen(self.c(abstract="x" * 40, info_type="disclosure"), AS_OF, CFG))
        self.assertEqual(screen(self.c(abstract="x" * 40, info_type="paper"), AS_OF, CFG), "abstract_too_short")


class NewsPoolTests(unittest.TestCase):
    """ニュースは単体評価せず、同一企業の評価へ同梱する材料として扱う再設計の検証。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "d" / "trl.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def news_raw(self, sid, title, published="2026-10-19", source="gnews"):
        return {"source": source, "source_id": sid, "title": title,
                "abstract": title + " に関する事業ニュースの見出し。",
                "url": f"https://example.org/{sid}", "published": published,
                "fields": ["日本経済新聞"], "affiliations": [], "info_type": "news"}

    def test_collect_drops_news_without_entity(self):
        store = Store(self.path)
        cfg = dict(CFG)
        cfg["sources"] = {"defaults": {"max_results": 5, "lookback_days": 30},
                          "queries": [{"name": "n", "gnews": "q"}]}
        raws = [self.news_raw("n1", "アストロスケールが防衛案件の大型契約を獲得"),
                self.news_raw("n2", "人気アニメのポップアップショップが開店")]
        r = run_pipeline(store, cfg, None, as_of=AS_OF, sources=("gnews",), kinds=("long",),
                         fetchers={"gnews": lambda q, n, d, a: raws}, today=AS_OF, log=lambda *_: None)
        self.assertEqual(store.count_candidates(), 1)
        self.assertEqual(r["counts"]["invalid"], 1)
        store.close()

    def test_collect_drops_paper_without_entity(self):
        store = Store(self.path)
        cfg = json.loads(json.dumps(CFG))
        cfg["sources"] = {"defaults": {"max_results": 10, "lookback_days": 365},
                          "queries": [{"name": "t", "arxiv": "q"}]}
        raws = [raw(sid="2610.9701", title="Debris removal demonstration",
                    affiliations=["Astroscale Holdings Inc."]),
                raw(sid="2610.9702", title="Unrelated quantum theory", affiliations=[])]
        r = run_pipeline(store, cfg, None, as_of=AS_OF, sources=("arxiv",), kinds=("long",),
                         fetchers={"arxiv": lambda q, n, d, a: raws}, today=AS_OF, log=lambda *_: None)
        self.assertEqual(store.count_candidates(), 1)
        self.assertEqual(r["counts"]["invalid"], 1)
        store.close()

    def test_selection_prefers_entity_linked_candidates(self):
        store = Store(self.path)

        def add(r):
            rec, _ = normalize(r, "2026-10-20T00:00:00+00:00")
            cid, _ = store.upsert_candidate(rec, "r0")
            store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
            store.set_status(cid, "screened")
            return cid
        # 新しい順は unlinked > weak > strong だが、選定は企業リンク優先になり、
        # リンクの無い候補は評価対象から外れる
        unlinked = add(raw(sid="2610.9101", title="Unrelated quantum theory", published="2026-10-19",
                           affiliations=[]))
        weak = add(raw(sid="2610.9102", title="アストロスケール関連の市場分析メモ", published="2026-10-18",
                       affiliations=[]))
        strong = add(raw(sid="2610.9103", title="Debris removal study", published="2026-10-17",
                         affiliations=["Astroscale Holdings Inc."]))
        sel = select_for_eval(store, CFG, AS_OF, 10)
        self.assertEqual([c["id"] for c in sel], [strong, weak])
        store.close()

    def test_demo_signal_detection(self):
        self.assertTrue(T.has_demo_signal({"title": "Demonstration of a prototype device", "abstract": ""}))
        self.assertTrue(T.has_demo_signal({"title": "量子デバイスの実証実験", "abstract": ""}))
        self.assertTrue(T.has_demo_signal({"title": "A Phase 2 study", "abstract": "", "info_type": "trial"}))
        self.assertFalse(T.has_demo_signal({"title": "A theory of quantum codes", "abstract": "We propose a framework."}))

    def test_selection_prefers_demo_signal_within_link_tier(self):
        store = Store(self.path)

        def add(r):
            rec, _ = normalize(r, "2026-10-20T00:00:00+00:00")
            cid, _ = store.upsert_candidate(rec, "r0")
            store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
            store.set_status(cid, "screened")
            return cid
        # どちらも weak リンク。新しさでは theory が勝つが、実証シグナル優先で demo が先に選ばれる
        theory = add(raw(sid="2610.9201", title="アストロスケール関連メモ: A theory of fault-tolerant codes",
                         published="2026-10-19", affiliations=[]))
        demo = add(raw(sid="2610.9202", title="アストロスケール関連メモ: Demonstration of a prototype quantum memory",
                       published="2026-10-15", affiliations=[]))
        # リンク段位はシグナルより上位: strong リンク(シグナルなし)が weak+シグナルありより先
        strong = add(raw(sid="2610.9203", title="Debris removal study", published="2026-10-10",
                         affiliations=["Astroscale Holdings Inc."]))
        sel = select_for_eval(store, CFG, AS_OF, 10)
        self.assertEqual([c["id"] for c in sel], [strong, demo, theory])
        store.close()

    def test_selection_reserves_paper_slots(self):
        store = Store(self.path)

        def add(r, info_type=None):
            rec, _ = normalize(r, "2026-10-20T00:00:00+00:00")
            if info_type:
                rec["info_type"] = info_type
            cid, _ = store.upsert_candidate(rec, "r0")
            store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
            store.set_status(cid, "screened")
            return cid
        # 治験は実証シグナルで常に上位に来るが、選定6件のうち2件は論文枠になる
        trials = [add(raw(sid=f"2610.98{i:02d}", title=f"A Phase 2 study of compound {i}",
                          published="2026-10-19"), info_type="trial") for i in range(6)]
        papers = [add(raw(sid=f"2610.99{i:02d}", title=f"Peptide discovery research note {i}",
                          published="2026-10-18")) for i in range(3)]
        sel = select_for_eval(store, CFG, AS_OF, 6)
        kinds = ["trial" if c["id"] in trials else "paper" for c in sel]
        self.assertEqual(len(sel), 6)
        self.assertEqual(kinds.count("paper"), 2)
        self.assertTrue(all(p in [c["id"] for c in sel] for p in papers[:2]))
        store.close()

    def test_selection_excludes_disclosure(self):
        store = Store(self.path)
        rec, _ = normalize(raw(sid="2610.9401", affiliations=["Astroscale Holdings Inc."]),
                           "2026-10-20T00:00:00+00:00")
        rec["info_type"] = "disclosure"  # Yahoo等の財務スナップショットはプール専用で単体評価しない
        cid, _ = store.upsert_candidate(rec, "r0")
        store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
        store.set_status(cid, "screened")
        sel = select_for_eval(store, CFG, AS_OF, 10)
        self.assertEqual(sel, [])
        store.close()

    def test_selection_hold_gate_and_material_update_bypass(self):
        store = Store(self.path)

        def add_hold(sid, updated=None):
            rec, _ = normalize(raw(sid=sid), "2026-10-20T00:00:00+00:00")
            cid, _ = store.upsert_candidate(rec, "r0")
            store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
            store.set_status(cid, "hold", eval_at="2026-10-17T00:00:00+00:00")  # 3日前 (間隔は14日)
            if updated:
                store.conn.execute("UPDATE candidates SET material_updated_at=? WHERE id=?",
                                   (updated, cid))
                store.conn.commit()
            return cid

        stale = add_hold("2610.9501")
        fresh = add_hold("2610.9502", updated="2026-10-19T00:00:00+00:00")  # 評価後に材料が更新
        sel_ids = [c["id"] for c in select_for_eval(store, CFG, AS_OF, 10)]
        self.assertNotIn(stale, sel_ids)
        self.assertIn(fresh, sel_ids)
        store.close()

    def test_selection_caps_per_entity(self):
        store = Store(self.path)

        def add(r):
            rec, _ = normalize(r, "2026-10-20T00:00:00+00:00")
            cid, _ = store.upsert_candidate(rec, "r0")
            store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
            store.set_status(cid, "screened")
            return cid
        for i in range(12):  # 同一企業に12件あってもキューには上限10件まで
            add(raw(sid=f"2610.95{i:02d}", title=f"Debris removal study {i}",
                    published="2026-10-19", affiliations=["Astroscale Holdings Inc."]))
        for i in range(2):  # 別企業は上限の対象外
            add(raw(sid=f"2610.96{i:02d}", title=f"ペプチドリーム関連の研究メモ {i}",
                    published="2026-10-19", affiliations=[]))
        sel = select_for_eval(store, CFG, AS_OF, 100)
        self.assertEqual(len(sel), 12)
        store.close()

    def test_selection_excludes_watch_only_candidates(self):
        store = Store(self.path)

        def add(r):
            rec, _ = normalize(r, "2026-10-20T00:00:00+00:00")
            cid, _ = store.upsert_candidate(rec, "r0")
            store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
            store.set_status(cid, "screened")
            return cid
        # オンコリスはウォッチ層: リンクがあっても評価対象にしない
        watch = add(raw(sid="2610.9301", title="オンコリスの臨床試験に関する報告", published="2026-10-19",
                        affiliations=[]))
        core = add(raw(sid="2610.9302", title="アストロスケール関連の市場分析メモ", published="2026-10-18"))
        sel = select_for_eval(store, CFG, AS_OF, 10)
        self.assertEqual([c["id"] for c in sel], [core])
        store.close()

    def test_news_not_selected_but_pooled(self):
        store = Store(self.path)
        paper, _ = normalize(raw(sid="2610.7777", affiliations=["Astroscale Holdings Inc."]),
                             "2026-10-20T00:00:00+00:00")
        pid, _ = store.upsert_candidate(paper, "r0")
        store.set_entities(pid, link_entities(paper, CFG.get("entities", {})))
        news, _ = normalize(self.news_raw("n9", "アストロスケールが200億円規模の受注見込みを発表"),
                            "2026-10-20T00:00:00+00:00")
        nid, _ = store.upsert_candidate(news, "r0")
        store.set_entities(nid, link_entities(news, CFG.get("entities", {})))
        store.set_status(pid, "screened")
        store.set_status(nid, "screened")
        sel = select_for_eval(store, CFG, AS_OF, 10)
        self.assertEqual([c["id"] for c in sel], [pid])
        rel = store.related_news(pid, AS_OF)
        self.assertEqual(len(rel), 1)
        self.assertIn("受注見込み", rel[0]["title"])
        # 製品リンクだけの候補 (スポンサー名が資料に無い治験など) にも同じ企業のニュースが付く
        news2, _ = normalize(self.news_raw("n10", "オンコリスバイオファーマが海外企業と提携を発表"),
                             "2026-10-20T00:00:00+00:00")
        n2id, _ = store.upsert_candidate(news2, "r0")
        store.set_entities(n2id, link_entities(news2, CFG.get("entities", {})))
        prod, _ = normalize(raw(sid="2610.8888", title="OBP-301 Phase II study results",
                                abstract="Results of the OBP-301 study. " * 12, affiliations=[]),
                            "2026-10-20T00:00:00+00:00")
        prid, _ = store.upsert_candidate(prod, "r0")
        store.set_entities(prid, link_entities(prod, CFG.get("entities", {})))
        news3, _ = normalize(self.news_raw("n11", "オンコリスバイオファーマが新薬候補の導出契約を締結",
                                           source="prtimes"),
                             "2026-10-20T00:00:00+00:00")
        n3id, _ = store.upsert_candidate(news3, "r0")
        store.set_entities(n3id, link_entities(news3, CFG.get("entities", {})))
        rel2 = store.related_news(prid, AS_OF)
        self.assertEqual(len(rel2), 2)  # 報道(gnews)と企業発表(prtimes)が擦り合わさる
        self.assertEqual({r["origin"] for r in rel2}, {"報道", "企業発表"})
        store.close()

    def test_evaluate_one_includes_related(self):
        cand = {"id": "c1", "title": "T", "abstract": "A" * 300, "url": "u", "source": "trials",
                "published": "2026-10-10", "fields": []}
        seen = []
        base = make_llm_fn(long_scores={k: 70 for k in LONG_KEYS})

        def fn(p):
            seen.append(p)
            return base(p)
        related = [{"title": "アストロスケールが200億円規模の受注見込み", "published": "2026-10-19",
                    "url": "https://example.org/n9", "media": "日本経済新聞"}]
        res = evaluate_one(cand, "long", CFG, Mock(fn), AS_OF, related=related)
        self.assertEqual(res["status"], "final")
        self.assertEqual(res["extra"]["related_news_count"], 1)
        self.assertTrue(any("受注見込み" in p and "関連事業ニュース" in p for p in seen))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "d" / "trl.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def fetchers(self, fail_pubmed=False):
        def arx(q, n, days, as_of):
            return [raw(sid="2610.0001", doi="10.1/a"), raw(sid="2610.0002", title="B"),
                    raw(sid="2610.0003", title="Short", abstract="x"), {"title": "", "source": "arxiv"}]

        def pm(q, n, days, as_of):
            if fail_pubmed:
                raise RuntimeError("HTTP 500 for http://x?api_key=SECRETSECRET")
            return [raw(source="pubmed", sid="9", doi="10.1/a", title="Same paper via pubmed")]
        return {"arxiv": arx, "pubmed": pm}

    def cfg(self):
        c = dict(CFG)
        c["sources"] = {"defaults": {"max_results": 5, "lookback_days": 30},
                        "queries": [{"name": "q", "arxiv": "a", "pubmed": "p"}]}
        return c

    def test_end_to_end_and_rerun_idempotent(self):
        store = Store(self.path)
        llm = Mock(make_llm_fn(long_scores={"monetization": None}))
        r1 = run_pipeline(store, self.cfg(), llm, as_of=AS_OF, kinds=("long",), fetchers=self.fetchers(),
                          today=AS_OF, log=lambda *_: None)
        c = r1["counts"]
        self.assertEqual((c["fetched"], c["invalid"], c["sources_ok"]), (5, 1, 2))
        self.assertEqual(store.count_candidates(), 2 + 1)  # A(doi統合) , B , Short
        self.assertEqual((c["rejected"], c["screened"]), (1, 2))
        self.assertEqual((c["hold"], c["final"]), (2, 0))
        q = store.list_queue()
        self.assertEqual(len(q), 2)
        self.assertIn("monetization", q[0]["reason"])
        ev1 = store.evaluations_for_run(r1["run_id"], "long")[0]
        self.assertEqual(json.loads(ev1["prompt_versions_json"])["long"]["version"], "v1")
        self.assertEqual(ev1["config_version"], CFG["config_version"])
        store.close()

        # 再実行: 候補は増えず、保留は再評価間隔内なので再評価されない。キューは残る
        store = Store(self.path)
        r2 = run_pipeline(store, self.cfg(), llm, as_of=AS_OF, kinds=("long",), fetchers=self.fetchers(),
                          today=AS_OF, log=lambda *_: None)
        self.assertEqual(store.count_candidates(), 3)
        self.assertEqual(r2["counts"]["new"], 0)
        self.assertEqual(r2["counts"]["selected"], 0)
        self.assertEqual(len(store.list_queue()), 2)

        # 間隔経過後に情報が揃えば確定し、キューは解消
        later = date(2026, 11, 20)
        llm2 = Mock(make_llm_fn(long_scores={k: 65 for k in LONG_KEYS}))
        r3 = run_pipeline(store, self.cfg(), llm2, as_of=AS_OF, kinds=("long",), do_collect=False, today=later,
                          log=lambda *_: None)
        self.assertEqual(r3["counts"]["final"], 2)
        self.assertEqual(store.list_queue(), [])
        text = build_report(store, r3["run_id"])
        self.assertIn("65.0", text)
        self.assertIn("投資利益率ではありません", text)
        store.close()

    def test_purge_evaluations_resets_candidates(self):
        store = Store(self.path)
        llm = Mock(make_llm_fn(long_scores={"monetization": None}))
        r1 = run_pipeline(store, self.cfg(), llm, as_of=AS_OF, kinds=("long",), fetchers=self.fetchers(),
                          today=AS_OF, log=lambda *_: None)
        self.assertEqual(r1["counts"]["hold"], 2)
        self.assertEqual(len(store.list_queue()), 2)
        n_candidates = store.count_candidates()
        counts = store.purge_evaluations()
        self.assertEqual(counts, {"evaluations": 2, "queue": 2, "candidates_reset": 2})
        self.assertEqual(store.list_queue(), [])
        self.assertEqual(store.evaluations_for_run(r1["run_id"], "long"), [])
        self.assertEqual(store.count_candidates(), n_candidates)  # 候補・材料は残る
        statuses = {c["status"] for c in store.list_by_status(["screened", "hold", "error", "final"])}
        self.assertEqual(statuses, {"screened"})
        # purge 直後は再評価間隔を待たずに再評価できる
        r2 = run_pipeline(store, self.cfg(), llm, as_of=AS_OF, kinds=("long",), do_collect=False,
                          today=AS_OF, log=lambda *_: None)
        self.assertEqual(r2["counts"]["hold"], 2)
        store.close()

    def test_source_failure_isolated_and_secret_redacted(self):
        store = Store(self.path)
        logs = []
        r = run_pipeline(store, self.cfg(), None, as_of=AS_OF, fetchers=self.fetchers(fail_pubmed=True),
                         today=AS_OF, log=logs.append)
        self.assertEqual((r["counts"]["sources_ok"], r["counts"]["sources_failed"], r["failed"]), (1, 1, False))
        self.assertFalse(any("SECRETSECRET" in str(x) for x in logs))
        srcs = {s["source"]: s for s in store.run_sources(r["run_id"])}
        self.assertEqual(srcs["pubmed"]["ok"], 0)
        self.assertNotIn("SECRETSECRET", srcs["pubmed"]["error"])
        self.assertEqual(srcs["arxiv"]["ok"], 1)
        self.assertGreater(r["counts"]["skipped_no_llm"], 0)  # LLM未設定 → 評価せず収集のみ

    def test_all_sources_failed_flag(self):
        store = Store(self.path)

        def boom(*a):
            raise RuntimeError("down")
        r = run_pipeline(store, self.cfg(), None, as_of=AS_OF, fetchers={"arxiv": boom, "pubmed": boom},
                         today=AS_OF, log=lambda *_: None)
        self.assertTrue(r["failed"])

    def test_both_kinds_independent_with_shared_extraction(self):
        store = Store(self.path)
        n = {"extract": 0}
        base = make_llm_fn(long_scores={k: 70 for k in LONG_KEYS}, event_scores={k: 50 for k in EVENT_KEYS},
                           event={"classification": 2, "market_expectation": "x"})

        def fn(p):
            if "根拠抽出」担当" in p:
                n["extract"] += 1
            return base(p)
        r = run_pipeline(store, self.cfg(), Mock(fn), as_of=AS_OF, kinds=("long", "event"), fetchers=self.fetchers(),
                         today=AS_OF, log=lambda *_: None)
        self.assertEqual(n["extract"], 2)  # 候補2件、抽出は候補ごとに1回
        self.assertEqual(r["counts"]["final"], 4)
        totals = {(e["kind"], e["total"]) for e in store.evaluations_for_run(r["run_id"])}
        self.assertEqual(totals, {("long", 70.0), ("event", 50.0)})  # 平均されない




class RepoFilesTests(unittest.TestCase):
    def test_all_required_files_exist_in_repo(self):
        self.assertEqual(T.missing_files(), [], "リポジトリ直下に必須ファイルがありません")

    def test_missing_json_gives_clear_error(self):
        d = Path(tempfile.mkdtemp())
        with self.assertRaises(ConfigError) as cm:
            T.load_config(d)
        self.assertIn("scoring.json", str(cm.exception))
        (d / "scoring.json").write_bytes((T.ROOT / "scoring.json").read_bytes())
        with self.assertRaises(ConfigError) as cm:
            T.load_config(d)
        self.assertIn("sources.json", str(cm.exception))
        self.assertIn("sources.json", T.missing_files(d))

    def test_prompt_files_named_by_scoring_json_are_required(self):
        names = [n for n, _ in T.required_files()]
        for n in ("extract.v1.md", "long.v1.md", "event.v1.md"):
            self.assertIn(n, names)


class SourcesSchemaTests(unittest.TestCase):
    def good(self):
        return copy.deepcopy(CFG["sources"])

    def test_shipped_sources_valid(self):
        T.validate_sources(CFG["sources"])

    def test_invalid_sources_rejected(self):
        cases = []
        s = self.good(); del s["defaults"]; cases.append(s)
        s = self.good(); s["defaults"]["max_results"] = 0; cases.append(s)
        s = self.good(); s["queries"] = []; cases.append(s)
        s = self.good(); s["queries"][0] = {"name": "x"}; cases.append(s)
        s = self.good(); s["queries"][0]["name"] = ""; cases.append(s)
        s = self.good(); s["queries"].append(dict(s["queries"][0])); cases.append(s)
        s = self.good(); s["queries"][0]["lookback_days"] = "30"; cases.append(s)
        for c in cases:
            with self.assertRaises(ConfigError):
                T.validate_sources(c)

    def test_invalid_screening_or_prompts_rejected(self):
        bad = copy.deepcopy(CFG)
        bad["scoring"]["screening"]["max_age_days"] = "365"
        with self.assertRaises(ConfigError):
            validate_config(bad)
        bad = copy.deepcopy(CFG)
        del bad["scoring"]["prompts"]["event"]
        with self.assertRaises(ConfigError):
            validate_config(bad)


class EntityQueryGenerationTests(unittest.TestCase):
    # 移行前の手書きクエリ全27件。生成結果がこれと完全一致すること(移行の等価性)。
    GOLDEN = [
    {
        "arxiv": "all:\"solid-state battery\"",
        "name": "solid-state-battery"
    },
    {
        "arxiv": "all:\"quantum error correction\"",
        "name": "quantum-error-correction"
    },
    {
        "name": "gene-therapy",
        "pubmed": "gene therapy[Title/Abstract] AND clinical trial[Publication Type]"
    },
    {
        "gnews": "アストロスケール (決算 OR 受注 OR 契約 OR 提携 OR 資金調達 OR 業績 OR 採択)",
        "name": "astroscale-news"
    },
    {
        "gnews": "ispace (決算 OR 受注 OR 契約 OR 提携 OR 資金調達 OR 業績 OR 採択 OR 月面)",
        "name": "ispace-news"
    },
    {
        "gnews": "Synspective (決算 OR 受注 OR 契約 OR 提携 OR 資金調達 OR 業績 OR 採択 OR 衛星)",
        "name": "synspective-news"
    },
    {
        "gnews": "オンコリスバイオファーマ (決算 OR 契約 OR 提携 OR 導出 OR 承認 OR 治験 OR 資金調達 OR 採択 OR 助言)",
        "name": "oncolys-news"
    },
    {
        "gnews": "リプロセル (決算 OR 契約 OR 提携 OR 導出 OR 承認 OR 治験 OR 資金調達 OR 採択 OR 助言)",
        "name": "reprocell-news"
    },
    {
        "gnews": "ペプチドリーム (決算 OR 契約 OR 提携 OR 導出 OR 承認 OR 治験 OR 資金調達 OR 採択 OR 助言)",
        "name": "peptidream-news"
    },
    {
        "lookback_days": 180,
        "name": "oncolys-trials",
        "trials": "Oncolys BioPharma"
    },
    {
        "lookback_days": 180,
        "name": "reprocell-trials",
        "trials": "REPROCELL"
    },
    {
        "lookback_days": 180,
        "name": "peptidream-trials",
        "trials": "PeptiDream"
    },
    {
        "edinet": "186A",
        "lookback_days": 120,
        "name": "astroscale-edinet"
    },
    {
        "edinet": "9348",
        "lookback_days": 120,
        "name": "ispace-edinet"
    },
    {
        "edinet": "290A",
        "lookback_days": 120,
        "name": "synspective-edinet"
    },
    {
        "edinet": "4588",
        "lookback_days": 120,
        "name": "oncolys-edinet"
    },
    {
        "edinet": "4978",
        "lookback_days": 120,
        "name": "reprocell-edinet"
    },
    {
        "edinet": "4587",
        "lookback_days": 120,
        "name": "peptidream-edinet"
    },
    {
        "name": "astroscale-prtimes",
        "prtimes": "Astroscale|アストロスケール",
        "lookback_days": 90
    },
    {
        "name": "ispace-prtimes",
        "prtimes": "ispace",
        "lookback_days": 90
    },
    {
        "name": "synspective-prtimes",
        "prtimes": "Synspective|シンスペクティブ",
        "lookback_days": 90
    },
    {
        "name": "oncolys-prtimes",
        "prtimes": "Oncolys|オンコリス",
        "lookback_days": 90
    },
    {
        "name": "reprocell-prtimes",
        "prtimes": "REPROCELL|リプロセル",
        "lookback_days": 90
    },
    {
        "name": "peptidream-prtimes",
        "prtimes": "PeptiDream|ペプチドリーム",
        "lookback_days": 90
    },
    {
        "name": "peptidream-pubmed",
        "pubmed": "PeptiDream[Affiliation]",
        "lookback_days": 365
    },
    {
        "name": "ispace-openalex",
        "openalex": "I4405267988",
        "lookback_days": 365
    }
]

    def test_generated_queries_match_golden(self):
        # 移行前の手書き27件が、生成結果の中に同じ相対順序で全て含まれること(企業追加後も不変)。
        qs = CFG["sources"]["queries"]
        it = iter(qs)
        for g in self.GOLDEN:
            self.assertTrue(any(q == g for q in it), f"golden query missing or out of order: {g['name']}")

    def src(self):
        return {"keyword_sets": {"space": ["決算", "受注"], "bio": ["治験"]},
                "entity_watch": {"lookback_days": {"prtimes": 90, "trials": 365, "edinet": 60}}}

    def test_generation_rules(self):
        ents = {"entities": [
            {"id": "a", "sector": "space", "ticker": "1000",
             "watch": {"news_name": "A社", "extra_keywords": ["月面"]}},
            {"id": "b", "sector": "bio",
             "watch": {"news_name": "B社", "prtimes_terms": ["B", "ビー"], "sponsor": "B Inc"}},
            {"id": "c"},
        ]}
        qs = T.build_entity_queries(ents, self.src())
        by_name = {q["name"]: q for q in qs}
        self.assertEqual(by_name["a-news"]["gnews"], "A社 (決算 OR 受注 OR 月面)")
        self.assertEqual(by_name["a-prtimes"]["prtimes"], "A社")  # prtimes_terms 省略時は news_name
        self.assertEqual(by_name["a-edinet"]["edinet"], "1000")
        self.assertNotIn("a-trials", by_name)  # sponsor が無い
        self.assertEqual(by_name["b-trials"]["trials"], "B Inc")
        self.assertEqual(by_name["b-prtimes"]["prtimes"], "B|ビー")
        self.assertNotIn("b-edinet", by_name)  # ticker が無い
        self.assertFalse(any(n.startswith("c-") for n in by_name))  # watch が無い企業は生成しない

    def test_generation_rules_foreign_market(self):
        src = {"keyword_sets": {"space": ["決算"]},
               "keyword_sets_en": {"space": ["earnings", "contract"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "us1", "sector": "space", "market": "US", "ticker": "ABC",
             "watch": {"news_name": "ABC Corp", "extra_keywords": ["rocket"]}},
            {"id": "gb1", "sector": "space", "market": "GB", "ticker": "XYZ",
             "watch": {"news_name": "XYZ plc"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        q = by_name["us1-news"]["gnews"]
        self.assertEqual((q["hl"], q["gl"], q["ceid"]), ("en", "US", "US:en"))
        self.assertEqual(q["q"], "ABC Corp (earnings OR contract OR rocket)")
        self.assertEqual(by_name["us1-yahoo"]["yahoo"], "ABC")     # 米は接尾辞なし
        self.assertEqual(by_name["gb1-yahoo"]["yahoo"], "XYZ.L")   # 英は .L
        self.assertNotIn("us1-edinet", by_name)   # EDINET は JP のみ
        self.assertNotIn("us1-prtimes", by_name)  # PR TIMES も JP のみ

    def test_generation_rules_german_market(self):
        src = {"keyword_sets": {"space": ["決算"]},
               "keyword_sets_en": {"space": ["earnings"]},
               "keyword_sets_de": {"space": ["Ergebnisse", "Auftrag"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "de1", "sector": "space", "market": "DE", "ticker": "OHB",
             "watch": {"news_name": "OHB", "extra_keywords": ["Satelliten"]}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        q = by_name["de1-news"]["gnews"]
        self.assertEqual((q["hl"], q["gl"], q["ceid"]), ("de", "DE", "DE:de"))
        self.assertEqual(q["q"], "OHB (Ergebnisse OR Auftrag OR Satelliten)")  # 独語キーワード集合を使う
        self.assertEqual(by_name["de1-yahoo"]["yahoo"], "OHB.DE")
        self.assertNotIn("de1-edinet", by_name)
        self.assertNotIn("de1-prtimes", by_name)

    def test_generation_rules_canada_australia(self):
        src = {"keyword_sets": {"space": ["決算"]},
               "keyword_sets_en": {"space": ["earnings", "contract"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "ca1", "sector": "space", "market": "CA", "ticker": "MDA",
             "watch": {"news_name": "MDA Space"}},
            {"id": "au1", "sector": "space", "market": "AU", "ticker": "EOS",
             "watch": {"news_name": "EOS Ltd"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        self.assertEqual(by_name["ca1-news"]["gnews"]["gl"], "CA")
        self.assertEqual(by_name["ca1-yahoo"]["yahoo"], "MDA.TO")
        self.assertEqual(by_name["au1-news"]["gnews"]["ceid"], "AU:en")
        self.assertEqual(by_name["au1-yahoo"]["yahoo"], "EOS.AX")
        self.assertNotIn("au1-prtimes", by_name)

    def test_generation_rules_italy_sweden(self):
        src = {"keyword_sets": {"space": ["決算"]},
               "keyword_sets_it": {"space": ["risultati", "lancio"]},
               "keyword_sets_sv": {"space": ["resultat", "uppskjutning"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "it1", "sector": "space", "market": "IT", "ticker": "AVIO",
             "watch": {"news_name": "Avio"}},
            {"id": "se1", "sector": "space", "market": "SE", "ticker": "AAC",
             "watch": {"news_name": "AAC Clyde Space"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        self.assertEqual(by_name["it1-news"]["gnews"]["q"], "Avio (risultati OR lancio)")
        self.assertEqual(by_name["it1-yahoo"]["yahoo"], "AVIO.MI")
        self.assertEqual(by_name["se1-news"]["gnews"]["hl"], "sv")
        self.assertEqual(by_name["se1-yahoo"]["yahoo"], "AAC.ST")

    def test_generation_rules_denmark(self):
        # デンマークは Google News のデンマーク語圏の収載が薄いため英語集合+DKロケールを使う
        src = {"keyword_sets": {"bio": ["治験"]},
               "keyword_sets_en": {"bio": ["earnings", "clinical trial"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "dk1", "sector": "bio", "market": "DK", "ticker": "GMAB",
             "watch": {"news_name": "Genmab", "sponsor": "Genmab"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        q = by_name["dk1-news"]["gnews"]
        self.assertEqual((q["hl"], q["gl"]), ("en", "DK"))
        self.assertEqual(q["q"], "Genmab (earnings OR clinical trial)")
        self.assertEqual(by_name["dk1-yahoo"]["yahoo"], "GMAB.CO")
        self.assertEqual(by_name["dk1-trials"]["trials"], "Genmab")

    def test_generation_rules_spain(self):
        # スペインも英語集合+現地ロケール方式。Yahoo は .MC、通貨はユーロ圏
        src = {"keyword_sets": {"bio": ["治験"]},
               "keyword_sets_en": {"bio": ["earnings", "clinical trial"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "es1", "sector": "bio", "market": "ES", "ticker": "PHM",
             "watch": {"news_name": "PharmaMar", "sponsor": "PharmaMar"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        q = by_name["es1-news"]["gnews"]
        self.assertEqual((q["hl"], q["gl"], q["ceid"]), ("en", "ES", "ES:en"))
        self.assertEqual(q["q"], "PharmaMar (earnings OR clinical trial)")
        self.assertEqual(by_name["es1-yahoo"]["yahoo"], "PHM.MC")
        self.assertEqual(by_name["es1-trials"]["trials"], "PharmaMar")

    def test_generation_rules_france_norway(self):
        # フランス/ノルウェーも英語集合+現地ロケール。Yahoo 接尾辞は .PA / .OL
        src = {"keyword_sets": {"semiconductor": ["治験"]},
               "keyword_sets_en": {"semiconductor": ["earnings", "wafer"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "fr1", "sector": "semiconductor", "market": "FR", "ticker": "SOI",
             "watch": {"news_name": "Soitec"}},
            {"id": "no1", "sector": "semiconductor", "market": "NO", "ticker": "NOD",
             "watch": {"news_name": "Nordic Semiconductor"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        q = by_name["fr1-news"]["gnews"]
        self.assertEqual((q["hl"], q["gl"], q["ceid"]), ("en", "FR", "FR:en"))
        self.assertEqual(by_name["fr1-yahoo"]["yahoo"], "SOI.PA")
        self.assertEqual(by_name["no1-yahoo"]["yahoo"], "NOD.OL")
        self.assertEqual(by_name["no1-news"]["gnews"]["q"],
                         "Nordic Semiconductor (earnings OR wafer)")

    def test_generation_rules_openalex(self):
        src = {"keyword_sets": {"space": ["契約"]},
               "entity_watch": {"lookback_days": {"openalex": 365}}}
        ents = {"entities": [
            {"id": "sp1", "sector": "space", "market": "JP",
             "watch": {"news_name": "Example Space", "openalex": "I123456789"}},
            {"id": "sp2", "sector": "space", "market": "JP",
             "watch": {"news_name": "No Papers Co"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        self.assertEqual(by_name["sp1-openalex"]["openalex"], "I123456789")
        self.assertEqual(by_name["sp1-openalex"]["lookback_days"], 365)
        self.assertNotIn("sp2-openalex", by_name)

    def test_invalid_market_rejected(self):
        ents = {"entities": [{"id": "x", "name": "X Corp", "aliases": ["X Corp Ltd"], "market": "CN"}]}
        with self.assertRaises(ConfigError):
            T.validate_entities(ents)

    def test_invalid_tier_rejected(self):
        ents = {"entities": [{"id": "x", "name": "X Corp", "aliases": ["X Corp Ltd"], "tier": "vip"}]}
        with self.assertRaises(ConfigError):
            T.validate_entities(ents)

    def test_unknown_sector_rejected(self):
        ents = {"entities": [{"id": "x", "sector": "chem", "watch": {"news_name": "X社"}}]}
        with self.assertRaises(ConfigError):
            T.build_entity_queries(ents, self.src())

    def test_missing_news_name_rejected(self):
        ents = {"entities": [{"id": "x", "sector": "space", "watch": {}}]}
        with self.assertRaises(ConfigError):
            T.build_entity_queries(ents, self.src())

    def test_entities_dir_split_loading(self):
        import tempfile
        root = os.path.dirname(os.path.abspath(__file__))
        with tempfile.TemporaryDirectory() as td:
            for n in ("scoring.json", "sources.json"):
                with open(os.path.join(root, n), encoding="utf-8") as f:
                    data = f.read()
                with open(os.path.join(td, n), "w", encoding="utf-8") as f:
                    f.write(data)
            os.mkdir(os.path.join(td, "entities"))
            jp = {"entities": [{"id": "jp1", "name": "JP Corp", "aliases": ["JP Corp Ltd"],
                                "sector": "space", "ticker": "1000",
                                "watch": {"news_name": "JP Corp"}}]}
            us = {"entities": [{"id": "us1", "name": "US Corp", "aliases": ["US Corp Inc"],
                                "sector": "space", "market": "US", "ticker": "USX",
                                "watch": {"news_name": "US Corp"}}]}
            # ファイル名の辞書順 (jp < us) ではなく ENTITY_FILE_ORDER で合体される
            for fname, data in (("us.json", us), ("jp.json", jp)):
                with open(os.path.join(td, "entities", fname), "w", encoding="utf-8") as f:
                    json.dump(data, f)
            cfg = T.load_config(td)
            self.assertEqual([e["id"] for e in cfg["entities"]["entities"]], ["jp1", "us1"])
            names = [q["name"] for q in cfg["sources"]["queries"]]
            self.assertIn("jp1-news", names)
            self.assertIn("us1-yahoo", names)

    def test_invalid_keyword_sets_rejected(self):
        s = copy.deepcopy(CFG["sources"]); s["keyword_sets"]["bio"] = "治験"
        with self.assertRaises(ConfigError):
            T.validate_sources(s)
        s = copy.deepcopy(CFG["sources"]); s["topics"] = [{"name": "t"}]
        with self.assertRaises(ConfigError):
            T.validate_sources(s)


if __name__ == "__main__":
    unittest.main()
