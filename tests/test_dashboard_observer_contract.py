# -*- coding: utf-8 -*-
from pathlib import Path
import unittest


HTML = (Path(__file__).parents[1] / "intraday_decision_dataflow.html").read_text(
    encoding="utf-8"
)


class DashboardObserverContractTest(unittest.TestCase):
    def test_post_status_observer_does_not_rewrite_unchanged_text(self):
        """The observer must not create the same mutation that wakes it again."""
        self.assertIn(
            "if(pill.textContent!==nextStatus)pill.textContent=nextStatus", HTML
        )


if __name__ == "__main__":
    unittest.main()
