"""PubMed (NCBI E-utilities)。"""

import json
import os
import threading
import time
import xml.etree.ElementTree as ET
from datetime import timedelta
from ..http import http_fetch


BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

_rate_lock = threading.Lock()
_last_call = [0.0]


def _throttle(interval, sleep=time.sleep, now=time.monotonic):
    """NCBI の上限 (キー無し3 req/s / キー有り10 req/s) を並行実行でも超えないよう、
    プロセス全体で呼び出し間隔を空ける。クエリ単位の sleep では5並行のバーストを
    抑えられず HTTP 429 になる (2026-10-29 の実行で6クエリ失敗)。"""
    with _rate_lock:
        wait = interval - (now() - _last_call[0])
        if wait > 0:
            sleep(wait)
        _last_call[0] = now()


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _month(v):
    if not v:
        return None
    v = v.strip().lower()
    if v.isdigit():
        return int(v)
    return MONTHS.get(v[:3])


def _date_from(el):
    if el is None:
        return None
    y = el.findtext("Year")
    if not y or not y.isdigit():
        return None
    mo = _month(el.findtext("Month"))
    d = el.findtext("Day")
    if mo and d and d.isdigit():
        return f"{int(y):04d}-{mo:02d}-{int(d):02d}"
    if mo:
        return f"{int(y):04d}-{mo:02d}"
    return f"{int(y):04d}"


def parse_efetch(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = []
    for art in root.findall(".//PubmedArticle"):
        pmid = art.findtext("MedlineCitation/PMID")
        a = art.find("MedlineCitation/Article")
        if pmid is None or a is None:
            continue
        title_el = a.find("ArticleTitle")
        title = "".join(title_el.itertext()) if title_el is not None else ""
        parts = []
        for t in a.findall("Abstract/AbstractText"):
            txt = "".join(t.itertext()).strip()
            label = t.get("Label")
            parts.append(f"{label}: {txt}" if label and txt else txt)
        doi = None
        for aid in art.findall("PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi":
                doi = (aid.text or "").strip()
        published = _date_from(a.find("ArticleDate")) or _date_from(a.find("Journal/JournalIssue/PubDate"))
        mesh = [m.text for m in art.findall(".//MeshHeading/DescriptorName") if m.text][:10]
        affiliations = []
        for aff in a.findall("AuthorList/Author/AffiliationInfo/Affiliation"):
            txt = "".join(aff.itertext()).strip()
            if txt:
                affiliations.append(txt)
        affiliations = list(dict.fromkeys(affiliations))
        out.append({
            "source": "pubmed", "source_id": pmid.strip(), "pmid": pmid.strip(), "doi": doi,
            "title": title, "abstract": " ".join(p for p in parts if p),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid.strip()}/",
            "published": published, "fields": mesh,
            "affiliations": affiliations, "info_type": "paper",
        })
    return out


def pubmed_fetch(query, max_results, lookback_days, as_of, http=None, sleep=time.sleep):
    http = http or http_fetch
    key = os.environ.get("NCBI_API_KEY")
    interval = 0.12 if key else 0.4  # 全体スロットルの間隔 (上限の 3 / 10 req/s に対して余裕を持たせる)
    common = {"tool": "trl-analysis"}
    if key:
        common["api_key"] = key
    params = dict(common, db="pubmed", term=query, retmax=max(max_results, 100), retmode="json",
                  datetype="pdat", sort="pub_date",
                  mindate=(as_of - timedelta(days=lookback_days)).strftime("%Y/%m/%d"),
                  maxdate=as_of.strftime("%Y/%m/%d"))
    _throttle(interval, sleep=sleep)
    data = json.loads(http(BASE + "esearch.fcgi", params=params))
    ids = data.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    _throttle(interval, sleep=sleep)
    xml = http(BASE + "efetch.fcgi", params=dict(common, db="pubmed", id=",".join(ids), retmode="xml"))
    return parse_efetch(xml)
