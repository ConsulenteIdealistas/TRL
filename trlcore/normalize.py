"""正規化と基本検証。重複判定は安定ID(DOI/PMID/arXiv ID/URL)のみ。タイトル類似では統合しない。"""

import re
from .dates import parse_date


def clean_text(s):
    return re.sub(r"\s+", " ", s or "").strip()


def norm_doi(d):
    d = clean_text(d).lower()
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", d)
    return d or None


def norm_url(u):
    u = clean_text(u)
    if not u:
        return None
    u = u.split("#")[0].rstrip("/")
    m = re.match(r"^(https?)://([^/]+)(.*)$", u, re.I)
    return f"{m.group(1).lower()}://{m.group(2).lower()}{m.group(3)}" if m else u


FATAL = {"missing_title", "no_stable_identifier"}


def normalize(raw, retrieved_at):
    """-> (record | None, problems)。致命的な問題があれば record は None。"""
    problems = []
    title = clean_text(raw.get("title"))
    if not title:
        problems.append("missing_title")
    keys = []
    doi = norm_doi(raw.get("doi"))
    if doi:
        keys.append("doi:" + doi)
    if raw.get("pmid"):
        keys.append("pmid:" + str(raw["pmid"]).strip())
    if raw.get("arxiv_id"):
        keys.append("arxiv:" + str(raw["arxiv_id"]).strip().lower())
    url = norm_url(raw.get("url"))
    if url:
        keys.append("url:" + url)
    if not keys:
        problems.append("no_stable_identifier")
    published = raw.get("published")
    if published and parse_date(published) is None:
        problems.append("bad_published_date")
        published = None
    if FATAL & set(problems):
        return None, problems
    rec = {
        "keys": keys,
        "source": raw.get("source", "unknown"),
        "source_id": str(raw.get("source_id") or ""),
        "doi": doi,
        "title": title,
        "abstract": clean_text(raw.get("abstract")),
        "url": url or "",
        "media_url": norm_url(raw.get("media_url")) or "",
        "published": published,
        "retrieved_at": retrieved_at,
        "fields": [clean_text(f) for f in raw.get("fields", []) if clean_text(f)],
        "affiliations": list(dict.fromkeys(
            clean_text(a) for a in raw.get("affiliations", []) if clean_text(a)
        )),
        "info_type": raw.get("info_type", "paper"),
    }
    return rec, problems
