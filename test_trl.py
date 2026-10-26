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
trials = NS(parse=T.parse_studies, fetch=T.trials_fetch)
edinet = NS(parse=T.edinet_parse_results, fetch=T.edinet_fetch, sec_code=T.sec_code)
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
         "published": published, "url": f"https://example.org/{sid}", "fields": ["x"]}
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
        r = gnews.parse_feed(GNEWS_XML)[0]
        self.assertEqual((r["source"], r["info_type"], r["published"]), ("gnews", "news", "2026-10-18"))
        self.assertIn("配信元: Example News", r["abstract"])
        self.assertTrue(r["url"].startswith("https://news.google.com/"))

    def test_gnews_fetch_window(self):
        out = gnews.fetch("アストロスケール", 20, 30, AS_OF, http=lambda url, params=None, **kw: GNEWS_XML)
        self.assertEqual(len(out), 1)  # 2026-08-01 は lookback 30 日の外

    def test_trials(self):
        r = trials.parse(TRIALS_DATA)[0]
        self.assertEqual((r["source"], r["info_type"], r["published"]), ("trials", "trial", "2026-10-01"))
        self.assertEqual(r["url"], "https://clinicaltrials.gov/study/NCT00000001")
        self.assertIn("Status: RECRUITING", r["abstract"])
        self.assertEqual(r["affiliations"], ["PeptiDream Inc."])

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


if __name__ == "__main__":
    unittest.main()
