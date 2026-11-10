"""スイス上場10社の決算発表を EQS / GlobeNewswire の2ワイヤから取得する。

役割は財務候補の補完 (Yahoo / EDGAR と同じ位置づけ): SIX 公式には決算発表
本文の集約公開が存在しないため、発行体が義務として配信するニュースワイヤ
2社の和集合で拾う (調査: ~/workspace/trl-swiss-sweep/RESULTS.md、2026-11-03
実測で EQS 5社 + GlobeNewswire 6社 = 和集合 10/10)。片方だけでは
Basilea (GNW) か Kardex (EQS) が必ず漏れる非対称性があるため併用する。

検知・同定の方式 (2026-11-10 に live で確認):
- EQS: eqs-news.com の検索結果ページを会社名で巡回し、記事本文末尾の
  構造化フッター (Company / ISIN / EQS News ID) の ISIN が対象企業の
  ISIN と一致した記事だけを採用する。会社名検索の混入は ISIN で排除する
  (同定は ISIN が担う)。記事 URL は /news/{種別}/{slug}/{UUID} の安定形式
- GlobeNewswire: 会社別の Organization Atom フィード (直近20件・全文入り)
  を巡回する。フィード自体が会社単位なので、同定はフィードが担う。
  フィードのトークンは会社ごとに実測で確認したものだけを表に置く
  (未確認の会社は表で None とし、収集側で「未対応」として記録する)
- どちらのワイヤも、決算発表と判定した記事 (タイトル / GNW の subject が
  決算系) のうち、本文から通貨金額を含む文を抽出できたものだけを財務候補
  にする。数値が確認できない発表は材料にしない (調査の判定基準と同じ)

統合 (対象選定・取得間隔・記録) は pipeline の _collect_swiss が担う。
レコードの形は ROIC と同じ (info_type=disclosure、要旨に「財務数値(...)」
マーカーと抽出数値) で、既存の財務プール (related_financials) に乗る。
"""

import html
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from html.parser import HTMLParser

from ..http import http_fetch

EQS_BASE = "https://www.eqs-news.com"
GNW_FEED_BASE = "https://www.globenewswire.com/atomfeed/organization"

# スイス10社 (entities/ch.json の id) の ISIN と、ワイヤ別の巡回情報。
# ISIN は 2026-11-10 に各社の公表資料・銘柄情報で実測確認した値
# (Comet は調査 RESULTS.md の記事フッター記載、それ以外も銘柄情報源で照合)。
# EQS の検索語は会社名 (ティッカーでは引けない。RESULTS.md の見通しどおり)。
# GNW フィードのトークンは Basilea (フィード実物を取得して確認) と
# Molecular Partners (フィード索引で確認) の2社分のみ確認済み。他社は
# None = 未対応として収集側に記録され、確認でき次第ここへ足す
def swiss_enabled():
    """スイス収集の有効条件。キー不要のため本番では常時有効だが、実辞書を
    使うテストが実ネットワークへ出ないよう、無効化スイッチだけ用意する
    (ESEF と同じく、収集の後段バッチをテスト環境で切り離すためのゲート)。"""
    return not os.environ.get("TRL_SWISS_DISABLE")


SWISS_COMPANIES = {
    "idorsia": {"isin": "CH0363463438", "eqs_name": "Idorsia", "gnw_feed": None},
    "lonza": {"isin": "CH0013841017", "eqs_name": "Lonza", "gnw_feed": None},
    "comet": {"isin": "CH0360826991", "eqs_name": "Comet Holding", "gnw_feed": None},
    "basilea": {"isin": "CH0011432447", "eqs_name": "Basilea",
                "gnw_feed": "LIBAhmdQLCCbNyIagCjiEw=="},
    "molecular_partners": {"isin": "CH0256379097", "eqs_name": "Molecular Partners",
                           "gnw_feed": "bdDiccsOUe1piyuPtvT0LQ=="},
    "newron": {"isin": "IT0004147952", "eqs_name": "Newron", "gnw_feed": None},
    "kardex": {"isin": "CH0100837282", "eqs_name": "Kardex", "gnw_feed": None},
    "interroll": {"isin": "CH0006372897", "eqs_name": "Interroll", "gnw_feed": None},
    "santhera": {"isin": "CH0027148649", "eqs_name": "Santhera", "gnw_feed": None},
    "kuros": {"isin": "CH0325814116", "eqs_name": "Kuros", "gnw_feed": None},
}

# 決算発表らしいタイトルの目印 (英独)。本文の数値抽出が最終ゲートになるため、
# ここは記事取得の前段フィルタとして広めに取る (半期のトレーディング
# アップデートや受注発表も売上数値を含むため対象に含める)
RESULTS_TITLE_TERMS = (
    "result", "half-year", "half year", "full-year", "full year",
    "annual report", "financial", "earnings", "interim", "quarter",
    "trading update", "order intake", "auftragseingang",
    "halbjahr", "geschäftsjahr", "jahresbericht", "umsatz", "ergebnis",
    "q1 ", "q3 ", "9m ", "nine months", "first nine months",
)
# 決算数値を含まないことが明らかな定例発表 (タイトルにあっても除外する)
NON_RESULTS_TITLE_TERMS = (
    "general meeting", "hauptversammlung", "capital markets day",
    "appoint", "ernennung", "annual general meeting",
)

_MONEY_RE = re.compile(
    r"(?:CHF|EUR|USD|Fr\.)\s?\d[\d'’.,]*"
    r"|\d[\d'’.,]*\s?(?:million|billion|Millionen|Milliarden)\s?(?:CHF|EUR|USD)"
    r"|[€$]\s?\d[\d'’.,]*")
_ISIN_RE = re.compile(r"ISIN:?\s*\|?\s*([A-Z]{2}[A-Z0-9]{9}\d)")
_EQS_ID_RE = re.compile(r"EQS News ID:?\s*\|?\s*(\d+)")
_EQS_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s*/\s*\d{2}:\d{2}")


def is_results_title(title):
    t = f" {(title or '').lower()} "
    if any(term in t for term in NON_RESULTS_TITLE_TERMS):
        return False
    return any(term in t for term in RESULTS_TITLE_TERMS)


def html_to_text(raw):
    """HTML 文字列 → プレーンテキスト (script/style を落とし、タグを空白に)。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def extract_money_sentences(text, limit=8, max_chars=1200):
    """本文から通貨金額を含む文を抽出する。財務候補の数値ゲートを兼ねる:
    1文も取れなければ「数値を確認できない発表」として材料にしない。"""
    if not text:
        return []
    # 文の区切りはピリオド+空白+大文字/数字 (小数点は後ろが数字なので割れない)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9\"“])", text)
    out, total = [], 0
    for p in parts:
        p = p.strip()
        if not p or not _MONEY_RE.search(p):
            continue
        if len(p) > 400:
            p = p[:400].rstrip() + "…"
        if total + len(p) > max_chars and out:
            break
        out.append(p)
        total += len(p)
        if len(out) >= limit:
            break
    return out


def _record(source, marker, source_id, title, sentences, url, published, name):
    return {
        "source": source,
        "source_id": source_id,
        "title": title,
        "abstract": f"{name}の財務数値({marker}): " + " ".join(sentences),
        "url": url,
        "published": published,
        "fields": [marker, "財務"],
        "affiliations": [],
        "info_type": "disclosure",
    }


# ---------------------------------------------------------------- EQS

class _AnchorParser(HTMLParser):
    """検索結果ページから記事リンク (href, テキスト) を拾う。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._href = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if text:
                self.links.append((self._href, text))
            self._href = None
            self._buf = []


def parse_eqs_search(raw):
    """EQS 検索結果 HTML → [{url, title}] (記事 URL の形をしたリンクだけ)。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    parser = _AnchorParser()
    parser.feed(raw or "")
    out, seen = [], set()
    for href, text in parser.links:
        m = re.match(r"^/(?:[a-z]{2}/)?news/[^/]+/[^/]+/"
                     r"([0-9a-f-]{36})(?:_en|_de)?$", href)
        if not m or len(text) < 12 or href in seen:
            continue
        seen.add(href)
        out.append({"url": urllib.parse.urljoin(EQS_BASE, href),
                    "title": text, "uuid": m.group(1)})
    return out


def parse_eqs_article(raw):
    """EQS 記事 HTML → {isin, news_id, published, text}。同定情報はフッター。"""
    text = html_to_text(raw)
    isin = _ISIN_RE.search(text)
    news_id = _EQS_ID_RE.search(text)
    dm = _EQS_DATE_RE.search(text)
    published = None
    if dm:
        published = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
    return {"isin": isin.group(1) if isin else None,
            "news_id": news_id.group(1) if news_id else None,
            "published": published, "text": text}


def fetch_eqs(entity_id, max_results, lookback_days, as_of, http=None):
    """entity_id の会社の EQS 決算発表を取得する。query 相当は entity_id で、
    pipeline の _collect_swiss から呼ばれる (ROIC の fetcher と同じ流儀)。
    検索ページ自体の取得失敗は例外にする (0件で握りつぶさない)。"""
    comp = SWISS_COMPANIES[entity_id]
    fetch = http or http_fetch
    raw = fetch(EQS_BASE + "/search-results/",
                params={"searchtype": "news", "searchword": comp["eqs_name"],
                        "searchphrase": "all", "ordering": "newest",
                        "pageLimit": 50},
                retries=1)
    out = []
    article_errors = 0
    for hit in parse_eqs_search(raw):
        if len(out) >= max_results:
            break
        if not is_results_title(hit["title"]):
            continue
        try:
            article = parse_eqs_article(fetch(hit["url"], retries=1))
        except Exception:
            article_errors += 1
            continue
        # 同定: フッターの ISIN が対象企業と一致しない記事は、会社名検索に
        # 混入した他社の発表なので採用しない (ティッカーでは引けない前提)
        if article["isin"] != comp["isin"]:
            continue
        published = article["published"]
        pub = None
        if published:
            try:
                pub = date.fromisoformat(published)
            except ValueError:
                pub = None
        if pub is not None:
            if pub < as_of - timedelta(days=lookback_days) or pub > as_of + timedelta(days=2):
                continue
        sentences = extract_money_sentences(article["text"])
        if not sentences:
            continue
        out.append(_record("eqs", "EQS", article["news_id"] or hit["uuid"],
                           hit["title"], sentences, hit["url"], published,
                           comp["eqs_name"]))
    if article_errors and not out:
        # 記事が全滅した場合は検索成功・0件で握りつぶさず、失敗として上げる
        raise RuntimeError(f"EQS article fetch failed ({article_errors} articles)")
    return out


# ---------------------------------------------------------------- GlobeNewswire

_ATOM = {"a": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}


def parse_gnw_feed(raw):
    """GlobeNewswire の Organization Atom フィード → 記事のリスト。
    同一リリースの多言語版 (同じ dc:identifier) は英語版を優先して1件に畳む。"""
    if isinstance(raw, str):
        raw = raw.encode()
    root = ET.fromstring(raw)
    by_id = {}
    for entry in root.findall("a:entry", _ATOM):
        def txt(path):
            el = entry.find(path, _ATOM)
            return (el.text or "").strip() if el is not None else ""
        link = ""
        for el in entry.findall("a:link", _ATOM):
            if el.get("rel") in (None, "alternate"):
                link = el.get("href") or link
        if not link:
            link = txt("a:id")
        ident = txt("dc:identifier") or link
        subjects = [ (el.text or "").strip()
                     for el in entry.findall("dc:subject", _ATOM)]
        item = {"identifier": ident, "title": txt("a:title"), "link": link,
                "published": (txt("a:published") or txt("a:updated"))[:10],
                "summary": txt("a:summary"), "subjects": subjects}
        prev = by_id.get(ident)
        if prev is None or ("/en/" in link and "/en/" not in prev["link"]):
            by_id[ident] = item
    return list(by_id.values())


def fetch_gnw(entity_id, max_results, lookback_days, as_of, http=None):
    """entity_id の会社の GlobeNewswire 決算発表を会社別フィードから取得する。
    フィード未確認の会社は呼び出し側 (pipeline) で未対応として記録され、
    ここには来ない。フィード取得の失敗は例外にする (0件で握りつぶさない)。"""
    comp = SWISS_COMPANIES[entity_id]
    fetch = http or http_fetch
    raw = fetch(f"{GNW_FEED_BASE}/{comp['gnw_feed']}", retries=1)
    out = []
    for item in parse_gnw_feed(raw):
        if len(out) >= max_results:
            break
        published = item["published"]
        pub = None
        if published:
            try:
                pub = date.fromisoformat(published)
            except ValueError:
                pub = None
        if pub is not None:
            if pub < as_of - timedelta(days=lookback_days) or pub > as_of + timedelta(days=2):
                continue
        earnings = any("earnings" in s.lower() for s in item["subjects"])
        if not earnings and not is_results_title(item["title"]):
            continue
        sentences = extract_money_sentences(html_to_text(item["summary"]))
        if not sentences:
            continue
        out.append(_record("gnw", "GlobeNewswire", item["identifier"],
                           item["title"], sentences, item["link"], published,
                           comp["eqs_name"]))
    return out
