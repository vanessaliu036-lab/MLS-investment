"""Fetch live VPS data once and generate Reversal Lab evidence JSON + HTML."""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

from .live_bridge import build_live_view, fetch_live_rows

try:
    from navigation import NAV_CSS, nav_html
except ImportError:
    _screen_dir = Path(__file__).resolve().parents[2] / "篩選邏輯"
    if str(_screen_dir) not in sys.path:
        sys.path.insert(0, str(_screen_dir))
    from navigation import NAV_CSS, nav_html


def _fmt(v, digits=2):
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:,.{digits}f}"
    return str(v)


def _signed_fmt(v, digits=2):
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:+,.{digits}f}"
    return str(v)


def _num(v, digits=2, sign=False, direction=None):
    if v is None:
        return "<span class='num neutral'>—</span>"
    try:
        numeric = float(v)
    except (TypeError, ValueError):
        return f"<span class='num neutral'>{html.escape(str(v))}</span>"
    tone = direction or ("rise" if numeric > 0 else "fall" if numeric < 0 else "neutral")
    value = _signed_fmt(numeric, digits) if sign else _fmt(numeric, digits)
    return f"<span class='num {tone}'>{value}</span>"


_ZH = {
    "A_FLOW_POSITIVE": "資金流入",
    "A_FLOW_NEGATIVE": "資金流出",
    "A_FLOW_NO_DATA": "無資金流資料",
    "A_FLOW_FLIPPED": "資金流向翻正",
    "PRIOR_OUTFLOW": "前期籌碼流出",
    "PRIOR_CHIPS_POSITIVE": "前期籌碼偏多",
    "PRIOR_CHIPS_NONPOSITIVE": "前期籌碼未偏多",
    "PRICE_CONFIRMATION_CONFIRMED": "股價確認",
    "PRICE_CONFIRMATION_WEAKENED": "股價確認轉弱",
    "PRICE_CONFIRMATION_FAILED": "股價未確認",
    "PRICE_REVERSED": "股價反轉",
    "PRICE_CONTINUATION_FAILED": "股價未延續",
    "PRICE_NOT_REVERSED": "股價尚未反轉",
    "PRICE_WEAK": "股價偏弱",
    "ABOVE_VWAP": "站上均價",
    "BELOW_VWAP": "跌破均價",
    "VWAP_ACCEPTANCE_WEAKENED": "均價接受度轉弱",
    "EXTENSION_RISK_HIGH": "延伸風險高",
    "DO_NOT_CHASE": "不追",
    "PERSISTENCE_NO_DATA": "明日觀察資金延續",
    "REVERSAL_ALREADY_OCCURRED": "前期已發生反轉",
    "NO_DAY1_TRIGGER": "未達第一日反轉條件",
    "REVERSAL_NOT_CONFIRMED": "反轉未確認",
    "NO_PRIOR_OUTFLOW": "無前期流出",
    "REVERSAL_DAY1_EARLY": "反轉第 1 日（早期）",
    "REVERSAL_DAY1_EARLY_EXTENDED": "反轉第 1 日（已延伸）",
    "REVERSAL_DAY1": "反轉第 1 日",
    "PREVIOUS_FLOW_REVERSAL_DAY2_3_FAILURE": "前期反轉後第 2～3 日延續失敗",
    "OUTFLOW_WATCH_NOT_TRIGGERED": "流出觀察，尚未觸發反轉",
    "OUTFLOW_REVERSAL_WATCH": "流出反轉觀察",
    "OUTFLOW_WATCH": "流出觀察",
    "REVERSAL_FAILURE_CONTROL": "反轉失敗控制",
    "TREND_CONTROL": "趨勢控制",
    "OTHER_CONTROL": "其他對照",
    "NOT_REVERSAL": "非反轉",
    "STRONG_BUT_EXTENDED": "強勢但已延伸",
    "FLOW_POSITIVE_PRICE_NOT_ACCEPTED": "資金流入，但股價尚未被均價接受",
    "FLOW_CHIP_RESONANCE": "資金與籌碼共振",
    "FLOW_POSITIVE": "資金流入",
    "STRONG_CHIP_INTRADAY_OUTFLOW": "前期籌碼偏多，但盤中資金流出",
    "OUTFLOW_WEAK": "資金流出偏弱",
    "NO_DATA": "無資料",
    "NO_CHASE": "不追",
    "WATCH_PRIORITY": "優先觀察",
    "WAIT": "等待確認",
    "WATCH": "觀察",
    "OBSERVE_ONLY": "僅觀察",
    "NO_ENTRY": "不進場",
    "CONFIRMED": "已確認",
    "PARTIAL": "部分確認",
    "WEAK": "偏弱",
    "FAILED": "未確認",
    "PENDING_PERSISTENCE": "待確認資金延續",
    "N/A": "不適用",
    "NOT_STARTED": "未啟動",
    "PREPARING_TO_ACTIVATE": "準備啟動",
    "ACTIVATING": "啟動中",
    "MAIN_UPTREND_CONTINUATION": "主升續攻",
    "ACCELERATION_ATTACK": "加速攻擊",
    "EXHAUSTION_FAILURE": "衰竭／失敗",
    "RETURNING": "回流",
    "STRENGTHENING": "增強",
    "SUSTAINED": "持續",
    "SLOWING": "減速",
    "TURNED_BEARISH": "翻空",
    "CHASE_BREAKOUT": "可追",
    "SMALL_SIZE_CHASE": "小部位可追",
    "WAIT_PULLBACK": "等回踩",
    "BREAKOUT_CHASE": "突破追",
    "PULLBACK_ENTRY": "回踩接",
    "VWAP_SUPPORT": "VWAP承接",
    "FUND_FLOW_REACCELERATION": "資金再加速",
    "BELOW_VWAP": "跌 VWAP",
    "A_FLOW_TURNED_NEGATIVE": "A-flow 翻負",
    "VOLUME_STALL": "爆量滯漲",
    "KEY_PRICE_BREAK": "跌破關鍵價",
    "CONFIRMED_REVERSAL": "反轉確認",
    "FAILED_REVERSAL": "反轉失敗",
    "FLOW_FLIP": "資金翻轉",
    "ACCUMULATION": "累積買超",
    "REVERSAL_TRIGGER": "等待確認",
    "OUTFLOW_BASELINE": "連續流出觀察",
    "OBSERVING": "資料觀察中",
}


def _zh(value):
    return _ZH.get(str(value), str(value).replace("_", " "))


def _reasons(values):
    return " · ".join(html.escape(_zh(x)) for x in values)


def _badge(text, kind="neutral"):
    return f"<span class='badge {kind}'>{html.escape(str(text))}</span>"


def _pipeline(c: dict) -> str:
    prior_outflow = (c.get("foreign_net_5d") or 0) < 0 or (c.get("foreign_net_20d") or 0) < 0
    flow_flip = "YES" if prior_outflow and (c.get("aflow") or 0) > 0 else "NO"
    persistence = c.get("flow_persistence") or "NO_DATA"
    price_conf = c.get("price_confirmation") or "NO_DATA"
    sector_conf = c.get("sector_confirmation") or "NO_DATA"
    day2 = c.get("day2_ready") or "N/A"
    persistence_label = "明日觀察資金是否持續" if persistence == "NO_DATA" else _zh(persistence)
    day2_label = "明日確認" if day2 == "PENDING_PERSISTENCE" else _zh(day2)
    return f"""
      <div class='pipeline'>
        <div><small>資金是否翻轉</small><b>{html.escape("是" if flow_flip == "YES" else "否")}</b></div>
        <div><small>資金是否延續</small><b>{html.escape(persistence_label)}</b></div>
        <div><small>股價是否確認</small><b>{html.escape(_zh(price_conf))}</b></div>
        <div><small>族群是否確認</small><b>{html.escape(_zh(sector_conf))}</b></div>
        <div><small>第二日觀察</small><b>{html.escape(day2_label)}</b></div>
      </div>"""


def _chip_stance(c: dict) -> str:
    days = c.get("foreign_days")
    if days is not None:
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = None
    if days is not None and days < 0:
        return f"外資籌碼連續流出 {abs(days)} 日"
    if days is not None and days > 0:
        return f"外資籌碼連續流入 {days} 日"
    f5, f20 = c.get("foreign_net_5d"), c.get("foreign_net_20d")
    if f5 is not None and f20 is not None and f5 < 0 and f20 < 0:
        return "近 5 日與近 20 日籌碼皆偏流出"
    if f5 is not None and f20 is not None and f5 > 0 and f20 > 0:
        return "近 5 日與近 20 日籌碼皆偏流入"
    if f5 is not None and f5 < 0:
        return "近 5 日籌碼偏流出"
    if f5 is not None and f5 > 0:
        return "近 5 日籌碼偏流入"
    return "籌碼連續性資料不足"


def _analysis(c: dict) -> str:
    judgment = c.get("participation_summary")
    if judgment:
        return ""
    chip = _chip_stance(c)
    aflow = c.get("aflow")
    change = c.get("change_rate")
    above = c.get("above_vwap_proxy")
    flow = "今日盤中資金回流" if aflow is not None and aflow > 0 else "今日盤中資金流出" if aflow is not None and aflow < 0 else "今日盤中資金無資料"
    price = "股價上漲" if change is not None and change > 0 else "股價下跌" if change is not None and change < 0 else "股價漲跌無資料"
    position = "並站上盤中均價" if above else "且低於盤中均價" if above is not None else "盤中均價無資料"
    role = c.get("lab_role")

    if role == "REVERSAL_DAY1" and aflow is not None and aflow > 0 and change is not None and change > 0:
        return f"{chip}，{flow}、{price}{position}，屬於前期流出後的資金反轉初期（第一日觀察）。"
    if role == "REVERSAL_FAILURE_CONTROL":
        return f"{chip}，{flow}且{price}{position}，屬於前期反轉後未能延續的反轉失敗控制情況。"
    if role == "OUTFLOW_WATCH" and aflow is not None and aflow < 0 and change is not None and change < 0:
        return f"{chip}，{flow}、{price}{position}，屬於籌碼偏多但盤中轉弱的資金背離觀察情況。"
    if role == "OUTFLOW_WATCH":
        return f"{chip}，{flow}、{price}{position}，目前屬於流出觀察，尚未形成明確反轉。"
    if role == "TREND_CONTROL" and aflow is not None and aflow < 0 and change is not None and change < 0:
        return f"{chip}，{flow}、{price}{position}，屬於籌碼與盤中資金同步轉弱的趨勢控制情況。"
    if aflow is not None and aflow > 0 and change is not None and change > 0:
        return f"{chip}，{flow}、{price}{position}，屬於資金與股價同步偏強的觀察情況。"
    if aflow is not None and aflow < 0 and change is not None and change < 0:
        return f"{chip}，{flow}、{price}{position}，屬於盤中資金與股價同步偏弱的觀察情況。"
    return f"{chip}，{flow}、{price}{position}，目前資料不足以確認反轉方向。"


def _reversal_explanation(c: dict) -> str:
    judgment = c.get("participation_summary")
    if judgment:
        chip = _chip_stance(c)
        aflow = c.get("aflow")
        change = c.get("change_rate")
        flow = "資金回流" if aflow is not None and aflow > 0 else "資金流出" if aflow is not None and aflow < 0 else "資金無資料"
        price = "股價上漲" if change is not None and change > 0 else "股價下跌" if change is not None and change < 0 else "股價漲跌無資料"
        permission = _zh(c.get("chase_permission") or "DO_NOT_CHASE")
        entry = _zh(c.get("entry_method") or "PULLBACK_ENTRY")
        failures = "、".join(_zh(item.get("key")) for item in (c.get("failure_conditions") or [])) or "—"
        return (
            f"{chip}，{flow}、{price}；{judgment}"
            f" 判斷：趨勢階段 {_zh(c.get('trend_stage'))}；"
            f"資金狀態 {_zh(c.get('capital_state'))}；"
            f"追價許可：{permission}；進場方式：{entry}；"
            f"失敗條件：{failures}。"
        )
    chip = _chip_stance(c)
    aflow = c.get("aflow")
    change = c.get("change_rate")
    flow = "今日資金回流" if aflow is not None and aflow > 0 else "今日資金流出" if aflow is not None and aflow < 0 else "今日資金無資料"
    price = "股價上漲" if change is not None and change > 0 else "股價下跌" if change is not None and change < 0 else "股價漲跌無資料"
    role = c.get("lab_role")
    avg = c.get("avg_price")
    avg_text = _fmt(avg) if avg is not None else "盤中均價"
    change_text = _signed_fmt(change) if change is not None else "—"
    aflow_text = _signed_fmt(aflow, 0) if aflow is not None else "—"
    gap = c.get("ma5_distance_pct")
    ratio = c.get("aflow_ratio")
    ratio_text = f"，資金占成交量 {ratio * 100:.2f}%" if ratio is not None else ""
    base = c.get("c1_c2_label") or "前期條件待確認"

    if role == "REVERSAL_DAY1":
        if avg is not None and c.get("above_vwap_proxy"):
            return f"{base}；{chip}，{flow} {aflow_text} 張、{price}並站上盤中均價 {avg_text}{ratio_text}，盤中動能偏強；盤後列為反轉第 1 日，可觀察明日資金是否持續回流。"
        if avg is not None:
            return f"{base}；{chip}，雖然{flow} {aflow_text} 張，但尚未站上盤中均價 {avg_text}，盤後反轉尚未確認，不進場，等站上 {avg_text} 再確認。"
        return f"{base}；{chip}，{flow} {aflow_text} 張並{price}，盤後列為反轉第 1 日，可觀察明日資金是否持續回流。"
    if role == "REVERSAL_FAILURE_CONTROL":
        return f"{base}；{chip}，但{flow} {aflow_text} 張、{price}，前期反轉未能延續，屬於盤後反轉失敗控制；不進場，等重新站上盤中均價 {avg_text} 再觀察。"
    if role == "OUTFLOW_WATCH":
        if aflow is not None and aflow > 0 and avg is not None and not c.get("above_vwap_proxy"):
            return f"{base}；{chip}，雖然今日資金回流 {aflow_text} 張，但股價尚未站上盤中均價 {avg_text}，盤後反轉尚未確認；不進場，等站上 {avg_text} 再確認反轉。"
        if aflow is not None and aflow < 0 and change is not None and change < 0:
            return f"{base}；{chip}，今日資金流出 {aflow_text} 張且{price}，屬於資金與股價同步轉弱；不進場，等重新站上盤中均價 {avg_text} 再觀察。"
        return f"{base}；{chip}，{flow}、{price}，目前列為流出觀察；不進場，等站上盤中均價 {avg_text} 再確認。"
    if role == "TREND_CONTROL":
        return f"{base}；{chip}，{flow}、{price}，作為盤後趨勢對照，不列入反轉確認；不進場，等站上盤中均價 {avg_text} 再觀察。"
    return f"{base}；{chip}，{flow}、{price}，目前列為反轉觀察；不進場，等站上盤中均價 {avg_text} 再確認。"


def _reversal_explanation_html(c: dict) -> str:
    """保留說明原文，只把方向數字標色，讓句子可快速核對。"""
    text = html.escape(_reversal_explanation(c))
    chip = html.escape(_chip_stance(c))
    if chip and chip in text:
        chip_class = "chip-outflow" if "流出" in chip else "chip-inflow" if "流入" in chip else "chip-neutral"
        text = text.replace(chip, f"<span class='{chip_class}'>{chip}</span>", 1)

    aflow = c.get("aflow")
    if aflow is not None:
        amount = html.escape(_signed_fmt(aflow, 0))
        flow_phrase = "資金回流" if aflow > 0 else "資金流出" if aflow < 0 else "資金"
        text = text.replace(
            f"{flow_phrase} {amount} 張",
            f"{flow_phrase} <strong class='flow-value {'flow-in' if aflow > 0 else 'flow-out' if aflow < 0 else 'flow-flat'}'>{amount}</strong> 張",
            1,
        )

    avg = c.get("avg_price")
    if avg is not None:
        avg_text = html.escape(_fmt(avg))
        text = text.replace(f"均價 {avg_text}", f"均價 <strong class='vwap-value'>{avg_text}</strong>")
        text = text.replace(f"站上 {avg_text}", f"站上 <strong class='vwap-value'>{avg_text}</strong>")
    return text


def _reversal_box(c: dict) -> str:
    avg = c.get("avg_price")
    avg_text = _fmt(avg) if avg is not None else "—"
    flow_label = c.get("line_b_flow_label") or ("今日資金回流" if (c.get("aflow") or 0) > 0 else "今日資金流出" if (c.get("aflow") or 0) < 0 else "今日資金狀態待確認")
    price_label = f"已站上盤中均價 {avg_text}" if c.get("above_vwap_proxy") else f"尚未站上盤中均價 {avg_text}" if avg is not None else "盤中均價待確認"
    result = c.get("line_b_status_label") or _zh(c.get("lab_role") or "OTHER_CONTROL")
    explanation = "" if c.get("participation_summary") else f"<span>{_reversal_explanation_html(c)}</span>"
    return f"""
      <div class='reversal-box'>
        <strong>盤後反轉核對</strong>
        <div class='reversal-facts'>
          <div><small>前期基礎</small><b>{html.escape(c.get('c1_c2_label') or '前期條件待確認')}</b><em class='fact-note'>{html.escape(c.get('c1_c2_note') or '前期分類待確認')}</em></div>
          <div><small>今日資金結論</small><b>{html.escape(flow_label)}</b></div>
          <div><small>盤後價格結論</small><b>{html.escape(price_label)}</b></div>
          <div><small>盤後狀態</small><b>{html.escape(result)}</b></div>
        </div>
        {explanation}
      </div>"""


def _participation_box(c: dict) -> str:
    """Render the actionable participation decision separately from warnings."""
    conditions = c.get("failure_conditions") or []
    condition_text = " · ".join(
        f"{_zh(item.get('key'))}{'（目前觸發）' if item.get('active') else '（未觸發）'}"
        for item in conditions
    ) or "—"
    stage = c.get("trend_stage") or "NOT_STARTED"
    permission = c.get("chase_permission") or "DO_NOT_CHASE"
    tone = "danger" if permission == "DO_NOT_CHASE" else "attack" if permission == "CHASE_BREAKOUT" else "ready"
    return f"""
      <div class='participation-box {tone}' style='margin-top:12px;padding:12px 13px;border:1px solid #cfd9e8;border-left:4px solid #465ed9;border-radius:12px;background:#f3f6ff;color:#34435e;font-size:13px;line-height:1.55'>
        <strong style='display:block;color:#172033;font-size:15px;margin-bottom:7px'>現在能不能參與</strong>
        <div class='participation-grid' style='display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px'>
          <div style='padding:8px;background:#fff;border:1px solid #e0e6f0;border-radius:8px'><small style='display:block;color:#71809b;font-size:11px'>趨勢階段</small><b>{html.escape(_zh(stage))}</b></div>
          <div style='padding:8px;background:#fff;border:1px solid #e0e6f0;border-radius:8px'><small style='display:block;color:#71809b;font-size:11px'>資金狀態</small><b>{html.escape(_zh(c.get('capital_state') or 'SLOWING'))}</b></div>
          <div style='padding:8px;background:#fff;border:1px solid #e0e6f0;border-radius:8px'><small style='display:block;color:#71809b;font-size:11px'>追價許可</small><b>{html.escape(_zh(permission))}</b></div>
          <div style='padding:8px;background:#fff;border:1px solid #e0e6f0;border-radius:8px'><small style='display:block;color:#71809b;font-size:11px'>進場方式</small><b>{html.escape(_zh(c.get('entry_method') or 'PULLBACK_ENTRY'))}</b></div>
        </div>
        <p>{html.escape(c.get('participation_summary') or '目前資料不足，等待新訊號。')}</p>
        <small class='failure-list'><b>失敗條件</b>：{html.escape(condition_text)}</small>
      </div>"""


def _state_machine_box(c: dict) -> str:
    """Show the saved event path instead of inferring a state from one row."""
    state = c.get("state_machine") or "OBSERVING"
    notes = {
        "CONFIRMED_REVERSAL": "連賣後出現異常買，下一交易日仍延續買超。",
        "FAILED_REVERSAL": "連賣後出現異常買，但下一交易日轉回資金流出。",
        "FLOW_FLIP": "同一交易時段已出現買、賣、買的快速資金切換。",
        "ACCUMULATION": "未出現前期連賣，但至少兩個交易日連續異常買。",
        "REVERSAL_TRIGGER": "已出現異常買，等待下一交易日的延續買或再賣。",
        "OUTFLOW_BASELINE": "官方籌碼仍為連賣背景，尚未出現異常買。",
        "OBSERVING": "尚未符合反轉狀態；系統只保存實際新事件。",
    }
    path = "".join(
        f"<span class='state-step {html.escape(str(item.get('status') or 'pending'))}'>{html.escape(str(item.get('label') or '—'))}</span>"
        for item in c.get("state_path", [])
    )
    events = c.get("state_events") or []
    labels = {"OUTFLOW_BASELINE": "連賣", "ABNORMAL_BUY": "異常買", "BUY": "買", "SELL": "再賣"}
    ledger = " · ".join(
        f"{str(item.get('session') or '')[-5:]} {labels.get(item.get('kind'), item.get('kind') or '—')}"
        for item in events
    ) or "尚未累積事件"
    return f"""<div class='state-machine {html.escape(state.lower())}'>
      <strong>{html.escape(_zh(state))}</strong><div class='state-path'>{path}</div>
      <p>{html.escape(notes.get(state, notes['OBSERVING']))}</p><small>事件：{html.escape(ledger)}</small></div>"""


def _card(c: dict, reversal: bool = False) -> str:
    state = (c.get("state_machine") or c["reversal_state"]) if reversal else c["flow_state"]
    reasons = c["reversal_reason_codes"] if reversal else c["reason_codes"]
    af = c.get("aflow")
    change = c.get("change_rate")
    role = c.get("lab_role") or "—"
    grade = c.get("reversal_grade")
    role_badges = _badge(_zh(role), "lab") + (" " + _badge(f"{grade}級", "grade") if grade else "")
    price_direction = "rise" if (change or 0) > 0 else "fall" if (change or 0) < 0 else "neutral"
    ratio = (c.get("aflow_ratio") or 0) * 100 if c.get("aflow_ratio") is not None else None
    analysis = _analysis(c)
    analysis_html = (
        f"<div class='analysis'><strong>盤中資金判讀</strong><span>{html.escape(analysis)}</span></div>"
        if analysis else ""
    )
    return f"""
    <article class='card'>
      <div class='head'><div><b>{html.escape(c.get('symbol',''))} {html.escape(c.get('name') or '')}</b><small>{html.escape(c.get('sector') or '')}</small></div>
      <div class='right'><strong class='num {price_direction}'>{_fmt(c.get('price'))}</strong><span class='chg {price_direction}'>{_signed_fmt(change)}%</span></div></div>
      <div class='badges'>{role_badges}</div>
      <div class='flow'>盤中主動資金 <b>{_num(af, 0, sign=True)}</b> · 比例 {_num(ratio, 2, sign=True)}% · VWAP/均價 <span class='num neutral'>{_fmt(c.get('avg_price'))}</span></div>
      {_participation_box(c)}
      {_state_machine_box(c) if reversal else ''}
      {_reversal_box(c) if reversal else ''}
      <div class='state'>{html.escape(_zh(state))}</div>
      <div class='action'>{html.escape(_zh(c.get('action') or 'OBSERVE_ONLY'))}</div>
      <div class='reasons'>{_reasons(reasons)}</div>
      {analysis_html}
      <div class='chips'><div class='chips-title'>前一交易日外資籌碼</div>
        <div class='chip-grid'>
          <div><small>單日（D）</small><strong>{_num(c.get('foreign_net_d'), 0, sign=True)}<em> 張</em></strong></div>
          <div><small>近 5 日合計（5D）</small><strong>{_num(c.get('foreign_net_5d'), 0, sign=True)}<em> 張</em></strong></div>
          <div><small>近 20 日合計（20D）</small><strong>{_num(c.get('foreign_net_20d'), 0, sign=True)}<em> 張</em></strong></div>
        </div>
        <div class='chips-explain'>D＝前一交易日；5D／20D＝近 5／20 個交易日法人買賣超合計</div>
        <div class='source'>來源：{html.escape(c.get('foreign_source') or '—')}｜資料日：{html.escape(c.get('foreign_source_date') or '—')}<br>行情：{html.escape(c.get('price_source') or '—')} / {html.escape(c.get('quote_status') or '—')}</div>
      </div>
    </article>"""


def render_html(view: dict) -> str:
    lab_name = "資金反轉驗證"
    scope = "盤中資金反轉觀察"
    participation = view.get("participation") or view.get("reversal") or []
    groups = view.get("state_groups") or {}
    summary = view.get("state_summary") or {}
    confirmed = groups.get("confirmed") or []
    failed = groups.get("failed") or []
    flow_flip = groups.get("flip") or []
    accumulation = groups.get("accumulation") or []
    css = """
    *{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'PingFang TC','Noto Sans TC',sans-serif;background:#f6f8fc;color:#172033;width:min(100%,860px);margin:auto;padding:18px;line-height:1.35;overflow-x:hidden}
    h1{font-size:24px;margin:0}.sub{color:#66758f;font-size:13px;margin:5px 0 0}.notice{background:#fff8df;border:1px solid #efd888;border-radius:14px;padding:12px 14px;margin:14px 0;color:#68551a;font-size:13px}.notice b{display:block;color:#302c20;margin-bottom:3px}.color-guide{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0 2px;font-size:13px;font-weight:800}.color-guide .rise{color:#d84d68}.color-guide .fall{color:#078d6b}.ranking-definition{margin:10px 0;padding:11px 13px;border:1px solid #dfe5ef;border-radius:12px;background:#fff;color:#59677e;font-size:13px;line-height:1.55}.ranking-definition strong{color:#172033;margin-right:8px}.tabs{display:flex;gap:7px;margin:14px 0;position:sticky;top:0;z-index:10;background:#f6f8fc;padding:8px 0}.tabs .tab{flex:1;text-align:center;padding:11px;border:0;border-radius:11px;background:#e8edf6;color:#172033;font:inherit;font-weight:800;cursor:pointer}.tabs .tab.active{background:#172033;color:#fff}.section{scroll-margin-top:70px}.section h2{font-size:18px;margin:18px 0 8px}.sort-note{font-size:12px;font-weight:600;color:#71809b;margin-left:6px}.meta{color:#71809b;font-size:12px;margin:6px 0 14px}.card{background:white;border:1px solid #dfe5ef;border-radius:16px;padding:14px;margin:10px 0;box-shadow:0 3px 14px rgba(35,53,84,.05)}.head{display:flex;justify-content:space-between;gap:12px}.head b{font-size:18px}.head small{display:block;color:#74829b;margin-top:3px}.right{text-align:right;white-space:nowrap}.right strong{font-size:20px}.num{font-variant-numeric:tabular-nums}.num.rise{color:#d84d68}.num.fall{color:#078d6b}.num.neutral{color:#172033}.chg{display:block;font-weight:800}.chg.rise{color:#d84d68}.chg.fall{color:#078d6b}.badges{margin:9px 0 2px}.badge{display:inline-block;padding:4px 7px;border-radius:7px;font-size:11px;font-weight:850;border:1px solid #d6ddec;background:#f7f9fc}.badge.lab{color:#7a5b09;background:#fff8df;border-color:#efd888}.badge.grade{color:#7d2db1;background:#f7edff;border-color:#dcbcf2}.flow{margin-top:9px;color:#59677e;font-size:15px}.flow b{font-size:18px}.pipeline{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-top:12px}.pipeline div{background:#f7f9fc;border:1px solid #e2e7f0;border-radius:10px;padding:8px;min-width:0}.pipeline small{display:block;color:#7a879d;font-size:9px;line-height:1.2}.pipeline b{display:block;margin-top:3px;font-size:11px;overflow-wrap:anywhere}.reversal-box{margin-top:12px;padding:12px 13px;border:1px solid #c8a52f;border-left:4px solid #b18912;border-radius:12px;background:#fffaf0;color:#4d4434;font-size:13px;line-height:1.65}.reversal-box strong{display:block;color:#7a5b09;font-size:15px;margin-bottom:3px}.reversal-facts{display:grid;grid-template-columns:repeat(2,1fr);gap:7px;margin:8px 0}.reversal-facts>div{padding:8px 9px;background:#fffdf7;border:1px solid #eee4c9;border-radius:8px}.reversal-facts small{display:block;color:#817864;font-size:11px}.reversal-facts b{display:block;margin-top:2px;color:#302c20;font-size:12px;line-height:1.35}.reversal-box span{display:block}.state{font-size:13px;font-weight:900;color:#8b6c12;margin-top:10px}.action{font-size:16px;font-weight:900;margin-top:2px}.reasons{margin-top:6px;color:#4f5d74;font-size:13px}.analysis{margin-top:10px;padding:10px 11px;border-left:3px solid #c39a22;background:#f9fafc;color:#425069;font-size:13px;line-height:1.65}.analysis strong{display:block;color:#7a5b09;font-size:12px;margin-bottom:2px}.analysis span{display:block}.chips{margin-top:12px;padding:12px 13px;background:#f8f6ef;border-radius:10px;color:#6b6251;font-size:13px;line-height:1.5}.chips-title{font-size:15px;font-weight:900;color:#403a2e;margin-bottom:8px}.chip-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.chip-grid>div{padding:7px 8px;background:#fffdf7;border:1px solid #ece5d3;border-radius:8px}.chip-grid small{display:block;font-size:11px;color:#776f61}.chip-grid strong{display:block;font-size:18px;margin-top:2px}.chip-grid em{font-size:11px;font-style:normal;color:#776f61}.chips-explain{margin-top:8px;font-size:11px;color:#776f61}.source{margin-top:6px;font-size:11px;color:#776f61;overflow-wrap:anywhere}@media(max-width:640px){body{padding:12px}.ranking-definition strong{display:block;margin:0 0 3px}.pipeline{grid-template-columns:1fr 1fr}.pipeline div:last-child{grid-column:1/-1}.head b{font-size:17px}.right strong{font-size:18px}.reversal-facts{grid-template-columns:1fr}.chip-grid{grid-template-columns:1fr}.chip-grid>div{display:flex;align-items:center;justify-content:space-between;gap:8px}.chip-grid small{font-size:12px}.chip-grid strong{font-size:20px;margin-top:0}}
    .card{overflow:hidden}.head>div:first-child{min-width:0}.head b,.flow,.state,.action,.reasons,.analysis span,.reversal-box span,.chips-explain,.source{overflow-wrap:anywhere}.state{padding-top:10px;border-top:1px solid #edf0f4}.action{line-height:1.4}.reasons{line-height:1.55}.analysis{padding:12px 13px}.chips{padding:13px}.reversal-box{padding:13px 14px}.reversal-facts{align-items:stretch}.reversal-facts>div{min-width:0}.reversal-facts b{overflow-wrap:anywhere}.reversal-facts .fact-note{display:block;margin-top:3px;color:#817864;font-size:10px;font-style:normal;line-height:1.35;overflow-wrap:anywhere}.source{word-break:break-word}.chip-outflow,.flow-out{color:#078d6b}.chip-inflow,.flow-in{color:#d84d68}.chip-neutral,.flow-flat{color:#172033}.flow-value,.vwap-value{font-weight:900}.vwap-value{color:#172033}
    @media(max-width:640px){.head{align-items:flex-start}.head b{font-size:17px}.flow{font-size:14px}.state{font-size:14px}.action{font-size:15px}.analysis,.chips,.reversal-box{margin-left:0;margin-right:0}}
    .tabs{top:0;overflow-x:auto}.tabs .tab{min-width:max-content}.section{scroll-margin-top:24px}.reversal-box>span{display:block}.reversal-box .chip-outflow,.reversal-box .chip-inflow,.reversal-box .chip-neutral{display:inline}.state-summary{display:flex;margin:16px 0 4px;border:1px solid #dfe5ef;background:#fff;overflow-x:auto}.state-summary div{min-width:110px;padding:10px 12px;border-right:1px solid #e7ebf2}.state-summary div:last-child{border-right:0}.state-summary small{display:block;color:#71809b;font-size:11px;font-weight:800}.state-summary b{display:block;margin-top:3px;font-size:20px}.state-machine{margin-top:12px;padding:12px;border-left:4px solid #71809b;background:#f7f9fc;color:#43516a}.state-machine strong{display:block;color:#172033;font-size:16px}.state-machine p{margin:8px 0 0;font-size:13px;line-height:1.55}.state-machine small{display:block;margin-top:7px;color:#71809b;font-size:11px}.state-path{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}.state-step{padding:5px 7px;border:1px solid #d8e0eb;background:#fff;color:#71809b;border-radius:6px;font-size:12px;font-weight:850}.state-step.done{border-color:#99d9c5;background:#eefbf6;color:#087f61}.state-step.failed{border-color:#efb6c2;background:#fff1f3;color:#c84963}.confirmed_reversal{border-color:#087f61;background:#f0fbf6}.failed_reversal{border-color:#c84963;background:#fff4f5}.flow_flip{border-color:#465ed9;background:#f3f5ff}.accumulation{border-color:#9c6b00;background:#fff9ed}.empty-state{padding:24px 16px;border:1px dashed #cfd8e6;background:#fff;color:#66758f;text-align:center;line-height:1.6}
    """
    participation_css = ".participation-box.ready{border-left-color:#087f61;background:#f0fbf6}.participation-box.attack{border-left-color:#d84d68;background:#fff4f5}.participation-box.danger{border-left-color:#c84963;background:#fff4f5}.participation-grid b{display:block;margin-top:3px;color:#172033}.failure-list{display:block;margin-top:8px;color:#66758f}.failure-list b{color:#172033}@media(max-width:640px){.participation-grid{grid-template-columns:1fr 1fr!important}}"
    page_shell_css = "html,body{width:100%!important;max-width:none!important;min-width:0!important;overflow-x:hidden!important}body{margin:0!important;padding:0 28px 40px!important}.mls-nav{position:relative!important;top:auto!important;width:calc(100% + 56px)!important;margin-left:-28px!important;margin-right:-28px!important;margin-top:0!important;margin-bottom:24px!important;overflow-x:auto!important;overflow-y:hidden!important;flex-wrap:nowrap!important}.mls-nav-link{flex:0 0 auto!important}@media(max-width:640px){body{padding:0 12px 32px!important}.mls-nav{width:calc(100% + 24px)!important;margin-left:-12px!important;margin-right:-12px!important;margin-bottom:16px!important;overflow-x:visible!important;overflow-y:visible!important;flex-wrap:wrap!important}.mls-nav-link{flex:0 0 auto!important}}"
    return f"""<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(lab_name)}</title><style>{css}</style><style>{participation_css}</style>{NAV_CSS}<style>{page_shell_css}</style></head><body>
    {nav_html('reversal')}
    <h1>{html.escape(lab_name)}</h1><div class='sub'>連賣 → 異常買 → 延續買／再賣，保留原本反轉判讀。</div>
    <div class='meta'>即時資料 · {html.escape(str(view.get('updated_at') or view.get('snapshot') or '—'))}</div>
    <div class='notice'><b>{html.escape(scope)}</b>這張卡回答的是「現在能不能參與」，不是只把風險翻譯成禁止交易。高位、連漲三天、乖離高都不是淘汰條件；只有增量資金消失、價格不再接受更高位置、動能衰竭，或固定失敗條件觸發時，才顯示不追。</div>
    <div class='color-guide'><span class='rise'>● 漲幅／資金流入／籌碼增加：紅色</span><span class='fall'>● 跌幅／資金流出／籌碼減少：綠色</span></div>
    <div class='ranking-definition'><strong>判讀規則</strong>連賣使用前一交易日官方籌碼；異常買為 A-flow 淨買且占成交量至少 5%。反轉確認、反轉失敗與資金翻轉只依實際跨日事件判定。固定輸出：趨勢階段／資金狀態／追價許可／進場方式／失敗條件。</div>
    <div class='tabs' role='tablist'><button type='button' class='tab active' data-target='in' role='tab'>資金流入10</button><button type='button' class='tab' data-target='out' role='tab'>資金流出10</button><button type='button' class='tab' data-target='confirmed' role='tab'>反轉確認 {int(summary.get('confirmed') or 0)}</button><button type='button' class='tab' data-target='history' role='tab'>反轉歷史紀錄</button></div>
    <section id='in' class='section tab-panel' role='tabpanel'><h2>資金流入 TOP10</h2><p class='meta'>今日盤中主動資金流入，由高至低排列。</p>{''.join(_card(c) for c in view.get('inflow') or []) or "<div class='empty-state'>目前沒有資金流入資料。</div>"}</section>
    <section id='out' class='section tab-panel' role='tabpanel' hidden><h2>資金流出 TOP10</h2><p class='meta'>今日盤中主動資金流出，由高至低排列；作為反轉觀察來源。</p>{''.join(_card(c) for c in view.get('outflow') or []) or "<div class='empty-state'>目前沒有資金流出資料。</div>"}</section>
    <section id='confirmed' class='section tab-panel' role='tabpanel' hidden><h2>反轉確認</h2><p class='meta'>連賣 → 異常買 → 下一交易日延續買。</p>{''.join(_card(c, True) for c in confirmed) or "<div class='empty-state'>尚無反轉確認，系統持續累積跨日事件。</div>"}</section>
    <section id='history' class='section tab-panel' role='tabpanel' hidden><h2>反轉歷史紀錄</h2><p class='meta'>每日保存反轉 TOP20；點擊日期只在本頁切換，不另開分頁。</p><select id='history-date' style='padding:9px;border:1px solid #dfe5ef;border-radius:8px;background:#fff;font:inherit'>{''.join(f"<option value='{html.escape(str(d), quote=True)}'>{html.escape(str(d))}</option>" for d in (view.get('reversal_history_dates') or [])) or "<option>尚無歷史紀錄</option>"}</select><div id='history-summary' style='margin-top:12px;padding:13px 14px;border:1px solid #dfe5ef;border-left:4px solid #465ed9;border-radius:11px;background:#fff;color:#425069;line-height:1.65'>選擇日期後載入當日總結。</div><div id='history-body' class='meta' style='margin-top:12px'>選擇日期後載入反轉 TOP20。</div></section>
    <script>var tabs=Array.from(document.querySelectorAll('.tab'));function activate(button,writeHash){{tabs.forEach(function(item){{var active=item===button;item.classList.toggle('active',active);item.setAttribute('aria-selected',active?'true':'false')}});document.querySelectorAll('.tab-panel').forEach(function(panel){{panel.hidden=panel.id!==button.dataset.target}});if(writeHash)history.replaceState(null,'','#'+button.dataset.target);if(button.dataset.target==='history')loadHistory()}}tabs.forEach(function(button){{button.addEventListener('click',function(){{activate(button,true)}})}});var initial=document.querySelector('.tab[data-target="'+location.hash.slice(1)+'"]')||tabs[0];activate(initial,false);async function loadHistory(){{var select=document.getElementById('history-date'),body=document.getElementById('history-body'),summary=document.getElementById('history-summary');if(!select||!select.value||select.value==='尚無歷史紀錄')return;body.textContent='讀取中…';try{{var r=await fetch('/api/reversal-lab/history?date='+encodeURIComponent(select.value),{{cache:'no-store'}}),d=await r.json();var zh={{NOT_STARTED:'未啟動',PREPARING_TO_ACTIVATE:'準備啟動',ACTIVATING:'啟動中',MAIN_UPTREND_CONTINUATION:'主升續攻',ACCELERATION_ATTACK:'加速攻擊',EXHAUSTION_FAILURE:'衰竭／失敗',RETURNING:'回流',STRENGTHENING:'增強',SUSTAINED:'持續',SLOWING:'減速',TURNED_BEARISH:'翻空',CHASE_BREAKOUT:'可追',SMALL_SIZE_CHASE:'小部位可追',WAIT_PULLBACK:'等回踩',DO_NOT_CHASE:'不追',BREAKOUT_CHASE:'突破追',PULLBACK_ENTRY:'回踩接',VWAP_SUPPORT:'VWAP承接',FUND_FLOW_REACCELERATION:'資金再加速'}};var rows=d.rows||[],pc={{}} ,tc={{}},fc={{}};rows.forEach(function(c){{pc[c.chase_permission]=(pc[c.chase_permission]||0)+1;tc[c.trend_stage]=(tc[c.trend_stage]||0)+1;fc[c.capital_state]=(fc[c.capital_state]||0)+1}});var list=function(counts){{return Object.keys(counts).map(function(k){{return String(zh[k]||k||'—')+' '+counts[k]}}).join(' · ')||'—'}};summary.innerHTML='<b>'+String(d.trade_date||d.pool_date||select.value)+' 當日總結</b><br>保存 '+rows.length+' 檔　｜　追價許可：'+list(pc)+'<br>趨勢階段：'+list(tc)+'<br>資金狀態：'+list(fc);body.innerHTML=rows.map(function(c){{return '<div class="card"><b>'+String(c.symbol||'')+' '+String(c.name||'')+'</b><span style="float:right">'+String(c.change_rate??'—')+'%</span><div class="meta">趨勢階段：'+String(zh[c.trend_stage]||c.trend_stage||'—')+' · 資金狀態：'+String(zh[c.capital_state]||c.capital_state||'—')+' · 追價許可：'+String(zh[c.chase_permission]||c.chase_permission||'—')+'<br>進場方式：'+String(zh[c.entry_method]||c.entry_method||'—')+' · 主動資金：'+String(c.aflow??'—')+'</div></div>'}}).join('')||'<div class="empty-state">該日沒有保存資料。</div>'}}catch(e){{summary.textContent='當日總結讀取失敗。';body.textContent='歷史紀錄讀取失敗。'}}}}var hs=document.getElementById('history-date');if(hs)hs.addEventListener('change',loadHistory);</script>
    </body></html>"""


def render_history_html(history: dict) -> str:
    """Standalone Chinese history UI; it does not add a fourth main tab."""
    dates = history.get("dates") or []
    selected = history.get("pool_date") or ""
    options = "".join(
        f"<option value='{html.escape(str(date), quote=True)}' {'selected' if str(date) == str(selected) else ''}>{html.escape(str(date))}</option>"
        for date in dates
    ) or "<option>尚無歷史紀錄</option>"
    rows = history.get("rows") or []
    cards = []
    for row in rows:
        change = row.get("change_rate")
        change_text = f"{float(change):+.2f}%" if isinstance(change, (int, float)) else "—"
        change_tone = "rise" if isinstance(change, (int, float)) and change > 0 else "fall" if isinstance(change, (int, float)) and change < 0 else "neutral"
        cards.append(
            f"<article class='history-card'><b>{html.escape(str(row.get('symbol') or ''))} {html.escape(str(row.get('name') or ''))}</b>"
            f"<strong class='{change_tone}'>{html.escape(change_text)}</strong><p>趨勢階段：{html.escape(_zh(row.get('trend_stage') or 'NOT_STARTED'))}　"
            f"追價許可：{html.escape(_zh(row.get('chase_permission') or 'DO_NOT_CHASE'))}</p>"
            f"<p>資金狀態：{html.escape(_zh(row.get('capital_state') or 'SLOWING'))}　"
            f"進場方式：{html.escape(_zh(row.get('entry_method') or 'PULLBACK_ENTRY'))}　"
            f"主動資金：{html.escape(_signed_fmt(row.get('aflow'), 0))}</p></article>"
        )
    return f"""<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>反轉歷史紀錄</title><style>
    *{{box-sizing:border-box}}body{{width:100%;max-width:none;margin:0;padding:24px 18px 40px;font-family:-apple-system,BlinkMacSystemFont,'PingFang TC','Noto Sans TC',sans-serif;background:#f6f8fc;color:#172033}}h1{{margin:0;font-size:26px}}.sub{{margin:7px 0 18px;color:#71809b}}.bar{{display:flex;align-items:center;gap:10px;margin-bottom:16px}}select{{padding:9px 12px;border:1px solid #dfe5ef;border-radius:9px;background:#fff;font:inherit}}.history-card{{position:relative;margin:10px 0;padding:16px 18px;border:1px solid #dfe5ef;border-radius:16px;background:#fff;box-shadow:0 3px 14px rgba(35,53,84,.05)}}.history-card b{{font-size:18px}}.history-card strong{{position:absolute;right:18px;top:16px;font-size:18px}}.history-card strong.rise{{color:#d84d68}}.history-card strong.fall{{color:#078d6b}}.history-card strong.neutral{{color:#172033}}.history-card p{{margin:9px 0 0;color:#66758f;font-size:14px}}.empty{{padding:28px;text-align:center;border:1px dashed #cfd8e6;background:#fff;color:#66758f}}@media(max-width:640px){{body{{padding:16px 12px 32px}}h1{{font-size:23px}}}}
    </style></head><body><h1>反轉歷史紀錄</h1><div class='sub'>每日保存反轉 TOP20，不覆蓋既有日期。</div><div class='bar'><label for='history-date'>交易日</label><select id='history-date' onchange="location.href='/reversal-lab/history?date='+encodeURIComponent(this.value)">{options}</select></div><div class='sub'>資料日：{html.escape(str(selected or '—'))}　共 {len(rows)} 檔</div>{''.join(cards) or "<div class='empty'>該日尚無反轉紀錄。</div>"}</body></html>"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=None)
    p.add_argument("--json", default="reversal_lab_live.json")
    p.add_argument("--html", default="reversal_lab_live.html")
    args = p.parse_args()
    payload = fetch_live_rows(args.url) if args.url else fetch_live_rows()
    view = build_live_view(payload)
    Path(args.json).write_text(json.dumps(view, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(args.html).write_text(render_html(view), encoding="utf-8")
    print(json.dumps({
        "lab": view.get("lab_name"),
        "scope": view.get("model_scope"),
        "snapshot": view.get("updated_at") or view.get("snapshot"),
        "inflow": [(c["symbol"], c["aflow"], c["flow_state"]) for c in view["inflow"]],
        "outflow": [(c["symbol"], c["aflow"], c["flow_state"]) for c in view["outflow"]],
        "reversal": [(c["symbol"], c["lab_role"], c.get("reversal_grade"), c["reversal_state"]) for c in view["reversal"][:12]],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
