# -*- coding: utf-8 -*-
"""回歸測試：主站 scoring 的 bid/ask 方向不可反轉。"""

import os
import sys

# 舊路徑 "../.." 從 tests/ 只會爬到 mls-intraday 的上一層(Desktop/)，
# 從沒指到任何有 scoring.py 的目錄，這支測試自匯入以來就沒真的跑過
# (2026-08-21 才發現)。scoring.py 的正本在「個股卡片相關檔案_20260722」
# (8000 站活的後端，見 memory not-a-shell-server-py-is-live)。
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "個股卡片相關檔案_20260722"))

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
