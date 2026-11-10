"""フェーズ導出 (trlcore/phases.py、phases/v1) の境界テスト。

実行: python -m unittest test_phases -v
境界の論拠は設計書 (~/workspace/trl-phases/DESIGN.md §3): 障壁区間には
「移行の手前側の最終レベル」を割り当て、ダーウィンの海に定着期は無い。
"""
import unittest

from trlcore.phases import (BARRIER_ORDER, PHASE_RULES_VERSION, ZONE_ORDER,
                            ZONE_SPANS, phase_for_trl)


class PhaseForTrlTests(unittest.TestCase):
    def test_zone_for_all_levels(self):
        # TRL の9レベルがちょうど1つの区間に属する (現在地が一意に決まる)
        expected = {1: "research", 2: "research", 3: "devil_river",
                    4: "development", 5: "development", 6: "valley_of_death",
                    7: "commercialization", 8: "commercialization",
                    9: "darwinian_sea"}
        for level, zone in expected.items():
            ph = phase_for_trl(level)
            self.assertIsNotNone(ph, level)
            self.assertEqual(ph["v"], PHASE_RULES_VERSION)
            self.assertEqual(ph["trl"], level)
            self.assertEqual(ph["zone"], zone)
        # 区間の TRL 幅の合計は9 (トラックの比例幅の前提)
        self.assertEqual(sum(ZONE_SPANS[z] for z in ZONE_ORDER), 9)

    def test_barrier_states(self):
        # 魔の川: 前駆期 1〜2 / 移行期 3 / 定着期 4〜9
        # 死の谷: 前駆期 1〜5 / 移行期 6 / 定着期 7〜9
        # ダーウィンの海: 前駆期 1〜8 / 移行期 9 / 定着期は定義しない
        cases = {
            1: ("before", "before", "before"),
            2: ("before", "before", "before"),
            3: ("in", "before", "before"),
            4: ("crossed", "before", "before"),
            5: ("crossed", "before", "before"),
            6: ("crossed", "in", "before"),
            7: ("crossed", "crossed", "before"),
            8: ("crossed", "crossed", "before"),
            9: ("crossed", "crossed", "in"),
        }
        for level, states in cases.items():
            barriers = phase_for_trl(level)["barriers"]
            self.assertEqual(tuple(barriers[k] for k in BARRIER_ORDER),
                             states, level)
            # 海の定着期はどのレベルでも現れない (P-2)
            self.assertNotEqual(barriers["darwinian_sea"], "crossed")

    def test_invalid_levels_have_no_phase(self):
        for bad in (None, 0, 10, -1, "6", 6.0, True, [6]):
            self.assertIsNone(phase_for_trl(bad), bad)


if __name__ == "__main__":
    unittest.main()
