"""TRL投資分析システム (単一ファイル版 v0.1.0)。使い方は README.md を参照。"""
import argparse
import calendar
import hashlib
import html
import json
import math
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

__version__ = "0.1.0"


# ==================== config ====================
"""設定の読み込みと検証。設定ファイルのハッシュを実行ごとに記録する。"""

ROOT = Path(__file__).resolve().parent


class ConfigError(ValueError):
    pass


def validate_config(cfg):
    sc = cfg["scoring"]
    for kind in ("long", "event"):
        sec = sc.get(kind)
        if not sec:
            raise ConfigError(f"missing section: {kind}")
        w = sec["weights"]
        if any((not isinstance(v, (int, float))) or v <= 0 for v in w.values()):
            raise ConfigError(f"{kind}: weights must be positive numbers")
        if abs(sum(w.values()) - 100) > 1e-9:
            raise ConfigError(f"{kind}: weights must sum to 100 (got {sum(w.values())})")
        for r in sec["required"]:
            if r not in w:
                raise ConfigError(f"{kind}: required item {r} not in weights")
    bands = sc["bands"]
    if bands[0]["min"] != 0 or bands[-1]["max"] != 100:
        raise ConfigError("bands must cover 0..100")
    for a, b in zip(bands, bands[1:]):
        if b["min"] != a["max"] + 1:
            raise ConfigError("bands must be contiguous")
    for k in ("min_abstract_chars", "max_age_days", "max_eval_per_run", "reeval_after_days", "max_retries"):
        v = sc.get("screening", {}).get(k)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ConfigError(f"scoring.json: screening.{k} must be a non-negative integer")
    for k in ("extract", "long", "event"):
        if not isinstance(sc.get("prompts", {}).get(k), str) or not sc["prompts"][k]:
            raise ConfigError(f"scoring.json: prompts.{k} (version string) is required")
    mode = sc.get("screening", {}).get("require_entity", "off")
    if mode not in ENTITY_MODES:
        raise ConfigError("scoring.json: screening.require_entity must be off/strong/any")
    if cfg.get("entities") is not None:
        validate_entities(cfg["entities"])
    elif mode != "off":
        raise ConfigError("scoring.json: screening.require_entity needs entities.json")
    validate_sources(cfg["sources"])


def validate_sources(src):
    """sources.json のスキーマ検証。
    {"defaults": {"max_results": int>0, "lookback_days": int>0},
     "queries": [{"name": str, "pubmed": str?, "arxiv": str?, "max_results": int?, "lookback_days": int?}]}"""
    d = src.get("defaults")
    if not isinstance(d, dict):
        raise ConfigError("sources.json: defaults must be an object")
    for k in ("max_results", "lookback_days"):
        if isinstance(d.get(k), bool) or not isinstance(d.get(k), int) or d[k] <= 0:
            raise ConfigError(f"sources.json: defaults.{k} must be a positive integer")
    qs = src.get("queries")
    if not isinstance(qs, list) or not qs:
        raise ConfigError("sources.json: queries must be a non-empty list")
    names = set()
    for i, q in enumerate(qs):
        if not isinstance(q, dict) or not isinstance(q.get("name"), str) or not q["name"].strip():
            raise ConfigError(f"sources.json: queries[{i}].name is required")
        if q["name"] in names:
            raise ConfigError(f"sources.json: duplicate query name: {q['name']}")
        names.add(q["name"])
        if not any(isinstance(q.get(s), str) and q[s].strip() for s in ("pubmed", "arxiv")):
            raise ConfigError(f"sources.json: queries[{i}] needs a non-empty 'pubmed' or 'arxiv' query string")
        for k in ("max_results", "lookback_days"):
            if k in q and (isinstance(q[k], bool) or not isinstance(q[k], int) or q[k] <= 0):
                raise ConfigError(f"sources.json: queries[{i}].{k} must be a positive integer")


CONFIG_FILES = ("scoring.json", "sources.json")


def required_files(config_dir=None):
    """動作に必要なファイル(設定JSON + scoring.json の prompts が指すプロンプト)と存在有無。"""
    d = Path(config_dir) if config_dir else ROOT
    try:
        prompts = json.loads((d / "scoring.json").read_text(encoding="utf-8-sig")).get("prompts", {})
    except (OSError, ValueError):
        prompts = {}
    names = list(CONFIG_FILES) + [f"{n}.{v}.md" for n, v in prompts.items()]
    if not prompts:
        names += ["extract.v1.md", "long.v1.md", "event.v1.md"]  # scoring.json が読めないときの既定
    return [(n, (d / n).is_file()) for n in names]


def missing_files(config_dir=None):
    return [n for n, ok in required_files(config_dir) if not ok]



def load_config(config_dir=None):
    d = Path(config_dir) if config_dir else ROOT
    gone = [n for n in CONFIG_FILES if not (d / n).is_file()]
    if gone:
        raise ConfigError(f"必須ファイルが見つかりません: {', '.join(gone)} (探した場所: {d})。リポジトリ直下に作成してください")
    raw, h = {}, hashlib.sha256()
    for name in ("scoring", "sources"):
        text = (d / f"{name}.json").read_text(encoding="utf-8-sig")
        h.update(text.encode("utf-8"))
        raw[name] = json.loads(text)

    entities_path = d / "entities.json"
    entities = None
    if entities_path.is_file():
        entities_text = entities_path.read_text(encoding="utf-8-sig")
        h.update(entities_text.encode("utf-8"))
        entities = json.loads(entities_text)

    cfg = {
        "scoring": raw["scoring"],
        "sources": raw["sources"],
        "entities": entities,
        "config_version": raw["scoring"]["config_version"],
        "config_hash": h.hexdigest()[:16],
    }
    validate_config(cfg)
    return cfg



# ==================== freshness ====================
"""日付の解釈と鮮度計算。基準日(as_of)は必ず引数で受け取る(内部で today を使わない)。"""

_RE = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")


def parse_date(s, end=False):
    """YYYY / YYYY-MM / YYYY-MM-DD を date に。欠けた部分は end=False で期間の最初、True で最後。"""
    if isinstance(s, date):
        return s
    if not isinstance(s, str):
        return None
    m = _RE.fullmatch(s.strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), m.group(2), m.group(3)
    try:
        if mo is None:
            return date(y, 12, 31) if end else date(y, 1, 1)
        mo = int(mo)
        last = calendar.monthrange(y, mo)[1]
        if d is None:
            return date(y, mo, last if end else 1)
        return date(y, mo, int(d))
    except ValueError:
        return None


def days_since(published, as_of):
    p = parse_date(published)
    return None if p is None else (as_of - p).days


def freshness_info(published, as_of):
    n = days_since(published, as_of)
    if n is None:
        bucket = "unknown"
    elif n < 0:
        bucket = "future"
    elif n <= 30:
        bucket = "fresh_30d"
    elif n <= 180:
        bucket = "recent_180d"
    else:
        bucket = "older"
    return {"published": published, "as_of": as_of.isoformat(), "days_since_published": n, "bucket": bucket}


# ==================== http ====================
"""HTTP取得(リトライ・秘密情報のマスク付き)。標準ライブラリのみ。"""

SECRET_ENV = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "NCBI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "TOKEN")
_PARAM_RE = re.compile(r"(?i)(api[_-]?key|key|token)=([^&\s]+)")


def redact(text):
    text = str(text)
    for name in SECRET_ENV:
        v = os.environ.get(name)
        if v and len(v) >= 6:
            text = text.replace(v, "***")
    return _PARAM_RE.sub(lambda m: m.group(1) + "=***", text)


class HttpError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def http_fetch(url, params=None, headers=None, data=None, timeout=30, retries=3,
          backoff=2.0, opener=None, sleep=time.sleep):
    opener = opener or urllib.request.urlopen
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    hdrs = {"User-Agent": "trl-analysis/0.1"}
    hdrs.update(headers or {})
    attempt = 0
    while True:
        req = urllib.request.Request(full, data=data, headers=hdrs)
        try:
            with opener(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            status, retryable, msg = e.code, (e.code == 429 or e.code >= 500), f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            status, retryable, msg = None, True, f"network error: {type(e).__name__}"
        attempt += 1
        if not retryable or attempt > retries:
            # URLのクエリ(APIキーが入り得る)はメッセージに含めない
            raise HttpError(redact(f"{msg} for {url}"), status)
        sleep(backoff ** attempt)


# ==================== normalize ====================
"""正規化と基本検証。重複判定は安定ID(DOI/PMID/arXiv ID/URL)のみ。タイトル類似では統合しない。"""



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
        "published": published,
        "retrieved_at": retrieved_at,
        "fields": [clean_text(f) for f in raw.get("fields", []) if clean_text(f)],
        "affiliations": list(dict.fromkeys(
            clean_text(a) for a in raw.get("affiliations", []) if clean_text(a)
        )),
        "info_type": raw.get("info_type", "paper"),
    }
    return rec, problems


# ==================== sources/pubmed ====================
"""PubMed (NCBI E-utilities)。"""


BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
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
    delay = 0.12 if key else 0.4
    common = {"tool": "trl-analysis"}
    if key:
        common["api_key"] = key
    params = dict(common, db="pubmed", term=query, retmax=max_results, retmode="json",
                  datetype="pdat", sort="pub_date",
                  mindate=(as_of - timedelta(days=lookback_days)).strftime("%Y/%m/%d"),
                  maxdate=as_of.strftime("%Y/%m/%d"))
    data = json.loads(http(BASE + "esearch.fcgi", params=params))
    ids = data.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    sleep(delay)
    xml = http(BASE + "efetch.fcgi", params=dict(common, db="pubmed", id=",".join(ids), retmode="xml"))
    return parse_efetch(xml)


# ==================== sources/arxiv ====================
"""arXiv API (Atom)。"""


URL = "http://export.arxiv.org/api/query"
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


# ==================== scoring ====================
"""採点の検証・総合点計算・スコア帯。nullを含む場合は総合点を確定せず hold にする(重み再配分はしない)。"""


def item_keys(kind, cfg):
    return list(cfg["scoring"][kind]["weights"].keys())


def validate_items(items, kind, cfg):
    """items: {key: score|None}。形式エラーの一覧を返す(null は許容)。"""
    errors = []
    keys = set(item_keys(kind, cfg))
    for k in keys - set(items):
        errors.append(f"missing_item:{k}")
    for k in set(items) - keys:
        errors.append(f"unknown_item:{k}")
    for k, v in items.items():
        if k not in keys or v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errors.append(f"not_a_number:{k}")
        elif math.isnan(v) or math.isinf(v) or v < 0 or v > 100:
            errors.append(f"out_of_range:{k}")
    return errors


def band_for(score, cfg):
    best = None
    for b in cfg["scoring"]["bands"]:
        if score >= b["min"]:
            best = b
    return {"label": best["label"], "description": best["description"]}


def compute(items, kind, cfg):
    """-> {status: final|hold|error, total, band, missing, critical_missing, errors}"""
    errors = validate_items(items, kind, cfg)
    if errors:
        return {"status": "error", "total": None, "band": None, "missing": [],
                "critical_missing": [], "errors": errors}
    sec = cfg["scoring"][kind]
    missing = [k for k in sec["weights"] if items[k] is None]
    critical = [k for k in sec["required"] if items[k] is None]
    if missing:
        return {"status": "hold", "total": None, "band": None, "missing": missing,
                "critical_missing": critical, "errors": []}
    total = round(sum(items[k] * w for k, w in sec["weights"].items()) / 100.0, 1)
    return {"status": "final", "total": total, "band": band_for(total, cfg),
            "missing": [], "critical_missing": [], "errors": []}

def _norm_text(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())
ENTITY_MODES = ("off", "strong", "any")


def validate_entities(data):
    """entities.json の形式・ID・別名の衝突を検証する。"""
    if not isinstance(data, dict):
        raise ConfigError("entities.json must be an object")

    entities = data.get("entities")
    if not isinstance(entities, list) or not entities:
        raise ConfigError("entities.json: entities must be a non-empty list")

    ids = set()
    aliases = {}

    for i, entity in enumerate(entities):
        if not isinstance(entity, dict):
            raise ConfigError(f"entities[{i}] must be an object")

        eid = entity.get("id")
        name = entity.get("name")
        if not isinstance(eid, str) or not re.fullmatch(r"[a-z0-9_]+", eid):
            raise ConfigError(f"entities[{i}].id is invalid")
        if eid in ids:
            raise ConfigError(f"duplicate entity id: {eid}")
        ids.add(eid)

        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"entities[{i}].name is required")

        entity_aliases = entity.get("aliases", [])
        if not isinstance(entity_aliases, list):
            raise ConfigError(f"entities[{i}].aliases must be a list")

        names = [name] + entity_aliases
        for alias in names:
            if not isinstance(alias, str) or not alias.strip():
                raise ConfigError(f"entities[{i}] has an empty or invalid alias")
            normalized = _norm_text(alias)
            if len(normalized) < 3:
                raise ConfigError(f"entity alias is too short: {alias}")
            previous = aliases.get(normalized)
            if previous is not None and previous != eid:
                raise ConfigError(
                    f"entity alias collision: {alias} ({previous}, {eid})"
                )
            aliases[normalized] = eid

        products = entity.get("products", [])
        if not isinstance(products, list):
            raise ConfigError(f"entities[{i}].products must be a list")

        product_ids = set()
        for j, product in enumerate(products):
            if not isinstance(product, dict):
                raise ConfigError(f"entities[{i}].products[{j}] must be an object")

            pid = product.get("id")
            pname = product.get("name")
            if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9_]+", pid):
                raise ConfigError(f"entities[{i}].products[{j}].id is invalid")
            if pid in product_ids:
                raise ConfigError(f"duplicate product id in {eid}: {pid}")
            product_ids.add(pid)

            if not isinstance(pname, str) or not pname.strip():
                raise ConfigError(f"entities[{i}].products[{j}].name is required")

            product_aliases = product.get("aliases", [])
            if not isinstance(product_aliases, list):
                raise ConfigError(
                    f"entities[{i}].products[{j}].aliases must be a list"
                )
            for alias in [pname] + product_aliases:
                if not isinstance(alias, str) or not alias.strip():
                    raise ConfigError(
                        f"entities[{i}].products[{j}] has an invalid alias"
                    )

    return True


def _contains(alias, text):
    """英数字の単語境界を考慮し、日本語などは部分一致で照合する。"""
    alias = _norm_text(alias)
    text = _norm_text(text)
    if not alias or not text:
        return False

    if re.search(r"[a-z0-9]", alias):
        pattern = r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])"
        return re.search(pattern, text) is not None

    return alias in text

def link_entities(rec, data):
    """論文と企業・製品を照合する。所属機関一致はstrong、本文一致はweak。"""
    if not isinstance(data, dict):
        return []

    title = rec.get("title", "")
    abstract = rec.get("abstract", "")
    affiliations = rec.get("affiliations", []) or []

    if isinstance(affiliations, str):
        affiliations = [affiliations]

    fields = [
        ("affiliation", affiliations),
        ("title", [title]),
        ("abstract", [abstract]),
    ]

    results = []

    for entity in data.get("entities", []):
        names = [entity.get("name", "")] + entity.get("aliases", [])
        match = None

        for field, values in fields:
            for value in values:
                for alias in names:
                    if alias and _contains(alias, value):
                        match = {
                            "entity_id": entity["id"],
                            "alias": alias,
                            "field": field,
                            "strength": "strong" if field == "affiliation" else "weak",
                            "link_type": "entity",
                        }
                        break
                if match:
                    break
            if match:
                break

        if match:
            results.append(match)

        for product in entity.get("products", []):
            product_names = [product.get("name", "")] + product.get("aliases", [])

            for field, values in fields:
                matched = False
                for value in values:
                    for alias in product_names:
                        if alias and _contains(alias, value):
                            results.append({
                                "entity_id": entity["id"],
                                "alias": alias,
                                "field": field,
                                "strength": "weak",
                                "link_type": "product",
                                "product_id": product["id"],
                                "product_name": product["name"],
                            })
                            matched = True
                            break
                    if matched:
                        break
                if matched:
                    break

    return results

def entity_screen(candidate, mode):
    """企業リンクの有無を判定する。通過時はNone、除外時は理由を返す。"""
    if mode not in ENTITY_MODES:
        raise ConfigError("require_entity must be off/strong/any")

    if mode == "off":
        return None

    links = candidate.get("entities", []) or []
    direct_links = [
        link for link in links
        if link.get("link_type", "entity") == "entity"
    ]

    if mode == "strong":
        if any(link.get("strength") == "strong" for link in direct_links):
            return None
        return "no_linked_entity"

    if any(link.get("strength") in ("strong", "weak") for link in direct_links):
        return None
    return "no_linked_entity"
# ==================== store ====================
"""SQLite による永続化。重複排除は identifiers テーブルの安定IDで行う。"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates(
  id TEXT PRIMARY KEY, title TEXT, abstract TEXT, url TEXT, source TEXT, source_id TEXT,
  doi TEXT, published TEXT, retrieved_at TEXT, fields_json TEXT, info_type TEXT,
  status TEXT NOT NULL DEFAULT 'new', screen_reason TEXT, last_eval_at TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0, first_seen_run TEXT);
CREATE TABLE IF NOT EXISTS identifiers(key TEXT PRIMARY KEY, candidate_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, candidate_id TEXT, kind TEXT, as_of TEXT,
  evaluated_at TEXT, status TEXT, total REAL, band TEXT, items_json TEXT, extraction_json TEXT,
  event_json TEXT, prompt_versions_json TEXT, config_version TEXT, config_hash TEXT,
  llm_provider TEXT, llm_model TEXT, input_hash TEXT, errors_json TEXT);
CREATE TABLE IF NOT EXISTS review_queue(
  candidate_id TEXT, kind TEXT, reason TEXT, missing_json TEXT, next_sources_json TEXT,
  reeval_conditions_json TEXT, last_eval_at TEXT, versions_json TEXT, created_at TEXT,
  updated_at TEXT, resolved_at TEXT, PRIMARY KEY(candidate_id, kind));
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, config_version TEXT, config_hash TEXT,
  as_of TEXT, params_json TEXT, counts_json TEXT, status TEXT, duration_sec REAL);
CREATE TABLE IF NOT EXISTS run_sources(
  run_id TEXT, source TEXT, query TEXT, fetched INTEGER, new INTEGER, duplicates INTEGER,
  invalid INTEGER, ok INTEGER, error TEXT, seconds REAL);
CREATE TABLE IF NOT EXISTS candidate_entities (
    candidate_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    strength TEXT NOT NULL,
    matched_by TEXT NOT NULL,
    matched_alias TEXT NOT NULL,
    link_type TEXT NOT NULL DEFAULT 'entity',
    product_id TEXT NOT NULL DEFAULT '',
    product_name TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (
        candidate_id,
        entity_id,
        link_type,
        product_id,
        matched_by,
        matched_alias
    ),
    FOREIGN KEY (candidate_id) REFERENCES candidates(id)
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _j(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=DELETE")  # -wal/-shm を作らない(コミット対象を1ファイルに)
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.commit()
        self.conn.close()

    # --- candidates ---
    def upsert_candidate(self, rec, run_id=None):
        """-> (candidate_id, is_new)。既存IDに一致すれば統合し、欠けた項目だけ補う。"""
        c = self.conn
        existing = None
        for k in rec["keys"]:
            row = c.execute("SELECT candidate_id FROM identifiers WHERE key=?", (k,)).fetchone()
            if row:
                existing = row[0]
                break
        if existing:
            for k in rec["keys"]:
                c.execute("INSERT OR IGNORE INTO identifiers VALUES(?,?)", (k, existing))
            cur = self.get_candidate(existing)
            if not cur["abstract"] and rec["abstract"]:
                c.execute("UPDATE candidates SET abstract=? WHERE id=?", (rec["abstract"], existing))
            if not cur["doi"] and rec.get("doi"):
                c.execute("UPDATE candidates SET doi=? WHERE id=?", (rec["doi"], existing))
            c.commit()
            return existing, False
        cid = "c_" + hashlib.sha1(rec["keys"][0].encode()).hexdigest()[:12]
        c.execute(
            "INSERT INTO candidates(id,title,abstract,url,source,source_id,doi,published,retrieved_at,"
            "fields_json,info_type,status,first_seen_run) VALUES(?,?,?,?,?,?,?,?,?,?,?,'new',?)",
            (cid, rec["title"], rec["abstract"], rec["url"], rec["source"], rec["source_id"], rec.get("doi"),
             rec["published"], rec["retrieved_at"], _j(rec["fields"]), rec["info_type"], run_id))
        for k in rec["keys"]:
            c.execute("INSERT OR IGNORE INTO identifiers VALUES(?,?)", (k, cid))
        c.commit()
        return cid, True

    def get_candidate(self, cid):
        row = self.conn.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["fields"] = json.loads(d.pop("fields_json") or "[]")
        d["entities"] = self.get_entities(cid)
        return d
        
    def set_entities(self, cid, links):
        """企業・製品リンクを保存し、既存の強いリンクを維持する。"""
        c = self.conn

        for link in links:
            entity_id = link["entity_id"]
            strength = link["strength"]
            matched_by = link.get("matched_by", link.get("field", ""))
            matched_alias = link.get("matched_alias", link.get("alias", ""))
            link_type = link.get("link_type", "entity")
            product_id = link.get("product_id") or ""
            product_name = link.get("product_name") or ""

            existing = c.execute(
                "SELECT strength FROM candidate_entities "
                "WHERE candidate_id=? AND entity_id=? AND link_type=? "
                "AND product_id=? AND matched_by=? AND matched_alias=?",
                (
                    cid, entity_id, link_type, product_id,
                    matched_by, matched_alias,
                ),
            ).fetchone()

            if existing:
                if existing["strength"] != "strong" and strength == "strong":
                    c.execute(
                        "UPDATE candidate_entities SET strength=? "
                        "WHERE candidate_id=? AND entity_id=? AND link_type=? "
                        "AND product_id=? AND matched_by=? AND matched_alias=?",
                        (
                            "strong", cid, entity_id, link_type,
                            product_id, matched_by, matched_alias,
                        ),
                    )
                continue

            c.execute(
                "INSERT INTO candidate_entities "
                "(candidate_id, entity_id, strength, matched_by, matched_alias, "
                "link_type, product_id, product_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cid, entity_id, strength, matched_by, matched_alias,
                    link_type, product_id, product_name,
                ),
            )

        c.commit()


    def get_entities(self, cid):
        """候補論文に紐づく企業・製品リンクを取得する。"""
        rows = self.conn.execute(
            "SELECT entity_id, strength, matched_by, matched_alias, "
            "link_type, product_id, product_name "
            "FROM candidate_entities WHERE candidate_id=? "
            "ORDER BY entity_id, link_type, matched_by",
            (cid,),
        ).fetchall()

        return [
            {
                "entity_id": row["entity_id"],
                "strength": row["strength"],
                "matched_by": row["matched_by"],
                "matched_alias": row["matched_alias"],
                "link_type": row["link_type"],
                "product_id": row["product_id"],
                "product_name": row["product_name"],
            }
            for row in rows
        ]

    def list_by_status(self, statuses):
        q = ",".join("?" * len(statuses))
        rows = self.conn.execute(f"SELECT id FROM candidates WHERE status IN ({q})", statuses).fetchall()
        return [self.get_candidate(r[0]) for r in rows]

    def set_status(self, cid, status, reason=None, eval_at=None, retry_inc=False):
        self.conn.execute(
            "UPDATE candidates SET status=?, screen_reason=COALESCE(?,screen_reason), "
            "last_eval_at=COALESCE(?,last_eval_at), retry_count=retry_count+? WHERE id=?",
            (status, reason, eval_at, 1 if retry_inc else 0, cid))
        self.conn.commit()

    def count_candidates(self):
        return self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]

    # --- evaluations ---
    def add_evaluation(self, run_id, cid, ev):
        cur = self.conn.execute(
            "INSERT INTO evaluations(run_id,candidate_id,kind,as_of,evaluated_at,status,total,band,items_json,"
            "extraction_json,event_json,prompt_versions_json,config_version,config_hash,llm_provider,llm_model,"
            "input_hash,errors_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, cid, ev["kind"], ev["as_of"], now_iso(), ev["status"], ev["total"],
             (ev["band"] or {}).get("label"), _j(ev["items"]), _j(ev.get("extraction")), _j(ev.get("event")),
             _j(ev["prompt_versions"]), ev["config_version"], ev["config_hash"], ev["llm_provider"],
             ev["llm_model"], ev["input_hash"], _j(ev["errors"])))
        self.conn.commit()
        return cur.lastrowid

    def latest_evaluation(self, cid, kind):
        row = self.conn.execute(
            "SELECT * FROM evaluations WHERE candidate_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (cid, kind)).fetchone()
        return dict(row) if row else None

    def evaluations_for_run(self, run_id, kind=None):
        q = "SELECT e.*, c.title, c.url FROM evaluations e JOIN candidates c ON c.id=e.candidate_id WHERE run_id=?"
        args = [run_id]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        return [dict(r) for r in self.conn.execute(q, args).fetchall()]

    # --- review queue ---
    def upsert_queue(self, cid, kind, reason, missing, next_sources, reeval, last_eval_at, versions):
        now = now_iso()
        self.conn.execute(
            "INSERT INTO review_queue(candidate_id,kind,reason,missing_json,next_sources_json,"
            "reeval_conditions_json,last_eval_at,versions_json,created_at,updated_at,resolved_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(candidate_id,kind) DO UPDATE SET "
            "reason=excluded.reason, missing_json=excluded.missing_json, "
            "next_sources_json=excluded.next_sources_json, reeval_conditions_json=excluded.reeval_conditions_json, "
            "last_eval_at=excluded.last_eval_at, versions_json=excluded.versions_json, "
            "updated_at=excluded.updated_at, resolved_at=NULL",
            (cid, kind, reason, _j(missing), _j(next_sources), _j(reeval), last_eval_at, _j(versions), now, now))
        self.conn.commit()

    def resolve_queue(self, cid, kind):
        self.conn.execute("UPDATE review_queue SET resolved_at=? WHERE candidate_id=? AND kind=? AND resolved_at IS NULL",
                          (now_iso(), cid, kind))
        self.conn.commit()

    def list_queue(self, open_only=True):
        q = ("SELECT r.*, c.title, c.url FROM review_queue r JOIN candidates c ON c.id=r.candidate_id"
             + (" WHERE r.resolved_at IS NULL" if open_only else "") + " ORDER BY r.updated_at DESC")
        out = []
        for r in self.conn.execute(q).fetchall():
            d = dict(r)
            for k in ("missing_json", "next_sources_json", "reeval_conditions_json", "versions_json"):
                d[k[:-5]] = json.loads(d.pop(k) or "null")
            out.append(d)
        return out

    # --- runs ---
    def start_run(self, run_id, config_version, config_hash, as_of, params):
        self.conn.execute("INSERT INTO runs(run_id,started_at,config_version,config_hash,as_of,params_json,status) "
                          "VALUES(?,?,?,?,?,?,'running')", (run_id, now_iso(), config_version, config_hash, as_of, _j(params)))
        self.conn.commit()

    def finish_run(self, run_id, counts, status, duration):
        self.conn.execute("UPDATE runs SET finished_at=?, counts_json=?, status=?, duration_sec=? WHERE run_id=?",
                          (now_iso(), _j(counts), status, round(duration, 2), run_id))
        self.conn.commit()

    def add_run_source(self, run_id, source, query, fetched, new, dups, invalid, ok, error, seconds):
        self.conn.execute("INSERT INTO run_sources VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (run_id, source, query, fetched, new, dups, invalid, 1 if ok else 0, error, round(seconds, 2)))
        self.conn.commit()

    def get_run(self, run_id):
        r = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(r) if r else None

    def run_sources(self, run_id):
        return [dict(r) for r in self.conn.execute("SELECT * FROM run_sources WHERE run_id=?", (run_id,)).fetchall()]


# ==================== prompts ====================
"""プロンプトの読み込みと差し込み。バージョンと内容ハッシュを評価結果に紐づける。"""


PROMPT_DIR = ROOT
_PH = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


class PromptError(ValueError):
    pass


def load_prompt(name, version, prompt_dir=None):
    path = Path(prompt_dir or PROMPT_DIR) / f"{name}.{version}.md"
    if not path.exists():
        raise PromptError(f"prompt not found: {path.name}")
    text = path.read_text(encoding="utf-8-sig")
    return {"name": name, "version": version, "sha": hashlib.sha256(text.encode()).hexdigest()[:12], "text": text}


def render(template, values):
    """{{name}} を一括置換。未指定のプレースホルダはエラー。差し込んだ値は再展開しない。"""
    missing = {m for m in _PH.findall(template) if m not in values}
    if missing:
        raise PromptError(f"missing values for placeholders: {sorted(missing)}")
    return _PH.sub(lambda m: str(values[m.group(1)]), template)


# ==================== llm ====================
"""LLMプロバイダ。既定は none(評価をせず収集のみ)。課金が発生し得るため明示設定が必要。"""


SYSTEM = "あなたは根拠を重視する投資分析アシスタントです。指定されたJSON形式のみで答え、推測で情報を補いません。"


class LLMError(Exception):
    pass


def extract_json(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise LLMError("no JSON object in response")
    try:
        obj = json.loads(t[a:b + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"invalid JSON: {e.msg}") from None
    if not isinstance(obj, dict):
        raise LLMError("JSON root must be an object")
    return obj


class Anthropic:
    name = "anthropic"

    def __init__(self, model=None, http=None):
        self.model = model or os.environ.get("TRL_LLM_MODEL") or "claude-haiku-5-5"
        self.http = http or http_fetch
        self.key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.key:
            raise LLMError("ANTHROPIC_API_KEY is not set")

    def complete(self, prompt):
        body = json.dumps({"model": self.model, "max_tokens": 3000, "system": SYSTEM,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        raw = self.http("https://api.anthropic.com/v1/messages", data=body, headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"}, timeout=120)
        try:
            return "".join(b.get("text", "") for b in json.loads(raw)["content"])
        except (KeyError, ValueError, TypeError):
            raise LLMError("unexpected Anthropic response shape") from None


class Gemini:
    name = "gemini"

    def __init__(self, model=None, http=None):
        self.model = model or os.environ.get("TRL_LLM_MODEL") or "gemini-3.8-flash"
        self.http = http or http_fetch
        self.key = os.environ.get("GEMINI_API_KEY")
        if not self.key:
            raise LLMError("GEMINI_API_KEY is not set")

    def complete(self, prompt):
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}).encode()
        raw = self.http(f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                        data=body, headers={"x-goog-api-key": self.key, "content-type": "application/json"}, timeout=120)
        try:
            parts = json.loads(raw)["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, ValueError, TypeError):
            raise LLMError("unexpected Gemini response shape") from None


class Mock:
    """テスト用。fn(prompt) -> str。"""
    name = "mock"
    model = "mock-1"

    def __init__(self, fn):
        self.fn = fn

    def complete(self, prompt):
        return self.fn(prompt)


def get_provider(name, model=None, http=None):
    name = (name or "none").lower()
    if name == "none":
        return None
    if name == "anthropic":
        return Anthropic(model, http)
    if name == "gemini":
        return Gemini(model, http)
    raise LLMError(f"unknown provider: {name}")


# ==================== evaluate ====================
"""AI応答の検証と評価の実行(根拠抽出 → 長期 / イベント)。"""


EVIDENCE_KINDS = {"fact", "inference", "forecast", "hypothesis"}
EXTRACT_LISTS = ("facts", "inferences", "forecasts", "hypotheses", "counter_points", "missing_info")
STATEMENT_LISTS = ("facts", "inferences", "forecasts", "hypotheses")


def validate_extraction(obj):
    errors = []
    for k in EXTRACT_LISTS:
        if not isinstance(obj.get(k), list):
            errors.append(f"extraction.{k}:must_be_list")
    for k in STATEMENT_LISTS:
        for i, it in enumerate(obj.get(k) or []):
            if not isinstance(it, dict) or not str(it.get("statement", "")).strip():
                errors.append(f"extraction.{k}[{i}]:statement_required")
    return errors


def _nonempty_list(v):
    return isinstance(v, list) and any(str(x).strip() for x in v)


def validate_assessment(obj, kind, cfg):
    """-> (scores, details, errors)。スコア無根拠(理由/出典なし)や不足理由なしのnullは不正。"""
    errors, scores, details = [], {}, {}
    items = obj.get("items")
    if not isinstance(items, dict):
        return {}, {}, ["items:must_be_object"]
    for key in item_keys(kind, cfg):
        it = items.get(key)
        if not isinstance(it, dict):
            errors.append(f"missing_item:{key}")
            continue
        score = it.get("score")
        scores[key] = score
        ev = it.get("evidence")
        if score is not None:
            if not str(it.get("reason", "")).strip():
                errors.append(f"reason_required:{key}")
            if not isinstance(ev, list) or not ev:
                errors.append(f"evidence_required:{key}")
            else:
                for j, e in enumerate(ev):
                    if not isinstance(e, dict) or not str(e.get("ref", "")).strip() \
                            or not str(e.get("summary", "")).strip() or e.get("kind") not in EVIDENCE_KINDS:
                        errors.append(f"bad_evidence:{key}[{j}]")
        elif not _nonempty_list(it.get("missing_info")):
            errors.append(f"missing_info_required_for_null:{key}")
        for f in ("counter_evidence", "missing_info"):
            if it.get(f) is not None and not isinstance(it.get(f), list):
                errors.append(f"{f}:must_be_list:{key}")
        details[key] = {"score": score, "reason": it.get("reason", ""), "evidence": ev or [],
                        "counter_evidence": it.get("counter_evidence") or [],
                        "missing_info": it.get("missing_info") or []}
    for f in ("next_sources", "reeval_conditions"):
        if obj.get(f) is not None and not isinstance(obj.get(f), list):
            errors.append(f"{f}:must_be_list")
    if kind == "event":
        ev = obj.get("event")
        if not isinstance(ev, dict):
            errors.append("event:must_be_object")
        elif ev.get("classification") not in (None, 1, 2, 3, 4):
            errors.append("event.classification:invalid")
    return scores, details, errors


def _candidate_block(cand):
    return json.dumps({k: cand.get(k) for k in ("id", "title", "abstract", "url", "source", "published", "fields")},
                      ensure_ascii=False, indent=1)


def input_hash(cand, as_of):
    s = "|".join(str(cand.get(k)) for k in ("title", "abstract", "url", "published")) + "|" + as_of.isoformat()
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def run_extraction(cand, cfg, llm, as_of, prompt_dir=None):
    p = load_prompt("extract", cfg["scoring"]["prompts"]["extract"], prompt_dir)
    text = render(p["text"], {"as_of": as_of.isoformat(), "candidate": _candidate_block(cand),
                              "freshness": json.dumps(freshness_info(cand["published"], as_of))})
    obj = extract_json(llm.complete(text))
    errs = validate_extraction(obj)
    if errs:
        raise LLMError("invalid extraction: " + ",".join(errs[:5]))
    return obj, {"name": "extract", "version": p["version"], "sha": p["sha"]}


def evaluate_one(cand, kind, cfg, llm, as_of, extraction=None, extract_meta=None, prompt_dir=None):
    """1候補・1系統の評価。失敗しても例外を投げず status=error の結果を返す。"""
    versions = {}
    result = {"kind": kind, "as_of": as_of.isoformat(), "status": "error", "total": None, "band": None,
              "items": {}, "extraction": extraction, "event": None, "next_sources": [], "reeval_conditions": [],
              "missing": [], "critical_missing": [], "errors": [], "prompt_versions": versions,
              "config_version": cfg["config_version"], "config_hash": cfg["config_hash"],
              "llm_provider": getattr(llm, "name", None), "llm_model": getattr(llm, "model", None),
              "input_hash": input_hash(cand, as_of)}
    try:
        if extraction is None:
            extraction, meta = run_extraction(cand, cfg, llm, as_of, prompt_dir)
            result["extraction"], extract_meta = extraction, meta
        versions["extract"] = extract_meta
        p = load_prompt(kind, cfg["scoring"]["prompts"][kind], prompt_dir)
        versions[kind] = {"name": kind, "version": p["version"], "sha": p["sha"]}
        text = render(p["text"], {"as_of": as_of.isoformat(), "candidate": _candidate_block(cand),
                                  "extraction": json.dumps(extraction, ensure_ascii=False),
                                  "item_keys": ", ".join(item_keys(kind, cfg))})
        obj = extract_json(llm.complete(text))
        scores, details, errs = validate_assessment(obj, kind, cfg)
        if errs:
            result["errors"] = errs
            return result
        if kind == "event":
            ev = obj.get("event") or {}
            result["event"] = ev
            if not str(ev.get("market_expectation", "") or "").strip():
                # 市場予想が確認できない → 分類は確定せず、乖離・織り込み度は根拠なしとして保留
                ev["classification"] = None
                for k in ("expectation_gap", "not_priced_in"):
                    if scores.get(k) is not None:
                        scores[k] = None
                        details[k]["score"] = None
                        details[k]["missing_info"] = list(details[k]["missing_info"]) + ["市場予想が未確認のため保留"]
        res = compute(scores, kind, cfg)
        result.update({"status": res["status"], "total": res["total"], "band": res["band"], "items": details,
                       "missing": res["missing"], "critical_missing": res["critical_missing"], "errors": res["errors"],
                       "next_sources": [str(x) for x in obj.get("next_sources") or []],
                       "reeval_conditions": [str(x) for x in obj.get("reeval_conditions") or []]})
    except Exception as e:  # LLMError, HttpError, PromptError など。1候補の失敗で全体を止めない
        result["errors"] = [f"{type(e).__name__}: {redact(e)}"[:300]]
    return result


# ==================== pipeline ====================
"""処理フロー: 収集 → 正規化 → 重複排除 → 検証 → スクリーニング → 評価 → 保存。"""


DEFAULT_FETCHERS = {"pubmed": pubmed_fetch, "arxiv": arxiv_fetch}


def new_run_id(now=None):
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def screen(cand, as_of, cfg):
    """合格なら None、不合格なら理由。as_of より後の資料は事前評価に混入させない。"""
    sc = cfg["scoring"]["screening"]
    if not cand["published"]:
        return "no_published_date"
    end = parse_date(cand["published"], end=True)
    if end is None or end > as_of:
        return "published_after_as_of"
    if days_since(cand["published"], as_of) > sc["max_age_days"]:
        return "stale"
    if len(cand["abstract"] or "") < sc["min_abstract_chars"]:
        return "abstract_too_short"
    return None


def collect(store, cfg, run_id, as_of, sources, fetchers, counts, log):
    d = cfg["sources"]["defaults"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for q in cfg["sources"]["queries"]:
        for sname in sources:
            qs = q.get(sname)
            if not qs:
                continue
            t0, fetched, new, dups, invalid, err = time.monotonic(), 0, 0, 0, 0, None
            log(f"[collect:start] {sname}/{q['name']}")
            try:
                raws = fetchers[sname](qs, q.get("max_results", d["max_results"]),
                                       q.get("lookback_days", d["lookback_days"]), as_of)
                fetched = len(raws)
                for raw in raws:
                    rec, problems = normalize(raw, now)
                    if rec is None:
                        invalid += 1
                        continue
                    cid, is_new = store.upsert_candidate(rec, run_id)
                    entity_links = link_entities(rec, cfg.get("entities", {}))
                    store.set_entities(cid, entity_links)
                    new += is_new
                    dups += (not is_new)
            except Exception as e:
                err = redact(f"{type(e).__name__}: {e}")[:300]
                log(f"::warning::source {sname} / {q['name']} failed: {err}")
            store.add_run_source(run_id, sname, q["name"], fetched, new, dups, invalid, err is None, err,
                                 time.monotonic() - t0)
            for k, v in (("fetched", fetched), ("new", new), ("duplicates", dups), ("invalid", invalid)):
                counts[k] += v
            counts["sources_ok" if err is None else "sources_failed"] += 1
            log(f"[collect] {sname}/{q['name']}: fetched={fetched} new={new} dup={dups} invalid={invalid}"
                + (f" ERROR={err}" if err else ""))



def select_for_eval(store, cfg, today, limit, retry_failed="no"):
    sc = cfg["scoring"]["screening"]
    rows = []

    force_retry = retry_failed == "force"
    retry_enabled = (
        retry_failed is True
        or retry_failed == "yes"
        or force_retry
    )

    statuses = ["screened", "hold", "error"]

    for c in store.list_by_status(statuses):
        # エラー候補は、再試行が明示的に許可された場合だけ選ぶ
        if c["status"] == "error":
            if not retry_enabled:
                continue
            if not force_retry and c["retry_count"] >= sc["max_retries"]:
                continue

        # 保留候補は、再評価間隔が経過するまで選ばない
        if c["status"] == "hold":
            last = parse_date((c["last_eval_at"] or "")[:10])
            if last and (today - last).days < sc["reeval_after_days"]:
                continue

        rows.append(c)

    rows.sort(key=lambda c: c["published"] or "", reverse=True)
    return rows[:limit]

def _queue_reason(res):
    if res["status"] == "error":
        return "evaluation_error: " + "; ".join(res["errors"][:3])
    crit = ",".join(res["critical_missing"])
    return "missing_items: " + ",".join(res["missing"]) + (f" (critical: {crit})" if crit else "")


def run_pipeline(store, cfg, llm, *, as_of, sources=("pubmed", "arxiv"), kinds=("long",), max_eval=None,
                 retry_failed=False, do_collect=True, fetchers=None, today=None, log=print, prompt_dir=None):
    t0 = time.monotonic()
    today = today or datetime.now(timezone.utc).date()
    run_id = new_run_id()
    store.start_run(run_id, cfg["config_version"], cfg["config_hash"], as_of.isoformat(),
                {"sources": list(sources), "kinds": list(kinds), "max_eval": max_eval,
                 "retry_failed": retry_failed, "collect": do_collect,
                 "llm": getattr(llm, "name", "none")})
    counts = {k: 0 for k in ("fetched", "new", "duplicates", "invalid", "sources_ok", "sources_failed", "screened",
                             "rejected", "selected", "final", "hold", "error", "skipped_no_llm")}
    log(f"[run] {run_id} as_of={as_of} config={cfg['config_version']}/{cfg['config_hash']}")
    if do_collect:
        collect(store, cfg, run_id, as_of, sources, {**DEFAULT_FETCHERS, **(fetchers or {})}, counts, log)
    for c in store.list_by_status(["new"]):
        reason = screen(c, as_of, cfg)
        store.set_status(c["id"], "rejected" if reason else "screened", reason)
        counts["rejected" if reason else "screened"] += 1
    limit = max_eval if max_eval is not None else cfg["scoring"]["screening"]["max_eval_per_run"]
    selected = select_for_eval(
    store, cfg, today, limit, retry_failed=retry_failed
)
    counts["selected"] = len(selected)
    if selected and llm is None:
        counts["skipped_no_llm"] = len(selected)
        log("[eval] LLMプロバイダ未設定のため評価をスキップ(収集のみ)。TRL_LLM_PROVIDER を設定してください。")
        selected = []
    for c in selected:
        extraction, meta, outcomes = None, None, []
        for kind in kinds:
            eval_t0 = time.monotonic()
            log(f"[eval:start] {c['id']} {kind}")
            res = evaluate_one(c, kind, cfg, llm, as_of, extraction, meta, prompt_dir)
            eval_seconds = time.monotonic() - eval_t0
            log(f"[eval:time] {c['id']} {kind}: {eval_seconds:.1f}s")
            extraction, meta = res["extraction"], res["prompt_versions"].get("extract")
            store.add_evaluation(run_id, c["id"], res)
            counts[res["status"]] += 1
            outcomes.append(res["status"])
            if res["status"] == "final":
                store.resolve_queue(c["id"], kind)
            else:
                store.upsert_queue(c["id"], kind, _queue_reason(res), res["missing"] or res["errors"],
                                   res["next_sources"], res["reeval_conditions"] or ["追加情報の取得後、または再評価間隔の経過後"],
                                   datetime.now(timezone.utc).isoformat(timespec="seconds"), res["prompt_versions"])
            log(
    f"[eval] {c['id']} {kind}: "
    f"{res['status']} total={res['total']} "
    f"errors={res['errors']} "
    f"missing={res['missing']} "
    f"critical_missing={res['critical_missing']}"
)
        status = "hold" if "hold" in outcomes else ("error" if "error" in outcomes else "evaluated")
        store.set_status(c["id"], status, eval_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         retry_inc=(status == "error"))
    all_failed = do_collect and counts["sources_ok"] == 0 and counts["sources_failed"] > 0
    store.finish_run(run_id, counts, "failed" if all_failed else "ok", time.monotonic() - t0)
    return {"run_id": run_id, "counts": counts, "failed": all_failed}
    

# ==================== research links ====================
"""追加APIを使わず、企業・論文に関する外部検索リンクを生成する。"""


def build_research_links(title, company_name=None):
    """検索URLのみ生成。検索結果の取得や企業との関係の断定はしない。"""
    company = clean_text(company_name or "")
    query = company or clean_text(title or "")
    if not query:
        return []

    q = urllib.parse.quote_plus(query)
    ir_q = urllib.parse.quote_plus(f'"{query}" IR')
    patents_q = urllib.parse.quote_plus(query)

    label = "企業名" if company else "論文タイトル等（企業名未特定）"

    return [
        {
            "label": f"IR・決算資料 [{label}]",
            "url": f"https://www.google.com/search?q={ir_q}",
        },
        {
            "label": f"特許 [{label}]",
            "url": f"https://patents.google.com/?q={patents_q}",
        },
        {
            "label": f"臨床試験登録 [{label}]",
            "url": f"https://clinicaltrials.gov/search?term={q}",
        },
        {
            "label": f"企業・技術情報 [{label}]",
            "url": f"https://www.google.com/search?q={q}",
        },
    ]


def format_research_links(title, company_name=None):
    """Markdown用の検索リンク一覧を返す。"""
    links = build_research_links(title, company_name)
    return [
        f"  - [{item['label']}]({item['url']})"
        for item in links
    ]


# ==================== report ====================
"""Markdownレポート(スマホで読みやすい箇条書き中心)。"""

DISCLAIMER = ("※ スコアは評価尺度であり、株価上昇確率・成功確率・投資利益率ではありません。"
              "売買の自動判定にも使えません。")



def build_report(store, run_id):
    run = store.get_run(run_id)
    counts = json.loads(run["counts_json"] or "{}")
    L = [
        f"# TRL 実行レポート {run_id}", "",
        DISCLAIMER, "",
        f"- 基準日: {run['as_of']} / 設定: {run['config_version']} ({run['config_hash']})",
        f"- 状態: {run['status']} / 所要: {run['duration_sec']}秒",
        f"- 取得 {counts.get('fetched', 0)} / 新規 {counts.get('new', 0)} / 重複 {counts.get('duplicates', 0)} / 不正 {counts.get('invalid', 0)}",
        f"- スクリーニング通過 {counts.get('screened', 0)} / 除外 {counts.get('rejected', 0)} / 評価対象 {counts.get('selected', 0)}",
        f"- 評価: 確定 {counts.get('final', 0)} / 保留 {counts.get('hold', 0)} / エラー {counts.get('error', 0)}"
        f" / LLM未設定でスキップ {counts.get('skipped_no_llm', 0)}",
        "", "## 情報源ごとの状況"
    ]

    for s in store.run_sources(run_id):
        mark = "OK" if s["ok"] else "失敗"
        L.append(
            f"- [{mark}] {s['source']} / {s['query']}: "
            f"取得{s['fetched']} 新規{s['new']} 重複{s['duplicates']} 不正{s['invalid']}"
            + (f" — {s['error']}" if s["error"] else "")
        )

    for kind, title in (
        ("long", "長期成長評価(確定)"),
        ("event", "イベント評価(確定)")
    ):
        rows = [
            e for e in store.evaluations_for_run(run_id, kind)
            if e["status"] == "final"
        ]
        rows.sort(key=lambda e: e["total"], reverse=True)
        L += ["", f"## {title}"]

        if not rows:
            L.append("- 該当なし")

        for e in rows:
            L.append(f"- **{e['total']}** ({e['band']}) {e['title']}")
            L.append(f"  論文・原資料: {e['url']}")
            L.extend(format_research_links(e["title"]))

    
    q = store.list_queue(open_only=True)
    errors = [r for r in q if str(r["reason"]).startswith("evaluation_error:")]
    holds = [r for r in q if not str(r["reason"]).startswith("evaluation_error:")]

    L += ["", f"## レビューキュー（未解消 {len(q)}件）",
          f"- 情報不足・保留: {len(holds)}件",
          f"- 評価エラー: {len(errors)}件"]

    for section, rows in (("情報不足・保留", holds), ("評価エラー", errors)):
        L += ["", f"### {section}（{len(rows)}件）"]
        if not rows:
            L.append("- 該当なし")
            continue

        for r in rows:
            L.append(f"- [{r['kind']}] {r['title']}")
            L.append(f"  理由: {r['reason']}")
            if section == "情報不足・保留":
                L.append(f"  確認先: {', '.join(r['next_sources'] or ['未指定'])}")
                L.append(f"  再評価条件: {', '.join(r['reeval_conditions'] or [])}")
            else:
                L.append(f"  最終評価日時: {r['last_eval_at'] or '不明'}")
            L.append("  関連情報の検索:")
            L.extend(format_research_links(r["title"]))
    L += [
        "",
        "## 制約",
        "- IR・特許・臨床試験登録は検索リンクのみ生成し、検索結果の自動取得は行いません。",
        "- 企業名が未特定の場合は論文タイトル等を検索語に使います。",
        "- 市場予想が確認できない場合、イベント評価は保留になります。"
    ]
    return "\n".join(L) + "\n"


# ==================== cli ====================
def _prune(d, keep=30):
    files = sorted(p for p in d.glob("2*.md"))
    for p in files[:-keep]:
        p.unlink()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="trl")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--data-dir", default="data")
    r.add_argument("--as-of")
    r.add_argument("--sources", default="pubmed,arxiv")
    r.add_argument("--kinds", default="long")
    r.add_argument("--max-eval", type=int)
    r.add_argument(
    "--retry-failed",
    choices=["no", "yes", "force"],
    default="no",
    help="Retry previously failed evaluations",
)
    r.add_argument("--llm", default=os.environ.get("TRL_LLM_PROVIDER") or "none")
    r.add_argument("--model")
    r.add_argument("--skip-collect", action="store_true")
    q = sub.add_parser("queue")
    q.add_argument("--data-dir", default="data")
    sub.add_parser("check-config")
    a = ap.parse_args(argv)
    if a.cmd == "check-config":
        for n, ok in required_files():
            print(f"{'OK     ' if ok else 'MISSING'} {n}")
    gone = missing_files()
    if gone:
        print(f"::error::必須ファイルがありません: {', '.join(gone)}。リポジトリ直下に同名で作成してください(README参照)")
        return 2
    try:
        cfg = load_config()
    except (ConfigError, ValueError, KeyError) as e:
        print(f"::error::設定ファイルが不正です: {type(e).__name__}: {e}")
        return 2
    if a.cmd == "check-config":
        print(f"OK config={cfg['config_version']} hash={cfg['config_hash']}")
        return 0
    store = Store(Path(a.data_dir) / "trl.sqlite")
    if a.cmd == "queue":
        for x in store.list_queue():
            print(f"[{x['kind']}] {x['title']}\n  {x['reason']}")
        return 0
    as_of = parse_date(a.as_of) if a.as_of else datetime.now(timezone.utc).date()
    if as_of is None:
        print("invalid --as-of (YYYY-MM-DD)")
        return 2
    kinds = [k for k in a.kinds.split(",") if k]
    sources = [s for s in a.sources.split(",") if s]
    if not set(kinds) <= {"long", "event"} or not set(sources) <= {"pubmed", "arxiv"}:
        print("invalid --kinds or --sources")
        return 2
    try:
        llm = get_provider(a.llm, a.model)
    except LLMError as e:
        print(f"::error::{e}")
        return 2
    res = run_pipeline(
    store,
    cfg,
    llm,
    as_of=as_of,
    sources=sources,
    kinds=kinds,
    max_eval=a.max_eval,
    retry_failed=a.retry_failed,
    do_collect=not a.skip_collect,
)
    text = build_report(store, res["run_id"])
    rep = Path(a.data_dir) / "reports"
    rep.mkdir(parents=True, exist_ok=True)
    (rep / f"{res['run_id']}.md").write_text(text, encoding="utf-8")
    (rep / "latest.md").write_text(text, encoding="utf-8")
    _prune(rep)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)
    store.close()
    print(f"[done] {res['counts']}")
    return 1 if res["failed"] else 0




if __name__ == "__main__":
    sys.exit(main())
