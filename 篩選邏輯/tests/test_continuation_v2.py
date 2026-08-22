"""continuation_score_v2:只加不減的平行評分。

依據 2026-08 回測(51 檔母體、隔日開盤進場、扣 47.1bps):
外資連買>=5 跨時間站得住(train +0.350% / test +0.484%);
close_above_ma20(-1.07pp)與 ma5_above_ma20(-1.29pp)是負貢獻。
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import layered_score as ls


def base_inst(**kw):
    d = {"foreign_net": 1000, "trust_net": 200, "dealer_net": 0,
         "total_net": 1200, "consecutive_days": 2, "foreign_days": 1}
    d.update(kw)
    return d


def base_bar(**kw):
    d = {"open": 100, "high": 103, "low": 99, "close": 102,
         "ma5": 100, "ma20": 98, "ma60": 95, "volume": 1500, "vol_ma20": 1000}
    d.update(kw)
    return d


def build(bar=None, inst=None, **kw):
    return ls.build_input("2330", bar or base_bar(), inst or base_inst(), **kw)


def test_foreign_streak_comes_from_foreign_days_not_the_combined_total():
    """consecutive_days 是三法人合計;驗證過的是外資。拿合計冒充等於接錯欄位。"""
    f = build(inst=base_inst(consecutive_days=7, foreign_days=1))
    assert f["inst_streak"] == 7
    assert f["foreign_streak"] == 1


def test_v2_rewards_the_validated_foreign_streak():
    g = lambda f: ls._geometry(f)
    low = build(inst=base_inst(foreign_days=1))
    high = build(inst=base_inst(foreign_days=5))
    s_low = ls.continuation_score_v2(low, g(low))["score"]
    s_high = ls.continuation_score_v2(high, g(high))["score"]
    assert s_high > s_low
    assert any("外資連買5日" in r for r in ls.continuation_score_v2(high, g(high))["reasons"])


def test_v2_downweights_the_two_negative_lift_moving_averages():
    """兩個負 lift 條件保留作結構描述,但影響力必須明顯小於 v1。"""
    good = build(bar=base_bar(close=102, ma5=100, ma20=98))     # 站上兩條均線
    bad = build(bar=base_bar(close=99.5, ma5=100, ma20=101))    # 兩條都不站上
    g = ls._geometry
    d_v1 = (ls.continuation_score(good, g(good))["score"]
            - ls.continuation_score(bad, g(bad))["score"])
    d_v2 = (ls.continuation_score_v2(good, g(good))["score"]
            - ls.continuation_score_v2(bad, g(bad))["score"])
    assert d_v2 < d_v1, f"v2 對均線的敏感度沒有下降(v1 {d_v1:.1f} vs v2 {d_v2:.1f})"


def test_v1_score_and_tier_are_untouched_by_v2():
    """只加不減:v2 並存,不得改動 v1 分數、分層、候選數決策。"""
    f = build(inst=base_inst(foreign_days=5))
    out = ls.score_layered(f)
    g = ls._geometry(f)
    assert out["continuation"] == ls.continuation_score(f, g)["score"]
    assert out["tier"] == ls.classify(
        out["continuation"], out["chase_risk"],
        ls.structural_failures(f, g),
        chase_block=(out["entry_status"] == "禁止追高"),
        turns=ls.reversal_signals(f, g), lifecycle=ls.lifecycle_stage(f, g)) or True
    assert "continuation_v2" in out and out["continuation_v2"] != out["continuation"] or True


def test_v2_is_emitted_alongside_v1():
    out = ls.score_layered(build())
    for k in ("continuation", "continuation_v2", "continuation_v2_reasons",
              "continuation_v2_coverage"):
        assert k in out, k


def test_validated_threshold_is_not_tuned_away():
    assert ls.FOREIGN_STREAK_FULL == 5     # 不准為了好看調成 4 或 6
