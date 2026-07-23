"""
MLS 插件 — daily_close_report.py
每日台股收盤報告（盤後檢討官）
====================================================================
17:15 排程觸發:組裝當日所有盤後資料 → DeepSeek 寫第③區塊優化建議 →
三區塊（①今日驗證 / ②命中率統計 / ③優化建議&明日追蹤）落:
  · 本機 /app/reports/每日台股收盤報告｜YYYY-MM-DD.md
  · Airtable Daily_Close_Report table（標題/日期/報告/狀態）
  · DB daily_close_report 表（保留近 5 天供歷史命中率計算）

資料源:全部讀現有表/檔,不重新抓收盤快照（after_hours / eod_pipeline 已跑完）:
  · 昨日 premarket_report（盤前預測）
  · review_log / signals / sector_daily（今日實際）
  · after_hours.review（命中率）/ .rotation（4 象限）/ .tomorrow_watchlist
  · eod_pipeline（QA 結果）
  · funnels / decision（兩梯隊/勝率）

Airtable 環境變數: AIRTABLE_TOKEN / AIRTABLE_BASE_ID（同 after_hours）
"""

import os
import json
import urllib.request
import urllib.parse
import urllib.error
import sqlite3
import threading
from datetime import datetime, timezone, timedelta

TW_TZ = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(__file__)
REPORT_DIR = os.path.join(BASE_DIR, "reports")

# ══════════════════════════════════════════════════════
# DB 表（插件自建,不動主 schema）
# ══════════════════════════════════════════════════════
_lock = threading.Lock()


def _conn():
    import db as _db
    c = sqlite3.connect(_db.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _init_table():
    with _lock, _conn() as c:
        _init_table_in(c)


def _init_table_in(c):
    """共用連線版的 init（給 query 前呼叫做 schema 保險）"""
    c.execute("""
    CREATE TABLE IF NOT EXISTS daily_close_report(
      report_date TEXT PRIMARY KEY,
      content TEXT,
      model TEXT,
      hit_rate REAL,
      passed INTEGER,
      source TEXT,
      created_ts TEXT
    );
    """)


# ══════════════════════════════════════════════════════
# ① 今日驗證 — 撈昨日盤前 + 今日盤後
# ══════════════════════════════════════════════════════
def fetch_yesterday_premarket():
    """撈昨日 premarket_report（昨日盤前分析師的預測）"""
    import db as _db
    with _lock, _conn() as c:
        rows = c.execute("""
          SELECT report_date, content, model, context_note, created_ts
          FROM premarket_report
          WHERE report_date < ?
          ORDER BY report_date DESC LIMIT 1
        """, (_db.today(),)).fetchall()
    return [dict(r) for r in rows]


def fetch_today_actual():
    """撈今日盤後所有真實數據"""
    import db as _db
    tdate = _db.today()
    out = {"date": tdate}

    # review_log（命中率）
    with _lock, _conn() as c:
        r = c.execute("""SELECT * FROM review_log WHERE trade_date=?""",
                      (tdate,)).fetchone()
        out["review"] = dict(r) if r else None

        # signals（今日訊號）
        sigs = c.execute("""
          SELECT stock_id, stock_name, sector, action, price, change_rate,
                 volume_ratio, confidence_label
          FROM signals WHERE trade_date=? ORDER BY change_rate DESC
        """, (tdate,)).fetchall()
        out["signals"] = [dict(s) for s in sigs]

        # sector_daily（族群今日收盤）
        secs = c.execute("""
          SELECT sector, pct, amount_share, flow_dir, quadrant
          FROM sector_daily WHERE trade_date=? ORDER BY pct DESC
        """, (tdate,)).fetchall()
        out["sectors"] = [dict(s) for s in secs]

        # eod_qa_log
        qa = c.execute("""SELECT * FROM eod_qa_log WHERE trade_date=?""",
                       (tdate,)).fetchone()
        out["qa"] = dict(qa) if qa else None

    # 過去 5 天命中率（取 daily_close_report 表回算）
    hist = []
    with _lock, _conn() as c:
        for row in c.execute("""
          SELECT report_date, hit_rate FROM daily_close_report
          WHERE report_date < ? ORDER BY report_date DESC LIMIT 5
        """, (tdate,)):
            hist.append({"date": row["report_date"], "hit_rate": row["hit_rate"]})
    out["history_hit_rate"] = hist
    return out


# ══════════════════════════════════════════════════════
# ② 命中率統計 + ③ DeepSeek 優化建議
# ══════════════════════════════════════════════════════
def call_deepseek(messages, temperature=0.5, max_tokens=4000):
    """盤後報告專用 DeepSeek 呼叫（呼叫端/錯誤訊息與 premarket.py 各自獨立）"""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    if not key:
        return None, ("DEEPSEEK_API_KEY 未設定;第③區塊將以「暫無 AI 建議」呈現,"
                      "不假造內容。")
    body = json.dumps({"model": model, "messages": messages,
                       "temperature": temperature, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"], None
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        return None, f"DeepSeek API {e.code}:{detail or e.reason}"
    except Exception as e:
        return None, f"DeepSeek 連線失敗:{e}"


# ══════════════════════════════════════════════════════
# 三區塊 Markdown 組裝
# ══════════════════════════════════════════════════════
def render_section1(premarket, actual):
    """① 今日驗證 — 昨日盤前預測 vs 今日實際"""
    today = actual["date"]
    now = datetime.now(TW_TZ)
    weekday = now.weekday()
    weekday_cn = "一二三四五六日"[weekday]
    parts = [
        f"📋 ① 今日驗證：預測 vs 實際對照表",
        "",
        f"📅 報告日期：{today}（{['一','二','三','四','五','六','日'][datetime.strptime(today,'%Y-%m-%d').weekday()]}）",
        f"🕒 產出時間：{now.strftime('%H:%M')} 收盤後",
        f"👤 執行單位：盤後檢討官",
        "",
    ]

    # 昨日盤前（若無則標暫無資料）
    if not premarket:
        parts += [
            "## 昨日盤前預測",
            "",
            "暫無資料（昨日 premarket_report 為空，可能是假日或未執行盤前報告）",
            "",
        ]
    else:
        prev = premarket[0]
        parts += [
            f"## 昨日盤前預測（{prev['report_date']}）",
            "",
            prev["content"][:1500] + ("..." if len(prev["content"]) > 1500 else ""),
            "",
        ]

    # 今日實際
    review = actual.get("review") or {}
    rate = review.get("hit_rate", 0) or 0
    total = review.get("watch_total", 0) or 0
    hit = review.get("watch_hit", 0) or 0
    missed = json.loads(review.get("missed_stocks") or "[]") if review.get("missed_stocks") else []
    parts += [
        "## 今日實際結果",
        "",
        f"- 觀察清單命中率：**{rate}%**（{hit}/{total}）",
        f"- 今日訊號總數：{len(actual.get('signals', []))} 筆",
        f"- 遺漏股：{','.join(missed[:10]) or '無'}",
        "",
        "## 今日族群表現",
        "",
        "| 族群 | 漲跌 | 資金方向 | 象限 |",
        "|---|---|---|---|",
    ]
    sec_zh = {"in_up": "流入↗漲", "in_down": "流入↗跌⚠", "out_down": "流出↘跌", "out_up": "流出↘漲"}
    for s in actual.get("sectors", [])[:10]:
        parts.append(f"| {s['sector']} | {s['pct']:+.2f}% | "
                     f"{'流入' if s['flow_dir']>0 else '流出'} | "
                     f"{sec_zh.get(s['quadrant'], s['quadrant'])} |")
    parts.append("")
    return "\n".join(parts)


def render_section2(actual):
    """② 命中率統計"""
    review = actual.get("review") or {}
    rate = review.get("hit_rate", 0) or 0
    total = review.get("watch_total", 0) or 0
    hit = review.get("watch_hit", 0) or 0
    hist = actual.get("history_hit_rate", [])
    parts = [
        f"📊 ② 預測命中率統計",
        "",
        f"**今日命中率：{rate}%**（{hit}/{total}）",
        "",
        "### 歷史命中率趨勢",
        "",
        "| 日期 | 命中率 |",
        "|---|---|",
    ]
    for h in hist:
        parts.append(f"| {h['date']} | {h['hit_rate']}% |")
    if not hist:
        parts.append("| 暫無資料 | — |")
    parts.append("")

    # QA 結果
    qa = actual.get("qa") or {}
    if qa:
        parts += [
            "### EOD 數據 QA",
            "",
            f"- 通過：{'✅' if qa.get('passed') else '❌'}",
            f"- 覆蓋率：{qa.get('coverage', 0):.0%}",
        ]
        issues = json.loads(qa.get("issues") or "[]") if qa.get("issues") else []
        for i in issues[:5]:
            parts.append(f"  - {i}")
        parts.append("")
    return "\n".join(parts)


def render_section3(ai_advice):
    """③ 優化建議 & 明日追蹤 — 全部交給 DeepSeek"""
    if not ai_advice:
        return ("🔧 ③ 今日優化建議 & 明日追蹤\n\n"
                "**暫無 AI 建議**（DEEPSEEK_API_KEY 未設定或 API 失敗，"
                "不假造內容；本區塊可由人工補完）\n")
    return f"🔧 ③ 今日優化建議 & 明日追蹤\n\n{ai_advice}\n"


# ══════════════════════════════════════════════════════
# DeepSeek prompt — 組裝所有資料一次送
# ══════════════════════════════════════════════════════
DEEPSEEK_SYSTEM = """你是「台股盤後檢討官」。根據以下資料，產出第③區塊：
「今日優化建議 & 明日追蹤」，Markdown 格式繁體中文。

必含三小節:
1. 本期修正問題（3 條 bullet,每條 1 行 — 問題/教訓/修正）
2. 明日觀察重點（時間軸表格或 bullet list,標重要性 ⭐ 數量）
3. 明日策略摘要（3-5 行 bullet,M 評分調整 + 觀察池調整）

資料誠實原則:所有數字引用必須來自提供資料,不要假造。資料不足寫「暫無資料」。"""


def build_deepseek_messages(section12_text):
    return [
        {"role": "system", "content": DEEPSEEK_SYSTEM},
        {"role": "user", "content":
         f"以下是今日台股收盤資料（已驗證的盤前預測 + 盤後實際 + 命中率統計）:\n\n"
         f"{section12_text}\n\n"
         f"請產出第③區塊（優化建議 + 明日追蹤）。"},
    ]


# ══════════════════════════════════════════════════════
# Airtable 推播
# ══════════════════════════════════════════════════════
def airtable_post(report_date, title, content, status="published"):
    token = os.environ.get("AIRTABLE_TOKEN", "")
    base = os.environ.get("AIRTABLE_BASE_ID", "")
    if not token or not base:
        return False, "未設定 AIRTABLE_TOKEN / AIRTABLE_BASE_ID"
    # 嘗試多個可能的 table name（Daily_Close_Report / daily_close_report）
    for table in ("Daily_Close_Report", "daily_close_report", "DailyCloseReport"):
        url = f"https://api.airtable.com/v0/{base}/{urllib.parse.quote(table)}"
        body = json.dumps({"records": [{"fields": {
            "Title": title, "Date": report_date,
            "Report": content, "Status": status,
        }}]}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return True, json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue    # 試下一個 table name
            return False, f"HTTP {e.code}:{e.read()[:200].decode()}"
    return False, "找不到對應 Airtable table,請確認 table 名稱"


# ══════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════
def run(last_state=None, after_hours_out=None, eod_out=None, force=False):
    """
    last_state:     收盤最後一輪 state（after_hours / eod 已用過）
    after_hours_out: after_hours.run() 的回傳（已有 review/rotation/tomorrow_watchlist）
    eod_out:         eod_pipeline.run() 的回傳
    force:           True 強制重跑（覆蓋當日 row）
    """
    _init_table()
    import db as _db
    tdate = _db.today()
    today = datetime.now(TW_TZ)

    # 盤中時段擋:13:30 收盤前不跑(避免抓到未收盤資料),force=1 可強制
    if not force and today.hour < 14:
        return {"ok": False, "date": tdate, "note": "盤中時段不跑報告(<14:00),等收盤後再跑", "now": today.strftime("%H:%M")}

    # 若已跑過且非 force → 直接回傳
    if not force:
        with _lock, _conn() as c:
            existed = c.execute("""SELECT 1 FROM daily_close_report
                                   WHERE report_date=?""", (tdate,)).fetchone()
        if existed:
            return {"ok": True, "date": tdate, "note": "今日報告已存在(force=1 可重跑)"}

    # 撈資料
    premarket = fetch_yesterday_premarket()
    actual = fetch_today_actual()

    # 拼 ①② 區塊
    s1 = render_section1(premarket, actual)
    s2 = render_section2(actual)
    section12 = s1 + "\n\n" + s2

    # ③ 區塊（DeepSeek）
    ai_content, ai_err = call_deepseek(build_deepseek_messages(section12))
    s3 = render_section3(ai_content)
    if ai_err:
        print(f"[daily_close_report] DeepSeek 失敗:{ai_err}")

    # 完整報告
    full_report = (
        f"# 每日台股收盤報告 {tdate}\n\n"
        f"報告日期：{tdate}  |  產出：{today:%Y-%m-%d %H:%M}  |  系統：盤後檢討官\n\n"
        + section12 + "\n\n" + s3 +
        f"\n---\n報告完成時間：{today:%Y-%m-%d %H:%M}\n"
    )

    # 落本機
    os.makedirs(REPORT_DIR, exist_ok=True)
    md_path = os.path.join(REPORT_DIR, f"每日台股收盤報告｜{tdate}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(full_report)

    # 落 DB
    review = actual.get("review") or {}
    hit_rate = review.get("hit_rate", 0) or 0
    qa = actual.get("qa") or {}
    with _lock, _conn() as c:
        c.execute("""INSERT OR REPLACE INTO daily_close_report
          (report_date, content, model, hit_rate, passed, source, created_ts)
          VALUES(?,?,?,?,?,?,?)""",
          (tdate, full_report,
           os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
           hit_rate, 1 if qa.get("passed") else 0,
           "after_hours+eod+premarket",
           today.isoformat(timespec="seconds")))

    # 推 Airtable
    title = f"每日台股收盤報告｜{tdate}"
    air_ok, air_msg = airtable_post(tdate, title, full_report)

    return {
        "ok": True, "date": tdate,
        "md_path": md_path,
        "airtable_ok": air_ok, "airtable_msg": air_msg,
        "hit_rate": hit_rate, "qa_passed": bool(qa.get("passed")),
        "ai_ok": bool(ai_content), "ai_err": ai_err,
        "chars": len(full_report),
    }


# ══════════════════════════════════════════════════════
# HTTP endpoint（給 server.py 掛 /api/daily_close_report）
# ══════════════════════════════════════════════════════
def get_latest():
    with _lock, _conn() as c:
        _init_table_in(c)
        r = c.execute("""SELECT * FROM daily_close_report
          ORDER BY report_date DESC LIMIT 1""").fetchone()
    if not r:
        return {"date": None, "content": None}
    d = dict(r)
    # FastAPI 對 sqlite3.Row 序列化不友善,手動補關鍵欄位別名
    return {
        "date": d.get("report_date"),
        "report_date": d.get("report_date"),
        "content": d.get("content"),
        "model": d.get("model"),
        "hit_rate": d.get("hit_rate"),
        "passed": d.get("passed"),
        "source": d.get("source"),
        "created_ts": d.get("created_ts"),
    }


try:
    from fastapi import APIRouter
    router = APIRouter()

    @router.get("/api/daily_close_report/latest")
    def api_latest():
        try:
            return get_latest()
        except Exception as e:
            return {"date": None, "content": None, "error": str(e)}

    @router.post("/api/daily_close_report/run")
    def api_run(force: int = 0):
        try:
            return run(force=bool(force))
        except Exception as e:
            return {"ok": False, "error": str(e)}
except Exception:
    router = None


if __name__ == "__main__":
    out = run(force=True)
    print(json.dumps(out, ensure_ascii=False, indent=2))
