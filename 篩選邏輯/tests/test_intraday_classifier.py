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


if __name__ == "__main__":
    unittest.main()
