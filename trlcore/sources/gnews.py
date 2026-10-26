"""Google News RSS。企業名クエリで受注・契約・提携・決算などの事業ニュースを集める。

RSS の description は関連見出しのリンク集で本文ではないため、根拠テキストは
タイトル + 配信元 + 配信日で構成する (info_type="news")。論文と違い短文なので、
スクリーニングの最低文字数は scoring.json の min_abstract_chars_by_info_type で別途定める。
"""

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from email.utils import parsedate_to_datetime
from ..http import http_fetch


URL = "https://news.google.com/rss/search"


def _strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _date_of(item):
    raw = (item.findtext("pubDate") or "").strip()
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except (TypeError, ValueError):
        return None


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = []
    for item in root.findall(".//item"):
        title = re.sub(r"\s+", " ", item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        media = (item.findtext("source") or "").strip()
        published = _date_of(item)
        desc = _strip_html(item.findtext("description"))
        # description は多くの場合タイトルの繰り返し+媒体名。タイトルと異なる情報が
        # 含まれるときだけ後ろに足し、二重記載を避ける
        abstract = title if desc.startswith(title) else f"{title} {desc}".strip()
        if media:
            abstract += f"（配信元: {media}）"
        sid = hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]
        out.append({
            "source": "gnews", "source_id": sid,
            "title": title, "abstract": abstract,
            "url": link, "published": published,
            "fields": [media] if media else [],
            "affiliations": [], "info_type": "news",
        })
    return out


def gnews_fetch(query, max_results, lookback_days, as_of, http=None):
    http = http or http_fetch
    start = as_of - timedelta(days=lookback_days)
    q = f"{query} after:{start.isoformat()}"
    xml = http(URL, params={"q": q, "hl": "ja", "gl": "JP", "ceid": "JP:ja"})
    items = [r for r in parse_feed(xml)
             if r["published"] and start.isoformat() <= r["published"] <= as_of.isoformat()]
    items.sort(key=lambda r: r["published"], reverse=True)
    return items[:max_results]
