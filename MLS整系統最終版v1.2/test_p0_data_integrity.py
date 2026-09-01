import sys
import types

try:
    import shioaji  # noqa: F401
except ModuleNotFoundError:
    sys.modules["shioaji"] = types.SimpleNamespace()

import scoring
import stock_card


def test_missing_aflow_is_none_not_fake_zero():
    scoring.reset_aflow()
    assert scoring.get_aflow("2330") is None
    got = scoring.update_aflow(
        "2330", 1000, buy_volume=None, sell_volume=None, source=None
    )
    assert got is None
    assert scoring.get_aflow("2330") is None


def test_quote_queue_values_cannot_be_promoted_to_aflow():
    scoring.reset_aflow()
    got = scoring.update_aflow(
        "2330", 1000, buy_volume=524, sell_volume=103,
        source="shioaji_snapshot_quote"
    )
    assert got is None
    assert scoring.get_aflow("2330") is None


def test_verified_tick_flow_can_be_used_as_canonical_aflow():
    scoring.reset_aflow()
    got = scoring.update_aflow(
        "2330", 1000, buy_volume=170, sell_volume=50,
        source="shioaji_ticks"
    )
    assert got == 120
    assert scoring.get_aflow("2330") == 120


def test_stock_card_ignores_snapshot_quote_queue_for_active_flow():
    card = stock_card.build_card(
        "2330",
        snap={
            "price": 100, "high": 101, "change_rate": 1.0,
            "buy_volume": 524, "sell_volume": 103,
        },
        injected_bars=[],
        chip_detail={},
    )
    assert card["flow"]["active_buy_pct"] is None
    assert card["flow"]["active_sell_pct"] is None


def test_stock_card_uses_only_explicit_verified_active_trade_fields():
    card = stock_card.build_card(
        "2330",
        snap={
            "price": 100, "high": 101, "change_rate": 1.0,
            "active_buy_volume": 60, "active_sell_volume": 40,
            "active_flow_source": "shioaji_ticks",
        },
        injected_bars=[],
        chip_detail={},
    )
    assert card["flow"]["active_buy_pct"] == 60.0
    assert card["flow"]["active_sell_pct"] == 40.0
    assert card["flow"]["source"] == "shioaji_ticks"
