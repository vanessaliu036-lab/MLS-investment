# -*- coding: utf-8 -*-
"""v4 三態 filter、極端價防護與盤前 MA20 快取測試。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.intraday_filter import (  # noqa: E402
    FAIL,
    NO_DATA,
    PASS,
    REGIME_ATTACK,
    StockSnap,
    aflow_from_sides,
    aflow_official,
    is_extreme_price,
    passes_filters,
    rank_potential,
    st_above_ma20,
    st_aflow_intensity,
    st_aflow_positive,
    st_regime_quadrant,
)
from app.prefetch_ma20 import (  # noqa: E402
    build_ma20_cache_from_closes,
    compute_ma20,
)


def snap(**kwargs):
    data = dict(code="0000", track="attack", price=105.0,
                change_rate=2.0, aflow=120, total_volume=600, ma20=100.0)
    data.update(kwargs)
    return StockSnap(**data)


def test_v4_keeps_corrected_aflow_direction():
    assert aflow_from_sides(500, 300) == 200
    assert aflow_from_sides(100, 400) == -300
    assert aflow_official(300, 500) == 200
    assert aflow_official(400, 100) == -300


def test_extreme_price_degrades_price_sensitive_checks_to_no_data():
    s = snap(change_rate=-9.72)
    assert is_extreme_price(-9.0) is True
    assert is_extreme_price(-8.99) is False
    assert st_aflow_positive(s) == NO_DATA
    assert st_aflow_intensity(s) == NO_DATA
    assert st_regime_quadrant(s, REGIME_ATTACK) == NO_DATA
    result = passes_filters(s, regime=REGIME_ATTACK)
    assert result["extreme"] is True
    assert result["all_pass"] is False
    assert len(result["no_data"]) >= 3
    assert "—主動差>0" in result["display"]


def test_missing_ma20_is_no_data_not_failed():
    result = passes_filters(snap(ma20=None))
    assert st_above_ma20(snap(ma20=None)) == NO_DATA
    assert "站上MA20" in result["no_data"]
    assert "站上MA20" not in result["failed"]
    assert result["all_pass"] is False


def test_loaded_ma20_can_pass_or_fail():
    assert st_above_ma20(snap(price=105, ma20=100)) == PASS
    assert st_above_ma20(snap(price=95, ma20=100)) == FAIL


def test_normal_attack_snapshot_can_all_pass():
    result = passes_filters(snap(), regime=REGIME_ATTACK)
    assert result["all_pass"] is True
    assert result["no_data"] == []
    assert all(value == PASS for value in result["states"].values())


def test_ma20_prefetch_is_premarket_pure_calculation():
    assert compute_ma20(list(range(1, 20))) is None
    assert compute_ma20(list(range(1, 21))) == 10.5
    cache = build_ma20_cache_from_closes({"2330": list(range(1, 21)),
                                          "bad": [100]})
    assert cache == {"2330": 10.5, "bad": None}


def test_rank_puts_extreme_price_at_bottom():
    normal = snap(code="normal", change_rate=3.0)
    extreme = snap(code="extreme", change_rate=9.2, aflow=999,
                   total_volume=1000)
    ranked = rank_potential([extreme, normal])
    assert [row["code"] for row in ranked] == ["normal", "extreme"]


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")
