"""設定の読み込みと検証。設定ファイルのハッシュを実行ごとに記録する。"""

import hashlib
import json
from pathlib import Path
from .errors import ConfigError
from .entities import ENTITY_MODES, validate_entities


__version__ = "0.1.0"

SOURCE_NAMES = ("pubmed", "arxiv", "gnews", "prtimes", "trials", "edinet", "yahoo")


ROOT = Path(__file__).resolve().parent.parent  # リポジトリ直下(trlcore/ の1つ上)


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
    v = sc.get("screening", {}).get("eval_queue_per_entity", 10)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ConfigError("scoring.json: screening.eval_queue_per_entity must be a non-negative integer")
    for k in ("extract", "long", "event"):
        if not isinstance(sc.get("prompts", {}).get(k), str) or not sc["prompts"][k]:
            raise ConfigError(f"scoring.json: prompts.{k} (version string) is required")
    for k, v in (sc.get("llm") or {}).items():
        if k not in LLM_DEFAULTS:
            raise ConfigError(f"scoring.json: unknown llm.{k}")
        if isinstance(v, bool) or not isinstance(v, int) or v < (0 if k == "retries" else 1):
            raise ConfigError(f"scoring.json: llm.{k} must be an integer ({'>=0' if k == 'retries' else '>=1'})")
    for k, v in (sc.get("screening", {}).get("min_abstract_chars_by_info_type") or {}).items():
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ConfigError(f"scoring.json: screening.min_abstract_chars_by_info_type.{k} must be a non-negative integer")
    mode = sc.get("screening", {}).get("require_entity", "off")
    if mode not in ENTITY_MODES:
        raise ConfigError("scoring.json: screening.require_entity must be off/strong/any")
    if cfg.get("entities") is not None:
        validate_entities(cfg["entities"])
    elif mode != "off":
        raise ConfigError("scoring.json: screening.require_entity needs entities.json")
    validate_sources(cfg["sources"])
    src = cfg["sources"]
    if cfg.get("entities") is not None and ("topics" in src or "keyword_sets" in src):
        generated = build_entity_queries(cfg["entities"], src)  # sector/news_name の不整合はここで落ちる
        topic_names = {q["name"] for q in src.get("topics") or []}
        for q in generated:
            if q["name"] in topic_names:
                raise ConfigError(f"sources.json: generated query name collides with a topic: {q['name']}")


def _validate_query_list(qs, label):
    if not isinstance(qs, list):
        raise ConfigError(f"sources.json: {label} must be a list")
    names = set()
    for i, q in enumerate(qs):
        if not isinstance(q, dict) or not isinstance(q.get("name"), str) or not q["name"].strip():
            raise ConfigError(f"sources.json: {label}[{i}].name is required")
        if q["name"] in names:
            raise ConfigError(f"sources.json: duplicate query name: {q['name']}")
        names.add(q["name"])
        if not any(isinstance(q.get(s), str) and q[s].strip()
                   or (s == "gnews" and isinstance(q.get(s), dict) and str(q[s].get("q") or "").strip())
                   for s in SOURCE_NAMES):
            raise ConfigError(f"sources.json: {label}[{i}] needs a non-empty query string for one of {SOURCE_NAMES}")
        for k in ("max_results", "lookback_days"):
            if k in q and (isinstance(q[k], bool) or not isinstance(q[k], int) or q[k] <= 0):
                raise ConfigError(f"sources.json: {label}[{i}].{k} must be a positive integer")
    return names


def validate_sources(src):
    """sources.json のスキーマ検証。
    {"defaults": {"max_results": int>0, "lookback_days": int>0},
     "keyword_sets": {sector: [str, ...]},
     "entity_watch": {"lookback_days": {"gnews"/"prtimes"/"trials"/"edinet"/"yahoo": int>0}},
     "topics": [クエリ, ...]}
    企業クエリは entities.json の watch から生成する(build_entity_queries)。
    "queries" は生成後に load_config が付ける実効リストで、あれば同じ形で検証する。"""
    d = src.get("defaults")
    if not isinstance(d, dict):
        raise ConfigError("sources.json: defaults must be an object")
    for k in ("max_results", "lookback_days"):
        if isinstance(d.get(k), bool) or not isinstance(d.get(k), int) or d[k] <= 0:
            raise ConfigError(f"sources.json: defaults.{k} must be a positive integer")
    ks = src.get("keyword_sets")
    if ks is not None:
        if not isinstance(ks, dict) or not ks:
            raise ConfigError("sources.json: keyword_sets must be a non-empty object")
        for sector, words in ks.items():
            if not isinstance(words, list) or not words or any(not isinstance(w, str) or not w.strip() for w in words):
                raise ConfigError(f"sources.json: keyword_sets.{sector} must be a non-empty list of strings")
    ew = src.get("entity_watch")
    if ew is not None:
        if not isinstance(ew, dict) or not isinstance(ew.get("lookback_days"), dict):
            raise ConfigError("sources.json: entity_watch.lookback_days must be an object")
        for sname, v in ew["lookback_days"].items():
            if sname not in SOURCE_NAMES or isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ConfigError(f"sources.json: entity_watch.lookback_days.{sname} must be a positive integer")
    if "topics" not in src and "queries" not in src:
        raise ConfigError("sources.json: topics is required")
    if "topics" in src:
        _validate_query_list(src["topics"], "topics")
    if "queries" in src:
        if not isinstance(src["queries"], list) or not src["queries"]:
            raise ConfigError("sources.json: queries must be a non-empty list")
        _validate_query_list(src["queries"], "queries")


WATCH_LOOKBACK_DEFAULTS = {"prtimes": 90, "trials": 365, "edinet": 60, "yahoo": 400}


# 市場ごとの Yahoo シンボル接尾辞と Google News ロケール。market 未指定は JP。
YAHOO_SUFFIX = {"JP": ".T", "US": "", "GB": ".L", "CH": ".SW", "DE": ".DE", "CA": ".TO", "AU": ".AX",
                "IT": ".MI", "SE": ".ST", "DK": ".CO"}
GNEWS_LOCALE = {"US": {"hl": "en", "gl": "US", "ceid": "US:en"},
                "GB": {"hl": "en", "gl": "GB", "ceid": "GB:en"},
                "CH": {"hl": "en", "gl": "CH", "ceid": "CH:en"},
                "DE": {"hl": "de", "gl": "DE", "ceid": "DE:de"},
                "CA": {"hl": "en", "gl": "CA", "ceid": "CA:en"},
                "AU": {"hl": "en", "gl": "AU", "ceid": "AU:en"},
                "IT": {"hl": "it", "gl": "IT", "ceid": "IT:it"},
                "SE": {"hl": "sv", "gl": "SE", "ceid": "SE:sv"},
                "DK": {"hl": "en", "gl": "DK", "ceid": "DK:en"}}
# 市場ごとのキーワード集合の置き場所 (JP は日本語、それ以外は市場言語。未指定の市場は英語)
# デンマークは Google News のデンマーク語圏の収載が薄いため英語集合を使う (ロケールは DK)
KEYWORD_SETS_BY_MARKET = {"JP": "keyword_sets", "DE": "keyword_sets_de",
                          "IT": "keyword_sets_it", "SE": "keyword_sets_sv"}


def build_entity_queries(entities, src):
    """entities.json の watch から企業クエリを生成する。
    生成順は gnews(全社) → trials → edinet → yahoo → prtimes で、各系統内は entities の並び順。
    watch の無い企業は監視対象にしない。sponsor が無ければ trials、ticker が無ければ yahoo を生成しない。
    market が JP 以外の企業は海外扱い: EDINET と PR TIMES は生成せず、gnews は市場のロケールと
    英語キーワード集合(keyword_sets_en)を使い、yahoo は市場の接尾辞つきシンボルにする。"""
    ks = src.get("keyword_sets") or {}
    params = (src.get("entity_watch") or {}).get("lookback_days") or {}
    groups = {"gnews": [], "trials": [], "edinet": [], "yahoo": [], "prtimes": []}
    for e in (entities or {}).get("entities", []):
        w = e.get("watch")
        if not isinstance(w, dict):
            continue
        eid = e.get("id")
        news_name = w.get("news_name")
        if not isinstance(news_name, str) or not news_name.strip():
            raise ConfigError(f"entities.json: {eid}.watch.news_name is required")
        sector = e.get("sector")
        market = e.get("market") or "JP"
        if market == "JP":
            if sector not in ks:
                raise ConfigError(f"entities.json: {eid}.sector {sector!r} has no keyword_sets entry in sources.json")
            words = list(ks[sector]) + list(w.get("extra_keywords") or [])
            groups["gnews"].append({"name": f"{eid}-news", "gnews": f"{news_name} ({' OR '.join(words)})"})
        else:
            set_key = KEYWORD_SETS_BY_MARKET.get(market, "keyword_sets_en")
            sets = src.get(set_key) or {}
            if sector not in sets:
                raise ConfigError(f"entities.json: {eid}.sector {sector!r} has no {set_key} entry in sources.json")
            words = list(sets[sector]) + list(w.get("extra_keywords") or [])
            q = {"q": f"{news_name} ({' OR '.join(words)})"}
            q.update(GNEWS_LOCALE[market])
            groups["gnews"].append({"name": f"{eid}-news", "gnews": q})
        if w.get("sponsor"):
            groups["trials"].append({"name": f"{eid}-trials", "trials": w["sponsor"],
                                     "lookback_days": params.get("trials", WATCH_LOOKBACK_DEFAULTS["trials"])})
        if e.get("ticker"):
            if market == "JP":
                groups["edinet"].append({"name": f"{eid}-edinet", "edinet": e["ticker"],
                                         "lookback_days": params.get("edinet", WATCH_LOOKBACK_DEFAULTS["edinet"])})
            groups["yahoo"].append({"name": f"{eid}-yahoo", "yahoo": e["ticker"] + YAHOO_SUFFIX[market],
                                    "lookback_days": params.get("yahoo", WATCH_LOOKBACK_DEFAULTS["yahoo"])})
        if market == "JP":
            terms = w.get("prtimes_terms") or [news_name]
            groups["prtimes"].append({"name": f"{eid}-prtimes", "prtimes": "|".join(terms),
                                      "lookback_days": params.get("prtimes", WATCH_LOOKBACK_DEFAULTS["prtimes"])})
    return groups["gnews"] + groups["trials"] + groups["edinet"] + groups["yahoo"] + groups["prtimes"]


CONFIG_FILES = ("scoring.json", "sources.json")

# entities/ ディレクトリ内の市場別ファイルの合体順 (辞書の並び = クエリ生成順を決める)
ENTITY_FILE_ORDER = ("jp", "us", "gb", "ch", "de", "ca", "au", "it", "se", "dk")


LLM_DEFAULTS = {"timeout_sec": 120, "retries": 3, "max_consecutive_failures": 2, "phase_budget_sec": 1200,
                "eval_workers": 1}


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

    # 企業辞書は市場別ファイル entities/<market>.json に分けて置き、合体して読む。
    # 合体順は ENTITY_FILE_ORDER (新しい市場は末尾に追加)。旧形式の entities.json も読める。
    entities = None
    ent_dir = d / "entities"
    if ent_dir.is_dir():
        files = sorted(ent_dir.glob("*.json"),
                       key=lambda p: (ENTITY_FILE_ORDER.index(p.stem) if p.stem in ENTITY_FILE_ORDER
                                      else len(ENTITY_FILE_ORDER), p.stem))
        merged = []
        for p in files:
            text = p.read_text(encoding="utf-8-sig")
            h.update(text.encode("utf-8"))
            merged.extend((json.loads(text) or {}).get("entities") or [])
        entities = {"entities": merged}
    elif (d / "entities.json").is_file():
        entities_text = (d / "entities.json").read_text(encoding="utf-8-sig")
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
    if entities is not None:
        cfg["sources"]["queries"] = list(cfg["sources"].get("topics") or []) + build_entity_queries(entities, cfg["sources"])
    return cfg
