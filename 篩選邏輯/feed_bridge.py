# -*- coding: utf-8 -*-
"""feed_bridge.py — 盤中報價橋接

正解（非獨立登入 Shioaji）：讀 8000 mls-intraday 服務「已連線的」Shioaji buffer
(GET /api/intraday-test)，把即時價量寫進 AB 引擎的 quote_snap / aflow 表。
screen_intraday 的讀取路徑完全不動，只是把報價來源從「自己 login Shioaji」換成
「8000 現成的 buffer」——避免同一組金鑰兩處登入互踢 session。

只讀 8000、不寫 8000；8000 回退快照(snapshot=True，非即時)時不寫，避免污染。
以 systemd 常駐（Restart=always），盤中每 30 秒一輪。
"""
import time
import json
import urllib.request
import datetime as _dt

import store

SRC = "http://127.0.0.1:8000/api/intraday-test"
# quote_snap / aflow 的 owner 是 "intraday"（原 intraday_feed 用）。bridge 取代
# intraday_feed 當同一角色的報價 producer，沿用同一 owner 名才過 store 寫入守衛。
PLUGIN = "intraday"
DB = "mls.db"
HTTP_TIMEOUT = 25
LIVE_INTERVAL = 30      # 有即時料時的輪詢秒數
IDLE_INTERVAL = 120     # 非即時(快照/休市)時放慢


def _tw_date():
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).date().isoformat()


def once():
    """回傳 (寫入檔數, 來源說明)；n<0 表示本輪不寫(非即時)。"""
    with urllib.request.urlopen(SRC, timeout=HTTP_TIMEOUT) as r:
        d = json.loads(r.read().decode())
    if d.get("ok") is False:
        return -1, d.get("error") or "not-ok"
    if d.get("snapshot"):                 # 8000 回退快照 → 非即時，不寫
        return -1, "snapshot"
    rows = d.get("rows") or []
    dd = _tw_date()
    now = _dt.datetime.now().isoformat(timespec="seconds")
    quotes, aflows = [], []
    for r0 in rows:
        code = str(r0.get("code") or "")
        price = r0.get("price")
        if not code or not price:         # 無價的檔不寫空列
            continue
        quotes.append({
            "code": code, "data_date": dd, "price": price,
            "change_rate": r0.get("change_rate") or 0.0,
            "volume": r0.get("total_volume") or 0,
            "bid_vol": r0.get("raw_bid_side_total_vol") or 0,
            "ask_vol": r0.get("raw_ask_side_total_vol") or 0,
            "updated_at": now,
        })
        buy = r0.get("buy_volume") or 0
        sell = r0.get("sell_volume") or 0
        # aflow 只信 Shioaji 真流:aflow_status==LIVE 且 aflow 有值才寫。
        # UNAVAILABLE(Shioaji 死/過期,quote_health 已判定並疊 MIS 價量)一律不寫此列
        # aflow、絕不用 buy-sell 回填假值 —— 保留 AB 上一輪好值,不讓假資金流污染
        # 盤中嚴判/B鏈(對齊 quote_health §1:行情死只損 aflow,不拿五檔/量差偽裝)。
        if r0.get("aflow_status") == "LIVE" and r0.get("aflow") is not None:
            aflows.append({
                "code": code, "data_date": dd,
                "active_buy": buy, "active_sell": sell,
                "net_active": r0.get("aflow"),
                "method": "bridge_8000", "updated_at": now,
            })
    if quotes:
        store.upsert_intraday("quote_snap", PLUGIN, quotes, DB)
    if aflows:
        # 8000 源頭偶發「51 檔 aflow 全 0」的壞輪詢(真實盤中不可能同時全 0,
        # 根因在該 app 的 Shioaji tick 聚合會週期性歸零)。這種輪詢跳過 aflow
        # 寫入、保留上一輪好值,避免 net_active 被抹成 0 讓盤中資金流/嚴判/B鏈失真。
        if any(a["net_active"] for a in aflows):
            store.upsert_intraday("aflow", PLUGIN, aflows, DB)
        else:
            print("[bridge] aflow 整批全 0 → 判壞輪詢,跳過寫入(保留上輪好值)", flush=True)
    return len(quotes), d.get("source")


def main():
    print("[bridge] start — 讀 8000 buffer 寫 AB quote_snap/aflow", flush=True)
    while True:
        try:
            n, src = once()
            if n >= 0:
                print(f"[bridge] wrote {n} quotes src={src}", flush=True)
                time.sleep(LIVE_INTERVAL)
            else:
                print(f"[bridge] skip ({src})", flush=True)
                time.sleep(IDLE_INTERVAL)
        except Exception as exc:
            print(f"[bridge] err: {exc}", flush=True)
            time.sleep(LIVE_INTERVAL)


if __name__ == "__main__":
    main()
