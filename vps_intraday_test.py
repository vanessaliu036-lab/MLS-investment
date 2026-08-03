# -*- coding: utf-8 -*-
"""隔離盤中測試服務。

這個服務只讀既有 MLS broker 的 Shioaji 訂閱 buffer，不寫資料庫、
不啟動第二組訂閱，也不改主站的 STATE；另將最後一筆有效盤中結果
原子保存為 VPS 本地快照，供收盤後 API 還原。部署到 VPS 時可獨立跑在 8002。
"""

import sys
import time
import json
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

router = APIRouter()
HISTORY_DB = BASE / "intraday_eod.db"
CHIP_CACHE = BASE / "個股卡片相關檔案_20260722" / "chips_cache.json"
INTRADAY_SNAPSHOT_PATH = BASE / "intraday_live_snapshot.json"
TW_TZ = ZoneInfo("Asia/Taipei")

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


def _trade_date():
    return datetime.now(TW_TZ).date().isoformat()


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


def _write_intraday_snapshot(result):
    """原子保存最後一筆有效盤中結果，供收盤後 API 直接回傳。"""
    try:
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
    """真實欄位才計分；缺資料列入 missing，絕不補分。

    量級修正（2026-07-24）：net_active 原本用 aflow/50,000,000 正規化，
    但 aflow 單位是「張」（典型 +300 張），除下來永遠 ≈0 → 全體 0 分，
    22 分因子等同報廢。改用「主動買賣差 ÷ 當日成交量」的比例計分。
    """
    price = float(raw.get("price") or 0)
    change = float(raw.get("change_rate") or 0)
    aflow = int(raw.get("buy_volume") or 0) - int(raw.get("sell_volume") or 0)
    volume = int(raw.get("total_volume") or 0)
    points = 0.0
    missing = []
    factors = {}
    ev = []          # 支持進場的證據
    against = []     # 扣分/風險證據

    for key in ("money_health", "absorption"):
        factors[key] = {"points": None, "max": FACTOR_WEIGHTS[key], "status": "盤後驗證"}

    # ── 主動買賣差 22 分：用佔成交量比例，≥8% 給滿分 ──
    ratio = (aflow / volume) if volume > 0 else None
    if ratio is not None:
        p_na = FACTOR_WEIGHTS["net_active"] * _norm(ratio, 0.0, 0.08)
        points += p_na
        factors["net_active"] = {
            "points": round(p_na, 1), "max": 22, "status": "已接入",
            "detail": f"主動買賣差 {aflow:+,} 張，佔成交量 {ratio*100:+.1f}%",
        }
        if ratio >= 0.03:
            ev.append(f"主動買超佔量 {ratio*100:.1f}%（買盤積極）")
        elif ratio <= -0.03:
            against.append(f"主動賣超佔量 {abs(ratio)*100:.1f}%（賣壓沉重）")
    else:
        factors["net_active"] = {"points": None, "max": 22, "status": "缺資料"}
        missing.append("主動買賣差")

    # ── MA20 12 分：站上滿分，跌破依乖離給部分分 ──
    if price > 0 and ma20:
        dev = (price - float(ma20)) / float(ma20)
        if dev >= 0:
            p_ma = FACTOR_WEIGHTS["vs_ma20"]
            ev.append(f"站上月線（高於 MA20 {dev*100:.1f}%）")
        else:
            p_ma = FACTOR_WEIGHTS["vs_ma20"] * max(0.0, 1 + dev / 0.05) * 0.5
            against.append(f"跌破月線 {abs(dev)*100:.1f}%")
        points += p_ma
        factors["vs_ma20"] = {
            "points": round(p_ma, 1), "max": 12, "status": "已接入",
            "detail": f"現價 {price} vs MA20 {ma20}（乖離 {dev*100:+.1f}%）",
        }
    else:
        factors["vs_ma20"] = {"points": None, "max": 12, "status": "缺資料"}
        missing.append("MA20")

    # ── 法人連買 10 分 ──
    streak = chip.get("inst_streak")
    if streak is not None:
        p_st = FACTOR_WEIGHTS["inst_streak"] * _norm(streak, 0, 5)
        points += p_st
        factors["inst_streak"] = {
            "points": round(p_st, 1), "max": 10, "status": "已接入",
            "detail": f"法人連買 {streak} 日",
        }
        if streak >= 3:
            ev.append(f"法人連買 {streak} 日")
    else:
        factors["inst_streak"] = {"points": None, "max": 10,
                                  "status": "籌碼快取重建中"}

    factors["margin"] = {"points": None, "max": 8, "status": "盤後驗證"}

    # 盤中可計算上限（扣掉盤後驗證項），分數以此為基準判定門檻
    avail = sum(v["max"] for v in factors.values() if v["points"] is not None)
    pct = (points / avail * 100) if avail else None

    extreme = abs(change) >= 9.0
    fake_red = change > 0 and aflow < 0
    resting = change <= 0 and aflow < 0

    if change > 0:
        ev.append(f"股價上漲 {change:+.2f}%")
    elif change < 0:
        against.append(f"股價下跌 {change:+.2f}%")

    if extreme or fake_red or resting:
        group, subgroup = "排除", "風險訊號"
        if extreme:
            why = f"漲跌幅 {change:+.2f}% 已達極端區間，追價風險過高"
        elif fake_red:
            why = f"股價漲 {change:+.2f}% 但主動賣超 {abs(aflow):,} 張——假紅、主力邊拉邊出"
        else:
            why = f"股價 {change:+.2f}% 且主動賣超 {abs(aflow):,} 張——量價同步走弱"
        reason = why
    elif not missing and pct is not None and pct >= 65:
        group, subgroup = "可操作", "盤中因子達標"
        reason = (f"盤中可計算 {points:.0f}/{avail:.0f} 分（{pct:.0f}%）達 65% 門檻："
                  + "；".join(ev[:3]) + "。盤後仍須籌碼與融資蓋章。")
    else:
        group, subgroup = "觀察", "條件待確認"
        bits = []
        if ev:
            bits.append("有利：" + "、".join(ev[:3]))
        if against:
            bits.append("不利：" + "、".join(against[:2]))
        if pct is not None:
            gap = max(0.0, 0.65 * avail - points)
            bits.append(f"盤中 {points:.0f}/{avail:.0f} 分（{pct:.0f}%）"
                        + (f"，差 {gap:.0f} 分達標" if gap > 0 else ""))
        if missing:
            bits.append("等待：" + "、".join(missing))
        reason = "；".join(bits) + "。"

    return {
        "score": round(points, 1), "score_max": 100,
        "score_pct": round(pct, 1) if pct is not None else None,
        "score_available": round(avail, 1),
        "score_rule": "盤中可計算三因子（主動買賣差22＋MA20 12＋法人連買10）；達可計算分數 65% 且無缺資料才可操作",
        "factors": factors, "score_missing": missing,
        "evidence": ev, "against": against,
        "group": group, "subgroup": subgroup, "reason": reason,
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
        "extreme": abs(change) >= 9.0,
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
        "change_rate": round(change, 2),
        "buy_volume": buy,
        "sell_volume": sell,
        "tick_type": raw.get("tick_type"),
        # buy=主動買(=bid_side)、sell=主動賣(=ask_side)；raw_* 顯示回真實 bid/ask 側量
        "raw_bid_side_total_vol": buy,
        "raw_ask_side_total_vol": sell,
        "aflow": aflow,
        "quadrant": F.proxy_quadrant(aflow, change),
        "total_volume": int(raw.get("total_volume") or 0),
        "ma20": ma20,
        "ma20_cache": ma20_status,
        "volume_ratio": raw.get("volume_ratio"),
        "filters": filters,
        "classification": classification,
        "group": classification["group"],
        "subgroup": classification["subgroup"],
        "classification_reason": classification["reason"],
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


def _index_pct():
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
                return saved
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
                    return saved
        else:
            fallback_source = False
        regime = _current_regime()
        rows = [_row(item) for item in raw]
        # v5 分類攤平：可操作→觀察→排除；各群內仍維持漲幅優先，再按 aflow。
        group_order = {"可操作": 0, "觀察": 1, "排除": 2}
        rows.sort(key=lambda x: (group_order.get(x["group"], 9),
                                 -(x.get("score_pct") or 0),
                                 -(x.get("score") or 0),
                                 -x["change_rate"]))
        category_counts = {}
        for row in rows:
            category_counts[row["group"]] = category_counts.get(row["group"], 0) + 1
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
            "category_counts": category_counts,
            "regime": regime,
            "quota": quota,
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


@router.get("/api/intraday-watchpool")
def intraday_watchpool():
    """盤中雷達：固定池全集，僅把即時判讀套到有回報的檔案。"""
    started = time.time()
    try:
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
                    "ai": "固定觀察池成員，等待即時資料；不影響固定名單。",
                    "filter_no_data": ["即時行情"],
                    "has_data": False,
                })
            else:
                row = _row(raw)
                row["has_data"] = True
                rows.append(row)
        return {
            "ok": True,
            "source": "固定 51 檔觀察池 + VPS Shioaji 盤中觀察邏輯",
            "read_only": True,
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
