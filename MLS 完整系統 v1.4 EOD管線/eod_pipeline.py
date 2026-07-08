"""
MLS 插件 — eod_pipeline.py
盤後數據驗證 × 訓練資料管線(EOD QA & Training Pipeline)
====================================================================
純插件:由 after_hours 尾端掛鉤呼叫;server 收盤兜底掛鉤可獨立觸發。
解決的五個真實漏洞:
  ① server 重啟後 _last_full_state 為空 → 盤後不跑、資料全丟
     → 本插件可自行向 broker 重抓收盤快照(EOD snapshot 收盤後仍可取)
  ② 成敗判定用舊掃描價 → 以重抓的收盤快照為準覆核
  ③ chips 靜默 None → QA 計覆蓋率,低於門檻推播警告
  ④ 只有發訊號的股票才留紀錄 → 全池每日寫入 training_samples
     (features 當日寫、label 隔日回填,樣本量 = 50檔×每交易日)
  ⑤ sector_daily 缺天無人發現 → QA 逐項檢查,缺漏即補算或告警
"""

import os
import json
import csv
from datetime import datetime, timezone, timedelta

import config as C
import db
import chips
import scoring

TW_TZ = timezone(timedelta(hours=8))
REPORT_DIR = os.path.join(os.path.dirname(__file__), "reports")


# ════════════════════════════════════════════════════════
# 資料表(插件自建,不動主 schema)
# ════════════════════════════════════════════════════════
def _init_tables():
    import sqlite3
    with db._lock, db._conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS training_samples(
          trade_date TEXT, stock_id TEXT,
          features TEXT,            -- JSON:五因子/tnvr/aflow/chg/vr/籌碼/族群
          close_price REAL,
          label INTEGER,            -- 隔日回填:1=隔日收漲>0.3% 0=否 NULL=待填
          label_date TEXT,
          PRIMARY KEY(trade_date, stock_id)
        );
        CREATE TABLE IF NOT EXISTS eod_qa_log(
          trade_date TEXT PRIMARY KEY,
          passed INTEGER, coverage REAL, issues TEXT
        );
        """)


# ════════════════════════════════════════════════════════
# ① 收盤兜底重抓(state 缺失/過舊時)
# ════════════════════════════════════════════════════════
def fetch_eod_snaps():
    """收盤後直接向券商重抓觀察池快照,作為權威收盤數據。"""
    import broker
    snaps = broker.batch_snapshots(list(C.UNIVERSE))
    for s in snaps:
        sec, st = C.SECTOR_MAP.get(s["code"], ("其他", "attack"))
        s["sector"], s["sector_type"] = sec, st
    return snaps


def resolve_snaps(state):
    """優先用盤中最後 state;缺失或覆蓋率<80% 時重抓。回傳 (snaps, source)。"""
    snaps = (state or {}).get("_snaps") or []
    cov = len({s["code"] for s in snaps} & set(C.UNIVERSE)) / max(1, len(C.UNIVERSE))
    if cov >= 0.8:
        return snaps, "intraday_state"
    try:
        fresh = fetch_eod_snaps()
        if fresh:
            return fresh, "eod_refetch"
    except Exception as e:
        print(f"[eod] 重抓失敗:{e}")
    return snaps, "degraded"


# ════════════════════════════════════════════════════════
# ② 七項 QA 驗證
# ════════════════════════════════════════════════════════
def run_qa(snaps):
    issues, tdate = [], db.today()
    uni = set(C.UNIVERSE)
    got = {s["code"] for s in snaps}

    # QA1 覆蓋率(固定池 50 檔:缺任一檔即列異常)
    missing = sorted(uni - got)
    coverage = len(got & uni) / max(1, len(uni))
    if missing:
        sev = "嚴重" if coverage < 0.9 else "警告"
        issues.append(f"QA1[{sev}] 快照缺 {len(missing)} 檔"
                      f"(覆蓋率 {coverage:.0%}):{','.join(missing[:6])}"
                      + ("…" if len(missing) > 6 else ""))

    # QA2 欄位有效性(價格/量為 None 或 0)
    bad = [s["code"] for s in snaps
           if not s.get("price") or (s.get("total_volume") or 0) <= 0]
    if bad:
        issues.append(f"QA2 無效價量 {len(bad)} 檔:{','.join(bad[:6])}")

    # QA3 chips 籌碼覆蓋率
    ch_ok = 0
    for code in list(got)[:len(got)]:
        ch = chips.get_chips(code)
        if ch.get("inst_net_20d_lots") is not None:
            ch_ok += 1
    ch_cov = ch_ok / max(1, len(got))
    if ch_cov < 0.5:
        issues.append(f"QA3 籌碼資料覆蓋率僅 {ch_cov:.0%}(FinMind token/額度需檢查),"
                      f"籌碼因子學習將偏斜")

    # QA4 sector_daily 當日是否落地(攻擊族群數)
    atk = {v[0] for v in C.SECTOR_MAP.values()}
    with db._lock, db._conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM sector_daily WHERE trade_date=?",
                      (tdate,)).fetchone()["n"]
    if n < max(1, len(atk) - 2):
        issues.append(f"QA4 sector_daily 今日僅 {n} 列(應≈{len(atk)}),ABAB 判定可能失真")

    # QA5 今日訊號是否落地
    st = db.today_stats()
    if (st.get("total") or 0) == 0:
        issues.append("QA5 今日 signals 為 0 筆(盤中主迴圈可能未運行/中斷)")

    # QA6 成敗紀錄是否寫入
    with db._lock, db._conn() as c:
        n_out = c.execute("SELECT COUNT(*) n FROM signal_outcomes WHERE trade_date=?",
                          (tdate,)).fetchone()["n"]
    if (st.get("buys") or 0) > 0 and n_out == 0:
        issues.append("QA6 有進場訊號但 signal_outcomes 為 0(精度控制器無資料)")

    # QA7 昨日訓練樣本 label 是否已回填
    with db._lock, db._conn() as c:
        n_null = c.execute("""SELECT COUNT(*) n FROM training_samples
          WHERE label IS NULL AND trade_date < ?""", (tdate,)).fetchone()["n"]
    if n_null > len(uni):
        issues.append(f"QA7 待回填 label 累積 {n_null} 筆(>1個交易日),回填鏈中斷")

    passed = len(issues) == 0
    return passed, coverage, issues


# ════════════════════════════════════════════════════════
# ③ 訓練樣本管線:全池 features 當日寫、label 隔日回填
# ════════════════════════════════════════════════════════
def write_features(snaps, sectors):
    tdate = db.today()
    sec_med = {s["name"]: s["pct"] for s in (sectors or [])}
    locked = {s["name"] for s in (sectors or []) if s.get("locked")}
    rows = 0
    with db._lock, db._conn() as c:
        for s in snaps:
            if not s.get("price"):
                continue
            ch = chips.get_chips(s["code"])
            feat = {
                "chg": s.get("change_rate"), "vr": s.get("volume_ratio"),
                "tnvr": scoring.tnvr(s.get("total_volume"), None) or s.get("volume_ratio"),
                "aflow_ratio": round(scoring.get_aflow(s["code"]) /
                                     max(1, s.get("total_volume") or 1), 4),
                "above_avgp": 1 if (s.get("avg_price") and s["price"] >= s["avg_price"]) else 0,
                "at_high": 1 if (s.get("high") and s["price"] >= s["high"]) else 0,
                "rs_sector": round((s.get("change_rate") or 0)
                                   - sec_med.get(s.get("sector"), 0), 2),
                "sector_locked": 1 if s.get("sector") in locked else 0,
                "inst_net": ch.get("inst_net_20d_lots"),
                "inst_streak": ch.get("inst_streak"),
                "big_trend": ch.get("big_holder_trend"),
                "sector": s.get("sector"),
            }
            c.execute("""INSERT OR REPLACE INTO training_samples
              (trade_date,stock_id,features,close_price,label,label_date)
              VALUES(?,?,?,?,NULL,NULL)""",
              (tdate, s["code"], json.dumps(feat, ensure_ascii=False), s["price"]))
            rows += 1
    return rows


def backfill_labels(snaps):
    """用今日收盤,回填『最近一個未標記交易日』的 label。"""
    tdate = db.today()
    close = {s["code"]: s["price"] for s in snaps if s.get("price")}
    with db._lock, db._conn() as c:
        prev = c.execute("""SELECT MAX(trade_date) d FROM training_samples
          WHERE label IS NULL AND trade_date < ?""", (tdate,)).fetchone()["d"]
        if not prev:
            return 0, None
        rows = c.execute("""SELECT stock_id, close_price FROM training_samples
          WHERE trade_date=? AND label IS NULL""", (prev,)).fetchall()
        n = 0
        for r in rows:
            cl = close.get(r["stock_id"])
            if cl is None or not r["close_price"]:
                continue
            label = 1 if cl > r["close_price"] * 1.003 else 0
            c.execute("""UPDATE training_samples SET label=?, label_date=?
              WHERE trade_date=? AND stock_id=?""",
              (label, tdate, prev, r["stock_id"]))
            n += 1
    return n, prev


def export_training(path=None, min_rows=1):
    """匯出已標記樣本為 CSV(features 展平),供外部模型訓練。"""
    path = path or os.path.join(REPORT_DIR, "training_dataset.csv")
    os.makedirs(REPORT_DIR, exist_ok=True)
    with db._lock, db._conn() as c:
        rows = c.execute("""SELECT trade_date, stock_id, features, close_price, label
          FROM training_samples WHERE label IS NOT NULL ORDER BY trade_date""").fetchall()
    if len(rows) < min_rows:
        return None, 0
    keys = ["chg", "vr", "tnvr", "aflow_ratio", "above_avgp", "at_high",
            "rs_sector", "sector_locked", "inst_net", "inst_streak", "big_trend"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["trade_date", "stock_id", "sector"] + keys + ["close", "label"])
        for r in rows:
            feat = json.loads(r["features"])
            w.writerow([r["trade_date"], r["stock_id"], feat.get("sector", "")]
                       + [feat.get(k) for k in keys]
                       + [r["close_price"], r["label"]])
    return path, len(rows)


# ════════════════════════════════════════════════════════
# 主入口(after_hours 掛鉤 / server 兜底掛鉤共用)
# ════════════════════════════════════════════════════════
def run(state=None, sectors=None, notify=None):
    _init_tables()
    snaps, source = resolve_snaps(state)
    sectors = sectors or (state or {}).get("_sectors_full") or []

    # 順序:先回填昨日 label(用今日權威收盤)→ 再寫今日 features → QA
    n_label, prev_date = backfill_labels(snaps)
    n_feat = write_features(snaps, sectors)
    passed, coverage, issues = run_qa(snaps)
    csv_path, n_export = export_training()

    tdate = db.today()
    with db._lock, db._conn() as c:
        c.execute("INSERT OR REPLACE INTO eod_qa_log VALUES(?,?,?,?)",
                  (tdate, 1 if passed else 0, round(coverage, 3),
                   json.dumps(issues, ensure_ascii=False)))

    # QA 報告落檔
    os.makedirs(REPORT_DIR, exist_ok=True)
    d = datetime.now(TW_TZ)
    lines = [f"# EOD 數據驗證報告 {d:%Y-%m-%d}",
             f"資料來源:{source}|快照覆蓋率:{coverage:.0%}",
             f"訓練樣本:今日寫入 {n_feat} 筆 features;"
             f"回填 {prev_date or '—'} 的 label {n_label} 筆",
             f"可訓練資料集:{n_export} 筆" + (f" → {csv_path}" if csv_path else "(累積中)"),
             "", "## QA 結果:" + ("✅ 全數通過" if passed else f"❌ {len(issues)} 項異常")]
    lines += [f"- {i}" for i in issues] or ["- 無異常"]
    qa_path = os.path.join(REPORT_DIR, f"EOD_QA_{d:%Y%m%d}.md")
    with open(qa_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    summary = (f"🧪 EOD管線|來源{source}|覆蓋{coverage:.0%}|"
               f"features+{n_feat}|label回填{n_label}|訓練集{n_export}筆|"
               + ("QA✅" if passed else f"QA❌{len(issues)}項"))
    if notify:
        try:
            notify(summary + ("" if passed else "\n" + "\n".join(issues[:3])))
        except Exception:
            pass
    return {"passed": passed, "coverage": coverage, "issues": issues,
            "features_written": n_feat, "labels_filled": n_label,
            "export": csv_path, "export_rows": n_export,
            "source": source, "qa_report": qa_path, "summary": summary}
