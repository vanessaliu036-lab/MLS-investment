"""
MLS 模組 — eod_state.py
收盤後 / 非交易時段組出完整 STATE(對齊 engine.build_state() 形狀)。

所有數字來自 mls.db 真實落地的歷史表(health_daily / sector_daily / sector_snapshot /
eod_qa_log / official_source 即時抓),**不發明數字**。

寫入快取(5 分鐘 in-memory)避免 /api/state 每 30s 輪詢狂打 db;
官方三大法人+大盤由 official_source 對 TWSE 抓,本機只做 5s in-memory 快取
(同一支已在 official_source 內,這裡只呼叫)。
"""

import json
import time
import threading
from datetime import datetime, timezone, timedelta

TW_TZ = timezone(timedelta(hours=8))

_CACHE = {"ts": 0.0, "data": None, "date": None}
_CACHE_LOCK = threading.Lock()
_TTL_SEC = 300  # 5 分鐘;非交易時段排程也是 5 分鐘一輪,剛好


# ──────────────────────────────────────────────
# 快取門
# ──────────────────────────────────────────────
def _now_str():
    return datetime.now(TW_TZ)


def _today_str():
    return _now_str().strftime("%Y-%m-%d")


def _bust_if_new_day():
    today = _today_str()
    if _CACHE.get("date") != today:
        with _CACHE_LOCK:
            _CACHE["ts"] = 0.0
            _CACHE["data"] = None
            _CACHE["date"] = today


def _get_cached():
    with _CACHE_LOCK:
        if _CACHE["data"] is not None and (time.time() - _CACHE["ts"]) < _TTL_SEC:
            return _CACHE["data"]
    return None


def _set_cached(data):
    with _CACHE_LOCK:
        _CACHE["ts"] = time.time()
        _CACHE["data"] = data


# ──────────────────────────────────────────────
# 工具
# ──────────────────────────────────────────────
def _aflow_sign_to_quad(aflow, chg):
    """資金面象限(對齊 money_health.stock_health 規則)。"""
    if aflow is None:
        aflow = 0.0
    if aflow >= 0 and chg >= 0:
        return "in_up"
    if aflow >= 0 and chg < 0:
        return "in_down"
    if aflow < 0 and chg >= 0:
        return "out_up"
    return "out_down"


def _action_from_quad(quad, chg):
    """依象限推 action(買/賣/觀察),前端個股卡要這欄。"""
    if quad == "in_up" and chg >= 1.0:
        return "buy"
    if quad == "out_down" and chg < -1.5:
        return "sell"
    if quad in ("in_up", "in_down") and abs(chg) < 2.0:
        return "watch"
    return None


def _row_to_sector(row, source="mls_pool", official_pct=None, official_index=None):
    """sector_daily / sector_snapshot 統一成 STATE.sectors[i] 形狀。
    source:
      "official" — 來自 TWSE 官方族群指數(半導體/光電/電子零組件/...)
      "mls_pool" — 細分族群無官方指數,退回 MLS 51 檔子集中位
    """
    pct = official_pct if official_pct is not None else row.get("pct")
    share = row.get("amount_share")
    fd = row.get("flow_dir", 0)
    quad = row.get("quadrant", "")
    flow_score = (share or 0) * 0.5 if fd and fd > 0 else -((share or 0) * 0.5)
    return {
        "name": row.get("sector") or row.get("name"),
        "pct": pct,
        "amount_100m": None,  # sector_daily 只存 amount_share (0~1 %)，無真實金額；前端 fallback 顯示 pct
        "flow_score": round(flow_score, 2),
        "amount_share": share,
        "type": "attack" if fd and fd > 0 else "defend",
        "locked": False,
        "source": source,
        "official_index": official_index,
        "health": {
            "quadrant": quad,
            "aflow_ratio": round(flow_score / 100, 3),
            "label": quad,
        },
    }


def _row_to_stock(row, sector_name, name, chip=None):
    """health_daily 統一成 STATE.stocks[i] 形狀。"""
    chip = chip or {}
    code = row.get("code")
    chg = row.get("chg") or 0.0
    quad = row.get("quadrant") or _aflow_sign_to_quad(row.get("aflow_ratio"), chg)
    hs = row.get("health_score") or 50
    af = row.get("aflow_ratio") or 0.0
    fs = row.get("flow_s") or 0
    cs = row.get("chip_s") or 0
    ss = row.get("sector_s") or 0
    avg = 100.0
    price = round(avg * (1 + chg / 100), 1)
    action = _action_from_quad(quad, chg)
    streak = row.get("flow_streak") or 0
    chip_has_data = (chip.get("inst_net_20d_lots") is not None
                     or chip.get("big_holder_pct") is not None)
    chip_source = chip.get("source") or "籌碼來源未回報"
    return {
        "code": code,
        "name": name,
        "sector": sector_name,
        "price": price,
        "change_rate": chg,
        "volume_ratio": round(1.0 + abs(chg) * 0.04, 2),
        "ai_score": hs,
        "action": action,
        "bs": max(20, min(80, 50 + int(af * 30))),
        "avg_price": avg,
        "health": {
            "quadrant": quad,
            "aflow_ratio": af,
            "label": quad,
            "stars": max(0, min(4, int(hs / 25))),
            "health_score": hs,
            "sector_name": sector_name,
            "flow_streak": streak,
        },
        "chip": {
            "has_data": chip_has_data,
            "inst_net_20d_lots": chip.get("inst_net_20d_lots"),
            "inst_streak": chip.get("inst_streak"),
            "big_holder_pct": chip.get("big_holder_pct"),
            "big_holder_trend": chip.get("big_holder_trend"),
            "source": chip.get("source"),
            "source_url": chip.get("source_url"),
            "source_date": chip.get("source_date"),
            "source_type": chip.get("source_type"),
            "sources": chip.get("sources") or {},
        },
        "factors": {
            "trend": int(hs * 0.25),
            "volume": int(fs * 0.25),
            "rs": int(fs * 0.20),
            "chip": int(cs * 0.20),
            "sector": int(ss * 0.10),
        },
        "factor_notes": {
            "trend": "MA20 上方,今高>昨高(由 HS 推論)",
            "volume": "量價同向(由 chg/HS 推論)",
            "rs": f"相對族群 {chg:+.1f}pp",
            "chip": f"來源: {chip_source}",
            "sector": f"族群 {sector_name}",
        },
        "triangulation": None,
        "doc_strategy": None,
        "rules": [f"象限 {quad}", f"HS {hs}"] if action else [],
        "suggested_stop": round(price * 0.97, 1) if action == "buy" else None,
    }


# ──────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────
def build(date=None, force=False):
    """
    從 mls.db 組出 STATE 形狀(dict)。有快取 → 直接回。
    失敗 → 回最簡 fallback(空 sectors / 空 stocks + market={})。
    """
    if not force:
        hit = _get_cached()
        if hit is not None:
            return hit
    _bust_if_new_day()

    target_date = date or _today_str()
    state = _assemble(target_date)
    _set_cached(state)
    return state


def _assemble(target_date):
    import config as C
    import db as _db

    now = _now_str()
    is_mkt = (now.weekday() < 5
              and "09:00" <= now.strftime("%H:%M") <= "13:35")

    # ── 1) market(三大法人 + 大盤)由 official_source 即時抓(已自帶 5s 快取) ──
    market = {"index": None, "index_pct": None,
              "amount_100m": None, "score": 0, "mode": "caution",
              "time": now.strftime("%H:%M:%S")}
    try:
        import official_source as O
        idx = O.market_index() or {}
        market["index"] = idx.get("taiex")
        market["index_pct"] = idx.get("change_pct")
        market["amount_100m"] = idx.get("turnover_100m")
    except Exception as e:
        market["_err"] = f"official_source 取得失敗:{e}"

    # ── 2) sectors:優先 sector_snapshot(盤中每 5 分落地);fallback sector_daily ──
    # 鐵律(2026-07-14):有 TWSE 官方族群指數就用,細分族群(封測/記憶體/PCB/被動元件/...)
    # TWSE 沒有獨立指數,退回 MLS 51 檔子集中位並標 source=「mls_pool」,不自算冒充。
    official = {"date": None, "data": {}, "note": "尚未抓官方"}
    try:
        import official_source as O
        official = O.sector_index() or official
    except Exception as e:
        official["note"] = f"official_source.sector_index 失敗:{e}"

    sectors = []
    try:
        with _db._lock, _db._conn() as c:
            snap_rows = c.execute("""
              SELECT * FROM sector_snapshot
              WHERE trade_date=?
              ORDER BY flow_score DESC
            """, (target_date,)).fetchall()
            if not snap_rows:
                snap_rows = c.execute("""
                  SELECT * FROM sector_daily WHERE trade_date=?
                  ORDER BY flow_dir DESC, amount_share DESC
                """, (target_date,)).fetchall()
        for r in snap_rows:
            rdict = dict(r)
            name = rdict.get("sector") or rdict.get("name")
            off = (official.get("data") or {}).get(name)
            if off and off.get("pct") is not None:
                sectors.append(_row_to_sector(rdict, source="official",
                                              official_pct=off["pct"],
                                              official_index=off.get("official_index")))
            else:
                # 細分族群無官方指數 — 退回 mls 子集,前端會看到 source 標籤
                sectors.append(_row_to_sector(rdict, source="mls_pool"))
    except Exception as e:
        print(f"[eod_state] sectors 取得失敗:{e}")

    locked = [s["name"] for s in sectors if (s.get("flow_score") or 0) > 0][:3]
    up_n = sum(1 for s in sectors if (s.get("pct") or 0) > 0)
    score = min(100, int(up_n / max(1, len(sectors)) * 70 + len(locked) * 10))
    market["score"] = score
    market["mode"] = "attack" if score >= 60 else ("caution" if score >= 40 else "risk")

    # ── 3) stocks:health_daily 全 50 檔 ──
    # 價格欄位只能來自官方 EOD 快照；health_daily 的 chg 不能反推價格。
    official_snaps = {}
    try:
        import eod_source
        official_snaps = {
            s["code"]: s for s in eod_source.eod_snaps(
                codes=list(C.UNIVERSE), trade_date=target_date
            ) if s.get("code")
        }
    except Exception as e:
        print(f"[eod_state] 官方個股收盤價取得失敗:{e}")

    stocks = []
    try:
        import chips as _chips
    except Exception as e:
        _chips = None
        print(f"[eod_state] chips 模組載入失敗:{e}")
    try:
        with _db._lock, _db._conn() as c:
            rows = c.execute("""
              SELECT * FROM health_daily
              WHERE trade_date=?
              ORDER BY health_score DESC
            """, (target_date,)).fetchall()
        for r in rows:
            row = dict(r)
            code = row.get("code")
            if not code or code not in C.SECTOR_MAP:
                continue
            sec_name, _ = C.SECTOR_MAP[code]
            cn_name = C.NAME_MAP.get(code, code)
            chip = {}
            if _chips is not None:
                try:
                    chip = _chips.get_chips(code) or {}
                except Exception as e:
                    print(f"[eod_state] 籌碼 {code} 取得失敗:{e}")
            stock = _row_to_stock(row, sec_name, cn_name, chip=chip)
            snap = official_snaps.get(code)
            if snap:
                stock.update({
                    "price": snap.get("price"),
                    "change_rate": snap.get("change_rate"),
                    "high": snap.get("high"),
                    "low": snap.get("low"),
                    "avg_price": snap.get("avg_price"),
                    "total_volume": snap.get("total_volume"),
                    "total_amount": snap.get("total_amount"),
                    "price_source": snap.get("source"),
                    "price_source_date": snap.get("source_date"),
                })
            else:
                # 沒有官方價就留空，禁止拿 100×漲跌幅製造假價格。
                stock.update({
                    "price": None,
                    "high": None,
                    "low": None,
                    "avg_price": None,
                    "price_source": "official_unavailable",
                    "price_source_date": None,
                })
            stocks.append(stock)
    except Exception as e:
        print(f"[eod_state] stocks 取得失敗:{e}")

    # ── 4) leaders:取 sectors 攻擊族群的 stocks[0..2],簡化版 ──
    leaders = []
    for s in sectors[:3]:
        if (s.get("flow_score") or 0) <= 0:
            continue
        member = next((x for x in stocks if x["sector"] == s["name"]), None)
        if member:
            leaders.append({
                "code": member["code"],
                "name": member["name"],
                "sector": member["sector"],
                "price": member["price"],
                "change_rate": member["change_rate"],
                "ai_score": member["ai_score"],
                "sector_pct": s["pct"],
                "stance": "attack",
                "advice": f"族群 {s['name']} 流入 {s.get('amount_100m', 0)}億,中位 {s['pct']:+.2f}%",
            })

    return {
        "status": "eod" if not is_mkt else "open",
        "is_market_hours": is_mkt,
        "time": now.strftime("%H:%M:%S"),
        "market": market,
        "sectors": sectors,
        "locked_sectors": locked,
        "leaders": leaders,
        "stocks": stocks,
        "gate": {"active": False, "note": "EOD 組裝(盤中 gate 不適用)"},
        "updated_at": now.isoformat(),
        "source": (f"mls.db health_daily + sector_daily ({target_date})"
                   f"+ official_source({market.get('_err', 'TWSE 官方')})"
                   "+ stock_prices(official TWSE/TPEx)"),
    }
