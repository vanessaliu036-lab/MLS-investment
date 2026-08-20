"""Layer 0 閘的 stale 護欄 —— 昨天的寬度不准清空今天的名單。

存在理由(2026-08-19 實際咬過一次):
  candidate_pool 08-18 = {攻擊軌18, 引擎軌20, 觀察13}
  candidate_pool 08-19 = {觀察: 51}  ← 全數禁新倉
但 08-19 真實全市場寬度是 36.4%(漲858/跌1245),高於 30% 門檻,根本不該判 Risk Off。
結果 08-20 整天沒有任何可操作名單,隔日命中率分母也會是 0。

根因:_read_regime() 沒檢查 is_stale,而 build 跑在 15:05、TWSE EOD 常常還沒發布,
拿到的是前一交易日的收盤寬度。同模組 assess() 早就防了(breadth_live=None if stale),
只是整條管線沒人呼叫它。
"""
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import screen_post as sp
import market_regime as mr


def _row(pct, stale, date="2026-08-18"):
    return {"true_breadth": pct / 100, "advancing": 600, "declining": 1600,
            "total": 2222, "data_date": date, "is_stale": stale}


class StaleBreadthGuardTests(unittest.TestCase):

    def setUp(self):
        self._orig = mr.fetch_breadth

    def tearDown(self):
        mr.fetch_breadth = self._orig

    def _regime(self, pct, stale, date="2026-08-18"):
        mr.fetch_breadth = lambda force=False: _row(pct, stale, date)
        return sp._read_regime()

    def test_stale_breadth_never_triggers_risk_off(self):
        """昨日寬度低到 5% 也不准把今天定調成 Risk Off。"""
        for pct in (5.0, 15.0, 27.0, 29.9):
            r = self._regime(pct, stale=True)
            self.assertFalse(r["risk_off"], f"{pct}% stale 不該 risk_off")
            self.assertTrue(r["stale"])
            # 數字仍要保留當診斷,不是丟掉
            self.assertEqual(r["breadth_pct"], pct)
            self.assertEqual(r["breadth_date"], "2026-08-18")

    def test_same_day_breadth_still_triggers_risk_off(self):
        """當日寬度低於門檻時,Risk Off 照常成立 —— 護欄不能把閘整個廢掉。"""
        r = self._regime(27.0, stale=False, date="2026-08-19")
        self.assertTrue(r["risk_off"])
        self.assertFalse(r["stale"])

    def test_same_day_healthy_breadth_not_risk_off(self):
        r = self._regime(36.4, stale=False, date="2026-08-19")
        self.assertFalse(r["risk_off"])

    def test_08_19_real_numbers_should_not_have_been_risk_off(self):
        """08-19 真實數字(36.4%)無論 stale 與否都不該 Risk Off。"""
        for stale in (True, False):
            self.assertFalse(self._regime(36.4, stale=stale)["risk_off"])

    def test_stale_still_reports_risk_on_as_false(self):
        """stale 時也不准反向宣稱 Risk On —— 昨天的數字兩個方向都不可信。"""
        r = self._regime(85.0, stale=True)
        self.assertFalse(r["risk_on"])
        self.assertFalse(r["risk_off"])

    def test_fetch_failure_still_degrades_safely(self):
        mr.fetch_breadth = lambda force=False: (_ for _ in ()).throw(OSError("down"))
        r = sp._read_regime()
        self.assertTrue(r["unknown"])
        self.assertFalse(r["risk_off"])

    def test_purpose_states_the_data_is_yesterdays(self):
        """purpose 要誠實講明用的是前一交易日的值,不能靜默。"""
        r = self._regime(27.0, stale=True)
        txt = sp._pool_purpose("2026-08-20", [{"score": 10}], r)
        self.assertIn("尚未發布", txt)
        self.assertIn("2026-08-18", txt)
        self.assertNotIn("Risk Off", txt)


if __name__ == "__main__":
    unittest.main()
