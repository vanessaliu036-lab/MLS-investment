import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SCREEN_DIR = BASE / "screen-src" if (BASE / "screen-src").exists() else BASE
sys.path.insert(0, str(SCREEN_DIR))

import decision_view as dv
import layered_score as ls


def classified(tier=ls.TIER_CANDIDATE, **overrides):
    row = {"classification": tier, "potential_grade": "B", "trend_stage": "未啟動",
           "reasons": [], "risks": [], "is_limit_up": False}
    row.update(overrides)
    return row


def market(**overrides):
    row = {"close": 99.0, "current_price": 99.0, "open": 98.0, "high": 100.0,
           "low": 97.0, "prior_high": 100.0, "ma5": 98.0, "ma20": 98.5,
           "previous_close": 97.5, "change_rate": 1.5, "aflow_today": 500.0,
           "aflow_previous": -1000.0, "sector_rel": 1.2, "volume": 140000,
           "vol_ma20": 100000, "margin_change": 180, "margin_balance": 10180}
    row.update(overrides)
    return row


class DecisionViewTests(unittest.TestCase):
    def test_each_classification_maps_to_exactly_one_pool(self):
        expected = {ls.TIER_CORE: "core", ls.TIER_REVERSAL: "reversal",
                    ls.TIER_NO_CHASE: "pullback", ls.TIER_CANDIDATE: "watch",
                    ls.TIER_REJECTED: "rejected"}
        for tier, pool in expected.items():
            self.assertEqual(dv.build(classified(tier), market(), {})["display_pool"], pool)

    def test_signal_type_is_never_blank_even_when_rejected(self):
        expected = {ls.TIER_CORE: "強勢突破", ls.TIER_REVERSAL: "反轉訊號",
                    ls.TIER_NO_CHASE: "強勢不追", ls.TIER_CANDIDATE: "觀察訊號",
                    ls.TIER_REJECTED: "結構轉弱"}
        for tier, label in expected.items():
            result = dv.build(classified(tier), market(), {})
            self.assertEqual(result["signal_type"], label)
            self.assertTrue(result["signal_type"])

    def test_confirmed_reversal_upgrades_to_a_but_can_remain_no_chase(self):
        result = dv.build(classified(ls.TIER_REVERSAL),
                          market(close=102, current_price=102, prior_high=101, ma20=99,
                                 aflow_today=3000, is_limit_up=True),
                          {"trigger_price": 101, "track": "attack"})
        self.assertEqual(result["classification"], ls.TIER_CORE)
        self.assertEqual(result["display_pool"], "core")
        self.assertEqual(result["potential_grade"], "A")
        self.assertEqual(result["entry_state"], "no_chase")

    def test_confirmed_strong_no_chase_is_a_stock_with_separate_entry_block(self):
        result = dv.build(classified(ls.TIER_NO_CHASE),
                          market(close=102, current_price=102, prior_high=101, ma20=99,
                                 aflow_today=3000, is_limit_up=True), {})
        self.assertEqual(result["classification"], ls.TIER_CORE)
        self.assertEqual(result["display_pool"], "core")
        self.assertEqual(result["potential_grade"], "A")
        self.assertEqual(result["entry_state"], "no_chase")

    def test_margin_is_a_chip_tag_and_never_an_entry_reason(self):
        result = dv.build(classified(reasons=["融資增", "融資減(籌碼乾淨)", "價漲量增收盤穩"]),
                          market(), {"trigger_price": 100})
        self.assertTrue(result["margin_label"].startswith("融資：+1.8%"))
        self.assertIn("槓桿增加", result["margin_label"])
        self.assertFalse(any("融資" in reason for reason in result["reason_tags"]))
        self.assertNotIn("融資", result["next_upgrade_condition"])

    def test_entry_state_machine_has_six_plain_language_states(self):
        cases = [
            ({"trigger_price": 100}, market(current_price=97, high=98), "not_triggered", "尚未觸發"),
            ({"trigger_price": 100}, market(current_price=99.5, high=99.5), "near_trigger", "距離觸發 0.50%"),
            ({"trigger_price": 100}, market(current_price=101, high=101), "triggered", "已觸發"),
            ({"trigger_price": 100}, market(current_price=99, high=101), "trigger_failed", "觸發後失敗"),
            ({"trigger_price": 100}, market(current_price=101, is_limit_up=True), "no_chase", "禁止追價"),
            ({"trigger_price": 100, "track": "engine"}, market(current_price=101), "wait_pullback", "等待回測")]
        for trigger, values, state, label in cases:
            result = dv.build(classified(), values, trigger)
            self.assertEqual(result["entry_state"], state)
            self.assertIn(label, result["entry_state_label"])

    def test_next_upgrade_condition_matches_missing_confirmation(self):
        reversal = dv.build(classified(ls.TIER_REVERSAL),
                            market(close=99, current_price=99, ma20=98, prior_high=100,
                                   aflow_today=500), {"trigger_price": 100})
        watch = dv.build(classified(), market(close=97, current_price=97, ma20=98,
                                               aflow_today=-10), {})
        self.assertEqual(reversal["next_upgrade_condition"], "突破昨高 100 → A級")
        self.assertEqual(watch["next_upgrade_condition"], "站回 MA20 98 → 反轉")

    def test_reason_tags_are_short_market_reasons(self):
        result = dv.build(classified(), market(close=101, current_price=101), {})
        self.assertEqual(result["reason_tags"], ["突破前高", "主動資金翻正", "族群轉強"])

    def test_three_ranking_factors_measure_change_not_streak(self):
        strong = dv.ranking_factors(market(change_rate=4, aflow_today=1000, aflow_previous=-1000))
        weak = dv.ranking_factors(market(change_rate=.2, aflow_today=1000, aflow_previous=900))
        self.assertGreater(strong["buying_efficiency"], weak["buying_efficiency"])
        self.assertGreater(strong["chip_reversal_acceleration"], weak["chip_reversal_acceleration"])
        absorbed = dv.ranking_factors(market(change_rate=-.2, aflow_today=-2000,
                                             aflow_previous=-1000))
        self.assertGreaterEqual(absorbed["pressure_absorption"], 60)
        self.assertNotIn("inst_streak", strong)

    def test_breakout_trigger_keeps_the_higher_structural_resistance(self):
        result = dv.build(classified(), market(
            close=196, current_price=196, high=200.5, prior_high=205,
            ma20=155.4, aflow_today=-1236, change_rate=-.25), {
                "track": "attack", "trigger_price": 200.5})
        self.assertEqual(result["trigger_price"], 205)
        self.assertEqual(result["trigger_source"], "structure_resistance")
        self.assertEqual(result["entry_state"], "not_triggered")

    def test_weak_flow_watch_is_low_priority_without_stale_bullish_copy(self):
        result = dv.build(classified(reasons=[
            "資金健康度佳", "融資增", "收在月線上", "溫和放量"]), market(
                close=196, current_price=196, high=200.5, prior_high=205,
                ma20=155.4, aflow_today=-1236, change_rate=-.25), {
                    "track": "attack", "trigger_price": 200.5})
        self.assertEqual(result["classification"], ls.TIER_CANDIDATE)
        self.assertEqual(result["decision_priority"], "low")
        self.assertEqual(result["priority_label"], "👀 低優先觀察")
        self.assertEqual(result["next_upgrade_condition"],
                         "站上 205 + 主動資金翻正 → A級")
        self.assertEqual(result["reason_tags"],
                         ["未突破 205", "主動資金流出", "結構尚未失效"])
        self.assertEqual(result["decision_summary"],
                         "今日未突破 205，主動資金 -1,236 張，短線動能降溫；"
                         "結構尚未失效，因此保留觀察。")
        self.assertFalse(any(term in "".join(result["reason_tags"])
                             for term in ("資金健康", "融資", "溫和放量")))

    def test_metric_contract_separates_volume_aflow_and_prior_day_institution(self):
        result = dv.build(classified(), market(
            volume=18726,
            intraday_volume_ratio=.96,
            aflow_today=2845,
            active_buy=7200,
            active_sell=4355,
            institution_label="法人連買 3 日",
            chip_data_date="2026-09-02",
        ), {})
        self.assertEqual(result["volume_lots"], 18726)
        self.assertEqual(result["aflow_lots"], 2845)
        self.assertEqual(result["volume_ratio"], .96)
        self.assertEqual(result["flow_label"], "A-flow +2,845 張")
        self.assertEqual(result["volume_label"], "成交量 18,726 張 · 累計/20日均量 0.96x")
        self.assertEqual(result["institution_metric_label"], "法人籌碼（截至 2026-09-02）")
        self.assertFalse(result["metric_contract"]["aflow_is_institutional"])
        self.assertFalse(result["metric_contract"]["institution_is_intraday"])

    def test_aflow_conflict_blocks_directional_decision_value(self):
        result = dv.build(classified(), market(
            aflow_today=1500,
            active_buy=5200,
            active_sell=5000,
        ), {})
        self.assertIsNone(result["aflow_lots"])
        self.assertEqual(result["metric_contract"]["aflow_status"], "CONFLICT")
        self.assertEqual(result["flow_label"], "A-flow —")


if __name__ == "__main__":
    unittest.main()
