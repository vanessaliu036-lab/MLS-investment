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
        cls.screen_path = base / "screen_intraday.py"
        cls.screen_source = (cls.screen_path.read_text(encoding="utf-8")
                             if cls.screen_path.exists() else "")

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
        self.assertIn('streak_detail = f"法人連賣 {abs(int(streak))} 日"', self.source)
        self.assertNotIn('"detail": f"法人連買 {streak} 日"', self.source)

    def test_day_position_is_clamped_to_valid_percentile(self):
        if not self.screen_source:
            raise unittest.SkipTest("screen_intraday.py is not installed in this tree")
        self.assertIn("max(0, min(100", self.screen_source)
        self.assertIn("day_position_pct", self.screen_source)

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
        self.assertIn("chip_bearish = (chip_streak is not None and chip_streak <= -3)", self.source)
        self.assertIn('group, subgroup = "觀察", "🔄 反轉候選（籌碼偏空）"', self.source)
        bearish_branch_pos = self.source.index(
            "elif not missing and pct is not None and pct >= 65 and chip_bearish:")
        actionable_branch_pos = self.source.index(
            'elif not missing and pct is not None and pct >= 65:\n'
            '        group, subgroup = "可操作"')
        self.assertLess(bearish_branch_pos, actionable_branch_pos,
                        "chip_bearish 分支必須排在「可操作」判定之前,否則籌碼偏空會漏判成可操作")

    def test_screen_passes_live_volume_and_aflow_sides_to_decision_view(self):
        if not self.screen_source:
            raise unittest.SkipTest("screen_intraday.py is not installed in this tree")
        self.assertIn('"volume": it.get("volume")', self.screen_source)
        self.assertIn('"intraday_volume_ratio": it.get("intraday_volume_ratio")', self.screen_source)
        self.assertIn('"active_buy": it.get("active_buy")', self.screen_source)
        self.assertIn('"active_sell": it.get("active_sell")', self.screen_source)

    def test_screen_never_calls_aflow_institutional_buy_sell(self):
        if not self.screen_source:
            raise unittest.SkipTest("screen_intraday.py is not installed in this tree")
        self.assertIn('conds["A-flow 轉正"] = na > 0', self.screen_source)
        self.assertNotIn('conds["主動買超"]', self.screen_source)

    def test_screen_normalizes_live_lots_to_daily_bar_shares_before_volume_checks(self):
        """Shioaji 盤中 volume=張；daily_bar volume/vol_ma20=股，實判不得直接相除。"""
        if not self.screen_source:
            raise unittest.SkipTest("screen_intraday.py is not installed in this tree")
        self.assertIn("vol_shares = vol * 1000", self.screen_source)
        self.assertIn("pace = vol_shares / max(1.0, y_vol * session_frac)", self.screen_source)
        self.assertIn("vol_shares > y_vol * 0.8", self.screen_source)
        self.assertNotIn("pace = vol / max(1.0, y_vol * session_frac)", self.screen_source)


if __name__ == "__main__":
    unittest.main()
