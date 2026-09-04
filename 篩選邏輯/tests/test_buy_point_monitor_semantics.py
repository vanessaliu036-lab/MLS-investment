import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import navigation
import line_b_layers_render as render


class BuyPointMonitorSemanticsTests(unittest.TestCase):
    def test_navigation_distinguishes_research_ledger_from_buy_point_monitor(self):
        labels = {key: label for key, label, _href, _badge in navigation.NAV_ITEMS}
        self.assertEqual(labels["line-b"], "Line B C1+C2 研究")
        self.assertEqual(labels["layers"], "買點監控｜七層")

    def test_untriggered_stock_does_not_present_key_price_as_current_failure(self):
        row = {
            "code": "3532", "name": "台勝科", "price": 387.0, "trigger_price": 402.0,
            "distance_pct": -3.73,
            "chip": {"verdict": "BULLISH_TRUST", "summary": "偏多／投信主導",
                     "foreign_5d": -1346, "total_5d": 1000},
            "flow": {"verdict": "NEGATIVE", "net_active": -74, "flow_state": "翻空"},
            "trigger": {"verdict": "NO", "hold_minutes": 0},
            "volume": {"verdict": "THIN", "rvol": 0.39, "rvol_base_days": 22,
                       "turnover_pct": None},
            "acceptance": {"verdict": "N/A", "held_minutes": 0,
                           "max_drawdown_pct": None},
            "extension": {"verdict": "NORMAL", "reasons": [], "change_rate": 1.57,
                          "dist_ma5_pct": -0.6, "dist_ma20_pct": 8.3, "gap_pct": None},
            "sector": {"verdict": "STRONG", "group": "晶圓材料", "breadth_pct": 100.0},
            "state": {"state": "WATCH", "action": "不追",
                      "why": "尚未接近觸發價"},
            "trade_judgment": {
                "trend_stage": "未啟動", "flow_state": "翻空", "chase_permission": "不追",
                "entry_method": "回踩接",
                "failure_conditions": ["跌 VWAP", "A-flow 翻負", "爆量滯漲", "跌破關鍵價"],
                "failure_alerts": ["A-flow 翻負"],
            },
        }
        html = render._mobile_card(row)
        self.assertIn("PRICE TRIGGER", html)
        self.assertIn("NO · 觸發 402.00", html)
        self.assertIn("未來失敗條件", html)
        self.assertIn("目前異常：A-flow 翻負", html)
        self.assertNotIn("目前失敗：", html)


if __name__ == "__main__":
    unittest.main()
