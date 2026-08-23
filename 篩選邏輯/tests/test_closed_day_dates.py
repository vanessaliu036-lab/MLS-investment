"""休市日的日期口徑 —— 非交易日不准出現在前端日期下拉。

存在理由(2026-08-23 週日實際咬到):
  /ab/watchlist 回 phase=CLOSED、data_date=2026-08-21,但 applies_date=2026-08-23。
  那是 load_for_premarket() 的 PRE 口徑(applies_date=今天)被休市分支照抄,
  「今天」在週日根本不是交易日。前端把 applies_date 併進日期下拉,
  於是下拉冒出 2026-08-23(週日)。休市時這批盤後池真正適用的是次一交易日。
"""
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient

import api
import screen_post
from phase import Phase, is_trading_day, next_trading_day
from datetime import date as _date


class ClosedDayDatesTests(unittest.TestCase):

    def setUp(self):
        self._orig_phase = api.get_phase
        self._orig_load = screen_post.load_for_premarket
        # 2026-08-21(五)盤後池,在 2026-08-23(日)被讀取
        screen_post.load_for_premarket = lambda *a, **k: {
            "phase": "PRE", "data_date": "2026-08-21",
            "applies_date": "2026-08-23", "purpose": "", "actionable": False,
            "generated_at": None, "degraded": [], "items": [], "dropped": [],
        }
        api.get_phase = lambda *a, **k: Phase.CLOSED
        self.client = TestClient(api.app)

    def tearDown(self):
        api.get_phase = self._orig_phase
        screen_post.load_for_premarket = self._orig_load

    def test_applies_date_is_next_trading_day_not_today(self):
        d = self.client.get("/api/watchlist").json()
        self.assertEqual(d["phase"], "CLOSED")
        self.assertEqual(d["data_date"], "2026-08-21")
        self.assertEqual(d["applies_date"], "2026-08-24")
        self.assertTrue(is_trading_day(_date.fromisoformat(d["applies_date"])))

    def test_dropdown_dates_are_all_trading_days(self):
        d = self.client.get("/api/watchlist").json()
        for x in d.get("dates") or []:
            self.assertTrue(is_trading_day(_date.fromisoformat(x)),
                            f"日期下拉出現非交易日 {x}")


if __name__ == "__main__":
    unittest.main()
