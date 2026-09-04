import ast
import unittest
from pathlib import Path


class IntradayClassifierSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = Path(__file__).resolve().parents[1]
        cls.path = base / "vps_intraday_test.py"
        if not cls.path.exists():
            cls.path = base.parent / "vps_intraday_test.py"
        if not cls.path.exists():
            raise unittest.SkipTest("8000 main-site module is not installed in the AB engine tree")
        cls.source = cls.path.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_intraday_service_has_no_limitup_exclude_switch(self):
        names = {node.id for node in ast.walk(self.tree) if isinstance(node, ast.Name)}
        self.assertNotIn("limitup_exclude", names)

    def test_intraday_weakness_branches_do_not_assign_reject_group(self):
        reject_assignments = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == "group" for t in node.targets):
                    if isinstance(node.value, ast.Constant) and node.value.value == "排除":
                        reject_assignments.append(node.lineno)
        self.assertEqual(reject_assignments, [])

    def test_intraday_result_exposes_exact_limit_flag(self):
        seven_returns = [node for node in ast.walk(self.tree) if isinstance(node, ast.Return)
                         and isinstance(node.value, ast.Dict)]
        keys = {k.value for node in seven_returns for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        self.assertIn("is_limit_up", keys)

    def test_negative_institution_streak_is_labeled_as_selling(self):
        self.assertIn('detail = f"法人連賣 {abs(int(streak))} 日"', self.source)
        self.assertNotIn('"detail": f"法人連買 {streak} 日"', self.source)

    def test_day_position_is_clamped_to_valid_percentile(self):
        base = Path(__file__).resolve().parents[1]
        path = base / "screen_intraday.py"
        if not path.exists():
            raise unittest.SkipTest("screen_intraday.py is not installed in this tree")
        source = path.read_text(encoding="utf-8")
        self.assertIn("max(0, min(100", source)
        self.assertIn("day_position_pct", source)

    def test_display_order_is_group_then_score_pct_score_change_and_aflow(self):
        self.assertIn('DISPLAY_GROUP_ORDER = {"可操作": 0, "觀察": 1, "排除": 2}', self.source)
        key = self.source.split("def _display_sort_key(row):", 1)[1].split("\n\ndef _sort_display_rows", 1)[0]
        positions = [
            key.index('DISPLAY_GROUP_ORDER.get(row.get("group"), 9)'),
            key.index('row.get("score_pct")'),
            key.index('row.get("score")'),
            key.index('row.get("change_rate")'),
            key.index('row.get("aflow")'),
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("_sort_display_rows(rows)", self.source)

    def test_bearish_chip_cannot_reach_actionable_group_even_if_score_passes(self):
        """5483/6182 型態:盤中三因子達標(65%+),但法人籌碼明顯偏空(連賣/近月
        賣超)→ 首頁不得直接判「可操作」,也不得直接排除,要落在等待確認
        (Vanessa 2026-08-27 規格第七、八條；2026-09-02 起依 CLAUDE.md
        風險調整後參與規範，維度分離：chip_bearish 只擋 ENTRY gate、
        不再單獨轉成排除/AVOID 的專屬分支)。"""
        self.assertIn(
            "chip_bearish = ((chip_streak is not None and chip_streak <= -3) or",
            self.source)

        # ENTRY 那支 elif 必須把 chip_bearish 納入守門條件，籌碼偏空時
        # 不能被判「可操作」。
        entry_branch = self.source.split(
            'elif (money_nature["code"] == "TRUE_MOMENTUM" and core_entry and\n'
            '          not chip_bearish and not entry_missing and pct is not None and pct >= 65):',
            1)
        self.assertEqual(len(entry_branch), 2,
                          "ENTRY 分支必須以 not chip_bearish 守門，籌碼偏空時不得判可操作")
        self.assertIn('group, subgroup = "可操作", "🟢 可進場"', entry_branch[1][:200])

        # 落回等待確認時要點名「法人籌碼改善」，不能悄悄消失成沒有理由。
        self.assertIn('wait_for.append("法人籌碼改善")', self.source)


if __name__ == "__main__":
    unittest.main()
