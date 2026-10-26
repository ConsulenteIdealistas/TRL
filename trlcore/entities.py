"""企業・製品の定義検証と、論文との照合。"""

import re
import unicodedata
from .errors import ConfigError


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

        sector = entity.get("sector")
        if sector is not None and (not isinstance(sector, str) or not sector.strip()):
            raise ConfigError(f"entities[{i}].sector must be a non-empty string")
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
