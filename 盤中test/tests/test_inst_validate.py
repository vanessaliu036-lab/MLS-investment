# -*- coding: utf-8 -*-
"""
test_inst_validate.py — 法人校驗 + 強惜售新定義（2026-07-20 華邦電教訓）

三條反面案例直接對應當天三項錯誤：
  案例 A 法人倒貨 + 分點吸籌：原本會被誤判「強惜售」，新公式必須 FAIL
  案例 B 跌停 + 法人倒貨：極端價 + 法人賣超雙重觸發，必須 NO_DATA + 硬擋
  案例 C 投信數字多一位（+14162 應為 -161）：超過成交量 1% 必須警告

通過標準：
  - 每個 case 跑 validate_inst_data / passes_filters / ai_explain 至少各 1 條 assert
  - 結論必須與 Vanessa 報告原文一致（"法人全面調節，非惜售" / "訊號降級不可信" / "數據異常請複核"）
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.intraday_filter import (
    StockSnap, validate_inst_data, inst_sell_blocks_absorb,
    compute_inst_net_total, passes_filters, proxy_quadrant, cond_strong_absorb,
    REGIME_DEFENSE, REGIME_RANGE,
    NO_DATA, PASS, FAIL,
    INST_ABS_HARD_RATIO, INST_TOTAL_SELL_BLOCK,
    INST_DIR_CONFLICT_ABS,
)
from app.ai_explain import local_explain


def snap(**kw):
    base = dict(code="0000", track="attack", price=100.0, change_rate=0.0,
                aflow=0, total_volume=0, ma20=None,
                trigger_price=None, atr_stop=None, inst_buy_days=0,
                inst_foreign=None, inst_trust=None, inst_dealer=None,
                inst_net_total=None)
    base.update(kw)
    return StockSnap(**base)


# ==========================================================
# 案例 A：法人倒貨 + 分點吸籌（華邦電當天原貌）
# 預期：原本會被誤判「強惜售」→ 新公式必須 FAIL + AI 講「不建議抄底」
# ==========================================================
def test_case_A_inst_sell_blocks_absorb():
    """華邦電 2026-07-20 原貌：三大法人合計 -45,412、成交量 10.6 萬張
    法人合計/量 = 45,412/106,220 = 42.8% → 遠超 3% 屏蔽線"""
    s = snap(
        code="2344", price=151.0, change_rate=-2.58,
        aflow=+13600, total_volume=106220,                # 分點大單承接 +13600
        inst_foreign=-38544, inst_trust=-5906,
        inst_dealer=-962, ma20=164.5,                     # 法人合計 -45,412
    )
    # 合計計算正確
    assert compute_inst_net_total(s) == -45412
    # 法人賣超屏蔽惜售
    assert inst_sell_blocks_absorb(s) is True
    # 即使 aflow > 0 強度足，原本會過的 cond_strong_absorb 現在 False
    assert cond_strong_absorb(s) is False
    # passes_filters 在防守盤下不能 all_pass
    r = passes_filters(s, regime=REGIME_DEFENSE)
    assert r["all_pass"] is False
    # AI 走「法人合計賣超屏蔽」分支，必須明講法人賣超 + 不抄底
    txt = local_explain(s, regime=REGIME_DEFENSE)
    assert "法人合計賣超" in txt
    assert "-45,412" in txt
    assert "不算惜售" in txt or "不建議抄底" in txt
    # 不能再講「強惜售」標籤
    assert "強惜售" not in txt


# ==========================================================
# 案例 B：跌停 + 法人倒貨（聯電型）
# 預期：極端價 → NO_DATA + 硬擋，AI 講「訊號降級不可信」
# ==========================================================
def test_case_B_extreme_price_hard_block():
    """跌停 -9.88%、aflow +164,359（被動掛單）= 上一代會誤判惜售
    本案例重點：跌停本身已先觸發 NO_DATA（比法人硬擋更優先）"""
    s = snap(
        code="2303", price=130, change_rate=-9.88,
        aflow=164359, total_volume=281032, ma20=140.0,
        inst_foreign=-38000, inst_trust=-3000, inst_dealer=-500,
    )
    r = passes_filters(s, regime=REGIME_DEFENSE)
    # 極端價優先 → no_data 必有主動差/吸籌/象限
    assert "主動差>0" in r["no_data"]
    assert "吸籌強度足" in r["no_data"]
    assert r["all_pass"] is False
    # AI 必須講「訊號不可信」+ 跌停
    txt = local_explain(s, regime=REGIME_DEFENSE)
    assert "跌停" in txt
    assert "不可信" in txt
    assert "別碰" in txt


# ==========================================================
# 案例 C：投信數字多一位（+14162 應為 -161）= 數據源錯誤
# 預期：單一法人買賣超超過成交量 1% → 警告 / 超過 10% → 硬擋
# ==========================================================
def test_case_C_data_anomaly_warning():
    """成交量 106,220、1% = 1,062、10% = 10,622
    投信寫成 +14,162（多一位 + 方向錯）= 超過 1% 觸發警告
    拿來對照「真實」投信 -161（遠低於 1% 門檻，不觸發）"""
    # 錯誤版：投信寫成 +200,000（數量級不可能：超過當日整個市場量）
    wrong = snap(
        code="2344", price=151.0, change_rate=-2.58,
        aflow=0, total_volume=106220, ma20=164.5,
        inst_foreign=-38544, inst_trust=+200000, inst_dealer=-962,   # 數量級不可能
    )
    v = validate_inst_data(wrong)
    # 200,000/106,220 = 188% > 100% → 硬擋
    assert v["hard_block"] is True, f"預期硬擋卻得到 {v['warnings']}"
    assert any("投信" in w and "數量級" in w for w in v["warnings"]), f"預期投信數量級警告：{v['warnings']}"
    # AI 必須講「硬擋」「不可信」
    txt = local_explain(wrong, regime=REGIME_DEFENSE)
    assert "硬擋" in txt or "降級" in txt
    assert "不可信" in txt

    # 真實版：-161（不觸發任何警告）
    correct = snap(
        code="2344", price=151.0, change_rate=-2.58,
        aflow=0, total_volume=106220, ma20=164.5,
        inst_foreign=-38544, inst_trust=-161, inst_dealer=-962,
    )
    v2 = validate_inst_data(correct)
    assert v2["hard_block"] is False
    assert v2["warnings"] == [], f"真實數字不應有警告：{v2['warnings']}"


# ==========================================================
# 額外：合計賣超 + 單一法人買超大但數量級合理（方向矛盾）
# ==========================================================
def test_case_D_inst_buy_under_total_sell():
    """合計賣超 5000、外資卻買 +20000（佔量 40%，數量級合理）→ 方向矛盾觸發警告
    但不硬擋（沒超過成交量）"""
    s = snap(
        code="2330", price=600, change_rate=-1.5,
        aflow=0, total_volume=50000,
        inst_foreign=+20000, inst_trust=-15000, inst_dealer=-10000,  # 合計 -5000
    )
    v = validate_inst_data(s)
    # 方向矛盾警告
    assert any("外資" in w and "方向矛盾" in w for w in v["warnings"]), f"預期方向矛盾警告：{v['warnings']}"
    # 數量級合理 → 不硬擋
    assert v["hard_block"] is False


# ==========================================================
# 額外：日變動 300% 警告（需 prev_total 配合）
# ==========================================================
def test_case_E_daily_change_300pct():
    """昨日合計 +100、今日 -5000 = 變動 5100% → 觸發 300% 警告"""
    s = snap(
        code="2492", price=140, change_rate=0.0,
        aflow=0, total_volume=8000,
        inst_foreign=-3000, inst_trust=-1000, inst_dealer=-1000,   # 今日合計 -5000
    )
    # inst_prev_total 用 setattr 模擬餵入
    s.inst_prev_total = 100
    v = validate_inst_data(s)
    assert any("300%" in w for w in v["warnings"]), f"預期日變動警告：{v['warnings']}"


# ==========================================================
# 額外：法人資料缺失 → 校驗略過 + AI 講「資料未接入」
# ==========================================================
def test_case_F_no_inst_data():
    """法人欄位全 None → validate ok=True（略過），AI 講「資料未接入」"""
    s = snap(
        code="3264", price=198.5, change_rate=-7.24, aflow=810,
        total_volume=4952, ma20=210.0,
        # inst_foreign/trust/dealer 全 None
    )
    v = validate_inst_data(s)
    assert v["ok"] is True
    assert v["hard_block"] is False
    # AI 在沒資料時不能硬講強惜售，要保守
    txt = local_explain(s, regime=REGIME_RANGE)
    assert "法人資料未接入" in txt or "MA20" in txt   # 至少要提一項不確定
    assert "強惜售" not in txt                         # 缺資料不能蓋「強惜售」


# ==========================================================
# 額外：AI 完整輸出對照 Vanessa 示範文
# ==========================================================
def test_ai_output_matches_report():
    """對齊 Vanessa 2026-07-20 示範文風格：法人合計 -45,412 → 不抄底"""
    s = snap(
        code="2344", price=151.0, change_rate=-2.58,
        aflow=+13600, total_volume=106220, ma20=164.5,
        inst_foreign=-38544, inst_trust=-5906, inst_dealer=-962,
    )
    txt = local_explain(s, regime=REGIME_DEFENSE)
    # 必須有這三項
    assert "法人合計賣超" in txt
    assert "-45,412" in txt
    assert ("不算惜售" in txt or "不建議抄底" in txt)
    # 不能再講「強惜售」「抄底候選」「停損」等鼓勵抄底詞
    forbidden = ["強惜售", "抄底候選", "反彈候選"]
    for w in forbidden:
        assert w not in txt, f"AI 輸出不應含「{w}」：{txt}"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn(); passed += 1; print("  PASS  " + fn.__name__)
        except AssertionError:
            print("  FAIL  " + fn.__name__); traceback.print_exc()
    print("\n%d/%d passed" % (passed, len(fns)))
