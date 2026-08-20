"""#6 攻擊軌分母修正:候選數 → 觸發率 → 觸發後勝率 → 觸發後淨報酬/Alpha。

未觸發不再算交易失敗,只算「沒有形成交易」。混合命中率把沒下的單算進勝負,
會把一個觸發後表現正常的軌道打成 31.9%。
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from screen_verify import _track_breakdown, ROUND_TRIP_COST_PCT


def rec(code, track, triggered, hit, ret):
    return {"code": code, "track": track, "triggered": triggered,
            "hit": hit, "ret_pct": ret}


def test_untriggered_is_no_trade_not_a_loss():
    items = [
        rec("A", "攻擊軌", 1, 1, 7.0),
        rec("B", "攻擊軌", 1, 0, 0.1),
        rec("C", "攻擊軌", 0, 0, -0.5),      # 未觸發
        rec("D", "攻擊軌", 0, 0, -1.2),      # 未觸發
    ]
    b = _track_breakdown(items, set(), market_t1=1.0)["攻擊軌"]
    assert b["candidates"] == 4
    assert b["triggered"] == 2 and b["no_trade"] == 2
    assert b["trigger_rate"] == 50.0
    # 舊混合口徑會是 1/4 = 25%;觸發後勝率必須是 1/2 = 50%
    assert b["win_rate_after_trigger"] == 50.0
    assert b["scored_after_trigger"] == 2


def test_net_return_and_alpha_only_count_triggered_rows():
    items = [
        rec("A", "攻擊軌", 1, 1, 6.0),
        rec("B", "攻擊軌", 1, 0, 2.0),
        rec("C", "攻擊軌", 0, 0, -9.0),      # 未觸發,不得污染報酬
    ]
    b = _track_breakdown(items, set(), market_t1=1.5)["攻擊軌"]
    assert b["avg_ret_after_trigger"] == 4.0
    assert b["avg_net_ret_after_trigger"] == round(4.0 - ROUND_TRIP_COST_PCT, 2)
    assert b["avg_alpha_vs_taiex_after_trigger"] == 2.5      # (6-1.5, 2-1.5) 平均
    assert ROUND_TRIP_COST_PCT == 0.471                      # 隔日持有,非當沖


def test_regime_excluded_rows_leave_the_denominator_entirely():
    items = [
        rec("A", "攻擊軌", 1, 1, 5.0),
        rec("X", "攻擊軌", 1, None, -3.0),   # 當天 regime 閘擋下,沒進場
    ]
    b = _track_breakdown(items, {"X"}, market_t1=0.0)["攻擊軌"]
    assert b["candidates"] == 2
    assert b["excluded_by_regime"] == 1
    assert b["evaluable"] == 1 and b["triggered"] == 1
    assert b["win_rate_after_trigger"] == 100.0
    assert b["avg_ret_after_trigger"] == 5.0                 # 沒被 -3.0 拉低


def test_no_data_rows_are_reported_not_silently_dropped():
    items = [
        rec("A", "引擎軌", 1, 1, 2.0),
        rec("B", "引擎軌", None, None, None),                # 資料不足
    ]
    b = _track_breakdown(items, set(), market_t1=None)["引擎軌"]
    assert b["candidates"] == 2 and b["no_data"] == 1 and b["evaluable"] == 1
    assert b["avg_alpha_vs_taiex_after_trigger"] is None     # 沒有大盤就不硬編


def test_empty_track_is_absent_not_zero_division():
    b = _track_breakdown([rec("A", "觀察", None, None, 1.0)], set(), market_t1=0.0)
    assert b == {}
