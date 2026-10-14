"""Synthetic, point-in-time tests for independent Fast Turn Watch v2."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "篩選邏輯"))
from fast_turn_watch import discover_eod, intraday_review

DAY = "2026-10-13"
NOW = "2026-10-14T10:07:00+08:00"

def base(**updates):
    row = dict(code="9999", data_date=DAY, close=94, prev_close=100,
               high=100, low=88, volume=200, vol_ma5=180)
    row.update(updates)
    return row

def live(**updates):
    row = dict(data_date="2026-10-14", sessions_since_candidate=1, quote_ts="2026-10-14T10:06:30+08:00",
               aflow_ts="2026-10-14T10:06:25+08:00",
               sector_ts="2026-10-14T10:06:20+08:00",
               price=95, open=94, vwap=94.5, session_low=93.5,
               change_pct=1.2, aflow=100, relative_volume=1.4,
               sector_change_pct=1.1, sector_advancing_ratio=0.65,
               positive_observations=2, resistance=105, market_regime="NEUTRAL")
    row.update(updates)
    return row

class FastTurnWatchTest(unittest.TestCase):
    def test_shock_day_is_watch_not_buy(self):
        result = discover_eod(base())
        self.assertEqual(result["status"], "WATCH")
        self.assertIn("SHOCK_ABSORPTION", result["signals"])
        self.assertFalse(result["trade_approved"])
        self.assertEqual(result["evidence_status"], "DISCOVERY ONLY")

    def test_long_lower_shadow_without_institution_buy(self):
        result = discover_eod(base(close=101,prev_close=100,high=104,low=94))
        self.assertIn("LONG_LOWER_SHADOW", result["signals"])

    def test_flow_price_divergence_needs_correct_asof(self):
        facts = base(close=99, high=103, low=94, institutional_net=1200,
                     institutional_date=DAY, volume=200, vol_ma5=190)
        self.assertIn("PRICE_FLOW_DIVERGENCE", discover_eod(facts)["signals"])
        facts["institutional_date"] = "2026-10-12"
        self.assertNotIn("PRICE_FLOW_DIVERGENCE", discover_eod(facts)["signals"])

    def test_new_sector_turn_requires_same_date(self):
        facts = base(close=100,low=95,high=103,sector_breadth=.62,
                     sector_breadth_previous=.43,sector_change_pct=1,
                     sector_data_date=DAY)
        self.assertIn("SECTOR_NEW_TURN", discover_eod(facts)["signals"])
        facts["sector_data_date"] = "2026-10-12"
        self.assertNotIn("SECTOR_NEW_TURN", discover_eod(facts)["signals"])

    def test_price_cap_and_missing_facts(self):
        self.assertEqual(discover_eod(base(close=1001,high=1010,prev_close=990,low=980))["status"],"EXCLUDED")
        self.assertEqual(discover_eod(base(low=None))["status"],"NO_DATA")

    def test_early_live_rebound(self):
        c = discover_eod(base())
        r = intraday_review(c, live(), NOW)
        self.assertEqual(r["status"],"READY_FOR_MANUAL_REVIEW")
        self.assertGreaterEqual(r["reward_to_risk"], 1.5)
        self.assertFalse(r["trade_approved"])

    def test_stale_quote_rejected(self):
        c = discover_eod(base())
        self.assertEqual(intraday_review(c,live(quote_ts="2026-10-14T10:00:00+08:00"), NOW)["status"],"NO_DATA")

    def test_stale_flow_and_sector_rejected(self):
        c = discover_eod(base())
        self.assertEqual(intraday_review(c,live(aflow_ts="2026-10-14T09:12:00+08:00"),NOW)["status"],"NO_DATA")
        self.assertEqual(intraday_review(c,live(sector_ts="2026-10-14T09:44:00+08:00"),NOW)["status"],"NO_DATA")

    def test_no_fabricated_continuation(self):
        c = discover_eod(base())
        self.assertEqual(intraday_review(c,live(positive_observations=1),NOW)["status"],"WATCH")
        self.assertEqual(intraday_review(c,live(aflow=-30),NOW)["status"],"WATCH")

    def test_no_chase_and_risk_gate(self):
        c=discover_eod(base())
        self.assertEqual(intraday_review(c,live(change_pct=9.5),NOW)["status"],"NO_CHASE")
        self.assertEqual(intraday_review(c,live(market_regime="RISK_OFF"),NOW)["status"],"RISK_BLOCK")
        self.assertEqual(intraday_review(c,live(price=87),NOW)["status"],"INVALIDATED")

    def test_insufficient_reward_to_risk(self):
        c = discover_eod(base())
        self.assertEqual(intraday_review(c,live(resistance=96),NOW)["status"],"NO_CHASE")

    def test_expired_candidate_and_missing_session_age(self):
        c=discover_eod(base())
        self.assertEqual(intraday_review(c,live(sessions_since_candidate=3),NOW)["status"],"EXPIRED")
        self.assertEqual(intraday_review(c,live(sessions_since_candidate=None),NOW)["status"],"NO_DATA")

    def test_future_timestamp_rejected(self):
        c=discover_eod(base())
        self.assertEqual(intraday_review(c,live(quote_ts="2026-10-14T10:20:00+08:00"),NOW)["status"],"NO_DATA")

if __name__ == "__main__":
    unittest.main()
