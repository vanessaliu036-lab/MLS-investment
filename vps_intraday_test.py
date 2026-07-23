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
    """真實欄位才計分；缺資料列入 missing，絕不補 50 或用漲跌幅造分。"""
    price = float(raw.get("price") or 0)
    change = float(raw.get("change_rate") or 0)
    aflow = int(raw.get("buy_volume") or 0) - int(raw.get("sell_volume") or 0)
    volume = int(raw.get("total_volume") or 0)
    points = 0.0
    missing = []
    factors = {}

    # 盤中不讀盤後模組；資金健康度、承接品質、融資屬收盤後驗證，
    # 不得放進盤中「缺資料」或影響盤中觀察分類。
    for key in ("money_health", "absorption"):
        factors[key] = {"points": None, "max": FACTOR_WEIGHTS[key], "status": "盤後驗證"}

    if volume > 0 and aflow is not None:
        p = FACTOR_WEIGHTS["net_active"] * _norm(aflow, 0, 50_000_000)
        points += p
        factors["net_active"] = {"points": round(p, 1), "max": 22, "status": "已接入"}
    else:
        factors["net_active"] = {"points": None, "max": 22, "status": "缺資料"}
        missing.append("主動買賣差")

    if price > 0 and ma20:
        p = FACTOR_WEIGHTS["vs_ma20"] if price >= float(ma20) else 0.0
        points += p
        factors["vs_ma20"] = {"points": p, "max": 12, "status": "已接入"}
    else:
        factors["vs_ma20"] = {"points": None, "max": 12, "status": "缺資料"}
        missing.append("MA20")

    streak = chip.get("inst_streak")
    if streak is not None:
        p = FACTOR_WEIGHTS["inst_streak"] * _norm(streak, 0, 5)
        points += p
        factors["inst_streak"] = {"points": round(p, 1), "max": 10, "status": "已接入"}
    else:
        factors["inst_streak"] = {"points": None, "max": 10, "status": "缺資料"}
        missing.append("法人連買")

    # 融資只在盤後資料固定後驗證；盤中不列為缺資料。
    factors["margin"] = {"points": None, "max": 8, "status": "盤後驗證"}

    extreme = abs(change) >= 9.0
    fake_red = change > 0 and aflow < 0
    resting = change <= 0 and aflow < 0
    if extreme or fake_red or resting:
        group, subgroup = "排除", "風險訊號"
        reason = "極端價／假紅／資金流出，不能作進場依據"
    elif not missing and points >= 65:
        group, subgroup = "可操作", "七因子達標"
        reason = "七因子 100 分制達到 65 分門檻"
    else:
        group, subgroup = "觀察", "條件待確認"
        if missing:
            reason = "；".join(f"等待{m}盤中回報" for m in missing)
        elif aflow >= 0 and change >= 0:
            reason = "盤中資金流入、股價同向上漲，持續觀察。"
        elif aflow >= 0 and change < 0:
            reason = "盤中資金流入但股價下跌，觀察承接。"
        elif aflow < 0 and change >= 0:
            reason = "盤中股價上漲但資金流出，留意假紅。"
        else:
            reason = "盤中資金與股價同步偏弱，觀察。"

    return {
        "score": round(points, 1), "score_max": 100,
        "score_available": round(sum(v["max"] for v in factors.values() if v["points"] is not None), 1),
        "score_rule": "screen intraday.py：六項權重合計 100；65 分以上且無缺資料才可操作",
        "factors": factors, "score_missing": missing,
        "group": group, "subgroup": subgroup, "reason": reason,
    }


@router.get("/intraday-test/daily-report", response_class=HTMLResponse)
def daily_report_page():
    """顯示指定的 0722 每日報告 UI；報告頁內的 API 仍走同一台 VPS。"""
    report = ROOT / "每日報告 0722.html"
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
    explanation = ai_explain.local_explain(snap, regime=_current_regime())
    return {
        "code": code,
        "name": getattr(config, "NAME_MAP", {}).get(code, code),
        "price": price,
        "change_rate": round(change, 2),
        "buy_volume": buy,
        "sell_volume": sell,
        "tick_type": raw.get("tick_type"),
        "raw_bid_side_total_vol": sell,
        "raw_ask_side_total_vol": buy,
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


@router.get("/api/intraday-test")
def intraday_test():
    started = time.time()
    print(f"[diag][http] intraday_test.begin ts={time.strftime('%Y-%m-%dT%H:%M:%S%z')}", flush=True)
    try:
        raw = broker.raw_buffer_snapshots()
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
                                 -x["change_rate"], -x["aflow"]))
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
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
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
                rows.append({
                    "code": str(code),
                    "name": getattr(config, "NAME_MAP", {}).get(str(code), str(code)),
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
            "updated_at": saved_updated_at or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
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
