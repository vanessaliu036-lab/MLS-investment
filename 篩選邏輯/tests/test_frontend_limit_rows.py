import unittest
from pathlib import Path


class FrontendLimitRowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = Path(__file__).resolve().parents[1]
        path = base / "intraday_decision_dataflow.html"
        if not path.exists():
            path = base.parent / "intraday_decision_dataflow.html"
        if not path.exists():
            raise unittest.SkipTest("8000 main-site HTML is not installed in the AB engine tree")
        cls.html = path.read_text(encoding="utf-8")

    def test_shared_exact_limit_predicate_exists(self):
        self.assertIn("const exactLimitUp=", self.html)
        self.assertIn("row?.is_limit_up===true", self.html)

    def test_dropped_t1_rows_receive_limit_up_class(self):
        self.assertIn("exactLimitUp(x,x.t1_price,x.t1_change)?'limit-up-row'", self.html)

    def test_limit_rows_use_solid_taiwan_market_red(self):
        self.assertIn(".limit-up-row td,", self.html)
        self.assertIn("background:#e53935!important", self.html)

    def test_percentage_only_guess_is_removed(self):
        self.assertNotIn(">=9.8", self.html)

    def test_rejected_table_shows_recovery_fields(self):
        for label in ("淘汰原因", "救援訊號", "Recovery Score", "隔日觸發條件"):
            self.assertIn(label, self.html)


if __name__ == "__main__":
    unittest.main()
