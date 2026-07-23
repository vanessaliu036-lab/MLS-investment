# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.ai_explain import local_explain
from app.intraday_filter import REGIME_ATTACK, StockSnap
from app.test_page_wiring import build_rows, make_prefetch_of


def test_local_explain_always_returns_text():
    snap = StockSnap("2330", "attack", 100, 2.0, 120, 600, 90)
    text = local_explain(snap, REGIME_ATTACK)
    assert text
    assert isinstance(text, str)


def test_new_version_wiring_includes_ai_and_filter_state():
    prefetch = make_prefetch_of({"2330": 90}, {}, {}, {})
    rows = build_rows(
        ["2330"],
        lambda code: {"price": 100, "change_rate": 2.0,
                      "aflow": 120, "total_volume": 600, "name": "測試"},
        prefetch,
        REGIME_ATTACK,
    )
    assert rows[0]["ai"]
    assert rows[0]["filter_no_data"] == []
    assert rows[0]["filter_display"]
