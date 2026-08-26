"""Early Activation Research: candidate definitions and discovery KPIs."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import early_activation_score as eas


def day(foreign_days, ma5_distance_pct=0.0, **extra):
    row = {"foreign_days": foreign_days,
           "ma5_distance_pct": ma5_distance_pct}
    row.update(extra)
    return row


def t0(**extra):
    row = {
        "code": "9999",
        "foreign_days": 2,
        "ma5_distance_pct": 1.0,
        "volume_ratio": 0.7,
        "sector_regime": "NEUTRAL",
        "sector_ret_median": 0.6,
        "sector_breadth": 50.0,
    }
    row.update(extra)
    return row


def test_sector_context_keeps_strict_risk_on_separate_from_turning_positive():
    assert eas.sector_context(t0(sector_regime="RISK_ON")) == eas.RISK_ON
    assert eas.sector_context(t0()) == eas.TURNING_POSITIVE
    assert eas.sector_context(t0(sector_ret_median=0.0)) == eas.NEUTRAL


def test_new_turn_covers_fresh_two_or_three_day_runs():
    strong_mao = eas.classify(t0(code="2481"), [
        day(1), day(-4), day(-3), day(-2), day(-1),
    ])
    apower = eas.classify(t0(code="8261", foreign_days=3), [
        day(2), day(1), day(-3), day(-2), day(-1),
    ])
    assert strong_mao["setup_type"] == eas.NEW_TURN
    assert apower["setup_type"] == eas.NEW_TURN


def test_reconfirm_requires_prior_positive_then_interruption_then_new_run():
    result = eas.classify(t0(code="6451", sector_regime="RISK_ON"), [
        day(1), day(-1), day(1), day(-2), day(-1),
    ])
    assert result["setup_type"] == eas.RECONFIRM
    assert result["sector_context"] == eas.RISK_ON


def test_accumulation_retest_requires_intact_buying_and_prior_hot_ma5():
    result = eas.classify(t0(code="3037", foreign_days=12,
                             ma5_distance_pct=-0.9, volume_ratio=0.64), [
        day(11, 1.5), day(10, 3.2), day(9, 5.1),
        day(8, 6.4), day(7, 8.33),
    ])
    assert result["setup_type"] == eas.ACCUMULATION_RETEST


def test_setup_precedence_is_accumulation_then_reconfirm_then_new_turn():
    result = eas.classify(t0(foreign_days=5), [
        day(4, 1.0), day(3, 2.0), day(2, 7.2), day(1, 4.0), day(-1, 0.0),
    ])
    assert result["setup_type"] == eas.ACCUMULATION_RETEST


def test_common_eligibility_blocks_active_volume_far_ma5_risk_off_and_missing():
    cases = [
        (t0(volume_ratio=1.2), "VOLUME_ALREADY_ACTIVE"),
        (t0(ma5_distance_pct=2.01), "PRICE_NOT_NEAR_MA5"),
        (t0(sector_regime="RISK_OFF"), "SECTOR_RISK_OFF"),
        (t0(volume_ratio=None), "MISSING_REQUIRED_FACTS"),
    ]
    history = [day(1), day(-1), day(-2), day(-3), day(-4)]
    for facts, reason in cases:
        result = eas.classify(facts, history)
        assert result["setup_type"] is None
        assert reason in result["reasons"]


def test_classifier_never_emits_score_probability_or_production_evidence():
    result = eas.classify(t0(), [day(1), day(-1), day(-2), day(-3), day(-4)])
    assert result["evidence_status"] == eas.DISCOVERY_ONLY
    banned = [k for k in result if any(x in k.lower()
              for x in ("score", "probability", "confidence"))]
    assert not banned


def test_discovery_metrics_and_same_date_context_baseline():
    rows = [
        {"data_date": "2026-08-25", "code": "2481", "setup_type": eas.NEW_TURN,
         "sector_context": eas.TURNING_POSITIVE, "t1_return_pct": 8.02},
        {"data_date": "2026-08-25", "code": "8261", "setup_type": eas.NEW_TURN,
         "sector_context": eas.TURNING_POSITIVE, "t1_return_pct": 4.50},
        {"data_date": "2026-08-25", "code": "1111", "setup_type": None,
         "sector_context": eas.TURNING_POSITIVE, "t1_return_pct": -1.0},
        {"data_date": "2026-08-25", "code": "2222", "setup_type": None,
         "sector_context": eas.TURNING_POSITIVE, "t1_return_pct": 1.0},
        # Same context but another date must not leak into the 8/25 matched baseline.
        {"data_date": "2026-08-24", "code": "3333", "setup_type": None,
         "sector_context": eas.TURNING_POSITIVE, "t1_return_pct": 20.0},
    ]
    report = eas.evaluate(rows)
    cell = next(x for x in report["by_setup_context"]
                if x["setup_type"] == eas.NEW_TURN)
    assert cell["metrics"]["n"] == 2
    assert cell["metrics"]["hit_plus_3_rate"] == 100.0
    assert cell["metrics"]["mean_return_pct"] == 6.26
    assert cell["matched_baseline"]["n"] == 2
    assert cell["matched_baseline"]["mean_return_pct"] == 0.0
    assert report["evidence_status"] == eas.DISCOVERY_ONLY

