"""関門導出 (trlcore/barriers.py、barriers/v1) のテスト。

実行: python -m unittest test_barriers -v
規則の正本は設計書 (~/workspace/trl-phases/DESIGN-V2.md §4)。閾値の
根拠: Valley の1年は継続企業評価の地平 (規程第12条5と一致)、
Darwinian Sea のゲート TRL ≥7 は泰崇の追加判断 (TRL7-8 でも起こりうる)。
"""
import unittest

from trlcore.barriers import (BARRIER_ORDER, BARRIER_RULES_VERSION,
                              annual_revenue_series, barriers_for_evaluation,
                              compute_runway, parse_financial_text,
                              parse_trial_status, referenced_ncts,
                              trial_started_phase1)

EDGAR_TEXT = (
    "INSMED INC(INSM)の財務数値(SEC EDGAR): 直近四半期(2026-09-30まで) "
    "売上高 524百万米ドル、純利益 △100百万米ドル、営業キャッシュフロー △223百万米ドル。"
    "通期(2025-12-31まで) 売上高 606百万米ドル、純利益 △1,277百万米ドル、"
    "営業キャッシュフロー △935百万米ドル。"
    "貸借対照表(2026-09-30時点) 現金及び現金同等物 397百万米ドル、"
    "総資産 2,226百万米ドル、純資産 726百万米ドル、短期有価証券 750百万米ドル、"
    "自己資本比率 32.6%。")

YAHOO_TEXT = (
    "Idorsia Ltd(IDIA)の財務数値(Yahoo Finance): 直近四半期(2026-09-30まで) "
    "売上高 62百万スイスフラン、純利益 △53百万スイスフラン、"
    "通期(2025-12-31まで) 売上高 221百万スイスフラン、純利益 △112百万スイスフラン、"
    "最新指標 現預金 89百万スイスフラン、総有利子負債 1,319百万スイスフラン、流動比率 0.58。")

EDINET_TEXT = (
    "レーザーテック株式会社がEDINETに有価証券報告書－第64期(2025/07/01－2026/06/30)を提出した。"
    "対象期間: 2025-07-01〜2026-06-30。 財務数値(EDINET XBRL): "
    "売上高 230,485百万円(前期 251,477百万円)、純利益 77,485百万円(前期 84,652百万円)、"
    "営業キャッシュフロー 48,540百万円(前期 77,874百万円)。")


def fin(text, source="edgar", published="2026-09-30", title="財務情報"):
    return {"id": "c_fin", "source": source, "title": title,
            "abstract": text, "published": published}


def roic_text(end, revenue, net=-50):
    return (f"Z Corp(Z)の財務数値(ROIC.ai): 直近通期({end}まで) "
            f"売上高 {revenue}百万米ドル、純利益 {'△' if net < 0 else ''}{abs(net)}百万米ドル。"
            f"貸借対照表({end}時点) 現金及び現金同等物 100百万米ドル、"
            "総資産 500百万米ドル、純資産 300百万米ドル。")


class TrialParseTests(unittest.TestCase):
    def test_status_and_phase(self):
        t = parse_trial_status("... Status: RECRUITING. Phase: PHASE2. Sponsor: X.")
        self.assertEqual(t, {"status": "RECRUITING", "max_phase": 2})
        self.assertTrue(trial_started_phase1(t))

    def test_not_yet_recruiting_is_not_started(self):
        t = parse_trial_status("Status: NOT_YET_RECRUITING. Phase: PHASE2.")
        self.assertFalse(trial_started_phase1(t))

    def test_phase_variants(self):
        self.assertEqual(parse_trial_status("Phase: PHASE1/PHASE2")["max_phase"], 2)
        self.assertIsNone(parse_trial_status("Phase: NA")["max_phase"])
        self.assertFalse(trial_started_phase1(
            {"status": "RECRUITING", "max_phase": None}))
        for st in ("ACTIVE_NOT_RECRUITING", "COMPLETED", "ENROLLING_BY_INVITATION"):
            self.assertTrue(trial_started_phase1({"status": st, "max_phase": 1}), st)

    def test_referenced_ncts_from_facts(self):
        ex = {"facts": [{"source": "https://clinicaltrials.gov/study/NCT07821762",
                         "statement": "第2相試験"}],
              "missing_info": ["NCT99999999 は根拠ではない"]}
        # 根拠 (facts) の参照だけを拾い、missing_info の番号は拾わない
        self.assertEqual(referenced_ncts(ex), ["NCT07821762"])
        self.assertEqual(referenced_ncts(None), [])
        self.assertEqual(referenced_ncts({}), [])


class FinancialParseTests(unittest.TestCase):
    def test_edgar_format(self):
        p = parse_financial_text(EDGAR_TEXT)
        self.assertEqual(p["source_label"], "SEC EDGAR")
        a = p["annual"]["2025-12-31"]
        self.assertEqual(a["revenue"]["value"], 606)
        self.assertEqual(a["net_income"]["value"], -1277)
        self.assertEqual(p["balance"]["cash"]["value"], 397)
        self.assertEqual(p["balance"]["short_investments"]["value"], 750)

    def test_yahoo_latest_cash_is_broad(self):
        p = parse_financial_text(YAHOO_TEXT)
        self.assertEqual(p["latest_cash"]["value"], 89)
        self.assertEqual(p["latest_cash"]["currency"], "スイスフラン")
        self.assertEqual(p["annual"]["2025-12-31"]["net_income"]["value"], -112)

    def test_edinet_generic_annual_not_in_series(self):
        p = parse_financial_text(EDINET_TEXT, "有価証券報告書－第64期(2025/07/01－2026/06/30)")
        self.assertIsNotNone(p["generic_annual"])
        self.assertEqual(p["generic_annual"]["revenue"]["value"], 230485)
        # 前期の比較値は系列に入れない (前期の期間末日が明記されないため)
        self.assertEqual(annual_revenue_series([fin(EDINET_TEXT, "edinet")]), [])

    def test_runway_matches_codex_definition(self):
        # 原資=現預金+短期投資 (397+750)、burn=純損失 1277 (営業CF 935 より大)
        rw = compute_runway([fin(EDGAR_TEXT)])
        self.assertAlmostEqual(rw["runway"], 1147 / 1277, places=3)
        # Yahoo 形式: 広義の現預金 89 / 純損失 112
        rw2 = compute_runway([fin(YAHOO_TEXT, "yahoo")])
        self.assertAlmostEqual(rw2["runway"], 89 / 112, places=3)

    def test_runway_uses_operating_cf_when_larger(self):
        text = ("Aの財務数値(SEC EDGAR): 通期(2025-12-31まで) 売上高 100百万米ドル、"
                "純利益 △100百万米ドル、営業キャッシュフロー △400百万米ドル。"
                "貸借対照表(2026-09-30時点) 現金及び現金同等物 200百万米ドル。")
        rw = compute_runway([fin(text)])
        self.assertAlmostEqual(rw["runway"], 200 / 400, places=3)

    def test_runway_no_burn_and_unknown(self):
        text = ("Aの財務数値(SEC EDGAR): 通期(2025-12-31まで) 売上高 100百万米ドル、"
                "純利益 50百万米ドル。貸借対照表(2026-09-30時点) "
                "現金及び現金同等物 200百万米ドル。")
        rw = compute_runway([fin(text)])
        self.assertTrue(rw["no_burn"])
        self.assertIsNone(rw["runway"])
        self.assertIsNone(compute_runway([])["snapshot"])
        self.assertIsNone(compute_runway([fin("財務数値なし")])["snapshot"])


class BarrierRuleTests(unittest.TestCase):
    def test_devil_river(self):
        ex = {"facts": [{"source": "https://clinicaltrials.gov/study/NCT07821762",
                         "statement": "s"}]}
        trials = {"NCT07821762": {"status": "NOT_YET_RECRUITING", "max_phase": 2}}
        b = barriers_for_evaluation(2, ex, [], trials)
        self.assertEqual(b["v"], BARRIER_RULES_VERSION)
        self.assertEqual(b["devil_river"]["status"], "hit")
        self.assertIn("開始済みの Phase 1 以上なし", b["devil_river"]["evidence"])
        # 開始済みの Phase 1 以上があれば該当なし
        trials["NCT07821762"] = {"status": "RECRUITING", "max_phase": 1}
        self.assertEqual(barriers_for_evaluation(3, ex, [], trials)
                         ["devil_river"]["status"], "clear")
        # 根拠に治験参照が無くても該当 (判定しない条件は無い)
        self.assertEqual(barriers_for_evaluation(3, {"facts": []}, [], {})
                         ["devil_river"]["status"], "hit")
        # ゲート外 (TRL ≥4) は対象外
        self.assertEqual(barriers_for_evaluation(4, ex, [], trials)
                         ["devil_river"]["status"], "na")

    def test_valley_of_death(self):
        records = [fin(YAHOO_TEXT, "yahoo")]
        b = barriers_for_evaluation(8, {}, records, {})
        self.assertEqual(b["valley_of_death"]["status"], "hit")
        self.assertIn("ランウェイ 0.8年", b["valley_of_death"]["evidence"])
        self.assertIn("89百万スイスフラン", b["valley_of_death"]["evidence"])
        # 売上のある企業も対象 (V2-3): 売上 221 でも資金で該当する
        # ランウェイ ≥1年は該当なし
        rich = ("Aの財務数値(SEC EDGAR): 通期(2025-12-31まで) 売上高 100百万米ドル、"
                "純利益 △100百万米ドル。貸借対照表(2026-09-30時点) "
                "現金及び現金同等物 300百万米ドル。")
        self.assertEqual(barriers_for_evaluation(7, {}, [fin(rich)], {})
                         ["valley_of_death"]["status"], "clear")
        # 財務が抜けなければ判定しない (該当なしに倒さない)
        self.assertEqual(barriers_for_evaluation(7, {}, [], {})
                         ["valley_of_death"]["status"], "unknown")
        self.assertEqual(barriers_for_evaluation(7, {}, None, {})
                         ["valley_of_death"]["status"], "unknown")
        # ゲート外 (TRL ≤3)
        self.assertEqual(barriers_for_evaluation(3, {}, records, {})
                         ["valley_of_death"]["status"], "na")

    def test_darwinian_sea_gate_trl7(self):
        # ゲートは TRL ≥7 (追加判断)。TRL6 は対象外、TRL7 から判定する
        recs = [fin(roic_text("2024-12-31", 500), "roic", "2025-03-01"),
                fin(roic_text("2025-12-31", 400), "roic", "2026-03-01")]
        b6 = barriers_for_evaluation(6, {}, recs, {})
        self.assertEqual(b6["darwinian_sea"]["status"], "na")
        b7 = barriers_for_evaluation(7, {}, recs, {})
        self.assertEqual(b7["darwinian_sea"]["status"], "hit")
        self.assertIn("前年比 -20.0%", b7["darwinian_sea"]["evidence"])
        # 売上増なら該当なし
        recs_up = [fin(roic_text("2024-12-31", 400), "roic", "2025-03-01"),
                   fin(roic_text("2025-12-31", 500), "roic", "2026-03-01")]
        self.assertEqual(barriers_for_evaluation(9, {}, recs_up, {})
                         ["darwinian_sea"]["status"], "clear")
        # 2年度分が無ければ判定しない
        self.assertEqual(barriers_for_evaluation(9, {}, recs[:1], {})
                         ["darwinian_sea"]["status"], "unknown")
        self.assertEqual(barriers_for_evaluation(9, {}, [], {})
                         ["darwinian_sea"]["status"], "unknown")

    def test_invalid_trl_is_na(self):
        for bad in (None, 0, 10, "7", True):
            b = barriers_for_evaluation(bad, {}, [], {})
            for k in BARRIER_ORDER:
                self.assertEqual(b[k]["status"], "na", (bad, k))


if __name__ == "__main__":
    unittest.main()
