# -*- coding: utf-8 -*-
"""quote_gateway.py — 盤中報價閘道（唯一 Shioaji 擁有者 · 獨立常駐 daemon）

為什麼要這支（根治「一個月每天壞」的病灶）：
    舊架構 Shioaji 登入/訂閱/buffer 全塞在 uvicorn 網頁 process 裡，沒有隔離、
    沒有健康監控、沒有備援，重啟又殺不乾淨 → 多重登入互踢 session → 盤中整個
    畫面死。本 daemon 把 Shioaji 抽成「獨立、單一擁有者」的行情層：

        Shioaji ─┐
                 ├─ Quote Gateway(本檔) ─ 原子快照(/dev/shm) + HTTP:8005 ─ 消費端
        TWSE MIS ┘                                               (8000 / feed_bridge / AB)

    網頁 process 從此「只讀快照、不登入 Shioaji」——多重登入從架構上不可能再發生。

職責（對齊 Vanessa 規劃 §1–§6）：
    §1 Shioaji 只負責 aflow / tick_type；連線異常 → aflow 標 UNAVAILABLE，
       不拿五檔壓力偽裝成 aflow。
    §2 價/量/突破：主源 Shioaji，備援 TWSE MIS(mis_source)；Shioaji 掛，價量照跑。
    §3 Session Watchdog：門檻見 WD_* 常數。
    §4 整包重連：unsubscribe → dispose → new instance → login → contracts →
       subscribe → 驗第一筆 tick → 恢復 aflow（見 _full_reconnect）。
    §6 每檔輸出 price_source / aflow_source / quote_status / aflow_status /
       last_tick_age，讓畫面顯示資料品質。

備註：目前 store 用 /dev/shm 原子快照檔（tmpfs）＋本地 HTTP，無 Redis 依賴；
    消費端契約固定，日後要換 Redis 免動消費端。
"""
from __future__ import annotations

import json
import os
import threading
import time
import datetime as _dt

import mis_source

# ── Watchdog 門檻（§3）──────────────────────────────────────────────
WD_TICK_DEGRADED = 8      # 全域無 tick 進展 >8s → DEGRADED
WD_TICK_RECONNECT = 20    # >20s → 觸發整包重連
WD_FAIL_TO_BACKUP = 3     # 連續 3 次重連/訂閱失敗 → 價量切 MIS 備援
WD_AFLOW_UNAVAIL = 15     # aflow >15s 未更新 → aflow_status=UNAVAILABLE
MIS_POLL_SEC = 5          # 備援輪詢頻率（TWSE 節流友善）
LOOP_SEC = 2              # 主迴圈心跳
SNAPSHOT_PATH = "/dev/shm/mls_quote_gateway.json"
HTTP_PORT = 8005

_TW = _dt.timezone(_dt.timedelta(hours=8))


def _now_tw():
    return _dt.datetime.now(_TW)


def _is_market_hours():
    n = _now_tw()
    if n.weekday() >= 5:
        return False
    m = n.hour * 60 + n.minute
    return 9 * 60 <= m <= 13 * 60 + 35   # 09:00–13:35（含尾盤緩衝）


class Gateway:
    def __init__(self, codes: list[str]):
        self.codes = [str(c) for c in codes]
        self._lock = threading.Lock()
        self._sha_buf: dict[str, dict] = {}     # Shioaji 最新（code -> row）
        self._mis: dict[str, dict] = {}          # MIS 最新
        self._merged: dict[str, dict] = {}       # 對外統一快照
        # 健康度
        self._last_tick_ts = 0.0                 # 全域最後一筆 tick 進展時間
        self._last_vol_sum = -1                  # 上一輪 buffer 量總和（偵測進展）
        self._fail_streak = 0
        self._feed_state = "INIT"                # INIT/LIVE/DEGRADED/RECONNECTING/BACKUP
        self._last_mis_poll = 0.0
        self._last_reconnect = 0.0

    # ── Shioaji 端（唯一擁有者）────────────────────────────────────
    def _poll_shioaji(self):
        """讀 broker buffer；偵測全域 tick 是否有進展以更新 last_tick_ts。"""
        import broker
        try:
            broker.ensure_subscribed(self.codes)   # 冪等
            rows = broker.raw_buffer_snapshots()
        except Exception as e:
            print(f"[gw] shioaji poll err: {type(e).__name__}: {e}", flush=True)
            return False
        vol_sum = 0
        buf = {}
        for r in rows:
            code = str(r.get("code") or "")
            if not code:
                continue
            buf[code] = r
            vol_sum += int(r.get("total_volume") or 0)
        with self._lock:
            self._sha_buf = buf
        # 全域進展偵測：買賣量總和變動 = 有新 tick 進來。
        # 冷啟護欄：首見(_last_vol_sum<0)只記基準、不宣稱新鮮——否則死 feed(量非0但
        # 凍結)第一筆就被當進展 → 假 LIVE。沒有真進展前 _last_tick_ts 維持 0 → 老化重連。
        if vol_sum > 0:
            if self._last_vol_sum < 0:
                self._last_vol_sum = vol_sum
            elif vol_sum != self._last_vol_sum:
                self._last_vol_sum = vol_sum
                self._last_tick_ts = time.time()
                return True
        return False

    def _full_reconnect(self):
        """§4 整包重連：dispose 舊 session → 重登 → 重訂 → 驗第一筆 tick。
        只 re-login 不 dispose 是舊系統不穩的主因，故這裡先把單例清掉。"""
        import broker
        self._feed_state = "RECONNECTING"
        print("[gw] === FULL RECONNECT begin ===", flush=True)
        try:
            api = getattr(broker, "_api", None)
            if api is not None:
                # unsubscribe 舊訂閱（盡力，失敗不擋）
                try:
                    import config as C
                    for c in list(getattr(broker, "_SUBSCRIBED", set())):
                        try:
                            api.unsubscribe(api.Contracts.Stocks[c], quote_type="tick")
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    api.logout()
                except Exception:
                    pass
        finally:
            # dispose：清掉單例與訂閱/callback 綁定狀態，強制下一次全新建立
            broker._api = None
            broker._QUOTE_CB_BOUND = False
            try:
                broker._SUBSCRIBED.clear()
            except Exception:
                pass
        try:
            broker.ensure_subscribed(self.codes)   # 重新 login + bind + subscribe
            # 驗第一筆 tick：等最多 6s 看 buffer 有沒有量進來
            ok = False
            for _ in range(12):
                time.sleep(0.5)
                if self._poll_shioaji():
                    ok = True
                    break
            if ok:
                self._fail_streak = 0
                self._feed_state = "LIVE"
                print("[gw] === RECONNECT ok（已收到 tick）===", flush=True)
                return True
        except Exception as e:
            print(f"[gw] reconnect err: {type(e).__name__}: {e}", flush=True)
        self._fail_streak += 1
        print(f"[gw] === RECONNECT fail streak={self._fail_streak} ===", flush=True)
        return False

    # ── MIS 備援端 ─────────────────────────────────────────────────
    def _poll_mis(self):
        try:
            self._mis = mis_source.fetch(self.codes)
        except Exception as e:
            print(f"[gw] mis poll err: {type(e).__name__}: {e}", flush=True)

    # ── 合併與健康判定（§1 §2 §6）─────────────────────────────────
    def _merge(self):
        now = time.time()
        tick_age = round(now - self._last_tick_ts, 1) if self._last_tick_ts else None
        aflow_live = tick_age is not None and tick_age <= WD_AFLOW_UNAVAIL
        # 價量是否可信 Shioaji：feed 沒進 BACKUP，且近期有 tick
        sha_ok = self._feed_state not in ("BACKUP", "RECONNECTING") and \
            tick_age is not None and tick_age <= WD_TICK_RECONNECT

        merged = {}
        for code in self.codes:
            sha = self._sha_buf.get(code) or {}
            mis = self._mis.get(code) or {}
            row = {"code": code}
            if sha_ok and sha.get("price"):
                buy = int(sha.get("buy_volume") or 0)
                sell = int(sha.get("sell_volume") or 0)
                row.update({
                    "price": sha.get("price"),
                    "change_rate": sha.get("change_rate"),
                    "total_volume": int(sha.get("total_volume") or 0),
                    "price_source": "shioaji", "quote_status": "LIVE",
                    # §1 aflow 只信 Shioaji，且要新鮮
                    "aflow": (buy - sell) if aflow_live else None,
                    "buy_volume": buy if aflow_live else None,
                    "sell_volume": sell if aflow_live else None,
                    "aflow_source": "shioaji" if aflow_live else None,
                    "aflow_status": "LIVE" if aflow_live else "UNAVAILABLE",
                })
            elif mis.get("price"):
                # §2 價量走備援；§1 aflow 一律 UNAVAILABLE，絕不用五檔偽裝
                row.update({
                    "price": mis.get("price"),
                    "change_rate": mis.get("change_rate"),
                    "total_volume": int(mis.get("total_volume") or 0),
                    "price_source": "twse_mis", "quote_status": "BACKUP",
                    "aflow": None, "buy_volume": None, "sell_volume": None,
                    "aflow_source": None, "aflow_status": "UNAVAILABLE",
                })
            else:
                row.update({
                    "price": None, "change_rate": None, "total_volume": 0,
                    "price_source": None, "quote_status": "NO_DATA",
                    "aflow": None, "aflow_source": None, "aflow_status": "UNAVAILABLE",
                })
            row["last_tick_age"] = tick_age
            merged[code] = row
        with self._lock:
            self._merged = merged
        return tick_age

    def snapshot(self) -> dict:
        with self._lock:
            rows = list(self._merged.values())
        live = sum(1 for r in rows if r["quote_status"] == "LIVE")
        backup = sum(1 for r in rows if r["quote_status"] == "BACKUP")
        aflow_live = sum(1 for r in rows if r["aflow_status"] == "LIVE")
        return {
            "ok": True,
            "updated_at": _now_tw().isoformat(timespec="seconds"),
            "feed_state": self._feed_state,
            "last_tick_age": (round(time.time() - self._last_tick_ts, 1)
                              if self._last_tick_ts else None),
            "counts": {"total": len(rows), "quote_live": live,
                       "quote_backup": backup, "aflow_live": aflow_live},
            "rows": rows,
        }

    def _write_snapshot(self):
        data = self.snapshot()
        tmp = SNAPSHOT_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, SNAPSHOT_PATH)   # 原子替換，讀端永遠讀到完整檔
        except Exception as e:
            print(f"[gw] snapshot write err: {e}", flush=True)

    # ── 主迴圈（Watchdog 狀態機）───────────────────────────────────
    def run(self):
        print(f"[gw] start · {len(self.codes)} 檔 · 唯一 Shioaji 擁有者", flush=True)
        while True:
            try:
                market = _is_market_hours()
                progressed = self._poll_shioaji() if market else False
                tick_age = (time.time() - self._last_tick_ts) if self._last_tick_ts else None

                if market:
                    if self._fail_streak >= WD_FAIL_TO_BACKUP:
                        self._feed_state = "BACKUP"
                    elif tick_age is None or tick_age > WD_TICK_RECONNECT:
                        # >20s 無 tick → 整包重連（避免過度頻繁：兩次間隔 ≥15s）
                        if time.time() - self._last_reconnect > 15:
                            self._last_reconnect = time.time()
                            self._full_reconnect()
                    elif tick_age > WD_TICK_DEGRADED:
                        self._feed_state = "DEGRADED"
                    elif progressed:
                        self._feed_state = "LIVE"

                    # 備援輪詢：非 LIVE 時一定要有 MIS；LIVE 時也定期備著（供突破監控/對照）
                    if time.time() - self._last_mis_poll > MIS_POLL_SEC:
                        self._last_mis_poll = time.time()
                        self._poll_mis()
                else:
                    self._feed_state = "CLOSED"

                self._merge()
                self._write_snapshot()
            except Exception as e:
                print(f"[gw] loop err: {type(e).__name__}: {e}", flush=True)
            time.sleep(LOOP_SEC)


# ── 對外 HTTP（消費端二選一：讀 /dev/shm 檔 或 打這個）────────────────
def make_app(gw: "Gateway"):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    app = FastAPI(title="MLS Quote Gateway")

    @app.get("/quotes")
    def quotes():
        return JSONResponse(gw.snapshot())

    @app.get("/health")
    def health():
        s = gw.snapshot()
        return {"feed_state": s["feed_state"], "last_tick_age": s["last_tick_age"],
                "counts": s["counts"]}
    return app


def _load_universe() -> list[str]:
    ns: dict = {"__file__": os.path.join(os.path.dirname(__file__), "config.py"),
                "__name__": "cfg"}
    src = open(ns["__file__"], encoding="utf-8").read()
    exec(compile(src, "config.py", "exec"), ns)
    return list(ns["UNIVERSE"])


def main():
    gw = Gateway(_load_universe())
    t = threading.Thread(target=gw.run, daemon=True)
    t.start()
    import uvicorn
    uvicorn.run(make_app(gw), host="127.0.0.1", port=HTTP_PORT, log_level="warning")


if __name__ == "__main__":
    main()
