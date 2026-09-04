# -*- coding: utf-8 -*-
"""隔離盤中測試服務。

這個服務只讀既有 MLS broker 的 Shioaji 訂閱 buffer，不寫資料庫、
不啟動第二組訂閱，也不改主站的 STATE；另將最後一筆有效盤中結果
原子保存為 VPS 本地快照，供收盤後 API 還原。部署到 VPS 時可獨立跑在 8002。
"""

import os
import sys
import time
import json
import importlib.util
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import broker  # noqa: E402  (VPS 的既有真實行情連線)
import config  # noqa: E402
from tw_price_limit import is_limit_up  # noqa: E402
try:
    import quote_health  # noqa: E402  (行程內 MIS 備援 + 資料品質判定)
except Exception:        # 備援模組缺席時不擋主流程(維持舊行為)
    quote_health = None
try:
    from mls_intraday import intraday_filter as F  # noqa: E402
except ImportError:
    from app import intraday_filter as F  # noqa: E402
try:
    from mls_intraday import ai_explain  # noqa: E402
except ImportError:
    from app import ai_explain  # noqa: E402
try:
    from mls_intraday import classify  # noqa: E402
except ImportError:
    from app import classify  # noqa: E402
try:
    import review_rules  # noqa: E402  (盤後驗證：分類規則命中率，自動累積)
except ImportError:
    review_rules = None
try:
    import market_breadth  # noqa: E402  (市場資金廣度：Risk On/Off 與真假行情)
except ImportError:
    market_breadth = None

try:
    from pre_activation import (overlay_live_price_activation,
                                overlay_foreign_confirmation)
except ImportError:
    # 正式 8000 服務的 cwd 不一定是「篩選邏輯」；用檔案位置載入同一份
    # 純判定函式,避免盤中路徑另寫一套價格 Activation 規則。
    #
    # ⚠ 路徑要試兩個位置,而且順序不能反(2026-08-27 部署前驗出來的):
    #   · 本機開發:repo 是 <BASE>/篩選邏輯/pre_activation.py
    #   · VPS 正式:8000 站在 /opt/mls-intraday,但引擎正本在 /opt/mls-screen,
    #     /opt/mls-intraday/篩選邏輯/ 根本沒有這支檔(見 memory
    #     ab-engine-runtime-topology)。原本只找 BASE/篩選邏輯/ 會靜默 fallback
    #     成 None,兩個 overlay 直接被跳過——不會報錯,但功能完全沒生效。
    # 找不到就維持 None(呼叫端有 guard),但一定要印出來,不要無聲失敗。
    _pa_mod = None
    for _pa_path in (BASE / "篩選邏輯" / "pre_activation.py",
                     Path("/opt/mls-screen/pre_activation.py")):
        if not _pa_path.exists():
            continue
        _pa_spec = importlib.util.spec_from_file_location("_mls_pre_activation", _pa_path)
        if _pa_spec and _pa_spec.loader:
            _pa_mod = importlib.util.module_from_spec(_pa_spec)
            _pa_spec.loader.exec_module(_pa_mod)
            break
    if _pa_mod is not None:
        overlay_live_price_activation = _pa_mod.overlay_live_price_activation
        overlay_foreign_confirmation = _pa_mod.overlay_foreign_confirmation
    else:
        print("[pre_activation] 找不到 pre_activation.py，盤中 PA overlay 停用"
              "（外資與價格啟動將維持盤後快照原值）")
        overlay_live_price_activation = None
        overlay_foreign_confirmation = None

router = APIRouter()
HISTORY_DB = BASE / "intraday_eod.db"
CHIP_CACHE = BASE / "個股卡片相關檔案_20260722" / "chips_cache.json"
INTRADAY_SNAPSHOT_PATH = BASE / "intraday_live_snapshot.json"
TW_TZ = ZoneInfo("Asia/Taipei")
SOURCE_TABLE = "intraday_live_snapshot"
SOURCE_VERSION = "aflow-volume-canonical-v1"

# 依「篩選邏輯/screen intraday.py」的 100 分權重。該文件實際定義的是
# 六個加權因子（合計 100），不是前端原本用漲跌幅假算的分數。
FACTOR_WEIGHTS = {
    "money_health": 30,
    "net_active": 22,
    "absorption": 18,
    "vs_ma20": 12,
    "inst_streak": 10,
    "margin": 8,
}

# 決策首頁、雷達與快照回退共用同一個排序契約。分類先決定「先看誰」，
# 分類內才比較盤中達標程度；同分的價格／資金只做最後的穩定排序。
DECISION_STATUS_ORDER = {"可進場": 0, "等待確認": 1, "資料不足": 2, "風險警報": 3}
HOME_DECISION_ORDER = {
    "🟢 可進場": 0,
    "🔵 等回測": 1,
    "🟡 等待觀察": 2,
    "🟠 承接觀察": 3,
    "🔴 不進場": 4,
}
DISPLAY_GROUP_ORDER = {"可操作": 0, "觀察": 1, "排除": 2}

# 雷達狀態回答「現在能不能買」，不再把代理象限的多方方向直接當成買點。
RADAR_STATUS_ORDER = {"可進場": 0, "等回測": 1, "保留觀察": 2,
                      "尚未觸發": 3, "不進場": 4}
RADAR_ENTRY_EXTENSION_PCT = 3.0
RADAR_PULLBACK_BAND_PCT = 2.0


def _display_number(value):
    """排序用數字；缺值永遠排在同分類的已知數值之後。"""
    try:
        number = float(value)
        return number if number == number else -1e12
    except (TypeError, ValueError):
        return -1e12


def _display_sort_key(row):
    return (
        HOME_DECISION_ORDER.get(
            row.get("home_decision_label"),
            DECISION_STATUS_ORDER.get(
                row.get("decision_status"),
                DISPLAY_GROUP_ORDER.get(row.get("group"), 9),
            ),
        ),
        -_display_number(row.get("score_pct")),
        -_display_number(row.get("score")),
        -_display_number(row.get("change_rate")),
        -_display_number(row.get("aflow")),
    )


def _sort_display_rows(rows):
    rows.sort(key=_display_sort_key)
    return rows


def _optional_float(value):
    """把可選行情欄位轉成有限浮點數；缺值維持 None，不猜數字。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _row_optional_float(row, *keys):
    for key in keys:
        value = _optional_float(row.get(key))
        if value is not None:
            return value
    return None


def _radar_price_position(row, *, extension_state=None):
    """判斷價格是否仍在有效進場區，不把「今天上漲」當成 Price Gate。"""
    price = _optional_float(row.get("price"))
    trigger = _row_optional_float(row, "trigger_price", "entry_ref", "key_price")
    signal_kind = str(row.get("signal_kind") or row.get("entry_kind") or "").lower()
    entry_rule = str(row.get("entry_rule") or "")
    is_pullback = signal_kind in {"pullback", "engine", "resilient"} or any(
        word in entry_rule for word in ("回測", "月線", "MA20", "引擎"))

    def zone_for(value):
        if value is None or value <= 0:
            return {"low": None, "high": None, "text": None}
        low = value * (1 - RADAR_PULLBACK_BAND_PCT / 100) if is_pullback else value
        high = value * (1 + (RADAR_PULLBACK_BAND_PCT if is_pullback
                              else RADAR_ENTRY_EXTENSION_PCT) / 100)
        return {"low": round(low, 2), "high": round(high, 2),
                "text": f"{low:.2f}–{high:.2f}"}

    zone = zone_for(trigger)

    if price is None:
        return {"state": "UNKNOWN", "trigger_price": trigger,
                "distance_pct": None, "zone": zone,
                "reason": "缺現價，無法確認價格位置。"}
    if extension_state == "EXTENDED":
        distance = ((price / trigger) - 1) * 100 if trigger else None
        return {"state": "TOO_FAR", "trigger_price": trigger,
                "distance_pct": round(distance, 2) if distance is not None else None,
                "zone": zone,
                "reason": "價格已過度延伸，等待回測有效進場區。"}
    if trigger is None or trigger <= 0:
        return {"state": "UNKNOWN", "trigger_price": None,
                "distance_pct": None, "zone": zone,
                "reason": "缺關鍵價，無法確認有效進場區。"}

    distance = (price / trigger - 1) * 100
    if is_pullback:
        if -RADAR_PULLBACK_BAND_PCT <= distance <= RADAR_PULLBACK_BAND_PCT:
            state, reason = "IN_ZONE", "現價位於回測有效區。"
        elif distance > RADAR_PULLBACK_BAND_PCT:
            state, reason = "TOO_FAR", f"現價高於回測進場區 {distance:+.1f}%，等待回測。"
        else:
            state, reason = "BROKEN", f"現價跌破回測關鍵價 {abs(distance):.1f}%，價格結構失效。"
    elif distance < 0:
        state, reason = "NOT_TRIGGERED", f"尚未站上關鍵價，距離 {distance:+.1f}%。"
    elif distance <= RADAR_ENTRY_EXTENSION_PCT:
        state, reason = "IN_ZONE", f"現價位於突破有效區，距觸發價 {distance:+.1f}%。"
    else:
        state, reason = "TOO_FAR", f"現價已高於觸發價 {distance:+.1f}%，超出有效進場區。"
    return {"state": state, "trigger_price": trigger,
            "distance_pct": round(distance, 2), "zone": zone, "reason": reason}


def _radar_judgment(row, *, ma20=None, base_status=None, data_missing=None,
                    structure_confirmed=None, extension_state=None):
    """機會雷達 canonical 判讀：方向、結構、價格位置三個責任分離。"""
    price = _optional_float(row.get("price"))
    change = _optional_float(row.get("change_rate"))
    flow = _optional_float(row.get("aflow"))
    vwap = _optional_float(row.get("avg_price"))
    ma20_value = _optional_float(ma20 if ma20 is not None else row.get("ma20"))
    if structure_confirmed is None:
        structure_confirmed = bool(
            (price is not None and vwap is not None and price >= vwap)
            or (price is not None and ma20_value is not None and price >= ma20_value)
        )
    missing = list(dict.fromkeys(data_missing or []))
    if price is None and "現價" not in missing:
        missing.append("現價")
    if change is None and "漲跌幅" not in missing:
        missing.append("漲跌幅")
    if flow is None and "主動資金" not in missing:
        missing.append("主動資金")
    if vwap is None and ma20_value is None and "VWAP／MA20位置" not in missing:
        missing.append("VWAP／MA20位置")

    position = _radar_price_position(row, extension_state=extension_state)
    flow_positive = flow is not None and flow > 0
    direction_confirmed = bool(change is not None and change > 0 and flow_positive)
    quadrant = row.get("quadrant")
    true_attack = direction_confirmed and (quadrant in (None, "真攻擊"))
    structural_failures = row.get("structural_failures") or []
    risk = bool(
        (flow is not None and flow < 0)
        or (change is not None and change < 0 and not structure_confirmed
            and flow is not None and flow <= 0)
        or len(structural_failures) >= 2
        or position["state"] == "BROKEN"
        or base_status in {"風險警報", "不進場"}
    )
    wait_for = []
    if position["state"] == "TOO_FAR":
        trigger = position.get("trigger_price")
        wait_for.append(f"回測關鍵價 {trigger:g}" if trigger else "回測有效進場區")
    elif position["state"] == "NOT_TRIGGERED":
        trigger = position.get("trigger_price")
        wait_for.append(f"站上關鍵價 {trigger:g}" if trigger else "站上關鍵價")
    elif position["state"] == "UNKNOWN":
        wait_for.append("補齊引擎關鍵價／進場區")
    elif position["state"] == "BROKEN":
        wait_for.append("價格重新站回關鍵價")
    if flow is None:
        wait_for.append("主動資金資料")
    elif flow <= 0:
        wait_for.append("A-flow 翻正且持續")
    elif change is not None and change <= 0:
        wait_for.append("價格止跌並與資金同步")
    if not structure_confirmed:
        wait_for.append("站回 VWAP／MA20")
    wait_for = list(dict.fromkeys(wait_for))

    if risk:
        status = "不進場"
        reason = "不進場｜結構或資金條件失效。"
        if change is not None and change > 0 and flow is not None and flow < 0:
            reason = (f"不進場｜價量背離：漲幅 {change:+.2f}%，但主動資金 "
                      f"{flow:+,.0f} 張；代理象限只代表方向真假，不代表可買。")
        next_step = "不追價，等待資金翻正、結構修復後重新判定。"
    elif missing:
        status = "保留觀察"
        reason = "保留觀察｜資料尚未完整：" + "、".join(missing) + "。"
        next_step = "等待：" + "、".join(wait_for or missing) + "。"
    elif true_attack and structure_confirmed and position["state"] == "IN_ZONE":
        status = "可進場"
        reason = "可進場｜多方結構成立，主動資金確認，現價仍位於有效進場區。"
        next_step = "依既定停損與部位規則執行，不追價加碼。"
    elif true_attack and structure_confirmed and position["state"] == "TOO_FAR":
        status = "等回測"
        reason = (f"方向成立｜目前價格偏離進場區，等待回測確認，不追價。"
                  f"（{position['reason']}）")
        next_step = (f"不追價，等待回測關鍵價 {position['trigger_price']:g}"
                     f" 附近，且 A-flow 維持正值／重新出現承接後再判定。"
                     if position.get("trigger_price") else
                     "不追價，等待回測有效進場區，且 A-flow 維持正值／重新出現承接後再判定。")
    elif true_attack and not structure_confirmed:
        status = "保留觀察"
        reason = "觀察｜攻擊訊號出現，但結構或進場條件尚未完整確認。"
        next_step = "等待：" + "、".join(wait_for or ["站回 VWAP／MA20", "確認承接"]) + "。"
    elif flow_positive and change is not None and change <= 0:
        status = "保留觀察"
        reason = "觀察｜主動資金流入但價格尚未止穩，先確認是健康換手而非反彈失敗。"
        next_step = "等待：價格止跌、收復 VWAP／MA20，且 A-flow 維持正值；未確認前不搶反彈。"
    elif flow_positive or (change is not None and change > 0):
        status = "尚未觸發"
        reason = "候選｜尚未出現完整攻擊訊號。"
        next_step = "等待：主動買盤持續為正、價格站上關鍵價，並確認結構同步。"
    else:
        status = "保留觀察"
        reason = "觀察｜攻擊訊號出現，但進場條件尚未完整確認。"
        next_step = "等待資金、結構與價格位置同步。"

    return {
        "status": status,
        "code": {"可進場": "ENTRY", "等回測": "PULLBACK", "保留觀察": "OBSERVE",
                 "尚未觸發": "CANDIDATE", "不進場": "NO_ENTRY"}[status],
        "can_buy": status == "可進場", "reason": reason, "next_step": next_step,
        "direction_confirmed": direction_confirmed, "true_attack": true_attack,
        "price_position": position, "price_gate": position["state"] == "IN_ZONE",
        "structure_gate": bool(structure_confirmed), "money_gate": bool(flow_positive),
        "missing": missing, "wait_for": wait_for,
    }


def _sector_context(rows):
    """只用當次同源盤中 rows 建立族群相對強度，避免另抓一份行情。"""
    buckets = {}
    for row in rows:
        price = _optional_float(row.get("price"))
        change = _optional_float(row.get("change_rate"))
        if price is None or change is None:
            continue
        sector = row.get("sector") or "其他"
        buckets.setdefault(sector, []).append(change)

    context = {}
    for sector, changes in buckets.items():
        positive = sum(1 for change in changes if change > 0)
        average = sum(changes) / len(changes) if changes else None
        context[sector] = {
            "count": len(changes),
            "breadth_pct": round(positive / len(changes) * 100, 1),
            "average_change": round(average, 2) if average is not None else None,
        }
    averages = [v["average_change"] for v in context.values()
                if v["average_change"] is not None]
    for values in context.values():
        if values["average_change"] is None or not averages:
            values["relative_pct"] = None
        else:
            rank = sum(average <= values["average_change"] for average in averages)
            values["relative_pct"] = round(rank / len(averages) * 100, 1)
    return context


def _attach_radar_execution_overlay(rows):
    """機會雷達的交易執行層。

    這一層只回答「現在值得追蹤、等待什麼或先跳過」，不回寫 C1/C2、七因子
    score、group 或盤後驗證結果；只同步首頁可見的 home_decision。EXTENDED
    會要求回測，資金轉弱／背離或價格失效則直接標記不進場。
    """
    sectors = _sector_context(rows)
    for row in rows:
        price = _optional_float(row.get("price"))
        change = _optional_float(row.get("change_rate"))
        flow = _optional_float(row.get("aflow"))
        volume_ratio = _row_optional_float(row, "volume_ratio")
        total_volume = _optional_float(row.get("total_volume"))
        flow_ratio = (flow / total_volume if flow is not None and total_volume and total_volume > 0
                      else None)
        pa = row.get("pre_activation") or {}
        # Shioaji 訂閱 TickSTKv1 的 volume_ratio 常固定回 0；0 在這裡代表
        # 「來源未提供」，不是量能真的為零。沿用既有 PA 快照的已知量比，並
        # 明示來源，避免把資料缺口誤判成動能消失。
        volume_ratio_source = "LIVE" if volume_ratio is not None and volume_ratio > 0 else None
        if volume_ratio is None or volume_ratio <= 0:
            pa_volume_ratio = _row_optional_float(pa, "volume_ratio")
            if pa_volume_ratio is not None and pa_volume_ratio > 0:
                volume_ratio = pa_volume_ratio
                volume_ratio_source = "PRE_ACTIVATION_CACHE"
        ma5_distance = _row_optional_float(pa, "ma5_distance_pct")
        breakout_5d = _row_optional_float(pa, "breakout_5d_pct")
        return_3d = _row_optional_float(row, "return_3d", "change_3d", "price_return_3d")
        vwap = _optional_float(row.get("avg_price"))
        acceptance = ("CONFIRMED" if price is not None and vwap is not None and price >= vwap
                      else "FAILED" if price is not None and vwap is not None else "UNKNOWN")
        trigger = bool(pa.get("price_activated")) or bool(row.get("is_limit_up"))
        if breakout_5d is not None and breakout_5d >= 0:
            trigger = True
        trigger_state = "CONFIRMED" if trigger else "PENDING"
        sector_values = sectors.get(row.get("sector") or "其他", {})
        breadth = sector_values.get("breadth_pct")
        sector_avg = sector_values.get("average_change")
        sector_relative = sector_values.get("relative_pct")
        sector_strong = bool(
            breadth is not None and sector_avg is not None
            and breadth >= 60 and sector_avg > 0
            and (sector_values.get("count", 0) >= 2 or breadth >= 75)
        )

        opportunity_points = 0.0
        opportunity_reasons = []
        if flow_ratio is not None and flow_ratio >= 0.03:
            opportunity_points += 25
            opportunity_reasons.append(f"A-flow 佔量 {flow_ratio * 100:.1f}%")
        elif flow is not None and flow > 0:
            opportunity_points += 15
            opportunity_reasons.append(f"A-flow {flow:+,.0f}")
        if sector_strong:
            opportunity_points += 20
            opportunity_reasons.append(f"族群廣度 {breadth:.0f}%")
        elif breadth is not None and breadth >= 50 and sector_avg is not None and sector_avg > 0:
            opportunity_points += 10
            opportunity_reasons.append(f"族群偏強 {breadth:.0f}%")
        if sector_relative is not None and sector_relative >= 80:
            opportunity_points += 15
            opportunity_reasons.append("族群相對強度前 20%")
        elif sector_avg is not None and sector_avg > 0:
            opportunity_points += 8
        if volume_ratio is not None and volume_ratio >= 1.5:
            opportunity_points += 15
            opportunity_reasons.append(f"RVOL {volume_ratio:.2f}x")
        elif volume_ratio is not None and volume_ratio >= 1.2:
            opportunity_points += 8
        if trigger:
            opportunity_points += 15
            opportunity_reasons.append("PRICE TRIGGER 已確認")
        if acceptance == "CONFIRMED":
            opportunity_points += 10
            opportunity_reasons.append("站在 VWAP 上方")
        score_pct = _optional_float(row.get("score_pct"))
        if score_pct is not None and score_pct >= 65:
            opportunity_points += 10

        extension_flags = []
        if pa.get("stage") == "EXTENDED":
            extension_flags.append("PA stage EXTENDED")
        if ma5_distance is not None and ma5_distance >= 7:
            extension_flags.append(f"距 MA5 {ma5_distance:+.1f}%")
        if breakout_5d is not None and breakout_5d >= 3:
            extension_flags.append(f"突破 5D 高 {breakout_5d:+.1f}%")
        if return_3d is not None and return_3d >= 15:
            extension_flags.append(f"近 3 日 {return_3d:+.1f}%")
        if volume_ratio is not None and volume_ratio >= 2.5:
            extension_flags.append(f"RVOL {volume_ratio:.2f}x 爆量")
        if change is not None and (row.get("is_limit_up") or change >= 9.5):
            extension_flags.append("極端漲幅")
        vwap_loss = acceptance == "FAILED"
        trigger_loss = not trigger
        flow_negative = flow is not None and flow <= 0
        climax = volume_ratio is not None and volume_ratio >= 2.5
        exhausted = bool(flow_negative and climax and (vwap_loss or trigger_loss))
        if exhausted:
            extension_state = "EXHAUSTED"
            extension_risk = "HIGH"
            risk_reasons = ["資金轉弱", "爆量", "失守 VWAP/Trigger"]
        else:
            extension_state = "EXTENDED" if extension_flags else "NORMAL"
            extension_risk = ("HIGH" if len(extension_flags) >= 2 or pa.get("stage") == "EXTENDED"
                              else "MEDIUM" if extension_flags else "LOW")
            risk_reasons = extension_flags[:]
            if vwap_loss:
                risk_reasons.append("失守 VWAP")
            if flow_negative:
                risk_reasons.append("A-flow 非正")

        risk_points = min(100.0, len(extension_flags) * 15.0
                          + (15 if vwap_loss else 0)
                          + (15 if flow_negative else 0)
                          + (15 if climax else 0))
        edge = round(opportunity_points - risk_points, 1)
        opportunity_points = round(min(100.0, opportunity_points), 1)
        if opportunity_points >= 80:
            opportunity_score = "VERY HIGH"
        elif opportunity_points >= 65:
            opportunity_score = "HIGH"
        elif opportunity_points >= 50:
            opportunity_score = "MEDIUM"
        else:
            opportunity_score = "LOW"

        strong_flow = (flow_ratio is not None and flow_ratio >= 0.03) or (flow is not None and flow > 0)
        momentum_base = (strong_flow and sector_strong and volume_ratio is not None
                         and volume_ratio >= 1.5 and trigger and acceptance == "CONFIRMED"
                         and opportunity_points >= 65)
        acceleration = bool(momentum_base and volume_ratio >= 2.0
                            and change is not None and change >= 5.0)
        if extension_state == "EXHAUSTED":
            momentum_state = "EXHAUSTED"
        elif acceleration:
            momentum_state = "ACCELERATION"
        elif momentum_base:
            momentum_state = "CONTINUATION"
        else:
            momentum_state = "NONE"

        if momentum_state in ("CONTINUATION", "ACCELERATION"):
            trade_state = "MOMENTUM"
            position_rule = "1/3 START"
            execution_action = ("FIRST PULLBACK / FLOW RE-ACCELERATION"
                                if momentum_state == "CONTINUATION"
                                else "1/3 ONLY / FLOW RE-ACCELERATION")
        elif momentum_state == "EXHAUSTED":
            trade_state = "ACTIVE" if trigger else "WATCH"
            position_rule = "NO TRADE"
            execution_action = "NO CHASE / WAIT VWAP + FLOW RECOVERY"
        elif trigger and acceptance == "CONFIRMED" and strong_flow:
            trade_state = "ACTIVE"
            position_rule = "FULL" if extension_state == "NORMAL" else "1/2"
            execution_action = "CONFIRMED ENTRY / ACCEPTANCE HOLD"
        elif trigger or pa.get("stage") in ("ARMED", "TRIGGER"):
            trade_state = "ARMED"
            position_rule = "NO TRADE"
            execution_action = "WAIT ACCEPTANCE + FLOW CONFIRMATION"
        else:
            trade_state = "WATCH"
            position_rule = "NO TRADE"
            execution_action = "WAIT PRICE / FLOW CONFIRMATION"

        # 交易狀態與代理象限分責：象限只描述方向真假，這裡才決定
        # 「值得追蹤、等回測或先跳過」。量比等非必要欄位缺失不會被誤當
        # 成失敗；但沒有現價、漲跌、A-flow 或關鍵位置就不放行。
        radar = _radar_judgment(
            row,
            data_missing=[],
            base_status=row.get("decision_status"),
            structure_confirmed=(
                price is not None and (
                    (vwap is not None and price >= vwap)
                    or (vwap is None and _optional_float(row.get("ma20")) is not None
                        and price >= _optional_float(row.get("ma20")))
                )
            ),
            extension_state=extension_state,
        )
        radar_label = {
            "可進場": "🟢 可進場",
            "等回測": "🔵 等回測",
            "保留觀察": "🟡 保留觀察",
            "尚未觸發": "⚪ 尚未觸發",
            "不進場": "🔴 不進場",
        }[radar["status"]]
        money_label = row.get("intraday_money_nature_label") or "主動資金待確認"
        radar_reading = f"{money_label}｜{radar['reason']}"
        price_position = radar["price_position"]
        if radar["price_gate"]:
            price_gate_label = "PASS｜有效進場區"
        elif price_position["state"] == "TOO_FAR":
            price_gate_label = "WAIT｜偏離進場區，等回測"
        elif price_position["state"] == "NOT_TRIGGERED":
            price_gate_label = "WAIT｜尚未站上關鍵價"
        elif price_position["state"] == "BROKEN":
            price_gate_label = "FAIL｜跌破關鍵價"
        else:
            price_gate_label = "WAIT｜關鍵價／進場區待確認"
        structure_gate_label = "PASS｜站穩 VWAP／MA20" if radar["structure_gate"] else "WAIT｜站回 VWAP／MA20"

        row.update({
            "extension_risk": extension_risk,
            "extension_state": extension_state,
            "momentum_state": momentum_state,
            "opportunity_score": opportunity_score,
            "opportunity_score_value": opportunity_points,
            "risk_score": round(risk_points, 1),
            "edge": edge,
            "position_rule": position_rule,
            "trade_state": trade_state,
            "trade_state_label": trade_state,
            "execution_action": execution_action,
            "acceptance_state": acceptance,
            "price_triggered": trigger,
            "trigger_state": trigger_state,
            "sector_breadth_pct": breadth,
            "sector_average_change": sector_avg,
            "sector_relative_pct": sector_relative,
            "radar_volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
            "volume_ratio_source": volume_ratio_source,
            "momentum_reasons": opportunity_reasons,
            "risk_reasons": risk_reasons,
            "risk_adjusted_policy": "OPPORTUNITY_MINUS_RISK_V1",
            "radar_status": radar["status"],
            "radar_status_code": radar["code"],
            "radar_status_reason": radar["reason"],
            "radar_next_step": radar["next_step"],
            "radar_direction_confirmed": radar["direction_confirmed"],
            "radar_true_attack": radar["true_attack"],
            "radar_price_position": radar["price_position"]["state"],
            "radar_price_position_reason": radar["price_position"]["reason"],
            "radar_price_distance_pct": radar["price_position"]["distance_pct"],
            "radar_price_trigger": radar["price_position"]["trigger_price"],
            "radar_price_zone_low": radar["price_position"]["zone"]["low"],
            "radar_price_zone_high": radar["price_position"]["zone"]["high"],
            "radar_price_zone": radar["price_position"]["zone"]["text"],
            "radar_gates": {
                "price": "PASS" if radar["price_gate"] else "FAIL",
                "money": "PASS" if radar["money_gate"] else "FAIL",
                "structure": "PASS" if radar["structure_gate"] else "FAIL",
            },
            "radar_follow_action": "持續觀察" if radar["status"] in ("可進場", "等回測", "保留觀察") else "先跳過",
            "radar_wait_for": radar["wait_for"],
            "radar_wait_signal": "、".join(radar["wait_for"]) if radar["wait_for"] else "—",
            # 保留 radar_* 作為雷達 API 的專用欄位；首頁的可見決策則同步
            # 到同一個 canonical home_decision，避免首頁與雷達各自判讀。
            "radar_decision": {
                "code": radar["code"],
                "label": radar_label,
                "decision": radar["status"],
                "money_state": money_label,
                "price_gate": "PASS" if radar["price_gate"] else "WAIT",
                "price_gate_label": price_gate_label,
                "structure_gate": "PASS" if radar["structure_gate"] else "WAIT",
                "structure_gate_label": structure_gate_label,
                "reading": radar_reading,
                "action": radar["next_step"],
            },
            "radar_decision_code": radar["code"],
            "radar_decision_label": radar_label,
            "radar_decision_text": radar["status"],
            "radar_money_state": money_label,
            "radar_price_gate": "PASS" if radar["price_gate"] else "WAIT",
            "radar_price_gate_label": price_gate_label,
            "radar_structure_gate": "PASS" if radar["structure_gate"] else "WAIT",
            "radar_structure_gate_label": structure_gate_label,
            "radar_action": radar["next_step"],
            "radar_intraday_reading": radar_reading,
            "home_decision": {
                "code": radar["code"],
                "label": radar_label,
                "decision": radar["status"],
                "money_state": money_label,
                "price_gate": "PASS" if radar["price_gate"] else "WAIT",
                "price_gate_label": price_gate_label,
                "structure_gate": "PASS" if radar["structure_gate"] else "WAIT",
                "structure_gate_label": structure_gate_label,
                "reading": radar_reading,
                "action": radar["next_step"],
            },
            "home_decision_code": radar["code"],
            "home_decision_label": radar_label,
            "home_decision_text": radar["status"],
            "home_money_state": money_label,
            "home_price_gate": "PASS" if radar["price_gate"] else "WAIT",
            "home_price_gate_label": price_gate_label,
            "home_structure_gate": "PASS" if radar["structure_gate"] else "WAIT",
            "home_structure_gate_label": structure_gate_label,
            "home_action": radar["next_step"],
            "home_intraday_reading": radar_reading,
        })
    return rows


def _trade_date():
    return datetime.now(TW_TZ).date().isoformat()


def _stamp_snapshot_identity(result):
    if not isinstance(result, dict):
        return _stamp_snapshot_identity(result)
    snapshot_time = result.get("updated_at") or datetime.now(TW_TZ).isoformat(timespec="seconds")
    snapshot_id = f"{result.get('trade_date') or _trade_date()}:{snapshot_time}"
    result.setdefault("snapshot_id", snapshot_id)
    result.setdefault("snapshot_time", snapshot_time)
    result.setdefault("source_table", SOURCE_TABLE)
    result.setdefault("source_version", SOURCE_VERSION)
    for row in result.get("rows") or []:
        if isinstance(row, dict):
            row.setdefault("snapshot_id", snapshot_id)
            row.setdefault("snapshot_time", snapshot_time)
            row.setdefault("source_table", SOURCE_TABLE)
            row.setdefault("source_version", SOURCE_VERSION)
    return result


def _intraday_session_open():
    """頁面請求的盤中閘門；盤後不得觸碰 Shioaji quote buffer。"""
    now = datetime.now(TW_TZ)
    return now.weekday() < 5 and 9 <= now.hour * 60 + now.minute <= 13 * 60 + 35


def _read_intraday_snapshot(allow_prev_day=False):
    """讀取最後一筆 VPS 快照。

    預設只回今日資料；allow_prev_day=True 時，今日尚無資料則回退
    最近一次快照（標明 data_date），確保盤前／清晨永遠有最新可用數據
    而不是空白。"""
    try:
        if not INTRADAY_SNAPSHOT_PATH.exists():
            return None
        payload = json.loads(INTRADAY_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        result = payload.get("result")
        if not (isinstance(result, dict) and result.get("rows")):
            return None
        snap_date = payload.get("trade_date")
        if snap_date != _trade_date():
            if not allow_prev_day:
                return None
            result = dict(result)
            result["data_date"] = snap_date
            result["prev_day"] = True
            result.setdefault("notes", []).append(
                f"今日盤中資料尚未累積，顯示最近一次盤中快照（{snap_date}）")
        return result
    except Exception as exc:
        print(f"[snapshot] 讀取失敗: {exc}", flush=True)
        return None


def _snapshot_quality(result):
    """快照品質：有 aflow 值的檔數。用來擋「降級覆蓋」。"""
    rows = (result or {}).get("rows") or []
    return sum(1 for r in rows if r.get("aflow") is not None)


def _intraday_money_nature(change, aflow, volume_ratio, price, vwap, ma20, low, high):
    """把盤中價、A-flow、量/承接翻成首頁最高層的資金性質。"""
    if change is None or aflow is None:
        return {
            "code": "PENDING",
            "label": "🟡 尚未確認",
            "tone": "neutral",
            "reason": "價格或 A-flow 尚未完整，先不判斷資金性質。",
        }

    above_vwap = vwap is not None and price is not None and price >= vwap
    above_ma20 = ma20 is not None and price is not None and price >= float(ma20)
    # 盤中判讀優先 VWAP。VWAP 已知卻跌破時，不可再用 MA20 把日內承接救成
    # 「健康換手」；MA20 只在 VWAP 缺值時作 fallback。
    key_held = bool(above_vwap if vwap is not None else above_ma20)
    rebound = bool(low is not None and price is not None and low > 0 and
                   (price - low) / low >= 0.018)
    pullback_from_high = bool(high is not None and price is not None and high > 0 and
                              (high - price) / high >= 0.01)
    volume_active = volume_ratio is not None and volume_ratio >= 1.2

    if change > 0 and aflow < 0 and (volume_active or pullback_from_high or not key_held):
        return {
            "code": "FAKE_RED_DISTRIBUTION",
            "label": "⚠️ 假紅誘高",
            "tone": "risk",
            "reason": "價格在紅盤但主動資金流出，若又放量或站不穩關鍵價，視為誘高風險。",
        }
    if change < 0 and aflow < 0 and not key_held:
        return {
            "code": "CAPITAL_EXIT",
            "label": "🔻 資金撤退",
            "tone": "risk",
            "reason": "價格走弱、A-flow 為負且失守 VWAP/MA20，資金撤退優先處理。",
        }
    if change > 0 and aflow > 0 and key_held and volume_active:
        return {
            "code": "TRUE_MOMENTUM",
            "label": "🔥 真資金推升",
            "tone": "bull",
            "reason": "價格上漲、主動買盤為正，且量能與 VWAP/關鍵價同時守住。",
        }
    if (aflow > 0 and key_held and
            (rebound or abs(change) <= 1.5 or volume_active)):
        return {
            "code": "HEALTHY_ROTATION",
            "label": "🔄 健康換手／有人承接",
            "tone": "watch",
            "reason": "賣壓出現後價格沒有被打下去，A-flow 轉正且守住 VWAP/關鍵價。",
        }
    if change < 0 and aflow > 0 and (rebound or key_held):
        return {
            "code": "DIP_ABSORPTION",
            "label": "🔄 健康換手／有人承接",
            "tone": "watch",
            "reason": "價格下跌但主動買盤承接，先看能否止跌並重新站回關鍵價。",
        }
    return {
        "code": "PENDING",
        "label": "🟡 尚未確認",
        "tone": "neutral",
        "reason": "價格、資金與承接尚未同向，等待下一筆量價確認。",
    }


def _intraday_money_evidence(change, aflow, volume_ratio, price, vwap, ma20):
    parts = []
    parts.append("價格 —" if change is None else f"價格 {change:+.2f}%")
    parts.append("A-flow —" if aflow is None else f"A-flow {aflow:+,.0f} 張")
    parts.append("量比 —" if volume_ratio is None or volume_ratio <= 0
                 else f"量比 {volume_ratio:.2f}x")
    if price is not None and vwap is not None and vwap > 0:
        parts.append("站上 VWAP" if price >= vwap else "跌破 VWAP")
    elif price is not None and ma20 is not None and float(ma20) > 0:
        parts.append("站上 MA20" if price >= float(ma20) else "跌破 MA20")
    else:
        parts.append("VWAP/MA20 —")
    return "｜".join(parts)


def _money_flow_100m(aflow, price):
    """A-flow 張數換算成估算資金流（億元）。"""
    if aflow is None or price is None:
        return None
    return round(float(aflow) * float(price) * 1000 / 100000000, 2)


def _fmt_price(value):
    if value is None:
        return "—"
    number = float(value)
    return f"{number:.1f}" if number < 1000 else f"{number:.0f}"


def _pct_gap(price, ref):
    if price is None or ref is None or float(ref) == 0:
        return None
    return (float(price) / float(ref) - 1) * 100


def _price_gate_label(price, vwap, ma20):
    if price is not None and vwap is not None and vwap > 0:
        return (("PASS", f"VWAP 上 {_fmt_price(vwap)}") if price >= vwap
                else ("FAIL", f"VWAP 下 {_fmt_price(vwap)}"))
    if price is not None and ma20 is not None and float(ma20) > 0:
        return (("PASS", f"MA20 上 {_fmt_price(ma20)}") if price >= float(ma20)
                else ("FAIL", f"MA20 下 {_fmt_price(ma20)}"))
    return ("NO_DATA", "價格位置缺資料")


def _home_decision(change, aflow, volume_ratio, price, vwap, ma20, money_nature,
                   data_missing, risk_gate, extreme_up, core_entry,
                   chip_bearish, entry_missing, pct):
    """首頁交易決策層：只回答「現在做什麼」。

    這層刻意和機會雷達分開。雷達可以說某檔正在變強；首頁只有在
    價格、資金、結構、風險都確認後，才允許顯示「可進場」。
    """
    price_gate, price_gate_label = _price_gate_label(price, vwap, ma20)
    structure_gate = "PASS" if price_gate == "PASS" else ("NO_DATA" if price_gate == "NO_DATA" else "WAIT")
    structure_label = {
        "PASS": "突破／站穩",
        "WAIT": "尚未確認",
        "NO_DATA": "結構缺資料",
    }[structure_gate]

    money_code = money_nature.get("code")
    money_state = {
        "TRUE_MOMENTUM": "攻擊 ↑",
        "HEALTHY_ROTATION": "承接 ↑",
        "DIP_ABSORPTION": "承接 ↑",
        "FAKE_RED_DISTRIBUTION": "假訊號 ⚠",
        "CAPITAL_EXIT": "轉弱 ↓",
    }.get(money_code, "未確認")

    def pack(code, label, decision, reading, action):
        return {
            "code": code,
            "label": label,
            "decision": decision,
            "money_state": money_state,
            "price_gate": price_gate,
            "price_gate_label": price_gate_label,
            "structure_gate": structure_gate,
            "structure_gate_label": structure_label,
            "reading": reading,
            "action": action,
        }

    if risk_gate or money_code in ("FAKE_RED_DISTRIBUTION", "CAPITAL_EXIT"):
        if money_code == "FAKE_RED_DISTRIBUTION":
            return pack("NO_ENTRY", "🔴 不進場", "不進場",
                        "紅盤但主動資金流出，疑似假紅誘高",
                        "不追價，等資金翻正並站回 VWAP")
        return pack("NO_ENTRY", "🔴 不進場", "不進場",
                    "資金或結構轉弱，買點失效",
                    "暫停進場，等止跌與資金修復")

    if change is not None and change < 0 and aflow is not None and aflow > 0:
        absorption_action = ("等止跌＋守住 VWAP，不搶反彈"
                             if price_gate == "PASS"
                             else "等止跌＋收復 VWAP，不搶反彈")
        return pack("ABSORPTION_WATCH", "🟠 承接觀察", "有承接，不等於買點",
                    "價跌但 A-flow 為正，有資金承接",
                    absorption_action)

    entry_ready = bool(money_code == "TRUE_MOMENTUM" and core_entry and
                       not chip_bearish and not entry_missing and
                       pct is not None and pct >= 65)
    if entry_ready and not extreme_up and not (change is not None and change >= 6.0):
        return pack("ENTRY", "🟢 可進場", "可進場",
                    "資金、價格與結構同步確認",
                    "依計畫進場，嚴守停損")

    if entry_ready:
        return pack("WAIT_PULLBACK", "🔵 等回測", "等回測，不追價",
                    "多方成立但漲幅偏高，追價風險上升",
                    "等回測 VWAP／關鍵價後再評估")

    if data_missing:
        missing = "、".join(dict.fromkeys(data_missing))
        return pack("WAIT_CONFIRM", "🟡 等待觀察", "等待觀察",
                    f"訊號不足：{missing}",
                    "等資料補齊後再判定")

    if aflow is not None and aflow > 0:
        if price_gate == "FAIL":
            return pack("WAIT_CONFIRM", "🟡 等待觀察", "等待觀察",
                        "有資金，但價格 Gate 未完成",
                        "站回 VWAP 再判定，不追價")
        return pack("WAIT_CONFIRM", "🟡 等待觀察", "等待觀察",
                    "有資金，但結構尚未形成有效進場點",
                    "等待突破或回測承接確認")

    return pack("WAIT_CONFIRM", "🟡 等待觀察", "等待觀察",
                "價格、資金與結構尚未同步",
                "等待價格／資金／結構同步確認")


def _attach_home_decision_levels(rows):
    """把首頁決策補上可執行價位，避免只寫「站穩／回測」的空語意。"""
    for row in rows:
        home = row.get("home_decision")
        if not isinstance(home, dict):
            continue
        price = _optional_float(row.get("price"))
        vwap = _optional_float(row.get("avg_price"))
        ma20 = _optional_float(row.get("ma20"))
        trigger = _row_optional_float(row, "trigger_price", "entry_ref", "key_price", "base_close")
        ref = vwap if vwap is not None else ma20
        ref_name = "VWAP" if vwap is not None else ("MA20" if ma20 is not None else "關鍵價")
        if ref is None:
            ref = trigger
            ref_name = "關鍵價"
        gap = _pct_gap(price, ref)
        gap_text = "" if gap is None else f"，目前距離 {gap:+.1f}%"
        code = home.get("code")

        if row.get("home_money_flow_label"):
            home["money_flow_label"] = row["home_money_flow_label"]
        if ref is not None:
            home["decision_level"] = round(float(ref), 2)
            row["home_decision_level"] = round(float(ref), 2)
            row["home_decision_level_label"] = f"{ref_name} {_fmt_price(ref)}"

        if code == "WAIT_CONFIRM" and ref is not None:
            if home.get("price_gate") == "FAIL":
                home["reading"] = f"有資金，但價格還在 {ref_name} {_fmt_price(ref)} 下方"
                home["action"] = f"站回 {ref_name} {_fmt_price(ref)} 後再判定{gap_text}；不追價"
                home["structure_gate_label"] = f"尚未站回 {_fmt_price(ref)}"
            else:
                level = f"{ref_name} {_fmt_price(ref)}" if ref is not None else "關鍵價"
                home["reading"] = f"有資金，但還缺有效買點確認"
                home["action"] = f"等突破或回測 {level} 有承接再看{gap_text}"
        elif code == "ABSORPTION_WATCH" and ref is not None:
            if home.get("price_gate") == "PASS":
                home["action"] = f"守住 {ref_name} {_fmt_price(ref)} 並止跌；未連續站穩不搶"
                home["structure_gate_label"] = f"守住 {_fmt_price(ref)}"
            else:
                home["action"] = f"收復 {ref_name} {_fmt_price(ref)} 再看；未站回不搶"
                home["structure_gate_label"] = f"尚未站回 {_fmt_price(ref)}"
            home["reading"] = "價跌但資金流入，先看承接是否守得住"
        elif code == "WAIT_PULLBACK":
            pullback = trigger if trigger is not None else ref
            pullback_name = "回測價" if trigger is not None else ref_name
            if pullback is not None:
                gap = _pct_gap(price, pullback)
                gap_text = "" if gap is None else f"，目前距離 {gap:+.1f}%"
                home["decision_level"] = round(float(pullback), 2)
                row["home_decision_level"] = round(float(pullback), 2)
                row["home_decision_level_label"] = f"{pullback_name} {_fmt_price(pullback)}"
                home["reading"] = f"方向成立，但現價離 {pullback_name} {_fmt_price(pullback)} 太遠"
                home["action"] = f"等回測 {pullback_name} {_fmt_price(pullback)} 附近有承接再看{gap_text}"
        elif code == "ENTRY" and ref is not None:
            home["action"] = f"可依計畫進場；跌破 {ref_name} {_fmt_price(ref)} 轉弱"

        for key in ("reading", "action", "price_gate_label", "structure_gate_label"):
            row_key = {
                "reading": "home_intraday_reading",
                "action": "home_action",
                "price_gate_label": "home_price_gate_label",
                "structure_gate_label": "home_structure_gate_label",
            }[key]
            if key == "reading":
                row[row_key] = f"{home.get('label')}｜{home.get(key)}"
            elif home.get(key):
                row[row_key] = home[key]
        row["home_decision"] = home
    return rows


def _write_intraday_snapshot(result):
    """原子保存最後一筆有效盤中結果，供收盤後 API 直接回傳。

    ⚠ 只進不退：非交易日／行情未開時，本輪 result 的 aflow 會整片是 None。
    直接覆蓋會把上一個交易日「有 aflow」的那份洗掉 —— 實測 2026-08-22（週六）
    就是這樣讓畫面上 51 檔 aflow 全變「—」，而 DB 裡 8/21 的 51 筆 net_active
    其實好好的。同一條「只進不退」原則之前只做在 intraday_stock_daily，
    這個快照檔漏掉了。
    """
    try:
        new_q = _snapshot_quality(result)
        if new_q == 0 and INTRADAY_SNAPSHOT_PATH.exists():
            try:
                old_payload = json.loads(INTRADAY_SNAPSHOT_PATH.read_text(encoding="utf-8"))
                if _snapshot_quality(old_payload.get("result")) > 0:
                    print("[snapshot] 本輪 aflow 全空，保留上一份有效快照，不覆蓋",
                          flush=True)
                    return
            except Exception:
                pass
        result = _stamp_snapshot_identity(dict(result))
        payload = {"trade_date": _trade_date(), "saved_at": datetime.now(TW_TZ).isoformat(),
                   "result": result}
        tmp = INTRADAY_SNAPSHOT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str, ensure_ascii=False), encoding="utf-8")
        tmp.replace(INTRADAY_SNAPSHOT_PATH)
    except Exception as exc:
        print(f"[snapshot] 寫入失敗: {exc}", flush=True)


def _last_server_intraday_snapshots():
    """收盤後從主服務保留的最後盤中 state 補回原始快照。

    這裡只保存盤中資料，不套用盤後篩選；避免 Shioaji buffer 清空後，
    /api/intraday-test 被誤回傳為 0，導致盤後無法接續今日盤中結果。
    """
    try:
        import server
        state = getattr(server, "LIVE_STATE", None) or getattr(server, "_last_full_state", None)
        if not isinstance(state, dict):
            return []
        for key in ("_snaps", "stocks"):
            rows = state.get(key)
            if isinstance(rows, list) and rows:
                valid = [x for x in rows if isinstance(x, dict) and x.get("code")
                         and x.get("price") is not None]
                if valid:
                    return valid
    except Exception as exc:
        print(f"[snapshot] 主服務最後 state 讀取失敗: {exc}", flush=True)
    return []


_chip_mem = {"mtime": None, "stocks": {}}


def _chip_snapshot(code):
    """只讀盤後快取；盤中不呼叫法人 API。檔案以 mtime 快取在記憶體，
    避免每檔每次輪詢都重讀整份 JSON。"""
    try:
        mtime = CHIP_CACHE.stat().st_mtime
        if _chip_mem["mtime"] != mtime:
            payload = json.loads(CHIP_CACHE.read_text(encoding="utf-8"))
            _chip_mem["stocks"] = payload.get("stocks") or {}
            _chip_mem["mtime"] = mtime
        return _chip_mem["stocks"].get(str(code)) or {}
    except Exception:
        return {}


def _norm(value, lo, hi):
    if value is None or hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (float(value) - lo) / (hi - lo)))


def _seven_factor_score(raw, ma20, chip):
    """用「能否買」的四層 Gate 分類，不再把未觸發失效誤當成可進場。

    `group` 保留舊的三值（可操作／觀察／排除）供盤後驗證相容；
    `decision_status` 是新的決策語意，首頁與雷達應以它顯示：
    可進場／等待確認／資料不足／風險警報。
    """
    def number(value):
        return _optional_float(value)

    price = number(raw.get("price"))
    change = number(raw.get("change_rate"))
    volume = number(raw.get("total_volume"))
    vwap = number(raw.get("avg_price"))
    low = number(raw.get("low"))
    high = number(raw.get("high"))
    aflow_unavailable = bool(raw.get("_aflow_unavailable"))
    if aflow_unavailable:
        aflow = None
    elif raw.get("aflow") is not None:
        aflow = number(raw.get("aflow"))
    elif raw.get("buy_volume") is not None or raw.get("sell_volume") is not None:
        aflow = (number(raw.get("buy_volume")) or 0) - (number(raw.get("sell_volume")) or 0)
    else:
        aflow = None

    points = 0.0
    factors = {}
    score_missing = []
    evidence = []
    against = []

    for key in ("money_health", "absorption"):
        factors[key] = {"points": None, "max": FACTOR_WEIGHTS[key], "status": "盤後驗證"}

    ratio = (aflow / volume) if aflow is not None and volume and volume > 0 else None
    if ratio is not None:
        p_na = FACTOR_WEIGHTS["net_active"] * _norm(ratio, 0.0, 0.08)
        points += p_na
        factors["net_active"] = {
            "points": round(p_na, 1), "max": 22, "status": "已接入",
            "detail": f"主動買賣差 {aflow:+,.0f} 張，佔成交量 {ratio*100:+.1f}%",
        }
        if ratio >= 0.03:
            evidence.append(f"主動買超佔量 {ratio*100:.1f}%")
        elif ratio <= -0.03:
            against.append(f"主動賣超佔量 {abs(ratio)*100:.1f}%")
    else:
        factors["net_active"] = {"points": None, "max": 22, "status": "缺資料"}
        score_missing.append("主動買賣差")

    if price is not None and ma20 is not None and float(ma20) > 0:
        dev = (price - float(ma20)) / float(ma20)
        p_ma = FACTOR_WEIGHTS["vs_ma20"] if dev >= 0 else FACTOR_WEIGHTS["vs_ma20"] * max(0.0, 1 + dev / 0.05) * 0.5
        points += p_ma
        factors["vs_ma20"] = {
            "points": round(p_ma, 1), "max": 12, "status": "已接入",
            "detail": f"現價 {price:g} vs MA20 {float(ma20):g}（乖離 {dev*100:+.1f}%）",
        }
        (evidence if dev >= 0 else against).append(
            f"站上月線（高於 MA20 {dev*100:.1f}%）" if dev >= 0
            else f"跌破月線 {abs(dev)*100:.1f}%")
    else:
        factors["vs_ma20"] = {"points": None, "max": 12, "status": "缺資料"}
        score_missing.append("MA20")

    streak = number(chip.get("inst_streak"))
    if streak is not None:
        p_st = FACTOR_WEIGHTS["inst_streak"] * _norm(streak, 0, 5)
        points += p_st
        if streak >= 2:
            detail = f"法人連買 {int(streak)} 日"
        elif streak <= -2:
            detail = f"法人連賣 {abs(int(streak))} 日"
        elif streak > 0:
            detail = "法人今日買超"
        elif streak < 0:
            detail = "法人今日賣超"
        else:
            detail = "法人中性"
        factors["inst_streak"] = {
            "points": round(p_st, 1), "max": 10, "status": "已接入", "detail": detail,
        }
        if streak >= 3:
            evidence.append(f"法人連買 {int(streak)} 日")
        elif streak <= -3:
            against.append(f"法人連賣 {abs(int(streak))} 日")
    else:
        factors["inst_streak"] = {"points": None, "max": 10, "status": "籌碼快取重建中"}

    factors["margin"] = {"points": None, "max": 8, "status": "盤後驗證"}
    avail = sum(v["max"] for v in factors.values() if v["points"] is not None)
    pct = (points / avail * 100) if avail else None

    extreme_up = price is not None and is_limit_up(price, change_rate=change or 0)
    extreme_down = change is not None and change <= -9.0
    above_vwap = vwap is not None and price is not None and price >= vwap
    above_ma20 = ma20 is not None and price is not None and price >= float(ma20)
    structure_known = (vwap is not None and vwap > 0) or (ma20 is not None and float(ma20) > 0)
    # 盤中首頁優先 VWAP。VWAP 已知且失守時，MA20 只能當背景支撐，
    # 不能把「跌破 VWAP」救成進場結構成立。
    structure_confirmed = bool(above_vwap if vwap is not None else above_ma20)
    rebound = bool(low is not None and price is not None and low > 0 and
                   (price - low) / low >= 0.02)
    price_gate = bool(change is not None and (change > 0 or rebound))
    flow_gate = bool(aflow is not None and aflow > 0)

    # 資料不足不是弱勢的另一種說法：aflow<0 是 FAIL，只有訊號來源少於 3
    # 個才是 NO_DATA。BS 這裡使用 broker 已正規化的主動買／賣量來源；不把
    # 「0」量比當成有效訊號（Shioaji TickSTKv1 常以 0 表示未提供）。
    volume_ratio = number(raw.get("volume_ratio"))
    signal_sources = {
        "aflow": aflow is not None,
        "法人籌碼": streak is not None,
        "BS主動買賣盤": (not aflow_unavailable and
                         raw.get("buy_volume") is not None and
                         raw.get("sell_volume") is not None),
        "量比": volume_ratio is not None and volume_ratio > 0,
    }
    signal_count = sum(signal_sources.values())
    data_missing = []
    if signal_count < 3:
        data_missing.extend(label for label, available in signal_sources.items()
                            if not available)
    if price is None:
        data_missing.append("現價")
    if change is None:
        data_missing.append("漲跌幅")

    chip_streak = streak
    chip_net20 = number(chip.get("inst_net_20d_lots"))
    chip_bearish = ((chip_streak is not None and chip_streak <= -3) or
                    (chip_net20 is not None and chip_net20 <= -3000))

    if change is not None:
        if change > 0:
            evidence.append(f"股價上漲 {change:+.2f}%")
        elif change < 0:
            against.append(f"股價下跌 {change:+.2f}%")
    if aflow is not None and aflow < 0:
        against.append(f"主動資金流出 {abs(aflow):,.0f} 張")

    money_nature = _intraday_money_nature(
        change, aflow, volume_ratio, price, vwap, ma20, low, high)
    money_flow_100m = _money_flow_100m(aflow, price)
    money_evidence = _intraday_money_evidence(
        change, aflow, volume_ratio, price, vwap, ma20)

    # 風險 Gate：已知價弱＋資金流出直接進風險，不再等待「四重失效」。
    hard_selloff = bool(change is not None and aflow is not None and
                        change <= -4 and aflow < 0)
    deep_drop = bool(change is not None and change <= -7 and
                     not (rebound and flow_gate and structure_confirmed))
    broken_structure = bool(change is not None and aflow is not None and
                            change < 0 and aflow < 0 and not structure_confirmed)
    risk_gate = bool(hard_selloff or deep_drop or extreme_down or broken_structure)

    core_entry = bool(price_gate and flow_gate and structure_confirmed)
    # VWAP／MA20 尚未回傳是「進場條件未確認」，不是資料完整度不足；
    # 資料不足只由可用盤中訊號少於 3 個（或連現價／漲跌幅都沒有）觸發。
    entry_missing = list(data_missing)
    if not structure_known and "VWAP／MA20位置" not in entry_missing:
        entry_missing.append("VWAP／MA20位置")

    # 候選池生命週期與今日是否能買是兩條獨立軌。原始結構淘汰欄位若尚未
    # 由上游提供，預設保留觀察；不能把 D 級盤中弱勢直接當成淘汰。
    structural_failures = list(raw.get("structural_failures") or [])
    candidate_lifecycle = "淘汰" if len(structural_failures) >= 2 else "保留觀察"
    risk_layer = "E" if len(structural_failures) >= 2 else ("D" if risk_gate else None)

    # 已知的「價弱＋資金流出」優先標風險；不能因為其他訊號缺失，把
    # aflow<0 翻譯成「資料不足」。只有沒有足夠證據、也沒有明確風險時才 NO_DATA。
    if risk_gate:
        decision_status = "風險警報"
        group = "排除" if candidate_lifecycle == "淘汰" else "觀察"
        subgroup = "🔴 風險警報"
        details = []
        if change is not None and change <= -4:
            details.append(f"跌幅 {change:+.2f}%")
        if aflow is not None and aflow < 0:
            details.append(f"主動資金 {aflow:+,.0f} 張")
        if not structure_confirmed:
            details.append("價格未站回 VWAP／MA20")
        lifecycle_note = ("結構尚未達淘汰門檻，仍保留觀察"
                          if candidate_lifecycle != "淘汰" else "結構失效，淘汰")
        reason = (f"風險警報（{risk_layer or 'D'}級）｜" + "、".join(details) +
                  f"｜價格偏弱且資金流出，沒有盤中止跌或資金翻正證據，暫不進場。｜{lifecycle_note}。")
    elif data_missing:
        decision_status = "資料不足"
        group, subgroup = "觀察", "🟠 資料不足"
        reason = ("資料不足｜缺少：" + "、".join(dict.fromkeys(data_missing)) +
                  "｜可用訊號少於 3 個，不下判斷，不進場。")
    elif extreme_up:
        decision_status = "等待確認"
        group, subgroup = "觀察", "⏳ 強勢但不追"
        reason = "等待確認｜漲停／極端漲幅，禁止追價；等回測或新的承接確認。"
    elif (money_nature["code"] == "TRUE_MOMENTUM" and core_entry and
          not chip_bearish and not entry_missing and pct is not None and pct >= 65):
        decision_status = "可進場"
        group, subgroup = "可操作", "🟢 可進場"
        reason = ("可進場｜價格、主動資金、結構三項同時確認" +
                  (f"｜盤中分數 {pct:.0f}%" if pct is not None else "") + "。")
    else:
        decision_status = "等待確認"
        group, subgroup = "觀察", "🟡 等待確認"
        wait_for = []
        if not price_gate:
            wait_for.append("價格轉強")
        if not flow_gate:
            wait_for.append("主動資金翻正")
        if not structure_confirmed:
            wait_for.append("站回 VWAP／MA20")
        if chip_bearish:
            wait_for.append("法人籌碼改善")
        reason = "等待確認｜尚未通過多方進場條件：" + "、".join(wait_for or ["突破／承接確認"]) + "｜不買。"

    home_decision = _home_decision(
        change, aflow, volume_ratio, price, vwap, ma20, money_nature,
        data_missing, risk_gate, extreme_up, core_entry, chip_bearish,
        entry_missing, pct)
    home_reading = f"{home_decision['label']}｜{home_decision['reading']}"

    return {
        "score": round(points, 1), "score_max": 100,
        "score_pct": round(pct, 1) if pct is not None else None,
        "score_available": round(avail, 1),
        "score_rule": "ENTRY IS EARNED：可進場必須同時通過價格、資金、結構三個核心 Gate；未通過不預設為買點",
        "factors": factors, "score_missing": score_missing,
        "evidence": evidence, "against": against,
        "group": group, "subgroup": subgroup, "reason": reason,
        "classification": subgroup,
        "decision_status": decision_status,
        "decision_code": {"可進場": "ENTRY", "等待確認": "WAIT", "資料不足": "DATA", "風險警報": "RISK"}[decision_status],
        "decision_can_buy": decision_status == "可進場",
        "decision_missing": list(dict.fromkeys(entry_missing if decision_status == "等待確認" else data_missing)),
        "decision_signal_count": signal_count,
        "decision_signal_sources": signal_sources,
        "risk_layer": risk_layer,
        "structural_failures": structural_failures,
        "candidate_lifecycle": candidate_lifecycle,
        "candidate_retained": candidate_lifecycle != "淘汰",
        "decision_gates": {
            "data_integrity": "PASS" if not data_missing else "NO_DATA",
            "risk": "FAIL" if risk_gate else "PASS",
            "price": "PASS" if price_gate else "FAIL",
            "money": "PASS" if flow_gate else "FAIL",
            "structure": "PASS" if structure_confirmed else "FAIL",
        },
        "home_decision": home_decision,
        "home_decision_code": home_decision["code"],
        "home_decision_label": home_decision["label"],
        "home_decision_text": home_decision["decision"],
        "home_money_state": home_decision["money_state"],
        "home_money_flow_100m": money_flow_100m,
        "home_money_flow_label": ("—" if money_flow_100m is None
                                  else f"{money_flow_100m:+.2f} 億"),
        "home_price_gate": home_decision["price_gate"],
        "home_price_gate_label": home_decision["price_gate_label"],
        "home_structure_gate": home_decision["structure_gate"],
        "home_structure_gate_label": home_decision["structure_gate_label"],
        "home_action": home_decision["action"],
        "intraday_money_nature": money_nature,
        "intraday_money_nature_label": money_nature["label"],
        "intraday_money_nature_code": money_nature["code"],
        "intraday_money_nature_reason": money_nature["reason"],
        "intraday_money_evidence": money_evidence,
        "home_intraday_reading": home_reading,
        "potential_grade": "A" if decision_status == "可進場" else "B",
        "entry_status": "可進場" if decision_status == "可進場" else ("暫不進場" if decision_status == "風險警報" else "等待確認"),
        "is_limit_up": extreme_up,
    }


@router.get("/intraday-test/daily-report", response_class=HTMLResponse)
def daily_report_page():
    """顯示指定的 0722 每日報告 UI；報告頁內的 API 仍走同一台 VPS。"""
    # 檔案在 repo 根＝BASE(vps_intraday_test.py 同層)。原本誤用 ROOT=BASE.parent
    # (=/opt)，讀不到 → 500。找不到時回友善提示，不再吐 Internal Server Error。
    report = BASE / "每日報告 0722.html"
    if not report.exists():
        return HTMLResponse(
            f"<div style='padding:24px;font-family:sans-serif;color:#73809a'>"
            f"每日報告尚未產出（找不到 {report.name}）。</div>",
            status_code=200, headers={"Cache-Control": "no-store, max-age=0"})
    return HTMLResponse(report.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store, max-age=0"})


def _eod_module():
    """支援本機 mls_intraday 與 VPS app 兩種套件路徑。"""
    try:
        from mls_intraday import eod_stamp
    except ImportError:
        from app import eod_stamp
    return eod_stamp


def _history_ready(eod_stamp):
    """空的獨立 DB 也要能正常回傳空歷史，不得讓 API 500。"""
    if not HISTORY_DB.exists():
        return False
    import sqlite3
    with sqlite3.connect(HISTORY_DB) as conn:
        eod_stamp.ensure_table(conn)
    return True


def _row(raw):
    code = str(raw.get("code", ""))
    buy = int(raw.get("buy_volume") or 0)
    sell = int(raw.get("sell_volume") or 0)
    # broker 已把 buy/sell 正規化成 active_buy/active_sell；核心公式仍吃 raw bid/ask。
    aflow = F.aflow_official(sell, buy)
    # 行情層死、價量走 MIS 備援時：aflow 只信 Shioaji，一律標停用,絕不用五檔偽裝。
    aflow_unavail = bool(raw.get("_aflow_unavailable"))
    aflow_out = None if aflow_unavail else aflow
    change = float(raw.get("change_rate") or 0)
    price = float(raw.get("price") or 0)
    ma20 = None
    ma20_status = {}
    try:
        import server
        ma20 = server.get_ma20(code)
        ma20_status = server.ma20_cache_status()
    except Exception:
        pass
    snap = F.StockSnap(
        code=code,
        track="engine" if code in getattr(config, "ENGINE_STOCKS", set()) else "attack",
        price=price,
        change_rate=change,
        aflow=aflow,
        total_volume=int(raw.get("total_volume") or 0),
        ma20=ma20,
    )
    filters = F.passes_filters(snap, regime=_current_regime())
    seven = _seven_factor_score(raw, ma20, _chip_snapshot(code))
    classification = {
        "group": seven["group"], "subgroup": seven["subgroup"],
        "reason": seven["reason"], "all_pass": seven["group"] == "可操作",
        "decision_status": seven["decision_status"],
        "candidate_lifecycle": seven["candidate_lifecycle"],
        "extreme": bool(seven.get("is_limit_up")) or change <= -9.0,
    }
    # 白話判語跟著實際分類 group 走：沒進「可操作」就不會被說成真攻擊/強惜售。
    explanation = ai_explain.local_explain(snap, regime=_current_regime(),
                                           group=seven["group"])
    _sec = getattr(config, "SECTOR_MAP", {}).get(code)
    return {
        "code": code,
        "name": getattr(config, "NAME_MAP", {}).get(code, code),
        "sector": _sec[0] if _sec else "其他",
        "track": _sec[1] if _sec and len(_sec) > 1 else "attack",
        "price": price,
        # 引擎／候選池提供的原定關鍵價與進場型態，供雷達判斷「現價是否仍能買」；
        # 缺值就明示待確認，不用今天的漲跌幅反推一個虛構買點。
        "trigger_price": raw.get("trigger_price"),
        "entry_ref": raw.get("entry_ref"),
        "signal_kind": raw.get("signal_kind", raw.get("entry_kind")),
        "entry_kind": raw.get("entry_kind"),
        "entry_rule": raw.get("entry_rule"),
        # 盤中最高：供「今日觸發」三態燈判定「曾觸及後回落」，不影響其他欄位
        "high": (float(raw.get("high")) or None) if raw.get("high") else None,
        # 盤中最低＋均價(VWAP)：Shioaji tick/snapshot 原生欄位(avg_price/average_price),
        # 是交易所口徑的真實成交量加權均價,不是前端算出來的近似值 —— 供「距買點」
        # 「日內位置」「VWAP乖離」判讀用,不得省略成 None 硬編。
        "low": (float(raw.get("low")) or None) if raw.get("low") else None,
        "avg_price": (float(raw.get("avg_price")) or None) if raw.get("avg_price") else None,
        "change_rate": round(change, 2),
        "is_limit_up": bool(seven.get("is_limit_up")),
        "potential_grade": seven.get("potential_grade"),
        "entry_status": seven.get("entry_status"),
        "buy_volume": None if aflow_unavail else buy,
        "sell_volume": None if aflow_unavail else sell,
        "tick_type": raw.get("tick_type"),
        # buy=主動買(=bid_side)、sell=主動賣(=ask_side)；raw_* 顯示回真實 bid/ask 側量
        "raw_bid_side_total_vol": None if aflow_unavail else buy,
        "raw_ask_side_total_vol": None if aflow_unavail else sell,
        "aflow": aflow_out,
        "quadrant": F.proxy_quadrant(aflow_out if aflow_out is not None else 0, change),
        # 資料品質標記（§6）：讓畫面一眼看出是即時 Shioaji 還是 MIS 備援、aflow 是否停用
        "price_source": raw.get("_price_source"),
        "quote_status": raw.get("_quote_status"),
        "aflow_status": "UNAVAILABLE" if aflow_unavail else "LIVE",
        "last_tick_age": raw.get("_last_tick_age"),
        "total_volume": int(raw.get("total_volume") or 0),
        "ma20": ma20,
        "ma20_cache": ma20_status,
        "volume_ratio": raw.get("volume_ratio"),
        # 若行情來源有提供歷史報酬，供機會雷達風險層使用；缺值不補造。
        "return_3d": raw.get("return_3d", raw.get("change_3d", raw.get("price_return_3d"))),
        "filters": filters,
        "classification": classification,
        "group": classification["group"],
        "subgroup": classification["subgroup"],
        "classification_reason": classification["reason"],
        "decision_status": seven["decision_status"],
        "decision_code": seven["decision_code"],
        "decision_can_buy": seven["decision_can_buy"],
        "decision_missing": seven["decision_missing"],
        "decision_signal_count": seven["decision_signal_count"],
        "decision_signal_sources": seven["decision_signal_sources"],
        "risk_layer": seven["risk_layer"],
        "structural_failures": seven["structural_failures"],
        "candidate_lifecycle": seven["candidate_lifecycle"],
        "candidate_retained": seven["candidate_retained"],
        "decision_gates": seven["decision_gates"],
        "home_decision": seven["home_decision"],
        "home_decision_code": seven["home_decision_code"],
        "home_decision_label": seven["home_decision_label"],
        "home_decision_text": seven["home_decision_text"],
        "home_money_state": seven["home_money_state"],
        "home_money_flow_100m": seven["home_money_flow_100m"],
        "home_money_flow_label": seven["home_money_flow_label"],
        "home_price_gate": seven["home_price_gate"],
        "home_price_gate_label": seven["home_price_gate_label"],
        "home_structure_gate": seven["home_structure_gate"],
        "home_structure_gate_label": seven["home_structure_gate_label"],
        "home_action": seven["home_action"],
        "intraday_money_nature": seven["intraday_money_nature"],
        "intraday_money_nature_label": seven["intraday_money_nature_label"],
        "intraday_money_nature_code": seven["intraday_money_nature_code"],
        "intraday_money_nature_reason": seven["intraday_money_nature_reason"],
        "intraday_money_evidence": seven["intraday_money_evidence"],
        "home_intraday_reading": seven["home_intraday_reading"],
        "score": seven["score"],
        "score_max": seven["score_max"],
        "score_available": seven["score_available"],
        "score_rule": seven["score_rule"],
        "score_factors": seven["factors"],
        "score_missing": seven["score_missing"],
        "score_pct": seven["score_pct"],
        "evidence": seven["evidence"],
        "against": seven["against"],
        # 雷達是盤中頁：缺資料欄只顯示當下盤中 filter 的缺口，
        # 不把盤後模組(score_missing)倒灌到盤中。
        "filter_no_data": filters["no_data"],
        "extreme_price": filters["extreme"],
        "ai": explanation,
        "bidask_available": False,
    }


def _current_regime():
    """讀主站同 process 的溫度計，不另開行情連線。"""
    try:
        import server
        score = (server.STATE.get("market") or {}).get("score")
        if score is not None:
            return F.market_regime(int(score))
    except Exception:
        pass
    return F.REGIME_RANGE


def _index_pct(allow_broker=True):
    """加權指數漲跌幅（%）。優先讀同 process 的 state，避免另打行情。"""
    try:
        import server
        for state in (getattr(server, "LIVE_STATE", None), getattr(server, "STATE", None)):
            if isinstance(state, dict):
                val = (state.get("market") or {}).get("index_pct")
                if val is not None:
                    return float(val)
    except Exception:
        pass
    if not allow_broker:
        return None
    try:
        val = (broker.index_snapshot() or {}).get("index_pct")
        return float(val) if val is not None else None
    except Exception:
        return None


# B：盤中即時寬度。EOD(STOCK_DAY_ALL)盤中只有昨收，全市場快照又吃流量；
# 依 Vanessa 指示，改用「已訂閱的 51 檔觀察池」即時 buffer 逐檔數漲跌 —— 這批本來
# 就在訂閱，零額外額度、盤中逐筆更新，資料日＝今天。標為 intraday_pool，明確不是
# 全市場寬度（全市場寬度仍走 EOD，收盤後校準），避免拿 51 檔冒充全市場。
_POOL_MIN_SAMPLE = 20      # 開盤初期 buffer 太少（<20 檔有價）不出手，退回 EOD


def _intraday_pool_breadth(rows):
    """用 51 檔訂閱池即時報價算盤中寬度（今日、非 stale）；樣本不足/失敗回 None。

    rows：與 aflow 同源的即時列，需含 change_rate。"""
    if market_breadth is None or not rows:
        return None
    try:
        import market_regime as _mr
        snaps = [{"change_rate": r.get("change_rate")}
                 for r in rows if r.get("change_rate") is not None]
        ib = _mr.breadth_from_snapshots(snaps)
        if ib and ib.get("total", 0) >= _POOL_MIN_SAMPLE:
            ib["source"] = "intraday_pool"     # 明示：51 檔訂閱池，非全市場
            return ib
    except Exception as exc:
        print(f"[breadth] 盤中池寬度失敗，退回 EOD: {exc}", flush=True)
    return None


def _breadth(rows, live=True):
    """算今日資金廣度；即時來源才落地時間序列（快照／回退不記）。"""
    if market_breadth is None or not rows:
        return None
    try:
        payload = market_breadth.api_payload(
            rows=rows, index_pct=_index_pct(),
            intraday_breadth=_intraday_pool_breadth(rows) if live else None)
        if live and not payload.get("stale"):
            market_breadth.record(payload)
        return payload
    except Exception as exc:
        print(f"[breadth] 計算失敗: {exc}", flush=True)
        return None


@router.get("/api/market-breadth")
def market_breadth_api():
    """市場資金廣度：Risk On/Off、指數 vs 廣度背離、日內與日線時間序列。"""
    if market_breadth is None:
        return {"ok": False, "error": "market_breadth 模組未載入"}
    try:
        if not _intraday_session_open():
            saved = _read_intraday_snapshot(allow_prev_day=True) or {}
            rows = [{"code": r.get("code"), "change_rate": r.get("change_rate"),
                     "aflow": r.get("aflow")} for r in (saved.get("rows") or [])
                    if r.get("aflow") is not None]
            payload = market_breadth.api_payload(
                rows=rows, index_pct=_index_pct(allow_broker=False), intraday_breadth=None)
            payload["session_closed"] = True
            payload["stale"] = True
            return payload
        rows = []
        for item in broker.raw_buffer_snapshots():
            rows.append({"code": str(item.get("code", "")),
                         "change_rate": item.get("change_rate"),
                         "aflow": F.aflow_official(int(item.get("sell_volume") or 0),
                                                   int(item.get("buy_volume") or 0))})
        live = bool(rows)
        if not rows:
            saved = _read_intraday_snapshot(allow_prev_day=True) or {}
            rows = [{"code": r.get("code"), "change_rate": r.get("change_rate"),
                     "aflow": r.get("aflow")}
                    for r in (saved.get("rows") or []) if r.get("aflow") is not None]
        payload = market_breadth.api_payload(
            rows=rows, index_pct=_index_pct(),
            intraday_breadth=_intraday_pool_breadth(rows) if live else None)
        if live and not payload.get("stale"):
            market_breadth.record(payload)
        return payload
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/api/market/live-index")
def market_live_index():
    """加權指數盤中即時（Shioaji TSE001，單一指數合約、5s 記憶體快取）。

    只打 1 檔指數合約、且 broker.index_snapshot 內建 5s TTL — 不影響主迴圈、
    額度可忽略。失敗回 {ok:False}，前端自動退回官方 EOD 值，永不弄壞版面。"""
    if not _intraday_session_open():
        return {"ok": False, "session_closed": True}
    try:
        snap = broker.index_snapshot() or {}
        if snap.get("index") is not None:
            return {"ok": True, "index": snap.get("index"),
                    "index_pct": snap.get("index_pct"),
                    "amount_100m": snap.get("amount_100m"),
                    "asof": datetime.now(TW_TZ).isoformat(timespec="seconds")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": False}


@router.get("/api/intraday-test")
def intraday_test():
    started = time.time()
    print(f"[diag][http] intraday_test.begin ts={time.strftime('%Y-%m-%dT%H:%M:%S%z')}", flush=True)
    try:
        if not _intraday_session_open():
            saved = _read_intraday_snapshot(allow_prev_day=True)
            if saved:
                saved = dict(saved)
                saved["stale"] = True
                saved["snapshot"] = True
                saved["session_closed"] = True
                saved["source"] = "VPS persisted intraday snapshot (session closed)"
                return _with_pre_activation(saved)
            return {"ok": True, "rows": [], "count": 0, "session_closed": True,
                    "source": "VPS session closed; no live fetch"}
        raw = broker.raw_buffer_snapshots()
        # 服務重啟會清空 Shioaji tick buffer，盤中清單會瞬間從 51 檔掉到個位數。
        # 用今日快照補齊尚未回報的檔案（只補不覆蓋），檔數不因重啟而縮水。
        if raw:
            try:
                have = {str(x.get("code")) for x in raw}
                saved = _read_intraday_snapshot() or {}
                for row in (saved.get("rows") or []):
                    code = str(row.get("code") or "")
                    if code and code not in have and row.get("price") is not None:
                        merged = dict(row)
                        merged["stale_row"] = True
                        raw.append(merged)
                        have.add(code)
            except Exception as exc:
                print(f"[snapshot] 合併快照失敗: {exc}", flush=True)
        if not raw:
            saved = _read_intraday_snapshot()
            if saved:
                saved = dict(saved)
                saved["stale"] = True
                saved["snapshot"] = True
                saved["source"] = "VPS persisted intraday snapshot"
                saved.setdefault("notes", []).append("收盤後由 VPS 回傳最後一筆盤中快照，不依賴瀏覽器快取")
                return _with_pre_activation(saved)
            # 首次在收盤後開啟頁面時，API buffer 可能已清空；
            # 改用主服務尚未被盤後篩選覆寫的最後盤中 state。
            raw = _last_server_intraday_snapshots()
            fallback_source = bool(raw)
            if not raw:
                # 今日完全無資料（清晨／假日／服務剛重啟）→ 回最近一次
                # 快照並標明資料日，永遠不回空白。
                saved = _read_intraday_snapshot(allow_prev_day=True)
                if saved:
                    saved = dict(saved)
                    saved["stale"] = True
                    saved["snapshot"] = True
                    saved["source"] = "VPS persisted intraday snapshot (latest)"
                    return _with_pre_activation(saved)
        else:
            fallback_source = False
        regime = _current_regime()
        # 行情健康判定 + MIS 備援疊加：Shioaji 死時價量走 MIS、aflow 標停用（§1–§3）。
        # 只在真正即時 buffer(非快照回退)時判健康,避免對盤後快照誤判/亂抓 MIS。
        health_meta = None
        if quote_health is not None and not fallback_source:
            try:
                raw, health_meta = quote_health.apply(raw)
            except Exception as _e:
                print(f"[intraday-test] quote_health 跳過: {_e}", flush=True)
        rows = [_row(item) for item in raw]
        _pa_date, _pa_n = _attach_pre_activation(rows)
        _attach_home_decision_levels(rows)
        _attach_radar_execution_overlay(rows)
        # 排序契約：可操作→觀察→排除；群內 score_pct→score→漲跌幅→aflow。
        _sort_display_rows(rows)
        category_counts = {}
        decision_category_counts = {key: 0 for key in DECISION_STATUS_ORDER}
        home_decision_counts = {key: 0 for key in HOME_DECISION_ORDER}
        for row in rows:
            category_counts[row["group"]] = category_counts.get(row["group"], 0) + 1
            status = row.get("decision_status")
            if status in decision_category_counts:
                decision_category_counts[status] += 1
            home_status = row.get("home_decision_label")
            if home_status in home_decision_counts:
                home_decision_counts[home_status] += 1
        # usage() 是 Shioaji 網路請求，放在首頁輪詢路徑會拖慢整頁；
        # 這裡只回本地已知的訂閱數與 buffer 大小，額度查詢移到 /api/quota。
        quota = {
            "subscribed": len(getattr(broker, "_SUBSCRIBED", set())),
            "buffer_filled": len(getattr(broker, "_QUOTE_BUF", {})),
        }
        result = {
            "ok": True,
            "source": ("VPS persisted last intraday state" if fallback_source
                        else "VPS Shioaji subscription buffer"),
            "read_only": True,
            "updated_at": datetime.now(TW_TZ).isoformat(timespec="seconds"),
            "trade_date": _trade_date(),
            "count": len(rows),
            "rows": rows,
            "pre_activation_date": _pa_date,
            "pre_activation_count": _pa_n,
            "category_counts": category_counts,
            "decision_category_counts": decision_category_counts,
            "home_decision_counts": home_decision_counts,
            "regime": regime,
            "quota": quota,
            # 資料品質總覽（§3 §6）：feed_state=LIVE/DEGRADED/BACKUP、aflow 是否可用
            "feed_health": health_meta,
            "latency_ms": round((time.time() - started) * 1000, 1),
            "notes": [
                "aflow 使用既有訂閱 buffer 的官方買賣盤累積量",
                "此頁不寫 mls.db、不改主站 STATE",
                "MA20 由盤前快取接入；快取尚未建立時標示無資料，不補造數字",
                f"v4 三態 filter：{regime}；極端價訊號降級 NO_DATA",
            ],
        }
        result["breadth"] = _breadth(rows, live=not fallback_source)
        if fallback_source:
            result["snapshot"] = True
            result["notes"].append("首次收盤後讀取：由主服務保留的最後盤中 state 補存，與盤後篩選分離")
        _stamp_snapshot_identity(result)
        if rows:
            _write_intraday_snapshot(result)
        # 盤後驗證：只記即時 buffer 的分類訊號，快照/回退來源不記，
        # 避免把舊資料當成今日訊號。
        if rows and not fallback_source and review_rules is not None:
            try:
                review_rules.record(rows)
            except Exception as exc:
                print(f"[review_rules] 記錄失敗: {exc}", flush=True)
        print(f"[diag][http] intraday_test.end rows={len(rows)} elapsed_ms={round((time.time()-started)*1000,1)}", flush=True)
        return result
    except Exception as exc:
        print(f"[diag][http] intraday_test.error elapsed_ms={round((time.time()-started)*1000,1)} error={exc!r}", flush=True)
        return {"ok": False, "source": "VPS Shioaji subscription buffer", "error": str(exc)}



# ── Pre-Activation stage 併入(唯讀)────────────────────────────────────
# 盤後算好的四階段存在 AB 引擎的 candidate_pool.payload;盤中這支自己不算,
# 也不該重算 —— 同一份資料在兩個地方算出不同答案是這套踩過最多次的坑。
# 唯讀併入,任何失敗都只是少了 badge,不影響觀察池本體。
ENGINE_DB = os.environ.get("MLS_ENGINE_DB", "/opt/mls-screen/mls.db")


def _attach_pre_activation(rows):
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{ENGINE_DB}?mode=ro", uri=True)
        day = con.execute("SELECT MAX(data_date) FROM candidate_pool").fetchone()[0]
        if not day:
            return None, 0
        pa = {}
        pool_meta = {}
        for (payload,) in con.execute(
                "SELECT payload FROM candidate_pool WHERE data_date=?", (day,)):
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if obj.get("code"):
                pool_meta[str(obj.get("code"))] = obj
            if obj.get("pre_activation"):
                pa[str(obj.get("code"))] = obj["pre_activation"]
        n = 0
        for r in rows:
            v = pa.get(str(r.get("code")))
            meta = pool_meta.get(str(r.get("code"))) or {}
            # 進場區的關鍵價來自池日定案資料；它只描述原定觸發位，
            # 不因為今日上漲就把現價改寫成「可買」。
            for key in ("trigger_price", "entry_ref", "signal_kind", "entry_kind", "entry_rule"):
                if r.get(key) is None and meta.get(key) is not None:
                    r[key] = meta.get(key)
            if v:
                chip = _chip_snapshot(str(r.get("code")))
                # FinMind/官方快取是盤後已完成資料；盤中不打 API，
                # 只把最新快取的外資判讀補回 PA，解掉「外資：—」。
                live_pa = (overlay_foreign_confirmation(v, chip)
                           if overlay_foreign_confirmation else v)
                # candidate_pool 是盤後快照；盤中價格可能已經漲停，但量能
                # 尚未達門檻。把「價格已啟動」疊回快照，量能仍保留原值，
                # 不讓舊的 EARLY 再把使用者送回「等待 ARMED」。
                live_pa = (overlay_live_price_activation(
                    live_pa, is_limit_up=r.get("is_limit_up"),
                    change_rate=r.get("change_rate"))
                           if overlay_live_price_activation else live_pa)
                r["pre_activation"] = live_pa
                r["pre_activation_stage"] = live_pa.get("stage")
                r["foreign_net_d"] = live_pa.get("foreign_net_d")
                r["foreign_net_20d"] = live_pa.get("foreign_net_20d")
                r["foreign_source"] = live_pa.get("foreign_source")
                r["foreign_source_date"] = live_pa.get("foreign_source_date")
                n += 1
        return day, n
    except Exception as exc:
        print(f"[pre_activation] 併入略過(不影響本體): {exc}", flush=True)
        return None, 0


def _with_pre_activation(payload):
    """把 stage 貼到 payload["rows"] 上並記錄快照日。
    intraday-test 有多個快照回退出口,每個出口都要帶,否則首頁會時有時無。"""
    try:
        rows = payload.get("rows") or []
        day, n = _attach_pre_activation(rows)
        _attach_home_decision_levels(rows)
        _attach_radar_execution_overlay(rows)
        _sort_display_rows(rows)
        payload["pre_activation_date"] = day
        payload["pre_activation_count"] = n
    except Exception as exc:
        print(f"[pre_activation] payload 併入略過: {exc}", flush=True)
    return payload


@router.get("/api/intraday-watchpool")
def intraday_watchpool():
    """盤中雷達：固定池全集，僅把即時判讀套到有回報的檔案。"""
    started = time.time()
    try:
        if not _intraday_session_open():
            saved = _read_intraday_snapshot(allow_prev_day=True)
            if saved:
                saved = dict(saved)
                saved["stale"] = True
                saved["snapshot"] = True
                saved["session_closed"] = True
                saved["source"] = "VPS persisted intraday snapshot (session closed)"
                return _with_pre_activation(saved)
            return {"ok": True, "rows": [], "count": 0, "session_closed": True,
                    "source": "VPS session closed; no live fetch"}
        raw_rows = {str(item.get("code", "")): item
                    for item in broker.raw_buffer_snapshots()}
        saved_rows = {}
        saved_updated_at = None
        if not raw_rows:
            saved = _read_intraday_snapshot(allow_prev_day=True)
            if saved:
                saved_rows = {str(item.get("code", "")): item
                              for item in saved.get("rows") or []}
                saved_updated_at = saved.get("updated_at")
        rows = []
        for code in config.UNIVERSE:
            raw = raw_rows.get(str(code))
            if raw is None and str(code) in saved_rows:
                row = dict(saved_rows[str(code)])
                row["has_data"] = True
                rows.append(row)
            elif raw is None:
                _s = getattr(config, "SECTOR_MAP", {}).get(str(code))
                rows.append({
                    "code": str(code),
                    "name": getattr(config, "NAME_MAP", {}).get(str(code), str(code)),
                    "sector": _s[0] if _s else "其他",
                    "price": None,
                    "change_rate": None,
                    "aflow": None,
                    "quadrant": None,
                    "group": "觀察",
                    "subgroup": "等待即時回報",
                    "classification_reason": "固定觀察池成員，等待 Shioaji 回報",
                    "decision_status": "資料不足",
                    "decision_code": "DATA",
                    "decision_can_buy": False,
                    "decision_missing": ["現價", "漲跌幅", "盤中資金流", "BS主動買賣盤", "量比"],
                    "decision_signal_count": 0,
                    "decision_signal_sources": {},
                    "risk_layer": None,
                    "structural_failures": [],
                    "candidate_lifecycle": "保留觀察",
                    "candidate_retained": True,
                    "decision_gates": {"data_integrity": "NO_DATA", "risk": "UNKNOWN",
                                        "price": "UNKNOWN", "money": "UNKNOWN",
                                        "structure": "UNKNOWN"},
                    "ai": "固定觀察池成員，等待即時資料；不影響固定名單。",
                    "filter_no_data": ["即時行情"],
                    "has_data": False,
                })
            else:
                row = _row(raw)
                row["has_data"] = True
                rows.append(row)
        pa_date, pa_n = _attach_pre_activation(rows)
        _attach_home_decision_levels(rows)
        _attach_radar_execution_overlay(rows)
        _sort_display_rows(rows)
        foreign_rows = [r for r in rows if r.get("foreign_source_date")]
        foreign_dates = sorted({r["foreign_source_date"] for r in foreign_rows})
        foreign_sources = sorted({r["foreign_source"] for r in foreign_rows
                                  if r.get("foreign_source")})
        return {
            "ok": True,
            "source": "固定 51 檔觀察池 + VPS Shioaji 盤中觀察邏輯",
            "read_only": True,
            "pre_activation_date": pa_date,
            "pre_activation_count": pa_n,
            "foreign_cache": {
                "covered": len(foreign_rows),
                "total": len(rows),
                "source_dates": foreign_dates,
                "sources": foreign_sources,
                "note": "盤中只讀最新完成交易日的法人快取，不代表今日盤中法人流向",
            },
            "updated_at": saved_updated_at or datetime.now(TW_TZ).isoformat(timespec="seconds"),
            "count": len(rows),
            "rows": rows,
            "live_count": sum(1 for row in rows if row["has_data"]),
            "snapshot": bool(saved_rows),
            "latency_ms": round((time.time() - started) * 1000, 1),
        }
    except Exception as exc:
        return {"ok": False, "source": "固定 51 檔觀察池 + VPS Shioaji 盤中觀察邏輯", "error": str(exc)}


@router.get("/api/review/rules")
def review_rules_api():
    """盤後驗證頁：分類規則命中率（自動版盤後驗證.py）。"""
    if review_rules is None:
        return {"ok": False, "error": "review_rules 模組未載入"}
    try:
        return review_rules.api_payload()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/intraday-test", response_class=HTMLResponse)
def home():
    return RedirectResponse(url="/", status_code=307)
