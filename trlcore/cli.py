"""コマンドライン入口。"""

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path
from .errors import ConfigError, LLMError
from .dates import parse_date
from .config import SOURCE_NAMES, load_config, missing_files, required_files
from .store import Store
from .llm import configure_llm, get_provider
from .pipeline import run_pipeline
from .site import export_site
from .report import build_report


def _prune(d, keep=30):
    for suffix in (".md", ".html"):  # .html は旧版が作った分を古い順に整理するために残す
        files = sorted(
            p for p in d.glob(f"2*{suffix}") if p.is_file()
        )
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
    ex = sub.add_parser("export")
    ex.add_argument("--data-dir", default="data")
    ex.add_argument("--out", default="_site")
    ex.add_argument("--web", default=None, help="web/ のパス(既定: リポジトリ内の web)")
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
    if a.cmd == "export":
        db = Path(a.data_dir) / "trl.sqlite"
        if not db.is_file():
            print(f"::error::{db} がありません。先に run を実行してください")
            return 2
        store = Store(db)
        data = export_site(store, cfg, a.out, a.web)
        store.close()
        print(f"[export] {len(data['candidates'])}件 -> {Path(a.out) / 'data.json'}")
        return 0
    store = Store(Path(a.data_dir) / "trl.sqlite")
    if a.cmd == "queue":
        for x in store.list_queue():
            print(f"[{x['kind']}] {x['title']}\n  {x['reason']}")
        store.close()
        return 0
    as_of = parse_date(a.as_of) if a.as_of else datetime.now(timezone.utc).date()
    if as_of is None:
        print("invalid --as-of (YYYY-MM-DD)")
        return 2
    kinds = [k for k in a.kinds.split(",") if k]
    sources = [s for s in a.sources.split(",") if s]
    if not set(kinds) <= {"long", "event"} or not set(sources) <= set(SOURCE_NAMES):
        print("invalid --kinds or --sources")
        return 2
    try:
        llm = configure_llm(get_provider(a.llm, a.model), cfg)
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
