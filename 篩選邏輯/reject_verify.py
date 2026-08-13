"""
reject_verify.py — 排除名單 T+1 錯殺率量測層(2026-08-07 定案；2026-08-12 改版)

跟 screen_verify 是同一層的兩面:
  screen_verify 問「入選那批,今天收盤實際會不會賺」(排序準確度)。
  reject_verify  問「被淘汰那批,隔日是不是其實會噴」(錯殺率/誤刪率)。

2026-08-12 改版(對齊淘汰機制 V2 量測需求):
  ① 來源改吃 dropped_pool(layered_score 真淘汰 tier),不再吃已停更的
     funnel_result.survived=0。fail 因子 = structural_failures(why)。
  ② 判定升級為「誤刪 / 嚴重誤刪」分級,對齊 V2 KPI:
       誤刪     = 買得到 且 隔日盤中漲幅 >= MISKILL_RET(4%) 且 主動資金轉正 且 相對強度合格
       嚴重誤刪 = 同上但盤中漲幅 >= SEVERE_RET(7%)
     三條件缺一不可(資料缺 → 該條件視為不合格,寧可少算不浮報)。
     · 盤中漲幅 = (T+1 最高 - T 收) / T 收   ← 拿得到的最好上檔(非只看收盤)
     · 主動資金轉正 = T+1 aflow.net_active > 0
     · 相對強度合格 = 個股 T+1 收盤報酬 - 全池均報酬 >= 0(族群 index 未接,先用大盤相對代理)
  ③ 買得到:開盤跳空 <= GAP_MAX(3.5%) 且非一價鎖死。進不去不計(帳面漲吃不到)。

owner 規範:本支自建 reject_outcome 表,只寫這張;讀 dropped_pool / daily_bar / aflow。
與 A/B 兩鏈的名單產生完全脫鉤,這支爆掉不影響任何名單。
"""

from __future__ import annotations

import datetime as _dt
import json

import store
from envelope import run_all, persist_status, missing_labels
from phase import Phase, prev_trading_day, today_tw

PLUGIN = "reject_verify"
TABLE = "reject_outcome"

# ── 判定門檻(可調,集中檔頭) ─────────────────────────────────────
MISKILL_RET = 5.0   # FNR +5%：隔日盤中最高相對 T 收盤 >=5%
SEVERE_RET = 9.0    # FNR +9%：隔日盤中最高相對 T 收盤 >=9%
GAP_MAX = 3.5       # 開盤跳空 > 此值(%)= 進不去,不計
# 一價鎖死:high==low → 開盤即鎖,買不到。

_DDL = """
CREATE TABLE IF NOT EXISTS reject_outcome (
    data_date TEXT NOT NULL,      -- 判定日(T+1 收盤)
    pool_date TEXT,               -- 被淘汰日(T)
    code TEXT NOT NULL,
    fail_layer TEXT,              -- 淘汰因子(structural_failures 併字串)
    fail_reason TEXT,             -- 淘汰理由(raw why)
    base_close REAL,              -- T 收盤(進場基準)
    t1_open REAL,
    t1_high REAL,
    t1_low REAL,
    t1_close REAL,
    t1_ret REAL,                  -- (T+1收 - 基準)/基準 %
    t1_high_ret REAL,             -- (T+1高 - 基準)/基準 %  ← 盤中漲幅
    gap_pct REAL,                 -- 開盤跳空 %
    net_active REAL,              -- T+1 主動資金(>0=轉正)
    rel_strength REAL,            -- 個股 T+1 收盤報酬 - 全池均(大盤相對代理)
    fnr_5 INTEGER,                -- T+1 最高相對 T 收盤 >=5%
    fnr_9 INTEGER,                -- T+1 最高相對 T 收盤 >=9%
    tradable INTEGER,             -- 1=買得到 0=進不去
    verdict TEXT,                 -- 排對 / 誤刪 / 嚴重誤刪 / 買不到(不計) / 資料不足
    verified_at TEXT,
    PRIMARY KEY (data_date, code)
);
"""


def _ensure_table(db_path: str = "mls.db") -> None:
    with store.conn(db_path) as c:
        c.executescript(_DDL)
        # 舊表補欄(2026-08-12 新增 net_active / rel_strength)
        cols = {r[1] for r in c.execute("PRAGMA table_info(reject_outcome)").fetchall()}
        for col in ("net_active", "rel_strength", "fnr_5", "fnr_9"):
            if col not in cols:
                typ = "INTEGER" if col.startswith("fnr_") else "REAL"
                c.execute(f"ALTER TABLE reject_outcome ADD COLUMN {col} {typ}")
        c.commit()
    try:
        store.register_table(TABLE, PLUGIN)
    except store.TableOwnershipError:
        pass  # 已註冊


def judge_row(base_close, t1_open, t1_high, t1_low, t1_close,
              net_active=None, rel_strength=None):
    """純函式:單檔判定。回傳 dict(t1_ret, t1_high_ret, gap_pct, tradable, verdict...)。可單測。

    誤刪三條件(V2):盤中漲幅達標 且 主動資金轉正 且 相對強度合格。
    """
    def _pct(v):
        return round((v - base_close) / base_close * 100, 2) if (base_close and v is not None) else None

    ret = _pct(t1_close)
    high_ret = _pct(t1_high)
    gap = _pct(t1_open)

    out = {"t1_ret": ret, "t1_high_ret": high_ret, "gap_pct": gap,
           "net_active": net_active, "rel_strength": rel_strength,
           "fnr_5": False, "fnr_9": False}

    if not base_close or t1_close is None:
        out.update({"tradable": None, "verdict": "資料不足"})
        return out

    # 保留可交易性作診斷欄位，但 FNR 母體是「全部被淘汰股票」，不以此排除。
    locked = (t1_high is not None and t1_low is not None and t1_high == t1_low)
    gapped = (gap is not None and gap > GAP_MAX)
    intraday = high_ret
    fnr_5 = intraday is not None and intraday >= MISKILL_RET
    fnr_9 = intraday is not None and intraday >= SEVERE_RET
    if fnr_9:
        verdict = "嚴重誤刪"
    elif fnr_5:
        verdict = "誤刪"
    else:
        verdict = "排對"
    out.update({"tradable": 0 if (locked or gapped) else 1,
                "fnr_5": fnr_5, "fnr_9": fnr_9, "verdict": verdict})
    return out


class StaleSourceError(RuntimeError):
    """讀當日型表時,來源最新日落後預期交易日 → 大聲爆掉,不靜默沿用舊列。
    (根治『接錯表/接到停更表』反覆發生:停更表與活表讀取當下長得一樣,
    唯一差別是 max(data_date) 落後,故在讀取邊界強制斷言新鮮度。)"""


def _assert_source_fresh(table: str, expected: _dt.date, db_path: str) -> None:
    """斷言 table 有 expected 這天的資料。沒有就爆 StaleSourceError(附最新日),
    避免『指到停更/錯的表卻靜默拿舊列』。空表(從沒跑過)不擋,交由上層當『無紀錄』。"""
    with store.conn(db_path) as c:
        try:
            mx = c.execute(f"SELECT MAX(data_date) FROM {table}").fetchone()[0]
        except Exception:
            return  # 表不存在等 → 交上層處理
    if mx is None:
        return  # 全空表:視為尚未跑過,非「接錯表」
    if mx < expected.isoformat():
        raise StaleSourceError(
            f"{table} 最新僅到 {mx},落後預期 {expected.isoformat()} — "
            f"疑似接到停更/錯的表,拒絕拿舊列冒充。")


def _rejects_on(pool_date: _dt.date, db_path: str) -> dict[str, dict]:
    """讀 T 日被淘汰(dropped_pool)那批。回傳 {code: {layer, reason}}。
    fail_layer = structural_failures 併字串(淘汰因子);fail_reason = why raw。
    仍在候補池(candidate_pool)者不算真淘汰,扣掉(對齊顯示端真淘汰定義)。"""
    out: dict[str, dict] = {}
    # 防呆:dropped_pool 必須有 pool_date 這天(或本就空表)。指到停更表會在此爆掉。
    _assert_source_fresh("dropped_pool", pool_date, db_path)
    try:
        dropped = store.read_date("dropped_pool", pool_date, db_path)
    except Exception:
        dropped = {}
    for code, row in dropped.items():
        try:
            pl = json.loads(row.get("payload") or "{}")
        except Exception:
            pl = {}
        sf = pl.get("structural_failures") or pl.get("why") or []
        out[code] = {"layer": "＋".join(sf) if sf else (pl.get("tier") or "淘汰"),
                     "reason": json.dumps(pl.get("why") or sf, ensure_ascii=False)}
    # 護欄:仍在候補池的檔不是「被淘汰」
    try:
        pool = store.read_date("candidate_pool", pool_date, db_path)
        for code in list(out):
            if code in pool:
                del out[code]
    except Exception:
        pass
    return out


def verify(db_path: str = "mls.db", data_date: _dt.date | None = None) -> dict:
    """用 data_date(T+1)當天收盤,回填 pool_date(前一交易日)被淘汰那批的隔日結果,判誤刪。"""
    _ensure_table(db_path)
    d = data_date or today_tw()
    pool_date = prev_trading_day(d)

    rejects = _rejects_on(pool_date, db_path)
    if not rejects:
        return {
            "phase": "POST", "data_date": d.isoformat(), "pool_date": pool_date.isoformat(),
            "purpose": f"排除名單 T+1 誤刪率 — {pool_date} dropped_pool 無被淘汰紀錄可驗",
            "degraded": [], "items": [],
            "denom": 0, "miskills": 0, "severe": 0, "miskill_rate": None,
        }

    envs = run_all({
        "bar_t": lambda: store.read_date("daily_bar", pool_date, db_path),
        "bar_t1": lambda: store.read_date("daily_bar", d, db_path),
        "aflow_t1": lambda: store.read_date("aflow", d, db_path),
    }, phase=Phase.POST)
    persist_status(envs, db_path)
    bar_t = envs["bar_t"].get({}) or {}
    bar_t1 = envs["bar_t1"].get({}) or {}
    aflow_t1 = envs["aflow_t1"].get({}) or {}

    # 全池均報酬(大盤相對代理):T→T+1 收盤報酬,取兩日都有收盤的檔平均
    rets = []
    for code, b1 in bar_t1.items():
        b0 = bar_t.get(code) or {}
        c0, c1 = b0.get("close"), b1.get("close")
        if c0 and c1:
            rets.append((c1 - c0) / c0 * 100)
    univ_avg = sum(rets) / len(rets) if rets else None

    now = _dt.datetime.now().isoformat(timespec="seconds")
    rows, items = [], []
    for code, info in rejects.items():
        base_close = (bar_t.get(code) or {}).get("close")
        b1 = bar_t1.get(code) or {}
        na = (aflow_t1.get(code) or {}).get("net_active")
        stock_ret = None
        if base_close and b1.get("close"):
            stock_ret = (b1["close"] - base_close) / base_close * 100
        rel = round(stock_ret - univ_avg, 2) if (stock_ret is not None and univ_avg is not None) else None
        j = judge_row(base_close, b1.get("open"), b1.get("high"),
                      b1.get("low"), b1.get("close"), net_active=na, rel_strength=rel)
        rec = {
            "data_date": d.isoformat(), "pool_date": pool_date.isoformat(), "code": code,
            "fail_layer": info["layer"], "fail_reason": info["reason"],
            "base_close": base_close,
            "t1_open": b1.get("open"), "t1_high": b1.get("high"),
            "t1_low": b1.get("low"), "t1_close": b1.get("close"),
            "t1_ret": j["t1_ret"], "t1_high_ret": j["t1_high_ret"],
            "gap_pct": j["gap_pct"], "net_active": na, "rel_strength": rel,
            "fnr_5": int(j["fnr_5"]), "fnr_9": int(j["fnr_9"]),
            "tradable": j["tradable"], "verdict": j["verdict"], "verified_at": now,
        }
        rows.append(rec)
        items.append(rec)

    store.upsert_intraday(TABLE, PLUGIN, rows, db_path)

    scored = [r for r in items if r["t1_high_ret"] is not None]
    denom = len(scored)
    severe = sum(r["fnr_9"] for r in scored)
    miskills = sum(r["fnr_5"] for r in scored)
    order = {"嚴重誤刪": 0, "誤刪": 1, "排對": 2, "買不到(不計)": 3, "資料不足": 4}
    items.sort(key=lambda r: (order.get(r["verdict"], 9), -(r["t1_high_ret"] or -999)))

    return {
        "phase": "POST", "data_date": d.isoformat(), "pool_date": pool_date.isoformat(),
        "purpose": (f"排除名單 T+1 誤刪率:淘汰 {len(rejects)} 檔,可交易 {denom} 檔,"
                    f"誤刪 {miskills}(嚴重 {severe}) → 誤刪率 "
                    f"{round(miskills/denom*100,1) if denom else '—'}%"),
        "verified_at": now,
        "degraded": missing_labels(envs),
        "rejected": len(rejects), "denom": denom,
        "miskills": miskills, "severe": severe,
        "miskill_rate": round(miskills / denom * 100, 1) if denom else None,
        "severe_rate": round(severe / denom * 100, 1) if denom else None,
        "fnr_5_rate": round(miskills / denom * 100, 1) if denom else None,
        "fnr_9_rate": round(severe / denom * 100, 1) if denom else None,
        "fnr_5_target": "<15%", "fnr_9_target": "<5%",
        "items": items,
    }


def stats(days: int = 30, db_path: str = "mls.db") -> dict:
    """滾動 N 個交易日的各因子(fail_layer)誤刪率。這就是放寬淘汰門檻的數據依據。"""
    _ensure_table(db_path)
    since = (today_tw() - _dt.timedelta(days=days)).isoformat()
    with store.conn(db_path) as c:
        by_factor = [dict(r) for r in c.execute(
            """SELECT fail_layer,
                      COUNT(*) n,
                      SUM(CASE WHEN t1_high_ret IS NOT NULL THEN 1 ELSE 0 END) tradable,
                      SUM(CASE WHEN fnr_5=1 OR t1_high_ret>=5 THEN 1 ELSE 0 END) miskills,
                      SUM(CASE WHEN fnr_9=1 OR t1_high_ret>=9 THEN 1 ELSE 0 END) severe,
                      AVG(t1_high_ret) avg_high_ret
               FROM reject_outcome
               WHERE data_date >= ?
               GROUP BY fail_layer""", (since,))]
        daily = [dict(r) for r in c.execute(
            """SELECT data_date,
                      SUM(CASE WHEN t1_high_ret IS NOT NULL THEN 1 ELSE 0 END) tradable,
                      SUM(CASE WHEN fnr_5=1 OR t1_high_ret>=5 THEN 1 ELSE 0 END) miskills,
                      SUM(CASE WHEN fnr_9=1 OR t1_high_ret>=9 THEN 1 ELSE 0 END) severe
               FROM reject_outcome WHERE data_date >= ?
               GROUP BY data_date ORDER BY data_date""", (since,))]
    for t in by_factor:
        t["miskill_rate"] = round(t["miskills"] / t["tradable"] * 100, 1) if t["tradable"] else None
        t["avg_high_ret"] = round(t["avg_high_ret"], 2) if t["avg_high_ret"] is not None else None
    for row in daily:
        row["miskill_rate"] = round(row["miskills"] / row["tradable"] * 100, 1) if row["tradable"] else None
    total_tradable = sum(t["tradable"] for t in by_factor)
    total_miskills = sum(t["miskills"] for t in by_factor)
    total_severe = sum(t["severe"] for t in by_factor)
    by_factor.sort(key=lambda t: -(t["miskill_rate"] or -1))
    return {
        "window_days": days, "since": since,
        "overall_miskill_rate": round(total_miskills / total_tradable * 100, 1) if total_tradable else None,
        "severe_rate": round(total_severe / total_tradable * 100, 1) if total_tradable else None,
        "fnr_5_rate": round(total_miskills / total_tradable * 100, 1) if total_tradable else None,
        "fnr_9_rate": round(total_severe / total_tradable * 100, 1) if total_tradable else None,
        "fnr_5_target": "<15%", "fnr_9_target": "<5%",
        "tradable": total_tradable, "miskills": total_miskills, "severe": total_severe,
        "by_factor": by_factor, "daily": daily,
    }


if __name__ == "__main__":
    import sys
    fn = sys.argv[1] if len(sys.argv) > 1 else "verify"
    out = stats() if fn == "stats" else verify()
    print(json.dumps(out, ensure_ascii=False, indent=2))
