"""
api.py — 唯一的名單出口

規範:
1. 名單只能在後端算,只能有兩支計分函式(盤中一支、盤後一支)。
2. 前端不准出現任何 filter、任何 if 判定分組、任何自訂排序。
   前端只准做兩件事:選欄位顯示、取前 N 筆。
3. 只有一個名單端點 /api/watchlist。
   舊的 /api/intraday-test、/api/watchpool、機會雷達各自的端點全部廢除。
4. purpose 那行字由後端決定,前端不准自己寫。

驗收條件:任兩個分頁同時打開,前 10 檔的股票代號和順序必須完全一致。
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import Body, FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse

import config
import screen_intraday
import screen_post
import store
from tw_price_limit import is_limit_up
from phase import (Phase, get_phase, describe, today_tw,
                   prev_trading_day, next_trading_day)
from datetime import date as _dt_date

# B 鏈(盤中發現 → 盤後驗證 → 隔日候選)。與 A 鏈完全獨立,只在 merge_pool 交會。
import b_snapshot
import b_discover
import b_verify
import recovery_scan
import merge_pool
# 當日收盤復盤(命中率/勝率分析層)。衡量模型準度,與名單產生脫鉤。
import screen_verify
# 淘汰名單「今日盤中說明」:後台算事實(今日資金流 vs 淘汰理由背離/確認),前台只印。
import intraday_note
# AI 盤中判讀:後台算結論(判讀+下一步),前台只印。不參與篩選。
import intraday_verdict

app = FastAPI(title="MLS v4.0")


@app.on_event("startup")
def _startup():
    """
    開機只做兩件事:建表、跑自檢。
    不跑任何補資料流程、不重抓、不重算。缺哪天的資料標「缺 X/XX」,你自己按按鈕補。
    這是你等待的主因,現在移除。
    """
    store.init_db()
    import preflight
    preflight.run(fail_fast=True)


def _attach_intraday_note(dropped):
    """為淘汰列附加今日盤中資金流與一句白話說明(唯讀,不影響名單)。
    只認『今天這一盤』的 quote_snap/aflow;開盤前最新一筆仍是昨日盤中,
    抓進來會被誤標成今日 → 一律以 data_date==今天交易日為閘,不等於就當作
    資料未到(flow/change=None),讓 intraday_note 說『待開盤後判讀』。"""
    import sqlite3
    td = today_tw().isoformat()
    c = sqlite3.connect("mls.db"); c.row_factory = sqlite3.Row
    try:
        q = {r["code"]: r for r in c.execute(
            "SELECT code,price,change_rate FROM quote_snap WHERE data_date=?", (td,))}
        a = {r["code"]: r for r in c.execute(
            "SELECT code,net_active FROM aflow WHERE data_date=?", (td,))}
    finally:
        c.close()
    for row in dropped:
        if not isinstance(row, dict):
            continue
        code = str(row.get("stock_id") or row.get("code") or "")
        qq, aa = q.get(code), a.get(code)
        chg = qq["change_rate"] if qq else None
        flow = aa["net_active"] if aa else None
        why = row.get("why") or row.get("reason") or row.get("detail") or ""
        if qq is not None:
            row["today_price"] = qq["price"]
        row["today_change"] = chg
        row["today_aflow"] = flow
        row["intraday_note"] = intraday_note.build(why, flow, chg)


def _attach_t1_verify_flow(rows, pool_date):
    """Attach only the next-trading-day verification flow for a dropped pool.

    The pool build date is T.  T's intraday flow is never a verification
    result for that same pool; until T+1 arrives the fields stay pending.
    """
    from phase import next_trading_day, today_tw

    today = today_tw()
    verify_date = next_trading_day(pool_date)
    pending = verify_date > today
    af = {} if pending else store.read_date("aflow", verify_date)
    quotes = {} if pending else store.read_date("quote_snap", verify_date)
    for row in rows:
        code = str(row.get("stock_id") or row.get("code") or "")
        flow = (af.get(code) or {}).get("net_active")
        quote = quotes.get(code) or {}
        row["verify_date"] = verify_date.isoformat()
        row["verify_pending"] = pending
        row["t1_aflow"] = flow
        row["t1_price"] = quote.get("price")
        row["t1_change"] = quote.get("change_rate")
        row["t1_is_limit_up"] = is_limit_up(quote.get("price"), change_rate=quote.get("change_rate"))
        row["t1_is_live"] = verify_date == today
        if row.get("recovery_pool"):
            trigger = recovery_scan.evaluate_t1_trigger(
                price=quote.get("price"), aflow=flow,
                ma5=row.get("rejected_ma5"),
                rejected_high=row.get("rejected_high"))
            row["recovery_t1_checks"] = trigger["checks"]
            row["recovery_t1_triggered"] = trigger["triggered"]
            row["recovery_t1_classification"] = trigger["classification"]
        why = row.get("why") or row.get("reason") or row.get("detail") or ""
        row["t1_note"] = ("待 T+1 驗證" if pending else
                          intraday_note.build(why, flow, quote.get("change_rate")))


def _attach_verdict(rows, pool_date):
    """附加 reject_outcome 已驗證的誤刪判定(排對/誤刪/嚴重誤刪)。
    這是拿 T+1 盤中資金流打臉淘汰理由後的結果,唯讀併欄,不影響淘汰本身。"""
    import reject_verify
    v = reject_verify.verdicts_for(pool_date)
    for row in rows:
        code = str(row.get("stock_id") or row.get("code") or "")
        d = v.get(code)
        row["verdict"] = d.get("verdict") if d else None
        row["fnr_5"] = d.get("fnr_5") if d else None
        row["fnr_9"] = d.get("fnr_9") if d else None
        row["t1_high_ret"] = d.get("t1_high_ret") if d else None


def _attach_pool_outcome(items, pool_date):
    """歷史複盤:把該池的 T+1 實際結果(pool_outcome)併回每一檔。

    沒有這步的話,複盤只剩「當時選了誰」,看不到「後來走成怎樣」—— 而後者才是
    複盤的目的。欄位名沿用顯示端既有契約(triggered/success/t1_close_pct),
    讓前端不必為歷史另寫一套讀法。
    未驗證的池(T+1 還沒到)留 None,不填 0 —— 0 會被讀成「觸發了但沒漲」。
    """
    import sqlite3
    with sqlite3.connect("mls.db", timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        oc = {r["code"]: dict(r) for r in conn.execute(
            "SELECT * FROM pool_outcome WHERE pool_date=?", (pool_date.isoformat(),))}
    for it in items:
        o = oc.get(str(it.get("code") or ""))
        if not o:
            it["verified"] = False
            continue
        it["verified"] = True
        it["base_close"] = o.get("base_close")
        it["triggered"] = o.get("triggered")
        it["success"] = o.get("hit")
        it["t1_close_pct"] = o.get("ret_pct")
        it["t1_high"] = o.get("next_high")
        it["t1_close"] = o.get("next_close")
        it["verdict"] = o.get("verdict")
    return sum(1 for it in items if it.get("verified"))


@app.get("/api/watchlist")
def watchlist(phase: Optional[str] = Query(None, description="PRE|INTRADAY|POST,預設依當下時段"),
              date: Optional[str] = Query(None, description="資料日(池日) YYYY-MM-DD;指定=歷史複盤"),
              applies_date: Optional[str] = Query(None, description="檢視日(該池適用日);與 /api/dropped 同口徑")):
    """
    全系統唯一的名單端點。每檔已經算完 score / rank / reasons / missing。

    固定回傳 config.UNIVERSE 全集,不預先剔除。
    沒回報的檔標 has_data=false,灰掉但不消失 —— 消失比顯示更誤導。

    歷史複盤:攤該日已定案的盤後池留痕,不看時段、不重算。兩種日期口徑,
    與 /api/dropped 一致 —— date=資料日(池日),applies_date=檢視日(池適用日,
    = 資料日的下一交易日)。兩者同時給時 date 優先。混用這兩個口徑會整體差
    一個交易日,所以端點收哪一種必須寫死,不靠呼叫端猜。

    dates 一律回「檢視日」清單(新→舊),與前端日期下拉同口徑;pool_dates 是
    對應的資料日。日期下拉只能吃 dates,不准再從別的端點湊。
    """
    ph = Phase(phase) if phase else get_phase()

    pool_dates = screen_post.available_dates()

    if date or applies_date:
        # 歷史複盤優先於時段判斷。看 8/11 就是看 8/11,不因為現在是盤中而改讀別的。
        try:
            if date:
                data = screen_post.load_for_date(date)
            else:
                d = _dt_date.fromisoformat(applies_date)
                data = screen_post.load_for_date(prev_trading_day(d))
        except ValueError:
            bad = date or applies_date
            return JSONResponse({"error": f"日期格式須為 YYYY-MM-DD,收到 {bad!r}"},
                                status_code=400)
        try:
            n = _attach_pool_outcome(
                data.get("items") or [], _dt_date.fromisoformat(data["data_date"]))
            data["verified_count"] = n
        except Exception as _e:
            print(f"[watchlist] pool_outcome attach skip: {_e}")
    elif ph in (Phase.PRE, Phase.CLOSED):
        # 盤前/休市 = 直接讀上一交易日盤後名單,不重算、不重抓、零 API,秒開。
        # 休市(週末/國定假日)絕不因為時鐘到 09:00 就跑盤中。
        data = screen_post.load_for_premarket()
        if ph is Phase.CLOSED:
            info = describe(Phase.CLOSED)
            data["phase"] = info["phase"]
            data["purpose"] = info["purpose"]
            data["actionable"] = False
            # 週末/國定假日不出表單:清空名單,只留休市狀態。避免把上一交易日的
            # 池貼上「今日/明日」標籤,造成「非交易日卻多一張不一樣的表」的混淆。
            data["market_closed"] = True
            data["items"] = []
    elif ph is Phase.INTRADAY:
        # 盤中嚴判:只盯昨日候選池,四條件全中才綠燈(不吃 universe,自己讀 pool)
        data = screen_intraday.build()
    else:
        # 「盤後」是顯示,不是無條件重算。用真實時鐘分流:
        #   真盤後(今日已收盤定案,13:31 後)才 build,產出明日候選;
        #   盤中/盤前看盤後 = 攤上一交易日已定案那份(A一:對照昨篩今走)。
        #   否則 build(today) 會拿今天未收盤的日期去要死值 → 六因子全 NO_DATA,
        #   就是「資料待補、資金流沒勁」的病灶。
        if get_phase() is Phase.POST:
            data = screen_post.build(config.UNIVERSE)
        else:
            data = screen_post.load_last_post()

    # 淘汰列只附加 T+1 驗證流；絕不把砍當天流掛上去。
    if isinstance(data, dict) and data.get("dropped"):
        try:
            import datetime as _dt
            pd = _dt.date.fromisoformat(data["data_date"])
            _attach_t1_verify_flow(data["dropped"], pd)
            _attach_verdict(data["dropped"], pd)
        except Exception as _e:
            print(f"[watchlist] T+1 verify flow skip: {_e}")

    # AI 盤中判讀:每一檔都附「這代表什麼 / 為何歸這類 / 什麼變化會升降級」。
    # 顯示端不再自己把欄位串成句子(那只是把數字念第二遍)。
    if isinstance(data, dict) and data.get("items"):
        try:
            intraday_verdict.attach(data["items"])
        except Exception as _e:
            print(f"[watchlist] verdict skip: {_e}")

    # 日期下拉的唯一來源。前端不准再從別的端點湊日期清單。
    if isinstance(data, dict):
        try:
            data["pool_dates"] = pool_dates
            data["dates"] = sorted(
                {next_trading_day(_dt_date.fromisoformat(p)).isoformat()
                 for p in pool_dates}, reverse=True)
        except Exception as _e:
            print(f"[watchlist] dates skip: {_e}")
    return JSONResponse(data)


@app.get("/api/dropped")
def dropped_review(pool_date: Optional[str] = Query(None, description="池日(=淘汰資料日);直接指定則優先"),
                   applies_date: Optional[str] = Query(None, description="與觀察清單同一個 build 的 data_date")):
    """Return the dropped rows from the same build as the watchlist.

    ``pool_date`` and ``applies_date`` both identify the build date directly;
    there is intentionally no previous-trading-day fallback.  A pool built on
    T is verified only with T+1 flow, and remains pending before T+1 arrives.
    """
    from phase import next_trading_day, today_tw
    import datetime as _dt
    if pool_date:
        pd = _dt.date.fromisoformat(pool_date)
    elif applies_date:
        pd = _dt.date.fromisoformat(applies_date)
    else:
        # Same build as the currently served post-market watchlist.  Before
        # today's close this is normally the previous trading day's build.
        current = screen_post.load_last_post()
        raw_date = current.get("data_date") if isinstance(current, dict) else None
        pd = _dt.date.fromisoformat(raw_date) if raw_date else today_tw()
    rows = screen_post.load_dropped(pd)
    today = today_tw()
    verify_date = next_trading_day(pd)
    pending = verify_date > today
    _attach_t1_verify_flow(rows, pd)
    _attach_verdict(rows, pd)
    return JSONResponse({
        "data_date": pd.isoformat(),
        "verify_date": verify_date.isoformat(),
        "verify_pending": pending,
        "today": today.isoformat(),
        "note": (f"淘汰={pd.isoformat()} 收盤篩掉;用 T+1={verify_date.isoformat()} 盤中資金流驗證"
                 + ("(尚未到,待驗證)" if pending else "(驗證中/已驗)")),
        "dropped": rows,
    })


@app.get("/api/funnel")
def funnel_view(phase: Optional[str] = Query(None, description="INTRADAY|POST;預設依時段")):
    """新版漏斗逐層淘汰（唯讀·對照/驗證用）。盤中 with_chips=False(L0→L2.5)、
    盤後 with_chips=True(含 L3 籌碼)。尚未取代 /api/watchlist；先並存供驗證。
    lazy import：funnel 若有問題只壞這條端點,不影響引擎啟動與其他名單。"""
    import funnel
    ph = Phase(phase) if phase else get_phase()
    r = funnel.run(list(config.UNIVERSE), dict(config.CODE_GROUP),
                   db_path="mls.db", with_chips=(ph is Phase.POST))
    r["phase"] = ph.value
    return JSONResponse(r)


# ════════════════════════════════════════════════════════════════════
# B 鏈端點(UI 分開:盤中畫面永遠只有 A 鏈燈號;B 鏈的東西收盤後才出現)
# 盤中發現 → 盤後法人驗證 → 匯流成明日候選池新血。
# ════════════════════════════════════════════════════════════════════

@app.post("/api/b/snapshot")
def b_take_snapshot(buffer: dict = Body(..., description="Shioaji 記憶體 buffer {code:{price,change_rate,volume,net_active,bid_vol,ask_vol}}")):
    """盤中每 5 分鐘由訂閱端推一次。零 API、純本地寫檔。非盤中時段自動略過。"""
    return JSONResponse(b_snapshot.take(buffer))


@app.post("/api/b/scan")
def b_scan():
    """13:20 最終掃描:四判準中兩項成立才標記。不出燈號、不進盤中畫面。"""
    return JSONResponse(b_discover.scan(config.UNIVERSE, config.CODE_GROUP))


@app.get("/api/b/discovery")
def b_discovery_view():
    """B 鏈今日發現(唯讀顯示)。盤中只記錄,收盤後才有意義。"""
    rows = store.read_date("b_discovery", today_tw())
    items = sorted(rows.values(), key=lambda r: (-(r.get("hits") or 0), r["code"]))
    return JSONResponse({
        "chain": "B", "data_date": today_tw().isoformat(),
        "purpose": f"B鏈盤中發現 {len(items)} 檔 — 非進場訊號,待盤後法人驗證",
        "actionable": False, "items": items,
    })


@app.post("/api/b/verify")
def b_do_verify():
    """13:31 後:用今日法人數字驗 B 鏈標記(PASS 進池 / FAIL 淘汰 / NO_DATA 留驗)。"""
    return JSONResponse(b_verify.verify())


@app.post("/api/pool/merge")
def pool_merge():
    """盤後匯流:明日候選池 = A鏈寬篩 ∪ B鏈驗證通過。任一鏈掛掉另一照併。"""
    return JSONResponse(merge_pool.merge())


@app.get("/api/pool/tomorrow")
def pool_tomorrow():
    """隔日盤中觀察清單(唯讀):匯流後的候選池,標源(A鏈/雙鏈確認/B鏈新血)。
    序號一律重排 1..N(不沿用殘留 rank);注入公司名;purpose 講明適用日與資料是否就緒。"""
    from phase import next_trading_day
    d = today_tw()
    rows = store.read_date("candidate_pool", d)
    ordered = sorted(rows.values(), key=lambda r: (r.get("rank") or 999, r["code"]))
    items = []
    for i, r in enumerate(ordered, 1):
        payload = {}
        if r.get("payload"):
            try:
                payload = json.loads(r["payload"])
            except Exception:
                pass
        items.append({
            "rank": i, "code": r["code"], "name": config.NAME.get(r["code"]),
            "score": r.get("score"), "track": r.get("track"),
            "trigger_price": r.get("trigger_price"), "entry_rule": r.get("entry_rule"),
            "source": payload.get("source"),
            # payload 內已算好的欄位一併帶出（原本只取 source，導致明日候選頁
            # close/入選理由全空。verify-history 端點有帶，這裡對齊）。
            "close": payload.get("close"),
            "reasons": payload.get("reasons") or [],
            "missing": payload.get("missing") or [],
            "has_data": payload.get("has_data"),
            **{key: payload.get(key) for key in (
                "classification", "display_pool", "potential_grade", "trend_stage",
                "entry_quality", "entry_state", "entry_state_label",
                "next_upgrade_condition", "reason_tags", "chip_tags",
                "institution_label", "flow_label", "margin_label", "volume_label",
                "decision_summary", "decision_priority", "priority_label",
                "trigger_source", "flow_status_label",
                "pressure_absorption", "buying_efficiency",
                "chip_reversal_acceleration", "decision_rank_score")},
        })
    applies = next_trading_day(d).isoformat()
    scored = [x for x in items if (x.get("score") or 0) > 0]
    if not scored:
        # step 4 池生命週期：收盤前明日候選還沒真正篩出（只有 score 0 佔位）→
        # 盤中回「空白」，不預先決定隔日名單（盤中每小時都在變）。
        # 收盤後 collect 跑完 screen_post，score 有真值才顯示。
        return JSONResponse({
            "data_date": d.isoformat(), "applies_date": applies,
            "purpose": f"待今日收盤後篩選產出（適用 {applies}）— 盤中不預先決定隔日名單",
            "actionable": False, "items": [],
        })
    purpose = f"適用 {applies}(次一交易日)— {len(scored)} 檔候選,非進場名單"
    return JSONResponse({
        "data_date": d.isoformat(), "applies_date": applies,
        "purpose": purpose, "actionable": False, "items": items,
    })


# ════════════════════════════════════════════════════════════════════
# 當日收盤復盤(命中率/勝率)— 衡量模型準度,獨立於名單產生
# ════════════════════════════════════════════════════════════════════

@app.post("/api/verify/run")
def verify_run():
    """用今日收盤復盤昨日候選池,依進場軌判命中,寫 pool_outcome。"""
    return JSONResponse(screen_verify.verify())


@app.get("/api/verify/stats")
def verify_stats(days: int = Query(30, description="滾動交易日窗")):
    """滾動勝率/報酬(分軌)。模型的真正驗證。"""
    return JSONResponse(screen_verify.stats(days))


@app.get("/api/verify/reject")
def verify_reject(date: str = Query("", description="判定日 data_date;預設最近已驗證日"),
                  days: int = Query(30, description="滾動誤刪率窗")):
    """淘汰名單誤刪率(唯讀):被淘汰那批的隔日表現。
    FNR +5%=被淘汰股票中 T+1 盤中最高漲幅≥5%;FNR +9%=同口徑≥9%。
    不以資金流、相對強度或是否跳空過濾母體。
    stats=滾動各淘汰因子誤刪率(最該放寬門檻的排前面);rows=某判定日逐檔明細。"""
    import sqlite3
    import reject_verify
    stats = reject_verify.stats(days)
    conn = sqlite3.connect("mls.db", timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT data_date FROM reject_outcome ORDER BY data_date DESC")]
    dd = date or (dates[0] if dates else None)
    rows = []
    if dd:
        order = {"嚴重誤刪": 0, "誤刪": 1, "排對": 2, "買不到(不計)": 3, "資料不足": 4}
        for r in conn.execute("SELECT * FROM reject_outcome WHERE data_date=?", (dd,)):
            d = dict(r); d["name"] = config.NAME.get(d["code"])
            rows.append(d)
        rows.sort(key=lambda r: (order.get(r["verdict"], 9), -(r["t1_high_ret"] or -999)))
    conn.close()
    return JSONResponse({
        "data_date": dd, "dates": dates,
        "overall_miskill_rate": stats["overall_miskill_rate"],
        "severe_rate": stats["severe_rate"],
        "recovery_kpis": stats.get("recovery_kpis"),
        "by_factor": stats["by_factor"], "daily": stats["daily"],
        "note": "誤刪率高的淘汰因子=最該放寬的門檻;族群相對強度族群index未接,暫用大盤相對代理",
        "rows": rows,
    })


@app.get("/api/verify/history")
def verify_history(date: str = Query("", description="pool_date;預設最近已驗證日")):
    """歷史回測(唯讀):某日候選池的「當時入選理由 vs T+1 最終結果」逐檔對照。
    join pool_outcome(結果:命中/報酬/verdict)＋ candidate_pool(當時 reasons/軌道)。"""
    import sqlite3
    conn = sqlite3.connect("mls.db", timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT pool_date FROM pool_outcome ORDER BY pool_date DESC")]
    pd = date or (dates[0] if dates else None)
    rows = []
    if pd:
        oc = {r["code"]: dict(r) for r in conn.execute(
            "SELECT * FROM pool_outcome WHERE pool_date=?", (pd,))}
        cp = {r["code"]: dict(r) for r in conn.execute(
            "SELECT * FROM candidate_pool WHERE data_date=?", (pd,))}
        for code, o in oc.items():
            c = cp.get(code, {})
            payload = {}
            try:
                payload = json.loads(c.get("payload") or "{}")
            except Exception:
                pass
            # 連買賣天數即時重算(根治[法人資料晚出]:凍結理由常用晚出資料算錯天數/反號)。
            # 用 inst_flow 每日淨額序列重算,替換 reasons 內的「法人連買/連賣N日」。
            _reasons = payload.get("reasons") or []
            try:
                _s = screen_post.streak_from_flow(code, pd, "mls.db")
                if _s is not None:
                    _fx = (f"法人連{'買' if _s > 0 else '賣'}{abs(_s)}日") if _s != 0 else None
                    _reasons = [(_fx if ("法人連買" in r or "法人連賣" in r) else r) for r in _reasons]
                    _reasons = [r for r in _reasons if r]
            except Exception:
                pass
            rows.append({
                "code": code, "name": config.NAME.get(code),
                "track": o.get("track"), "verdict": o.get("verdict"),
                "hit": o.get("hit"), "ret_pct": o.get("ret_pct"),
                "base_close": o.get("base_close"), "next_close": o.get("next_close"),
                "reasons": _reasons,
                "chip_tags": payload.get("chip_tags") or [],
                "next_upgrade_condition": payload.get("next_upgrade_condition"),
                "display_pool": payload.get("display_pool"),
                "entry_state_label": payload.get("entry_state_label"),
                "decision_summary": payload.get("decision_summary"),
                "decision_priority": payload.get("decision_priority"),
                "priority_label": payload.get("priority_label"),
                "trigger_source": payload.get("trigger_source"),
                "flow_status_label": payload.get("flow_status_label"),
            })
        rows.sort(key=lambda r: (0 if r["hit"] == 1 else 1 if r["hit"] == 0 else 2,
                                 -(r["ret_pct"] if r["ret_pct"] is not None else -999)))
    conn.close()
    judged = [r for r in rows if r["hit"] is not None]
    hits = sum(1 for r in judged if r["hit"] == 1)

    # 觀察軌摘要:不計命中,但仍看得到「這批實際 T+1 表現」。
    # Risk Off 期整池被降觀察軌時,denom=0/命中率「—」,唯一有意義的訊號就在這裡。
    obs = [r for r in rows if r["track"] == "觀察"]
    obs_rets = sorted(x["ret_pct"] for x in obs if x["ret_pct"] is not None)
    obs_median = obs_rets[len(obs_rets) // 2] if obs_rets else None
    obs_pos = sum(1 for x in obs_rets if x > 0)

    return JSONResponse({
        "pool_date": pd, "dates": dates,
        "denom": len(judged), "hits": hits,
        "hit_rate": round(hits / len(judged) * 100, 1) if judged else None,
        "obs_n": len(obs), "obs_scored": len(obs_rets),
        "obs_ret_median": obs_median,
        "obs_ret_avg": round(sum(obs_rets) / len(obs_rets), 2) if obs_rets else None,
        "obs_pos": obs_pos,
        "obs_pos_ratio": round(obs_pos / len(obs_rets) * 100, 1) if obs_rets else None,
        "note": "當時入選理由 vs T+1 最終結果；觀察軌不計命中(denom 只含攻擊/引擎軌)",
        "rows": rows,
    })


@app.get("/api/phase")
def phase_info():
    return describe()


@app.get("/api/health")
def health():
    """今天實際打了幾次外部 API。重開服務後應該是 0。"""
    return {
        "fetch_count_today": store.fetch_count_today(),
        "phase": get_phase().value,
    }


@app.get("/")
def index():
    return FileResponse("index.html")
