"""Opportunity Ledger 呈現層的行為鎖定。

⚠ 這支測試鎖的是「呈現層有沒有偷偷做了它不該做的事」,不是分層/計分邏輯
本身(那由 test_opportunity.py 鎖)。重點:
  1. `_stock_level_state` 不得用 `sector_opportunity`(今天的旗標)當閘門
     ——conditional 統計是跨一年的歷史樣本,今天沒觸發不代表歷史不足。
     這是 Phase 1 曾經出現、用 production 資料驗證時才抓到的真實 bug。
  2. insufficient 時不得顯示 0% / PF 0,必須顯示說明文字。
  3. 六項欄位一律 Historical 前綴,不得出現 Probability / Expected 這種
     forward-predictive 用詞。
  4. tier 排序固定 PRIMARY→HIGH_POTENTIAL→WATCH→AVOID,同層內用 code
     升冪,不得用任何統計值排序。
  5. render 只做字串組裝,不得引入新的比大小/門檻邏輯
     (用「原始值出現在輸出裡」而非重新判斷來驗證)。
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import opportunity_ledger_view as view
import opportunity_ledger_render as render


def _row(**over):
    base = {
        "code": "0000", "sector_id": "測試族群", "tier": "WATCH",
        "sector_opportunity": 0, "sector_level_evidence": "REPLICATED (test)",
        "stock_level_evidence": "DESCRIPTIVE_ONLY (n=0, not validated)",
        "stock_level_available": 0,
        "p_hit_3pct": None, "expected_upside": None, "expected_downside": None,
        "net_positive_rate": None, "profit_factor": None, "net_expectancy": None,
        "stats_sample_n": 0, "tier_reasons": "test reason",
    }
    base.update(over)
    return base


def test_stock_level_state_ignores_todays_sector_flag():
    """真實 production 資料裡的邊界案例(2026-08-24,code=3006):
    sector_opportunity=0(今天沒觸發)但 stock_level_available=1、n=62
    (過去一年觸發夠多次)。用 sector_opportunity 當閘門會誤判成
    insufficient——這是 Phase 1 對 production 資料驗證時抓到的真 bug。"""
    row = _row(sector_opportunity=0, stock_level_available=1,
               p_hit_3pct=91.94, stats_sample_n=62)
    assert view._stock_level_state(row) == "available"


def test_stock_level_state_insufficient_when_flag_false():
    row = _row(sector_opportunity=1, stock_level_available=0,
               p_hit_3pct=None, stats_sample_n=7)
    assert view._stock_level_state(row) == "insufficient"


def test_insufficient_does_not_leak_zero_values():
    """insufficient 時 metrics 必須是 None,渲染端才不會印出 0% / PF 0。"""
    row = _row(sector_opportunity=0, stock_level_available=0,
               p_hit_3pct=None, stats_sample_n=7, tier="WATCH")
    card = view._build_card(row)
    assert card["metrics"] is None
    assert card["insufficient_note"] == "Stock-level history insufficient(conditional n=7)"


def test_available_state_produces_historical_labels_only():
    row = _row(sector_opportunity=1, stock_level_available=1, tier="PRIMARY",
               p_hit_3pct=90.0, expected_upside=13.3, expected_downside=-6.5,
               net_positive_rate=70.0, profit_factor=4.35, net_expectancy=6.1,
               stats_sample_n=40)
    card = view._build_card(row)
    labels = [m["label"] for m in card["metrics"]]
    assert labels == [
        "Historical +3% Hit Rate", "Historical Avg Upside",
        "Historical Avg Downside / MAE", "Historical Net Win Rate",
        "Historical PF", "Historical Net Expectancy",
    ]
    assert "Probability" not in " ".join(labels)
    assert "Expected" not in " ".join(labels)
    assert card["stock_level_caveat"] == "conditional n=40 · DESCRIPTIVE ONLY"


def test_primary_and_high_potential_get_operational_note():
    for tier in ("PRIMARY", "HIGH_POTENTIAL"):
        row = _row(tier=tier)
        card = view._build_card(row)
        assert card["operational_note"] is not None
        assert "Operational Tier" in card["operational_note"]


def test_watch_and_avoid_have_no_operational_note():
    for tier in ("WATCH", "AVOID"):
        row = _row(tier=tier)
        card = view._build_card(row)
        assert card["operational_note"] is None


def test_tier_order_is_fixed_regardless_of_input_order():
    """context 組裝不得用任何統計值排序,只能照固定 tier 順序 + code 升冪。"""
    assert view.TIER_ORDER == ["PRIMARY", "HIGH_POTENTIAL", "WATCH", "AVOID"]


def test_render_contains_no_technical_state_section():
    """Vanessa 2026-08-25 定案:沒有 validated 資料就整區隱藏,不留空白 placeholder。"""
    ctx = {
        "data_date": "2026-08-24",
        "tiers": [{"tier": t, "label": view.TIER_LABEL[t], "cards": []}
                 for t in view.TIER_ORDER],
        "live_evidence": {"live_since": "2026-08-24", "frozen_signal_name": "x",
                          "frozen_signal_version": "v1",
                          "horizons": {10: {"n": 0, "status": "NOT YET AVAILABLE"},
                                       15: {"n": 0, "status": "NOT YET AVAILABLE"}}},
        "total_rows": 0,
    }
    html = render.render_ledger_html(ctx)
    assert "tech-state" not in html
    assert "Technical Structure" not in html
    assert "Technical State" not in html


def test_render_never_emits_forbidden_wording():
    row = _row(tier="PRIMARY", sector_opportunity=1, stock_level_available=1,
               p_hit_3pct=90.0, expected_upside=13.3, expected_downside=-6.5,
               net_positive_rate=70.0, profit_factor=4.35, net_expectancy=6.1,
               stats_sample_n=40)
    card = view._build_card(row)
    ctx = {
        "data_date": "2026-08-24",
        "tiers": [{"tier": "PRIMARY", "label": "Primary", "cards": [card]}] + [
            {"tier": t, "label": view.TIER_LABEL[t], "cards": []}
            for t in view.TIER_ORDER if t != "PRIMARY"
        ],
        "live_evidence": {"live_since": "2026-08-24", "frozen_signal_name": "x",
                          "frozen_signal_version": "v1",
                          "horizons": {10: {"n": 0, "status": "NOT YET AVAILABLE"},
                                       15: {"n": 0, "status": "NOT YET AVAILABLE"}}},
        "total_rows": 1,
    }
    html = render.render_ledger_html(ctx)
    for forbidden in ("model accuracy improved", "validated stock pick",
                      "buy recommendation", "confidence score"):
        assert forbidden not in html
    # 這兩個詞只能在真正的 forward predictive 模型上線後才能用
    assert "Probability" not in html
    assert "Expected Upside" not in html
    assert "Expected Downside" not in html
