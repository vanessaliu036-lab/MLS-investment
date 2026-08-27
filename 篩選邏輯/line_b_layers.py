"""line_b_layers.py — 七層交易狀態觀察層(DESCRIPTIVE ONLY,唯讀,不寫 DB)。

⚠ 這支跟 line_b_explain / line_b_live / opportunity_* 完全獨立:
   · 不 import 也不修改任何既有 scoring / tier / track 邏輯
   · 不動 line_b_watch_ledger 的凍結定義,不動那張 77/11 校準表
   · /line-b-ledger 舊頁面行為完全不受影響(兩支各自算各自的)

為什麼要獨立:`ACTIVE` 的定義(Trigger+Volume+Acceptance)跟舊 Line B 的
「watch_mode_activated」不是同一件事。舊校準表校的是舊定義的啟動率;把新定義
混進去會讓 62.1% 那種數字被套用在它沒有校準過的狀態上。所以兩套並存、分開看。

═══ 七層(每一欄只回答一件事,2026-08-27 Vanessa 定案) ═══
  CHIP            中期法人籌碼好不好      (inst_flow 近 5 日)
  FLOW            今天資金方向            (aflow / b_snapshot net_active)
  PRICE TRIGGER   是否突破                (現價 > 昨高)
  VOLUME QUALITY  突破是否有真量          (RVOL / 量加速)
  ACCEPTANCE      突破是否站穩            (維持時間 / 回撤 / VWAP)
  EXTENSION RISK  現在是不是太貴太晚      (MA5/MA20/20日高/Gap/3日累漲)
  SECTOR          族群是否支持            (同族群寬度 / 個股排名)

═══ 狀態機(EXTENSION 不參與 ACTIVE 判定,只做交易覆寫) ═══
    WATCH → ARMED → ACTIVE
                     ├─ EXTENSION NORMAL → ACTION 可評估進場
                     ├─ EXTENSION HIGH   → TRADE STATE EXTENDED → 禁追
                     └─ 跌回 Trigger/VWAP → FAILED

  一檔股票就算漲太高,它仍然「真的啟動了」。不能因為不能買就說它沒 ACTIVE——
  啟動事實(ACTIVE)與可否進場(ACTION)是兩件事,不得互相污染。
  同理不再有 `ARMED-HIGH`:ARMED 是生命週期,HIGH 是價格風險,分兩欄各自表述。

═══ 誠實標註 ═══
  · Turnover = 當日成交股數 ÷ 已發行普通股數。股數來自 TWSE/TPEx 官方免費
    OpenAPI(見 stock_shares.py),抓不到才顯示「—」,不用估算值頂替。
    (2026-08-27 更正:先前誤判「沒有資料源」是沒查就下結論,實際抓得到。)
  · Gap = (今日開盤 − 昨收) ÷ 昨收,用 daily_bar.open 真開盤價。當日 daily_bar
    要收盤後才寫,所以盤中 Gap 顯示「—」——不用 09:15 快照價當 proxy 硬湊。
  · RVOL 的歷史母體只有 b_snapshot 累積的交易日(目前約 11 個乾淨日),
    母體天數一律隨值回傳,不讓它看起來比實際可靠。
  · 所有門檻都是「暫定觀察值」,不是回測出來的。這一版的用途是累積 20–30 個
    交易日的 forward 樣本,之後才用 T+1/T+3/MFE/MAE 判斷哪些訊號值得留。
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path
from typing import Optional

import phase
import stock_shares as _shares

DB = "mls.db"
BLIND_MIN_SLOT = "0915"   # 與 run_line_b_ledger 同一條資料品質鐵律,不是交易訊號

# ── 暫定門檻(觀察用,非回測結論;改動要 bump OBSERVATION_VERSION)──
RVOL_PASS = 1.5           # 量能:同時間累計量至少是歷史均量的 1.5 倍才算「有真量」
VOL_ACCEL_PASS = 1.0      # 最近兩格增量 / 前兩格增量
ACCEPT_MAX_DD_PCT = 1.0   # 突破後最大回撤上限
ACCEPT_MIN_SLOTS = 1      # 突破後至少站穩幾格(1 格 = 5 分鐘)
EXT_MA5_PCT = 8.0         # 距 MA5 乖離
EXT_MA20_PCT = 15.0       # 距 MA20 乖離
EXT_RET3D_PCT = 15.0      # 近 3 日累計漲幅
EXT_NEAR_LIMIT_PCT = 9.0  # 當日漲幅接近漲停
EXT_GAP_PCT = 3.0         # 今日跳空幅度
ARMED_NEAR_PCT = 3.0      # 距觸發價多少 % 以內算「已就位」
CHIP_STRONG_LOTS = 3000   # 5 日法人淨額顯著門檻
FLOW_STRONG = 1000        # net_active 視為 STRONG 的幅度

OBSERVATION_VERSION = "lineb_layers_v1_2026-08-27_descriptive_only"


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(a, b):
    """a 相對 b 的百分比差。b 缺或 0 回 None。"""
    a, b = _num(a), _num(b)
    if a is None or not b:
        return None
    return round((a / b - 1) * 100, 2)


def _meta():
    ns: dict = {}
    exec((Path(__file__).parent / "config.py").read_text(encoding="utf-8"), ns)
    return list(ns["UNIVERSE"]), ns.get("CODE_GROUP", {}), ns.get("NAME", {})


def _rows(conn, sql, params):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ────────────────────────────────── CHIP ──────────────────────────────────

def chip_layer(inst_rows: list[dict]) -> dict:
    """inst_rows newest-first。回傳 5 日法人結構 + 一句判定。"""
    five = inst_rows[:5]
    if not five:
        return {"verdict": "NO_DATA", "total_5d": None, "foreign_5d": None,
                "trust_5d": None, "dealer_5d": None, "foreign_days": None, "summary": "法人資料不足"}

    def s(key):
        vals = [_num(r.get(key)) for r in five]
        return sum(v for v in vals if v is not None)

    total, foreign, trust, dealer = s("total_net"), s("foreign_net"), s("trust_net"), s("dealer_net")
    fdays = _num((inst_rows[0] or {}).get("foreign_days"))

    # 最近 1–2 日是否已轉正(判 REVERSAL 用:過去賣、現在不賣了甚至開始買)
    recent = [_num(r.get("total_net")) for r in inst_rows[:2]]
    recent_pos = any(v is not None and v > 0 for v in recent)

    pos = [x > 0 for x in (foreign, trust, dealer)]
    if total > 0 and all(pos):
        verdict, summary = "CONFIRMED", "三方皆偏正"
    elif total > 0 and foreign > 0:
        verdict, summary = "BULLISH_FOREIGN", "偏多／外資主導"
    elif total > 0 and trust > 0:
        verdict, summary = "BULLISH_TRUST", "偏多／投信主導"
    elif total < 0 and recent_pos:
        verdict, summary = "REVERSAL", "累計偏空但近日轉正"
    elif total <= -CHIP_STRONG_LOTS and foreign < 0:
        verdict, summary = "BEARISH", "明顯偏空"
    elif total < 0 and foreign < 0:
        verdict, summary = "BEARISH", "偏空"
    else:
        verdict, summary = "DIVERGENT", "分歧"

    return {"verdict": verdict, "summary": summary, "total_5d": total, "foreign_5d": foreign,
            "trust_5d": trust, "dealer_5d": dealer, "foreign_days": fdays}


# ────────────────────────────────── FLOW ──────────────────────────────────

def flow_layer(net_active: Optional[float]) -> dict:
    na = _num(net_active)
    if na is None:
        return {"verdict": "NO_DATA", "net_active": None}
    if na >= FLOW_STRONG:
        v = "STRONG"
    elif na > 0:
        v = "POSITIVE"
    elif na == 0:
        v = "FLAT"
    else:
        v = "NEGATIVE"
    return {"verdict": v, "net_active": na}


# ─────────────────────────── PRICE TRIGGER ────────────────────────────────

def price_trigger_layer(price, trigger, slots, vwap) -> dict:
    """slots: 今日 b_snapshot(遞增,已過 BLIND_MIN)。trigger = 昨高。"""
    price, trigger, vwap = _num(price), _num(trigger), _num(vwap)
    prices = [_num(s.get("price")) for s in slots if _num(s.get("price")) is not None]
    open_proxy = prices[0] if prices else None          # 非真開盤價,是第一格快照價
    prior_high = max(prices[:-1]) if len(prices) > 1 else None

    broke = price is not None and trigger is not None and price > trigger

    # 突破後維持幾格:從第一次站上 trigger 起,連續維持在 trigger 之上的格數
    hold_slots, first_cross_idx = 0, None
    if trigger is not None:
        for i, p in enumerate(prices):
            if p > trigger:
                first_cross_idx = i
                break
        if first_cross_idx is not None:
            for p in prices[first_cross_idx:]:
                if p > trigger:
                    hold_slots += 1
                else:
                    break

    return {
        "verdict": "YES" if broke else "NO",
        "trigger_price": trigger,
        "above_prev_high": broke,
        "above_open_proxy": (price > open_proxy) if (price is not None and open_proxy) else None,
        "above_intraday_prior_high": (price >= prior_high) if (price is not None and prior_high) else None,
        "above_vwap": (price >= vwap) if (price is not None and vwap) else None,
        "hold_slots": hold_slots,
        "hold_minutes": hold_slots * 5,
        "open_proxy": open_proxy,
        "vwap": vwap,
    }


# ────────────────────────── VOLUME QUALITY ────────────────────────────────

def volume_layer(today_slots, hist_by_slot: dict, current_slot: Optional[str],
                 issued_shares: Optional[float] = None) -> dict:
    """RVOL = 今日該格累計量 ÷ 歷史同格累計量均值。b_snapshot.volume 是當日累計量(張)。

    Turnover = 當日累計成交股數 ÷ 已發行普通股數。股數來自 stock_shares(TWSE/TPEx
    官方免費 OpenAPI,見該模組)。抓不到就回 None 顯示「—」,不用估算值頂替。
    """
    vols = [(_num(s.get("volume")), s.get("slot")) for s in today_slots]
    vols = [(v, sl) for v, sl in vols if v is not None]
    cur_vol, cur_slot = (vols[-1] if vols else (None, None))
    slot_key = current_slot or cur_slot

    hist = hist_by_slot.get(slot_key) or []
    base = (sum(hist) / len(hist)) if hist else None
    rvol = round(cur_vol / base, 2) if (cur_vol is not None and base) else None

    # 量加速:最近兩格增量 vs 前兩格增量(累計量差分)
    accel = None
    if len(vols) >= 5:
        v = [x[0] for x in vols]
        recent = (v[-1] - v[-3])
        prior = (v[-3] - v[-5])
        accel = round(recent / prior, 2) if prior > 0 else None

    if rvol is None:
        verdict = "NO_DATA"
    elif rvol >= RVOL_PASS and (accel is None or accel >= VOL_ACCEL_PASS):
        verdict = "PASS"
    elif rvol >= RVOL_PASS:
        verdict = "PASS_NO_ACCEL"
    else:
        verdict = "THIN"

    # b_snapshot.volume 單位是張(來自 Shioaji total_volume),×1000 換成股再除以發行股數
    turnover = None
    if cur_vol is not None and issued_shares:
        turnover = round(cur_vol * 1000 / issued_shares * 100, 3)   # %

    return {"verdict": verdict, "rvol": rvol, "rvol_base_days": len(hist),
            "vol_accel": accel, "cum_volume": cur_vol, "slot": slot_key,
            "turnover_pct": turnover, "issued_shares": issued_shares,
            "turnover_note": None if turnover is not None else "無該檔發行股數快取"}


# ─────────────────────────── ACCEPTANCE ───────────────────────────────────

def acceptance_layer(slots, trigger, vwap, price) -> dict:
    """突破後有沒有被市場接受:站穩格數 / 突破後最大回撤 / VWAP 有沒有守住。"""
    trigger, vwap, price = _num(trigger), _num(vwap), _num(price)
    prices = [_num(s.get("price")) for s in slots if _num(s.get("price")) is not None]
    if trigger is None or not prices:
        return {"verdict": "NO_DATA", "held_slots": 0, "max_drawdown_pct": None,
                "vwap_held": None}

    idx = next((i for i, p in enumerate(prices) if p > trigger), None)
    if idx is None:
        return {"verdict": "N/A", "held_slots": 0, "max_drawdown_pct": None,
                "vwap_held": None, "note": "尚未突破,不適用"}

    after = prices[idx:]
    held = 0
    for p in after:
        if p > trigger:
            held += 1
        else:
            break
    peak = max(after)
    trough_after_peak = min(after[after.index(peak):]) if peak in after else min(after)
    dd = round((peak - trough_after_peak) / peak * 100, 2) if peak else None
    vwap_held = (price >= vwap) if (price is not None and vwap) else None
    still_above = price is not None and price > trigger

    ok = (held >= ACCEPT_MIN_SLOTS and dd is not None and dd <= ACCEPT_MAX_DD_PCT
          and still_above and (vwap_held is not False))
    return {"verdict": "YES" if ok else "NO", "held_slots": held,
            "held_minutes": held * 5, "max_drawdown_pct": dd,
            "vwap_held": vwap_held, "still_above_trigger": still_above}


# ────────────────────────── EXTENSION RISK ────────────────────────────────

def extension_layer(price, bars, change_rate, today_open=None) -> dict:
    """bars newest-first(T-1 起)。距 MA5 / MA20 / 20日高 / Gap / 3日累漲。

    Gap = (今日開盤 − 昨收) ÷ 昨收。today_open 來自 daily_bar.open(當日收盤後才
    寫入)。盤中還沒有真開盤價時回 None 顯示「—」——不用第一格快照價當 proxy,
    那是 09:15 的價格,不是開盤價。
    """
    price = _num(price)
    b0 = bars[0] if bars else {}
    ma5, ma20 = _num(b0.get("ma5")), _num(b0.get("ma20"))
    prev_close = _num(b0.get("close"))
    highs20 = [_num(b.get("high")) for b in bars[:20]]
    high20 = max([h for h in highs20 if h is not None], default=None)
    closes = [_num(b.get("close")) for b in bars[:4]]
    ret3 = _pct(prev_close, closes[3]) if len(closes) > 3 and closes[3] else None
    cr = _num(change_rate)

    d_ma5, d_ma20 = _pct(price, ma5), _pct(price, ma20)
    d_high20 = _pct(price, high20)
    gap = _pct(today_open, prev_close)

    flags = []
    if d_ma5 is not None and d_ma5 > EXT_MA5_PCT:
        flags.append(f"距MA5 +{d_ma5:.1f}%")
    if d_ma20 is not None and d_ma20 > EXT_MA20_PCT:
        flags.append(f"距MA20 +{d_ma20:.1f}%")
    if ret3 is not None and ret3 > EXT_RET3D_PCT:
        flags.append(f"近3日 +{ret3:.1f}%")
    if cr is not None and cr >= EXT_NEAR_LIMIT_PCT:
        flags.append(f"today +{cr:.1f}%")
    if gap is not None and gap > EXT_GAP_PCT:
        flags.append(f"跳空 +{gap:.1f}%")

    return {"verdict": "HIGH" if flags else "NORMAL", "reasons": flags,
            "dist_ma5_pct": d_ma5, "dist_ma20_pct": d_ma20,
            "dist_high20_pct": d_high20, "ret_3d_pct": ret3, "gap_pct": gap,
            "change_rate": cr}


# ──────────────────────────── SECTOR ──────────────────────────────────────

def sector_layer(code, group, change_map, group_map) -> dict:
    peers = [c for c, g in group_map.items() if g == group]
    vals = [(c, change_map.get(c)) for c in peers]
    known = [(c, v) for c, v in vals if v is not None]
    if not known:
        return {"verdict": "NO_DATA", "group": group, "breadth_pct": None,
                "peers": len(peers), "rank": None}
    up = sum(1 for _, v in known if v > 0)
    breadth = round(up / len(known) * 100, 1)
    ordered = sorted(known, key=lambda x: -(x[1] or 0))
    rank = next((i + 1 for i, (c, _) in enumerate(ordered) if c == code), None)
    pctile = round((1 - (rank - 1) / len(ordered)) * 100) if rank else None
    verdict = "STRONG" if breadth >= 60 else "MIXED" if breadth >= 40 else "WEAK"
    return {"verdict": verdict, "group": group, "breadth_pct": breadth,
            "peers": len(known), "rank": rank, "percentile": pctile,
            "leadership": bool(pctile is not None and pctile >= 80)}


# ─────────────────────── TRADE STATE 狀態機 ───────────────────────────────

def trade_state(chip, flow, trig, vol, acc, ext, structure_ok: Optional[bool],
                distance_pct: Optional[float]) -> dict:
    """EXTENSION 不參與 ACTIVE 判定,只在 ACTIVE 成立後做交易覆寫
    (2026-08-27 Vanessa 明確修正:漲太高不代表沒啟動)。"""
    if structure_ok is False:
        return {"state": "REJECT", "action": "淘汰", "action_code": "REJECT",
                "why": "T-1 收盤跌破 MA20,結構不合格"}

    triggered = trig["verdict"] == "YES"
    ever_triggered = (trig.get("hold_slots") or 0) > 0 or triggered

    # FAILED:曾經突破,現在跌回觸發價或跌破 VWAP
    if ever_triggered and not triggered:
        return {"state": "FAILED", "action": "撤退／不進", "action_code": "AVOID",
                "why": "突破後跌回觸發價"}
    if triggered and acc.get("vwap_held") is False:
        return {"state": "FAILED", "action": "撤退／不進", "action_code": "AVOID",
                "why": "突破後跌破 VWAP"}

    # ACTIVE = Trigger + Volume + Acceptance(不看 EXTENSION)
    if triggered and vol["verdict"].startswith("PASS") and acc["verdict"] == "YES":
        if ext["verdict"] == "HIGH":
            return {"state": "EXTENDED", "action": "DO NOT CHASE", "action_code": "NO_CHASE",
                    "why": "啟動成立,但價格位置已延伸:" + "、".join(ext["reasons"]),
                    "activated": True}
        return {"state": "ACTIVE", "action": "可評估進場", "action_code": "ENTRY_ELIGIBLE",
                "why": "突破 + 有量 + 站穩", "activated": True}

    # 已突破但量/承接沒到位 → 仍在 ARMED,不升 ACTIVE
    if triggered:
        miss = []
        if not vol["verdict"].startswith("PASS"):
            miss.append("量未達標")
        if acc["verdict"] != "YES":
            miss.append("尚未站穩")
        return {"state": "ARMED", "action": "等站穩確認", "action_code": "WAIT",
                "why": "已突破但" + "、".join(miss)}

    near = distance_pct is not None and distance_pct >= -ARMED_NEAR_PCT
    if near and flow["verdict"] in ("STRONG", "POSITIVE"):
        act = ("等回撤，不追高" if ext["verdict"] == "HIGH" else "等突破")
        return {"state": "ARMED", "action": act,
                "action_code": "WAIT_PULLBACK" if ext["verdict"] == "HIGH" else "WAIT",
                "why": "距觸發價 %.2f%%,資金%s" % (distance_pct, flow["verdict"])}

    return {"state": "WATCH", "action": "等", "action_code": "WAIT",
            "why": "尚未接近觸發價"}


# ──────────────────────────── 主流程 ──────────────────────────────────────

def _hist_volume_all(conn, T: str) -> dict:
    """全 universe 的歷史各 slot 累計量,一次查完 → {code: {slot: [vol,...]}}。

    ⚠ 效能:原本是每檔各跑一次全表掃描(51 檔 = 51 次),整頁要 14–26 秒。
    b_snapshot 只有 (data_date, code, slot) 的 PK,以 code 起頭的查詢用不到它,
    每次都是全表掃。改成單次查詢後在記憶體分組,頁面回到 1 秒內。"""
    rows = _rows(conn, "SELECT code, slot, volume FROM b_snapshot "
                       "WHERE data_date<? AND slot>=? AND volume IS NOT NULL",
                 (T, BLIND_MIN_SLOT))
    out: dict = {}
    for r in rows:
        v = _num(r["volume"])
        if v is not None:
            out.setdefault(r["code"], {}).setdefault(r["slot"], []).append(v)
    return out


# ── 短期快取:整份 compute 約 3 秒(51 檔 × 3 表查詢),而底層資料最快也只有
# 每 30 秒(quote_snap)/每 5 分鐘(b_snapshot)才動一次。連續重新整理沒必要
# 每次重算。TTL 內回同一份結果,盤中仍然是「當下」的資料(最多落後 CACHE_TTL 秒)。
CACHE_TTL_SEC = 30
_cache: dict = {}


def compute(db_path: str = DB, T: Optional[str] = None, use_cache: bool = True) -> dict:
    T = T or phase.today_tw().isoformat()
    key = (db_path, T)
    if use_cache:
        hit = _cache.get(key)
        if hit and (_dt.datetime.now() - hit[0]).total_seconds() < CACHE_TTL_SEC:
            return hit[1]
    result = _compute_uncached(db_path, T)
    if use_cache:
        _cache[key] = (_dt.datetime.now(), result)
    return result


def _compute_uncached(db_path: str, T: str) -> dict:
    universe, group_map, name_map = _meta()

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT MAX(data_date) FROM daily_bar WHERE data_date<?", (T,)).fetchone()
        T1 = row[0] if row else None
        if T1 is None:
            return {"T": T, "T1": None, "rows": [], "skipped": "no prior daily_bar"}

        quotes = {r["code"]: r for r in
                  _rows(conn, "SELECT * FROM quote_snap WHERE data_date=?", (T,))}
        aflows = {r["code"]: r for r in
                  _rows(conn, "SELECT * FROM aflow WHERE data_date=?", (T,))}
        inst_max = conn.execute("SELECT MAX(data_date) FROM inst_flow").fetchone()[0]
        shares_map = _shares.load(db_path)
        # 今日 daily_bar 收盤後才寫入;盤中沒有就是沒有,不用 proxy 頂替
        today_open = {r['code']: r['open'] for r in _rows(
            conn, 'SELECT code, open FROM daily_bar WHERE data_date=?', (T,))}

        change_map = {c: _num(q.get("change_rate")) for c, q in quotes.items()}
        hist_vol = _hist_volume_all(conn, T)   # 一次查完,不在迴圈裡逐檔掃表

        out = []
        for code in universe:
            bars = _rows(conn, "SELECT * FROM daily_bar WHERE code=? AND data_date<=? "
                               "ORDER BY data_date DESC LIMIT 25", (code, T1))
            inst = _rows(conn, "SELECT * FROM inst_flow WHERE code=? AND data_date<=? "
                               "ORDER BY data_date DESC LIMIT 25", (code, T1))
            if len(bars) < 6 or len(inst) < 5:
                continue
            slots = _rows(conn, "SELECT * FROM b_snapshot WHERE code=? AND data_date=? "
                                "AND slot>=? ORDER BY slot", (code, T, BLIND_MIN_SLOT))

            q = quotes.get(code) or {}
            a = aflows.get(code) or {}
            price = _num(q.get("price")) or (_num(slots[-1].get("price")) if slots else None)
            if price is None:
                continue

            b0 = bars[0]
            trigger = _num(b0.get("high"))
            ma20_t1, close_t1 = _num(b0.get("ma20")), _num(b0.get("close"))
            structure_ok = (close_t1 >= ma20_t1) if (close_t1 is not None and ma20_t1) else None
            vwap = _num(q.get("avg_price"))
            distance_pct = _pct(price, trigger)

            chip = chip_layer(inst)
            flow = flow_layer(a.get("net_active") if a else
                              (slots[-1].get("net_active") if slots else None))
            trig = price_trigger_layer(price, trigger, slots, vwap)
            vol = volume_layer(slots, hist_vol.get(code, {}),
                               slots[-1]["slot"] if slots else None,
                               shares_map.get(code))
            acc = acceptance_layer(slots, trigger, vwap, price)
            ext = extension_layer(price, bars, q.get("change_rate"), today_open.get(code))
            sec = sector_layer(code, group_map.get(code), change_map, group_map)
            st = trade_state(chip, flow, trig, vol, acc, ext, structure_ok, distance_pct)

            out.append({
                "code": code, "name": name_map.get(code, code),
                "price": price, "trigger_price": trigger, "distance_pct": distance_pct,
                "chip": chip, "flow": flow, "trigger": trig, "volume": vol,
                "acceptance": acc, "extension": ext, "sector": sec, "state": st,
                "freshness": {
                    "inst_flow_through": inst_max,
                    "quote_updated_at": q.get("updated_at"),
                    "aflow_updated_at": a.get("updated_at"),
                    "t1_bar_date": T1,
                },
            })

        # 排序:先看交易狀態的急迫性,同狀態再看今日資金強度
        order = {"ACTIVE": 0, "EXTENDED": 1, "ARMED": 2, "FAILED": 3, "WATCH": 4, "REJECT": 5}
        out.sort(key=lambda r: (order.get(r["state"]["state"], 9),
                                -(r["flow"].get("net_active") or 0)))
        return {"T": T, "T1": T1, "rows": out, "inst_flow_through": inst_max,
                "shares_coverage": sum(1 for r in out if r["volume"].get("issued_shares")),
                "gap_available": sum(1 for r in out if r["extension"].get("gap_pct") is not None),
                "observation_version": OBSERVATION_VERSION,
                "counts": _counts(out)}
    finally:
        conn.close()


def _counts(rows) -> dict:
    c: dict = {}
    for r in rows:
        c[r["state"]["state"]] = c.get(r["state"]["state"], 0) + 1
    return c
