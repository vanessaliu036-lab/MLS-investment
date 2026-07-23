# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.classify import (  # noqa: E402
    GROUP_ACTIONABLE, GROUP_EXCLUDE, GROUP_WATCH,
    SUB_EXTREME, SUB_STRONG_ABSORB, SUB_TRUE_ATTACK,
    classify_all, classify_one,
)
from app.intraday_filter import REGIME_ATTACK, REGIME_DEFENSE, StockSnap  # noqa: E402


def snap(**updates):
    data = dict(code="0000", track="attack", price=105, change_rate=2,
                aflow=120, total_volume=600, ma20=100)
    data.update(updates)
    return StockSnap(**data)


def test_true_attack_is_actionable():
    result = classify_one(snap(), regime=REGIME_ATTACK)
    assert result["group"] == GROUP_ACTIONABLE
    assert result["subgroup"] == SUB_TRUE_ATTACK


def test_strong_absorb_does_not_require_ma20():
    result = classify_one(snap(change_rate=-3.96, aflow=1077,
                               total_volume=7203, ma20=None),
                          regime=REGIME_DEFENSE)
    assert result["group"] == GROUP_ACTIONABLE
    assert result["subgroup"] == SUB_STRONG_ABSORB


def test_missing_ma20_true_attack_is_watch():
    result = classify_one(snap(ma20=None), regime=REGIME_ATTACK)
    assert result["group"] == GROUP_WATCH


def test_extreme_price_is_excluded():
    result = classify_one(snap(change_rate=-9.72), regime=REGIME_DEFENSE)
    assert result["group"] == GROUP_EXCLUDE
    assert result["subgroup"] == SUB_EXTREME


def test_classify_all_has_counts():
    result = classify_all([snap(code="a"), snap(code="b", change_rate=-10)],
                          regime=REGIME_ATTACK)
    assert result["counts"][GROUP_ACTIONABLE] == 1
    assert result["counts"][GROUP_EXCLUDE] == 1
