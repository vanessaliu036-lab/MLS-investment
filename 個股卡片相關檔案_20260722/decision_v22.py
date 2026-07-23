"""
MLS 插件 — decision_v22.py
MLS 資金決策 v2.2 · 盤後決策中心(個股健康指數 × 隔日觀察清單 × 勝率統計)
====================================================================
系統定位(v2.2 核心,本插件即為此定位的落地):

  功能            盤中          盤後
  資金健康度      ⭐⭐⭐⭐⭐(溫度計)  ⭐⭐⭐⭐(驗證盤中判斷)
  訊號版          ⭐⭐⭐⭐⭐        ⭐⭐⭐
  李佛摩六點轉向  ⭐(不刷新)     ⭐⭐⭐⭐⭐(每日15:00後選股中心)
  個股健康指數    ⭐⭐⭐          ⭐⭐⭐⭐⭐(本插件主體)

個股健康指數(Stock Health Index)不是單日模型,是時間序列模型:
  1. 資金持續性 — 今天買,還是連續買(flow-in 連續天數)
  2. 價格反應   — 資金有沒有推動股價(in_up 才算有效)
  3. 資金一致性 — 法人/大戶是否同方向(chips 盤後蓋章)
  4. 健康趨勢   — 今天比昨天健康(象限升級)還是惡化(降級)

它不回答「哪一檔可以買」,它回答「今天資金進入個股後,價格如何反應」,
並把結論落成每天一份「隔日觀察清單」→ 隔日盤後自動驗證 → 累積命中率。
觀察 → 驗證 → 決策 三層分工;真正的交易決策建立在這份觀察之上。

────────────────────────────────────────────────────────────────
純插件:只新增資料表(dec_ 前綴),不改主系統任何檔案。
掛法與 nexora / livermore 相同:
  server.py 加兩行:
      import decision_v22
      app.include_router(decision_v22.router)
  after_hours.run() 尾端插件掛鉤區加(或由 /api/dec/run 手動觸發):
      try:
          import decision_v22
          out = decision_v22.run_report(last_state)
          notifier.push_summary(out["summary"])
      except Exception as e:
          print(f"[plugin/decision] 跳過:{e}")

鐵律(Rule 0,最高優先):
  • 主引擎(config.ENGINE_STOCKS)只當溫度計,永不進觀察清單。
  • 盤中資金流(主動買賣差)≠ 法人;法人一律以 chips 盤後資料蓋章。
  • 盤中資金流出 ≠ 出貨;象限只是市場狀態分類,不是買賣訊號。
"""

import os
import json
from datetime import datetime, timedelta, timezone

import config as C

try:
    import broker
except Exception:
    broker = None
try:
    import db
except Exception:
    db = None
try:
    import chips as _chips
except Exception:
    _chips = None
try:
    import scoring as _scoring
except Exception:
    _scoring = None

TW_TZ = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(__file__)
REPORT_DIR = os.path.join(BASE_DIR, "reports")

# ── 可調參數(對接只改這裡) ──────────────────────────────
SUCCESS_PCT = 0.3        # 隔日收盤 ≥ +0.3% = 達標(與主系統成敗判定一致)
TRIGGER_MODE = "high"    # 觸發價=觀察日最高價(突破前高才算觸發)
MAX_HOLD_DAYS = 5        # 模擬持有上限(天)
EXIT_RULE = "break_entry_low"   # 出場:收盤跌破進場日低點,或滿5日
TOP_N_WATCH = 12         # 隔日觀察清單上限
READY_MIN = 65           # Ready 門檻(健康分)
WATCH_MIN = 50           # Watch 門檻
STATS_DAYS = 30          # 勝率統計滾動窗
FLOW_EPS = 0.02          # aflow_ratio 視為有方向的最小值

QUAD_RANK = {"out_down": 0, "in_down": 1, "out_up": 2, "in_up": 3}
QUAD_NAME = {"in_up": "流入↗漲(健康)", "in_down": "流入↗跌(假紅/待驗證)",
             "out_up": "流出↘漲(惜售)", "out_down": "流出↘跌(休息)"}


# ════════════════════════════════════════════════════════
# 〇、資料表(插件自建 dec_ 前綴,不動主 schema)
# ════════════════════════════════════════════════════════
def _init_tables():
    with db._lock, db._conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS dec_health(
          trade_date TEXT, code TEXT, name TEXT, sector TEXT,
          stock_type TEXT, close REAL, high REAL, low REAL,
          chg REAL, aflow_ratio REAL, flow_src TEXT,
          quadrant TEXT, flow_streak INTEGER,
          trend TEXT,                 -- 改善 / 持平 / 惡化 / 新增
          chip_ok INTEGER,            -- 1 蓋章 / 0 反向 / NULL 無資料
          chip_note TEXT,
          score INTEGER, grade TEXT,  -- Ready / Watch / Hold / 溫度計
          PRIMARY KEY(trade_date, code)
        );
        CREATE TABLE IF NOT EXISTS dec_watchlist(
          obs_date TEXT,              -- 產出清單的觀察日
          target_date TEXT,           -- 要驗證的隔日(引擎軌=進場日)
          code TEXT, name TEXT, sector TEXT, track TEXT,
          grade TEXT, score INTEGER, quadrant TEXT, trend TEXT,
          base_close REAL, trigger_price REAL, reason TEXT,
          PRIMARY KEY(target_date, code)
        );
        CREATE TABLE IF NOT EXISTS dec_verify(
          target_date TEXT, code TEXT, grade TEXT, track TEXT,
          triggered INTEGER,          -- 隔日最高 > 觸發價
          entered INTEGER,            -- triggered 且 grade=Ready
          next_high_pct REAL,         -- 隔日最高 vs 觀察日收盤
          next_close_pct REAL,        -- 隔日收盤 vs 觀察日收盤
          success INTEGER,            -- next_close_pct >= SUCCESS_PCT
          hold_days INTEGER,          -- 模擬持有天數(entered 才算)
          hold_ret_pct REAL,          -- 模擬持有報酬(vs 觸發價)
          verified_ts TEXT,
          PRIMARY KEY(target_date, code)
        );
        CREATE INDEX IF NOT EXISTS idx_dec_health_code
          ON dec_health(code, trade_date);
        """)
        # v3.0 舊庫遷移:補 track 欄
        for t in ("dec_watchlist", "dec_verify"):
            try:
                c.execute(f"ALTER TABLE {t} ADD COLUMN track TEXT")
            except Exception:
                pass


def _today():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d")


def _next_trade_date(from_date=None):
    d = datetime.strptime(from_date or _today(), "%Y-%m-%d")
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


# ════════════════════════════════════════════════════════
# 一、收盤快照取得(state 優先,缺失時自行重抓)
# ════════════════════════════════════════════════════════
def _resolve_snaps(state):
    snaps = (state or {}).get("_snaps") or []
    cov = len({s["code"] for s in snaps} & set(C.UNIVERSE)) / max(1, len(C.UNIVERSE))
    if cov >= 0.8:
        return snaps, "intraday_state"
    try:
        import eod_pipeline
        fresh = eod_pipeline.fetch_eod_snaps()
        if fresh:
            return fresh, "eod_refetch"
    except Exception as e:
        print(f"[plugin/decision] EOD 重抓失敗:{e}")
    return snaps, "degraded"


def _aflow_ratio(s, flow_src):
    """
    主動買賣差 / 總量。鐵律:這是盤中主動單估計,不是法人。
    server 重啟或 EOD 重抓時 aflow 歸零 → 降級用量比方向近似,
    並在 flow_src 標 'vr_proxy' 讓報告誠實揭露。
    """
    ratio, src = 0.0, flow_src
    if _scoring is not None:
        try:
            af = _scoring.get_aflow(s["code"])
            tv = s.get("total_volume") or 0
            if tv > 0 and af:
                ratio = af / tv
        except Exception:
            pass
    if abs(ratio) < 1e-9:                       # 無盤中累積 → 量比近似
        vr = s.get("volume_ratio") or 0
        chg = s.get("change_rate") or 0
        ratio = 0.05 if (vr >= 1.0 and chg >= 0) else \
                (-0.05 if (vr >= 1.0 and chg < 0) else
                 (0.01 if chg >= 0 else -0.01))
        src = flow_src + "+vr_proxy"
    return round(ratio, 3), src


def _quadrant(ratio, chg):
    flow_in = ratio >= 0
    if flow_in and chg >= 0:
        return "in_up"
    if flow_in:
        return "in_down"
    return "out_up" if chg >= 0 else "out_down"


def _get_chip(code):
    """chips 盤後蓋章(經 chip_provider,quality 誠實標記)。
    回傳 (chip_ok 1/0/None, note)。失敗一律 None。"""
    try:
        import chip_provider
        ch, quality = chip_provider.get_chip_data(code)
        ch = ch or {}
    except Exception:
        if _chips is None:
            return None, "chips 模組不可用"
        try:
            ch, quality = (_chips.get_chips(code) or {}), "finmind_basic"
        except Exception:
            return None, "籌碼待補(chips 未回應)"
    net = ch.get("inst_net_20d_lots")
    streak = ch.get("inst_streak")
    trend = ch.get("big_holder_trend")
    if net is None and streak is None and trend is None:
        return None, "無籌碼資料"
    pos = ((net or 0) > 0) or ((streak or 0) >= 3) or ((trend or 0) > 0)
    neg = ((net or 0) < 0 and (streak or 0) <= -3)
    note = (f"法人近月{(net or 0):+,}張"
            + (f",連{'買' if (streak or 0) > 0 else '賣'}{abs(streak)}日"
               if streak else "")
            + (f",大戶{trend:+.1f}pp" if trend is not None else ""))
    if pos and not neg:
        return 1, note
    if neg:
        return 0, note
    return None, note


# ════════════════════════════════════════════════════════
# 二、個股健康指數(時間序列)— 核心公式
# ════════════════════════════════════════════════════════
def _prev_health(code, before_date):
    with db._lock, db._conn() as c:
        r = c.execute("""SELECT * FROM dec_health WHERE code=? AND trade_date<?
                         ORDER BY trade_date DESC LIMIT 1""",
                      (code, before_date)).fetchone()
        return dict(r) if r else None


def _livermore_state(code):
    """選配:讀李佛摩六欄+六點轉向落地表(無表時安靜跳過)。"""
    out = None
    try:
        with db._lock, db._conn() as c:
            r = c.execute("""SELECT state, pivot FROM livermore_record
                             WHERE code=? ORDER BY trade_date DESC LIMIT 1""",
                          (code,)).fetchone()
            out = dict(r) if r else None
    except Exception:
        pass
    try:
        with db._lock, db._conn() as c:
            r = c.execute("""SELECT qualified, retest50_hold FROM livermore_sixpoint
                             WHERE code=? ORDER BY trade_date DESC LIMIT 1""",
                          (code,)).fetchone()
            if r:
                out = out or {}
                out["sixpoint"] = r["qualified"]
                out["retest50_hold"] = r["retest50_hold"]
    except Exception:
        pass
    return out


def score_stock(quadrant, trend, flow_streak, chg, chip_ok, liv=None):
    """
    健康分 0–100。四個構面(對齊 SHI 核心邏輯 v1):
      ① 象限基礎(價格反應):in_up 60 / in_down 45 / out_up 40 / out_down 25
      ② 資金持續性:flow-in 連續天數,每日 +4,上限 +12
      ③ 健康趨勢:改善 +10 / 持平 0 / 惡化 −10
      ④ 資金一致性(chips 蓋章):+15 / 反向 −10 / 無資料 0
      ⑤ 價格推動加成:in_up 且漲 ≥2% +8;跌 ≤−3% −8
      ⑥ 李佛摩結構(選配):上升趨勢 +5,多方關鍵點 +5,\n         六點合格 long +8 / short −8,50%回測未破 +3
    """
    base = {"in_up": 60, "in_down": 45, "out_up": 40, "out_down": 25}[quadrant]
    s = base
    s += min(12, max(0, (flow_streak - 1)) * 4)
    s += {"改善": 10, "持平": 0, "惡化": -10, "新增": 0}.get(trend, 0)
    if chip_ok == 1:
        s += 15
    elif chip_ok == 0:
        s -= 10
    if quadrant == "in_up" and (chg or 0) >= 2:
        s += 8
    if (chg or 0) <= -3:
        s -= 8
    if liv:
        if liv.get("state") == "上升趨勢":
            s += 5
        if liv.get("pivot") and str(liv["pivot"]).startswith("多方"):
            s += 5
        if liv.get("sixpoint") == "long":      # 六點轉向正式合格(盤後選股中心)
            s += 8
        elif liv.get("sixpoint") == "short":
            s -= 8
        if liv.get("retest50_hold") == 1:      # 50% 回測未破
            s += 3
    return int(max(0, min(100, s)))


ENGINE_READY_MIN = 60      # 引擎軌 Ready 門檻(波段,標準略寬)
ENGINE_SUCCESS_PCT = 1.0   # 引擎軌達標:5日收盤 ≥ +1% 且未收破月線
ENGINE_MAX_HOLD = 10       # 引擎軌模擬持有上限(日)


def _ma20_of(bars, idx=None):
    closes = [b["close"] for b in bars if b.get("close") is not None]
    if idx is not None:
        closes = [b["close"] for b in bars[:idx + 1] if b.get("close") is not None]
    if len(closes) < 20:
        return None
    return sum(closes[-20:]) / 20


def grade_of(code, quadrant, trend, score, chip_ok, track="attack",
             above_ma20=None):
    """v3.0 雙軌:引擎/攻擊是玩法標籤,不是資格門檻。"""
    if track == "engine":
        # 引擎軌(波段):站上月線 + 法人未反向 + 健康分達標
        if above_ma20 and score >= ENGINE_READY_MIN and chip_ok != 0:
            return "Ready"
        if above_ma20 or score >= WATCH_MIN:
            return "Watch"
        return "Hold"
    if score >= READY_MIN and quadrant == "in_up" and chip_ok != 0:
        return "Ready"
    if score >= WATCH_MIN or (quadrant == "in_down" and trend == "改善"):
        return "Watch"
    return "Hold"


_ENGINE_BARS = None


def record_today(snaps, flow_src, trade_date=None, engine_bars=None):
    global _ENGINE_BARS
    _ENGINE_BARS = engine_bars
    """把觀察池全數寫入 dec_health(每日一列,系統的核心資料資產)。"""
    tdate = trade_date or _today()
    rows = []
    for s in snaps:
        code = s.get("code")
        if code not in C.SECTOR_MAP:
            continue
        sector, styp = C.SECTOR_MAP[code]
        chg = s.get("change_rate") or 0
        ratio, src = _aflow_ratio(s, flow_src)
        quad = _quadrant(ratio, chg)
        prev = _prev_health(code, tdate)
        if prev is None:
            trend = "新增"
            streak = 1 if quad.startswith("in") else 0
        else:
            d = QUAD_RANK[quad] - QUAD_RANK.get(prev["quadrant"], 1)
            trend = "改善" if d > 0 else ("惡化" if d < 0 else "持平")
            streak = (prev.get("flow_streak") or 0) + 1 \
                if quad.startswith("in") and str(prev["quadrant"]).startswith("in") \
                else (1 if quad.startswith("in") else 0)
        chip_ok, chip_note = _get_chip(code)
        liv = _livermore_state(code)
        score = score_stock(quad, trend, streak, chg, chip_ok, liv)
        track = "engine" if code in C.ENGINE_STOCKS else "attack"
        above_ma20 = None
        if track == "engine":
            bars = _bars_map(code, days=40,
                             injected=(_ENGINE_BARS or {}).get(code))
            ma20 = _ma20_of(bars)
            if ma20 and s.get("price"):
                above_ma20 = s["price"] >= ma20
        grade = grade_of(code, quad, trend, score, chip_ok,
                         track=track, above_ma20=above_ma20)
        rows.append({
            "trade_date": tdate, "code": code,
            "name": C.NAME_MAP.get(code, code), "sector": sector,
            "stock_type": track,
            "close": s.get("price"), "high": s.get("high"),
            "low": s.get("low"), "chg": chg,
            "aflow_ratio": ratio, "flow_src": src,
            "quadrant": quad, "flow_streak": streak, "trend": trend,
            "chip_ok": chip_ok, "chip_note": chip_note,
            "score": score, "grade": grade,
        })
    with db._lock, db._conn() as c:
        for r in rows:
            c.execute("""INSERT OR REPLACE INTO dec_health VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (r["trade_date"], r["code"], r["name"], r["sector"],
               r["stock_type"], r["close"], r["high"], r["low"], r["chg"],
               r["aflow_ratio"], r["flow_src"], r["quadrant"],
               r["flow_streak"], r["trend"], r["chip_ok"], r["chip_note"],
               r["score"], r["grade"]))
    return rows


# ════════════════════════════════════════════════════════
# 三、隔日觀察清單(每日 15:00 後產出並落地 = 未來可分析的數據)
# ════════════════════════════════════════════════════════
def build_watchlist(rows, obs_date=None):
    obs = obs_date or _today()
    target = _next_trade_date(obs)
    cand = [r for r in rows if r["grade"] in ("Ready", "Watch")]
    cand.sort(key=lambda r: (-{"Ready": 1, "Watch": 0}[r["grade"]], -r["score"]))
    cand = cand[:TOP_N_WATCH]
    wl = []
    for r in cand:
        is_eng = r["stock_type"] == "engine"
        trig = r["close"] if is_eng else (r["high"] or r["close"])
        reason = ((f"引擎軌(波段):站上月線進場,月線停損 · " if is_eng else "")
                  + f"{QUAD_NAME[r['quadrant']]} · 趨勢{r['trend']}"
                  f" · 資金連續{r['flow_streak']}日"
                  f" · {r['chip_note'] or '籌碼未蓋章'}"
                  f" · 今日{(r['chg'] or 0):+.1f}%")
        wl.append({"obs_date": obs, "target_date": target,
                   "code": r["code"], "name": r["name"], "sector": r["sector"],
                   "track": r["stock_type"],
                   "grade": r["grade"], "score": r["score"],
                   "quadrant": r["quadrant"], "trend": r["trend"],
                   "base_close": r["close"], "trigger_price": trig,
                   "reason": reason})
    with db._lock, db._conn() as c:
        for w in wl:
            c.execute("""INSERT OR REPLACE INTO dec_watchlist
              (obs_date,target_date,code,name,sector,track,grade,score,
               quadrant,trend,base_close,trigger_price,reason)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (w["obs_date"], w["target_date"], w["code"], w["name"],
               w["sector"], w["track"], w["grade"], w["score"], w["quadrant"],
               w["trend"], w["base_close"], w["trigger_price"], w["reason"]))
    return wl, target


# ════════════════════════════════════════════════════════
# 四、隔日驗證(把每一份清單變成可統計的樣本)
# ════════════════════════════════════════════════════════
def _bars_map(code, days=MAX_HOLD_DAYS + 25, injected=None):
    if injected is not None:
        return injected
    if broker is None:
        return []
    try:
        raw = broker.daily_kbars(code, days=days)
    except Exception as e:
        print(f"[plugin/decision] {code} 日K失敗:{e}")
        return []
    bars = []
    for r in raw:
        cl = r.get("close")
        bars.append({"date": str(r.get("date"))[:10], "close": cl,
                     "high": r.get("high"), "low": r.get("low", cl)})
    return bars


def verify_pending(injected_bars=None, today=None):
    """
    對所有「target_date 已過、尚未驗證」的觀察清單列補驗證:
      是否觸發 = 隔日最高 > 觸發價(觀察日最高)
      是否進場 = 觸發 且 grade=Ready
      隔日最高% / 隔日收盤% = vs 觀察日收盤
      是否達標 = 隔日收盤 ≥ +SUCCESS_PCT%
      模擬持有 = 觸發價進場,收盤跌破進場日低點出場,上限 MAX_HOLD_DAYS 日
    """
    tdate = today or _today()
    with db._lock, db._conn() as c:
        pend = [dict(r) for r in c.execute("""
          SELECT w.* FROM dec_watchlist w
          LEFT JOIN dec_verify v ON v.target_date=w.target_date AND v.code=w.code
          WHERE v.code IS NULL AND w.target_date <= ?""", (tdate,))]
    done = []
    for w in pend:
        inj = (injected_bars or {}).get(w["code"])
        bars = _bars_map(w["code"], days=60, injected=inj)
        idx = next((i for i, b in enumerate(bars)
                    if b["date"] == w["target_date"]), None)
        if idx is None or not w.get("base_close"):
            continue                      # 該日尚無日K,下次再驗
        track = w.get("track") or ("engine" if w["code"] in C.ENGINE_STOCKS
                                   else "attack")
        nb = bars[idx]
        base = w["base_close"]
        nh = round((nb["high"] - base) / base * 100, 2) if nb.get("high") else None
        nc = round((nb["close"] - base) / base * 100, 2) if nb.get("close") else None
        trig_p = w["trigger_price"] or base

        if track == "engine":
            # ── 引擎軌(波段):觀察日收盤進場;達標=第5日收盤≥+1%且期間未收破月線
            win = bars[idx:idx + 5]
            ma_break = False
            for j, b in enumerate(win):
                ma = _ma20_of(bars, idx + j)
                if ma and (b.get("close") or 0) < ma:
                    ma_break = True
                    break
            if len(win) < 5 and not ma_break:
                continue                  # 5日窗未滿且未破線 → 之後再驗
            triggered, entered = 1, (1 if w["grade"] == "Ready" else 0)
            end_close = win[min(4, len(win) - 1)].get("close")
            ret5 = round((end_close - base) / base * 100, 2) if end_close else None
            success = 1 if (ret5 is not None and ret5 >= ENGINE_SUCCESS_PCT
                            and not ma_break) else 0
            nc = ret5 if ret5 is not None else nc   # 引擎軌收盤欄=5日報酬
            hold_days, hold_ret = None, None
            if entered:
                hold_days, exit_close = 1, nb.get("close")
                for j in range(idx + 1, min(idx + ENGINE_MAX_HOLD, len(bars))):
                    b = bars[j]
                    hold_days += 1
                    exit_close = b.get("close")
                    ma = _ma20_of(bars, j)
                    if ma and (b.get("close") or 0) < ma:   # 收破月線出場
                        break
                if exit_close and base:
                    hold_ret = round((exit_close - base) / base * 100, 2)
        else:
            # ── 攻擊軌(短線):沿用突破觸發/隔日達標/破進場日低出場
            triggered = 1 if (nb.get("high") or 0) > trig_p else 0
            entered = 1 if (triggered and w["grade"] == "Ready") else 0
            success = 1 if (nc is not None and nc >= SUCCESS_PCT) else 0
            hold_days, hold_ret = None, None
            if entered:
                entry_low = nb.get("low") or nb.get("close")
                hold_days, exit_close = 1, nb.get("close")
                for j in range(idx + 1, min(idx + MAX_HOLD_DAYS, len(bars))):
                    b = bars[j]
                    hold_days += 1
                    exit_close = b.get("close")
                    if (b.get("close") or 0) < (entry_low or 0):
                        break
                if exit_close and trig_p:
                    hold_ret = round((exit_close - trig_p) / trig_p * 100, 2)
        with db._lock, db._conn() as c:
            c.execute("""INSERT OR REPLACE INTO dec_verify
              (target_date,code,grade,track,triggered,entered,next_high_pct,
               next_close_pct,success,hold_days,hold_ret_pct,verified_ts)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
              (w["target_date"], w["code"], w["grade"], track, triggered,
               entered, nh, nc, success, hold_days, hold_ret,
               datetime.now(TW_TZ).isoformat(timespec="seconds")))
        done.append({**w, "track": track, "triggered": triggered, "entered": entered,
                     "next_high_pct": nh, "next_close_pct": nc,
                     "success": success, "hold_days": hold_days,
                     "hold_ret_pct": hold_ret})
    return done


# ════════════════════════════════════════════════════════
# 五、勝率統計(30 日滾動 — 模型的真正驗證)
# ════════════════════════════════════════════════════════
def stats(days=STATS_DAYS):
    since = (datetime.now(TW_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    out = {"window_days": days, "since": since, "grades": [],
           "tracks": [],
           "score_buckets": [], "quadrants": [], "hold_avoid": None}
    with db._lock, db._conn() as c:
        # ① 分級命中率 / 平均報酬 / 平均持有 / 最大回撤
        for g in ("Ready", "Watch"):
            r = c.execute("""SELECT COUNT(*) n, SUM(success) s,
                 AVG(next_close_pct) ac, AVG(next_high_pct) ah,
                 AVG(hold_days) hd, AVG(hold_ret_pct) hr,
                 SUM(triggered) tg, SUM(entered) en
               FROM dec_verify WHERE grade=? AND target_date>=?""",
               (g, since)).fetchone()
            n = r["n"] or 0
            # 最大回撤:進場交易依日期序的持有報酬累計曲線峰谷差(%)
            rets = [x["hold_ret_pct"] for x in c.execute(
                """SELECT hold_ret_pct FROM dec_verify
                   WHERE grade=? AND entered=1 AND hold_ret_pct IS NOT NULL
                     AND target_date>=? ORDER BY target_date""",
                (g, since))]
            mdd = None
            if rets:
                cum = peak = 0.0
                mdd = 0.0
                for x in rets:
                    cum += x
                    peak = max(peak, cum)
                    mdd = max(mdd, peak - cum)
                mdd = round(mdd, 2)
            out["grades"].append({
                "grade": g, "total": n, "success": r["s"] or 0,
                "hit_rate": round((r["s"] or 0) / n * 100, 1) if n else None,
                "triggered": r["tg"] or 0, "entered": r["en"] or 0,
                "avg_next_close_pct": round(r["ac"], 2) if r["ac"] is not None else None,
                "avg_next_high_pct": round(r["ah"], 2) if r["ah"] is not None else None,
                "avg_hold_days": round(r["hd"], 1) if r["hd"] is not None else None,
                "avg_hold_ret_pct": round(r["hr"], 2) if r["hr"] is not None else None,
                "max_drawdown_pct": mdd,
            })
        # ①b 分軌統計(v3.0:引擎軌/攻擊軌各自命中率——重點觀察數據)
        for tk, tk_name in (("engine", "引擎軌(波段)"), ("attack", "攻擊軌(短線)")):
            for g in ("Ready", "Watch"):
                r = c.execute("""SELECT COUNT(*) n, SUM(success) sc,
                     AVG(next_close_pct) ac, AVG(hold_days) hd,
                     AVG(hold_ret_pct) hr, SUM(entered) en
                   FROM dec_verify WHERE track=? AND grade=? AND target_date>=?""",
                   (tk, g, since)).fetchone()
                n = r["n"] or 0
                rets = [x["hold_ret_pct"] for x in c.execute(
                    """SELECT hold_ret_pct FROM dec_verify
                       WHERE track=? AND grade=? AND entered=1
                         AND hold_ret_pct IS NOT NULL AND target_date>=?
                       ORDER BY target_date""", (tk, g, since))]
                mdd = None
                if rets:
                    cum = peak = 0.0; mdd = 0.0
                    for x in rets:
                        cum += x; peak = max(peak, cum); mdd = max(mdd, peak - cum)
                    mdd = round(mdd, 2)
                out["tracks"].append({
                    "track": tk, "track_name": tk_name, "grade": g,
                    "total": n, "success": r["sc"] or 0,
                    "hit_rate": round((r["sc"] or 0) / n * 100, 1) if n else None,
                    "entered": r["en"] or 0,
                    "avg_ret_pct": round(r["ac"], 2) if r["ac"] is not None else None,
                    "avg_hold_days": round(r["hd"], 1) if r["hd"] is not None else None,
                    "avg_hold_ret_pct": round(r["hr"], 2) if r["hr"] is not None else None,
                    "max_drawdown_pct": mdd,
                    "success_def": ("5日收盤≥+1%且未收破月線" if tk == "engine"
                                    else f"隔日收盤≥+{SUCCESS_PCT}%")})

        # ② 分數區間實際勝率(90–100 / 80–89 / 70–79 / 65–69 / 50–64)
        for lo, hi in ((90, 100), (80, 89), (70, 79), (65, 69), (50, 64)):
            r = c.execute("""SELECT COUNT(*) n, SUM(v.success) s,
                 AVG(v.next_close_pct) ac
               FROM dec_verify v JOIN dec_watchlist w
                 ON w.target_date=v.target_date AND w.code=v.code
               WHERE w.score BETWEEN ? AND ? AND v.target_date>=?""",
               (lo, hi, since)).fetchone()
            n = r["n"] or 0
            out["score_buckets"].append({
                "bucket": f"{lo}–{hi}", "total": n,
                "hit_rate": round((r["s"] or 0) / n * 100, 1) if n else None,
                "avg_next_close_pct": round(r["ac"], 2) if r["ac"] is not None else None})
        # ③ 四象限隔日表現(全池 dec_health 自我連結,不限清單)
        rows = c.execute("""
          SELECT h.quadrant q, COUNT(*) n, AVG(n2.chg) avg_next,
                 SUM(CASE WHEN n2.chg >= ? THEN 1 ELSE 0 END) win
          FROM dec_health h
          JOIN dec_health n2 ON n2.code=h.code AND n2.trade_date=(
            SELECT MIN(trade_date) FROM dec_health
            WHERE code=h.code AND trade_date>h.trade_date)
          WHERE h.trade_date>=? GROUP BY h.quadrant""",
          (SUCCESS_PCT, since)).fetchall()
        for r in rows:
            out["quadrants"].append({
                "quadrant": r["q"], "name": QUAD_NAME.get(r["q"], r["q"]),
                "total": r["n"],
                "avg_next_chg": round(r["avg_next"], 2) if r["avg_next"] is not None else None,
                "win_rate": round((r["win"] or 0) / r["n"] * 100, 1) if r["n"] else None})
        # ④ Hold 避開虧損率(健康分低的股票,隔日是否真的弱)
        r = c.execute("""
          SELECT COUNT(*) n,
                 SUM(CASE WHEN n2.chg < ? THEN 1 ELSE 0 END) avoided
          FROM dec_health h
          JOIN dec_health n2 ON n2.code=h.code AND n2.trade_date=(
            SELECT MIN(trade_date) FROM dec_health
            WHERE code=h.code AND trade_date>h.trade_date)
          WHERE h.grade='Hold' AND h.trade_date>=?""",
          (SUCCESS_PCT, since)).fetchone()
        if r["n"]:
            out["hold_avoid"] = {"total": r["n"],
                                 "avoid_rate": round((r["avoided"] or 0) / r["n"] * 100, 1)}
    return out


# ════════════════════════════════════════════════════════
# 六、插件入口 run_report(state) — 與 nexora / livermore 同契約
# ════════════════════════════════════════════════════════
def run_report(state=None, injected_snaps=None, injected_bars=None,
               trade_date=None):
    _init_tables()
    tdate = trade_date or _today()

    # ① 先驗證舊清單(今天的日K已可取得昨天目標日的結果)
    verified = verify_pending(injected_bars=injected_bars, today=tdate)

    # ② 收盤快照 → 全池健康指數落地(時間序列的每日一格)
    if injected_snaps is not None:
        snaps, src = injected_snaps, "injected"
    else:
        snaps, src = _resolve_snaps(state)
    rows = record_today(snaps, src, trade_date=tdate,
                        engine_bars=injected_bars) if snaps else []

    # ③ 隔日觀察清單
    wl, target = (build_watchlist(rows, obs_date=tdate)
                  if rows else ([], _next_trade_date(tdate)))

    # ④ 勝率統計
    st = stats()

    # ⑤ 報告
    d = datetime.strptime(tdate, "%Y-%m-%d")
    L = [f"# MLS 資金決策 v3.0 盤後報告(雙軌) {tdate}",
         f"資料來源:{src}(vr_proxy=盤中主動單缺失,以量比方向近似,僅供降級參考)",
         "", "## 1. 隔日觀察清單(目標日 " + target + ")"]
    if wl:
        for w in wl:
            L.append(f"- **{w['name']}({w['code']})** {w['sector']}｜"
                     f"{w['grade']} {w['score']}分｜觸發>{w['trigger_price']}｜{w['reason']}")
    else:
        L.append("(今日無符合條件之觀察標的;休息也是部位。)")
    L.append("\n## 2. 昨日清單驗證(觀察→驗證閉環)")
    if verified:
        L.append("| 股票 | AI | 觸發 | 進場 | 隔日最高 | 隔日收盤 | 達標 |")
        L.append("|---|---|---|---|---|---|---|")
        for v in verified:
            L.append(f"| {v['name']}({v['code']}) | {v['grade']} | "
                     f"{'✓' if v['triggered'] else '✗'} | "
                     f"{'✓' if v['entered'] else '✗'} | "
                     f"{v['next_high_pct']:+.2f}% | {v['next_close_pct']:+.2f}% | "
                     f"{'✓' if v['success'] else '✗'} |")
    else:
        L.append("(無待驗證清單,或目標日日K尚未產生。)")
    L.append(f"\n## 3. 最近 {st['window_days']} 天勝率統計(分軌)")
    for t in st["tracks"]:
        if t["total"]:
            L.append(f"- **{t['track_name']} {t['grade']}** 共{t['total']}檔 "
                     f"成功{t['success']} 命中率 **{t['hit_rate']}%**"
                     f"｜均酬 {t['avg_ret_pct']:+.2f}%｜持有 {t['avg_hold_days'] or '—'}天"
                     f"｜最大回撤 {t['max_drawdown_pct'] if t['max_drawdown_pct'] is not None else '—'}%"
                     f"（達標={t['success_def']}）")
    L.append("### 合併統計(不分軌,舊制對照)")
    for g in st["grades"]:
        if g["total"]:
            L.append(f"- **{g['grade']}** 共{g['total']}檔 成功{g['success']}檔 "
                     f"命中率 **{g['hit_rate']}%**｜隔日收盤均 {g['avg_next_close_pct']:+.2f}%"
                     f"｜平均持有 {g['avg_hold_days'] or '—'} 天"
                     f"（持有均酬 {g['avg_hold_ret_pct'] if g['avg_hold_ret_pct'] is not None else '—'}%）")
        else:
            L.append(f"- {g['grade']}:樣本累積中")
    for q in st["quadrants"]:
        L.append(f"- 象限 {q['name']}:{q['total']} 樣本,隔日均 "
                 f"{q['avg_next_chg']:+.2f}%,勝率 {q['win_rate']}%")
    if st["hold_avoid"]:
        L.append(f"- Hold 避開虧損率:{st['hold_avoid']['avoid_rate']}%"
                 f"({st['hold_avoid']['total']} 樣本)")
    L.append("\n## 4. 鐵律備註")
    L.append("- 本報告為觀察模型輸出:象限=市場狀態分類,不是買賣訊號。")
    L.append("- 盤中資金流出 ≠ 出貨;籌碼一律以 chips 盤後資料蓋章。")
    L.append("- v3.0 雙軌:引擎股走波段軌(月線進出),攻擊股走短線軌(突破/ATR),角色由 engine_review 每週依數據輪替,不寫死。")
    L.append(f"- 達標定義:隔日收盤 ≥ +{SUCCESS_PCT}%;觸發=隔日過觀察日高;"
             f"模擬出場=收盤破進場日低或滿 {MAX_HOLD_DAYS} 日。")

    report_md = "\n".join(L)
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"DECISION_{d:%Y%m%d}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report_md)

    ready = [g for g in st["grades"] if g["grade"] == "Ready"]
    hr = ready[0]["hit_rate"] if ready and ready[0]["total"] else None
    eng_n = sum(1 for w in wl if w.get("track") == "engine")
    summary = (f"🎯 決策v3.0｜觀察 {len(wl)} 檔(引擎軌{eng_n}/攻擊軌{len(wl)-eng_n},→{target})"
               f"｜驗證回填 {len(verified)} 檔"
               f"｜30日Ready命中率 {hr if hr is not None else '累積中'}"
               f"{'%' if hr is not None else ''}")
    return {"path": path, "summary": summary, "report": report_md,
            "watchlist": wl, "verified": verified, "stats": st}


# ════════════════════════════════════════════════════════
# 七、API Router(server.py 掛一行)
# ════════════════════════════════════════════════════════
try:
    from fastapi import APIRouter
    from fastapi.responses import FileResponse, JSONResponse

    router = APIRouter()

    @router.get("/decision")
    def page():
        return FileResponse(os.path.join(BASE_DIR, "decision.html"))

    @router.get("/api/dec/overview")
    def api_overview():
        _init_tables()
        with db._lock, db._conn() as c:
            r = c.execute("SELECT MAX(trade_date) d FROM dec_health").fetchone()
            tdate = r["d"]
            rows = [dict(x) for x in c.execute(
                "SELECT * FROM dec_health WHERE trade_date=? ORDER BY score DESC",
                (tdate,))] if tdate else []
        return {"date": tdate, "rows": rows, "quad_name": QUAD_NAME}

    @router.get("/api/dec/watchlist")
    def api_watchlist(date: str = ""):
        _init_tables()
        with db._lock, db._conn() as c:
            if not date:
                r = c.execute("SELECT MAX(target_date) d FROM dec_watchlist").fetchone()
                date = r["d"] or ""
            rows = [dict(x) for x in c.execute(
                """SELECT * FROM dec_watchlist WHERE target_date=?
                   ORDER BY CASE grade WHEN 'Ready' THEN 0 ELSE 1 END, score DESC""",
                (date,))]
        return {"target_date": date, "rows": rows}

    @router.get("/api/dec/verify")
    def api_verify(days: int = STATS_DAYS):
        _init_tables()
        since = (datetime.now(TW_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
        with db._lock, db._conn() as c:
            rows = [dict(x) for x in c.execute("""
              SELECT v.*, w.name, w.sector, w.score, w.base_close,
                     w.trigger_price, w.reason
              FROM dec_verify v JOIN dec_watchlist w
                ON w.target_date=v.target_date AND w.code=v.code
              WHERE v.target_date>=? ORDER BY v.target_date DESC, w.score DESC""",
              (since,))]
        return {"since": since, "rows": rows}

    @router.get("/api/dec/stats")
    def api_stats(days: int = STATS_DAYS):
        _init_tables()
        return stats(days)

    @router.get("/api/dec/card")
    def api_card(code: str):
        """v2.3 個股資訊卡:籌碼/資金/技術/交易計畫/AI 結論。"""
        _init_tables()
        try:
            import stock_card
            snap = None
            try:
                if broker is not None:
                    ss = broker.batch_snapshots([code])
                    snap = ss[0] if ss else None
            except Exception:
                pass
            return stock_card.build_card(code, snap=snap)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @router.get("/api/dec/brief")
    def api_brief():
        """v2.3 盤面速覽:資金流入前三族群(當日成交金額,億)。"""
        try:
            import stock_card
            return {"top": stock_card.market_brief()}
        except Exception as e:
            return JSONResponse({"top": [], "error": str(e)}, status_code=200)

    @router.post("/api/dec/run")
    def api_run():
        try:
            out = run_report(None)
            return {"ok": True, "summary": out["summary"],
                    "watch": len(out["watchlist"]),
                    "verified": len(out["verified"])}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

except Exception as _e:      # 無 fastapi 的離線測試環境
    router = None
    print(f"[plugin/decision] router 未載入:{_e}")


# ════════════════════════════════════════════════════════
# 冒煙測試:python decision_v22.py(合成三天資料驗證閉環)
# ════════════════════════════════════════════════════════
if __name__ == "__main__":
    import random
    random.seed(7)

    def synth_snaps(day_chg):
        out = []
        for code in C.UNIVERSE:
            chg = day_chg + random.uniform(-2.5, 2.5)
            px = 100 + random.uniform(-30, 60)
            out.append({"code": code, "price": round(px, 1),
                        "high": round(px * 1.02, 1), "low": round(px * 0.98, 1),
                        "change_rate": round(chg, 2),
                        "total_volume": random.randint(2000, 30000),
                        "volume_ratio": round(random.uniform(0.6, 2.4), 2)})
        return out

    # 日K:讓部分股票隔日過高且收漲
    def synth_bars():
        m = {}
        for code in C.UNIVERSE:
            base = 100 + random.uniform(-30, 60)
            bars, p = [], base
            for i, dt in enumerate(["2026-07-06", "2026-07-07", "2026-07-08",
                                    "2026-07-09", "2026-07-10"]):
                p *= 1 + random.uniform(-0.02, 0.035)
                bars.append({"date": dt, "close": round(p, 1),
                             "high": round(p * 1.025, 1),
                             "low": round(p * 0.985, 1)})
            m[code] = bars
        return m

    bars = synth_bars()
    print("=== D1 2026-07-08 ===")
    o1 = run_report(injected_snaps=synth_snaps(+0.8), injected_bars=bars,
                    trade_date="2026-07-08")
    print(o1["summary"])
    print("=== D2 2026-07-09(驗證 D1 清單)===")
    o2 = run_report(injected_snaps=synth_snaps(-0.6), injected_bars=bars,
                    trade_date="2026-07-09")
    print(o2["summary"])
    print("=== D3 2026-07-10(驗證 D2 清單 + 統計)===")
    o3 = run_report(injected_snaps=synth_snaps(+1.2), injected_bars=bars,
                    trade_date="2026-07-10")
    print(o3["summary"])
    print("---- 報告節錄 ----")
    print(o3["report"][:1800])
