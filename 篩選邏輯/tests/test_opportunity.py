"""Opportunity scoring / snapshot 的行為鎖定。

重點鎖三件曾經或可能出錯的事:
  1. 55% 是主榜資格線,**不是刪除線** —— 高 payoff 低勝率股票不得被丟掉
  2. 進場價必須是 T+1 開盤,不是 T0 收盤
  3. 族群相對強度必須 leave-one-out,不得把自己算進去
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import opportunity_score as osc


def _bars(closes, opens=None, highs=None, lows=None):
    n = len(closes)
    opens = opens or closes
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    return [{"date": f"2026-01-{i+1:02d}", "open": opens[i], "high": highs[i],
             "low": lows[i], "close": closes[i]} for i in range(n)]


def test_low_win_rate_high_payoff_is_kept_not_deleted():
    """章程第 9 條:勝率低於 55% 但 payoff 結構強,必須留在 HIGH_POTENTIAL。
    這是使用者明確要求 —— 禁止因單一勝率門檻丟掉高 payoff 股票。"""
    stats = {"insufficient": False, "n": 200,
             "net_positive_rate": 46.0,          # 低於 55%
             "profit_factor": 2.2,               # 但 PF 很高
             "expected_upside": 7.5, "p_hit_3pct": 61.0,
             "net_expectancy": 2.5, "expected_downside": -4.0,
             "avg_win": 9.0, "avg_loss": -3.5}
    tier, reasons = osc.assign_tier(stats, in_top_sector=False)
    assert tier == "HIGH_POTENTIAL", (tier, reasons)
    assert any("PF" in r or "payoff" in r for r in reasons)


def test_primary_requires_both_win_rate_and_sector():
    stats = {"insufficient": False, "n": 200, "net_positive_rate": 58.0,
             "profit_factor": 1.4, "expected_upside": 4.0, "p_hit_3pct": 60.0,
             "net_expectancy": 1.0, "expected_downside": -5.0,
             "avg_win": 6.0, "avg_loss": -5.0}
    assert osc.assign_tier(stats, in_top_sector=True)[0] == "PRIMARY"
    # 勝率達標但族群沒進 Top10% → 不進主榜,但也不能掉出去
    assert osc.assign_tier(stats, in_top_sector=False)[0] == "HIGH_POTENTIAL"


def test_avoid_only_for_clearly_bad():
    """AVOID 要留給真正不利的,不能變成第二條刪除線。"""
    weak = {"insufficient": False, "n": 200, "net_positive_rate": 44.0,
            "profit_factor": 1.0, "expected_upside": 3.0, "p_hit_3pct": 50.0,
            "net_expectancy": 0.1, "expected_downside": -5.0,
            "avg_win": 5.0, "avg_loss": -5.0}
    assert osc.assign_tier(weak, False)[0] == "WATCH"
    bad = dict(weak, net_expectancy=-1.5, expected_downside=-9.0)
    assert osc.assign_tier(bad, False)[0] == "AVOID"


def test_entry_is_next_day_open_not_today_close():
    """盤後名單在 T0 收盤買不到。用收盤會把隔夜跳空算成自己的績效。"""
    closes = [100.0] * 30
    opens = [100.0] * 30
    # 第 1 天收 100,第 2 天開 110(跳空);之後高點都在 110 附近
    opens[1] = 110.0
    highs = [101.0] * 30
    for i in range(1, 30):
        highs[i] = 111.0
    lows = [99.0] * 30
    b = _bars(closes, opens, highs, lows)
    s = osc.realized_opportunity_stats(b, horizon=10, window=250)
    # 進場價若誤用 T0 收盤 100,MFE 會是 +11%;正確用 T+1 開盤 110 → 約 +0.9%
    assert s["insufficient"] or s["expected_upside"] < 5.0, s


def test_sector_rs_excludes_self():
    """LOO:自己不得進入自己的族群強度,否則是偽裝的個股動能。"""
    # 自己暴漲,同儕平盤 → LOO 後族群強度應接近 0
    seq_self = [100.0] * 10 + [200.0]
    seq_peer = [100.0] * 11
    bars = {"AAA": seq_self, "BBB": seq_peer, "CCC": seq_peer, "DDD": seq_peer}
    rs = osc.sector_rs_10d(bars, "AAA", 10)
    assert rs is not None and abs(rs) < 1e-9, rs
    # 反過來:同儕暴漲、自己平盤 → LOO 後應為正
    bars2 = {"AAA": seq_peer, "BBB": seq_self, "CCC": seq_self, "DDD": seq_self}
    assert osc.sector_rs_10d(bars2, "AAA", 10) > 0.9


def test_sector_rs_requires_minimum_peers():
    """同儕不足時回 None,不得用 2 檔推論族群狀態。"""
    bars = {"AAA": [100.0] * 11, "BBB": [100.0] * 11, "CCC": [100.0] * 11}
    assert osc.sector_rs_10d(bars, "AAA", 10) is None      # LOO 後只剩 2 檔


def test_all_six_metrics_present():
    """章程第 10 條:六項原始指標必須全部保留在資料層。"""
    b = _bars([100 + i for i in range(300)])
    s = osc.realized_opportunity_stats(b, horizon=10)
    for k in ("p_hit_3pct", "expected_upside", "expected_downside",
              "net_positive_rate", "profit_factor", "net_expectancy"):
        assert k in s, k


def test_extended_stage_goes_to_avoid():
    b = _bars([100 + i for i in range(300)])
    r = osc.score_one("9999", b, {}, sector_rank_pct=0.95, stage="EXTENDED")
    assert r["tier"] == "AVOID"


def test_evidence_level_is_not_a_buy_recommendation():
    """章程第 17 條:證據不足不得包裝成買進推薦,UI 必須顯示 evidence level。"""
    assert "PENDING LIVE" in osc.EVIDENCE_LEVEL
    b = _bars([100 + i for i in range(300)])
    r = osc.score_one("9999", b, {}, sector_rank_pct=0.95, stage=None)
    assert r["evidence_level"] == osc.EVIDENCE_LEVEL


def test_frozen_constants_pinned():
    """凍結參數不得被就地更動 —— 要改必須另開版本號。"""
    assert osc.COST == 0.00471
    assert osc.OPPORTUNITY_THRESHOLD == 0.03
    assert osc.SECTOR_TOP_PCT == 0.90
    assert osc.MIN_SECTOR_PEERS == 3
    assert osc.PRIMARY_POSITIVE_RATE == 55.0
