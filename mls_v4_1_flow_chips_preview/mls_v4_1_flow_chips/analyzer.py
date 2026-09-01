"""MLS v4.1 DB-row decision analyzer.

The analyzer does not place trades and does not mutate the existing MLS
state. It translates one ranked row into an explainable preview result.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from .rules import (
    classify_volume_quality,
    compute_clv,
    price_acceptance,
    price_freshness,
    volume_ratio,
)


@dataclass(frozen=True)
class AnalysisInput:
    symbol: str
    name: str
    trade_date: str
    price_data_date: str | None
    chip_data_date: str | None
    flow_data_time: str | None
    snapshot_time: str | None
    net_flow_amount: float | None
    flow_threshold: float | None
    flow_consecutive_ticks: int
    price_change_pct: float | None
    high: float | None
    low: float | None
    close: float | None
    prev_close: float | None
    vwap: float | None
    volume: float | None
    ma5_volume: float | None
    net_active: float | None
    aflow_positive_2_samples: bool
    foreign_net_4d: float | None
    volume_4d: float | None
    big_holder_trend: float | None
    trigger_failed: bool | None
    trigger_passed: bool | None
    market_regime: str | None
    rescue_rule_approved: bool = False


@dataclass(frozen=True)
class AnalysisResult:
    symbol: str
    name: str
    state: str
    action: str
    scenario: str
    flow_direction: str
    flow_threshold_pass: bool | None
    flow_stable: bool
    chip_state: str
    foreign_net_4d_ratio: float | None
    close_location_value: float | None
    clv_confidence: str
    acceptance_ok: bool
    volume_ratio: float | None
    volume_quality: str
    restart_confirmed: bool
    stale_price_data: bool
    summary: str

    def to_dict(self) -> dict:
        return asdict(self)


def _flow_state(inp: AnalysisInput) -> tuple[str, bool | None, bool]:
    amount = inp.net_flow_amount
    if amount is None:
        return "NO_DATA", None, False
    direction = "inflow" if amount > 0 else ("outflow" if amount < 0 else "flat")
    if inp.flow_threshold is None or inp.flow_threshold <= 0:
        return direction, None, False
    passed = abs(amount) >= inp.flow_threshold
    stable = passed and inp.flow_consecutive_ticks >= 2
    return direction, passed, stable


def _chip_state(inp: AnalysisInput) -> tuple[str, float | None]:
    if inp.foreign_net_4d is None or inp.volume_4d is None or inp.volume_4d <= 0:
        return "NO_DATA", None
    ratio = inp.foreign_net_4d / inp.volume_4d
    if ratio > 0:
        return "GOOD", ratio
    if ratio < 0:
        return "WEAK", ratio
    return "FLAT", ratio


def _scenario(flow_direction: str, flow_stable: bool, chip_state: str, suffix: str | None = None) -> str:
    flow_key = f"{flow_direction.upper()}_{'STRONG' if flow_stable else 'UNCONFIRMED'}"
    key = f"{flow_key}__CHIP_{chip_state}"
    return f"{key}__{suffix}" if suffix else key


def _summary(state: str, action: str, flow_direction: str, chip_state: str, vq: str) -> str:
    if state == "STALE_PRICE_DATA":
        return "價格資料日期不符，資料治理閘門已阻止交易判斷。"
    if state.startswith("FALSE_FAIL_RESCUE_HIGH"):
        return "原 Trigger 失敗，但價格重新被接受且近期籌碼仍正；列入強救援觀察，尚未通過樣本外驗證前不產生正式進場。"
    if state.startswith("FALSE_FAIL_RESCUE"):
        return "原 Trigger 失敗，但 Acceptance × Persistent Flow 尚未破壞；保留錯殺觀察。"
    if state == "FLOW_PRICE_DIVERGENCE" or vq == "FLOW_PRICE_DIVERGENCE":
        return "資金/籌碼看似偏多，但價格拒絕上行且量能放大，視為反向警訊。"
    if state == "TRIGGERED_BUT_REJECTED":
        return "Trigger 雖通過，但爆量收低代表市場未接受，降級觀察。"
    if action == "CONSIDER_ENTRY":
        return "盤中資金與近期籌碼共振，且 VWAP/A-flow/Net Active 已確認；僅列為可考慮，不代表保證獲利。"
    if flow_direction == "inflow" and chip_state == "WEAK":
        return "盤中資金大流入，但近期籌碼未跟上；等待確認，不追價。"
    if flow_direction == "outflow" and chip_state == "GOOD":
        return "盤中資金流出但近期籌碼仍好；先判斷洗盤/錯殺，不直接接刀。"
    if flow_direction == "outflow" and chip_state == "WEAK":
        return "盤中資金與近期籌碼同步轉弱，排除。"
    return "條件不足，維持觀察。"


def analyze(inp: AnalysisInput) -> AnalysisResult:
    stale = price_freshness(inp.price_data_date, inp.trade_date) != "OK"
    flow_direction, threshold_pass, flow_stable = _flow_state(inp)
    chip_state, f4_ratio = _chip_state(inp)
    clv, clv_conf = compute_clv(inp.high, inp.low, inp.close, inp.prev_close)
    acceptance = price_acceptance(
        clv,
        clv_conf,
        close=inp.close,
        vwap=inp.vwap,
        prev_close=inp.prev_close,
    )
    vr = volume_ratio(inp.volume, inp.ma5_volume)
    vq = classify_volume_quality(clv, clv_conf, vr)
    above_vwap = bool(inp.close is not None and inp.vwap is not None and inp.close > inp.vwap)
    restart_confirmed = bool(
        above_vwap
        and inp.aflow_positive_2_samples
        and inp.net_active is not None
        and inp.net_active > 0
    )

    if stale:
        state, action = "STALE_PRICE_DATA", "OBSERVE_ONLY"
        scenario = _scenario(flow_direction, flow_stable, chip_state, "STALE")
    elif threshold_pass is None:
        state, action = "NO_FLOW_THRESHOLD", "OBSERVE_ONLY"
        scenario = _scenario(flow_direction, False, chip_state)
    elif inp.trigger_failed:
        if vq == "FLOW_PRICE_DIVERGENCE":
            state, action = "TRUE_FAIL", "EXCLUDE"
            scenario = _scenario(flow_direction, flow_stable, chip_state, "FLOW_PRICE_DIVERGENCE")
        elif chip_state != "GOOD" or not acceptance:
            state, action = "TRUE_FAIL", "EXCLUDE"
            scenario = _scenario(flow_direction, flow_stable, chip_state, "TRUE_FAIL")
        else:
            high_rescue = clv_conf == "VALID" and clv is not None and clv >= 0.75
            state = "FALSE_FAIL_RESCUE_HIGH" if high_rescue else "FALSE_FAIL_RESCUE"
            scenario = _scenario(flow_direction, flow_stable, chip_state, "RESCUE_HIGH" if high_rescue else "RESCUE")
            if inp.market_regime == "RISK_OFF":
                state += "_REGIME_SUSPENDED"
                action = "OBSERVE_ONLY"
            elif not inp.rescue_rule_approved:
                action = "OBSERVE_ONLY"
            else:
                action = "CONSIDER_ENTRY" if restart_confirmed else "WAIT"
    elif inp.trigger_passed and clv_conf == "VALID" and clv is not None and clv < 0.40 and vr is not None and vr > 1.5:
        state, action = "TRIGGERED_BUT_REJECTED", "WAIT"
        scenario = _scenario(flow_direction, flow_stable, chip_state, "TRIGGER_REJECTED")
    else:
        state = "TRIGGER_PASS" if inp.trigger_passed else "FLOW_CHIP_OBSERVATION"
        scenario = _scenario(flow_direction, flow_stable, chip_state)
        if not flow_stable:
            action = "WAIT"
        elif flow_direction == "inflow" and chip_state == "GOOD" and restart_confirmed:
            action = "CONSIDER_ENTRY"
        elif flow_direction == "inflow" and chip_state in ("WEAK", "FLAT"):
            action = "WAIT"
        elif flow_direction == "outflow" and chip_state == "WEAK":
            action = "EXCLUDE"
        elif flow_direction == "outflow" and chip_state == "GOOD":
            action = "OBSERVE_ONLY"
        else:
            action = "WAIT"

    return AnalysisResult(
        symbol=inp.symbol,
        name=inp.name,
        state=state,
        action=action,
        scenario=scenario,
        flow_direction=flow_direction,
        flow_threshold_pass=threshold_pass,
        flow_stable=flow_stable,
        chip_state=chip_state,
        foreign_net_4d_ratio=f4_ratio,
        close_location_value=clv,
        clv_confidence=clv_conf,
        acceptance_ok=acceptance,
        volume_ratio=vr,
        volume_quality=vq,
        restart_confirmed=restart_confirmed,
        stale_price_data=stale,
        summary=_summary(state, action, flow_direction, chip_state, vq),
    )
