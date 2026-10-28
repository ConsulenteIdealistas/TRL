"""企業・製品の定義検証と、論文との照合。"""

import re
import unicodedata
from .errors import ConfigError


def _norm_text(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


ENTITY_MODES = ("off", "strong", "any")

# 上場市場。未指定は JP とみなす。EDINET / PR TIMES は JP のみ、Yahoo シンボルと
# Google News のロケールは市場で切り替える (trlcore/config.py の生成規則)。
MARKETS = ("JP", "US", "GB", "CH", "DE", "CA", "AU", "IT", "SE", "DK", "ES", "FR", "NL", "FI", "NO", "BE")

# 監視の層。core は評価予算を使う対象、watch は収集とプール専用 (評価しない)。
# 評価選定はコア企業のリンクがある候補だけを対象にする (trlcore/pipeline.py)。
TIERS = ("core", "watch")


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

        sector = entity.get("sector")
        if sector is not None and (not isinstance(sector, str) or not sector.strip()):
            raise ConfigError(f"entities[{i}].sector must be a non-empty string")
        market = entity.get("market")
        if market is not None and market not in MARKETS:
            raise ConfigError(f"entities[{i}].market must be one of {MARKETS}")
        tier = entity.get("tier")
        if tier is not None and tier not in TIERS:
            raise ConfigError(f"entities[{i}].tier must be one of {TIERS}")
        watch = entity.get("watch")
        if watch is not None:
            if not isinstance(watch, dict):
                raise ConfigError(f"entities[{i}].watch must be an object")
            news_name = watch.get("news_name")
            if not isinstance(news_name, str) or not news_name.strip():
                raise ConfigError(f"entities[{i}].watch.news_name is required")
            for k in ("prtimes_terms", "extra_keywords"):
                v = watch.get(k)
                if v is not None and (not isinstance(v, list) or any(not isinstance(x, str) or not x.strip() for x in v)):
                    raise ConfigError(f"entities[{i}].watch.{k} must be a list of strings")
            sponsor = watch.get("sponsor")
            if sponsor is not None and (not isinstance(sponsor, str) or not sponsor.strip()):
                raise ConfigError(f"entities[{i}].watch.sponsor must be a non-empty string")

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


_PREPARED_CACHE = {}


def _alias_matcher(alias):
    """alias を正規化し、英数字を含む場合は単語境界つきパターンを compile して返す。"""
    norm = _norm_text(alias)
    pattern = None
    if norm and re.search(r"[a-z0-9]", norm):
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(norm) + r"(?![a-z0-9])")
    return norm, pattern


def _match_prepared(norm, pattern, norm_text):
    if not norm or not norm_text:
        return False
    if pattern is not None:
        return pattern.search(norm_text) is not None
    return norm in norm_text


def _prepared_entities(data):
    """entities の alias 正規化とパターン compile を設定ごとに使い回す。

    link_entities は収集した全レコードに対して呼ばれるため、alias 側の準備を
    レコードごとに繰り返さない。キーは entities の内容そのもの。
    """
    key = tuple(
        (e.get("id"), e.get("name"), tuple(e.get("aliases", []) or []),
         tuple((p.get("id"), p.get("name"), tuple(p.get("aliases", []) or []))
               for p in (e.get("products", []) or [])))
        for e in (data.get("entities", []) or []))
    prep = _PREPARED_CACHE.get(key)
    if prep is None:
        prep = []
        for entity in data.get("entities", []) or []:
            names = [entity.get("name", "")] + list(entity.get("aliases", []) or [])
            products = []
            for product in entity.get("products", []) or []:
                pnames = [product.get("name", "")] + list(product.get("aliases", []) or [])
                products.append((product, [(a, *_alias_matcher(a)) for a in pnames if a]))
            prep.append((entity, [(a, *_alias_matcher(a)) for a in names if a], products))
        _PREPARED_CACHE.clear()
        _PREPARED_CACHE[key] = prep
    return prep


def link_entities(rec, data):
    """論文と企業・製品を照合する。所属機関一致はstrong、本文一致はweak。"""
    if not isinstance(data, dict):
        return []

    title = rec.get("title", "")
    abstract = rec.get("abstract", "")
    affiliations = rec.get("affiliations", []) or []

    if isinstance(affiliations, str):
        affiliations = [affiliations]

    # テキストの正規化は値ごとに1回だけ (alias ごとに繰り返すと収集が遅くなる)
    fields = [
        ("affiliation", [_norm_text(v) for v in affiliations]),
        ("title", [_norm_text(title)]),
        ("abstract", [_norm_text(abstract)]),
    ]

    results = []

    for entity, names, products in _prepared_entities(data):
        match = None

        for field, values in fields:
            for value in values:
                for alias, norm, pattern in names:
                    if _match_prepared(norm, pattern, value):
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

        for product, product_names in products:
            for field, values in fields:
                matched = False
                for value in values:
                    for alias, norm, pattern in product_names:
                        if _match_prepared(norm, pattern, value):
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
