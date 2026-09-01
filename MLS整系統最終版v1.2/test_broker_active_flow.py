import sys
import types

try:
    import shioaji  # noqa: F401
except ModuleNotFoundError:
    sys.modules["shioaji"] = types.SimpleNamespace()

import broker


class Ticks:
    volume = [10, 3, 7, 5, 2]
    tick_type = [1, 2, 1, 0, 2]


class FakeContracts:
    Stocks = {"2330": object()}


class FakeApi:
    Contracts = FakeContracts()

    def __init__(self):
        self.calls = []

    def ticks(self, *, contract, date):
        self.calls.append((contract, date))
        return Ticks()


class FakeSnap:
    code = "2330"
    close = 100
    open = 99
    high = 101
    low = 98
    change_rate = 1.0
    volume_ratio = 0.8
    total_volume = 1000
    total_amount = 100000
    average_price = 99.5
    tick_type = None
    buy_volume = 524
    sell_volume = 103


class FakeBatchApi(FakeApi):
    def snapshots(self, contracts):
        return [FakeSnap()]


def test_aggregate_tick_flow_uses_outside_inside_trade_volume():
    got = broker._aggregate_tick_flow(Ticks())
    assert got == {
        "active_buy_volume": 17,
        "active_sell_volume": 5,
        "active_diff": 12,
        "classified_volume": 22,
    }


def test_active_flow_today_reads_today_ticks_and_returns_source(monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(broker, "get_api", lambda: api)
    got = broker.active_flow_today("2330", date="2026-09-01")
    assert got["active_buy_volume"] == 17
    assert got["active_sell_volume"] == 5
    assert got["active_diff"] == 12
    assert got["source"] == "shioaji_ticks"
    assert got["date"] == "2026-09-01"
    assert api.calls == [(api.Contracts.Stocks["2330"], "2026-09-01")]


def test_single_stock_snapshot_never_blocks_on_full_day_ticks(monkeypatch):
    api = FakeBatchApi()
    monkeypatch.setattr(broker, "get_api", lambda: api)
    monkeypatch.setattr(broker.time, "sleep", lambda *_: None)
    got = broker.batch_snapshots(["2330"])[0]

    # Snapshot buy/sell are quote-queue quantities, not active-trade flow.
    # They must never be overloaded as A-flow and a card request must not call
    # the expensive full-day ticks endpoint synchronously.
    assert api.calls == []
    assert got["bid_volume"] == 524
    assert got["ask_volume"] == 103
    assert got["buy_volume"] is None
    assert got["sell_volume"] is None
    assert got["active_buy_volume"] is None
    assert got["active_sell_volume"] is None
    assert got["active_flow_diff"] is None
    assert got["active_flow_source"] is None
