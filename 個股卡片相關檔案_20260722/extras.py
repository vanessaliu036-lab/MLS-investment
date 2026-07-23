# -*- coding: utf-8 -*-
"""個股卡片 / 每日報告 / 51 檔觀察池 — 三個 UI 路由的資料供應端。

設計目標：
- /api/stock/{code}  → 從 build_card() 撈單張個股決策卡
- /api/report        → 今日 / 昨日的盤後驗證摘要（接 review + state）
- /api/watchpool     → 51 檔全集觀察池（從 /api/intraday-test 抓 rows）

不另存資料庫、全部從 VPS Shioaji 訂閱 buffer 與 mls_intraday.py
既有路由的 STATE 拼裝出來。
"""
from __future__ import annotations

import sys
import datetime as _dt
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

# 保證 vps_intraday_test 與 app/ 都在 import 路徑
_BASE = Path(__file__).resolve().parent
_ROOT = _BASE.parent
for p in (str(_BASE), str(_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import config as C  # 0722 自己的 config
import stock_card
import vps_intraday_test as VIT  # 內含 51 檔 / Shioaji 訂閱查詢
import broker  # VPS Shioaji 訂閱 buffer


def _health_for_card(code: str, snap: Dict[str, Any], all_rows: List[Dict[str, Any]]):
    """盤後卡片的健康度 fallback；不讀 Shioaji 即時 buffer。"""
    try:
        import money_health
        import scoring
        # 盤後卡片沒有盤中 aflow；固定使用已落地的盤後資料，缺值就維持 0。
        scoring._aflow[str(code)] = int(snap.get("aflow") or 0)
        members = [r for r in all_rows
                   if C.SECTOR_MAP.get(str(r.get("code")), (None,))[0]
                   == C.SECTOR_MAP.get(str(code), (None,))[0]]
        changes = [r.get("change_rate") for r in members
                   if isinstance(r.get("change_rate"), (int, float))]
        sector_pct = (sum(changes) / len(changes)) if changes else None
        src = dict(snap)
        src["code"] = str(code)
        src["avg_price"] = snap.get("avg_price") or snap.get("price")
        src["high"] = snap.get("high") or snap.get("price")
        src["volume_ratio"] = snap.get("volume_ratio") or 0
        return money_health.stock_health(src, sector_pct=sector_pct)
    except Exception as exc:
        print(f"[extras] health {code} 失敗: {exc}", flush=True)
        return None


def _decision_factors(card: Dict[str, Any], live: Dict[str, Any]) -> Dict[str, Any]:
    """把卡片目前已取得的真實資料對齊七因子欄位。

    承接品質以可追溯的法人、融資、主動買賣與大戶代理欄位計算；
    若任一必要來源沒有值，保留 None，不用 UI 預設數字掩蓋資料缺口。
    """
    chip = card.get("chip") or {}
    hs = card.get("health_score")
    active = live.get("aflow")
    buy_pct = (card.get("flow") or {}).get("active_buy_pct")
    margin5 = chip.get("margin_change_5d")
    foreign20 = chip.get("foreign_net_20d")
    big_delta = chip.get("big400_delta")
    factors = {}
    if hs is not None:
        factors["money_health"] = {"points": round(30 * float(hs) / 100, 1), "max": 30, "status": "已接入"}
    else:
        factors["money_health"] = {"points": None, "max": 30, "status": "缺資料"}
    if active is not None:
        factors["net_active"] = {"points": round(22 * max(0, min(1, float(active) / 50000)), 1), "max": 22, "status": "已接入"}
    else:
        factors["net_active"] = {"points": None, "max": 22, "status": "缺資料"}
    ev_defs = [("主動買盤佔比", buy_pct, lambda v: v >= 50),
               ("法人20日", foreign20, lambda v: v > 0),
               ("融資5日", margin5, lambda v: v <= 0),
               ("大戶變化", big_delta, lambda v: v >= 0)]
    present = [(name, val, ok) for name, val, ok in ev_defs if val is not None]
    if len(present) >= 2:
        votes = sum(1 for _, val, ok in present if ok(val))
        absorption = round(votes / len(present) * 100, 1)
        miss_names = [name for name, val, _ in ev_defs if val is None]
        factors["absorption"] = {"points": round(18 * absorption / 100, 1), "max": 18,
                                  "status": ("已接入" if not miss_names else
                                             f"以 {len(present)}/4 項證據計分（缺{'、'.join(miss_names)}）"),
                                  "raw_score": absorption,
                                  "source": "Shioaji＋FinMind法人/融資/大戶代理"}
    else:
        factors["absorption"] = {"points": None, "max": 18, "status": "缺資料"}
    ma20 = live.get("ma20")
    price = live.get("price")
    if price is not None and ma20:
        factors["vs_ma20"] = {"points": 12 if float(price) >= float(ma20) else 0, "max": 12, "status": "已接入"}
    else:
        factors["vs_ma20"] = {"points": None, "max": 12, "status": "缺資料"}
    streak = None
    if foreign20 is not None:
        # 連買日由 get_chips() 的同一 FinMind 來源供應；未帶入時不猜測。
        streak = chip.get("inst_streak")
    if streak is not None:
        factors["inst_streak"] = {"points": round(10 * max(0, min(1, float(streak) / 5)), 1), "max": 10, "status": "已接入"}
    else:
        factors["inst_streak"] = {"points": None, "max": 10, "status": "缺資料"}
    if margin5 is not None:
        factors["margin"] = {"points": 8 if margin5 < 0 else 0, "max": 8, "status": "已接入"}
    else:
        factors["margin"] = {"points": None, "max": 8, "status": "缺資料"}
    available = sum(v["max"] for v in factors.values() if v["points"] is not None)
    score = round(sum(v["points"] or 0 for v in factors.values()), 1)
    return {"factors": factors, "score": score, "score_max": 100,
            "score_available": available,
            "missing": [k for k, v in factors.items() if v["points"] is None],
            "rule": "screen intraday.py 六因子 100 分制；缺資料不補分。"}


def _now_tw() -> str:
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")


# 族群平均/大盤漲跌快取（60 秒）:規範=個股數據一律抓最近可用日期計算，
# 不准顯示「資料缺：不計算」。來源:Shioaji 官方 snapshot(收盤後仍回當日
# 收盤值)＋official_source 官方大盤。
_sec_mkt_cache: Dict[str, Any] = {"ts": 0.0, "sector": {}, "market": None}


def _sector_market_pct(sector_name: str):
    """回 (族群平均漲跌%, 大盤漲跌%)，皆為最近可用交易日資料。"""
    import time as _t
    now = _t.time()
    if now - _sec_mkt_cache["ts"] > 60:
        _sec_mkt_cache["sector"] = {}
        _sec_mkt_cache["ts"] = now
        try:
            import official_source as _O
            _sec_mkt_cache["market"] = (_O.market_index() or {}).get("change_pct")
        except Exception as exc:
            print(f"[extras] 官方大盤讀取失敗: {exc}", flush=True)
    if sector_name and sector_name not in _sec_mkt_cache["sector"]:
        try:
            members = [c for c, (s, _) in C.SECTOR_MAP.items() if s == sector_name]
            snaps = broker.batch_snapshots(members) if members else []
            chs = [s["change_rate"] for s in snaps if s.get("change_rate") is not None]
            _sec_mkt_cache["sector"][sector_name] = (
                round(sum(chs) / len(chs), 2) if chs else None)
            _sec_mkt_cache.setdefault("snap", {}).update(
                {str(s.get("code")): s for s in snaps if s.get("code")})
        except Exception as exc:
            print(f"[extras] 族群 {sector_name} 平均計算失敗: {exc}", flush=True)
            _sec_mkt_cache["sector"][sector_name] = None
    return _sec_mkt_cache["sector"].get(sector_name), _sec_mkt_cache["market"]


def _latest_code_snap(code: str):
    """該檔最近交易日官方收盤 snapshot（含主動買賣量），batch 呼叫已被族群快取吸收。"""
    return (_sec_mkt_cache.get("snap") or {}).get(str(code))



def _five_factors(snap, chip, sector_avg, market_pct, sector_name):
    """五大因子(趨勢25/量能25/相對強度20/籌碼20/族群10)用最近可用資料計分。
    規範:個股數據一律抓最近日期;每項附計分理由小字,缺一補一、不整片放棄。"""
    F, N = {}, {}
    price, ma20 = snap.get("price"), snap.get("ma20")
    chg = snap.get("change_rate")
    vr = snap.get("volume_ratio")
    # 趨勢 25
    if price and ma20:
        above = float(price) >= float(ma20)
        F["trend"] = (25 if above and (chg or 0) > 0 else 15 if above else 0)
        N["trend"] = f"{'站上' if above else '跌破'}MA20({ma20}) → 趨勢分 {F['trend']}/25"
    else:
        F["trend"], N["trend"] = None, "MA20 未接入(盤前快取重建後恢復)"
    # 量能 25
    if vr is not None:
        v = float(vr)
        F["volume"] = 25 if v >= 1.5 else 18 if v >= 1.0 else 10 if v >= 0.8 else 5
        note = "放量" if v >= 1.5 else "量能正常" if v >= 1.0 else "量縮"
        if F["trend"] == 0 and F["volume"] > 5:
            F["volume"] = max(5, F["volume"] // 2)
            note += "＋破均線 → 量能打 5 折"
        N["volume"] = f"量比 {v:.2f}({note}) → 量能 {F['volume']}/25"
    else:
        F["volume"], N["volume"] = None, "量比未接入"
    # 相對強度 20
    if chg is not None and (sector_avg is not None or market_pct is not None):
        pts = 0
        parts = []
        if sector_avg is not None:
            d = float(chg) - float(sector_avg)
            pts += 12 if d >= 1.5 else 8 if d >= 0 else 3
            parts.append(f"vs 族群 {d:+.2f}pp")
        if market_pct is not None:
            d2 = float(chg) - float(market_pct)
            pts += 8 if d2 >= 1.0 else 5 if d2 >= 0 else 1
            parts.append(f"vs 大盤 {d2:+.2f}pp")
        F["rs"] = min(20, pts)
        N["rs"] = "｜".join(parts) + f" → 相對強度 {F['rs']}/20"
    else:
        F["rs"], N["rs"] = None, "族群/大盤官方資料未回補"
    # 籌碼 20
    f20 = chip.get("foreign_net_20d")
    streak = chip.get("inst_streak")
    m5 = chip.get("margin_change_5d")
    if f20 is not None or streak is not None:
        pts = 0
        parts = []
        if f20 is not None:
            pts += 10 if f20 > 0 else 0
            parts.append(f"法人近月{'買超' if f20 > 0 else '賣超'} {abs(int(f20)):,} 張")
        if streak is not None:
            pts += 5 if streak >= 3 else 2 if streak >= 1 else 0
            parts.append(f"法人連{'買' if streak >= 0 else '賣'} {abs(int(streak))} 日")
        if m5 is not None:
            pts += 5 if m5 <= 0 else 0
            parts.append(f"融資5日 {int(m5):+,} 張")
        F["chip"] = min(20, pts)
        N["chip"] = "｜".join(parts) + f" → 籌碼分 {F['chip']}/20"
    else:
        F["chip"], N["chip"] = None, "FinMind 法人資料未回補"
    # 族群 10
    if sector_avg is not None:
        F["sector"] = 8 if sector_avg > 0 else 3
        N["sector"] = (f"{sector_name}族群今日{'上漲' if sector_avg > 0 else '偏弱'}"
                       f"({sector_avg:+.2f}%) → 族群分 {F['sector']}/10")
    else:
        F["sector"], N["sector"] = None, "族群平均未回補"
    score = round(sum(v for v in F.values() if v is not None), 1)
    return {"factors": F, "notes": N, "score": score,
            "source": "最近交易日官方收盤＋FinMind 盤後籌碼",
            "missing": [k for k, v in F.items() if v is None]}


def _post_market_asof() -> tuple[str, bool]:
    """盤後資料的有效日期：每日 18:00 前固定看前一交易日。"""
    now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
    ready = now.hour >= 18
    limit = now.date() if ready else (now.date() - _dt.timedelta(days=1))
    return limit.isoformat(), ready


def _raw_rows() -> List[Dict[str, Any]]:
    """從 broker buffer 拿真實 snap、餵給 vps_intraday_test._row 計算 group/aflow。
    跟 /api/intraday-test endpoint 共用同一邏輯。"""
    try:
        raw = broker.raw_buffer_snapshots()
        return [VIT._row(item) for item in raw]
    except Exception:
        return []


# ── /api/stock/{code} ──────────────────────────────────────
def build_stock_card(code: str) -> Dict[str, Any]:
    """組單檔盤後固定卡片。

    這條路由刻意不呼叫 broker.raw_buffer_snapshots()；盤中即時資料只在
    /api/intraday-test 與 /api/intraday-watchpool 使用。卡片使用最近完整日 K、
    FinMind 盤後法人/融資，以及已落地的 eod_state 健康度。
    """
    asof_limit, official_ready = _post_market_asof()
    snap = None
    all_rows: List[Dict[str, Any]] = []
    post_health = None
    post_source = "盤後日K＋FinMind盤後資料"
    eod_flow = None
    eod_flow_date = None
    eod_group = None
    eod_quadrant = None
    post_ratio = None
    post_ratio_source = None
    post_health_row = None
    try:
        import sqlite3
        eod_db = _BASE / "intraday_eod.db"
        if eod_db.exists():
            with sqlite3.connect(eod_db) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM intraday_eod WHERE code=? AND trade_date<=? "
                    "ORDER BY trade_date DESC LIMIT 1", (str(code), asof_limit)
                ).fetchone()
            if row:
                eod_flow = row["aflow"]
                eod_flow_date = row["trade_date"]
                eod_group = row["group_name"]
                eod_quadrant = row["quadrant"]
    except Exception as exc:
        print(f"[extras] intraday_eod {code} 讀取失敗: {exc}", flush=True)
    # 舊版盤後資料庫的固定資料：ratio 是主動資金比，不冒充 net_active 差值。
    eod_candidates = [os.environ.get("MLS_EOD_DB"),
                      "/opt/mls-v4-new/app/data/mls.db",
                      str(_BASE / "mls.db")]
    for db_path in [p for p in eod_candidates if p and os.path.exists(p)]:
        try:
            import sqlite3
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM dec_health WHERE code=? AND trade_date<=? "
                    "ORDER BY trade_date DESC LIMIT 1", (str(code), asof_limit)
                ).fetchone()
            if row:
                post_health_row = dict(row)
                post_ratio = row["ratio"]
                post_ratio_source = row["ratio_src"]
                eod_quadrant = eod_quadrant or row["quad"]
                eod_group = eod_group or {"Ready": "可操作", "Watch": "觀察", "Hold": "排除"}.get(row["grade"])
                if not post_health:
                    post_health = {
                        "health_score": row["score"], "score": row["score"],
                        "quadrant": row["quad"], "label": row["grade"],
                        "stars": row["stars"], "chip_quality": row["chip_note"],
                    }
                post_source = f"盤後固定 DB dec_health（{row['trade_date']}）＋FinMind"
                break
        except Exception as exc:
            print(f"[extras] dec_health {db_path} 讀取失敗: {exc}", flush=True)
    try:
        import eod_state
        eod = eod_state.build(date=asof_limit)
        for item in eod.get("stocks") or []:
            if str(item.get("code")) == str(code):
                h = item.get("health") or {}
                if not post_health:
                    post_health = {
                        "health_score": h.get("health_score") or item.get("ai_score"),
                        "score": h.get("health_score") or item.get("ai_score"),
                        "quadrant": h.get("quadrant"), "label": h.get("label"),
                        "stars": h.get("stars"), "chip_quality": (item.get("chip") or {}).get("source"),
                    }
                    post_source = "mls.db 盤後固定資料＋FinMind盤後資料"
                break
    except Exception as exc:
        print(f"[extras] eod_state {code} 失敗: {exc}", flush=True)
    try:
        bars = stock_card._bars(code, days=80)
        valid = [b for b in bars
                 if b.get("close") is not None and str(b.get("date") or "")[:10] <= asof_limit]
        if valid:
            last = valid[-1]
            prev = valid[-2] if len(valid) > 1 else None
            close = float(last["close"])
            prev_close = float(prev["close"]) if prev else None
            snap = {
                "code": str(code), "price": close,
                "change_rate": round((close / prev_close - 1) * 100, 2)
                if prev_close else None,
                "high": last.get("high"), "low": last.get("low"),
                "avg_price": close, "total_volume": last.get("volume") or 0,
                "buy_volume": None, "sell_volume": None,
                "data_mode": "post_market_daily_kbar",
                "source_date": last.get("date"),
                "aflow": eod_flow,
                "aflow_ratio": post_ratio,
                "aflow_ratio_source": post_ratio_source,
                "aflow_source_date": eod_flow_date,
                "quadrant": eod_quadrant,
                "group": eod_group,
            }
            closes = [float(b["close"]) for b in valid[-20:]
                      if b.get("close") is not None]
            if len(closes) >= 20:
                snap["ma20"] = round(sum(closes[-20:]) / 20, 2)
            all_rows.append(snap)
    except Exception as exc:
        print(f"[extras] post kbar {code} 失敗: {exc}", flush=True)
    try:
        health = post_health or (_health_for_card(code, snap or {"code": code}, all_rows) if snap else None)
        grade_map = {"可操作": "Ready", "觀察": "Watch", "排除": "Hold"}
        grade = grade_map.get((snap or {}).get("group"))
        card = stock_card.build_card(code, snap=snap, health=health, grade=grade)
        card["decision"] = _decision_factors(card, snap or {})
    except Exception as e:
        return {"ok": False, "code": code, "error": f"build_card failed: {e}",
                "name": C.NAME_MAP.get(code, code),
                "sector": C.SECTOR_MAP.get(code, ("其他",))[0]}
    # 規範:個股數據一律抓最近日期計算，族群/大盤/相對強度不得「不計算」。
    snap = snap or {}
    try:
        _sec_name = C.SECTOR_MAP.get(code, ("其他",))[0]
        _sec_avg, _mkt_pct = _sector_market_pct(_sec_name)
        _own = _latest_code_snap(code) or {}
        if snap.get("aflow") is None and _own.get("buy_volume") is not None:
            snap["aflow"] = int(_own.get("buy_volume") or 0) - int(_own.get("sell_volume") or 0)
            snap["aflow_source"] = "Shioaji 官方收盤 snapshot(最近交易日)"
        if snap.get("volume_ratio") in (None, 0) and _own.get("volume_ratio"):
            snap["volume_ratio"] = _own.get("volume_ratio")
        snap.setdefault("sector_avg", _sec_avg)
        snap.setdefault("market_pct", _mkt_pct)
        _chg = snap.get("change_rate")
        if snap.get("vs_sector") is None and _chg is not None and _sec_avg is not None:
            snap["vs_sector"] = round(float(_chg) - float(_sec_avg), 2)
        if _chg is not None and _mkt_pct is not None:
            snap.setdefault("vs_market", round(float(_chg) - float(_mkt_pct), 2))
        snap.setdefault("rel_source", "族群=固定池成分股官方收盤平均；大盤=TWSE 官方")
        card["factors5"] = _five_factors(snap, card.get("chip") or {},
                                         _sec_avg, _mkt_pct, _sec_name)
        card["decision"] = _decision_factors(card, snap)
    except Exception as exc:
        print(f"[extras] 相對強弱補值失敗: {exc}", flush=True)
    data_date = (snap or {}).get("source_date") or eod_flow_date
    status = ("今日官方盤後資料已更新" if official_ready and data_date == _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).date().isoformat()
              else "等待今日 18:00 官方更新，目前顯示前一交易日資料")
    return {"ok": True, "code": code, "updated_at": _now_tw(),
            "data_date": data_date or asof_limit,
            "data_timestamp": _now_tw(),
            "official_ready": official_ready,
            "data_status": status,
            "official_update_rule": "每日 18:00 後才切換當日官方盤後資料",
            "card": card, "name": C.NAME_MAP.get(code, code),
            "sector": C.SECTOR_MAP.get(code, ("其他",))[0],
            "post_market": snap or {},
            "data_source": post_source,
            "intraday": {"available": False, "note": "盤中即時欄位：等收盤驗證；卡片只讀盤後固定資料"}}


# ── /api/report ────────────────────────────────────────────
def build_report() -> Dict[str, Any]:
    """今日 / 昨日的盤後報告 — 從 broker buffer 拿 51 檔真實 rows。"""
    rows = _raw_rows()
    groups = {"可操作": 0, "觀察": 0, "排除": 0}
    for r in rows:
        g = r.get("group", "")
        if g in groups:
            groups[g] += 1
    return {
        "ok": True,
        "updated_at": _now_tw(),
        "asof_date": _dt.datetime.utcnow().strftime("%Y-%m-%d"),
        "rows": rows,
        "groups": groups,
        "count": len(rows),
        "note": "mls-intraday 盤中即時觀察池摘要；盤後正式報告由 after_hours 模組補完",
    }


# ── /api/watchpool ────────────────────────────────────────
def build_watchpool() -> Dict[str, Any]:
    """51 檔觀察池全集 — 從 broker buffer 抓真實 rows，
    沒回報的檔用 config 補上 name/sector。"""
    rows_map = {str(r.get("code", "")): r for r in _raw_rows()}
    try:
        subs = set(getattr(broker, "_SUBSCRIBED", set())) or set(C.UNIVERSE)
    except Exception:
        subs = set(C.UNIVERSE)

    # 盤後固定資金比只作為 aflow 缺少蓋章時的明確替代欄位，
    # 不改名冒充主動買賣差。
    ratio_map: Dict[str, Dict[str, Any]] = {}
    for db_path in [os.environ.get("MLS_EOD_DB"),
                    "/opt/mls-v4-new/app/data/mls.db",
                    str(_BASE / "mls.db")]:
        if not db_path or not os.path.exists(db_path):
            continue
        try:
            import sqlite3
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                for row in conn.execute(
                    "SELECT code, ratio, ratio_src, trade_date FROM dec_health"
                ).fetchall():
                    ratio_map[str(row["code"])] = {
                        "aflow_ratio": row["ratio"],
                        "aflow_ratio_source": row["ratio_src"],
                        "aflow_ratio_date": row["trade_date"],
                    }
            break
        except Exception as exc:
            print(f"[extras] watchpool dec_health 讀取失敗: {exc}", flush=True)

    items: List[Dict[str, Any]] = []
    for code in C.UNIVERSE:
        snap = rows_map.get(str(code), {})
        ratio = ratio_map.get(str(code), {})
        # 固定觀察池不能因 Shioaji 未回報就顯示空白；盤中回報優先，
        # 沒有即時回報時回退到該股最近完整日 K。這裡只補價格欄位，
        # 不把日 K 冒充盤中 aflow 或盤中分類。
        if not snap.get("price"):
            try:
                bars = [b for b in stock_card._bars(str(code), days=3)
                        if b.get("close") is not None]
                if bars:
                    last = bars[-1]
                    prev = bars[-2] if len(bars) > 1 else None
                    close = float(last["close"])
                    prev_close = float(prev["close"]) if prev else None
                    snap = {
                        **snap,
                        "price": close,
                        "change_rate": round((close / prev_close - 1) * 100, 2)
                        if prev_close else None,
                        "high": last.get("high"),
                        "low": last.get("low"),
                        "total_volume": last.get("volume"),
                        "source_date": last.get("date"),
                        "data_mode": "post_market_daily_kbar",
                    }
            except Exception as exc:
                print(f"[extras] watchpool {code} 日K回退失敗: {exc}", flush=True)
        items.append({
            "code": code,
            "name": C.NAME_MAP.get(code, code),
            "sector": C.SECTOR_MAP.get(code, ("其他",))[0],
            "track": "engine" if code in getattr(C, "ENGINE_STOCKS", set()) else "attack",
            "subscribed": code in subs,
            "price": snap.get("price"),
            "change_rate": snap.get("change_rate"),
            "aflow": snap.get("aflow"),
            "aflow_ratio": ratio.get("aflow_ratio"),
            "aflow_ratio_source": ratio.get("aflow_ratio_source"),
            "aflow_ratio_date": ratio.get("aflow_ratio_date"),
            "group": snap.get("group"),
            "volume_ratio": snap.get("volume_ratio"),
            "has_data": bool(snap.get("price")),
            "data_mode": snap.get("data_mode") or ("intraday_shioaji" if snap.get("price") else None),
            "source_date": snap.get("source_date"),
        })
    return {
        "ok": True,
        "updated_at": _now_tw(),
        "count": len(items),
        "items": items,
    }
