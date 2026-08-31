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
from tw_price_limit import is_limit_up


def _read_intraday_daily(code: str, trade_date: str, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """讀取指定交易日已落地的盤中 VWAP／高低點；沒有就保留缺值。"""
    import sqlite3
    path = Path(db_path or (_ROOT / "intraday_eod.db"))
    if not trade_date or not path.exists():
        return {}
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT avg_price, high, low, aflow, volume, volume_ratio "
                "FROM intraday_stock_daily WHERE trade_date=? AND code=?",
                (trade_date, str(code)),
            ).fetchone()
        return dict(row) if row else {}
    except Exception as exc:
        print(f"[extras] intraday_stock_daily {code} 讀取失敗: {exc}", flush=True)
        return {}


def _sector_members(sector: str) -> List[Dict[str, str]]:
    """回傳族群平均採用的固定觀察池成分，讓前端可逐檔稽核。"""
    return [
        {"code": str(code), "name": C.NAME_MAP.get(str(code), str(code))}
        for code, meta in sorted(C.SECTOR_MAP.items())
        if meta[0] == sector
    ]


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
        src["avg_price"] = snap.get("avg_price")
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
    raw_score = round(sum(v["points"] or 0 for v in factors.values()), 1)
    score = round(raw_score / available * 100, 1) if available else None
    missing = [k for k, v in factors.items() if v["points"] is None]
    confidence = "High" if not missing else ("Medium" if available >= 50 else "Low")
    return {"factors": factors, "score": score, "score_raw": raw_score, "score_max": 100,
            "score_available": available,
            "missing": missing, "confidence": confidence,
            "rule": "只以已取得因子正規化計分；缺資料降低可信度，不當作 0 分。"}


def _now_tw() -> str:
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")


def _is_intraday_session(now: Optional[_dt.datetime] = None) -> bool:
    """是否處於台股盤中；盤中不能把昨日收盤冒充今日現價。"""
    tz = _dt.timezone(_dt.timedelta(hours=8))
    now = now.astimezone(tz) if now else _dt.datetime.now(tz)
    return now.weekday() < 5 and _dt.time(9, 0) <= now.time() < _dt.time(13, 35)


def _merge_intraday_quote(code: str, snap: Dict[str, Any]) -> Dict[str, Any]:
    """盤中以最新推播覆蓋收盤快照；取不到即保留收盤資料並標示原狀態。"""
    if not _is_intraday_session():
        return snap
    try:
        live_rows = broker.buffer_snapshots([str(code)])
        live = next((r for r in live_rows if str(r.get("code")) == str(code)), None)
    except Exception as exc:
        print(f"[extras] 盤中行情 {code} 讀取失敗:{exc}", flush=True)
        live = None
    if not live or live.get("price") is None or live.get("change_rate") is None:
        return snap

    merged = dict(snap)
    for key in ("price", "change_rate", "high", "low", "avg_price",
                "volume_ratio", "total_volume", "total_amount",
                "buy_volume", "sell_volume"):
        if live.get(key) is not None:
            merged[key] = live[key]
    merged["eod_close"] = snap.get("price")
    merged["eod_change_rate"] = snap.get("change_rate")
    merged["data_mode"] = "intraday_shioaji"
    merged["source_date"] = _dt.datetime.now(
        _dt.timezone(_dt.timedelta(hours=8))).date().isoformat()
    merged["price_source"] = "Shioaji 即時推播"
    merged["intraday_available"] = True
    return merged


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
            # 大盤%要鎖「最近有資料的交易日」，不能只問今天(休市/未公布會回 None)。
            _base = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
            _mpct = None
            for _back in range(0, 7):
                _dd = _base - _dt.timedelta(days=_back)
                if _dd.weekday() >= 5:      # 週末直接跳過
                    continue
                _mi = _O.market_index(_dd) or {}
                if _mi.get("change_pct") is not None:
                    _mpct = _mi.get("change_pct")
                    break
            _sec_mkt_cache["market"] = _mpct
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
    # 資料日期＝這份盤後資料本身的交易日(snap.source_date)，不是「現在」。
    # 盤中(今日尚未收盤)時 source_date 仍是最近已收盤日，避免蓋上未收盤的今日戳章。
    data_date = (snap or {}).get("source_date") \
        or _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).date().isoformat()
    return {"factors": F, "notes": N, "score": score,
            "source": "最近交易日官方收盤＋FinMind 盤後籌碼",
            "data_date": data_date,
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


# ── 個股卡片快取 ──────────────────────────────────────────
# 卡片吃的是盤後固定資料（收盤日K＋FinMind＋已落地健康度），同一個交易日
# 算出來的結果不會變。但每次 systemd 重啟後第一次點個股都要重跑 Shioaji
# 登入＋日K 抓取，實測要 ~40 秒，使用者體感就是「點不開」。
# 因此：同一 (code, 盤後有效日) 只算一次，並寫到磁碟，重啟後直接沿用。
_CARD_MEM: Dict[str, Dict[str, Any]] = {}
_CARD_DIR = _BASE / "card_cache"
_CARD_CACHE_VERSION = 2  # v2: 正確 VWAP、週期籌碼、可用分母計分與族群成分


def _card_cache_path(code: str, asof: str) -> Path:
    return _CARD_DIR / f"{asof}_{code}.json"


def _card_cache_read(code: str, asof: str) -> Optional[Dict[str, Any]]:
    key = f"{asof}_{code}"
    hit = _CARD_MEM.get(key)
    if hit is not None:
        return hit
    path = _card_cache_path(code, asof)
    if not path.exists():
        return None
    try:
        import json
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("_card_cache_version") != _CARD_CACHE_VERSION:
            return None
        _CARD_MEM[key] = data
        return data
    except Exception as exc:
        print(f"[extras] card cache 讀取失敗 {path.name}: {exc}", flush=True)
        return None


def _card_cache_write(code: str, asof: str, payload: Dict[str, Any]) -> None:
    payload = dict(payload)
    payload["_card_cache_version"] = _CARD_CACHE_VERSION
    _CARD_MEM[f"{asof}_{code}"] = payload
    try:
        import json
        _CARD_DIR.mkdir(exist_ok=True)
        path = _card_cache_path(code, asof)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        tmp.replace(path)
        # 只留最近 3 個盤後日的檔，不無限長大
        keep = sorted({p.name[:10] for p in _CARD_DIR.glob("*.json")})[-3:]
        for old in _CARD_DIR.glob("*.json"):
            if old.name[:10] not in keep:
                old.unlink(missing_ok=True)
    except Exception as exc:
        print(f"[extras] card cache 寫入失敗 {code}: {exc}", flush=True)


def _card_cache_stale(code: str) -> Optional[Dict[str, Any]]:
    """當日資料算不出來時，退回這檔最近一次算好的卡片，不讓畫面開天窗。"""
    try:
        files = sorted(_CARD_DIR.glob(f"*_{code}.json"))
        if not files:
            return None
        import json
        with files[-1].open(encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


# ── /api/stock/{code} ──────────────────────────────────────
def build_stock_card(code: str, refresh: bool = False) -> Dict[str, Any]:
    """對外入口：優先吃快取，沒有才真的重算（見 _build_stock_card）。"""
    asof_limit, _ready = _post_market_asof()
    code = str(code)
    # 盤中不可命中盤後卡片快取，否則會把昨日收盤顯示成今日現價。
    if not refresh and not _is_intraday_session():
        cached = _card_cache_read(code, asof_limit)
        if cached is not None:
            cached = dict(cached)
            cached["cache"] = {"hit": True, "asof": asof_limit,
                               "note": "盤後固定資料，當日只計算一次"}
            return cached
    try:
        result = _build_stock_card(code)
    except Exception as exc:
        stale = _card_cache_stale(code)
        if stale is not None:
            stale = dict(stale)
            stale["cache"] = {"hit": True, "stale": True, "error": str(exc),
                              "note": "本次重算失敗，顯示最近一次已算好的卡片"}
            return stale
        raise
    if result.get("ok"):
        _card_cache_write(code, asof_limit, result)
        result = dict(result)
        result["cache"] = {"hit": False, "asof": asof_limit}
    return result


def _build_stock_card(code: str) -> Dict[str, Any]:
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
    quad_hist = None
    _hist = None
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
    # 盤後 dec_health 正本在 mls-v4 的 docker volume(每交易日 15:05 由 after_hours 落地)。
    # /opt/mls-v4-new/app/data/mls.db 是未掛載的舊殼(凍在 07-21)，只能墊底。
    eod_candidates = [os.environ.get("MLS_EOD_DB"),
                      "/var/lib/docker/volumes/mls-v4-new_mls-v4-data/_data/mls.db",
                      "/opt/mls-v4-new/app/data/mls.db",
                      str(_BASE / "mls.db")]
    for db_path in [p for p in eod_candidates if p and os.path.exists(p)]:
        try:
            import sqlite3
            # 唯讀開啟：本行程只讀不寫，避免與容器寫入互鎖。
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                # 不同版本的盤後 DB 欄位名稱不同：
                # 舊版使用 quad/ratio，VPS 本地正本使用
                # quadrant/aflow_ratio。統一轉成下方使用的別名，
                # 避免五日歷史整段查詢失敗後只剩當天補值。
                _cols = {r[1] for r in conn.execute("PRAGMA table_info(dec_health)").fetchall()}
                _quad_col = "quad" if "quad" in _cols else "quadrant"
                _ratio_col = "ratio" if "ratio" in _cols else "aflow_ratio"
                _ratio_src_col = "ratio_src" if "ratio_src" in _cols else "flow_src"
                row = conn.execute(
                    f"SELECT *, {_quad_col} AS _quad, {_ratio_col} AS _ratio, "
                    f"{_ratio_src_col} AS _ratio_src FROM dec_health WHERE code=? AND trade_date<=? "
                    "ORDER BY trade_date DESC LIMIT 1", (str(code), asof_limit)
                ).fetchone()
                # 近5日收盤資金象限紀錄(盤後每日定調，不是盤中即時)
                _hist = conn.execute(
                    f"SELECT trade_date, {_quad_col} AS quad, chg, close FROM dec_health WHERE code=? "
                    "AND trade_date<=? ORDER BY trade_date DESC LIMIT 5",
                    (str(code), asof_limit)).fetchall()
            # mls-v4 若 Shioaji 斷線，snapshot() 會回 demo 假資料(每日同一價、chg 固定)，
            # 會污染象限/分數判斷。指紋：近日收盤價完全不變＝demo，整包棄用不當真實。
            _closes = [h["close"] for h in (_hist or []) if h["close"] is not None]
            _demo = len(_closes) > 1 and len(set(_closes)) <= 1
            if _demo:
                print(f"[extras] dec_health {code} 判定為 demo 假資料(每日同價)，棄用",
                      flush=True)
                _hist = None
                row = None
            if _hist and quad_hist is None:
                quad_hist = [{"date": h["trade_date"], "quadrant": h["quad"],
                              "chg": h["chg"]} for h in _hist]
            if row:
                post_health_row = dict(row)
                post_ratio = row["_ratio"]
                post_ratio_source = row["_ratio_src"]
                eod_quadrant = eod_quadrant or row["_quad"]
                eod_group = eod_group or {"Ready": "可操作", "Watch": "觀察", "Hold": "排除"}.get(row["grade"])
                if not post_health:
                    post_health = {
                        "health_score": row["score"], "score": row["score"],
                        "quadrant": row["_quad"], "label": row["grade"],
                        "stars": row["stars"] if "stars" in row.keys() else None,
                        "chip_quality": row["chip_note"],
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
            daily = _read_intraday_daily(code, str(last.get("date") or "")[:10])
            snap = {
                "code": str(code), "price": close,
                "prev_close": prev_close,
                "change_rate": round((close / prev_close - 1) * 100, 2)
                if prev_close else None,
                "high": daily.get("high") or last.get("high"),
                "low": daily.get("low") or last.get("low"),
                "avg_price": daily.get("avg_price"),
                "total_volume": daily.get("volume") or last.get("volume") or 0,
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
            if daily.get("volume_ratio") is not None:
                snap["volume_ratio"] = daily["volume_ratio"]
            closes = [float(b["close"]) for b in valid[-20:]
                      if b.get("close") is not None]
            snap["volume_history"] = [
                {"date": b.get("date"), "close": b.get("close"),
                 "volume": b.get("volume")}
                for b in valid[-30:] if b.get("volume") is not None
            ]
            if len(closes) >= 20:
                snap["ma20"] = round(sum(closes[-20:]) / 20, 2)
            snap = _merge_intraday_quote(code, snap)
            all_rows.append(snap)
    except Exception as exc:
        print(f"[extras] post kbar {code} 失敗: {exc}", flush=True)
    try:
        health = post_health or (_health_for_card(code, snap or {"code": code}, all_rows) if snap else None)
        grade_map = {"可操作": "Ready", "觀察": "Watch", "排除": "Hold"}
        grade = grade_map.get((snap or {}).get("group"))
        card = stock_card.build_card(code, snap=snap, health=health, grade=grade,
                                     chip_asof=asof_limit)
        card["decision"] = _decision_factors(card, snap or {})
    except Exception as e:
        return {"ok": False, "code": code, "error": f"build_card failed: {e}",
                "name": C.NAME_MAP.get(code, code),
                "sector": C.SECTOR_MAP.get(code, ("其他",))[0]}
    # 規範:個股數據一律抓最近日期計算，族群/大盤/相對強度不得「不計算」。
    snap = snap or {}
    if quad_hist:
        snap["quad_history"] = quad_hist          # 近5日收盤資金象限(盤後定調)
    try:
        _sec_name = C.SECTOR_MAP.get(code, ("其他",))[0]
        _sec_avg, _mkt_pct = _sector_market_pct(_sec_name)
        _own = _latest_code_snap(code) or {}
        # 不可用 snapshots 的 buy_volume/sell_volume 代替主動買賣差：
        # broker.batch_snapshots() 的兩欄是委買/委賣掛單量，漲停股會把
        # 巨量委買誤報成 aflow。卡片只接受盤中 eod 蓋章的真實 tick aflow；
        # 沒有就顯示缺資料，不把委買量偽裝成全日主動買賣差。
        if snap.get("aflow") is None:
            snap["aflow_source"] = None
        if snap.get("volume_ratio") in (None, 0) and _own.get("volume_ratio"):
            snap["volume_ratio"] = _own.get("volume_ratio")
        # dec_health 被判 demo 棄用時(quadrant/quad_history 為空)，用「真實 aflow＋真實收盤
        # 漲跌」現算當日象限，至少當天真實，不假造歷史。in/out=資金流向、up/down=收盤漲跌。
        _af, _cr = snap.get("aflow"), snap.get("change_rate")
        if snap.get("quadrant") is None and _af is not None and _cr is not None:
            _q = ("in_" if _af >= 0 else "out_") + ("up" if _cr >= 0 else "down")
            snap["quadrant"] = _q
            if not snap.get("quad_history"):
                snap["quad_history"] = [{"date": snap.get("source_date"),
                                         "quadrant": _q, "chg": _cr}]
        snap.setdefault("sector_avg", _sec_avg)
        snap.setdefault("market_pct", _mkt_pct)
        _chg = snap.get("change_rate")
        if snap.get("vs_sector") is None and _chg is not None and _sec_avg is not None:
            snap["vs_sector"] = round(float(_chg) - float(_sec_avg), 2)
        if _chg is not None and _mkt_pct is not None:
            snap.setdefault("vs_market", round(float(_chg) - float(_mkt_pct), 2))
        snap.setdefault("rel_source", "族群=固定池成分股官方收盤平均；大盤=TWSE 官方")
        card["sector_members"] = _sector_members(_sec_name)
        card["sector_aggregation"] = "固定觀察池成分股等權平均"
        # 相對強弱的資料日＝這份盤後 K 線的交易日，不是「現在」。未收盤的今日不得蓋章。
        snap.setdefault("rel_date", snap.get("source_date") or asof_limit)
        card["factors5"] = _five_factors(snap, card.get("chip") or {},
                                         _sec_avg, _mkt_pct, _sec_name)
        card["decision"] = _decision_factors(card, snap)
    except Exception as exc:
        print(f"[extras] 相對強弱補值失敗: {exc}", flush=True)
    data_date = (snap or {}).get("source_date") or eod_flow_date
    status = ("今日盤中即時資料" if (snap or {}).get("intraday_available") else
              "今日官方盤後資料已更新" if official_ready and data_date == _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).date().isoformat()
              else "等待今日 18:00 官方更新，目前顯示前一交易日資料")
    intraday_live = bool((snap or {}).get("intraday_available"))
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
            "intraday": {"available": intraday_live,
                         "note": "盤中即時行情已接入" if intraday_live
                         else "非盤中或即時行情不可用；卡片顯示盤後固定資料"}}


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
        # 盤後籌碼快取（法人20日淨/連買天數）；第一層 UI 法人欄用，
        # 缺快取時回 None，前端顯示待盤後資料。
        chip = VIT._chip_snapshot(str(code))
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
        inst_daily = (sum(chip[k] for k in ("foreign_net_d", "trust_net_d", "dealer_net_d"))
                      if all(chip.get(k) is not None for k in
                             ("foreign_net_d", "trust_net_d", "dealer_net_d")) else None)
        inst_5d = (sum(chip[k] for k in ("foreign_net_5d", "trust_net_5d", "dealer_net_5d"))
                   if all(chip.get(k) is not None for k in
                          ("foreign_net_5d", "trust_net_5d", "dealer_net_5d")) else None)
        items.append({
            "code": code,
            "name": C.NAME_MAP.get(code, code),
            "sector": C.SECTOR_MAP.get(code, ("其他",))[0],
            "track": "engine" if code in getattr(C, "ENGINE_STOCKS", set()) else "attack",
            "subscribed": code in subs,
            "price": snap.get("price"),
            "change_rate": snap.get("change_rate"),
            # 盤中 PA overlay 需要知道價格是否已觸及漲停；量能確認仍由
            # candidate_pool 的原始快照獨立提供，不在這裡合併。
            "is_limit_up": is_limit_up(snap.get("price"),
                                        change_rate=snap.get("change_rate")),
            "aflow": snap.get("aflow"),
            "aflow_ratio": ratio.get("aflow_ratio"),
            "aflow_ratio_source": ratio.get("aflow_ratio_source"),
            "aflow_ratio_date": ratio.get("aflow_ratio_date"),
            "group": snap.get("group"),
            # 外資判斷直接沿用盤後 FinMind/官方快取；盤中不重新打 API。
            # 這些欄位也讓第一層在 PA snapshot 尚未補齊時仍能看見最新外資事實。
            "foreign_net_d": chip.get("foreign_net_d"),
            "foreign_net_20d": chip.get("foreign_net_20d"),
            "inst_net_d_lots": inst_daily,
            "inst_net_5d_lots": inst_5d,
            "inst_net_20d_lots": chip.get("inst_net_20d_lots"),
            "foreign_source": chip.get("source"),
            "foreign_source_date": chip.get("source_date"),
            "inst_streak": chip.get("inst_streak"),
            "volume_ratio": snap.get("volume_ratio"),
            "has_data": bool(snap.get("price")),
            "data_mode": snap.get("data_mode") or ("intraday_shioaji" if snap.get("price") else None),
            "source_date": snap.get("source_date"),
        })
    # Pre-Activation 四階段：盤後由 AB 引擎算好、存 candidate_pool。
    # 這裡唯讀併入，第一層 UI 只負責印，不自己重算。
    pa_date, pa_n = VIT._attach_pre_activation(items)
    foreign_rows = [x for x in items if x.get("foreign_source_date")]
    foreign_dates = sorted({x["foreign_source_date"] for x in foreign_rows})
    foreign_sources = sorted({x["foreign_source"] for x in foreign_rows
                              if x.get("foreign_source")})
    return {
        "ok": True,
        "updated_at": _now_tw(),
        "pre_activation_date": pa_date,
        "pre_activation_count": pa_n,
        "foreign_cache": {
            "covered": len(foreign_rows),
            "total": len(items),
            "source_dates": foreign_dates,
            "sources": foreign_sources,
            "note": "盤中只讀最新完成交易日的法人快取，不代表今日盤中法人流向",
        },
        "count": len(items),
        "items": items,
    }
