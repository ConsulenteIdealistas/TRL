"""arXiv API (Atom)。"""

import re
import time
import xml.etree.ElementTree as ET
from datetime import timedelta
from ..http import http_fetch


URL = "https://export.arxiv.org/api/query"

NS = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}


_last_call = [0.0]


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = []
    for e in root.findall("a:entry", NS):
        raw_id = (e.findtext("a:id", default="", namespaces=NS) or "").strip()
        m = re.search(r"/abs/(.+?)(v\d+)?$", raw_id)
        if not m:
            continue
        aid = m.group(1)
        out.append({
            "source": "arxiv", "source_id": aid, "arxiv_id": aid,
            "doi": e.findtext("x:doi", default=None, namespaces=NS),
            "title": e.findtext("a:title", default="", namespaces=NS),
            "abstract": e.findtext("a:summary", default="", namespaces=NS),
            "url": f"https://arxiv.org/abs/{aid}",
            "published": (e.findtext("a:published", default="", namespaces=NS) or "")[:10] or None,
            "fields": [c.get("term") for c in e.findall("a:category", NS) if c.get("term")],
            "affiliations": [],
            "info_type": "paper",
        })
    return out


def arxiv_fetch(query, max_results, lookback_days, as_of, http=None, sleep=time.sleep):
    http = http or http_fetch
    wait = 3.1 - (time.monotonic() - _last_call[0])
    if _last_call[0] and wait > 0:
        sleep(wait)  # arXiv は 3 秒に 1 リクエスト程度を求めている
    start = (as_of - timedelta(days=lookback_days)).strftime("%Y%m%d0000")
    end = as_of.strftime("%Y%m%d2359")
    q = f"({query}) AND submittedDate:[{start} TO {end}]"
    try:
        xml = http(URL, params={"search_query": q, "start": 0, "max_results": max_results,
                                "sortBy": "submittedDate", "sortOrder": "descending"})
    finally:
        _last_call[0] = time.monotonic()
    return parse_feed(xml)
