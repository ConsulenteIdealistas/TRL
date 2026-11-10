"""Web ページ (web/ 配下の HTML・JS・CSS) 自体の構造チェック。

実行: python -m unittest test_web -v
test_site.py の ExportTests から、書き出しデータではなく web ファイル
自体を対象にするテストを分離した。クラス名はテスト ID の追跡可能性
のため ExportTests のままにしている (本体は test_site.py 側)。
"""
import json
import unittest
from pathlib import Path

import trl as T
from testutil import Mock, StoreFixture, cfg_with, make_llm_fn


class ExportTests(StoreFixture):
    def test_search_suggest_is_touch_friendly(self):
        # スマホでサジェストが効くための配線: タップは pointerdown、候補は入力欄の直下、
        # 入力欄は iOS が拡大しない 16px。どれか欠けると実機でタップがずれる/候補が離れる。
        html = (T.ROOT / "web" / "index.html").read_text(encoding="utf-8")
        js = (T.ROOT / "web" / "app.js").read_text(encoding="utf-8")
        css = (T.ROOT / "web" / "style.css").read_text(encoding="utf-8")
        self.assertIn('class="qin"', html)
        self.assertIn('addEventListener("pointerdown"', js)
        self.assertIn("compositionend", js)
        # 変換を確定しても候補が消えたままにならないこと (確定後に再表示、blur の消去タイマーは解除可能に)
        self.assertIn("compositionstart", js)
        self.assertIn("sugHideTimer", js)
        self.assertIn('addEventListener("focus"', js)
        self.assertIn(".qin{position:relative", css)
        self.assertIn("font-size:16px", css)
        # 候補リストがヒーローの下端で切り取られないこと (overflow:hidden があると
        # 絶対配置の候補がヒーローの箱の外側で見えなくなる)
        import re
        hero = re.search(r"\.hero\{([^}]*)\}", css).group(1)
        self.assertNotIn("overflow:hidden", hero)
    def test_audit_review_page_exists_and_linked(self):
        # 監査ページの存在と、一覧・詳細からの導線 (採点基準リンクと同格)
        web = T.ROOT / "web"
        page = (web / "audit.html").read_text(encoding="utf-8")
        js = (web / "audit.js").read_text(encoding="utf-8")
        self.assertIn("audit.js", page)
        self.assertIn("反映用 JSON をコピー", page)
        self.assertIn("trl-review", js)
        self.assertIn("review_inbox", js + page)
        # 状態の絞り込み・並び替えと、処分種別を含む8枠の統計。
        # ヘッダーには採点基準/監査リンクを置かない (他ページと同形)
        self.assertIn('id="stateFilters"', page)
        self.assertIn('id="sortSel"', page)
        self.assertIn("data-sfilter", js)
        for key in ("dconfirmed", "dfalse", "dwatch"):
            self.assertIn(key, js)
        for label in ("要確認", "グレー", "処分済み", "再評価予約", "調整済み",
                      "問題あり", "誤検知", "様子見"):
            self.assertIn(label, js)
        for label in ("TRL 高い順", "TRL 低い順", "新しい順", "古い順"):
            self.assertIn(label, page)
        self.assertNotIn('<a href="criteria.html">採点基準</a>', page)
        self.assertNotIn('<a href="audit.html">監査</a>', page)
        # 記事 (詳細) と監査カードの相互遷移: 詳細に監査セクションと
        # 監査ページへの深いリンク、監査側は ?c= で該当カードへ移動する
        app = (web / "app.js").read_text(encoding="utf-8")
        self.assertIn("auditSectionHtml", app)
        self.assertIn("audit.html?c=", app)
        self.assertIn('id="card-', js)
        self.assertIn('get("c")', js)
        self.assertIn("記事（詳細ページ）を見る", js)
        # イベント監査の表示: 種別切替と audit_event の利用、E1/E2 のラベル
        self.assertIn("audit_event", js)
        self.assertIn("data-kind", js)
        self.assertIn("イベント評価", js)
        self.assertIn("E1:", js)
        self.assertIn("E2:", js)
        self.assertIn("audit_event", app)
        # 一覧は検索と統計の間の .pagelinks に短いリンク、詳細はフッター
        # のみ (ヘッダーは両ページともリンクなしで揃える)
        index = (web / "index.html").read_text(encoding="utf-8")
        self.assertIn('<nav class="pagelinks"><a href="criteria.html">採点基準</a>'
                      '<a href="audit.html">監査</a><a href="watch.html">Look・See</a></nav>', index)
        self.assertIn('<a href="audit.html">監査（要確認の処分）</a>', index)
        # Look・See ページも同格に存在し、一覧から辿れる
        wpage = (web / "watch.html").read_text(encoding="utf-8")
        self.assertIn("watch.js", wpage)
        self.assertIn('<a href="watch.html">Look・See（未評価の監視企業）</a>', index)
        detail = (web / "detail.html").read_text(encoding="utf-8")
        self.assertIn('<a href="audit.html">監査（要確認の処分）</a>', detail)
        self.assertNotIn('<a href="audit.html">監査</a>', detail)
        # export でページ一式 (HTML/JS) も配布される
        self.build(Mock(make_llm_fn()))
        out = Path(self.tmp.name) / "site"
        T.export_site(self.store, cfg_with(), out)
        names = {p.name for p in out.iterdir()}
        self.assertIn("audit.html", names)
        self.assertIn("audit.js", names)
        self.assertIn("watch.html", names)
        self.assertIn("watch.js", names)
        # ウォッチ・参考の同梱データ (tier が watch / reference の企業だけ)
        data = json.loads((out / "data.json").read_text(encoding="utf-8"))
        self.assertIn("watch_companies", data)
        self.assertTrue(all(c["tier"] in ("watch", "reference")
                            for c in data["watch_companies"]))
    def test_kind_sort_on_index_and_audit_pages(self):
        # 一覧: 並び順に種別別 (長期→短期) があり、種別は明示 kind を第一に、
        # 無ければ長期評価の有無で決める (書き出しに kind は無いため、
        # 未評価の候補は長期側に寄る)
        web = T.ROOT / "web"
        index = (web / "index.html").read_text(encoding="utf-8")
        app = (web / "app.js").read_text(encoding="utf-8")
        self.assertIn('<option value="kind">種別別（長期→短期）</option>', index)
        self.assertIn("const kindOf", app)
        self.assertIn('o === "kind"', app)
        # 監査: 種別の「両方」表示と種別順。表示単位は候補×種別のカードで、
        # 統計・チップ・種別チップの件数はすべてカード単位に揃える
        page = (web / "audit.html").read_text(encoding="utf-8")
        js = (web / "audit.js").read_text(encoding="utf-8")
        self.assertIn('<option value="kind">種別順</option>', page)
        self.assertIn('["all", "両方"]', js)
        self.assertIn("function cards()", js)
        self.assertIn('viewState.sort === "kind"', js)
        self.assertIn('q.get("k") === "all"', js)
        self.assertIn("sbadge lngk", js)  # 両方表示で長期カードにも種別バッジ
    def test_filter_chips_match_watch_pill_style(self):
        # 主ページのフィルタチップはウォッチページの .dchip (スイッチ) に
        # 揃える: ピル型 (999px) で、選択時は accent のソリッド塗り+白文字。
        # min-height/min-width はタップターゲット確保のため主ページ側に残す。
        import re
        css = (T.ROOT / "web" / "style.css").read_text(encoding="utf-8")
        watch = (T.ROOT / "web" / "watch.html").read_text(encoding="utf-8")
        self.assertIn(".dchip{", watch)  # 揃える先 (ウォッチのピル型チップ)
        base = re.search(r"\.chips button\{([^}]*)\}", css).group(1)
        for decl in ("border-radius:999px", "padding:6px 11px",
                     "background:var(--surface)", "font-size:12px",
                     "font-weight:750", "min-height:42px", "min-width:40px"):
            self.assertIn(decl, base)
        on = re.search(r"\.chips button\.on\{([^}]*)\}", css).group(1)
        self.assertIn("background:var(--accent,#4a6cf7)", on)
        self.assertIn("color:#fff", on)
        self.assertIn("border-color:transparent", on)
        # --accent は style.css 側でも定義し、両テーマで解決できるようにする
        self.assertIn("--accent:#4a6cf7", css)
    def test_state_chips_paint_state_color_only_when_on(self):
        # 状態色チップ (評価状態 #fsc / 方向性 #fdc / TRL「判定済み」 #ftc) は
        # 普段は他のチップと同じ見た目 (未選択の色文字指定は持たない) にし、
        # 選択時にだけ一般チップの accent 塗りと同じ形 (ソリッド塗り+白文字+
        # 枠透明) で、塗り色を統計・バッジと同じ状態色に置き換える。
        # テーマ別の上書きも持たず、両テーマで同じ塗りに統一する。
        import re
        css = (T.ROOT / "web" / "style.css").read_text(encoding="utf-8")
        # この3グループの button セレクタは全て .on のものだけが残る
        # (未選択の色文字ルールや :not(.on) ルールが無いことの担保)
        sels = re.findall(r"#(?:fsc|fdc|ftc) button[^{]*\{", css)
        self.assertTrue(sels)
        for sel in sels:
            self.assertIn(".on", sel)
        self.assertNotIn(":not(.on)", css)
        # 各状態の .on は状態色のソリッド塗り+白文字+枠透明 (全マッチ共通)
        fills = {"final": "#087b5a", "positive": "#087b5a",
                 "review": "#087eaf", "hold": "#a96500", "mixed": "#a96500",
                 "pending": "#c87a00", "error": "#b52f49",
                 "negative": "#b52f49", "rejected": "#607080",
                 "none": "#607080"}
        for v, color in fills.items():
            bodies = re.findall(
                r'button\[data-v="%s"\]\.on[^{]*\{([^}]*)\}' % v, css)
            self.assertTrue(bodies, v)
            for body in bodies:
                self.assertIn("background:" + color, body)
                self.assertIn("color:#fff", body)
                self.assertIn("border-color:transparent", body)
        # 「判定済み」は :root の --pk 実値 (#db2777) で塗る。ダーク側の
        # 文字色用 --pk (#f472b6) は白文字が沈むため塗りに使わない
        bodies = re.findall(r'#ftc button\[data-l="all"\]\.on\{([^}]*)\}', css)
        self.assertEqual(len(bodies), 1)
        self.assertIn("background:#db2777", bodies[0])
        self.assertIn("color:#fff", bodies[0])
        self.assertIn("border-color:transparent", bodies[0])
    def test_hold_stat_breakdown_label_and_title(self):
        # 統計枠「保留」の内訳ラベルは実態に寄せる: 「企業未特定」は企業リンクが
        # 無いという意味に読めるが、実際は資料の中で対象企業を一意に定められ
        # なかった hold (リンクの有無とは別) を数えている。ラベルを
        # 「企業を特定できず」に改め、title に両内訳の定義を入れる。
        # 数値の入れ先 (#c3u / #c3e) と集計は不変。
        import re
        html = (T.ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("企業未特定", html)
        self.assertIn(
            '企業を特定できず <span id="c3u">0</span>件 / '
            '根拠不足 <span id="c3e">0</span>件', html)
        m = re.search(r'<div class="stat y" data-st="hold" title="([^"]*)"',
                      html)
        self.assertIsNotNone(m)
        title = m.group(1)
        self.assertIn("企業を特定できず＝資料の中で対象企業を一意に定められなかった",
                      title)
        self.assertIn("企業リンクの有無とは別の分類", title)
        self.assertIn(
            "根拠不足＝対象企業は特定できていますが、必須項目の根拠が"
            "資料にありませんでした", title)
    def test_tier_display_differentiation(self):
        # tier 差別化 (WEB.md W-1〜W-9 確定): Core も対称に併記し、
        # Conditional には導出の理由 (derived_reasons) を平文で併記する。
        # 分布・警告の帯と「企業の層」フィルタは主ページ、状態色は使わない
        web = T.ROOT / "web"
        html = (web / "index.html").read_text(encoding="utf-8")
        app = (web / "app.js").read_text(encoding="utf-8")
        det = (web / "detail.html").read_text(encoding="utf-8")
        self.assertIn('id="tierband"', html)
        self.assertIn('id="ftierc"', html)
        self.assertIn("summary", app)
        self.assertIn("tierBand", app)
        self.assertIn("tierSel", app)
        self.assertIn('core: "Core"', app)
        self.assertIn("dtText", app)
        self.assertIn("derived_reasons", app)
        self.assertIn('core: "Core"', det)
        self.assertIn("dtText", det)
        self.assertIn("derived_reasons", det)
        # ウォッチページは Look・See に改名し、バッジは無色に統一 (W-5/W-6)
        watch = (web / "watch.html").read_text(encoding="utf-8")
        self.assertIn("<title>Look・See — TRL Explorer</title>", watch)
        self.assertNotIn(".tbadge.watch", watch)
        self.assertNotIn(".tbadge.reference", watch)
        # 監査カードに導出 tier を併記する (W-9)
        audit = (web / "audit.js").read_text(encoding="utf-8")
        self.assertIn("dtLabelA", audit)
    def test_reference_item_display(self):
        # 参考項目 (規程第13条): 詳細・ドロワーとも「参考項目」として分離表示し、
        # 不足の集計 (missAll) からは外す。ラベルは3ページで共通
        web = T.ROOT / "web"
        det = (web / "detail.html").read_text(encoding="utf-8")
        app = (web / "app.js").read_text(encoding="utf-8")
        audit = (web / "audit.js").read_text(encoding="utf-8")
        for text in (det, app, audit):
            self.assertIn('management_execution: "経営陣・実行力"', text)
        for text in (det, app):
            self.assertIn("reference_items", text)
            self.assertIn("<h4>参考項目</h4>", text)
            self.assertIn("総合点・必須判定には含めません", text)
