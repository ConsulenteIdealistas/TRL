"""自動テスト(外部APIに依存しない)。実行: python -m unittest test_trl -v

コア系 (パイプライン・ストア・基盤) のテスト。収集系は test_sources.py、
評価・採点系は test_eval.py に分離した。共通フィクスチャは testutil を参照する。
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

from testutil import (ABSTRACT, AS_OF, CFG, EVENT_KEYS, EXTRACTION, LONG_KEYS,
                      TRIALS_DATA, arxiv, assessment, edinet, ev, freshness,
                      gnews, http, item, make_llm_fn, openalex, prtimes, pubmed,
                      raw, scoring, trials, yahoo)


class FreshnessTests(unittest.TestCase):
    def test_parse_and_days(self):
        self.assertEqual(freshness.parse_date("2026-10-10"), date(2026, 10, 10))
        self.assertEqual(freshness.parse_date("2026-10"), date(2026, 10, 1))
        self.assertEqual(freshness.parse_date("2026-10", end=True), date(2026, 10, 31))
        self.assertIsNone(freshness.parse_date("2026-13-01"))
        self.assertIsNone(freshness.parse_date("garbage"))
        self.assertEqual(freshness.days_since("2026-10-10", AS_OF), 10)

    def test_buckets_depend_on_as_of(self):
        self.assertEqual(freshness.freshness_info("2026-10-10", AS_OF)["bucket"], "fresh_30d")
        self.assertEqual(freshness.freshness_info("2026-10-10", date(2027, 6, 1))["bucket"], "older")
        self.assertEqual(freshness.freshness_info("2026-10-25", AS_OF)["bucket"], "future")
        self.assertEqual(freshness.freshness_info(None, AS_OF)["bucket"], "unknown")


class NormalizeTests(unittest.TestCase):
    def test_normalize_ok_and_fatal(self):
        rec, p = normalize(raw(doi="https://doi.org/10.1/ABC"), "t")
        self.assertEqual(rec["keys"][0], "doi:10.1/abc")
        self.assertIsNone(normalize(raw(title="  "), "t")[0])
        self.assertIsNone(normalize({"title": "x"}, "t")[0])
        rec, p = normalize(raw(published="bad"), "t")
        self.assertIsNone(rec["published"])
        self.assertIn("bad_published_date", p)


class FinancialPoolTests(unittest.TestCase):
    """財務根拠プール: EDINET 開示の財務数値を同一企業の評価へ同梱する。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "d" / "trl.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def _add(self, store, r):
        rec, _ = normalize(r, "2026-10-20T00:00:00+00:00")
        cid, _ = store.upsert_candidate(rec, "r0")
        store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
        store.set_status(cid, "screened")
        return cid

    def test_related_financials_pool(self):
        store = Store(self.path)
        paper = self._add(store, raw(sid="2610.7777", title="Debris removal study",
                                     affiliations=["Astroscale Holdings Inc."]))
        fin = {"source": "edinet", "source_id": "S100FIN", "title": "アストロスケール 第2四半期報告書",
               "abstract": "EDINETに第2四半期報告書を提出した。財務数値(EDINET XBRL): 売上高 1,234百万円、現金及び現金同等物 30,000百万円。",
               "url": "https://example.org/fin", "published": "2026-10-15",
               "fields": ["EDINET", "四半期報告書"], "affiliations": ["Astroscale Holdings Inc."],
               "info_type": "disclosure"}
        fin_id = self._add(store, fin)
        yf = {"source": "yahoo", "source_id": "186A-2026-06-30", "title": "Astroscale Holdings Inc. 財務情報",
              "abstract": "Astroscale Holdings Inc.(186A)の財務数値(Yahoo Finance): 直近四半期 売上高 1,200百万円。",
              "url": "https://example.org/yf", "published": "2026-06-30",
              "fields": ["Yahoo Finance", "財務"], "affiliations": ["Astroscale Holdings Inc."],
              "info_type": "disclosure"}
        yf_id = self._add(store, yf)
        # 実際の取得経路では開示はティッカー/提出者から strong で直結される
        # (本番DBでも開示リンクは全件 strong)。_add の本文照合だと weak に
        # なるため、本番の付き方に合わせて strong へ揃える (I-3 の条件前提)
        for fid_ in (fin_id, yf_id):
            store.conn.execute("UPDATE candidate_entities SET strength='strong' WHERE candidate_id=?", (fid_,))
        store.conn.commit()
        plain = dict(fin, source_id="S100PLAIN", title="アストロスケール 臨時報告書",
                     abstract="EDINETに臨時報告書を提出した。", url="https://example.org/plain")
        self._add(store, plain)
        rel = store.related_financials(paper, AS_OF)
        self.assertEqual(len(rel), 2)  # EDINET と Yahoo の両方が対象。数値マーカーの無い開示は入らない
        self.assertTrue(all("財務数値(" in r["text"] for r in rel))
        store.close()

    def _bare(self, store, r):
        rec, _ = normalize(r, "2026-10-20T00:00:00+00:00")
        cid, _ = store.upsert_candidate(rec, "r0")
        store.conn.execute("DELETE FROM candidate_entities WHERE candidate_id=?", (cid,))
        store.conn.commit()
        return cid

    def _link(self, store, cid, entity_id, strength, link_type="entity"):
        store.conn.execute(
            "INSERT INTO candidate_entities(candidate_id, entity_id, strength, matched_by,"
            " matched_alias, link_type, product_id, product_name) VALUES(?,?,?,'title','x',?,'','')",
            (cid, entity_id, strength, link_type))
        store.conn.commit()

    def test_related_pools_link_conditions_and_corrections(self):
        # I-3: プール側のリンク条件 (財務は strong の企業リンクのみ、ニュースは企業リンクのみ)
        # I-6: 同一期に訂正版がある元書類は財務プールから外れる
        store = Store(self.path)
        paper = self._add(store, raw(sid="2610.7779", title="Debris removal study",
                                     affiliations=["Astroscale Holdings Inc."]))
        ent = store.get_entities(paper)[0]["entity_id"]
        base = {"source": "edinet", "fields": ["EDINET"], "affiliations": [], "info_type": "disclosure"}
        weak = self._bare(store, dict(base, source_id="S100W", title="弱リンク開示",
                                      abstract="財務数値(EDINET XBRL): 売上高 1百万円。",
                                      url="https://example.org/w", published="2026-10-10"))
        self._link(store, weak, ent, "weak")
        orig = self._bare(store, dict(base, source_id="S100O", title="アストロスケール 有価証券報告書－第8期(2025/05/01－2026/04/30)",
                                      abstract="財務数値(EDINET XBRL): 売上高 900百万円。",
                                      url="https://example.org/o", published="2026-08-01"))
        self._link(store, orig, ent, "strong")
        corr = self._bare(store, dict(base, source_id="S100C", title="アストロスケール 訂正有価証券報告書－第8期(2025/05/01－2026/04/30)",
                                      abstract="財務数値(EDINET XBRL): 売上高 950百万円。",
                                      url="https://example.org/c", published="2026-09-01"))
        self._link(store, corr, ent, "strong")
        titles = [r["title"] for r in store.related_financials(paper, AS_OF)]
        self.assertNotIn("弱リンク開示", titles)  # weak は入らない
        self.assertTrue(any("訂正" in t for t in titles))  # 訂正版は入る
        self.assertFalse(any("訂正" not in t and "第8期" in t for t in titles))  # 元書類は外れる
        # ニュース: 製品リンクだけのものは入らず、企業リンクのものは入る
        nbase = {"source": "gnews", "fields": ["媒体"], "affiliations": [], "info_type": "news",
                 "abstract": "x" * 60}
        nprod = self._bare(store, dict(nbase, source_id="np1", title="製品名だけのニュース",
                                       url="https://example.org/np", published="2026-10-15"))
        self._link(store, nprod, ent, "weak", link_type="product")
        nent = self._bare(store, dict(nbase, source_id="ne1", title="企業名のニュース",
                                      url="https://example.org/ne", published="2026-10-16"))
        self._link(store, nent, ent, "weak")
        ntitles = [r["title"] for r in store.related_news(paper, AS_OF)]
        self.assertNotIn("製品名だけのニュース", ntitles)
        self.assertIn("企業名のニュース", ntitles)
        store.close()

    def test_disclosure_upsert_refreshes_facts(self):
        # I-2: 財務開示は再取得で要旨 (財務数値) が変われば取り込む。論文は従来どおり初回保持
        store = Store(self.path)
        fin = {"source": "edinet", "source_id": "S100R", "title": "アストロスケール 四半期報告書",
               "abstract": "財務数値(EDINET XBRL): 売上高 100百万円。",
               "url": "https://example.org/r", "published": "2026-10-15",
               "fields": ["EDINET"], "affiliations": [], "info_type": "disclosure"}
        rec, _ = normalize(fin, "2026-10-20T00:00:00+00:00")
        cid, is_new = store.upsert_candidate(rec, "r0")
        self.assertTrue(is_new)
        rec2, _ = normalize(dict(fin, abstract="財務数値(EDINET XBRL): 売上高 120百万円。"),
                            "2026-11-09T00:00:00+00:00")
        cid2, is_new2 = store.upsert_candidate(rec2, "r1")
        self.assertEqual(cid2, cid)
        self.assertFalse(is_new2)
        got = store.get_candidate(cid)
        self.assertIn("120百万円", got["abstract"])
        self.assertTrue(got["material_updated_at"])
        vs = store.candidate_versions(cid)
        self.assertEqual(len(vs), 1)
        self.assertIn("100百万円", vs[0]["abstract"])  # 非破壊: 上書き前の版が残る
        paper, _ = normalize(raw(sid="2610.7780", title="Paper", abstract="A" * 120), "2026-10-20T00:00:00+00:00")
        pid, _ = store.upsert_candidate(paper, "r0")
        paper2, _ = normalize(raw(sid="2610.7780", title="Paper", abstract="B" * 120), "2026-11-09T00:00:00+00:00")
        store.upsert_candidate(paper2, "r1")
        self.assertTrue(store.get_candidate(pid)["abstract"].startswith("A"))
        self.assertEqual(store.candidate_versions(pid), [])  # 論文は上書き自体が無い
        store.close()

    def test_trials_upsert_keeps_superseded_version(self):
        # 非破壊: 治験の要旨が更新 (結果公表・ステータス変更) されたら旧版を残す。
        # 旧版が無いと、評価時点の材料との比較ができなくなるため
        store = Store(self.path)
        t1 = {"source": "trials", "source_id": "NCT00000001", "title": "Trial A",
              "abstract": "Phase 2 recruiting patients for the study.",
              "url": "https://example.org/t", "published": "2026-10-15",
              "fields": ["ClinicalTrials"], "affiliations": [], "info_type": "news"}
        rec, _ = normalize(t1, "2026-10-20T00:00:00+00:00")
        cid, _ = store.upsert_candidate(rec, "r0")
        store.upsert_candidate(rec, "r1")  # 変化が無ければ版は増えない
        self.assertEqual(store.candidate_versions(cid), [])
        rec2, _ = normalize(dict(t1, abstract="Phase 2 completed. Primary endpoint met."),
                            "2026-11-09T00:00:00+00:00")
        store.upsert_candidate(rec2, "r2")
        vs = store.candidate_versions(cid)
        self.assertEqual(len(vs), 1)
        self.assertIn("recruiting", vs[0]["abstract"])
        self.assertIn("completed", store.get_candidate(cid)["abstract"])
        store.close()

    def test_evaluate_one_includes_financials(self):
        cand = {"id": "c1", "title": "T", "abstract": "A" * 300, "url": "u", "source": "trials",
                "published": "2026-10-10", "fields": []}
        seen = []
        base = make_llm_fn(long_scores={k: 70 for k in LONG_KEYS})

        def fn(p):
            seen.append(p)
            return base(p)

        fins = [{"title": "X 四半期報告書", "published": "2026-10-15", "url": "https://example.org/f",
                 "text": "財務数値(EDINET XBRL): 売上高 1,234百万円、現金及び現金同等物 30,000百万円。"}]
        res = ev.evaluate_one(cand, "long", CFG, Mock(fn), AS_OF, financials=fins)
        self.assertEqual(res["extra"]["related_financials_count"], 1)
        self.assertTrue(any("関連財務根拠" in p and "30,000百万円" in p for p in seen))

    def test_evaluate_one_records_used_model(self):
        # フォールバックで応答した場合は、評価の来歴も抽出キャッシュのキーも実モデルになる
        class UsedModelMock(Mock):
            model = "main-model"
            last_model = "fallback-model"
        cand = {"id": "c1", "title": "T", "abstract": "A" * 300, "url": "u", "source": "trials",
                "published": "2026-10-10", "fields": []}
        res = ev.evaluate_one(cand, "long", CFG, UsedModelMock(make_llm_fn()), AS_OF)
        self.assertEqual(res["llm_model"], "fallback-model")
        self.assertEqual(res["extraction_model"], "fallback-model")


class DedupeStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.sqlite"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def up(self, **kw):
        rec, _ = normalize(raw(**kw), "t")
        return self.store.upsert_candidate(rec)

    def test_trials_upsert_updates_abstract_on_change(self):
        rec1, _ = normalize(trials.parse(TRIALS_DATA)[0], "2026-10-20T00:00:00+00:00")
        cid, is_new = self.store.upsert_candidate(rec1, "r0")
        self.assertTrue(is_new)
        data = json.loads(json.dumps(TRIALS_DATA))
        data["studies"][0]["hasResults"] = True
        data["studies"][0]["protocolSection"]["statusModule"]["overallStatus"] = "COMPLETED"
        rec2, _ = normalize(trials.parse(data)[0], "2026-10-21T00:00:00+00:00")
        cid2, is_new2 = self.store.upsert_candidate(rec2, "r1")
        self.assertEqual((cid2, is_new2), (cid, False))
        cur = self.store.get_candidate(cid)
        self.assertIn("Results: Posted", cur["abstract"])
        self.assertIn("Status: COMPLETED", cur["abstract"])
        self.assertTrue(cur["material_updated_at"])

    def test_non_trials_upsert_keeps_abstract(self):
        cid, _ = self.up(sid="2610.7777")
        rec2, _ = normalize(raw(sid="2610.7777", abstract="書き換えた要旨です。 " * 5), "t2")
        self.store.upsert_candidate(rec2)
        cur = self.store.get_candidate(cid)
        self.assertNotIn("書き換えた要旨", cur["abstract"])
        self.assertFalse(cur["material_updated_at"])

    def test_same_id_not_duplicated(self):
        a, n1 = self.up()
        b, n2 = self.up()
        self.assertEqual((a, n1, n2), (b, True, False))
        self.assertEqual(self.store.count_candidates(), 1)

    def test_merge_across_sources_by_doi(self):
        a, _ = self.up(source="pubmed", sid="111", doi="10.1/abc")
        b, new = self.up(source="arxiv", sid="2610.9", doi="https://doi.org/10.1/ABC", title="Different title")
        self.assertEqual(a, b)
        self.assertFalse(new)

    def test_similar_titles_not_merged(self):
        a, _ = self.up(sid="1", title="Same Title")
        b, new = self.up(sid="2", title="Same Title")
        self.assertNotEqual(a, b)
        self.assertTrue(new)

    def test_missing_fields_filled_on_merge(self):
        a, _ = self.up(sid="1", abstract="")
        self.up(sid="1", abstract="now present")
        self.assertEqual(self.store.get_candidate(a)["abstract"], "now present")

    def test_persistence_across_reopen(self):
        a, _ = self.up()
        self.store.upsert_queue(a, "long", "r", ["m"], ["IR"], ["c"], "t", {"x": 1})
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get_candidate(a)["title"], "Paper A")
        q = self.store.list_queue()
        self.assertEqual((len(q), q[0]["missing"], q[0]["next_sources"]), (1, ["m"], ["IR"]))
        self.up()  # 再取得しても増えない
        self.assertEqual(self.store.count_candidates(), 1)
        self.store.resolve_queue(a, "long")
        self.assertEqual(self.store.list_queue(), [])


class GnewsDedupeTests(unittest.TestCase):
    """gnews の重複厳格化: 窓つき統合 (upsert_candidate) と統合パス (dedupe_gnews)。"""

    TITLE = "アストロスケールが防衛案件の大型契約を獲得した"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.sqlite"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def rec(self, sid, published, title=None, retrieved="2026-11-01T00:00:00+00:00"):
        raw_ = {"source": "gnews", "source_id": sid, "title": title or self.TITLE,
                "abstract": "ニュース本文のダイジェストです。",
                "url": f"https://news.example/{sid}", "published": published,
                "fields": ["媒体"], "affiliations": [], "info_type": "news"}
        rec, problems = normalize(raw_, retrieved)
        self.assertIsNotNone(rec, problems)
        return rec

    def add(self, sid, published, entities=("astroscale",), **kw):
        cid, is_new = self.store.upsert_candidate(self.rec(sid, published, **kw), "r0")
        self.store.set_entities(cid, [
            {"entity_id": e, "strength": "strong", "matched_by": "title",
             "matched_alias": e} for e in entities])
        return cid, is_new

    def aux(self, entity="astroscale", title=None):
        return f"gaux:{entity}:{gnews_title_norm(title or self.TITLE)}"

    def upsert_window(self, sid, published, entities=("astroscale",), **kw):
        return self.store.upsert_candidate(
            self.rec(sid, published, **kw), "r1",
            window_keys=[self.aux(entities[0])],
            entity_ids=list(entities), window_days=3)

    def owner_of(self, key):
        row = self.store.conn.execute(
            "SELECT candidate_id FROM identifiers WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def test_title_norm_unifies_fullwidth_and_symbols(self):
        self.assertEqual(gnews_title_norm("ＡＢＣニュース！ 第１報"),
                         gnews_title_norm("abcニュース 第1報"))

    def test_window_merge_within_3_days(self):
        cid1, _ = self.add("n1", "2026-10-10")
        self.store.dedupe_gnews()  # 生存行へ gaux を付与する
        cid2, is_new = self.upsert_window("n2", "2026-10-12")
        self.assertEqual((cid2, is_new), (cid1, False))
        self.assertEqual(self.store.count_candidates(), 1)
        # 今回の通常キー (URL) が所有者へ登録されている
        self.assertEqual(self.owner_of("url:https://news.example/n2"), cid1)

    def test_window_no_merge_after_4_days(self):
        cid1, _ = self.add("n1", "2026-10-10")
        self.store.dedupe_gnews()
        cid2, is_new = self.upsert_window("n2", "2026-10-14")
        self.assertTrue(is_new)
        self.assertNotEqual(cid2, cid1)
        self.assertEqual(self.store.count_candidates(), 2)

    def test_window_no_merge_when_entity_set_differs(self):
        cid1, _ = self.add("n1", "2026-10-10", entities=("astroscale", "other"))
        self.store.dedupe_gnews()
        # 所有者の企業集合は {astroscale, other}。{astroscale} だけの照合は
        # 集合が一致しないため、窓の中でも統合しない
        cid2, is_new = self.upsert_window("n2", "2026-10-11", entities=("astroscale",))
        self.assertTrue(is_new)
        self.assertNotEqual(cid2, cid1)

    def test_dedupe_merges_legacy_twins(self):
        cid_old, _ = self.add("n1", "2026-10-10", retrieved="2026-10-10T00:00:00+00:00")
        cid_new, _ = self.add("n2", "2026-10-10", retrieved="2026-10-11T00:00:00+00:00")
        self.assertNotEqual(cid_old, cid_new)
        self.assertEqual(self.store.dedupe_gnews(), 1)
        loser = self.store.get_candidate(cid_new)
        self.assertEqual(loser["status"], "rejected")
        self.assertEqual(loser["screen_reason"], f"duplicate_of:{cid_old}")
        # 識別子が survivor へ移り、survivor に gaux が付いている
        self.assertEqual(self.owner_of("url:https://news.example/n2"), cid_old)
        self.assertEqual(self.owner_of(self.aux()), cid_old)

    def test_dedupe_leaves_wide_spread_group(self):
        self.add("n1", "2026-10-10")
        self.add("n2", "2026-10-14")
        self.assertEqual(self.store.dedupe_gnews(), 0)
        self.assertEqual(self.store.count_candidates(), 2)
        for c in self.store.list_by_status(["new"]):
            self.assertEqual(c["status"], "new")

    def test_dedupe_skips_evaluated(self):
        cid1, _ = self.add("n1", "2026-10-10")
        cid2, _ = self.add("n2", "2026-10-10")
        self.store.conn.execute(
            "INSERT INTO evaluations(run_id, candidate_id, kind, status) "
            "VALUES('r0', ?, 'long', 'final')", (cid1,))
        self.store.conn.commit()
        self.assertEqual(self.store.dedupe_gnews(), 0)
        self.assertEqual(self.store.get_candidate(cid1)["status"], "new")
        self.assertEqual(self.store.get_candidate(cid2)["status"], "new")

    def test_dedupe_idempotent(self):
        self.add("n1", "2026-10-10")
        self.add("n2", "2026-10-11")
        self.assertEqual(self.store.dedupe_gnews(), 1)
        self.assertEqual(self.store.dedupe_gnews(), 0)

    def test_merged_duplicate_hidden_from_related_news(self):
        # 統合された重複行は関連ニュースのプールに出ず、survivor だけが残る。
        # スクリーニング除外 (duplicate_of 以外) は従来どおりプールに残る。
        from datetime import date
        center, _ = self.add("paper1", "2026-10-12", title="アストロスケールの軌道上サービス論文")
        keep, _ = self.add("n1", "2026-10-10")
        dupe, _ = self.add("n2", "2026-10-11")
        other, _ = self.add("n3", "2026-10-11", title="アストロスケールが新しいデブリ観測結果を公表した")
        self.store.conn.execute(
            "UPDATE candidates SET status='rejected', screen_reason='abstract_too_short' WHERE id=?",
            (other,))
        self.store.conn.commit()
        self.assertEqual(self.store.dedupe_gnews(), 1)
        pool = self.store.related_news(center, date(2026, 10, 12))
        titles = [p["title"] for p in pool]
        self.assertEqual(titles.count(self.TITLE), 1)
        self.assertIn("アストロスケールが新しいデブリ観測結果を公表した", titles)


class ListByStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.sqlite"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_list_by_status_matches_get_candidate(self):
        def up(**kw):
            rec, _ = normalize(raw(**kw), "t")
            cid, _ = self.store.upsert_candidate(rec)
            return cid
        cid1 = up(sid="2610.1111")
        cid2 = up(sid="2610.2222", title="Paper B")
        self.store.set_entities(cid1, [{"entity_id": "e1", "strength": "strong",
                                        "matched_by": "title", "matched_alias": "A"}])
        self.store.set_status(cid1, "screened")
        self.store.set_status(cid2, "screened")
        got = self.store.list_by_status(["screened"])
        want = [self.store.get_candidate(cid1), self.store.get_candidate(cid2)]
        self.assertEqual([g["id"] for g in got], [cid1, cid2])
        self.assertEqual(got, want)


class HttpTests(unittest.TestCase):
    def test_retry_then_success(self):
        seq = [urllib.error.HTTPError("u", 503, "x", {}, None), io.BytesIO(b"ok")]

        def opener(req, timeout):
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        self.assertEqual(http.fetch("http://x", opener=opener, sleep=lambda s: None), b"ok")

    def test_no_retry_on_4xx_and_error_hides_key(self):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "x", {}, None)
        with self.assertRaises(http.HttpError) as cm:
            http.fetch("http://x/api", params={"api_key": "SECRETSECRET"}, opener=opener, sleep=lambda s: None)
        self.assertNotIn("SECRETSECRET", str(cm.exception))
        self.assertEqual(cm.exception.status, 401)

    def test_gives_up_after_retries(self):
        n = []

        def opener(req, timeout):
            n.append(1)
            raise urllib.error.URLError("down")
        with self.assertRaises(http.HttpError):
            http.fetch("http://x", retries=2, opener=opener, sleep=lambda s: None)
        self.assertEqual(len(n), 3)

    def test_deadline_stops_retries(self):
        # 監査 A-2: 次の再試行待ちが呼び出し上限を超えるなら待たずに打ち切る
        n, slept = [], []

        def opener(req, timeout):
            n.append(1)
            raise urllib.error.HTTPError(req.full_url, 503, "x", {"Retry-After": "5"}, None)
        with self.assertRaises(http.HttpError) as cm:
            http.fetch("http://x", retries=3, opener=opener,
                       sleep=lambda s: slept.append(s), deadline=1)
        self.assertTrue(cm.exception.deadline_exceeded)
        self.assertEqual(len(n), 1)
        self.assertEqual(slept, [])

    def test_redact_env_secret(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-abcdef123456"
        try:
            self.assertNotIn("sk-abcdef123456", http.redact("fail sk-abcdef123456 here"))
        finally:
            del os.environ["ANTHROPIC_API_KEY"]


class BomTests(unittest.TestCase):
    def test_bom_files_load(self):
        d = Path(tempfile.mkdtemp())
        (d / "long.v9.md").write_bytes(b"\xef\xbb\xbf{{x}} \xe3\x81\x82")
        p = load_prompt("long", "v9", d)
        self.assertEqual(render(p["text"], {"x": "1"}), "1 あ")
        for f in ("scoring", "sources"):
            (d / f"{f}.json").write_bytes(b"\xef\xbb\xbf" + (T.ROOT / f"{f}.json").read_bytes().lstrip(b"\xef\xbb\xbf"))
        self.assertEqual(load_config(d)["config_version"], CFG["config_version"])

    def test_config_hash_includes_prompts(self):
        d = Path(tempfile.mkdtemp())
        for f in ("scoring", "sources"):
            (d / f"{f}.json").write_bytes((T.ROOT / f"{f}.json").read_bytes())
        names = []
        for n, v in CFG["scoring"]["prompts"].items():
            (d / f"{n}.{v}.md").write_bytes((T.ROOT / f"{n}.{v}.md").read_bytes())
            names.append(f"{n}.{v}.md")
        h1 = load_config(d)["config_hash"]
        (d / names[0]).write_text((d / names[0]).read_text(encoding="utf-8") + "\n変更\n", encoding="utf-8")
        self.assertNotEqual(load_config(d)["config_hash"], h1)  # プロンプトだけの変更でも世代が変わる


import json


import tempfile


import unittest


from datetime import date


from pathlib import Path


class NewsPoolTests(unittest.TestCase):
    """ニュースは単体評価せず、同一企業の評価へ同梱する材料として扱う再設計の検証。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "d" / "trl.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def news_raw(self, sid, title, published="2026-10-19", source="gnews"):
        return {"source": source, "source_id": sid, "title": title,
                "abstract": title + " に関する事業ニュースの見出し。",
                "url": f"https://example.org/{sid}", "published": published,
                "fields": ["日本経済新聞"], "affiliations": [], "info_type": "news"}

    def test_collect_drops_news_without_entity(self):
        store = Store(self.path)
        cfg = dict(CFG)
        cfg["sources"] = {"defaults": {"max_results": 5, "lookback_days": 30},
                          "queries": [{"name": "n", "gnews": "q"}]}
        raws = [self.news_raw("n1", "アストロスケールが防衛案件の大型契約を獲得"),
                self.news_raw("n2", "人気アニメのポップアップショップが開店")]
        r = run_pipeline(store, cfg, None, as_of=AS_OF, sources=("gnews",), kinds=("long",),
                         fetchers={"gnews": lambda q, n, d, a: raws}, today=AS_OF, log=lambda *_: None)
        self.assertEqual(store.count_candidates(), 1)
        self.assertEqual(r["counts"]["invalid"], 1)
        store.close()

    def test_gnews_same_title_deduped_across_urls(self):
        # 監査 I-4: 同一企業・同一タイトル・同日の gnews は配信社が違っても統合
        store = Store(self.path)
        cfg = dict(CFG)
        cfg["sources"] = {"defaults": {"max_results": 5, "lookback_days": 30},
                          "queries": [{"name": "n", "gnews": "q"}]}
        raws = [self.news_raw("n1", "アストロスケールが防衛案件の大型契約を獲得"),
                self.news_raw("n2", "アストロスケールが防衛案件の大型契約を獲得！")]
        r = run_pipeline(store, cfg, None, as_of=AS_OF, sources=("gnews",), kinds=(),
                         fetchers={"gnews": lambda q, n, d, a: raws}, today=AS_OF,
                         log=lambda *_: None)
        self.assertEqual(store.count_candidates(), 1)
        self.assertEqual(r["counts"]["duplicates"], 1)
        store.close()

    def test_cap_reached_queries_counted(self):
        # 監査 I-5: 取得上限ちょうどのクエリを数える
        store = Store(self.path)
        cfg = dict(CFG)
        cfg["sources"] = {"defaults": {"max_results": 5, "lookback_days": 30},
                          "queries": [{"name": "n", "gnews": "q"}]}
        raws = [self.news_raw(f"n{i}", f"アストロスケール関連の事業ニュース第{i}号が報じられた")
                for i in range(5)]
        r = run_pipeline(store, cfg, None, as_of=AS_OF, sources=("gnews",), kinds=(),
                         fetchers={"gnews": lambda q, n, d, a: raws}, today=AS_OF,
                         log=lambda *_: None)
        self.assertEqual(r["counts"].get("cap_reached_queries"), 1)
        store.close()

    def test_collect_processes_each_chunk_before_next_fetch(self):
        # 後処理の逐次化: 次の波の取得が始まる時点で、前の波のレコードは保存済み
        tmp = tempfile.TemporaryDirectory()
        dbpath = Path(tmp.name) / "d.sqlite"
        store = T.Store(dbpath)
        queries = [{"name": f"e{i}-news", "gnews": f"Q{i}"} for i in range(12)]
        cfg = {"sources": {"defaults": {"max_results": 5, "lookback_days": 30}, "queries": queries},
               "entities": CFG["entities"]}
        seen = {}

        def fetcher(qstr, max_results, lookback, as_of):
            if qstr == "Q11":
                import sqlite3 as sq
                conn = sq.connect(dbpath)
                seen["count"] = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
                conn.close()
            i = qstr[1:]
            return [self.news_raw(f"r{i}", f"アストロスケール関連の事業ニュース第{i}号が報じられた")]

        counts = {k: 0 for k in ("fetched", "new", "duplicates", "invalid", "sources_ok", "sources_failed")}
        T.collect(store, cfg, "run-x", AS_OF, ("gnews",), {"gnews": fetcher}, counts, lambda *a: None)
        # 第1波 (10件) は保存済み (第2波の自クエリ分が先に処理されれば 11 になりうる)。
        # 全取得後にまとめて処理する旧方式では、この時点では 0 件のはず
        self.assertGreaterEqual(seen.get("count", 0), 10)
        self.assertEqual(counts["sources_ok"], 12)
        store.close()

    def test_collect_drops_paper_without_entity(self):
        store = Store(self.path)
        cfg = json.loads(json.dumps(CFG))
        cfg["sources"] = {"defaults": {"max_results": 10, "lookback_days": 365},
                          "queries": [{"name": "t", "arxiv": "q"}]}
        raws = [raw(sid="2610.9701", title="Debris removal demonstration",
                    affiliations=["Astroscale Holdings Inc."]),
                raw(sid="2610.9702", title="Unrelated quantum theory", affiliations=[])]
        r = run_pipeline(store, cfg, None, as_of=AS_OF, sources=("arxiv",), kinds=("long",),
                         fetchers={"arxiv": lambda q, n, d, a: raws}, today=AS_OF, log=lambda *_: None)
        self.assertEqual(store.count_candidates(), 1)
        self.assertEqual(r["counts"]["invalid"], 1)
        store.close()

    def test_selection_prefers_entity_linked_candidates(self):
        store = Store(self.path)

        def add(r):
            rec, _ = normalize(r, "2026-10-20T00:00:00+00:00")
            cid, _ = store.upsert_candidate(rec, "r0")
            store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
            store.set_status(cid, "screened")
            return cid
        # 新しい順は unlinked > weak > strong だが、選定は企業リンク優先になり、
        # リンクの無い候補は評価対象から外れる
        unlinked = add(raw(sid="2610.9101", title="Unrelated quantum theory", published="2026-10-19",
                           affiliations=[]))
        weak = add(raw(sid="2610.9102", title="アストロスケール関連の市場分析メモ", published="2026-10-18",
                       affiliations=[]))
        strong = add(raw(sid="2610.9103", title="Debris removal study", published="2026-10-17",
                         affiliations=["Astroscale Holdings Inc."]))
        sel = select_for_eval(store, CFG, AS_OF, 10)
        self.assertEqual([c["id"] for c in sel], [strong, weak])
        store.close()

    def test_demo_signal_detection(self):
        self.assertTrue(T.has_demo_signal({"title": "Demonstration of a prototype device", "abstract": ""}))
        self.assertTrue(T.has_demo_signal({"title": "量子デバイスの実証実験", "abstract": ""}))
        self.assertTrue(T.has_demo_signal({"title": "A Phase 2 study", "abstract": "", "info_type": "trial"}))
        self.assertFalse(T.has_demo_signal({"title": "A theory of quantum codes", "abstract": "We propose a framework."}))

    def test_selection_prefers_demo_signal_within_link_tier(self):
        store = Store(self.path)

        def add(r):
            rec, _ = normalize(r, "2026-10-20T00:00:00+00:00")
            cid, _ = store.upsert_candidate(rec, "r0")
            store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
            store.set_status(cid, "screened")
            return cid
        # どちらも weak リンク。新しさでは theory が勝つが、実証シグナル優先で demo が先に選ばれる
        theory = add(raw(sid="2610.9201", title="アストロスケール関連メモ: A theory of fault-tolerant codes",
                         published="2026-10-19", affiliations=[]))
        demo = add(raw(sid="2610.9202", title="アストロスケール関連メモ: Demonstration of a prototype quantum memory",
                       published="2026-10-15", affiliations=[]))
        # リンク段位はシグナルより上位: strong リンク(シグナルなし)が weak+シグナルありより先
        strong = add(raw(sid="2610.9203", title="Debris removal study", published="2026-10-10",
                         affiliations=["Astroscale Holdings Inc."]))
        sel = select_for_eval(store, CFG, AS_OF, 10)
        self.assertEqual([c["id"] for c in sel], [strong, demo, theory])
        store.close()

    def test_selection_reserves_paper_slots(self):
        store = Store(self.path)

        def add(r, info_type=None):
            rec, _ = normalize(r, "2026-10-20T00:00:00+00:00")
            if info_type:
                rec["info_type"] = info_type
            cid, _ = store.upsert_candidate(rec, "r0")
            store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
            store.set_status(cid, "screened")
            return cid
        # 治験は実証シグナルで常に上位に来るが、選定6件のうち2件は論文枠になる
        trials = [add(raw(sid=f"2610.98{i:02d}", title=f"A Phase 2 study of compound {i}",
                          published="2026-10-19"), info_type="trial") for i in range(6)]
        papers = [add(raw(sid=f"2610.99{i:02d}", title=f"Peptide discovery research note {i}",
                          published="2026-10-18")) for i in range(3)]
        sel = select_for_eval(store, CFG, AS_OF, 6)
        kinds = ["trial" if c["id"] in trials else "paper" for c in sel]
        self.assertEqual(len(sel), 6)
        self.assertEqual(kinds.count("paper"), 2)
        self.assertTrue(all(p in [c["id"] for c in sel] for p in papers[:2]))
        store.close()

    def test_selection_excludes_disclosure(self):
        store = Store(self.path)
        rec, _ = normalize(raw(sid="2610.9401", affiliations=["Astroscale Holdings Inc."]),
                           "2026-10-20T00:00:00+00:00")
        rec["info_type"] = "disclosure"  # Yahoo等の財務スナップショットはプール専用で単体評価しない
        cid, _ = store.upsert_candidate(rec, "r0")
        store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
        store.set_status(cid, "screened")
        sel = select_for_eval(store, CFG, AS_OF, 10)
        self.assertEqual(sel, [])
        store.close()

    def test_selection_hold_gate_and_material_update_bypass(self):
        store = Store(self.path)

        def add_hold(sid, updated=None):
            rec, _ = normalize(raw(sid=sid), "2026-10-20T00:00:00+00:00")
            cid, _ = store.upsert_candidate(rec, "r0")
            store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
            store.set_status(cid, "hold", eval_at="2026-10-17T00:00:00+00:00")  # 3日前 (間隔は14日)
            if updated:
                store.conn.execute("UPDATE candidates SET material_updated_at=? WHERE id=?",
                                   (updated, cid))
                store.conn.commit()
            return cid

        stale = add_hold("2610.9501")
        fresh = add_hold("2610.9502", updated="2026-10-19T00:00:00+00:00")  # 評価後に材料が更新
        sel_ids = [c["id"] for c in select_for_eval(store, CFG, AS_OF, 10)]
        self.assertNotIn(stale, sel_ids)
        self.assertIn(fresh, sel_ids)
        store.close()

    def test_purge_resets_evaluated_candidates(self):
        # final 到達候補のステータスは 'evaluated'。purge のリセット対象に含めないと
        # 評価だけ消えて候補が二度と再評価されないまま取り残される
        store = Store(self.path)
        rec, _ = normalize(raw(sid="2610.9601"), "2026-10-20T00:00:00+00:00")
        cid, _ = store.upsert_candidate(rec, "r0")
        store.set_status(cid, "evaluated", eval_at="2026-10-20T00:00:00+00:00")
        counts = store.purge_evaluations()
        self.assertEqual(counts["candidates_reset"], 1)
        got = store.get_candidate(cid)
        self.assertEqual(got["status"], "screened")
        self.assertIsNone(got["last_eval_at"])
        store.close()

    def test_selection_evaluated_gate_and_material_update_bypass(self):
        # final (evaluated) も hold と同じ再評価ゲート (14日) と材料更新バイパスの対象
        store = Store(self.path)

        def add_ev(sid, updated=None):
            rec, _ = normalize(raw(sid=sid), "2026-10-20T00:00:00+00:00")
            cid, _ = store.upsert_candidate(rec, "r0")
            store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
            store.set_status(cid, "evaluated", eval_at="2026-10-17T00:00:00+00:00")
            if updated:
                store.conn.execute("UPDATE candidates SET material_updated_at=? WHERE id=?",
                                   (updated, cid))
                store.conn.commit()
            return cid

        stale = add_ev("2610.9503")
        fresh = add_ev("2610.9504", updated="2026-10-19T00:00:00+00:00")
        sel_ids = [c["id"] for c in select_for_eval(store, CFG, AS_OF, 10)]
        self.assertNotIn(stale, sel_ids)
        self.assertIn(fresh, sel_ids)
        store.close()

    def test_selection_only_new_mode(self):
        store = Store(self.path)

        def add(sid, status, eval_at=None):
            rec, _ = normalize(raw(sid=sid), "2026-10-20T00:00:00+00:00")
            cid, _ = store.upsert_candidate(rec, "r0")
            store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
            store.set_status(cid, status, eval_at=eval_at)
            return cid

        fresh = add("2610.9601", "screened")  # 一度も評価されていない
        old_hold = add("2610.9602", "hold", eval_at="2026-09-01T00:00:00+00:00")  # 間隔経過済みで通常は再評価対象
        both = [c["id"] for c in select_for_eval(store, CFG, AS_OF, 10)]
        self.assertIn(fresh, both)
        self.assertIn(old_hold, both)
        only = [c["id"] for c in select_for_eval(store, CFG, AS_OF, 10, only_new=True)]
        self.assertEqual(only, [fresh])
        store.close()

    def test_selection_caps_per_entity(self):
        store = Store(self.path)

        def add(r):
            rec, _ = normalize(r, "2026-10-20T00:00:00+00:00")
            cid, _ = store.upsert_candidate(rec, "r0")
            store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
            store.set_status(cid, "screened")
            return cid
        for i in range(12):  # 同一企業に12件あってもキューには上限10件まで
            add(raw(sid=f"2610.95{i:02d}", title=f"Debris removal study {i}",
                    published="2026-10-19", affiliations=["Astroscale Holdings Inc."]))
        for i in range(2):  # 別企業は上限の対象外
            add(raw(sid=f"2610.96{i:02d}", title=f"ペプチドリーム関連の研究メモ {i}",
                    published="2026-10-19", affiliations=[]))
        sel = select_for_eval(store, CFG, AS_OF, 100)
        self.assertEqual(len(sel), 12)
        store.close()

    def test_selection_excludes_watch_only_candidates(self):
        store = Store(self.path)

        def add(r):
            rec, _ = normalize(r, "2026-10-20T00:00:00+00:00")
            cid, _ = store.upsert_candidate(rec, "r0")
            store.set_entities(cid, link_entities(rec, CFG.get("entities", {})))
            store.set_status(cid, "screened")
            return cid
        # オンコリスはウォッチ層: リンクがあっても評価対象にしない
        watch = add(raw(sid="2610.9301", title="オンコリスの臨床試験に関する報告", published="2026-10-19",
                        affiliations=[]))
        core = add(raw(sid="2610.9302", title="アストロスケール関連の市場分析メモ", published="2026-10-18"))
        sel = select_for_eval(store, CFG, AS_OF, 10)
        self.assertEqual([c["id"] for c in sel], [core])
        store.close()

    def test_news_not_selected_but_pooled(self):
        store = Store(self.path)
        paper, _ = normalize(raw(sid="2610.7777", affiliations=["Astroscale Holdings Inc."]),
                             "2026-10-20T00:00:00+00:00")
        pid, _ = store.upsert_candidate(paper, "r0")
        store.set_entities(pid, link_entities(paper, CFG.get("entities", {})))
        news, _ = normalize(self.news_raw("n9", "アストロスケールが200億円規模の受注見込みを発表"),
                            "2026-10-20T00:00:00+00:00")
        nid, _ = store.upsert_candidate(news, "r0")
        store.set_entities(nid, link_entities(news, CFG.get("entities", {})))
        store.set_status(pid, "screened")
        store.set_status(nid, "screened")
        sel = select_for_eval(store, CFG, AS_OF, 10)
        self.assertEqual([c["id"] for c in sel], [pid])
        rel = store.related_news(pid, AS_OF)
        self.assertEqual(len(rel), 1)
        self.assertIn("受注見込み", rel[0]["title"])
        # 製品リンクだけの候補 (スポンサー名が資料に無い治験など) にも同じ企業のニュースが付く
        news2, _ = normalize(self.news_raw("n10", "オンコリスバイオファーマが海外企業と提携を発表"),
                             "2026-10-20T00:00:00+00:00")
        n2id, _ = store.upsert_candidate(news2, "r0")
        store.set_entities(n2id, link_entities(news2, CFG.get("entities", {})))
        prod, _ = normalize(raw(sid="2610.8888", title="OBP-301 Phase II study results",
                                abstract="Results of the OBP-301 study. " * 12, affiliations=[]),
                            "2026-10-20T00:00:00+00:00")
        prid, _ = store.upsert_candidate(prod, "r0")
        store.set_entities(prid, link_entities(prod, CFG.get("entities", {})))
        news3, _ = normalize(self.news_raw("n11", "オンコリスバイオファーマが新薬候補の導出契約を締結",
                                           source="prtimes"),
                             "2026-10-20T00:00:00+00:00")
        n3id, _ = store.upsert_candidate(news3, "r0")
        store.set_entities(n3id, link_entities(news3, CFG.get("entities", {})))
        rel2 = store.related_news(prid, AS_OF)
        self.assertEqual(len(rel2), 2)  # 報道(gnews)と企業発表(prtimes)が擦り合わさる
        self.assertEqual({r["origin"] for r in rel2}, {"報道", "企業発表"})
        store.close()

    def test_news_media_url_and_site_search_link(self):
        # Google News の記事URLは直接開けないため、媒体URLの保存とサイト側の検索リンク付けを担保する
        from trlcore.site import _news_for_site
        store = Store(self.path)
        paper, _ = normalize(raw(sid="2610.7701", affiliations=["Astroscale Holdings Inc."]),
                             "2026-10-20T00:00:00+00:00")
        pid, _ = store.upsert_candidate(paper, "r0")
        store.set_entities(pid, link_entities(paper, CFG.get("entities", {})))
        n = self.news_raw("n77", "アストロスケールが大型契約を獲得")
        n["url"] = "https://news.google.com/rss/articles/TOKEN"
        n["media_url"] = "https://www.nikkei.com"
        news, _ = normalize(n, "2026-10-20T00:00:00+00:00")
        nid, _ = store.upsert_candidate(news, "r0")
        store.set_entities(nid, link_entities(news, CFG.get("entities", {})))
        rel = store.related_news(pid, AS_OF)
        self.assertEqual(len(rel), 1)
        self.assertEqual(rel[0]["media_url"], "https://www.nikkei.com")
        site_n = _news_for_site(rel[0])
        self.assertTrue(site_n["via_google_news"])
        self.assertIn("google.com/search?q=", site_n["search_url"])
        from urllib.parse import unquote_plus
        self.assertIn("大型契約", unquote_plus(site_n["search_url"]))
        store.close()

    def test_evaluate_one_includes_related(self):
        cand = {"id": "c1", "title": "T", "abstract": "A" * 300, "url": "u", "source": "trials",
                "published": "2026-10-10", "fields": []}
        seen = []
        base = make_llm_fn(long_scores={k: 70 for k in LONG_KEYS})

        def fn(p):
            seen.append(p)
            return base(p)
        related = [{"title": "アストロスケールが200億円規模の受注見込み", "published": "2026-10-19",
                    "url": "https://example.org/n9", "media": "日本経済新聞"}]
        res = evaluate_one(cand, "long", CFG, Mock(fn), AS_OF, related=related)
        self.assertEqual(res["status"], "final")
        self.assertEqual(res["extra"]["related_news_count"], 1)
        self.assertTrue(any("受注見込み" in p and "関連事業ニュース" in p for p in seen))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "d" / "trl.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def fetchers(self, fail_pubmed=False):
        def arx(q, n, days, as_of):
            return [raw(sid="2610.0001", doi="10.1/a"), raw(sid="2610.0002", title="B"),
                    raw(sid="2610.0003", title="Short", abstract="x"), {"title": "", "source": "arxiv"}]

        def pm(q, n, days, as_of):
            if fail_pubmed:
                raise RuntimeError("HTTP 500 for http://x?api_key=SECRETSECRET")
            return [raw(source="pubmed", sid="9", doi="10.1/a", title="Same paper via pubmed")]
        return {"arxiv": arx, "pubmed": pm}

    def cfg(self):
        c = dict(CFG)
        c["sources"] = {"defaults": {"max_results": 5, "lookback_days": 30},
                        "queries": [{"name": "q", "arxiv": "a", "pubmed": "p"}]}
        return c

    def test_end_to_end_and_rerun_idempotent(self):
        store = Store(self.path)
        llm = Mock(make_llm_fn(long_scores={"monetization": None}))
        r1 = run_pipeline(store, self.cfg(), llm, as_of=AS_OF, kinds=("long",), fetchers=self.fetchers(),
                          today=AS_OF, log=lambda *_: None)
        c = r1["counts"]
        self.assertEqual((c["fetched"], c["invalid"], c["sources_ok"]), (5, 1, 2))
        self.assertEqual(store.count_candidates(), 2 + 1)  # A(doi統合) , B , Short
        self.assertEqual((c["rejected"], c["screened"]), (1, 2))
        self.assertEqual((c["hold"], c["final"]), (2, 0))
        q = store.list_queue()
        self.assertEqual(len(q), 2)
        self.assertIn("monetization", q[0]["reason"])
        ev1 = store.evaluations_for_run(r1["run_id"], "long")[0]
        self.assertEqual(json.loads(ev1["prompt_versions_json"])["long"]["version"],
                         CFG["scoring"]["prompts"]["long"])
        self.assertEqual(ev1["config_version"], CFG["config_version"])
        store.close()

        # 再実行: 候補は増えず、保留は再評価間隔内なので再評価されない。キューは残る
        store = Store(self.path)
        r2 = run_pipeline(store, self.cfg(), llm, as_of=AS_OF, kinds=("long",), fetchers=self.fetchers(),
                          today=AS_OF, log=lambda *_: None)
        self.assertEqual(store.count_candidates(), 3)
        self.assertEqual(r2["counts"]["new"], 0)
        self.assertEqual(r2["counts"]["selected"], 0)
        self.assertEqual(len(store.list_queue()), 2)

        # 間隔経過後に情報が揃えば確定し、キューは解消
        # 評価日時は実時計で刻まれるため、「間隔経過後」は実日付基準で
        # 決める (固定日だと実行日が進むにつれて間隔内に戻ってしまう)
        later = date.today() + timedelta(days=15)
        llm2 = Mock(make_llm_fn(long_scores={k: 65 for k in LONG_KEYS}))
        r3 = run_pipeline(store, self.cfg(), llm2, as_of=AS_OF, kinds=("long",), do_collect=False, today=later,
                          log=lambda *_: None)
        self.assertEqual(r3["counts"]["final"], 2)
        self.assertEqual(store.list_queue(), [])
        text = build_report(store, r3["run_id"])
        self.assertIn("65.0", text)
        self.assertIn("投資利益率ではありません", text)
        store.close()

    def test_llm_calls_recorded_from_worker_threads(self):
        # 計装レコードは評価ワーカースレッドから届く。Store への書き込みが
        # スレッドをまたぐと sqlite の制約で静かに全件消えるため (2026-11-03 の
        # live 実行で発生)、実プロバイダ + run 経由で残ることを固定する
        import os
        from trlcore.llm import Gemini
        old_key = os.environ.get("GEMINI_API_KEY")
        os.environ["GEMINI_API_KEY"] = "test"  # 生成に必要 (値は使われない)
        try:
            fn = make_llm_fn(long_scores={k: 65 for k in LONG_KEYS})

            def http(url, **kw):
                prompt = json.loads(kw["data"])["contents"][0]["parts"][0]["text"]
                return json.dumps({"candidates": [{"content": {"parts": [{"text": fn(prompt)}]}}],
                                   "usageMetadata": {"promptTokenCount": 11,
                                                     "candidatesTokenCount": 22}}).encode()
            store = Store(self.path)
            llm = Gemini(model="gemini-main", http=http, fallback_model="gemini-sub")
            r = run_pipeline(store, self.cfg(), llm, as_of=AS_OF, kinds=("long",), fetchers=self.fetchers(),
                             today=AS_OF, log=lambda *_: None)
            self.assertEqual(r["counts"]["final"], 2)
            rows = store.llm_calls(r["run_id"])
            self.assertTrue(rows)
            self.assertEqual({row["purpose"] for row in rows}, {"extract", "assess"})
            self.assertTrue(all(row["model"] == "gemini-main" for row in rows))
            self.assertTrue(all(row["tokens_in"] == 11 for row in rows))
            store.close()
        finally:
            if old_key is None:
                os.environ.pop("GEMINI_API_KEY", None)
            else:
                os.environ["GEMINI_API_KEY"] = old_key

    def test_handover_from_interrupted_run(self):
        store = Store(self.path)
        llm = Mock(make_llm_fn(long_scores={k: 65 for k in LONG_KEYS}))
        r1 = run_pipeline(store, self.cfg(), llm, as_of=AS_OF, kinds=("long",), fetchers=self.fetchers(),
                          today=AS_OF, log=lambda *_: None)
        cands = store.list_by_status(["evaluated", "hold"])
        self.assertEqual(len(cands), 2)
        done_id, todo_id = cands[0]["id"], cands[1]["id"]
        # 前回の実行 (interrupted) を作り、選定 [done, todo] と done 分の評価を記録する
        store.start_run("prev-run", CFG["config_version"], CFG["config_hash"], AS_OF.isoformat(), {})
        store.update_run_params("prev-run", selected=[done_id, todo_id])
        store.conn.execute(
            "INSERT INTO evaluations(run_id,candidate_id,kind,as_of,evaluated_at,status,total,band,"
            "items_json,extraction_json,event_json,prompt_versions_json,config_version,config_hash,"
            "llm_provider,llm_model,input_hash,errors_json,extra_json) "
            "SELECT 'prev-run', ?, kind, as_of, evaluated_at, status, total, band, items_json,"
            "extraction_json, event_json, prompt_versions_json, config_version, config_hash,"
            "llm_provider, llm_model, input_hash, errors_json, extra_json FROM evaluations LIMIT 1",
            (done_id,))
        store.conn.commit()
        store.finish_run("prev-run", {}, "interrupted", 1)
        # previous_run の判定を決定的にするため開始時刻を並べ替える (r1 < prev-run < 次の実行)
        store.conn.execute("UPDATE runs SET started_at='2020-01-01T00:00:00+00:00' WHERE run_id=?", (r1["run_id"],))
        store.conn.execute("UPDATE runs SET started_at='2020-06-01T00:00:00+00:00' WHERE run_id='prev-run'")
        store.conn.commit()
        # 再評価間隔内 (today=AS_OF) なので通常選定では何も選ばれないが、引き継ぎで todo だけが評価される
        r2 = run_pipeline(store, self.cfg(), llm, as_of=AS_OF, kinds=("long",), do_collect=False,
                          today=AS_OF, log=lambda *_: None)
        self.assertEqual(r2["counts"].get("handover"), 1)
        self.assertEqual(r2["counts"]["selected"], 1)
        evs = store.evaluations_for_run(r2["run_id"], "long")
        self.assertEqual([e["candidate_id"] for e in evs], [todo_id])
        params = json.loads(store.get_run(r2["run_id"])["params_json"])
        self.assertEqual(params["selected"], [todo_id])
        store.close()

    def test_run_budget_stops_evaluation(self):
        store = Store(self.path)
        llm = Mock(make_llm_fn(long_scores={k: 65 for k in LONG_KEYS}))
        r1 = run_pipeline(store, self.cfg(), llm, as_of=AS_OF, kinds=("long",), fetchers=self.fetchers(),
                          today=AS_OF, log=lambda *_: None)
        self.assertEqual(r1["counts"]["final"], 2)
        cfg2 = self.cfg()
        cfg2["scoring"] = json.loads(json.dumps(CFG["scoring"]))
        cfg2["scoring"]["llm"] = {**(cfg2["scoring"].get("llm") or {}), "run_budget_sec": 1}
        r2 = run_pipeline(store, cfg2, llm, as_of=AS_OF, kinds=("long",), do_collect=False,
                          today=date.today() + timedelta(days=15), log=lambda *_: None)
        # 締め切り (1秒) のため評価は打ち切られる (選定は run レコードに記録される)
        self.assertEqual(r2["counts"]["selected"], 2)
        self.assertEqual(r2["counts"]["final"], 0)
        self.assertEqual(r2["counts"].get("llm_degraded"), "run_budget_exceeded")
        params = json.loads(store.get_run(r2["run_id"])["params_json"])
        self.assertEqual(len(params["selected"]), 2)
        # 次の実行は再評価間隔内でも、打ち切られた選定を引き継いで評価する
        r3 = run_pipeline(store, self.cfg(), llm, as_of=AS_OF, kinds=("long",), do_collect=False,
                          today=AS_OF, log=lambda *_: None)
        self.assertEqual(r3["counts"].get("handover"), 2)
        self.assertEqual(r3["counts"]["final"], 2)
        store.close()

    def test_purge_evaluations_resets_candidates(self):
        store = Store(self.path)
        llm = Mock(make_llm_fn(long_scores={"monetization": None}))
        r1 = run_pipeline(store, self.cfg(), llm, as_of=AS_OF, kinds=("long",), fetchers=self.fetchers(),
                          today=AS_OF, log=lambda *_: None)
        self.assertEqual(r1["counts"]["hold"], 2)
        self.assertEqual(len(store.list_queue()), 2)
        n_candidates = store.count_candidates()
        counts = store.purge_evaluations()
        self.assertEqual(counts, {"evaluations": 2, "queue": 2, "candidates_reset": 2})
        self.assertEqual(store.list_queue(), [])
        self.assertEqual(store.evaluations_for_run(r1["run_id"], "long"), [])
        self.assertEqual(store.count_candidates(), n_candidates)  # 候補・材料は残る
        statuses = {c["status"] for c in store.list_by_status(["screened", "hold", "error", "final"])}
        self.assertEqual(statuses, {"screened"})
        # purge 直後は再評価間隔を待たずに再評価できる
        r2 = run_pipeline(store, self.cfg(), llm, as_of=AS_OF, kinds=("long",), do_collect=False,
                          today=AS_OF, log=lambda *_: None)
        self.assertEqual(r2["counts"]["hold"], 2)
        store.close()

    def test_source_failure_isolated_and_secret_redacted(self):
        store = Store(self.path)
        logs = []
        r = run_pipeline(store, self.cfg(), None, as_of=AS_OF, fetchers=self.fetchers(fail_pubmed=True),
                         today=AS_OF, log=logs.append)
        self.assertEqual((r["counts"]["sources_ok"], r["counts"]["sources_failed"], r["failed"]), (1, 1, False))
        # ソース単位の全滅は 'partial' として残る (監査 O-1)
        self.assertTrue(r["partial"])
        self.assertEqual(store.get_run(r["run_id"])["status"], "partial")
        self.assertFalse(any("SECRETSECRET" in str(x) for x in logs))
        srcs = {s["source"]: s for s in store.run_sources(r["run_id"])}
        self.assertEqual(srcs["pubmed"]["ok"], 0)
        self.assertNotIn("SECRETSECRET", srcs["pubmed"]["error"])
        self.assertEqual(srcs["arxiv"]["ok"], 1)
        self.assertGreater(r["counts"]["skipped_no_llm"], 0)  # LLM未設定 → 評価せず収集のみ

    def test_cli_llm_default_is_gemini(self):
        # TRL_LLM_PROVIDER が未設定・空でも既定は gemini (空文字で評価が
        # 音もなく止まった 2026-11-06 の事故の回帰テスト)。none は明示時のみ。
        import os
        from unittest import mock as um
        from trlcore import cli
        seen = []

        def fake_provider(name, model=None):
            seen.append(name)
            return object()

        def run_case(argv, env):
            seen.clear()
            with tempfile.TemporaryDirectory() as td, \
                    um.patch.dict(os.environ, env, clear=False), \
                    um.patch.object(cli, "get_provider", fake_provider), \
                    um.patch.object(cli, "configure_llm", lambda p, cfg: None), \
                    um.patch.object(cli, "run_pipeline",
                                    lambda *a, **k: {"run_id": "x", "counts": {}, "failed": False}), \
                    um.patch.object(cli, "build_report", lambda s, r, *a, **k: ""):
                if "TRL_LLM_PROVIDER" not in env:
                    os.environ.pop("TRL_LLM_PROVIDER", None)
                rc = cli.main(["run", "--data-dir", td, "--skip-collect"] + argv)
            self.assertEqual(rc, 0)
            return seen[-1]

        self.assertEqual(run_case([], {}), "gemini")                      # 未設定
        self.assertEqual(run_case([], {"TRL_LLM_PROVIDER": ""}), "gemini")  # 空 (本番の事故形)
        self.assertEqual(run_case([], {"TRL_LLM_PROVIDER": "none"}), "none")
        self.assertEqual(run_case(["--llm", "none"], {}), "none")          # 明示は尊重

    def test_collect_budget_skips_remaining_queries(self):
        # 収集フェーズの時間予算 (監査 A-1): 予算超過後は未着手クエリを打ち切る
        import copy
        import time as time_mod
        store = Store(self.path)
        store.start_run("r1", "test", "hash", AS_OF.isoformat(), {})
        c = copy.deepcopy(CFG)
        c["sources"] = {"defaults": {"max_results": 5, "lookback_days": 30},
                        "queries": [{"name": f"q{i}", "arxiv": "a"} for i in range(12)]}
        c["scoring"]["screening"]["collect_budget_sec"] = 0.05

        def slow(q, max_results, lookback_days, as_of):
            time_mod.sleep(0.1)
            return []

        counts = {k: 0 for k in ("fetched", "new", "duplicates", "invalid",
                                 "sources_ok", "sources_failed")}
        collect(store, c, "r1", AS_OF, ("arxiv",), {"arxiv": slow},
                counts, lambda *_: None)
        self.assertEqual(counts.get("collect_budget_exceeded"), 2)
        rows = store.run_sources("r1")
        skipped = [r for r in rows if "collect budget exceeded" in (r["error"] or "")]
        self.assertEqual(len(skipped), 2)
        store.close()

    def test_recalc_recomputes_totals_under_current_weights(self):
        # 規程 A2 類: 保存済み評価の総合点を現行の重みで再計算する (LLM不要)
        from trlcore.recalc import recalculate_evaluations
        store = Store(self.path)
        rec, _ = normalize(raw(sid="2401.0001", title="Recalc test paper"),
                           "2026-10-20T00:00:00+00:00")
        cid, _ = store.upsert_candidate(rec, "r0")
        items = {"technology_readiness": {"score": 100},
                 **{k: {"score": 0} for k in
                    ("technological_advantage", "monetization", "market_growth",
                     "financial_sustainability", "moat_durability")}}
        ev = {"kind": "long", "as_of": "2026-10-20", "status": "final", "total": 25.0,
              "band": {"label": "弱い"}, "items": items, "extraction": None, "event": None,
              "prompt_versions": {}, "config_version": "2026.10.0", "config_hash": "old",
              "llm_provider": "gemini", "llm_model": "gemini-3.6-flash",
              "input_hash": "rh", "errors": [],
              "extra": {"trl_level": {"level": 7, "reason": "r", "evidence": "e"}}}
        store.add_evaluation("r0", cid, ev)
        store.add_evaluation("r0", cid, dict(ev, status="hold", total=None, band=None,
                                             input_hash="rh2"))

        dry = recalculate_evaluations(store, CFG, apply=False)
        self.assertEqual((dry["checked"], dry["changed"]), (1, 1))
        row = store.final_evaluations()[0]
        self.assertEqual(row["total"], 25.0)  # 報告のみでは書き換えない

        done = recalculate_evaluations(store, CFG, apply=True)
        self.assertEqual(done["changed"], 1)
        row = store.final_evaluations()[0]
        self.assertEqual(row["total"], 30.0)  # 現行の重み (技術成熟度 30) で再計算
        self.assertEqual(row["config_version"], CFG["config_version"])
        extra = json.loads(row["extra_json"])
        self.assertEqual(extra["recalculated"]["from_version"], "2026.10.0")
        self.assertEqual(extra["recalculated"]["old_total"], 25.0)
        again = recalculate_evaluations(store, CFG, apply=True)
        self.assertEqual(again["changed"], 0)  # 冪等
        store.close()

    def test_recalc_preserves_as_evaluated_total(self):
        # 非破壊: 評価当時の総合点は保存時に固定され、重み改訂の再計算を
        # 何度受けても残る (2回目の改訂で来歴ごと消える事故の防止)
        from trlcore.recalc import recalculate_evaluations
        store = Store(self.path)
        rec, _ = normalize(raw(sid="2401.0002", title="Original total paper"),
                           "2026-10-20T00:00:00+00:00")
        cid, _ = store.upsert_candidate(rec, "r0")
        items = {"technology_readiness": {"score": 100},
                 **{k: {"score": 0} for k in
                    ("technological_advantage", "monetization", "market_growth",
                     "financial_sustainability", "moat_durability")}}
        ev = {"kind": "long", "as_of": "2026-10-20", "status": "final", "total": 25.0,
              "band": {"label": "弱い"}, "items": items, "extraction": None, "event": None,
              "prompt_versions": {}, "config_version": "2026.10.0", "config_hash": "old",
              "llm_provider": "gemini", "llm_model": "gemini-3.6-flash",
              "input_hash": "oh", "errors": [],
              "extra": {"trl_level": {"level": 7, "reason": "r", "evidence": "e"}}}
        store.add_evaluation("r0", cid, ev)
        extra = json.loads(store.final_evaluations()[0]["extra_json"])
        self.assertEqual(extra["as_evaluated"]["total"], 25.0)  # 保存時に固定される
        self.assertEqual(extra["as_evaluated"]["config_version"], "2026.10.0")

        recalculate_evaluations(store, CFG, apply=True)  # 1回目の改訂 (現行の重み)
        row = store.final_evaluations()[0]
        self.assertEqual(row["total"], 30.0)
        self.assertEqual(json.loads(row["extra_json"])["as_evaluated"]["total"], 25.0)

        cfg2 = copy.deepcopy(CFG)  # 2回目の改訂 (さらに別の重み)
        cfg2["scoring"]["long"]["weights"]["technology_readiness"] = 40
        cfg2["scoring"]["long"]["weights"]["monetization"] = 10
        cfg2["config_version"] = "2026.12.0"
        cfg2["config_hash"] = "newer"
        recalculate_evaluations(store, cfg2, apply=True)
        row = store.final_evaluations()[0]
        self.assertEqual(row["total"], 40.0)
        self.assertEqual(json.loads(row["extra_json"])["as_evaluated"]["total"], 25.0)
        store.close()

    def test_all_sources_failed_flag(self):
        store = Store(self.path)

        def boom(*a):
            raise RuntimeError("down")
        r = run_pipeline(store, self.cfg(), None, as_of=AS_OF, fetchers={"arxiv": boom, "pubmed": boom},
                         today=AS_OF, log=lambda *_: None)
        self.assertTrue(r["failed"])

    def test_both_kinds_independent_with_shared_extraction(self):
        store = Store(self.path)
        n = {"extract": 0}
        base = make_llm_fn(long_scores={k: 70 for k in LONG_KEYS}, event_scores={k: 50 for k in EVENT_KEYS},
                           event={"classification": 2, "market_expectation": "x"})

        def fn(p):
            if "根拠抽出」担当" in p:
                n["extract"] += 1
            return base(p)
        r = run_pipeline(store, self.cfg(), Mock(fn), as_of=AS_OF, kinds=("long", "event"), fetchers=self.fetchers(),
                         today=AS_OF, log=lambda *_: None)
        self.assertEqual(n["extract"], 2)  # 候補2件、抽出は候補ごとに1回
        self.assertEqual(r["counts"]["final"], 4)
        totals = {(e["kind"], e["total"]) for e in store.evaluations_for_run(r["run_id"])}
        self.assertEqual(totals, {("long", 70.0), ("event", 50.0)})  # 平均されない


class RepoFilesTests(unittest.TestCase):
    def test_all_required_files_exist_in_repo(self):
        self.assertEqual(T.missing_files(), [], "リポジトリ直下に必須ファイルがありません")

    def test_missing_json_gives_clear_error(self):
        d = Path(tempfile.mkdtemp())
        with self.assertRaises(ConfigError) as cm:
            T.load_config(d)
        self.assertIn("scoring.json", str(cm.exception))
        (d / "scoring.json").write_bytes((T.ROOT / "scoring.json").read_bytes())
        with self.assertRaises(ConfigError) as cm:
            T.load_config(d)
        self.assertIn("sources.json", str(cm.exception))
        self.assertIn("sources.json", T.missing_files(d))

    def test_prompt_files_named_by_scoring_json_are_required(self):
        names = [n for n, _ in T.required_files()]
        for n, v in CFG["scoring"]["prompts"].items():
            self.assertIn(f"{n}.{v}.md", names)


class SourcesSchemaTests(unittest.TestCase):
    def good(self):
        return copy.deepcopy(CFG["sources"])

    def test_shipped_sources_valid(self):
        T.validate_sources(CFG["sources"])

    def test_invalid_sources_rejected(self):
        cases = []
        s = self.good(); del s["defaults"]; cases.append(s)
        s = self.good(); s["defaults"]["max_results"] = 0; cases.append(s)
        s = self.good(); s["queries"] = []; cases.append(s)
        s = self.good(); s["queries"][0] = {"name": "x"}; cases.append(s)
        s = self.good(); s["queries"][0]["name"] = ""; cases.append(s)
        s = self.good(); s["queries"].append(dict(s["queries"][0])); cases.append(s)
        s = self.good(); s["queries"][0]["lookback_days"] = "30"; cases.append(s)
        for c in cases:
            with self.assertRaises(ConfigError):
                T.validate_sources(c)

    def test_invalid_screening_or_prompts_rejected(self):
        bad = copy.deepcopy(CFG)
        bad["scoring"]["screening"]["max_age_days"] = "365"
        with self.assertRaises(ConfigError):
            validate_config(bad)
        bad = copy.deepcopy(CFG)
        del bad["scoring"]["prompts"]["event"]
        with self.assertRaises(ConfigError):
            validate_config(bad)


class PruneTests(unittest.TestCase):
    """古い材料の削除ポリシー。評価済み・キュー入りは決して消さない。"""

    def _add(self, store, sid, source="gnews", info_type="news", published="2026-01-01",
             status=None, url=None, affiliations=None):
        kw = dict(source=source, sid=sid, info_type=info_type, published=published,
                  url=url or f"https://example.com/{sid}", abstract="材料テキスト")
        if affiliations:
            kw["affiliations"] = affiliations
        rec, _ = normalize(raw(**kw), "2026-10-20T00:00:00+00:00")
        cid, _ = store.upsert_candidate(rec, "r0")
        links = link_entities(rec, CFG.get("entities", {}))
        if links:
            store.set_entities(cid, links)
        if status:
            store.set_status(cid, status)
        return cid

    def test_prune_age_rules_and_protection(self):
        with tempfile.TemporaryDirectory() as td:
            store = T.Store(Path(td) / "d.sqlite")
            old_news = self._add(store, "old-news", published="2026-01-01")
            new_news = self._add(store, "new-news", published="2026-10-01")
            old_disc = self._add(store, "old-disc", source="yahoo", info_type="disclosure",
                                 published="2023-01-01")
            evaluated_news = self._add(store, "eval-news", published="2026-01-01")
            store.conn.execute("INSERT INTO evaluations(run_id, candidate_id, kind, status) "
                               "VALUES('r0', ?, 'long', 'ok')", (evaluated_news,))
            store.conn.commit()
            queued_news = self._add(store, "queue-news", published="2026-01-01")
            store.upsert_queue(queued_news, "long", "理由", [], [], [], "2026-10-20T00:00:00+00:00", {})
            counts = store.prune(AS_OF, entity_markets={})
            self.assertEqual(counts["news"], 1)
            self.assertEqual(counts["disclosure"], 1)
            self.assertIsNone(store.get_candidate(old_news))
            self.assertIsNone(store.get_candidate(old_disc))
            self.assertIsNotNone(store.get_candidate(new_news))
            self.assertIsNotNone(store.get_candidate(evaluated_news))  # 評価済みは保護
            self.assertIsNotNone(store.get_candidate(queued_news))     # キュー入りは保護
            store.close()

    def test_prune_stale_links(self):
        with tempfile.TemporaryDirectory() as td:
            store = T.Store(Path(td) / "d.sqlite")
            ins = ("INSERT INTO candidates(id, source, source_id, status) VALUES(?, 'pubmed', ?, 'screened')")
            link = ("INSERT INTO candidate_entities(candidate_id, entity_id, strength, matched_by, matched_alias) "
                    "VALUES(?, ?, 'strong', 'affiliation', ?)")
            store.conn.execute(ins, ("k1", "k1"))
            store.conn.execute(link, ("k1", "genmab", "Genmab"))        # 現行辞書にある → 残る
            store.conn.execute(ins, ("k2", "k2"))
            store.conn.execute(link, ("k2", "genmab", "消えたエイリアス"))  # 辞書に無い → 消える
            store.conn.execute(ins, ("k3", "k3"))
            store.conn.execute(link, ("k3", "no_such_entity", "何か"))    # 辞書外の企業 → 触らない
            store.conn.commit()
            entities = CFG.get("entities", {}).get("entities", [])
            self.assertEqual(store.prune_stale_links(entities), 1)
            rows = [tuple(r) for r in store.conn.execute(
                "SELECT candidate_id, entity_id, matched_alias FROM candidate_entities ORDER BY candidate_id")]
            self.assertEqual(rows, [("k1", "genmab", "Genmab"), ("k3", "no_such_entity", "何か")])

    def test_start_run_closes_stale_running(self):
        with tempfile.TemporaryDirectory() as td:
            store = T.Store(Path(td) / "d.sqlite")
            store.start_run("r1", "v", "h", "2026-10-20", {})
            self.assertEqual(store.get_run("r1")["status"], "running")
            store.start_run("r2", "v", "h", "2026-10-27", {})  # r1 は異常終了したものとして確定する
            self.assertEqual(store.get_run("r1")["status"], "interrupted")
            self.assertEqual(store.get_run("r2")["status"], "running")
            store.finish_run("r2", {}, "ok", 1.0)
            store.start_run("r3", "v", "h", "2026-11-03", {})  # 終了済みのものは触らない
            self.assertEqual(store.get_run("r2")["status"], "ok")

    def test_prune_stale_yahoo_twins(self):
        with tempfile.TemporaryDirectory() as td:
            store = T.Store(Path(td) / "d.sqlite")
            aff = ["Oxford Nanopore Technologies"]
            url = "https://finance.yahoo.com/quote/ONT.L"
            old = self._add(store, "ONT-2026-06-30", source="yahoo", info_type="disclosure",
                            published="2026-06-30", url=url, affiliations=aff)
            new = self._add(store, "ONT.L-2026-06-30", source="yahoo", info_type="disclosure",
                            published="2026-06-30", url=url, affiliations=aff)
            markets = {e["id"]: e.get("market") or "JP"
                       for e in CFG["entities"]["entities"]}
            counts = store.prune(AS_OF, entity_markets=markets)
            self.assertEqual(counts["stale_yahoo_keys"], 1)
            self.assertIsNone(store.get_candidate(old))
            self.assertIsNotNone(store.get_candidate(new))
            store.close()

    def test_prune_stale_yahoo_twins_without_identifier_row(self):
        # 本番の盲点の再現: 旧キー側に identifiers 行が無いと、索引経由の
        # 検出ではツインを見つけられなかった (source_id 直読みで検出する)
        with tempfile.TemporaryDirectory() as td:
            store = T.Store(Path(td) / "d.sqlite")
            aff = ["Oxford Nanopore Technologies"]
            url = "https://finance.yahoo.com/quote/ONT.L"
            old = self._add(store, "ONT-2026-06-30", source="yahoo", info_type="disclosure",
                            published="2026-06-30", url=url, affiliations=aff)
            new = self._add(store, "ONT.L-2026-06-30", source="yahoo", info_type="disclosure",
                            published="2026-06-30", url=url, affiliations=aff)
            store.conn.execute("DELETE FROM identifiers WHERE candidate_id=?", (old,))
            store.conn.commit()
            markets = {e["id"]: e.get("market") or "JP"
                       for e in CFG["entities"]["entities"]}
            counts = store.prune(AS_OF, entity_markets=markets)
            self.assertEqual(counts["stale_yahoo_keys"], 1)
            self.assertIsNone(store.get_candidate(old))
            self.assertIsNotNone(store.get_candidate(new))
            store.close()

    def test_prune_keeps_old_key_without_counterpart(self):
        with tempfile.TemporaryDirectory() as td:
            store = T.Store(Path(td) / "d.sqlite")
            aff = ["Oxford Nanopore Technologies"]
            only = self._add(store, "ONT-2026-06-30", source="yahoo", info_type="disclosure",
                             published="2026-06-30", url="https://finance.yahoo.com/quote/ONT.L",
                             affiliations=aff)
            markets = {e["id"]: e.get("market") or "JP"
                       for e in CFG["entities"]["entities"]}
            counts = store.prune(AS_OF, entity_markets=markets)
            self.assertEqual(counts["stale_yahoo_keys"], 0)
            self.assertIsNotNone(store.get_candidate(only))  # 唯一の財務は残す
            store.close()


class ReportWarningTests(unittest.TestCase):
    def test_report_surfaces_failed_and_quiet_zero(self):
        # 「静かな0件」と収集失敗は、レポートの頭で見えることが検知の前提
        import os, tempfile
        from trlcore.report import build_report
        from trlcore.store import Store
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            store = Store(path)
            store.start_run("run-x", "2026.11.0", "hash", "2026-11-04", {})
            store.finish_run("run-x", {"fetched": 0, "sources_failed": 3,
                                       "quiet_zero_sources": ["arxiv", "openalex"]},
                             "partial", 12.3)
            text = build_report(store, "run-x")
            self.assertIn("収集に失敗したクエリ 3 件", text)
            self.assertIn("取得0件のまま成功したソース: arxiv, openalex", text)
            store.start_run("run-y", "2026.11.0", "hash", "2026-11-04", {})
            store.finish_run("run-y", {"fetched": 10}, "ok", 9.0)
            text2 = build_report(store, "run-y")
            self.assertNotIn("取得0件のまま成功したソース", text2)
            self.assertNotIn("収集に失敗したクエリ", text2)
            store.close()
        finally:
            os.unlink(path)


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "d" / "trl.sqlite"
        self.out = Path(self.tmp.name) / "vault"

    def tearDown(self):
        self.tmp.cleanup()

    def _build_db(self):
        store = Store(self.path)
        cfg = dict(CFG)
        cfg["sources"] = {"defaults": {"max_results": 5, "lookback_days": 30},
                          "queries": [{"name": "q", "arxiv": "a"}]}

        def arx(q, n, days, as_of):
            return [raw(sid="2610.0001", doi="10.1/a")]
        run_pipeline(store, cfg, Mock(make_llm_fn(long_scores={k: 65 for k in LONG_KEYS})),
                     as_of=AS_OF, kinds=("long",), fetchers={"arxiv": arx},
                     today=AS_OF, log=lambda *_: None)
        store.close()
        return cfg

    def test_vault_export_is_readonly_and_complete(self):
        import hashlib
        from trlcore.vault import export_vault
        cfg = self._build_db()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        res = export_vault(self.path, cfg, self.out)
        after = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(before, after)  # 読み取り専用: 元データは一切変わらない
        self.assertEqual(res["evaluations"], 1)
        notes = list((self.out / "evals").glob("*.md"))
        self.assertEqual(len(notes), 1)
        text = notes[0].read_text(encoding="utf-8")
        self.assertIn("## 要旨（原文）", text)
        self.assertIn("## 要旨（日本語訳）", text)
        self.assertIn("未取得（未翻訳）", text)  # 無い情報は捏造せず未取得と書く
        self.assertIn("https://example.org/2610.0001", text)  # 出典URL
        self.assertIn("取得:", text)
        self.assertIn(f"config_version: \"{CFG['config_version']}\"", text)
        self.assertIn("../companies/", text)  # 標準 Markdown の相対リンク
        self.assertNotIn("[[", text)  # 特定ツールの固有記法に依存しない
        comp = list((self.out / "companies").glob("*.md"))
        self.assertEqual(len(comp), 1)
        self.assertIn("../evals/", comp[0].read_text(encoding="utf-8"))

    def test_vault_does_not_touch_unmanaged_files(self):
        from trlcore.vault import export_vault
        cfg = self._build_db()
        (self.out / "notes").mkdir(parents=True)
        mine = self.out / "notes" / "README.md"
        mine.write_text("自分のメモ", encoding="utf-8")
        stray = self.out / "stray.txt"
        stray.write_text("keep", encoding="utf-8")
        export_vault(self.path, cfg, self.out)
        self.assertEqual(mine.read_text(encoding="utf-8"), "自分のメモ")
        self.assertEqual(stray.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()

