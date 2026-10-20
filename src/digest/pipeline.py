"""収集→名寄せ→選定→入力生成→（任意で自動評価）→公開、および Issue 取り込み。"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import yaml

from .collectors import ArXivCollector, NormalizedItem, PubMedCollector
from .evaluator import evaluate
from .sanitize import sanitize_markdown_html

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DOCS_DIR = ROOT / "docs"
DIGESTS_DIR = DOCS_DIR / "digests"
PROMPTS_DIR = ROOT / "prompts"

DISCLAIMER = (
    "> 本ダイジェストは公開情報の整理であり、投資助言ではありません。"
    "投資判断は自己責任で行ってください。\n\n"
)

log = logging.getLogger("pipeline")


def _load_yaml(name: str) -> dict:
    with (CONFIG_DIR / name).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------- 名寄せ ----------
_DOI_PREFIX = re.compile(r"^(https?://(dx\.)?doi\.org/|doi:)", re.IGNORECASE)


def _norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    return _DOI_PREFIX.sub("", doi).strip().lower()


def dedupe(items: list[NormalizedItem]) -> list[NormalizedItem]:
    kept: list[NormalizedItem] = []
    seen_doi: set[str] = set()
    for it in items:
        doi = _norm_doi(it.doi)
        if doi and doi in seen_doi:
            continue
        if any(
            difflib.SequenceMatcher(None, it.title, k.title).ratio() >= 0.92
            for k in kept
        ):
            continue
        if doi:
            seen_doi.add(doi)
        kept.append(it)
    return kept


# ---------- スコアリング ----------
def _relevance(it: NormalizedItem, cfg: dict) -> float:
    kws = cfg["domains"].get(it.domain, {}).get("keywords", [])
    text = (it.title + " " + (it.abstract or "")).lower()
    hits = sum(1 for k in kws if k.lower() in text)
    return min(1.0, hits / max(1, len(kws) // 3))


def _freshness(it: NormalizedItem, cfg: dict, today: date) -> float:
    if not it.published_at:
        return 0.3
    days = (today - it.published_at).days
    max_days = int(cfg["freshness"].get("max_days", 7))
    if days < 0:
        return 0.5
    if days > max_days:
        return 0.0
    return 1.0 - (days / max_days)


def score_and_select(items: list[NormalizedItem], cfg: dict) -> list[NormalizedItem]:
    today = date.today()
    w = cfg["weights"]
    for it in items:
        rel = _relevance(it, cfg)
        fresh = _freshness(it, cfg, today)
        nov = 1.0 if it.raw_type in ("paper", "trial", "patent") else 0.5
        it.score = (
            w["relevance"] * rel
            + w["freshness"] * fresh
            + w["novelty"] * nov
            + w["diversity"] * 1.0
        )

    selected: list[NormalizedItem] = []
    for domain, dc in cfg["domains"].items():
        pool = sorted(
            (x for x in items if x.domain == domain),
            key=lambda x: x.score, reverse=True,
        )
        selected.extend(pool[: int(dc.get("top_n", 0))])
    total = int(cfg.get("selection", {}).get("total_top_n", 5))
    selected.sort(key=lambda x: x.score, reverse=True)
    return selected[:total]


# ---------- プロンプト生成 ----------
def build_input(items: list[NormalizedItem], today_iso: str) -> str:
    prompt = (PROMPTS_DIR / "eval_v1.md").read_text(encoding="utf-8")
    rows = []
    for i, it in enumerate(items, start=1):
        rows.append(
            f"### 項目 {i}\n"
            f"- ID: {it.id}\n"
            f"- 分野: {it.domain}\n"
            f"- ソース: {it.source}\n"
            f"- タイトル: {it.title}\n"
            f"- 公開日: {it.published_at}\n"
            f"- DOI: {it.doi or '-'}\n"
            f"- URL: {it.url}\n"
            f"- 要旨（公開分のみ）: {(it.abstract or '')[:1500]}\n"
        )
    body = prompt.replace("ITEMS_PLACEHOLDER", "\n".join(rows))
    return f"<!-- generated_for: {today_iso} -->\n\n" + body


# ---------- 公開 ----------
def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_metrics(payload: dict) -> Path:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {**payload, "generated_at": datetime.now(timezone.utc).isoformat()}
    p = DOCS_DIR / "metrics.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ---------- 日次実行 ----------
COLLECTORS = {
    "pubmed": PubMedCollector,
    "arxiv": ArXivCollector,
}


def run_daily(today: date | None = None) -> None:
    today = today or date.today()
    sources_cfg = _load_yaml("sources.yaml")["sources"]
    scoring_cfg = _load_yaml("scoring.yaml")
    evaluator_cfg = _load_yaml("evaluator.yaml")

    items: list[NormalizedItem] = []
    status: dict[str, str] = {}
    for name, cls in COLLECTORS.items():
        sc = sources_cfg.get(name)
        if not sc or not sc.get("enabled"):
            status[name] = "disabled"
            continue
        try:
            got = cls(sc.get("params", {})).collect()
            items.extend(got)
            status[name] = f"ok:{len(got)}"
        except Exception as e:  # 部分失敗許容
            status[name] = f"failed:{type(e).__name__}"
            log.warning("collector failed name=%s error=%s", name, e)

    if not items:
        p = _write(
            DIGESTS_DIR / f"{today.isoformat()}.md",
            DISCLAIMER + "## 本日は収集できませんでした\n\n全ソース収集不能。\n",
        )
        write_metrics({
            "today": today.isoformat(),
            "sources": status,
            "total_items": 0,
            "evaluated": False,
        })
        log.info("stub generated path=%s", p)
        return

    items = dedupe(items)
    selected = score_and_select(items, scoring_cfg)
    content = build_input(selected, today.isoformat())
    _write(DIGESTS_DIR / f"{today.isoformat()}-input.md", DISCLAIMER + content)

    # 自動評価（provider に応じて）
    ai_output = evaluate(content, evaluator_cfg)
    evaluated = bool(ai_output)
    if evaluated:
        _write(DIGESTS_DIR / f"{today.isoformat()}.md", DISCLAIMER + ai_output)
        log.info("auto evaluation ok date=%s", today.isoformat())
    elif evaluator_cfg.get("fallback_to_manual", True):
        log.info("auto evaluation skipped; manual fallback via Issue")
    else:
        log.warning("auto evaluation failed and fallback disabled")

    write_metrics({
        "today": today.isoformat(),
        "sources": status,
        "total_items": len(items),
        "selected": len(selected),
        "evaluated": evaluated,
    })
    log.info("daily ok selected=%d evaluated=%s", len(selected), evaluated)


# ---------- Issue 取り込み ----------
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def ingest_issue(issue_number: int) -> Path:
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    r = httpx.get(
        f"https://api.github.com/repos/{repo}/issues/{issue_number}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=20,
    )
    r.raise_for_status()
    issue = r.json()
    m = _DATE_RE.search(issue.get("title", ""))
    if not m:
        raise ValueError("Issue タイトルに YYYY-MM-DD を含めてください")
    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    body = sanitize_markdown_html(issue.get("body") or "")
    p = _write(DIGESTS_DIR / f"{d.isoformat()}.md", DISCLAIMER + body)
    log.info("ingested issue=%d date=%s", issue_number, d.isoformat())
    return p