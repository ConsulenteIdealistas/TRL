#!/usr/bin/env python3
"""trl.py を trlcore/ パッケージへ機械的に分割する(1回限りの移行ツール)。

使い方(リポジトリ直下で): python tools/split_trl.py
- trl.py の最上位定義を MODULES の表どおりに trlcore/*.py へ振り分ける(コードは1文字も書き換えない。ROOT の1行だけ例外)。
- 各モジュールの import は、使っている名前から自動で生成する(循環参照があれば停止)。
- trl.py は互換入口(ファサード)に置き換わる。`python trl.py ...` も `import trl as T` も従来どおり動く。
- 最後に、分割前の trl.py と分割後のファサードの名前空間が一致することを検証する。不一致なら非0で終了。
表に無い新しい定義が trl.py に増えていた場合は、名前を挙げて停止する(黙って落とさない)。
"""
import ast
import builtins
import importlib.util
import os
import re
import sys
from pathlib import Path

SRC = Path("trl.py")
OUT = Path("trlcore")

# モジュール名 -> (docstring, [最上位の名前...])。上から下へ向かう依存だけが許される。
MODULES = {
    "errors": ("例外クラス。", ["ConfigError", "HttpError", "LLMError", "PromptError"]),
    "dates": ("日付の解釈と鮮度計算。基準日(as_of)は必ず引数で受け取る(内部で today を使わない)。",
              ["_RE", "parse_date", "days_since", "freshness_info"]),
    "http": ("HTTP取得(リトライ・秘密情報のマスク付き)。標準ライブラリのみ。",
             ["SECRET_ENV", "_PARAM_RE", "redact", "_retry_after_seconds", "backoff_delay", "http_fetch"]),
    "normalize": ("正規化と基本検証。重複判定は安定ID(DOI/PMID/arXiv ID/URL)のみ。タイトル類似では統合しない。",
                  ["clean_text", "norm_doi", "norm_url", "FATAL", "normalize"]),
    "sources.pubmed": ("PubMed (NCBI E-utilities)。",
                       ["BASE", "MONTHS", "_month", "_date_from", "parse_efetch", "pubmed_fetch"]),
    "sources.arxiv": ("arXiv API (Atom)。", ["URL", "NS", "_last_call", "parse_feed", "arxiv_fetch"]),
    "scoring": ("採点の検証・総合点計算・スコア帯。nullを含む場合は総合点を確定せず hold にする(重み再配分はしない)。",
                ["item_keys", "validate_items", "band_for", "compute"]),
    "entities": ("企業・製品の定義検証と、論文との照合。",
                 ["_norm_text", "ENTITY_MODES", "validate_entities", "_contains", "link_entities", "entity_screen"]),
    "config": ("設定の読み込みと検証。設定ファイルのハッシュを実行ごとに記録する。",
               ["__version__", "ROOT", "CONFIG_FILES", "LLM_DEFAULTS", "validate_config", "validate_sources",
                "required_files", "missing_files", "load_config"]),
    "store": ("SQLite による永続化。重複排除は identifiers テーブルの安定IDで行う。",
              ["SCHEMA", "now_iso", "_j", "Store"]),
    "prompts": ("プロンプトの読み込みと差し込み。バージョンと内容ハッシュを評価結果に紐づける。",
                ["PROMPT_DIR", "_PH", "load_prompt", "render"]),
    "llm": ("LLMプロバイダ。既定は none(評価をせず収集のみ)。課金が発生し得るため明示設定が必要。",
            ["SYSTEM", "extract_json", "Anthropic", "Gemini", "Mock", "get_provider", "llm_settings",
             "configure_llm"]),
    "evaluate": ("AI応答の検証と評価の実行(根拠抽出 → 長期 / イベント)。",
                 ["EVIDENCE_KINDS", "EXTRACT_LISTS", "STATEMENT_LISTS", "validate_extraction", "_nonempty_list",
                  "validate_assessment", "parse_trl_level", "_candidate_block", "input_hash", "run_extraction",
                  "evaluate_one"]),
    "pipeline": ("処理フロー: 収集 → 正規化 → 重複排除 → 検証 → スクリーニング → 評価 → 保存。",
                 ["DEFAULT_FETCHERS", "new_run_id", "screen", "collect", "select_for_eval", "_queue_reason",
                  "run_pipeline"]),
    "site": ("trl.sqlite から静的サイト用の data.json を作る。Web(web/app.js)との契約は web/schema.json。",
             ["SITE_SCHEMA_VERSION", "TECH_ITEMS", "LEGACY_INFRA_QUEUE_PREFIX", "_ev_for_site", "display_status",
              "build_site_data", "export_site"]),
    "report": ("Markdownレポート(スマホで読みやすい箇条書き中心)と、外部検索リンクの生成。",
               ["build_research_links", "format_research_links", "DISCLAIMER", "build_report"]),
    "cli": ("コマンドライン入口。", ["_prune", "main"]),
}

MARKER = re.compile(r"#\s*=+")
SKIP_NS = {"__builtins__", "__file__", "__name__", "__doc__", "__spec__", "__loader__", "__package__", "__cached__"}


def fail(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def bound_names(node):
    """ノードが最上位で束縛する名前。"""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def free_names(node):
    """ノード内で読まれ、ノード内で束縛されていない名前(=モジュール外から必要な名前)。"""
    loads, binds = set(), set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            (loads if isinstance(n.ctx, ast.Load) else binds).add(n.id)
        elif isinstance(n, ast.arg):
            binds.add(n.arg)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                binds.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            binds.add(n.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not node:
            binds.add(n.name)
    return {x for x in loads - binds if not hasattr(builtins, x)}


def segment(lines, node):
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    i = start - 1
    while i > 0 and lines[i - 1].lstrip().startswith("#") and not MARKER.match(lines[i - 1].lstrip()):
        i -= 1
    return "\n".join(lines[i:node.end_lineno]).rstrip() + "\n"


def main():
    if OUT.exists():
        fail(f"{OUT}/ が既にあります。分割済みのため中止します")
    text = SRC.read_text(encoding="utf-8")
    lines = text.splitlines()
    tree = ast.parse(text)

    owner = {}
    for mod, (_, names) in MODULES.items():
        for n in names:
            if n in owner:
                fail(f"表で名前が重複しています: {n} ({owner[n]}, {mod})")
            owner[n] = mod

    std_imports, nodes, seen = [], {m: [] for m in MODULES}, set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            std_imports.append(node)
        elif isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant) \
                and isinstance(node.value.value, str):
            continue  # 区切り直下の説明文。各モジュールの docstring に置き換える
        elif isinstance(node, ast.If):
            continue  # if __name__ == "__main__": はファサードに置く
        else:
            names = bound_names(node)
            if not names:
                fail(f"想定外の最上位構文({type(node).__name__}, {node.lineno}行目)。tools/split_trl.py を更新してください")
            for n in names:
                if n not in owner:
                    fail(f"表に無い定義: {n} ({node.lineno}行目)。tools/split_trl.py の MODULES に追加してください")
                seen.add(n)
            nodes[owner[names[0]]].append(node)
    missing = set(owner) - seen
    if missing:
        fail(f"表にあるが trl.py に無い名前: {sorted(missing)}")

    # 標準ライブラリ import の名前 -> 文
    std_bound = {}
    for st in std_imports:
        for a in st.names:
            std_bound.setdefault((a.asname or a.name).split(".")[0], []).append((st, a))

    order = list(MODULES)
    deps, rendered = {}, {}
    for mod in order:
        need_std, need_mod, body = set(), {}, []
        for node in nodes[mod]:
            seg = segment(lines, node)
            if mod == "config" and seg.startswith("ROOT = Path(__file__).resolve().parent\n"):
                seg = "ROOT = Path(__file__).resolve().parent.parent  # リポジトリ直下(trlcore/ の1つ上)\n"
            elif mod == "config" and "ROOT = Path(__file__)" in seg:
                fail("ROOT の定義が想定と違います")
            body.append(seg)
            for n in free_names(node):
                if n in owner and owner[n] != mod:
                    need_mod.setdefault(owner[n], set()).add(n)
                elif n in std_bound:
                    need_std.add(n)
        deps[mod] = set(need_mod)
        head = ['"""' + MODULES[mod][0] + '"""', ""]
        emitted = set()
        for st in std_imports:  # 元の順序を保つ
            if isinstance(st, ast.Import):
                a_list = [a for a in st.names if (a.asname or a.name).split(".")[0] in need_std]
                for a in a_list:
                    line = f"import {a.name}" + (f" as {a.asname}" if a.asname else "")
                    if line not in emitted:
                        emitted.add(line)
                        head.append(line)
            else:
                a_list = [a for a in st.names if (a.asname or a.name) in need_std]
                if a_list:
                    names = ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in a_list)
                    head.append(f"from {st.module} import {names}")
        depth = mod.count(".") + 1
        for target in order:
            if target in need_mod:
                head.append(f"from {'.' * depth}{target} import {', '.join(sorted(need_mod[target]))}")
        rendered[mod] = "\n".join(head) + "\n\n\n" + "\n\n".join(body)

    # 循環参照の検出
    state = {}

    def visit(m, path):
        if state.get(m) == 1:
            fail("循環参照: " + " -> ".join(path + [m]))
        if state.get(m) == 2:
            return
        state[m] = 1
        for d in sorted(deps[m]):
            visit(d, path + [m])
        state[m] = 2

    for m in order:
        visit(m, [])

    OUT.mkdir()
    (OUT / "__init__.py").write_text('"""TRL投資分析システムの実装。入口は repo 直下の trl.py(互換ファサード)。"""\n', encoding="utf-8")
    for mod, src in rendered.items():
        p = OUT / (mod.replace(".", "/") + ".py")
        p.parent.mkdir(parents=True, exist_ok=True)
        if mod.count("."):
            (p.parent / "__init__.py").touch()
        p.write_text(src.rstrip() + "\n", encoding="utf-8")

    # ファサード: 元の import 行をそのまま残し(テストが T.time などを参照する)、全名前を再公開する
    facade = ['"""TRL投資分析システム 互換入口。実装は trlcore/ にある。',
              '`python trl.py ...` と `import trl as T` の両方を従来どおり使えるように、全ての名前を再公開する。"""']
    facade += [ast.get_source_segment(text, st) + "  # noqa: F401" for st in std_imports]
    for mod in order:
        facade.append(f"from trlcore.{mod} import {', '.join(MODULES[mod][1])}  # noqa: F401,E402")
    facade += ["", "", 'if __name__ == "__main__":', "    sys.exit(main())", ""]
    original_ns = {"__name__": "trl_orig", "__file__": str(SRC.resolve())}
    exec(compile(text, str(SRC), "exec"), original_ns)
    SRC.write_text("\n".join(facade), encoding="utf-8")

    # 検証: 分割前後で名前空間と ROOT が一致すること
    sys.path.insert(0, os.getcwd())
    spec = importlib.util.spec_from_file_location("trl_facade", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    a = {k for k in original_ns if k not in SKIP_NS}
    b = {k for k in vars(mod) if k not in SKIP_NS}
    if a != b:
        fail(f"名前空間が不一致: 欠落={sorted(a - b)} 余分={sorted(b - a)}")
    if original_ns["ROOT"] != mod.ROOT or original_ns["PROMPT_DIR"] != mod.PROMPT_DIR:
        fail("ROOT / PROMPT_DIR が変わっています")
    for k in sorted(a):
        if callable(original_ns[k]) and getattr(original_ns[k], "__module__", None) == "trl_orig" \
                and not hasattr(getattr(mod, k), "__call__"):
            fail(f"{k} が呼び出せません")
    print(f"OK: {len(MODULES)} モジュールに分割。名前 {len(a)} 個の一致を確認")
    for m in order:
        print(f"  trlcore/{m.replace('.', '/')}.py  <- {len(MODULES[m][1])} 個 / 依存: {', '.join(sorted(deps[m])) or '-'}")


if __name__ == "__main__":
    main()
