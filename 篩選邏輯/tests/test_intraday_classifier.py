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
        self.assertIn('streak_detail = f"法人連賣 {abs(int(streak))} 日"', self.source)
        self.assertNotIn('"detail": f"法人連買 {streak} 日"', self.source)


if __name__ == "__main__":
    unittest.main()
