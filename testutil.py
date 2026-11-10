"""テスト共通のフィクスチャとヘルパー (test_*.py 間の共有用)。

ファイル名が test_*.py のパターンに当たらないため、テスト discovery
には拾われない。元は test_trl.py 冒頭と test_site.py にあったものを
切り出した。
"""
import os

import trl as T
from trl import *  # noqa

# 実辞書 (LEI 登録済み) を使うパイプライン系テストが、収集の後段バッチ
# (ESEF) 経由で実ネットワークへ出ないようにする。ESEF 自体のテスト
# (test_esef) は対象範囲だけ明示的に有効化する
os.environ.setdefault("TRL_ESEF_DISABLE", "1")
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
from datetime import date, datetime, timedelta, timezone


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

import copy
import tempfile
import unittest
from pathlib import Path


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


class StoreFixture(unittest.TestCase):
    """一時 Store と export 用 build を用意する共通フィクスチャ。

    旧 test_site.ExportTests の setUp / tearDown / build を切り出したもの。
    """

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


# ClinicalTrials.gov の studies 応答サンプル (元は test_trl.py にあったもの)
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
