"""Markdown Vault への書き出し (Obsidian 等で読むための導出層)。

要件 (2026-11-04 泰崇):
- 元データ (DB) は変更しない: DB は読み取り専用 (mode=ro) で開く。
- 書き込みは管理フォルダ (companies/ evals/ sectors/ tags/ と README.md) だけ。
  それ以外のファイル (利用者のメモ置き場 notes/ 等) には触れない・消さない。
- 原文と日本語訳は混ぜず、別セクションにする。
- 出典URL・取得日時・評価基準のバージョンを各ノートに残す。
- 未取得の情報は捏造せず「未取得」と明記する (frontmatter には実在する値だけ)。
- リンクは標準 Markdown の相対リンクにし、特定ツールの固有記法に依存しない。
- 出力時点の最新評価を凍結して書く (スナップショット)。正本は DB で、
  Vault はいつでも再生成でき、消しても何も失われない。
- 評価の整形は site.py の _ev_for_site を再利用し、画面表示と内容を一致させる。
"""
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from .dates import parse_date
from .site import _ev_for_site, _prev_for_site
from .store import _filing_period_key

ITEM_LABELS = {
    "technology_readiness": "技術成熟度", "technological_advantage": "技術的優位性",
    "monetization": "収益化", "market_growth": "市場成長",
    "financial_sustainability": "財務持続性", "moat_durability": "参入障壁の持続性",
    "catalyst_quality": "材料の質", "expectation_gap": "期待との差",
    "not_priced_in": "未織り込み", "timing_feasibility": "時期の現実性",
    "supply_demand": "需給", "downside_resilience": "下落耐性",
}
MANAGED_DIRS = ("companies", "evals", "sectors", "tags")


def _q(s):
    """YAML のスカラーとして安全な表記 (JSON 文字列リテラルは YAML 互換)。"""
    return json.dumps(str(s), ensure_ascii=False)


def _safe(name):
    return re.sub(r'[\\/:*?"<>|]', "_", str(name))


def _jload(s, default=None):
    try:
        v = json.loads(s or "")
    except (TypeError, ValueError):
        return default
    return v if v is not None else default


def _related_news(conn, cid, as_of, limit=8, window_days=90):
    """store.related_news と同じ条件の読み取り専用版 (プールの定義を変えない)。"""
    from datetime import timedelta
    start = (as_of - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT n.title, n.published, n.url, n.fields_json, n.source, n.media_url "
        "FROM candidates n "
        "JOIN candidate_entities ne ON ne.candidate_id = n.id "
        "JOIN candidate_entities ce ON ce.entity_id = ne.entity_id "
        "WHERE ce.candidate_id = ? AND n.id != ? AND n.info_type = 'news' "
        "AND ne.link_type = 'entity' "
        "AND n.published IS NOT NULL AND n.published <= ? AND n.published >= ? "
        "ORDER BY n.published DESC, n.id LIMIT ?",
        (cid, cid, as_of.isoformat(), start, limit)).fetchall()
    out = []
    for r in rows:
        fields = _jload(r["fields_json"], [])
        out.append({"title": r["title"], "published": r["published"], "url": r["url"],
                    "media": fields[0] if fields else "",
                    "origin": "企業発表" if r["source"] == "prtimes" else "報道"})
    return out


def _related_financials(conn, cid, as_of, limit=2, window_days=400):
    """store.related_financials と同じ条件の読み取り専用版 (I-6 の訂正除外を含む)。"""
    from datetime import timedelta
    start = (as_of - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT n.title, n.published, n.url, n.abstract FROM candidates n "
        "JOIN candidate_entities ne ON ne.candidate_id = n.id "
        "JOIN candidate_entities ce ON ce.entity_id = ne.entity_id "
        "WHERE ce.candidate_id = ? AND n.id != ? AND n.info_type = 'disclosure' "
        "AND ne.strength = 'strong' AND ne.link_type = 'entity' "
        "AND n.abstract LIKE '%財務数値(%' "
        "AND n.published IS NOT NULL AND n.published <= ? AND n.published >= ? "
        "ORDER BY n.published DESC, n.id LIMIT ?",
        (cid, cid, as_of.isoformat(), start, limit + 4)).fetchall()
    out = [{"title": r["title"], "published": r["published"], "url": r["url"],
            "text": r["abstract"]} for r in rows]
    corrected = {_filing_period_key(r["title"]) for r in out
                 if "訂正" in (r["title"] or "") and _filing_period_key(r["title"])}
    if corrected:
        out = [r for r in out if "訂正" in (r["title"] or "")
               or _filing_period_key(r["title"]) not in corrected]
    return out[:limit]


def _items_md(view, weights):
    L = []
    items = view.get("items") or {}
    order = list(weights.keys()) + [k for k in items if k not in weights]
    for k in order:
        it = items.get(k)
        if not it:
            continue
        score = it.get("score")
        L.append(f"### {ITEM_LABELS.get(k, k)}: " + (f"{score}点" if score is not None else "未取得（不足）"))
        if it.get("reason"):
            L.append(it["reason"])
        for e in it.get("evidence") or []:
            ref = e.get("ref") or ""
            L.append(f"- 根拠: {e.get('summary', '')}" + (f"（[{ref}]({ref})）" if ref else ""))
        for ce in it.get("counter_evidence") or []:
            L.append(f"- 反証: {ce}")
        miss = [m for m in (it.get("missing_info") or []) if m and m != "特になし"]
        if miss:
            L.append("- 不足: " + " / ".join(miss))
        wait = it.get("awaiting_info") or []
        if wait:
            L.append("- 結果待ち: " + " / ".join(wait))
        L.append("")
    return L


def _eval_note(cand, view, ev_row, prev_view, ents, entities, news, fins, exported_at, weights):
    primary = ents[0] if ents else {}
    eid = primary.get("entity_id", "")
    ename = (entities.get(eid) or {}).get("name") or eid
    trl = view.get("trl_level") if isinstance(view.get("trl_level"), dict) else {}
    trl_val = trl.get("level")
    direc = view.get("direction") if isinstance(view.get("direction"), dict) else {}
    dir_val = direc.get("direction")
    fm = ["---", "type: evaluation", f"candidate: {_q(cand['id'])}"]
    if eid:
        fm.append(f"company: {_q(eid)}")
    fm.append(f"status: {_q(view['status'])}")
    if view.get("total") is not None:
        fm.append(f"total: {view['total']}")
    if view.get("band"):
        fm.append(f"band: {_q(view['band'])}")
    if trl_val:
        fm.append(f"trl: {trl_val}")
    if dir_val:
        fm.append(f"direction: {_q(dir_val)}")
    if view.get("llm_model"):
        fm.append(f"model: {_q(view['llm_model'])}")
    fm.append(f"evaluated: {_q((ev_row['evaluated_at'] or '')[:10])}")
    fm.append(f"config_version: {_q(ev_row['config_version'] or '')}")
    fm.append(f"source: {_q(cand['source'])}")
    if cand["published"]:
        fm.append(f"published: {_q(cand['published'][:10])}")
    if cand["retrieved_at"]:
        fm.append(f"retrieved: {_q(cand['retrieved_at'][:10])}")
    fm.append(f"exported_at: {_q(exported_at)}")
    fm.append("---")
    L = fm + ["", f"# {cand['title_ja'] or cand['title']}", ""]
    if cand["title_ja"]:
        L.append(f"原題: {cand['title']}")
    for e in ents:
        ent = entities.get(e["entity_id"]) or {}
        L.append(f"企業: [{ent.get('name') or e['entity_id']}](../companies/{_safe(e['entity_id'])}.md)"
                 f"（{ent.get('market') or 'JP'} / {ent.get('sector') or '未取得'}）")
    L.append(f"出典: [{cand['url']}]({cand['url']})（{cand['source']}）")
    L.append(f"公開: {cand['published'][:10] if cand['published'] else '未取得'}"
             f" / 取得: {cand['retrieved_at'][:10] if cand['retrieved_at'] else '未取得'}")
    L.append(f"評価日時: {ev_row['evaluated_at'] or '未取得'}"
             f" / 評価モデル: {view.get('llm_model') or '未取得'}"
             f" / 評価基準: {ev_row['config_version'] or '未取得'}（{ev_row['config_hash'] or '未取得'}）")
    if cand["translation_src"]:
        tsrc = _jload(cand["translation_src"])
        if isinstance(tsrc, dict) and tsrc:
            label = {"title": "題名", "abstract": "要旨"}
            L.append("翻訳: " + " / ".join(
                f"{label.get(k, k)}: {v}" for k, v in tsrc.items()))
        else:
            L.append(f"翻訳: {cand['translation_src']}")
    L.append("")
    L.append("## 評価サマリ")
    if view["status"] == "error":
        L.append("状態: エラー（評価が成立しなかった）")
        for e in view.get("errors") or []:
            L.append(f"- {e}")
    else:
        if view.get("total") is not None:
            L.append(f"総合点: {view['total']}（{view.get('band') or ''}）"
                     + (f" / 評価範囲: {view['total_scope']}" if view.get("total_scope") else ""))
            if view.get("total_orig") is not None and view.get("total_orig") != view.get("total"):
                L.append(f"評価当時の総合点: {view['total_orig']}（{view.get('band_orig') or ''}）")
        else:
            L.append("総合点: 未取得（保留のため算出せず）")
        L.append(f"TRL: {trl_val if trl_val else '未確定'}")
        if trl.get("reason"):
            L.append(f"TRL の理由: {trl['reason']}")
        if dir_val:
            L.append(f"方向性: {dir_val}")
            if direc.get("reason"):
                L.append(f"方向性の理由: {direc['reason']}")
        if prev_view:
            L.append(f"前回評価: 総合 {prev_view.get('total')} / TRL {prev_view.get('trl_level')}"
                     f" / 状態 {prev_view.get('status')}（{prev_view.get('evaluated_at') or ''}）")
    L.append("")
    L.append("## 項目別")
    L += _items_md(view, weights)
    crit = [ITEM_LABELS.get(k, k) for k in view.get("critical_missing") or []]
    if crit:
        L.append("## 決定的な不足")
        L += [f"- {c}" for c in crit]
        L.append("")
    L.append("## 要旨（原文）")
    L.append(cand["abstract"] or "未取得")
    L.append("")
    L.append("## 要旨（日本語訳）")
    L.append(cand["abstract_ja"] or "未取得（未翻訳）")
    L.append("")
    if news:
        L.append("## 関連ニュース（同一企業）")
        for n in news:
            L.append(f"- [{n['title']}]({n['url']})（{n['published'] or ''} / {n['origin']}"
                     + (f" / {n['media']}" if n["media"] else "") + "）")
        L.append("")
    if fins:
        L.append("## 関連財務（同一企業）")
        for f in fins:
            L.append(f"- [{f['title']}]({f['url']})（{f['published'] or ''}）")
        L.append("")
    return "\n".join(L).rstrip() + "\n"


def export_vault(db_path, cfg, out_dir, now=None):
    """DB (読み取り専用) から Markdown Vault を生成する。戻り値は件数の dict。

    書き込みは out_dir 配下の管理フォルダと README.md のみ。それ以外の
    既存ファイルは変更も削除もしない。
    """
    exported_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        entities = {e["id"]: e for e in (cfg.get("entities") or {}).get("entities", [])}
        weights = ((cfg.get("scoring") or {}).get("long") or {}).get("weights") or {}
        rows = conn.execute(
            "SELECT c.*, e.id AS _eid FROM candidates c "
            "JOIN evaluations e ON e.candidate_id = c.id AND e.kind = 'long' "
            "WHERE e.id = (SELECT id FROM evaluations WHERE candidate_id = c.id "
            "AND kind = 'long' ORDER BY evaluated_at DESC, id DESC LIMIT 1)").fetchall()
        eval_notes = {}
        company_evals = {}
        for row in rows:
            cand = {k: row[k] for k in row.keys() if k != "_eid"}
            ev = dict(conn.execute("SELECT * FROM evaluations WHERE id = ?",
                                   (row["_eid"],)).fetchone())
            view = _ev_for_site(ev, cfg)
            # 前回比の対象は site.py / store.prev_evaluation と同じ定義
            # (final/hold のみ、error は対象外) に揃える
            prev = conn.execute(
                "SELECT * FROM evaluations WHERE candidate_id = ? AND kind = 'long' "
                "AND id < ? AND status IN ('final', 'hold') ORDER BY id DESC LIMIT 1",
                (cand["id"], row["_eid"])).fetchone()
            prev_view = _prev_for_site(dict(prev)) if prev else None
            ents = [dict(r) for r in conn.execute(
                "SELECT entity_id FROM candidate_entities WHERE candidate_id = ? "
                "AND link_type = 'entity' ORDER BY strength DESC, entity_id", (cand["id"],))]
            as_of = parse_date(ev["as_of"]) or date.today()
            news = _related_news(conn, cand["id"], as_of)
            fins = _related_financials(conn, cand["id"], as_of)
            note = _eval_note(cand, view, ev, prev_view, ents, entities,
                              news, fins, exported_at, weights)
            eval_notes[cand["id"]] = (note, view, ev, ents)
            for e in ents:
                company_evals.setdefault(e["entity_id"], []).append((cand, view, ev))
        out = Path(out_dir)
        for d in MANAGED_DIRS:
            (out / d).mkdir(parents=True, exist_ok=True)
        for cid, (note, view, ev, ents) in eval_notes.items():
            (out / "evals" / f"{_safe(cid)}.md").write_text(note, encoding="utf-8")
        for eid, lst in sorted(company_evals.items()):
            ent = entities.get(eid) or {}
            fm = ["---", "type: company", f"id: {_q(eid)}", f"name: {_q(ent.get('name') or eid)}"]
            if ent.get("market"):
                fm.append(f"market: {_q(ent['market'])}")
            if ent.get("sector"):
                fm.append(f"sector: {_q(ent['sector'])}")
            if ent.get("stage"):
                fm.append(f"stage: {_q(ent['stage'])}")
            if ent.get("tier"):
                fm.append(f"tier: {_q(ent['tier'])}")
            if ent.get("tags"):
                fm.append("tags: [" + ", ".join(_q(t) for t in ent["tags"]) + "]")
            fm += ["---", "", f"# {ent.get('name') or eid}", ""]
            fm.append(f"市場: {ent.get('market') or 'JP'} / 分野: "
                      + (f"[{ent['sector']}](../sectors/{_safe(ent['sector'])}.md)" if ent.get("sector") else "未取得")
                      + (f" / 段階: {ent['stage']}" if ent.get("stage") else ""))
            fm.append("")
            fm.append(f"## 評価 ({len(lst)}件)")
            for cand, view, ev in sorted(lst, key=lambda x: x[2]["evaluated_at"] or "", reverse=True):
                trl = view.get("trl_level") if isinstance(view.get("trl_level"), dict) else {}
                line = (f"- [{cand['title_ja'] or cand['title']}](../evals/{_safe(cand['id'])}.md)"
                        f" — {view['status']}")
                if view.get("total") is not None:
                    line += f" / 総合 {view['total']}"
                if trl.get("level"):
                    line += f" / TRL {trl['level']}"
                line += f"（{(ev['evaluated_at'] or '')[:10]}）"
                fm.append(line)
            (out / "companies" / f"{_safe(eid)}.md").write_text("\n".join(fm) + "\n", encoding="utf-8")
        sectors = sorted({(entities.get(e) or {}).get("sector") for e in company_evals} - {None})
        tags = sorted({t for e in company_evals for t in (entities.get(e) or {}).get("tags") or []})
        for s in sectors:
            (out / "sectors" / f"{_safe(s)}.md").write_text(
                f"---\ntype: sector\nsector: {_q(s)}\n---\n\n# 分野: {s}\n\n"
                "この分野の企業は companies/ の各ノートの frontmatter (sector) で絞り込めます。\n",
                encoding="utf-8")
        for t in tags:
            (out / "tags" / f"{_safe(t)}.md").write_text(
                f"---\ntype: tag\ntag: {_q(t)}\n---\n\n# タグ: {t}\n\n"
                "このタグの企業は companies/ の各ノートの frontmatter (tags) で絞り込めます。\n",
                encoding="utf-8")
        notes_readme = out / "notes" / "README.md"
        if not notes_readme.exists():
            (out / "notes").mkdir(parents=True, exist_ok=True)
            notes_readme.write_text(
                "# 自分のメモ置き場\n\nこのフォルダは再生成の対象外です。調査メモはここに置き、"
                "生成ノートへは相対リンクで繋いでください。\n", encoding="utf-8")
        index = [
            "# TRL Vault", "",
            f"生成: {exported_at} / 評価 {len(eval_notes)} 件 / 企業 {len(company_evals)} 社。"
            "正本は DB で、このフォルダは出力時点の内容を凍結した導出層です"
            "（companies/ evals/ sectors/ tags/ と この README は再生成で更新されます）。", "",
            "## 歩き方", "",
            "- companies/ の企業ノートから、その企業の評価へ辿れます",
            "- evals/ の各ノートに項目点・理由・根拠・不足・前回比と、出典URL・取得日時・評価基準のバージョンが入っています",
            "- 原文と日本語訳は別セクションに分かれています。無い情報は「未取得」と明記されます",
            "- notes/ は自分のメモ専用で、再生成では触れません", "",
        ]
        (out / "README.md").write_text("\n".join(index), encoding="utf-8")
        return {"evaluations": len(eval_notes), "companies": len(company_evals)}
    finally:
        conn.close()
