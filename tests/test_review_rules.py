# -*- coding: utf-8 -*-
"""盤後驗證（自動版）：記錄→更新收盤→判定→累積統計。"""

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import review_rules  # noqa: E402

TW = ZoneInfo("Asia/Taipei")


def _t(h, m):
    return datetime(2026, 7, 23, h, m, tzinfo=TW)


def setup_function(_):
    review_rules.DB_PATH = Path("/tmp") / "test_review_rules.db"
    if review_rules.DB_PATH.exists():
        review_rules.DB_PATH.unlink()


def test_judge_rules():
    assert review_rules.judge("可操作", 100, 102) == "命中"      # +2% >= 1.5%
    assert review_rules.judge("可操作", 100, 101) == "未命中"    # +1% < 1.5%
    assert review_rules.judge("觀察", 100, 100.5) == "命中"      # 突破訊號價
    assert review_rules.judge("觀察", 100, 100) == "未命中"
    assert review_rules.judge("排除", 100, 98) == "命中"         # -2% <= -1.5%
    assert review_rules.judge("排除", 100, 99.5) == "未命中"
    assert review_rules.judge("不存在", 100, 102) == "無規則"
    assert review_rules.judge("可操作", None, 102) == "資料不足"


def test_record_validate_flow():
    row = {"code": "2330", "name": "台積電", "group": "可操作",
           "subgroup": "七因子達標", "aflow": 5000, "price": 100.0}
    # 10:00 首次進入分類 → 記訊號價 100
    review_rules.record([row], now=_t(10, 0))
    # 盤中價格變動只更新 last_price，不動訊號價
    review_rules.record([dict(row, price=101.0)], now=_t(11, 0))
    # 13:30 收盤定盤價 102（+2%）
    review_rules.record([dict(row, price=102.0)], now=_t(13, 30))

    # 13:00 就想驗證今日 → 不判定
    assert review_rules.validate(now=_t(13, 0)) == 0
    payload = review_rules.api_payload(now=_t(13, 0))
    assert payload["today"][0]["result"] == "待驗證"

    # 13:35 後自動判定：+2% >= 1.5% → 命中
    payload = review_rules.api_payload(now=_t(13, 40))
    assert payload["today"][0]["result"] == "命中"
    assert payload["today"][0]["signal_price"] == 100.0
    assert payload["today"][0]["last_price"] == 102.0
    assert payload["summary"] == [
        {"category": "可操作", "total": 1, "hits": 1, "hit_rate": 100.0,
         "rule": review_rules.RULES["可操作"]["desc"]}]


def test_no_new_signal_after_close():
    # 收盤後殘留 buffer 不得開新訊號
    row = {"code": "2603", "name": "長榮", "group": "觀察",
           "subgroup": "條件待確認", "aflow": 100, "price": 200.0}
    review_rules.record([row], now=_t(14, 0))
    payload = review_rules.api_payload(now=_t(14, 0))
    assert payload["today"] == []
