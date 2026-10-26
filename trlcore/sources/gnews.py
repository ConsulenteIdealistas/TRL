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

# 転載・コラム系で根拠として弱い媒体は収集段階で落とす (媒体名の部分一致)
EXCLUDED_MEDIA_PARTS = ("BigGo", "つぶやき")

# 媒体を問わず、定型ページや相場の寄せ集めで中身の無い見出しは落とす (タイトル部分一致)
EXCLUDED_TITLE_PARTS = ("株つぶやき", "現物信用売買内訳", "：決算情報", "企業情報（決算時期",
                        "掲示板", "空売", "テンバガー")


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


def parse_feed(xml_bytes, source="gnews"):
    root = ET.fromstring(xml_bytes)
    out = []
    for item in root.findall(".//item"):
        title = re.sub(r"\s+", " ", item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        media = (item.findtext("source") or "").strip()
        if any(p in media for p in EXCLUDED_MEDIA_PARTS):
            continue
        if any(p in title for p in EXCLUDED_TITLE_PARTS):
            continue
        if "上昇" in title and "下落" in title:
            continue  # 複数銘柄の値動きまとめは事業の根拠にならない

        published = _date_of(item)
        desc = _strip_html(item.findtext("description"))
        # description は多くの場合タイトルの繰り返し+媒体名。タイトルと異なる情報が
        # 含まれるときだけ後ろに足し、二重記載を避ける
        abstract = title if desc.startswith(title) else f"{title} {desc}".strip()
        if media:
            abstract += f"（配信元: {media}）"
        sid = hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]
        out.append({
            "source": source, "source_id": sid,
            "title": title, "abstract": abstract,
            "url": link, "published": published,
            "fields": [media] if media else [],
            "affiliations": [], "info_type": "news",
        })
    return out


def _rss_fetch(query, max_results, lookback_days, as_of, http, source):
    start = as_of - timedelta(days=lookback_days)
    q = f"{query} after:{start.isoformat()}"
    xml = http(URL, params={"q": q, "hl": "ja", "gl": "JP", "ceid": "JP:ja"})
    items = [r for r in parse_feed(xml, source=source)
             if r["published"] and start.isoformat() <= r["published"] <= as_of.isoformat()]
    items.sort(key=lambda r: r["published"], reverse=True)
    return items[:max_results]


def gnews_fetch(query, max_results, lookback_days, as_of, http=None):
    return _rss_fetch(query, max_results, lookback_days, as_of, http or http_fetch, "gnews")


def prtimes_fetch(query, max_results, lookback_days, as_of, http=None):
    """PR TIMES の企業公式プレスリリースだけを Google News RSS から拾う一次情報ソース。

    クエリは企業名 ("|" 区切りで英日などの別表記を併記可)。Google News の site:
    検索は関連性が緩く無関係なリリースが混ざるため、引用符付きフレーズで検索し、
    タイトルに企業名を含むものだけを残す厳密フィルタを併用する。件数は少なくて
    よい (一次情報プールは精度優先)。gnews(報道)と併用し、関連ニュースの同梱時に
    origin=企業発表 として区別する (擦り合わせ用)。
    """
    terms = [t.strip() for t in query.split("|") if t.strip()]
    phrase = " OR ".join(f'"{t}"' for t in terms)
    items = _rss_fetch(f"({phrase}) site:prtimes.jp", max_results, lookback_days, as_of,
                       http or http_fetch, "prtimes")
    return [i for i in items if any(t.lower() in i["title"].lower() for t in terms)]
