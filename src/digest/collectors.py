"""PubMed / arXiv の収集。共通DTOは dataclass で定義。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import feedparser
import httpx
from dateutil import parser as dtparser
from tenacity import retry, stop_after_attempt, wait_exponential


@dataclass
class NormalizedItem:
    id: str
    source: str
    title: str
    url: str
    domain: str
    raw_type: str
    published_at: date | None = None
    abstract: str | None = None
    doi: str | None = None
    score: float = 0.0
    extra: dict = field(default_factory=dict)


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return dtparser.parse(s, fuzzy=True).date()
    except Exception:
        return None


class PubMedCollector:
    name = "pubmed"
    domain = "biotech"

    def __init__(self, params: dict) -> None:
        self.params = params
        self.log = logging.getLogger(self.name)
        self.lookback = int(params.get("lookback_days", 7))
        self.retmax = int(params.get("retmax", 100))
        self.term = params.get("term", "biotechnology")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=10))
    def _get(self, url: str, **kw) -> httpx.Response:
        r = httpx.get(url, timeout=20, **kw)
        r.raise_for_status()
        return r

    def collect(self) -> list[NormalizedItem]:
        end = date.today()
        start = end - timedelta(days=self.lookback)
        esearch = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        r = self._get(esearch, params={
            "db": "pubmed", "term": self.term, "retmax": self.retmax,
            "retmode": "json", "datetype": "pdat",
            "mindate": start.isoformat(), "maxdate": end.isoformat(), "sort": "date",
        })
        ids = r.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []
        esummary = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        r2 = self._get(esummary, params={
            "db": "pubmed", "id": ",".join(ids), "retmode": "json",
        })
        data = r2.json().get("result", {})
        out: list[NormalizedItem] = []
        for pid in ids:
            d = data.get(pid)
            if not d:
                continue
            doi = next(
                (a["value"] for a in d.get("articleids", []) if a.get("idtype") == "doi"),
                None,
            )
            out.append(NormalizedItem(
                id=f"pubmed:{pid}", source="pubmed",
                title=(d.get("title") or "").strip(),
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
                domain="biotech", raw_type="paper",
                published_at=_parse_date(d.get("pubdate")),
                doi=doi,
            ))
        self.log.info("pubmed.collected count=%d", len(out))
        return out


class ArXivCollector:
    name = "arxiv"
    domain = "space"

    def __init__(self, params: dict) -> None:
        self.params = params
        self.log = logging.getLogger(self.name)
        self.categories = params.get("categories", ["astro-ph"])
        self.max_results = int(params.get("max_results", 50))

    def collect(self) -> list[NormalizedItem]:
        query = " OR ".join(f"cat:{c}*" for c in self.categories)
        url = "http://export.arxiv.org/api/query"
        r = httpx.get(url, params={
            "search_query": query, "start": 0, "max_results": self.max_results,
            "sortBy": "submittedDate", "sortOrder": "descending",
        }, timeout=30)
        r.raise_for_status()
        feed = feedparser.parse(r.text)
        out: list[NormalizedItem] = []
        for e in feed.entries:
            out.append(NormalizedItem(
                id=f"arxiv:{e.get('id', '')}", source="arxiv",
                title=(e.get("title") or "").strip().replace("\n", " "),
                url=e.get("link", ""), domain="space", raw_type="paper",
                published_at=_parse_date(e.get("published")),
                abstract=(e.get("summary") or "").strip()[:2000],
                doi=e.get("arxiv_doi"),
            ))
        self.log.info("arxiv.collected count=%d", len(out))
        return out