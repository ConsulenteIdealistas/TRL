"""収集系 (trlcore/sources/* の解析・取得と、収集段の制御) のテスト。

実行: python -m unittest test_sources -v
test_trl.py から分離した。共通フィクスチャは testutil を参照する。
"""
import trl as T
from trl import *  # noqa

import copy
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import date, timedelta
from pathlib import Path

from testutil import (AS_OF, CFG, TRIALS_DATA, arxiv, edinet, gnews, http,
                      openalex, prtimes, pubmed, raw, trials, yahoo)


PUBMED_XML = b"""<?xml version="1.0"?><PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>111</PMID>
<Article><Journal><JournalIssue><PubDate><Year>2026</Year><Month>Oct</Month></PubDate></JournalIssue></Journal>
<ArticleTitle>Gene <i>therapy</i> trial</ArticleTitle><Abstract><AbstractText Label="BACKGROUND">Bg text.</AbstractText>
<AbstractText>Plain.</AbstractText></Abstract></Article><MeshHeadingList><MeshHeading><DescriptorName>Genetic Therapy</DescriptorName></MeshHeading></MeshHeadingList></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="pubmed">111</ArticleId><ArticleId IdType="doi">10.1/ABC</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>"""


ARXIV_XML = b"""<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><entry>
<id>http://arxiv.org/abs/2610.01234v2</id><published>2026-10-05T10:00:00Z</published><title>Solid  state
 battery</title><summary> Abstract here. </summary><category term="cond-mat.mtrl-sci"/><arxiv:doi>10.1/abc</arxiv:doi></entry></feed>"""


class SourceParseTests(unittest.TestCase):
    def test_pubmed(self):
        r = pubmed.parse_efetch(PUBMED_XML)[0]
        self.assertEqual((r["pmid"], r["doi"], r["published"]), ("111", "10.1/ABC", "2026-10"))
        self.assertEqual(r["title"], "Gene therapy trial")
        self.assertIn("BACKGROUND: Bg text.", r["abstract"])

    def test_arxiv(self):
        r = arxiv.parse_feed(ARXIV_XML)[0]
        self.assertEqual((r["arxiv_id"], r["published"], r["doi"]), ("2610.01234", "2026-10-05", "10.1/abc"))

    def test_pubmed_fetch_uses_as_of_window(self):
        calls = []

        def fake(url, params=None, **kw):
            calls.append(params)
            return b'{"esearchresult":{"idlist":["111"]}}' if "esearch" in url else PUBMED_XML
        out = pubmed.fetch("q", 5, 30, AS_OF, http=fake, sleep=lambda s: None)
        self.assertEqual(len(out), 1)
        self.assertEqual(calls[0]["maxdate"], "2026/10/20")
        self.assertEqual(calls[0]["mindate"], "2026/09/20")


GNEWS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>news</title>
<item><title>Astroscale wins a debris removal contract - Example News</title>
<link>https://news.google.com/rss/articles/AAA</link>
<pubDate>Sun, 18 Oct 2026 01:00:00 GMT</pubDate>
<description>&lt;a href="https://example.com"&gt;Astroscale wins a debris removal contract&lt;/a&gt;</description>
<source url="https://example.com">Example News</source></item>
<item><title>Old news about Astroscale funding</title>
<link>https://news.google.com/rss/articles/BBB</link>
<pubDate>Sat, 01 Aug 2026 01:00:00 GMT</pubDate>
<description>Old news about Astroscale funding</description>
<source url="https://example.com">Example News</source></item>
<item><title>Astroscale column republished by an aggregator</title>
<link>https://news.google.com/rss/articles/CCC</link>
<pubDate>Mon, 19 Oct 2026 01:00:00 GMT</pubDate>
<description>Astroscale column</description>
<source url="https://biggo.example">BigGo Finance</source></item>
</channel></rss>"""


EDINET_RESULTS = [
    {"docID": "S100AAAA", "edinetCode": "E00001", "secCode": "93480", "filerName": "株式会社ispace",
     "docDescription": "臨時報告書", "submitDateTime": "2026-10-15 15:00", "periodStart": None,
     "periodEnd": None, "withdrawalStatus": "0"},
    {"docID": "S100BBBB", "edinetCode": "E00002", "secCode": "99990", "filerName": "別会社",
     "docDescription": "四半期報告書", "submitDateTime": "2026-10-15 15:00", "withdrawalStatus": "0"},
    {"docID": "S100CCCC", "edinetCode": "E00001", "secCode": "93480", "filerName": "株式会社ispace",
     "docDescription": "取下げ", "submitDateTime": "2026-10-14 15:00", "withdrawalStatus": "1"},
]


class NewSourceParseTests(unittest.TestCase):
    def test_gnews(self):
        items = gnews.parse_feed(GNEWS_XML)
        self.assertEqual(len(items), 2)  # BigGo の転載コラムは媒体フィルタで除外
        r = items[0]
        self.assertEqual(r["media_url"], "https://example.com")  # 媒体サイト (source url 属性)
        self.assertEqual((r["source"], r["info_type"], r["published"]), ("gnews", "news", "2026-10-18"))
        self.assertIn("配信元: Example News", r["abstract"])
        self.assertTrue(r["url"].startswith("https://news.google.com/"))

    def test_gnews_fetch_window(self):
        out = gnews.fetch("アストロスケール", 20, 30, AS_OF, http=lambda url, params=None, **kw: GNEWS_XML)
        self.assertEqual(len(out), 1)  # 2026-08-01 は lookback 30 日の外

    def test_gnews_fetch_with_locale_dict(self):
        calls = []

        def fake(url, params=None, **kw):
            calls.append(params)
            return GNEWS_XML

        q = {"q": "Rocket Lab (contract OR launch)", "hl": "en", "gl": "US", "ceid": "US:en"}
        out = gnews.fetch(q, 20, 30, AS_OF, http=fake)
        self.assertEqual(calls[0]["hl"], "en")
        self.assertEqual(calls[0]["gl"], "US")
        self.assertIn("Rocket Lab", calls[0]["q"])
        self.assertEqual(len(out), 1)

    def test_prtimes_fetch_uses_site_filter(self):
        calls = []

        def fake(url, params=None, **kw):
            calls.append(params)
            return GNEWS_XML
        out = prtimes.fetch("Example", 20, 30, AS_OF, http=fake)  # 固定XMLの見出し語で厳密フィルタを通す
        self.assertIn("site:prtimes.jp", calls[0]["q"])
        self.assertTrue(out)
        self.assertTrue(all(r["source"] == "prtimes" for r in out))

    def test_openalex_parse_and_fetch(self):
        data = {"results": [{
            "id": "https://openalex.org/W123", "doi": "https://doi.org/10.1000/xyz",
            "title": "On-orbit demonstration of a debris removal system",
            "publication_date": "2026-09-15",
            "abstract_inverted_index": {"On-orbit": [0], "demonstration": [1], "results": [2]},
            "authorships": [{"institutions": [{"display_name": "Astroscale"}]},
                            {"institutions": [{"display_name": "University of Tokyo"},
                                              {"display_name": "Astroscale"}]}],
            "primary_location": {"source": {"display_name": "Acta Astronautica"}},
            "type": "article"}]}
        r = openalex.parse(data)[0]
        self.assertEqual((r["source"], r["source_id"], r["info_type"]), ("openalex", "W123", "paper"))
        self.assertEqual(r["abstract"], "On-orbit demonstration results")
        self.assertEqual(r["affiliations"], ["Astroscale", "University of Tokyo"])
        self.assertEqual(r["doi"], "10.1000/xyz")
        self.assertEqual(r["fields"], ["Acta Astronautica"])
        seen = {}
        def stub(url, params=None, **kw):
            seen.update(params or {})
            return json.dumps(data)
        out = openalex.fetch("I999", 5, 365, AS_OF, http=stub)
        self.assertEqual(len(out), 1)
        # フィルタのフィールド名は複数形 institutions(単数形はAPIが拒否する)
        self.assertIn("authorships.institutions.id:I999", seen.get("filter", ""))

    def test_translation(self):
        self.assertTrue(T.needs_translation("High-throughput actinic mask inspection for EUV lithography"))
        self.assertFalse(T.needs_translation("月面探査ローバーの熱制御の設計と実装"))
        self.assertFalse(T.needs_translation("AI"))
        # Google: キーはヘッダーで送り、応答の HTML エスケープは戻す
        seen = {}
        def g_http(url, params=None, headers=None, data=None, **kw):
            seen["headers"] = headers or {}
            qs = json.loads(data)["q"]
            return json.dumps({"data": {"translations": [{"translatedText": "訳&amp;:" + q} for q in qs]}})
        out = T.google_translate(["Title"], "gkey", http=g_http)
        self.assertEqual(out, ["訳&:Title"])
        self.assertEqual(seen["headers"].get("X-Goog-Api-Key"), "gkey")

    def test_translate_display_set(self):
        # 最終段の Google で訳し、出典に google が記録される
        import os
        from unittest import mock as um
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")
        rec, _ = T.normalize(raw(), T.now_iso())
        cid, _ = store.upsert_candidate(rec, "rX")
        store.conn.execute(
            "INSERT INTO evaluations(run_id, candidate_id, kind, status) VALUES('rX', ?, 'long', 'ok')", (cid,))
        store.conn.commit()

        def g_http(url, params=None, headers=None, data=None, **kw):
            qs = json.loads(data)["q"]
            return json.dumps({"data": {"translations": [{"translatedText": "訳:" + q} for q in qs]}})
        with um.patch.dict(os.environ, {"GOOGLE_TRANSLATE_API_KEY": "gk"}, clear=False):
            n, stats, failed = T.translate_display_set(store, CFG, AS_OF, log=lambda *_: None, http=g_http)
            self.assertEqual(n, 1)
            self.assertEqual(failed, 0)
            # 訳した文字数は、送信した原文 (title + abstract) の字数を段ごとに数える
            self.assertEqual(stats, {"google": len(rec["title"]) + len(rec["abstract"])})
            c = store.get_candidate(cid)
            self.assertTrue(c["title_ja"].startswith("訳:"))
            self.assertEqual(json.loads(c["translation_src"]), {"title": "google", "abstract": "google"})
            # 2回目は未訳が無いので 0 件・0 字
            self.assertEqual(T.translate_display_set(store, CFG, AS_OF, log=lambda *_: None, http=g_http), (0, {}, 0))
        store.close()
        tmp.cleanup()

    def test_translate_failures_are_counted(self):
        # 全経路で訳せなかった候補は、件数として呼び出し側に返る (pipeline が
        # counts の translation_failed に残す)。失敗したバッチがあっても
        # 残りのバッチは続け、失敗分は原文表示のまま残る
        import os
        from unittest import mock as um
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")
        cids = []
        for i in range(6):  # BATCH=5 を超えて2バッチに分ける
            rec, _ = T.normalize(raw(source="pubmed", sid=f"F{i}", title=f"Failure Paper {i}"), T.now_iso())
            cid, _ = store.upsert_candidate(rec, "rX")
            store.conn.execute(
                "INSERT INTO evaluations(run_id, candidate_id, kind, status) VALUES('rX', ?, 'long', 'ok')", (cid,))
            cids.append(cid)
        store.conn.commit()
        calls = []

        def flaky_http(url, params=None, headers=None, data=None, **kw):
            calls.append(url)
            if len(calls) > 1:
                raise RuntimeError("google down")
            qs = json.loads(data)["q"]
            return json.dumps({"data": {"translations": [{"translatedText": "訳:" + q} for q in qs]}})
        env = {"GOOGLE_TRANSLATE_API_KEY": "gk", "AZURE_TRANSLATOR_KEY": "", "DEEPL_API_KEY": ""}
        with um.patch.dict(os.environ, env, clear=False):
            n, stats, failed = T.translate_display_set(store, CFG, AS_OF, log=lambda *_: None, http=flaky_http)
        self.assertEqual(n, 5)       # 1バッチ目は訳せる
        self.assertEqual(failed, 1)  # 2バッチ目の1件が失敗として数えられる
        self.assertEqual(set(stats), {"google"})
        untranslated = [cid for cid in cids if not store.get_candidate(cid).get("title_ja")]
        self.assertEqual(len(untranslated), 1)
        store.close()
        tmp.cleanup()

    def test_translate_watch_queue(self):
        # ウォッチ・参考の一次情報 (論文・治験・開示) だけを訳す
        # (ニュースは対象外、DeepL は使わない)
        import os
        from unittest import mock as um
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")
        cfg = dict(CFG)
        cfg["entities"] = {"entities": [
            {"id": "watch_co", "name": "Watch Co", "tier": "watch", "market": "US", "sector": "bio"},
            {"id": "ref_co", "name": "Ref Co", "tier": "reference", "market": "IN", "sector": "bio"},
            {"id": "core_co", "name": "Core Co", "tier": "core", "market": "US", "sector": "bio"}]}
        made = {}
        for key, src, sid, title, eid in (
                ("w1", "pubmed", "W1", "Watch Paper Alpha", "watch_co"),
                ("w2", "openalex", "W2", "Watch Paper Beta", "watch_co"),
                ("r1", "arxiv", "R1", "Reference Paper Gamma", "ref_co"),
                ("y1", "yahoo", "Y1", "Watch Filing Epsilon", "watch_co"),
                ("e1", "edgar", "E1", "Watch Filing Zeta", "watch_co"),
                ("g1", "gnews", "G1", "Watch News Delta", "watch_co")):
            rec, _ = T.normalize(raw(source=src, sid=sid, title=title), T.now_iso())
            cid, _ = store.upsert_candidate(rec, "rX")
            store.set_entities(cid, [{"entity_id": eid, "strength": "strong"}])
            made[key] = (cid, rec)
        urls = []

        def g_http(url, params=None, headers=None, data=None, **kw):
            urls.append(url)
            qs = json.loads(data)["q"]
            return json.dumps({"data": {"translations": [{"translatedText": "訳:" + q} for q in qs]}})
        env = {"GOOGLE_TRANSLATE_API_KEY": "gk", "DEEPL_API_KEY": "dk:fx"}
        with um.patch.dict(os.environ, env, clear=False):
            n, stats, failed = T.translate_watch_queue(store, cfg, AS_OF, log=lambda *_: None, http=g_http)
            self.assertEqual(n, 5)  # 論文3件 + 開示2件 (ニュースは訳さない)
            self.assertEqual(failed, 0)
            want = sum(len(made[k][1]["title"]) + len(made[k][1]["abstract"]) for k in ("w1", "w2", "r1", "y1", "e1"))
            self.assertEqual(stats, {"google": want})  # DeepL 温存枠は使わない
            self.assertTrue(all("translation.googleapis.com" in u for u in urls))
            self.assertIsNone(store.get_candidate(made["g1"][0]).get("title_ja"))
            self.assertEqual(json.loads(store.get_candidate(made["w1"][0])["translation_src"]),
                             {"title": "google", "abstract": "google"})
            # 2回目は未訳が無いので 0 件
            self.assertEqual(T.translate_watch_queue(store, cfg, AS_OF, log=lambda *_: None, http=g_http), (0, {}, 0))
            # Azure の月枠を極小にすると在庫があっても止まる
            rec, _ = T.normalize(raw(source="pubmed", sid="W9", title="Watch Paper Zeta"), T.now_iso())
            cid9, _ = store.upsert_candidate(rec, "rX")
            store.set_entities(cid9, [{"entity_id": "watch_co", "strength": "strong"}])
            with um.patch("trlcore.translate.AZURE_MONTHLY_QUOTA", 1):
                self.assertEqual(T.translate_watch_queue(store, cfg, AS_OF, log=lambda *_: None, http=g_http), (0, {}, 0))
        store.close()
        tmp.cleanup()

    def test_watch_queue_window_and_budget(self):
        # 表示窓 (企業ごとの一次情報・新しい順15件) の外と、prune の paper
        # 窓 (180日) を出た材料はキューに入らない。1実行の予算は Azure の
        # 月枠を当月の残り実行回数で割って導く (件数の上限は無い)
        from trlcore import translate as TT
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")
        cfg = dict(CFG)
        cfg["entities"] = {"entities": [
            {"id": "watch_co", "name": "Watch Co", "tier": "watch", "market": "US", "sector": "bio"},
            {"id": "core_co", "name": "Core Co", "tier": "core", "market": "US", "sector": "bio"}]}
        ids = {}
        # watch_co に新しい順で 17 件 (表示窓は 15 件まで)
        for i in range(17):
            rec, _ = T.normalize(raw(source="pubmed", sid=f"W{i:02d}",
                                     title=f"Watch Paper Number {i:02d}",
                                     published=f"2026-10-{20 - i:02d}"), T.now_iso())
            cid, _ = store.upsert_candidate(rec, "rX")
            store.set_entities(cid, [{"entity_id": "watch_co", "strength": "strong"}])
            ids[f"W{i:02d}"] = cid
        # core_co に paper 窓 (AS_OF - 180日 = 2026-04-23) を出た材料。
        # prune の N-2 と同じく、対象は screening 済み (screened) の paper
        rec, _ = T.normalize(raw(source="pubmed", sid="OLD", title="Core Paper Ancient",
                                 published="2026-01-15"), T.now_iso())
        cid_old, _ = store.upsert_candidate(rec, "rX")
        store.set_entities(cid_old, [{"entity_id": "core_co", "strength": "strong"}])
        store.conn.execute("UPDATE candidates SET status='screened' WHERE id=?", (cid_old,))
        store.conn.commit()
        todo = TT._watch_todo(store, cfg, AS_OF)
        got = {item[0] for item in todo}
        self.assertEqual(len(todo), 15)
        self.assertNotIn(ids["W15"], got)  # 16件目 (表示窓の外)
        self.assertNotIn(ids["W16"], got)  # 17件目 (表示窓の外)
        self.assertNotIn(cid_old, got)     # paper 窓の外
        # 予算の導出: 使用量ゼロなら (月枠 200万字) ÷ (当月の残り実行回数)
        # × 安全率 0.8。AS_OF = 2026-10-20 (火) で、残りは今日 + 水木金 +
        # 翌週の火〜金 = 8回
        self.assertEqual(TT.watch_run_budget(store, AS_OF),
                         int(2_000_000 // 8 * TT.WATCH_BUDGET_SAFETY_FACTOR))
        self.assertEqual(TT.watch_run_budget(store, AS_OF, spent_before=400_000),
                         int((2_000_000 - 400_000) // 8 * TT.WATCH_BUDGET_SAFETY_FACTOR))
        store.close()
        tmp.cleanup()

    def test_watch_queue_disclosure_sources(self):
        # yahoo / edgar (開示) もキューの対象で、表示窓 (企業ごとの一次
        # 情報 15件) を論文と一緒に消費する。gnews は窓の内側でも入らない
        from trlcore import translate as TT
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")
        cfg = dict(CFG)
        cfg["entities"] = {"entities": [
            {"id": "watch_co", "name": "Watch Co", "tier": "watch", "market": "US", "sector": "bio"}]}
        ids = {}
        # 一次情報を新しい順に 14件 (論文) + yahoo + edgar = 16件並べる
        for i in range(14):
            rec, _ = T.normalize(raw(source="pubmed", sid=f"P{i:02d}",
                                     title=f"Watch Paper Number {i:02d}",
                                     published=f"2026-10-{19 - i:02d}"), T.now_iso())
            cid, _ = store.upsert_candidate(rec, "rX")
            store.set_entities(cid, [{"entity_id": "watch_co", "strength": "strong"}])
            ids[f"P{i:02d}"] = cid
        for key, src, pub in (("yahoo", "yahoo", "2026-10-05"), ("edgar", "edgar", "2026-10-04")):
            rec, _ = T.normalize(raw(source=src, sid=key.upper(),
                                     title=f"Watch Filing {key}", published=pub), T.now_iso())
            cid, _ = store.upsert_candidate(rec, "rX")
            store.set_entities(cid, [{"entity_id": "watch_co", "strength": "strong"}])
            ids[key] = cid
        rec, _ = T.normalize(raw(source="gnews", sid="GN", title="Watch News Fresh",
                                 published="2026-10-19"), T.now_iso())
        cid_news, _ = store.upsert_candidate(rec, "rX")
        store.set_entities(cid_news, [{"entity_id": "watch_co", "strength": "strong"}])
        todo = TT._watch_todo(store, cfg, AS_OF)
        got = {item[0] for item in todo}
        self.assertEqual(len(todo), 15)          # 表示窓いっぱいまで
        self.assertIn(ids["yahoo"], got)         # 15件目 (窓の内側)
        self.assertNotIn(ids["edgar"], got)      # 16件目 (窓の外)
        self.assertNotIn(cid_news, got)          # ニュースは対象外
        store.close()
        tmp.cleanup()

    def test_mt_providers_and_fallback(self):
        import os
        from unittest import mock as um
        # DeepL: 無料キーは api-free ホスト、応答は入力と同じ並びで返る
        seen = {}
        def deepl_http(url, params=None, headers=None, data=None, **kw):
            seen["url"] = url
            texts = json.loads(data)["text"]
            return json.dumps({"translations": [{"text": "訳:" + t} for t in texts]})
        out = T.deepl_translate(["Title A", "Abstract A"], "key:fx", http=deepl_http)
        self.assertEqual(out, ["訳:Title A", "訳:Abstract A"])
        self.assertIn("api-free.deepl.com", seen["url"])
        # Azure: 配列形式の応答を展開する
        def azure_http(url, params=None, headers=None, data=None, **kw):
            return json.dumps([{"translations": [{"text": "訳:" + d["Text"]}]} for d in json.loads(data)])
        self.assertEqual(T.azure_translate(["X"], "k", http=azure_http), ["訳:X"])
        # 経路の優先順位: 本命 Azure が失敗したら DeepL、それも無ければ LLM。出典も記録される
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")
        rec, _ = T.normalize(raw(), T.now_iso())
        cid, _ = store.upsert_candidate(rec, "rX")
        store.conn.execute(
            "INSERT INTO evaluations(run_id, candidate_id, kind, status) VALUES('rX', ?, 'long', 'ok')", (cid,))
        store.conn.commit()

        def flaky_http(url, params=None, headers=None, data=None, **kw):
            if "microsofttranslator" in url:
                raise RuntimeError("azure down")
            return deepl_http(url, params=params, headers=headers, data=data)
        env = {"DEEPL_API_KEY": "k:fx", "AZURE_TRANSLATOR_KEY": "k2"}
        with um.patch.dict(os.environ, env, clear=False):
            n, stats, failed = T.translate_display_set(store, CFG, AS_OF, log=lambda *_: None, http=flaky_http)
        self.assertEqual(n, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(set(stats), {"deepl"})
        c = store.get_candidate(cid)
        self.assertTrue(c["title_ja"].startswith("訳:"))
        self.assertEqual(json.loads(c["translation_src"]), {"title": "deepl", "abstract": "deepl"})
        store.close()
        tmp.cleanup()

    def test_usage_thresholds_and_totals(self):
        self.assertEqual(T.crossed_thresholds(400_000, 550_000), [500_000])
        self.assertEqual(T.crossed_thresholds(500_000, 500_000), [])
        self.assertEqual(T.crossed_thresholds(900_000, 2_100_000),
                         [1_000_000, 1_250_000, 1_500_000, 1_750_000, 2_000_000])
        # 集計: azure/google は当月のみ、deepl は通算 (一回限り枠のため)
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")

        def add_run(started, chars):
            store.conn.execute("INSERT INTO runs(run_id, started_at, counts_json) VALUES(?, ?, ?)",
                               (started, started, json.dumps({"translation_chars": chars})))
        add_run("2026-09-15T00:00:00Z", {"azure": 700_000, "deepl": 300_000, "google": 10_000})
        add_run("2026-10-05T00:00:00Z", {"azure": 200_000, "deepl": 100_000, "google": 5_000})
        store.conn.commit()
        self.assertEqual(T.usage_totals(store, AS_OF), {"azure": 200_000, "deepl": 400_000, "google": 5_000})
        store.close()
        tmp.cleanup()

    def test_trials(self):
        r = trials.parse(TRIALS_DATA)[0]
        self.assertEqual((r["source"], r["info_type"], r["published"]), ("trials", "trial", "2026-10-01"))
        self.assertEqual(r["url"], "https://clinicaltrials.gov/study/NCT00000001")
        self.assertIn("Status: RECRUITING", r["abstract"])
        self.assertEqual(r["affiliations"], ["PeptiDream Inc."])

    def test_trials_results_posted(self):
        data = json.loads(json.dumps(TRIALS_DATA))
        data["studies"][0]["hasResults"] = True
        r = trials.parse(data)[0]
        self.assertIn("Results: Posted", r["abstract"])

    def test_edinet_sec_code_and_parse(self):
        self.assertEqual(edinet.sec_code("9348"), "93480")
        self.assertEqual(edinet.sec_code("186A"), "186A0")
        out = edinet.parse(EDINET_RESULTS, "9348")
        self.assertEqual(len(out), 1)  # 他社と取下げを除外
        self.assertEqual(out[0]["info_type"], "disclosure")
        self.assertIn("臨時報告書", out[0]["abstract"])

    def test_edinet_without_key_raises(self):
        # キー未設定を成功・0件で握りつぶさない (監査 A-3: 財務系の休止判定の誤作動防止)
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EDINET_API_KEY", None)
            called = []
            with self.assertRaises(RuntimeError):
                edinet.fetch("9348", 20, 30, AS_OF, http=lambda *a, **k: called.append(a) or b"{}")
        self.assertEqual(called, [])


def fin_csv_bytes():
    """EDINET type=5 の CSV 形式 (UTF-16・タブ区切り・全項目クォート) の最小再現。"""
    header = ["要素ID", "項目名", "コンテキストID", "相対年度", "連結・個別", "期間・時点", "ユニットID", "通貨", "単位", "値"]
    rows = [
        ["jpsps:NetSalesSummaryOfBusinessResults", "売上高", "CurrentYearDuration", "当期", "連結", "期間", "JPY", "JPY", "円", "12345000000"],
        ["jpsps:NetSalesSummaryOfBusinessResults", "売上高", "PriorYearDuration", "前期", "連結", "期間", "JPY", "JPY", "円", "10000000000"],
        ["jpsps:NetSalesSummaryOfBusinessResults", "売上高", "CurrentYearDuration_NonConsolidatedMember", "当期", "個別", "期間", "JPY", "JPY", "円", "99999000000"],
        ["jpsps:OperatingIncomeLossSummaryOfBusinessResults", "営業利益", "CurrentYearDuration", "当期", "連結", "期間", "JPY", "JPY", "円", "-500000000"],
        ["jpsps:CashAndCashEquivalentsSummaryOfBusinessResults", "現金及び現金同等物", "CurrentYearInstant", "当期", "連結", "時点", "JPY", "JPY", "円", "30000000000"],
        ["jpsps:EquityToAssetRatioSummaryOfBusinessResults", "自己資本比率", "CurrentYearInstant", "当期", "連結", "時点", "pure", "", "", "0.512"],
    ]
    lines = ["\t".join(f'"{c}"' for c in header)] + ["\t".join(f'"{c}"' for c in r) for r in rows]
    return ("\r\n".join(lines) + "\r\n").encode("utf-16")


FIN_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.w3.org/2003/xbrl/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
  xmlns:jpsps="http://example.jp/jpsps" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<xbrli:context id="CurrentYearDuration"><xbrli:entity><xbrli:identifier scheme="s">E00001</xbrli:identifier></xbrli:entity>
<xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2027-03-31</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:context id="CurrentYearInstant"><xbrli:entity><xbrli:identifier scheme="s">E00001</xbrli:identifier></xbrli:entity>
<xbrli:period><xbrli:instant>2027-03-31</xbrli:instant></xbrli:period></xbrli:context>
<xbrli:context id="CurrentYearInstant_NonConsolidatedMember"><xbrli:entity><xbrli:identifier scheme="s">E00001</xbrli:identifier>
<xbrli:segment><xbrldi:explicitMember dimension="d">jpcrp:NonConsolidatedMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
<xbrli:period><xbrli:instant>2027-03-31</xbrli:instant></xbrli:period></xbrli:context>
<jpsps:NetSalesSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPY" decimals="-6">12345000000</jpsps:NetSalesSummaryOfBusinessResults>
<jpsps:NetAssetsSummaryOfBusinessResults contextRef="CurrentYearInstant" unitRef="JPY" decimals="-6">8000000000</jpsps:NetAssetsSummaryOfBusinessResults>
<jpsps:NetAssetsSummaryOfBusinessResults contextRef="CurrentYearInstant_NonConsolidatedMember" unitRef="JPY" decimals="-6">9999999999</jpsps:NetAssetsSummaryOfBusinessResults>
</xbrli:xbrl>"""


class EdinetFinancialTests(unittest.TestCase):
    def test_csv_facts_select_format(self):
        facts = edinet.parse_facts_csv(fin_csv_bytes())
        sel = edinet.select_financials(facts)
        self.assertEqual(sel["売上高"]["当期"], 12345000000.0)  # 個別の大きな値でなく連結を選ぶ
        self.assertEqual(sel["売上高"]["前期"], 10000000000.0)
        text = edinet.format_financials(sel)
        self.assertIn("売上高 12,345百万円(前期 10,000百万円)", text)
        self.assertIn("営業利益 △500百万円", text)
        self.assertIn("現金及び現金同等物 30,000百万円", text)
        self.assertIn("自己資本比率 51.2%", text)

    def test_xbrl_facts_fallback(self):
        facts = edinet.parse_facts_xbrl(FIN_XML)
        sel = edinet.select_financials(facts)
        self.assertEqual(sel["売上高"]["当期"], 12345000000.0)
        self.assertEqual(sel["純資産"]["当期"], 8000000000.0)  # 非連結メンバーは選ばない

    def test_fetch_enriches_financial_doc(self):
        import os
        from unittest import mock
        from trlcore.sources import edinet as edinet_mod
        edinet_mod._day_cache.clear()
        edinet_mod._doc_cache.clear()
        edinet_mod._index_cache.clear()
        fin_doc = {"docID": "S100FIN", "edinetCode": "E00001", "secCode": "93480", "filerName": "株式会社ispace",
                   "docDescription": "第2四半期報告書", "docTypeCode": "140", "xbrlFlag": "1",
                   "submitDateTime": "2026-10-19 15:00", "periodStart": "2026-04-01",
                   "periodEnd": "2026-09-30", "withdrawalStatus": "0"}

        def fake_http(url, params=None, headers=None, **kw):
            if url.endswith("documents.json"):
                if params.get("date") == "2026-10-19":
                    return json.dumps({"results": [fin_doc]}).encode()
                return json.dumps({"results": []}).encode()
            if "/documents/S100FIN" in url:
                return fin_csv_bytes()
            raise AssertionError(url)

        with mock.patch.dict(os.environ, {"EDINET_API_KEY": "test-key"}):
            out = edinet.fetch("9348", 20, 30, AS_OF, http=fake_http, sleep=lambda *_: None)
        self.assertEqual(len(out), 1)
        self.assertIn("財務数値(EDINET XBRL)", out[0]["abstract"])
        self.assertIn("売上高 12,345百万円", out[0]["abstract"])

    def test_fetch_financial_failure_falls_back_to_metadata(self):
        import os
        from unittest import mock
        from trlcore.sources import edinet as edinet_mod
        edinet_mod._day_cache.clear()
        edinet_mod._doc_cache.clear()
        edinet_mod._index_cache.clear()
        fin_doc = {"docID": "S100FIN", "edinetCode": "E00001", "secCode": "93480", "filerName": "株式会社ispace",
                   "docDescription": "第2四半期報告書", "docTypeCode": "140", "xbrlFlag": "1",
                   "submitDateTime": "2026-10-19 15:00", "periodStart": "2026-04-01",
                   "periodEnd": "2026-09-30", "withdrawalStatus": "0"}

        def fake_http(url, params=None, headers=None, **kw):
            if url.endswith("documents.json"):
                if params.get("date") == "2026-10-19":
                    return json.dumps({"results": [fin_doc]}).encode()
                return json.dumps({"results": []}).encode()
            raise RuntimeError("HTTP 500")

        with mock.patch.dict(os.environ, {"EDINET_API_KEY": "test-key"}):
            out = edinet.fetch("9348", 20, 30, AS_OF, http=fake_http, sleep=lambda *_: None)
        self.assertEqual(len(out), 1)
        self.assertNotIn("財務数値", out[0]["abstract"])

    def test_fetch_uses_shared_sec_index(self):
        # 日次リストは証券コード別の索引として共有し、クエリごとに全件走査しない
        import os
        from unittest import mock
        from trlcore.sources import edinet as edinet_mod
        edinet_mod._day_cache.clear()
        edinet_mod._doc_cache.clear()
        edinet_mod._index_cache.clear()
        foreign = [{"docID": f"S100F{i}", "edinetCode": "E99999", "secCode": "99990",
                    "filerName": "他社", "docDescription": "有価証券報告書", "docTypeCode": "120",
                    "xbrlFlag": "1", "submitDateTime": "2026-10-19 15:00", "withdrawalStatus": "0"}
                   for i in range(50)]
        own = {"docID": "S100OWN", "edinetCode": "E00001", "secCode": "93480", "filerName": "株式会社ispace",
               "docDescription": "四半期報告書", "docTypeCode": "140", "xbrlFlag": "1",
               "submitDateTime": "2026-10-19 15:00", "withdrawalStatus": "0"}

        def fake_http(url, params=None, headers=None, **kw):
            if url.endswith("documents.json"):
                if params.get("date") == "2026-10-19":
                    return json.dumps({"results": foreign + [own]}).encode()
                return json.dumps({"results": []}).encode()
            raise RuntimeError("no doc download expected")

        seen_sizes = []
        orig_parse = edinet_mod.parse_results

        def counting(results, ticker):
            seen_sizes.append(len(results))
            return orig_parse(results, ticker)
        edinet_mod.parse_results = counting
        try:
            with mock.patch.dict(os.environ, {"EDINET_API_KEY": "test-key"}):
                out1 = edinet.fetch("9348", 20, 30, AS_OF, http=fake_http, sleep=lambda *_: None)
                out2 = edinet.fetch("7777", 20, 30, AS_OF, http=fake_http, sleep=lambda *_: None)
        finally:
            edinet_mod.parse_results = orig_parse
        self.assertEqual(len(out1), 1)
        self.assertEqual(len(out2), 0)
        # 索引のスライスだけが parse に渡る (他社の50件は混ざらない)
        self.assertEqual(seen_sizes, [1, 0])


YAHOO_SUMMARY = {
    "quoteSummary": {"result": [{
        "incomeStatementHistoryQuarterly": {"incomeStatementHistory": [
            {"endDate": {"raw": 1782777600, "fmt": "2026-06-30"},
             "totalRevenue": {"raw": 168000000}, "operatingIncome": {"raw": -5500000000},
             "netIncome": {"raw": -6336000000}}]},
        "incomeStatementHistory": {"incomeStatementHistory": [
            {"endDate": {"raw": 1743379200, "fmt": "2025-03-31"},
             "totalRevenue": {"raw": 1000000000}, "netIncome": {"raw": -9000000000}}]},
        "balanceSheetHistory": {"balanceSheetStatements": [
            {"endDate": {"raw": 1743379200, "fmt": "2025-03-31"},
             "cash": {"raw": 25700000000}, "totalAssets": {"raw": 40000000000},
             "totalStockholderEquity": {"raw": 10000000000},
             "shortLongTermDebt": {"raw": 5000000000}, "longTermDebt": {"raw": 24760000000}}]},
        "cashflowStatementHistoryQuarterly": {"cashflowStatements": [
            {"endDate": {"raw": 1782777600, "fmt": "2026-06-30"},
             "totalCashFromOperatingActivities": {"raw": -4000000000}}]},
        "financialData": {"totalCash": {"raw": 25700000000}, "totalDebt": {"raw": 29760000000},
                          "currentRatio": {"raw": 3.89}},
        "quoteType": {"longName": "ispace, Inc.", "shortName": "ispace"},
    }], "error": None}}


class YahooFinancialTests(unittest.TestCase):
    def setUp(self):
        from trlcore.sources import yahoo as yahoo_mod
        s = yahoo_mod._sess()
        s["crumb"] = None
        s["opener"] = None
        s["ua"] = None
        yahoo_mod._429_count[0] = 0
        yahoo_mod._cooldown_until[0] = 0.0

    def test_session_ua_from_pool_and_stable(self):
        from trlcore.sources import yahoo as yahoo_mod
        ua1 = yahoo_mod._session_ua()
        self.assertIn(ua1, yahoo_mod._UA_POOL)
        self.assertEqual(yahoo_mod._session_ua(), ua1)  # セッション内では変わらない

    def test_parse_quote_summary(self):
        text, period, name = yahoo.parse(YAHOO_SUMMARY, "9348.T")
        self.assertEqual(period, "2026-06-30")
        self.assertEqual(name, "ispace, Inc.")
        self.assertIn("売上高 168百万円", text)
        self.assertIn("営業利益 △5,500百万円", text)
        self.assertIn("自己資本比率 25.0%", text)
        self.assertIn("現預金 25,700百万円", text)
        self.assertIn("流動比率 3.89", text)

    def test_parse_quote_summary_currency_from_suffix(self):
        # price.currency が無くてもティッカーのサフィックスから通貨を推定する
        text, _, _ = yahoo.parse(YAHOO_SUMMARY, "XYZ")  # サフィックス無し = 米国
        self.assertIn("売上高 168百万米ドル", text)
        text, _, _ = yahoo.parse(YAHOO_SUMMARY, "ABC.L")
        self.assertIn("売上高 168百万英ポンド", text)

    def test_parse_quote_summary_unknown_currency_skips(self):
        # 通貨が特定できない財務は、単位を偽って記録しない
        self.assertEqual(yahoo.parse(YAHOO_SUMMARY), ("", None, ""))
        self.assertEqual(yahoo.parse(YAHOO_SUMMARY, "ABC.XX"), ("", None, ""))

    def test_parse_quote_summary_foreign_currency(self):
        import copy
        data = copy.deepcopy(YAHOO_SUMMARY)
        data["quoteSummary"]["result"][0]["price"] = {"currency": "USD"}
        text, _, _ = yahoo.parse(data)
        self.assertIn("売上高 168百万米ドル", text)
        self.assertNotIn("百万円", text)

    def test_fetch_builds_disclosure_candidate(self):
        def fake_http(url, params=None, headers=None, **kw):
            if "getcrumb" in url:
                return b"crumb123"
            if "fc.yahoo.com" in url:
                raise RuntimeError("404")
            if "quoteSummary" in url:
                return json.dumps(YAHOO_SUMMARY).encode()
            raise AssertionError(url)

        out = yahoo.fetch("9348.T", 5, 400, AS_OF, http=fake_http, sleep=lambda *_: None)
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertEqual((r["source"], r["info_type"], r["source_id"]), ("yahoo", "disclosure", "9348.T-2026-06-30"))
        self.assertIn("財務数値(Yahoo Finance)", r["abstract"])
        self.assertEqual(r["affiliations"], ["ispace, Inc."])

    def test_fetch_raises_after_retries_and_backs_off(self):
        # 通信が2回とも失敗したら空結果で握りつぶさず例外にする (収集側で失敗として記録される)
        from trlcore.sources import yahoo as yh
        waits = []

        def fake_http(url, params=None, headers=None, **kw):
            if "getcrumb" in url:
                return b"crumb123"
            if "fc.yahoo.com" in url:
                raise RuntimeError("404")
            raise RuntimeError("429 Too Many Requests")

        yh._last_call[0] = 0.0
        with self.assertRaises(RuntimeError):
            yahoo.fetch("9348.T", 5, 400, AS_OF, http=fake_http, sleep=waits.append)
        self.assertIn(2, waits)  # 再試行前にバックオフする
        yh._last_call[0] = 0.0

    def test_fetch_429_no_double_retry_and_sets_cooldown(self):
        # http_fetch の再試行をすり抜けた 429 は、この層では再試行せず
        # プロセス全体の休止を設定して即座に例外にする
        from trlcore.sources import yahoo as yh
        from trlcore.errors import HttpError
        calls = []

        def fake_http(url, params=None, headers=None, **kw):
            if "getcrumb" in url:
                return b"crumb123"
            if "fc.yahoo.com" in url:
                raise RuntimeError("404")
            calls.append(url)
            raise HttpError("HTTP 429 for url", 429, None)

        yh._last_call[0] = 0.0
        yh._cooldown_until[0] = 0.0
        try:
            with self.assertRaises(HttpError):
                yahoo.fetch("9348.T", 5, 400, AS_OF, http=fake_http, sleep=lambda *_: None)
            self.assertEqual(len(calls), 1)  # quoteSummary は1回だけ (二重再試行なし)
            self.assertGreater(yh._cooldown_until[0], 0.0)
        finally:
            yh._cooldown_until[0] = 0.0
            yh._last_call[0] = 0.0

    def test_wait_cooldown_sleeps_until_deadline(self):
        from trlcore.sources import yahoo as yh
        waits = []
        yh._cooldown_until[0] = 1030.0
        try:
            yh._wait_cooldown(waits.append, now=lambda: 1000.0)
            self.assertEqual(waits, [30.0])
            yh._wait_cooldown(waits.append, now=lambda: 2000.0)
            self.assertEqual(waits, [30.0])  # 期限を過ぎていれば待たない
        finally:
            yh._cooldown_until[0] = 0.0

    def test_rate_limit_constants_widened_and_shortened(self):
        # 2026-11-03 の方針: 間隔を広げて 429 の発生を抑え、全体休止は短くする
        from trlcore.sources import yahoo as yh
        self.assertEqual(yh.MIN_INTERVAL, 1.5)
        self.assertEqual(yh.COOLDOWN_SEC, 30.0)
        yh._cooldown_until[0] = 0.0
        try:
            yh._note_429(None, now=lambda: 1000.0)
            self.assertEqual(yh._cooldown_until[0], 1030.0)  # 既定は30秒先まで
            yh._note_429(120.0, now=lambda: 1000.0)
            self.assertEqual(yh._cooldown_until[0], 1120.0)  # Retry-After が長ければそちら
        finally:
            yh._cooldown_until[0] = 0.0

    def test_circuit_breaker_opens_after_limit(self):
        # 429 失敗が上限に達したら、その後の Yahoo 取得は通信せず即失敗になる
        from trlcore.sources import yahoo as yh
        from trlcore.errors import HttpError
        calls = []

        def fake_http(url, params=None, headers=None, **kw):
            if "getcrumb" in url:
                return b"crumb123"
            if "fc.yahoo.com" in url:
                raise RuntimeError("404")
            calls.append(url)
            raise HttpError("HTTP 429 for url", 429, None)

        yh._last_call[0] = 0.0
        try:
            for _ in range(yh.CIRCUIT_LIMIT):
                with self.assertRaises(HttpError):
                    yahoo.fetch("9348.T", 5, 400, AS_OF, http=fake_http, sleep=lambda *_: None)
            self.assertEqual(yh._429_count[0], yh.CIRCUIT_LIMIT)
            self.assertEqual(len(calls), yh.CIRCUIT_LIMIT)
            with self.assertRaises(HttpError):
                yahoo.fetch("9348.T", 5, 400, AS_OF, http=fake_http, sleep=lambda *_: None)
            self.assertEqual(len(calls), yh.CIRCUIT_LIMIT)  # 通信していない
        finally:
            yh._last_call[0] = 0.0

    def test_fetch_passes_single_retry_to_http_fetch(self):
        # Yahoo 経路の http 試行は2回まで (retries=1)。他ソースの既定には触らない
        from trlcore.sources import yahoo as yh
        seen = []
        orig = yh.http_fetch

        def stub(url, params=None, headers=None, opener=None, **kw):
            seen.append(kw.get("retries"))
            if "getcrumb" in url:
                return b"crumb123"
            if "fc.yahoo.com" in url:
                return b""
            return json.dumps(YAHOO_SUMMARY).encode()

        yh.http_fetch = stub
        yh._last_call[0] = 0.0
        try:
            out = yahoo.fetch("9348.T", 5, 400, AS_OF, sleep=lambda *_: None)
        finally:
            yh.http_fetch = orig
            yh._last_call[0] = 0.0
        self.assertEqual(len(out), 1)
        self.assertTrue(seen and all(r == 1 for r in seen))

    def test_fetch_source_id_distinguishes_same_ticker_markets(self):
        # NANO.TO (Nano One) と NANO.PA (Nanobiotix) のように市場違いの同名ティッカーで
        # source_id が衝突すると、一方の財務レコードが他方へ統合されて消える
        def fake_http(url, params=None, headers=None, **kw):
            if "getcrumb" in url:
                return b"crumb123"
            if "fc.yahoo.com" in url:
                raise RuntimeError("404")
            return json.dumps(YAHOO_SUMMARY).encode()

        a = yahoo.fetch("NANO.TO", 5, 400, AS_OF, http=fake_http, sleep=lambda *_: None)
        b = yahoo.fetch("NANO.PA", 5, 400, AS_OF, http=fake_http, sleep=lambda *_: None)
        self.assertEqual(a[0]["source_id"], "NANO.TO-2026-06-30")
        self.assertEqual(b[0]["source_id"], "NANO.PA-2026-06-30")
        self.assertNotEqual(a[0]["source_id"], b[0]["source_id"])

    def test_fetch_stale_period_returns_nothing(self):
        def fake_http(url, params=None, headers=None, **kw):
            if "getcrumb" in url:
                return b"crumb123"
            if "fc.yahoo.com" in url:
                raise RuntimeError("404")
            return json.dumps(YAHOO_SUMMARY).encode()

        out = yahoo.fetch("9348.T", 5, 400, date(2028, 1, 1), http=fake_http, sleep=lambda *_: None)
        self.assertEqual(out, [])


class EntityQueryGenerationTests(unittest.TestCase):
    # 移行前の手書きクエリ全27件。生成結果がこれと完全一致すること(移行の等価性)。
    GOLDEN = [
    {
        "arxiv": "all:\"solid-state battery\"",
        "name": "solid-state-battery"
    },
    {
        "arxiv": "all:\"quantum error correction\"",
        "name": "quantum-error-correction"
    },
    {
        "name": "gene-therapy",
        "pubmed": "gene therapy[Title/Abstract] AND clinical trial[Publication Type]"
    },
    {
        "gnews": "アストロスケール (決算 OR 受注 OR 契約 OR 提携 OR 資金調達 OR 業績 OR 採択)",
        "max_results": 10,
        "name": "astroscale-news"
    },
    {
        "gnews": "ispace (決算 OR 受注 OR 契約 OR 提携 OR 資金調達 OR 業績 OR 採択 OR 月面)",
        "max_results": 10,
        "name": "ispace-news"
    },
    {
        "gnews": "Synspective (決算 OR 受注 OR 契約 OR 提携 OR 資金調達 OR 業績 OR 採択 OR 衛星)",
        "max_results": 10,
        "name": "synspective-news"
    },
    {
        "gnews": "オンコリスバイオファーマ (決算 OR 契約 OR 提携 OR 導出 OR 承認 OR 治験 OR 資金調達 OR 採択 OR 助言)",
        "max_results": 10,
        "name": "oncolys-news"
    },
    {
        "gnews": "リプロセル (決算 OR 契約 OR 提携 OR 導出 OR 承認 OR 治験 OR 資金調達 OR 採択 OR 助言)",
        "max_results": 10,
        "name": "reprocell-news"
    },
    {
        "gnews": "ペプチドリーム (決算 OR 契約 OR 提携 OR 導出 OR 承認 OR 治験 OR 資金調達 OR 採択 OR 助言)",
        "max_results": 10,
        "name": "peptidream-news"
    },
    {
        "lookback_days": 180,
        "name": "oncolys-trials",
        "trials": "Oncolys BioPharma"
    },
    {
        "lookback_days": 180,
        "name": "reprocell-trials",
        "trials": "REPROCELL"
    },
    {
        "lookback_days": 180,
        "name": "peptidream-trials",
        "trials": "PeptiDream"
    },
    {
        "edinet": "186A",
        "lookback_days": 45,
        "name": "astroscale-edinet"
    },
    {
        "edinet": "9348",
        "lookback_days": 45,
        "name": "ispace-edinet"
    },
    {
        "edinet": "290A",
        "lookback_days": 45,
        "name": "synspective-edinet"
    },
    {
        "edinet": "4587",
        "lookback_days": 45,
        "name": "peptidream-edinet"
    },
    {
        "name": "astroscale-prtimes",
        "prtimes": "Astroscale|アストロスケール",
        "lookback_days": 90
    },
    {
        "name": "ispace-prtimes",
        "prtimes": "ispace",
        "lookback_days": 90
    },
    {
        "name": "synspective-prtimes",
        "prtimes": "Synspective|シンスペクティブ",
        "lookback_days": 90
    },
    {
        "name": "oncolys-prtimes",
        "prtimes": "Oncolys|オンコリス",
        "lookback_days": 90
    },
    {
        "name": "reprocell-prtimes",
        "prtimes": "REPROCELL|リプロセル",
        "lookback_days": 90
    },
    {
        "name": "peptidream-prtimes",
        "prtimes": "PeptiDream|ペプチドリーム",
        "lookback_days": 90
    },
    {
        "name": "peptidream-pubmed",
        "pubmed": "PeptiDream[Affiliation]",
        "lookback_days": 365
    },
    {
        "name": "ispace-openalex",
        "openalex": "I4405267988",
        "lookback_days": 365
    }
]

    def test_generated_queries_match_golden(self):
        # 移行前の手書き27件が、生成結果の中に同じ相対順序で全て含まれること(企業追加後も不変)。
        qs = CFG["sources"]["queries"]
        it = iter(qs)
        for g in self.GOLDEN:
            self.assertTrue(any(q == g for q in it), f"golden query missing or out of order: {g['name']}")

    def src(self):
        return {"keyword_sets": {"space": ["決算", "受注"], "bio": ["治験"]},
                "entity_watch": {"lookback_days": {"prtimes": 90, "trials": 365, "edinet": 60}}}

    def test_generation_rules(self):
        ents = {"entities": [
            {"id": "a", "sector": "space", "ticker": "1000",
             "watch": {"news_name": "A社", "extra_keywords": ["月面"]}},
            {"id": "b", "sector": "bio",
             "watch": {"news_name": "B社", "prtimes_terms": ["B", "ビー"], "sponsor": "B Inc"}},
            {"id": "c"},
        ]}
        qs = T.build_entity_queries(ents, self.src())
        by_name = {q["name"]: q for q in qs}
        self.assertEqual(by_name["a-news"]["gnews"], "A社 (決算 OR 受注 OR 月面)")
        self.assertEqual(by_name["a-prtimes"]["prtimes"], "A社")  # prtimes_terms 省略時は news_name
        self.assertEqual(by_name["a-edinet"]["edinet"], "1000")
        self.assertNotIn("a-trials", by_name)  # sponsor が無い
        self.assertEqual(by_name["b-trials"]["trials"], "B Inc")
        self.assertEqual(by_name["b-prtimes"]["prtimes"], "B|ビー")
        self.assertNotIn("b-edinet", by_name)  # ticker が無い
        self.assertFalse(any(n.startswith("c-") for n in by_name))  # watch が無い企業は生成しない

    def test_gnews_queries_carry_max_results_cap(self):
        # gnews の取得上限 (10) は生成時に埋め込む。他ソースのクエリには付けない
        ents = {"entities": [
            {"id": "a", "sector": "space", "ticker": "1000",
             "watch": {"news_name": "A社"}},
            {"id": "u", "sector": "space", "market": "US",
             "watch": {"news_name": "U Inc"}},
        ]}
        src = {"keyword_sets": {"space": ["決算"]},
               "keyword_sets_en": {"space": ["earnings"]},
               "entity_watch": {"lookback_days": {}}}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        self.assertEqual(by_name["a-news"]["max_results"], 10)
        self.assertEqual(by_name["u-news"]["max_results"], 10)
        self.assertNotIn("max_results", by_name["a-edinet"])

    def test_yahoo_queries_core_only(self):
        # Yahoo はコア企業のみ生成する (ウォッチは評価対象でなく、取得リクエスト削減のため)
        src = {"keyword_sets": {"space": ["決算"]},
               "keyword_sets_en": {"space": ["earnings"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "core1", "sector": "space", "market": "US", "ticker": "CORE", "tier": "core",
             "watch": {"news_name": "Core Corp"}},
            {"id": "watch1", "sector": "space", "market": "US", "ticker": "WATC", "tier": "watch",
             "watch": {"news_name": "Watch Corp"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        self.assertIn("core1-yahoo", by_name)
        self.assertNotIn("watch1-yahoo", by_name)
        self.assertIn("watch1-edgar", by_name)  # EDGAR は今回の削減対象外

    def test_edgar_queries_for_foreign_20f_filers(self):
        # 米国以外でも edgar_ticker (米上場ティッカー) を明示した企業は EDGAR 対象にする
        src = {"keyword_sets": {"bio": ["治験"]},
               "keyword_sets_en": {"bio": ["trial"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "fr20f", "sector": "bio", "market": "FR", "ticker": "IPH",
             "edgar_ticker": "IPHA", "tier": "core",
             "watch": {"news_name": "Innate-like"}},
            {"id": "frplain", "sector": "bio", "market": "FR", "ticker": "NANO",
             "tier": "core",
             "watch": {"news_name": "Plain SA"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        self.assertEqual(by_name["fr20f-edgar"]["edgar"], "IPHA")
        self.assertNotIn("frplain-edgar", by_name)  # 明示が無ければ従来どおり対象外

    def test_edinet_queries_core_only(self):
        # EDINET もコア企業のみ生成する (Yahoo と同じ扱い)
        src = {"keyword_sets": {"space": ["決算"]},
               "keyword_sets_en": {"space": ["earnings"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "jpcore", "sector": "space", "market": "JP", "ticker": "1111", "tier": "core",
             "watch": {"news_name": "コア社"}},
            {"id": "jpwatch", "sector": "space", "market": "JP", "ticker": "2222", "tier": "watch",
             "watch": {"news_name": "ウォッチ社"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        self.assertIn("jpcore-edinet", by_name)
        self.assertNotIn("jpwatch-edinet", by_name)

    def test_generation_rules_foreign_market(self):
        src = {"keyword_sets": {"space": ["決算"]},
               "keyword_sets_en": {"space": ["earnings", "contract"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "us1", "sector": "space", "market": "US", "ticker": "ABC",
             "watch": {"news_name": "ABC Corp", "extra_keywords": ["rocket"]}},
            {"id": "gb1", "sector": "space", "market": "GB", "ticker": "XYZ",
             "watch": {"news_name": "XYZ plc"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        q = by_name["us1-news"]["gnews"]
        self.assertEqual((q["hl"], q["gl"], q["ceid"]), ("en", "US", "US:en"))
        self.assertEqual(q["q"], "ABC Corp (earnings OR contract OR rocket)")
        self.assertEqual(by_name["us1-yahoo"]["yahoo"], "ABC")     # 米は接尾辞なし
        self.assertEqual(by_name["gb1-yahoo"]["yahoo"], "XYZ.L")   # 英は .L
        self.assertNotIn("us1-edinet", by_name)   # EDINET は JP のみ
        self.assertNotIn("us1-prtimes", by_name)  # PR TIMES も JP のみ

    def test_generation_rules_german_market(self):
        src = {"keyword_sets": {"space": ["決算"]},
               "keyword_sets_en": {"space": ["earnings"]},
               "keyword_sets_de": {"space": ["Ergebnisse", "Auftrag"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "de1", "sector": "space", "market": "DE", "ticker": "OHB",
             "watch": {"news_name": "OHB", "extra_keywords": ["Satelliten"]}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        q = by_name["de1-news"]["gnews"]
        self.assertEqual((q["hl"], q["gl"], q["ceid"]), ("de", "DE", "DE:de"))
        self.assertEqual(q["q"], "OHB (Ergebnisse OR Auftrag OR Satelliten)")  # 独語キーワード集合を使う
        self.assertEqual(by_name["de1-yahoo"]["yahoo"], "OHB.DE")
        self.assertNotIn("de1-edinet", by_name)
        self.assertNotIn("de1-prtimes", by_name)

    def test_generation_rules_canada_australia(self):
        src = {"keyword_sets": {"space": ["決算"]},
               "keyword_sets_en": {"space": ["earnings", "contract"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "ca1", "sector": "space", "market": "CA", "ticker": "MDA",
             "watch": {"news_name": "MDA Space"}},
            {"id": "au1", "sector": "space", "market": "AU", "ticker": "EOS",
             "watch": {"news_name": "EOS Ltd"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        self.assertEqual(by_name["ca1-news"]["gnews"]["gl"], "CA")
        self.assertEqual(by_name["ca1-yahoo"]["yahoo"], "MDA.TO")
        self.assertEqual(by_name["au1-news"]["gnews"]["ceid"], "AU:en")
        self.assertEqual(by_name["au1-yahoo"]["yahoo"], "EOS.AX")
        self.assertNotIn("au1-prtimes", by_name)

    def test_generation_rules_canada_tsxv(self):
        # カナダは取引所で接尾辞が分かれる (TSX=.TO / TSXV=.V)。辞書の exchange で判定する
        src = {"keyword_sets": {"robotics": ["決算"]},
               "keyword_sets_en": {"robotics": ["earnings"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "ca_tsx", "sector": "robotics", "market": "CA", "ticker": "MDA",
             "exchange": "TSX", "watch": {"news_name": "MDA Space"}},
            {"id": "ca_tsxv", "sector": "robotics", "market": "CA", "ticker": "PNG",
             "exchange": "TSXV", "watch": {"news_name": "Kraken Robotics"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        self.assertEqual(by_name["ca_tsx-yahoo"]["yahoo"], "MDA.TO")
        self.assertEqual(by_name["ca_tsxv-yahoo"]["yahoo"], "PNG.V")

    def test_generation_rules_italy_sweden(self):
        src = {"keyword_sets": {"space": ["決算"]},
               "keyword_sets_it": {"space": ["risultati", "lancio"]},
               "keyword_sets_sv": {"space": ["resultat", "uppskjutning"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "it1", "sector": "space", "market": "IT", "ticker": "AVIO",
             "watch": {"news_name": "Avio"}},
            {"id": "se1", "sector": "space", "market": "SE", "ticker": "AAC",
             "watch": {"news_name": "AAC Clyde Space"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        self.assertEqual(by_name["it1-news"]["gnews"]["q"], "Avio (risultati OR lancio)")
        self.assertEqual(by_name["it1-yahoo"]["yahoo"], "AVIO.MI")
        self.assertEqual(by_name["se1-news"]["gnews"]["hl"], "sv")
        self.assertEqual(by_name["se1-yahoo"]["yahoo"], "AAC.ST")

    def test_generation_rules_denmark(self):
        # デンマークは Google News のデンマーク語圏の収載が薄いため英語集合+DKロケールを使う
        src = {"keyword_sets": {"bio": ["治験"]},
               "keyword_sets_en": {"bio": ["earnings", "clinical trial"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "dk1", "sector": "bio", "market": "DK", "ticker": "GMAB",
             "watch": {"news_name": "Genmab", "sponsor": "Genmab"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        q = by_name["dk1-news"]["gnews"]
        self.assertEqual((q["hl"], q["gl"]), ("en", "DK"))
        self.assertEqual(q["q"], "Genmab (earnings OR clinical trial)")
        self.assertEqual(by_name["dk1-yahoo"]["yahoo"], "GMAB.CO")
        self.assertEqual(by_name["dk1-trials"]["trials"], "Genmab")

    def test_generation_rules_spain(self):
        # スペインも英語集合+現地ロケール方式。Yahoo は .MC、通貨はユーロ圏
        src = {"keyword_sets": {"bio": ["治験"]},
               "keyword_sets_en": {"bio": ["earnings", "clinical trial"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "es1", "sector": "bio", "market": "ES", "ticker": "PHM",
             "watch": {"news_name": "PharmaMar", "sponsor": "PharmaMar"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        q = by_name["es1-news"]["gnews"]
        self.assertEqual((q["hl"], q["gl"], q["ceid"]), ("en", "ES", "ES:en"))
        self.assertEqual(q["q"], "PharmaMar (earnings OR clinical trial)")
        self.assertEqual(by_name["es1-yahoo"]["yahoo"], "PHM.MC")
        self.assertEqual(by_name["es1-trials"]["trials"], "PharmaMar")

    def test_generation_rules_france_norway(self):
        # フランス/ノルウェーも英語集合+現地ロケール。Yahoo 接尾辞は .PA / .OL
        src = {"keyword_sets": {"semiconductor": ["治験"]},
               "keyword_sets_en": {"semiconductor": ["earnings", "wafer"]},
               "entity_watch": {"lookback_days": {}}}
        ents = {"entities": [
            {"id": "fr1", "sector": "semiconductor", "market": "FR", "ticker": "SOI",
             "watch": {"news_name": "Soitec"}},
            {"id": "no1", "sector": "semiconductor", "market": "NO", "ticker": "NOD",
             "watch": {"news_name": "Nordic Semiconductor"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        q = by_name["fr1-news"]["gnews"]
        self.assertEqual((q["hl"], q["gl"], q["ceid"]), ("en", "FR", "FR:en"))
        self.assertEqual(by_name["fr1-yahoo"]["yahoo"], "SOI.PA")
        self.assertEqual(by_name["no1-yahoo"]["yahoo"], "NOD.OL")
        self.assertEqual(by_name["no1-news"]["gnews"]["q"],
                         "Nordic Semiconductor (earnings OR wafer)")

    def test_generation_rules_openalex(self):
        src = {"keyword_sets": {"space": ["契約"]},
               "entity_watch": {"lookback_days": {"openalex": 365}}}
        ents = {"entities": [
            {"id": "sp1", "sector": "space", "market": "JP",
             "watch": {"news_name": "Example Space", "openalex": "I123456789"}},
            {"id": "sp2", "sector": "space", "market": "JP",
             "watch": {"news_name": "No Papers Co"}},
        ]}
        by_name = {q["name"]: q for q in T.build_entity_queries(ents, src)}
        self.assertEqual(by_name["sp1-openalex"]["openalex"], "I123456789")
        self.assertEqual(by_name["sp1-openalex"]["lookback_days"], 365)
        self.assertNotIn("sp2-openalex", by_name)

    def test_invalid_market_rejected(self):
        ents = {"entities": [{"id": "x", "name": "X Corp", "aliases": ["X Corp Ltd"], "market": "CN"}]}
        with self.assertRaises(ConfigError):
            T.validate_entities(ents)

    def test_invalid_tier_rejected(self):
        ents = {"entities": [{"id": "x", "name": "X Corp", "aliases": ["X Corp Ltd"], "tier": "vip"}]}
        with self.assertRaises(ConfigError):
            T.validate_entities(ents)

    def test_unknown_sector_rejected(self):
        ents = {"entities": [{"id": "x", "sector": "chem", "watch": {"news_name": "X社"}}]}
        with self.assertRaises(ConfigError):
            T.build_entity_queries(ents, self.src())

    def test_missing_news_name_rejected(self):
        ents = {"entities": [{"id": "x", "sector": "space", "watch": {}}]}
        with self.assertRaises(ConfigError):
            T.build_entity_queries(ents, self.src())

    def test_entities_dir_split_loading(self):
        import tempfile
        root = os.path.dirname(os.path.abspath(__file__))
        with tempfile.TemporaryDirectory() as td:
            for n in ("scoring.json", "sources.json"):
                with open(os.path.join(root, n), encoding="utf-8") as f:
                    data = f.read()
                with open(os.path.join(td, n), "w", encoding="utf-8") as f:
                    f.write(data)
            os.mkdir(os.path.join(td, "entities"))
            jp = {"entities": [{"id": "jp1", "name": "JP Corp", "aliases": ["JP Corp Ltd"],
                                "sector": "space", "ticker": "1000",
                                "watch": {"news_name": "JP Corp"}}]}
            us = {"entities": [{"id": "us1", "name": "US Corp", "aliases": ["US Corp Inc"],
                                "sector": "space", "market": "US", "ticker": "USX",
                                "watch": {"news_name": "US Corp"}}]}
            # ファイル名の辞書順 (jp < us) ではなく ENTITY_FILE_ORDER で合体される
            for fname, data in (("us.json", us), ("jp.json", jp)):
                with open(os.path.join(td, "entities", fname), "w", encoding="utf-8") as f:
                    json.dump(data, f)
            cfg = T.load_config(td)
            self.assertEqual([e["id"] for e in cfg["entities"]["entities"]], ["jp1", "us1"])
            names = [q["name"] for q in cfg["sources"]["queries"]]
            self.assertIn("jp1-news", names)
            self.assertIn("us1-yahoo", names)

    def test_invalid_keyword_sets_rejected(self):
        s = copy.deepcopy(CFG["sources"]); s["keyword_sets"]["bio"] = "治験"
        with self.assertRaises(ConfigError):
            T.validate_sources(s)
        s = copy.deepcopy(CFG["sources"]); s["topics"] = [{"name": "t"}]
        with self.assertRaises(ConfigError):
            T.validate_sources(s)


class FinancialSkipTests(unittest.TestCase):
    def test_collect_skips_recently_successful_financial_queries(self):
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")
        # 過去の実行記録を仕込む (AS_OF = 2026-10-20):
        # a は5日前に成功 → 休む / b は5日前に失敗 → 再試行 / c は25日前に成功 → 取りに行く
        store.conn.execute("INSERT INTO runs(run_id, started_at) VALUES('old-run', '2026-09-25T00:00:00+00:00')")
        store.conn.execute("INSERT INTO runs(run_id, started_at) VALUES('recent-run', '2026-10-15T00:00:00+00:00')")
        for rid, qname, ok in (("recent-run", "a-edinet", 1), ("recent-run", "b-edinet", 0), ("old-run", "c-edinet", 1)):
            store.conn.execute("INSERT INTO run_sources VALUES(?, 'edinet', ?, 1, 0, 1, 0, ?, NULL, 0.1)",
                               (rid, qname, ok))
        store.conn.commit()
        self.assertEqual(store.last_source_successes().get(("edinet", "a-edinet")),
                         "2026-10-15T00:00:00+00:00")
        self.assertNotIn(("edinet", "b-edinet"), store.last_source_successes())
        cfg = {"sources": {"defaults": {"max_results": 5, "lookback_days": 400}, "queries": [
                   {"name": "a-edinet", "edinet": "AAA"}, {"name": "b-edinet", "edinet": "BBB"},
                   {"name": "c-edinet", "edinet": "CCC"}, {"name": "d-news", "gnews": "NEWSQ"}]},
               "entities": {"entities": []}}
        calls = []

        def fake_fetcher(qstr, max_results, lookback, as_of):
            calls.append(qstr)
            return []
        counts = {k: 0 for k in ("fetched", "new", "duplicates", "invalid", "sources_ok", "sources_failed")}
        T.collect(store, cfg, "new-run", AS_OF, ("edinet", "gnews"),
                  {"edinet": fake_fetcher, "gnews": fake_fetcher}, counts, lambda *a: None)
        self.assertNotIn("AAA", calls)
        self.assertIn("BBB", calls)
        self.assertIn("CCC", calls)
        self.assertIn("NEWSQ", calls)  # 財務系以外は間隔の対象外
        self.assertEqual(counts.get("financial_skipped"), 1)
        store.close()

    def test_zero_fetch_success_rests_only_briefly(self):
        # S-1: 成功したが0件 (空振り) のクエリは20日でなく短い休止 (3日)。
        # 取得できたクエリは従来どおり20日休む
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")
        store.conn.execute("INSERT INTO runs(run_id, started_at) VALUES('d1-run', '2026-10-19T00:00:00+00:00')")
        store.conn.execute("INSERT INTO runs(run_id, started_at) VALUES('d5-run', '2026-10-15T00:00:00+00:00')")
        # e: 1日前の空振り → 短い休止の内側 (休む)。f: 5日前の空振り → 休止を
        # 過ぎたので再試行。g: 5日前に取得あり → 20日休止の内側 (休む)
        for rid, qname, fetched in (("d1-run", "e-edinet", 0), ("d5-run", "f-edinet", 0),
                                    ("d5-run", "g-edinet", 3)):
            store.conn.execute("INSERT INTO run_sources VALUES(?, 'edinet', ?, ?, 0, 0, 0, 1, NULL, 0.1)",
                               (rid, qname, fetched))
        store.conn.commit()
        cfg = {"sources": {"defaults": {"max_results": 5, "lookback_days": 400}, "queries": [
                   {"name": "e-edinet", "edinet": "EEE"}, {"name": "f-edinet", "edinet": "FFF"},
                   {"name": "g-edinet", "edinet": "GGG"}]},
               "entities": {"entities": []}}
        calls = []

        def fake_fetcher(qstr, max_results, lookback, as_of):
            calls.append(qstr)
            return []
        counts = {k: 0 for k in ("fetched", "new", "duplicates", "invalid", "sources_ok", "sources_failed")}
        T.collect(store, cfg, "new-run", AS_OF, ("edinet",),
                  {"edinet": fake_fetcher}, counts, lambda *a: None)
        self.assertNotIn("EEE", calls)
        self.assertIn("FFF", calls)
        self.assertNotIn("GGG", calls)
        self.assertEqual(counts.get("financial_skipped"), 2)
        store.close()

    def test_zero_fetch_streaks_measured_by_period(self):
        # S-2: 0件の継続は回数でなく期間 (四半期周期90日) で測る
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")

        def attempt(rid, day, source, qname, fetched, ok=1):
            store.conn.execute("INSERT INTO runs(run_id, started_at) VALUES(?, ?)",
                               (rid, f"{day}T00:00:00+00:00"))
            store.conn.execute("INSERT INTO run_sources VALUES(?, ?, ?, ?, 0, 0, 0, ?, NULL, 0.1)",
                               (rid, source, qname, fetched, ok))

        # z: 110日前に取得あり、その後0件が直近まで継続 → 警告対象
        attempt("r1", "2026-07-02", "edinet", "z-edinet", 2)
        attempt("r2", "2026-10-19", "edinet", "z-edinet", 0)
        # y: 0件だが継続19日 → 対象外
        attempt("r3", "2026-10-01", "edinet", "y-edinet", 0)
        attempt("r4", "2026-10-19", "edinet", "y-edinet", 0)
        # w: 継続は長いが最終試行が80日前 (対象から外れた残骸) → 対象外
        attempt("r5", "2026-06-01", "edinet", "w-edinet", 1)
        attempt("r6", "2026-08-01", "edinet", "w-edinet", 0)
        # v (simfin): バルク不在で最初から0件、直近まで継続 → 警告対象
        attempt("r7", "2026-07-05", "simfin", "v-simfin", 0)
        attempt("r8", "2026-10-18", "simfin", "v-simfin", 0)
        # u: 直近で取得が戻った → 継続は切れている
        attempt("r9", "2026-07-02", "edgar", "u-edgar", 0)
        attempt("r10", "2026-10-19", "edgar", "u-edgar", 5)
        store.conn.commit()
        streaks = store.zero_fetch_streaks(("edgar", "edinet", "simfin"), AS_OF, 90, 31)
        got = {(s["source"], s["query"]) for s in streaks}
        self.assertEqual(got, {("edinet", "z-edinet"), ("simfin", "v-simfin")})
        days = {s["query"]: s["days"] for s in streaks}
        self.assertEqual(days["z-edinet"], 110)  # 最終取得 (7/2) から AS_OF (10/20)
        self.assertEqual(days["v-simfin"], 107)  # 取得が無いので最初の試行から
        store.close()

    def test_llm_call_recording_roundtrip(self):
        # llm_calls への記録と読み戻し (run 単位の内訳集計の土台)
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")
        store.record_llm_call("run-1", {"candidate_id": "c1", "purpose": "assess",
                                        "provider": "gemini", "model": "gemini-sub",
                                        "seconds": 12.5, "ok": True, "error": None,
                                        "chars_in": 4000, "chars_out": 900,
                                        "tokens_in": 1000, "tokens_out": 200})
        store.record_llm_call("run-1", {"candidate_id": "c1", "purpose": "assess",
                                        "provider": "gemini", "model": "gemini-main",
                                        "seconds": 120.0, "ok": False,
                                        "error": "HttpError: HTTP 503",
                                        "chars_in": 4000, "chars_out": None,
                                        "tokens_in": None, "tokens_out": None})
        rows = store.llm_calls("run-1")
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["model"], rows[0]["ok"], rows[0]["tokens_in"]),
                         ("gemini-sub", 1, 1000))
        self.assertEqual((rows[1]["model"], rows[1]["ok"]), ("gemini-main", 0))
        self.assertEqual(store.llm_calls("other-run"), [])
        store.close()

    def test_collect_yahoo_period_gate(self):
        # Yahoo は期末ベース: 最新決算の期末 +97日を過ぎた企業だけ取りに行く
        tmp = tempfile.TemporaryDirectory()
        store = T.Store(Path(tmp.name) / "d.sqlite")
        store.conn.execute("INSERT INTO runs(run_id, started_at) VALUES('recent-run', '2026-10-15T00:00:00+00:00')")
        for qname, ok in (("open-yahoo", 1), ("closed-yahoo", 1), ("noperiod-yahoo", 1), ("failed-yahoo", 0)):
            store.conn.execute("INSERT INTO run_sources VALUES('recent-run', 'yahoo', ?, 1, 0, 1, 0, ?, NULL, 0.1)",
                               (qname, ok))
        # open: 期末 2026-06-30 (+97日 = 10/5 を AS_OF 10/20 は過ぎている) → 取得
        # closed: 期末 2026-09-30 (+97日 = 2027/1/5) → 休む。failed は期末が新しくても再試行する
        for cid, eid, pub in (("c1", "open", "2026-06-30"), ("c2", "closed", "2026-09-30"),
                              ("c3", "failed", "2026-09-30")):
            store.conn.execute("INSERT INTO candidates(id, source, source_id, published, status) "
                               "VALUES(?, 'yahoo', ?, ?, 'screened')", (cid, f"{eid}-{pub}", pub))
            store.conn.execute("INSERT INTO candidate_entities(candidate_id, entity_id, strength, matched_by, matched_alias) "
                               "VALUES(?, ?, 'strong', 'test', '')", (cid, eid))
        store.conn.commit()
        self.assertEqual(store.latest_financial_periods("yahoo").get("open"), "2026-06-30")
        cfg = {"sources": {"defaults": {"max_results": 5, "lookback_days": 400}, "queries": [
                   {"name": "open-yahoo", "yahoo": "OPEN"}, {"name": "closed-yahoo", "yahoo": "CLOSED"},
                   {"name": "noperiod-yahoo", "yahoo": "NOPER"}, {"name": "failed-yahoo", "yahoo": "FAIL"}]},
               "entities": {"entities": []}}
        calls = []

        def fake_fetcher(qstr, max_results, lookback, as_of):
            calls.append(qstr)
            return []
        counts = {k: 0 for k in ("fetched", "new", "duplicates", "invalid", "sources_ok", "sources_failed")}
        T.collect(store, cfg, "new-run", AS_OF, ("yahoo",), {"yahoo": fake_fetcher}, counts, lambda *a: None)
        self.assertIn("OPEN", calls)       # 期末 +97日を過ぎた → 次の決算を取りに行く
        self.assertNotIn("CLOSED", calls)  # まだ次の決算が出ない時期 → 休む
        self.assertNotIn("NOPER", calls)   # 記録なし + 前回成功が直近 → 発見の間隔で休む
        self.assertIn("FAIL", calls)       # 前回失敗 → 再試行する
        self.assertEqual(counts.get("financial_skipped"), 2)
        store.close()


class YahooRestingTests(unittest.TestCase):
    """_yahoo_resting の空振り間引き (ゲート: 期末 2026-06-30 + 97日 = 2026-10-05)。"""

    def rest(self, as_of, started, ok=True, period="2026-06-30", got="2026-08-10"):
        from trlcore import pipeline as P
        q = {"name": "acme-yahoo"}
        attempts = {("yahoo", "acme-yahoo"): (started, ok)}
        periods = {"acme": period} if period else {}
        retrievals = {"acme": got} if got else {}
        return P._yahoo_resting(q, as_of, attempts, periods, retrievals)

    def test_before_gate_rests(self):
        self.assertTrue(self.rest(date(2026, 9, 1), "2026-08-10T00:00:00+00:00"))

    def test_discovery_attempt_is_not_an_empty_poll(self):
        # 最新期間を取得した試行 (取得日と同日) は空振りに数えず、ゲート後は取りに行く
        self.assertFalse(self.rest(date(2026, 10, 20), "2026-10-06T00:00:00+00:00",
                                   got="2026-10-06"))

    def test_empty_poll_backs_off_14_days(self):
        # 期間取得より後の試行 = 空振り。その後14日は休む
        self.assertTrue(self.rest(date(2026, 10, 20), "2026-10-13T00:00:00+00:00",
                                  got="2026-10-06"))
        self.assertFalse(self.rest(date(2026, 10, 28), "2026-10-13T00:00:00+00:00",
                                   got="2026-10-06"))

    def test_failure_retries_regardless(self):
        self.assertFalse(self.rest(date(2026, 10, 20), "2026-10-19T00:00:00+00:00",
                                   ok=False, got="2026-10-06"))

    def test_missing_retrieval_falls_back_to_gate_only(self):
        # 取得日時が無い旧データでは間引きを判定できないため、従来どおりゲートのみ
        self.assertFalse(self.rest(date(2026, 10, 20), "2026-10-19T00:00:00+00:00",
                                   got=None))


class YahooThrottleTests(unittest.TestCase):
    def test_throttle_spaces_calls_process_wide(self):
        from trlcore.sources import yahoo as yh
        waits, clock = [], [1000.0]

        def fake_sleep(w):
            waits.append(w)
            clock[0] += w
        yh._last_call[0] = 0.0
        yh._throttle(yh.MIN_INTERVAL, sleep=fake_sleep, now=lambda: clock[0])
        self.assertEqual(waits, [])  # 初回は待たない
        yh._throttle(yh.MIN_INTERVAL, sleep=fake_sleep, now=lambda: clock[0])
        self.assertEqual(len(waits), 1)  # 直後の2回目は間隔ぶん待つ
        self.assertAlmostEqual(waits[0], yh.MIN_INTERVAL, places=2)
        yh._last_call[0] = 0.0

    def test_fetch_throttles_every_request(self):
        # cookie/crumb/quoteSummary の全リクエストが全体スロットルを通る
        from trlcore.sources import yahoo as yh
        waits = []

        def fake_http(url, params=None, headers=None, **kw):
            if "getcrumb" in url:
                return b"crumb123"
            if "fc.yahoo.com" in url:
                raise RuntimeError("404")
            if "quoteSummary" in url:
                return json.dumps(YAHOO_SUMMARY).encode()
            raise AssertionError(url)

        yh._last_call[0] = 0.0
        out = yahoo.fetch("9348.T", 5, 400, AS_OF, http=fake_http, sleep=waits.append)
        self.assertEqual(len(out), 1)
        # 2回目以降のリクエストでは MIN_INTERVAL 前後の待ちが発生する
        self.assertTrue(any(w > yh.MIN_INTERVAL * 0.8 for w in waits))
        yh._last_call[0] = 0.0


class PubMedThrottleTests(unittest.TestCase):
    def test_throttle_spaces_calls_process_wide(self):
        from trlcore.sources import pubmed as pm
        waits, clock = [], [1000.0]

        def fake_sleep(w):
            waits.append(w)
            clock[0] += w
        pm._last_call[0] = 0.0
        pm._throttle(0.4, sleep=fake_sleep, now=lambda: clock[0])
        self.assertEqual(waits, [])  # 初回は待たない
        pm._throttle(0.4, sleep=fake_sleep, now=lambda: clock[0])
        self.assertEqual(len(waits), 1)  # 直後の2回目は間隔ぶん待つ
        self.assertAlmostEqual(waits[0], 0.4, places=2)
        pm._last_call[0] = 0.0


class EdgarTests(unittest.TestCase):
    FACTS = {"entityName": "Test Corp", "facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            {"start": "2026-04-01", "end": "2026-06-30", "val": 234_000_000, "form": "10-Q", "filed": "2026-08-01"},
            {"start": "2025-01-01", "end": "2025-12-31", "val": 602_000_000, "form": "10-K", "filed": "2026-03-01"}]}},
        "NetIncomeLoss": {"units": {"USD": [
            {"start": "2026-04-01", "end": "2026-06-30", "val": -49_000_000, "form": "10-Q", "filed": "2026-08-01"},
            {"start": "2025-01-01", "end": "2025-12-31", "val": -198_000_000, "form": "10-K", "filed": "2026-03-01"}]}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
            {"end": "2026-06-30", "val": 500_000_000, "form": "10-Q", "filed": "2026-08-01"}]}},
        "Assets": {"units": {"USD": [
            {"end": "2026-06-30", "val": 1_000_000_000, "form": "10-Q", "filed": "2026-08-01"}]}},
        "StockholdersEquity": {"units": {"USD": [
            {"end": "2026-06-30", "val": 800_000_000, "form": "10-Q", "filed": "2026-08-01"}]}},
    }}}

    def test_parse_facts_prefers_fresher_fy_over_stale_quarter(self):
        # 20-F 年次報告のみの企業: 古い四半期 (2019) より新しい通期 (2025) を直近とする
        from trlcore.sources import edgar
        facts = {"entityName": "Annual Filer", "facts": {"us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                {"start": "2019-01-01", "end": "2019-03-31", "val": 52_000_000, "form": "6-K", "filed": "2019-05-01"},
                {"start": "2025-01-01", "end": "2025-12-31", "val": 549_000_000, "form": "20-F", "filed": "2026-03-05"}]}},
            "NetIncomeLoss": {"units": {"USD": [
                {"start": "2025-01-01", "end": "2025-12-31", "val": 457_000_000, "form": "20-F", "filed": "2026-03-05"}]}},
        }}}
        text, period, kind = edgar.parse_company_facts(facts, date(2026, 11, 2))
        self.assertEqual((period, kind), ("2025-12-31", "通期"))
        self.assertIn("売上高 549百万米ドル", text)

    def test_parse_facts_no_double_count_short_term_securities(self):
        # 広義の現金概念 (短期投資込み) が使えたら、短期有価証券を別掲しない (二重計上防止)
        from trlcore.sources import edgar
        facts = {"entityName": "Broad Cash Co", "facts": {"us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                {"start": "2025-01-01", "end": "2025-12-31", "val": 549_000_000, "form": "20-F", "filed": "2026-03-05"}]}},
            "CashCashEquivalentsAndShortTermInvestments": {"units": {"USD": [
                {"end": "2025-12-31", "val": 1_367_000_000, "form": "20-F", "filed": "2026-03-05"}]}},
            "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
                {"end": "2025-12-31", "val": 71_000_000, "form": "20-F", "filed": "2026-03-05"}]}},
            "MarketableSecuritiesCurrent": {"units": {"USD": [
                {"end": "2025-12-31", "val": 1_296_000_000, "form": "20-F", "filed": "2026-03-05"}]}},
        }}}
        text, period, kind = edgar.parse_company_facts(facts, date(2026, 11, 2))
        self.assertIn("現金及び現金同等物 1,367百万米ドル", text)
        self.assertNotIn("短期有価証券", text)

    def test_parse_facts(self):
        from trlcore.sources import edgar
        text, period, kind = edgar.parse_company_facts(self.FACTS, date(2026, 10, 29))
        self.assertEqual((period, kind), ("2026-06-30", "四半期"))
        self.assertIn("売上高 234百万米ドル", text)
        self.assertIn("純利益 △49百万米ドル", text)
        self.assertIn("自己資本比率 80.0%", text)

    def test_fetch_record_shape(self):
        from trlcore.sources import edgar
        tickers = json.dumps({"0": {"cik_str": 123, "ticker": "TEST", "title": "Test Corp"}}).encode()
        facts = json.dumps(self.FACTS).encode()

        def http(url, params=None, headers=None):
            return tickers if "company_tickers" in url else facts
        edgar._tickers_cache = None
        try:
            recs = edgar.edgar_fetch("TEST", 20, 400, date(2026, 10, 29), http=http)
        finally:
            edgar._tickers_cache = None
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["source"], "edgar")
        self.assertEqual(recs[0]["source_id"], "TEST-2026-06-30")
        self.assertIn("財務数値(SEC EDGAR)", recs[0]["abstract"])
        self.assertEqual(recs[0]["info_type"], "disclosure")

    def test_fetch_raises_on_http_failure(self):
        # 通信失敗を空結果で握りつぶさず例外にする (成功扱いになると取得間隔で不当に休むため)
        from trlcore.sources import edgar
        tickers = json.dumps({"0": {"cik_str": 123, "ticker": "TEST", "title": "Test Corp"}}).encode()

        def http(url, params=None, headers=None):
            if "company_tickers" in url:
                return tickers
            raise RuntimeError("403 Forbidden")
        edgar._tickers_cache = None
        try:
            with self.assertRaises(RuntimeError):
                edgar.edgar_fetch("TEST", 20, 400, date(2026, 10, 29), http=http)
        finally:
            edgar._tickers_cache = None

    def test_fetch_unknown_ticker_and_stale(self):
        from trlcore.sources import edgar
        tickers = json.dumps({"0": {"cik_str": 123, "ticker": "OLD", "title": "Old Corp"}}).encode()
        stale = {"entityName": "Old Corp", "facts": {"us-gaap": {
            "NetIncomeLoss": {"units": {"USD": [
                {"start": "2023-04-01", "end": "2023-06-30", "val": 1, "form": "10-Q", "filed": "2023-08-01"}]}}}}}

        def http(url, params=None, headers=None):
            return tickers if "company_tickers" in url else json.dumps(stale).encode()
        edgar._tickers_cache = None
        try:
            self.assertEqual(edgar.edgar_fetch("MISSING", 20, 400, date(2026, 10, 29), http=http), [])
            self.assertEqual(edgar.edgar_fetch("OLD", 20, 400, date(2026, 10, 29), http=http), [])
        finally:
            edgar._tickers_cache = None

    def test_queries_generated_for_us_and_20f_filers(self):
        cfg = T.load_config()
        edgar_qs = [q for q in cfg["sources"]["queries"] if q.get("edgar")]
        # 米国139社 + edgar_ticker を明示した 20-F 提出3社 (Innate/HUTCHMED/Nanobiotix)
        self.assertEqual(len(edgar_qs), 142)
        self.assertTrue(all(q["name"].endswith("-edgar") for q in edgar_qs))
        by_name = {q["name"]: q["edgar"] for q in edgar_qs}
        self.assertEqual(by_name["innate_pharma-edgar"], "IPHA")
        self.assertEqual(by_name["nanobiotix-edgar"], "NBTX")
        self.assertEqual(by_name["hutchmed-edgar"], "HCM")


class CollectThinningTests(unittest.TestCase):
    """Issue 3 (収集の間引き): 既知アイテムの再照合省略と invalid のネガティブキャッシュ。

    後処理の主コストは企業リンク照合 (辞書300社の走査) で、取得アイテムの
    97% は重複か invalid の再処理だった。published が不変の既知アイテムと、
    現行世代の辞書でリンク不成立だったアイテムは、照合を繰り返さない。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "d" / "trl.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, source):
        cfg = dict(CFG)
        cfg["sources"] = {"defaults": {"max_results": 5, "lookback_days": 30},
                          "queries": [{"name": "n", source: "q"}]}
        return cfg

    def run_collect(self, store, cfg, raws, source, as_of=AS_OF):
        return run_pipeline(store, cfg, None, as_of=as_of, sources=(source,), kinds=(),
                            fetchers={source: lambda q, n, d, a: raws}, today=as_of,
                            log=lambda *_: None)

    def test_known_news_skips_relink(self):
        from unittest import mock
        from trlcore import pipeline as PL
        store = Store(self.path)
        raws = [raw(source="gnews", sid="n1", title="アストロスケールが防衛案件の大型契約を獲得",
                    info_type="news")]
        with mock.patch.object(PL, "link_entities", wraps=PL.link_entities) as spy:
            self.run_collect(store, self.cfg("gnews"), raws, "gnews")
            self.assertEqual(spy.call_count, 1)
            r2 = self.run_collect(store, self.cfg("gnews"), raws, "gnews")
            self.assertEqual(spy.call_count, 1)  # 2回目は照合しない
        self.assertEqual(r2["counts"]["duplicates"], 1)
        self.assertEqual(r2["counts"].get("dup_skipped"), 1)
        store.close()

    def test_invalid_news_cached_until_dictionary_changes(self):
        from unittest import mock
        from trlcore import pipeline as PL
        store = Store(self.path)
        raws = [raw(source="gnews", sid="nx", title="人気アニメのポップアップショップが開店",
                    affiliations=[], info_type="news")]
        with mock.patch.object(PL, "link_entities", wraps=PL.link_entities) as spy:
            r1 = self.run_collect(store, self.cfg("gnews"), raws, "gnews")
            self.assertEqual(r1["counts"]["invalid"], 1)
            self.assertEqual(spy.call_count, 1)
            r2 = self.run_collect(store, self.cfg("gnews"), raws, "gnews")
            self.assertEqual(r2["counts"]["invalid"], 1)
            self.assertEqual(r2["counts"].get("invalid_cached"), 1)
            self.assertEqual(spy.call_count, 1)  # キャッシュで照合省略
            cfg2 = self.cfg("gnews")
            cfg2["config_hash"] = "another-generation"  # 辞書が変われば世代が変わる
            r3 = self.run_collect(store, cfg2, raws, "gnews")
            self.assertEqual(r3["counts"]["invalid"], 1)
            self.assertEqual(spy.call_count, 2)  # 再照合される
        store.close()

    def test_trials_update_is_reprocessed(self):
        # 治験は published=最終更新日。更新されれば published が変わり、省略の
        # 対象から外れて材料更新として取り込まれる (B2 再評価の契機を守る)
        store = Store(self.path)
        kw = dict(source="trials", sid="NCT00000001", info_type="trial",
                  url="https://clinicaltrials.gov/study/NCT00000001")
        r1 = self.run_collect(store, self.cfg("trials"),
                              [raw(title="Astroscale 治験試験", published="2026-10-10", **kw)], "trials")
        self.assertEqual(r1["counts"]["new"], 1)
        r2 = self.run_collect(store, self.cfg("trials"),
                              [raw(title="Astroscale 治験試験", published="2026-10-10", **kw)], "trials")
        self.assertEqual(r2["counts"].get("dup_skipped"), 1)
        r3 = self.run_collect(store, self.cfg("trials"),
                              [raw(title="Astroscale 治験試験", published="2026-10-18",
                                   abstract="結果が公表された治験の更新テキスト", **kw)], "trials")
        self.assertFalse(r3["counts"].get("dup_skipped"))
        self.assertEqual(r3["counts"]["duplicates"], 1)
        cand = store.get_candidate(store.conn.execute("SELECT id FROM candidates").fetchone()[0])
        self.assertEqual(cand["abstract"], "結果が公表された治験の更新テキスト")
        self.assertIsNotNone(cand["material_updated_at"])
        store.close()

    def test_disclosure_never_skipped(self):
        # 開示は訂正があり得るため、同一アイテムでも再統合を省略しない (I-2 の保護)
        store = Store(self.path)
        kw = dict(source="edgar", sid="ASTRO-2026-06-30", info_type="disclosure",
                  url="https://www.sec.gov/edgar/browse/?CIK=1")

        def fetch100():
            return [raw(title="Astroscale 財務情報(2026-06-30 四半期)",
                        published="2026-06-30", abstract="Astroscaleの財務数値: 売上高 100", **kw)]

        def age_runs():
            # 財務ソースの20日ゲートは runs.started_at (実時刻) 基準なので、
            # 過去の実行を古くしてゲートを開ける
            store.conn.execute("UPDATE runs SET started_at='2026-09-01T00:00:00+00:00'")
            store.conn.commit()

        r1 = self.run_collect(store, self.cfg("edgar"), fetch100(), "edgar")
        self.assertEqual(r1["counts"]["new"], 1)
        age_runs()
        r2 = self.run_collect(store, self.cfg("edgar"), fetch100(), "edgar")
        self.assertEqual(r2["counts"]["duplicates"], 1)
        self.assertFalse(r2["counts"].get("dup_skipped"))
        age_runs()
        r3 = self.run_collect(store, self.cfg("edgar"),
                              [raw(title="Astroscale 財務情報(2026-06-30 四半期)",
                                   published="2026-06-30", abstract="Astroscaleの財務数値: 売上高 120", **kw)],
                              "edgar")
        cand = store.get_candidate(store.conn.execute("SELECT id FROM candidates").fetchone()[0])
        self.assertIn("120", cand["abstract"])  # 訂正が取り込まれる
        store.close()


if __name__ == "__main__":
    unittest.main()
