"""設定の読み込みと検証。設定ファイルのハッシュを実行ごとに記録する。"""

import hashlib
import json
from pathlib import Path
from .errors import ConfigError
from .entities import ENTITY_MODES, validate_entities


__version__ = "0.1.0"


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
    for k in ("extract", "long", "event"):
        if not isinstance(sc.get("prompts", {}).get(k), str) or not sc["prompts"][k]:
            raise ConfigError(f"scoring.json: prompts.{k} (version string) is required")
    for k, v in (sc.get("llm") or {}).items():
        if k not in LLM_DEFAULTS:
            raise ConfigError(f"scoring.json: unknown llm.{k}")
        if isinstance(v, bool) or not isinstance(v, int) or v < (0 if k == "retries" else 1):
            raise ConfigError(f"scoring.json: llm.{k} must be an integer ({'>=0' if k == 'retries' else '>=1'})")
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


LLM_DEFAULTS = {"timeout_sec": 120, "retries": 3, "max_consecutive_failures": 2, "phase_budget_sec": 1200}


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
