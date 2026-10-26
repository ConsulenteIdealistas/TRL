"""Markdownレポート(スマホで読みやすい箇条書き中心)と、外部検索リンクの生成。"""

import json
import urllib.error
import urllib.parse
import urllib.request
from .normalize import clean_text


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
    ]
    if counts.get("llm_infra_failed") or counts.get("llm_degraded"):
        L += [
            f"- ⚠ LLM API失敗 {counts.get('llm_infra_failed', 0)}件(評価結果として保存せず未評価のまま残しています)"
            + (f" / 打ち切り理由: {counts['llm_degraded']}" if counts.get("llm_degraded") else "")
        ]
    L += [
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
