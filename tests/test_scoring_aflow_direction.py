# -*- coding: utf-8 -*-
"""回歸測試：主站 scoring 的 bid/ask 方向不可反轉。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import scoring


def test_scoring_uses_ask_minus_bid_for_active_flow():
    scoring.reset_aflow()
    scoring.update_aflow("TEST", 800, buy_volume=500, sell_volume=300)
    scoring.update_aflow("TEST", 900, buy_volume=600, sell_volume=350)
    # 增量：ask/主動買 +100、bid/主動賣 +50，主動淨流 = +50。
    assert scoring.get_aflow("TEST") == 50


def test_scoring_tick_type_one_is_active_buy():
    scoring.reset_aflow()
    scoring.update_aflow("TEST", 100, tick_type=1)
    scoring.update_aflow("TEST", 200, tick_type=1)
    assert scoring.get_aflow("TEST") == 100
