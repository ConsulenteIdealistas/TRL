"""コマンドライン入口。"""

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path
from .errors import ConfigError, LLMError
from .dates import parse_date
from .config import SOURCE_NAMES, load_config, missing_files, required_files
from .store import Store
from .llm import Gemini, configure_llm, get_provider
from .pipeline import run_pipeline
from .site import export_site
from .report import build_report
from .audit import build_audit_report, run_audit_rules
from .audit_api import run_audit_api
from .compare import build_compare_report, run_compare


def _compare_clients(cfg):
    """モデル比較用の、フォールバックを持たない単一モデルクライアント群。
    本番と同じ環境変数 (TRL_LLM_MODEL / _FALLBACK_MODEL)
    から組み立てる。"""
    clients, seen = [], set()
    for var in ("TRL_LLM_MODEL", "TRL_LLM_FALLBACK_MODEL"):
        m = os.environ.get(var)
        if m and m not in seen:
            seen.add(m)
            # 代替に主モデル自身を渡すと _fallback_models が同名を除いて階段が
            # 空になる。未指定だと環境変数の階段を拾い、名札と違うモデルが
            # 応答して比較が汚染される (初回実行で実際に発生した)
            clients.append((f"gemini/{m}", configure_llm(
                Gemini(m, fallback_model=m), cfg)))
    if not clients:
        g = Gemini()
        g.fallback_models = []  # 既定モデルでも階段は持たせない (比較の前提)
        clients.append((f"gemini/{g.model}", configure_llm(g, cfg)))
    return clients


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
    r.add_argument("--depth", choices=["deep", "shallow"], default="deep",
                   help="収集の深さ。shallow は発見系ソースの取得窓を数日に切り詰める (定期実行の浅い日用)")
    r.add_argument(
    "--retry-failed",
    choices=["no", "yes", "force"],
    default="no",
    help="Retry previously failed evaluations",
)
    # 既定は gemini (現行の唯一のプロバイダ)。環境変数が未設定・空でも評価が
    # 音もなく止まらないようにする。収集のみにしたいときは明示的に --llm none。
    r.add_argument("--llm", default=os.environ.get("TRL_LLM_PROVIDER") or "gemini")
    r.add_argument("--model")
    r.add_argument("--skip-collect", action="store_true")
    r.add_argument("--only-new", action="store_true",
                   help="Evaluate only never-evaluated candidates (skip re-evaluations)")
    q = sub.add_parser("queue")
    q.add_argument("--data-dir", default="data")
    cp = sub.add_parser("compare")
    cp.add_argument("--data-dir", default="data")
    cp.add_argument("--limit", type=int, default=None,
                    help="比較対象の上限。既定は上限なし (全対象)。明示指定時のみ効く")
    cp.add_argument("--budget-sec", type=int, default=0,
                    help="全体の時間予算 (秒)。0 は無制限")
    cp.add_argument("--as-of")
    au = sub.add_parser("audit")
    au.add_argument("--data-dir", default="data")
    au.add_argument("--limit", type=int, default=100)
    au.add_argument("--budget-sec", type=int, default=9000,
                    help="互換のため残す引数 (規則エンジンでは未使用)")
    ex = sub.add_parser("export")
    ex.add_argument("--data-dir", default="data")
    ex.add_argument("--out", default="_site")
    vt = sub.add_parser("vault")
    vt.add_argument("--data-dir", default="data")
    vt.add_argument("--out", default="vault",
                    help="Markdown Vault の出力先 (DB は読み取り専用で開く)")
    ex.add_argument("--web", default=None, help="web/ のパス(既定: リポジトリ内の web)")
    sub.add_parser("check-config")
    sub.add_parser("queries")
    pr = sub.add_parser("prune")
    pr.add_argument("--data-dir", default="data")
    pr.add_argument("--as-of")
    pr.add_argument("--dry-run", action="store_true",
                    help="削除せず件数だけを数える (本番での初回観察用)")
    pr.add_argument("--paper-days", type=int, default=None,
                    help="N-2 (paper 保持期限) をこの日数で試す/適用する。"
                         "既定は無効 (段階投入は pipeline 側の定数が正)")
    pg = sub.add_parser("purge")
    pg.add_argument("--data-dir", default="data")
    pg.add_argument("--evaluations", action="store_true",
                    help="評価とレビューキューを全削除し、候補を未評価に戻す")
    rc_ = sub.add_parser("recalc")
    rc_.add_argument("--data-dir", default="data")
    rc_.add_argument("--apply", action="store_true",
                     help="実際に書き換える (既定は報告のみ)")
    a = ap.parse_args(argv)
    if a.cmd == "check-config":
        for n, ok in required_files():
            print(f"{'OK     ' if ok else 'MISSING'} {n}")
    if a.cmd == "purge":
        if not a.evaluations:
            print("::error::削除対象を指定してください(現状は --evaluations のみ対応)")
            return 2
        db = Path(a.data_dir) / "trl.sqlite"
        if not db.is_file():
            print(f"[purge] {db} が無いため、削除する評価はありません")
            return 0
        store = Store(db)
        counts = store.purge_evaluations()
        store.close()
        print(f"[purge] evaluations={counts['evaluations']} queue={counts['queue']} "
              f"candidates_reset={counts['candidates_reset']}")
        return 0
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
    if a.cmd == "queries":
        defaults = cfg["sources"]["defaults"]
        for q in cfg["sources"]["queries"]:
            sname = next(s for s in SOURCE_NAMES if q.get(s))
            lookback = q.get("lookback_days", defaults["lookback_days"])
            print(f"{q['name']} [{sname}] lookback={lookback}d: {q[sname]}")
        return 0
    if a.cmd == "recalc":
        db = Path(a.data_dir) / "trl.sqlite"
        if not db.is_file():
            print(f"::error::{db} がありません")
            return 2
        from .recalc import recalculate_evaluations
        store = Store(db)
        res = recalculate_evaluations(store, cfg, apply=a.apply)
        store.close()
        for d in res["details"]:
            print(f"[recalc] eval {d['id']} ({d['candidate_id']}): "
                  f"total {d['old_total']} -> {d['new_total']}, "
                  f"band {d['old_band']} -> {d['new_band']}, "
                  f"version {d['old_version']} -> {cfg['config_version']}")
        print(f"[recalc] checked={res['checked']} changed={res['changed']} "
              f"anomalies={res['anomalies']} applied={a.apply}")
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
        # data.json の複製を data-dir にも置く。ワークフローの Persist は
        # data-dir を丸ごと data ブランチへ保存するため、Pages 障害時でも
        # 同じデータがブランチ側から読める (可用性の冗長化)
        (Path(a.data_dir) / "data.json").write_bytes((Path(a.out) / "data.json").read_bytes())
        # DB の照合用サイドカー (trl.sqlite.sha256) は廃止した (RA-1):
        # export を含まないジョブ (audit / compare) が DB を更新するたびに
        # 1世代古くなり、正常運用でも照合が一致せず、本物の化けと区別が
        # つかなくなっていた。DB の健全性はワークフロー Restore 段の
        # PRAGMA integrity_check と、data ブランチのコミット履歴
        # (Git 側のオブジェクト検証) で担保する
        print(f"[export] mirror -> {Path(a.data_dir) / 'data.json'}")
        return 0
    if a.cmd == "vault":
        db = Path(a.data_dir) / "trl.sqlite"
        if not db.is_file():
            print(f"::error::{db} がありません。先に run を実行してください")
            return 2
        from .vault import export_vault
        res = export_vault(db, cfg, a.out)
        print(f"[vault] evaluations={res['evaluations']} companies={res['companies']} -> {a.out}")
        return 0
    if a.cmd == "compare":
        db = Path(a.data_dir) / "trl.sqlite"
        if not db.is_file():
            print(f"::error::{db} がありません。先に run を実行してください")
            return 2
        store = Store(db)
        as_of = parse_date(a.as_of) if a.as_of else datetime.now(timezone.utc).date()
        if as_of is None:
            print("invalid --as-of (YYYY-MM-DD)")
            store.close()
            return 2
        clients = _compare_clients(cfg)
        records = []
        for _label, client in clients:
            try:
                client.recorder = records.append
            except Exception:
                pass
        res = run_compare(store, cfg, clients, as_of=as_of, limit=a.limit,
                          budget_sec=a.budget_sec or None)
        while records:
            store.record_llm_call(res["batch"], records.pop(0))
        text = build_compare_report(store, res["batch"])
        rep = Path(a.data_dir) / "compare-reports"
        rep.mkdir(parents=True, exist_ok=True)
        (rep / f"{res['batch']}.md").write_text(text, encoding="utf-8")
        store.close()
        print(text)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write(text)
        return 0
    if a.cmd == "audit":
        db = Path(a.data_dir) / "trl.sqlite"
        if not db.is_file():
            print(f"::error::database not found: {db}")
            return 2
        store = Store(db)
        # 規則監査 (audit_rules.json の照合だけ。LLM クライアントは
        # 構築しない。LLM 監査は 2026-11-06 に廃止) の後に、API 照合層
        # (外部レジストリ照合) を続けて実行する。
        res = run_audit_rules(store, cfg, limit=a.limit)
        text = build_audit_report(store, res["batch"])
        rep = Path(a.data_dir) / "audit-reports"
        rep.mkdir(parents=True, exist_ok=True)
        (rep / f"{res['batch']}.md").write_text(text, encoding="utf-8")
        # API 照合層は外部 API に依存するため、失敗しても規則監査の
        # 結果とレポートは残す (例外はここで隔離する)。
        try:
            api_res = run_audit_api(store, cfg, limit=a.limit)
            api_text = build_audit_report(store, api_res["batch"])
            (rep / f"{api_res['batch']}.md").write_text(api_text, encoding="utf-8")
            text += "\n" + api_text
        except Exception as e:
            print(f"::warning::API 照合層を実行できませんでした"
                  f" (規則監査の結果は保存済み): {type(e).__name__}: {e}")
        store.close()
        print(text)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write(text)
        return 0
    store = Store(Path(a.data_dir) / "trl.sqlite")
    if a.cmd == "prune":
        as_of_p = parse_date(a.as_of) if getattr(a, "as_of", None) else datetime.now(timezone.utc).date()
        counts = store.prune(as_of_p, entity_markets={
            e["id"]: e.get("market") or "JP" for e in (cfg.get("entities") or {}).get("entities", [])},
            config_hash=cfg.get("config_hash"), paper_days=a.paper_days,
            dry_run=a.dry_run)
        store.close()
        print(f"[prune] {'(dry-run) ' if a.dry_run else ''}{counts}")
        return 0
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
    if not kinds or not set(kinds) <= {"long", "event"} or not sources or not set(sources) <= set(SOURCE_NAMES):
        print("invalid --kinds or --sources")
        return 2
    if a.max_eval is not None and a.max_eval < 0:
        print("invalid --max-eval (must be >= 0)")
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
    only_new=a.only_new,
    depth=a.depth,
)

    text = build_report(store, res["run_id"], cfg)

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
