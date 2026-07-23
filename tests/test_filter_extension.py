# -*- coding: utf-8 -*-
"""回歸測試：盤中強度 filter 與防反相顯示結構。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.intraday_filter import (
    AFLOW_INTENSITY_MIN,
    StockSnap,
    aflow_intensity,
    cond_aflow_intensity,
    passes_filters,
    rank_potential,
    REGIME_ATTACK,
    REGIME_DEFENSE,
    REGIME_RANGE,
    cond_regime_quadrant,
    cond_strong_absorb,
    market_regime,
)


def make_snap(**kwargs):
    data = dict(code="0000", track="attack", price=100.0,
                change_rate=1.0, aflow=0, total_volume=0, ma20=90.0)
    data.update(kwargs)
    return StockSnap(**data)


def test_aflow_intensity_is_percentage():
    assert AFLOW_INTENSITY_MIN == 10.0
    assert aflow_intensity(136, 368) == 37.0
    assert aflow_intensity(97, 2237) == 4.3
    assert aflow_intensity(100, 0) is None


def test_intensity_filter_uses_ten_percent_threshold():
    assert cond_aflow_intensity(make_snap(aflow=136, total_volume=368)) is True
    assert cond_aflow_intensity(make_snap(aflow=97, total_volume=2237)) is False


def test_filter_display_separates_passed_and_failed():
    result = passes_filters(make_snap(aflow=-70, total_volume=8472))
    assert "主動差>0" in result["failed"]
    assert "主動差>0" not in result["passed"]
    assert "✗主動差>0" in result["display"]


def test_rank_potential_prefers_intensity_then_resilience():
    weak = make_snap(code="3317", aflow=234, total_volume=1750, change_rate=-7.82)
    strong = make_snap(code="6174", aflow=136, total_volume=368, change_rate=-8.25)
    ranked = rank_potential([weak, strong])
    assert ranked[0]["code"] == "6174"


def test_market_regime_thresholds():
    assert market_regime(68) == REGIME_ATTACK
    assert market_regime(30) == REGIME_DEFENSE
    assert market_regime(50) == REGIME_RANGE


def test_defense_regime_requires_strong_absorption():
    strong = make_snap(code="8028", change_rate=-3.96, aflow=1077,
                       total_volume=7203, ma20=300.0, price=302.5)
    weak = make_snap(code="6223", change_rate=-3.48, aflow=2,
                     total_volume=694, ma20=5300.0, price=5405.0)
    assert cond_strong_absorb(strong) is True
    assert cond_strong_absorb(weak) is False
    assert cond_regime_quadrant(strong, REGIME_DEFENSE) is True
    assert cond_regime_quadrant(strong, REGIME_ATTACK) is False
    assert cond_regime_quadrant(strong, REGIME_RANGE) is True
