"""OpenAlex (機関所属で論文を検索する)。全分野の学術論文インデックス。

クエリ値は OpenAlex の機関ID (例: I123456789)。著者の所属機関で検索するため、
ヒットした論文は対象企業に strong リンクする。抄録は abstract_inverted_index
(単語→位置) から復元する。APIキーは環境変数 OPENALEX_API_KEY (任意。あると
1日の予算が10倍になり安定する)。
"""

import json
import os
from datetime import timedelta
from ..http import http_fetch


BASE = "https://api.openalex.org/works"

SELECT = "id,doi,title,publication_date,abstract_inverted_index,authorships,primary_location,type"


def _abstract(inv):
    if not isinstance(inv, dict):
        return ""
    words = {}
    for w, poss in inv.items():
        for p in poss or []:
            if isinstance(p, int):
                words[p] = w
    return " ".join(words[i] for i in sorted(words))


def parse_works(data):
    out = []
    for w in (data or {}).get("results", []):
        wid = (w.get("id") or "").rsplit("/", 1)[-1]
        if not wid:
            continue
        affs, seen = [], set()
        for a in w.get("authorships") or []:
            for inst in (a or {}).get("institutions") or []:
                name = (inst or {}).get("display_name")
                if name and name not in seen:
                    seen.add(name)
                    affs.append(name)
        venue = (((w.get("primary_location") or {}).get("source") or {}).get("display_name"))
        doi = (w.get("doi") or "").replace("https://doi.org/", "") or None
        out.append({
            "source": "openalex", "source_id": wid, "doi": doi,
            "title": w.get("title") or "",
            "abstract": _abstract(w.get("abstract_inverted_index")),
            "published": w.get("publication_date"),
            "url": w.get("doi") or w.get("id"),
            "fields": [venue] if venue else [],
            "affiliations": affs, "info_type": "paper"})
    return out


def openalex_fetch(institution_id, max_results, lookback_days, as_of, http=None):
    http = http or http_fetch
    start = (as_of - timedelta(days=lookback_days)).isoformat()
    params = {"filter": (f"authorships.institutions.id:{institution_id},"
                         f"from_publication_date:{start},to_publication_date:{as_of.isoformat()}"),
              "sort": "publication_date:desc", "per-page": max(max_results, 100), "select": SELECT}
    key = os.environ.get("OPENALEX_API_KEY")
    if key:
        params["api_key"] = key
    return parse_works(json.loads(http(BASE, params=params)))
