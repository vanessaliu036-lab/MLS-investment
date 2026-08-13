import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SCREEN_DIR = BASE / "screen-src" if (BASE / "screen-src").exists() else BASE
sys.path.insert(0, str(SCREEN_DIR))

import funnel
import layered_score as ls
import screen_post
import datetime as dt


class FunnelRetentionTests(unittest.TestCase):
    def test_post_fallback_keeps_date_alignment_when_today_bar_is_missing(self):
        rows = [
            {"data_date": "2026-08-12", "close": 933, "high": 933, "ma20": 1003.05},
            {"data_date": "2026-08-11", "close": 849, "high": 883, "ma20": 1008},
        ]
        previous = screen_post._strict_previous_bar(rows, dt.date(2026, 8, 13))
        current = screen_post._bar_with_live_quote(None, {
            "price": 1025, "change_rate": 9.86}, previous)
        self.assertEqual(previous["high"], 933)
        self.assertEqual(current["close"], 1025)
        self.assertEqual(current["ma20"], 1003.05)

    def test_final_decision_uses_central_classifier_only(self):
        full = {"failure_gate_count": 4, "classification": ls.TIER_REJECTED}
        partial = {"failure_gate_count": 3, "classification": ls.TIER_CANDIDATE}
        self.assertFalse(funnel.central_keep(full))
        self.assertTrue(funnel.central_keep(partial))

    def test_screen_post_keeps_every_non_rejected_stock_without_top_twenty_cut(self):
        rows = [{"code": str(i)} for i in range(25)]
        self.assertEqual(len(screen_post.select_pool(rows)), 25)

    def test_both_pipelines_use_the_shared_recovery_scanner(self):
        screen_source = Path(screen_post.__file__).read_text(encoding="utf-8")
        funnel_source = Path(funnel.__file__).read_text(encoding="utf-8")
        self.assertIn("recovery_scan.scan", screen_source)
        self.assertIn("recovery_scan.scan", funnel_source)
        self.assertIn('if rec["in_recovery_pool"]:', funnel_source)

    def test_all_pipelines_use_the_shared_decision_view(self):
        intraday_source = (Path(screen_post.__file__).parent / "screen_intraday.py").read_text(encoding="utf-8")
        for source in (Path(screen_post.__file__).read_text(encoding="utf-8"),
                       Path(funnel.__file__).read_text(encoding="utf-8"), intraday_source):
            self.assertIn("decision_view.build", source)

    def test_api_keeps_canonical_interpretation_fields(self):
        api_source = (Path(screen_post.__file__).parent / "api.py").read_text(encoding="utf-8")
        for field in ("decision_summary", "decision_priority", "priority_label",
                      "trigger_source", "flow_status_label"):
            self.assertIn(f'"{field}"', api_source)

    def test_financing_is_not_emitted_as_screen_post_entry_reason(self):
        positive = screen_post.score_one("TEST", {"close": 101, "ma20": 100,
            "volume": 120, "vol_ma20": 100}, None,
            {"margin_change": 100, "margin_balance": 10100}, None, None)
        negative = screen_post.score_one("TEST", {"close": 101, "ma20": 100,
            "volume": 120, "vol_ma20": 100}, None,
            {"margin_change": -100, "margin_balance": 9900}, None, None)
        self.assertFalse(any("融資" in reason for reason in positive["reasons"] + negative["reasons"]))

    def test_financing_is_not_emitted_as_funnel_chip_reason(self):
        _, reasons, _ = funnel.layer3("TEST", {
            "foreign_net": 100, "trust_net": 50, "dealer_net": 0,
            "foreign_days": 1, "trust_days": 1,
        }, {"margin_change": 100, "margin_balance": 10100})
        self.assertFalse(any("融資" in reason for reason in reasons))

    def test_below_monthly_average_is_feature_not_drop(self):
        series = [{"slot": "1320", "price": 95.0, "change_rate": -2.5,
                   "volume": 80, "net_active": -500}]
        kept, reasons = funnel.layer1("TEST", series,
                                      {"ma20": 100.0, "volume": 100_000}, {}, None)
        self.assertTrue(kept)
        self.assertIn("跌破月線", reasons)

    def test_price_up_with_outflow_is_feature_not_drop(self):
        series = [{"slot": "1320", "price": 105.0, "change_rate": 5.0,
                   "volume": 100, "net_active": -800}]
        kept, reason = funnel.layer25(series)
        self.assertTrue(kept)
        self.assertIn("邊拉邊出", reason)

    def test_foreign_and_trust_selling_and_streaks_are_features_not_drop(self):
        kept, reasons, score = funnel.layer3("TEST", {
            "foreign_net": -5000, "trust_net": -1000, "dealer_net": -100,
            "foreign_days": -5, "trust_days": -4,
        }, {"margin_change": 100})
        self.assertTrue(kept)
        self.assertTrue(any("連賣" in reason or "賣超" in reason for reason in reasons))
        self.assertLessEqual(score, 0)

    def test_missing_institution_data_is_pending_not_drop(self):
        kept, reasons, score = funnel.layer3("TEST", None, None)
        self.assertTrue(kept)
        self.assertEqual(reasons, ["籌碼未到位"])
        self.assertEqual(score, 0.0)

    def test_low_core_score_is_diagnostic_not_drop(self):
        series = [{"slot": f"{hour:02d}{minute:02d}", "price": 100.0,
                   "change_rate": 0.2, "volume": 10 + idx, "net_active": -10}
                  for idx, (hour, minute) in enumerate([
                      (9, 15), (9, 30), (9, 45), (10, 0),
                      (10, 15), (10, 30), (10, 45), (11, 0)])]
        passed, detail, hits = funnel.layer2("TEST", series,
                                             {"volume": 1_000_000}, {}, None)
        self.assertFalse(passed)
        self.assertLess(hits, 2)
        self.assertTrue(funnel.central_keep({"classification": ls.TIER_CANDIDATE}))


if __name__ == "__main__":
    unittest.main()
