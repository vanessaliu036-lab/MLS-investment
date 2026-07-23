"""
MLS 插件 — premarket.py(v2.4 新增)
盤前報告:盤前蒐集資料分析 × OpenAI 官方 API 台股觀察池篩選員
====================================================================
每天開盤前執行(手動按鈕或排程 08:00):
  ① 蒐集盤前資料:美股四大指數(道瓊/那斯達克/S&P500/費半)、
     昨日 MLS 決策中心觀察清單與健康分、李佛摩六欄狀態、族群資金流
  ② 把資料組進「台股觀察池篩選員」指令(使用者提供,原文完整內嵌)
  ③ 呼叫 OpenAI 官方 API(chat completions)產出盤前觀察池報告
  ④ 落地 premarket_report 表,首頁「盤前報告」板塊顯示

【API 金鑰留空位】程式已完整串接 OpenAI 官方端點,
  金鑰請填在 .env:OPENAI_API_KEY=(留空時回覆設定指引,不假造報告)
  模型可用 OPENAI_MODEL 覆寫(預設 gpt-4o)。

資料誠實原則:蒐集不到的資料段落直接標「無資料」讓模型知道,
不填假數字;美股指數走 FinMind USStockPrice,失敗則標註缺失。
"""

import os
import json
import urllib.request
from datetime import datetime, timedelta, timezone

import config as C

try:
    import db
except Exception:
    db = None

TW_TZ = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(__file__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")   # ← 留空位,.env 填入
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

US_INDICES = [("^DJI", "道瓊"), ("^IXIC", "那斯達克"),
              ("^GSPC", "S&P500"), ("^SOX", "費城半導體")]

# ════════════════════════════════════════════════════════
# 使用者指令原文(台股觀察池篩選員)— 一字不改內嵌
# ════════════════════════════════════════════════════════
SCREENER_PROMPT = """你是一位「台股觀察池篩選員」，任務是每天從台股中篩選出適合小資金操作、可用 1000 股以下建立部位的觀察名單。

你的目標不是找最多股票，而是找出「正在轉強、即將突破、或剛進入主升段早期」的標的。弱勢股、無題材股、籌碼混亂股，只需要一句話淘汰，不浪費分析時間。
對比美股指數預測當日台股輪動走勢


篩選條件：

1. 股價條件
    優先篩選股價在 30–300 元之間的個股。
    若股價超過 300 元，必須具備明確主流題材、強勢籌碼、法人連買或即將突破關鍵壓力，才可納入觀察。
2. 部位限制
    單一標的初始觀察部位以 100–300 股為主。
    最高不得超過 1000 股。
    若風險偏高，只允許 100 股試單。
    不得一次滿倉，不得因為漲停或急拉追滿。
3. 題材條件
    優先觀察以下族群：
    AI伺服器、記憶體、PCB、CCL、散熱、電源、ASIC、功率半導體、IC設計、被動元件、先進封裝、機器人、航太、軍工、低軌衛星、金融補漲股。
4. 技術條件
    優先挑選符合以下任一條件的股票：
    股價站回月線並量能放大。
    整理 2–4 週後接近突破。
    突破前高但尚未噴出太遠。
    回測月線、季線不破。
    週轉率下降但股價仍穩。
    連續紅 K、低檔轉強、或法人買盤開始集中。
5. 籌碼條件
    優先納入：
    外資、投信、主力至少一方連續買超。
    大戶持股增加。
    融資沒有暴增。
    當沖比下降。
    週轉率下降但股價不跌。
    若主力大賣、融資暴增、當沖過熱，直接列為高風險觀察，不得進場。
6. 新聞與公司面
    每檔股票需檢查是否有近期催化：
    法說會、財報、營收創高、接單、新產品、AI伺服器供應鏈、產業報價上漲、政策題材、外資報告、法人調升目標價。
    沒有新聞支撐但單純技術反彈者，只能列為短線觀察。
7. 觀察池分類
    請把股票分成四類：

A級：可優先進場觀察
條件是題材強、技術轉強、籌碼有買盤、風險可控。
建議部位：100–300 股。

B級：等待突破
條件是整理接近完成，但還沒正式突破。
建議部位：先觀察，突破後 100–200 股。

C級：只適合低接
條件是題材還在，但股價偏高或剛拉回。
建議部位：只在支撐區 100 股試單。

D級：淘汰
條件是技術弱、籌碼亂、題材退燒、融資過熱、或跌破重要支撐。
只需簡短說明淘汰原因。

8. 輸出格式
    請用表格輸出：

股票名稱｜族群題材｜目前狀態｜技術位置｜籌碼狀況｜新聞催化｜等級｜建議動作｜1000股以下部位建議

9. 決策語氣
    請直接給結論，不要模糊。
    若是弱勢股，直接說「淘汰」或「暫不看」。
    若是強勢轉折股，請詳細說明為什麼值得觀察。
    若適合進場，請明確標出：
    試單價、加碼價、停損價、目標壓力區。
10. 每日更新任務
    每天盤後更新一次觀察池：
    新增轉強股。
    移除轉弱股。
    升級突破股。
    降級失敗股。
    追蹤是否符合進場條件。

11.記錄每檔股票的最高建議持股不得超過 1000 股。

最終目標：
建立一個小資金、高機動性的台股觀察池，用 100–1000 股以下的彈性部位，優先抓住主流族群的轉強點、突破點與主升段初期。"""


# ════════════════════════════════════════════════════════
# 〇、資料表
# ════════════════════════════════════════════════════════
def _init_tables():
    with db._lock, db._conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS premarket_report(
          report_date TEXT PRIMARY KEY,
          content TEXT, model TEXT, context_note TEXT, created_ts TEXT
        );""")


def _today():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d")


# ════════════════════════════════════════════════════════
# 一、盤前資料蒐集(缺哪段就誠實標「無資料」)
# ════════════════════════════════════════════════════════
def _us_indices():
    """美股前一交易日收盤(FinMind USStockPrice)。失敗回 None 值。"""
    out = []
    token = os.environ.get("FINMIND_TOKEN", "")
    start = (datetime.now(TW_TZ) - timedelta(days=7)).strftime("%Y-%m-%d")
    for sym, name in US_INDICES:
        row = {"symbol": sym, "name": name, "close": None, "chg_pct": None}
        try:
            url = ("https://api.finmindtrade.com/api/v4/data?dataset=USStockPrice"
                   f"&data_id={urllib.parse.quote(sym)}&start_date={start}"
                   + (f"&token={token}" if token else ""))
            with urllib.request.urlopen(url, timeout=10) as r:
                rows = json.loads(r.read()).get("data", [])
            if len(rows) >= 2:
                a, b = rows[-2], rows[-1]
                row["close"] = b.get("Close")
                if a.get("Close"):
                    row["chg_pct"] = round((b["Close"] - a["Close"]) / a["Close"] * 100, 2)
        except Exception as e:
            print(f"[premarket] 美股 {sym} 取得失敗:{e}")
        out.append(row)
    return out


def _mls_context():
    """昨日 MLS 內部資料:決策觀察清單、健康分前段、李佛摩狀態、勝率。"""
    ctx = {"watchlist": [], "health_top": [], "livermore": [], "stats": None}
    try:
        with db._lock, db._conn() as c:
            r = c.execute("SELECT MAX(target_date) d FROM dec_watchlist").fetchone()
            if r and r["d"]:
                ctx["watchlist"] = [dict(x) for x in c.execute(
                    """SELECT name, code, sector, grade, score, trigger_price, reason
                       FROM dec_watchlist WHERE target_date=? ORDER BY score DESC""",
                    (r["d"],))]
            r = c.execute("SELECT MAX(trade_date) d FROM dec_health").fetchone()
            if r and r["d"]:
                ctx["health_top"] = [dict(x) for x in c.execute(
                    """SELECT name, code, sector, close, chg, quadrant, trend, score, grade
                       FROM dec_health WHERE trade_date=? ORDER BY score DESC LIMIT 20""",
                    (r["d"],))]
            try:
                ctx["livermore"] = [dict(x) for x in c.execute(
                    """SELECT code, state, pivot FROM livermore_record
                       WHERE trade_date=(SELECT MAX(trade_date) FROM livermore_record)""")]
            except Exception:
                pass
    except Exception as e:
        print(f"[premarket] MLS 內部資料取得失敗:{e}")
    try:
        import decision_v22
        ctx["stats"] = decision_v22.stats()
    except Exception:
        pass
    return ctx


def build_messages():
    """組 OpenAI messages:system=篩選員指令原文,user=今日盤前資料包。"""
    us = _us_indices()
    mls = _mls_context()
    us_txt = "\n".join(
        f"- {r['name']}({r['symbol']}):收盤 {r['close'] if r['close'] is not None else '無資料'}"
        f",漲跌 {('%+.2f%%' % r['chg_pct']) if r['chg_pct'] is not None else '無資料'}"
        for r in us)
    wl_txt = "\n".join(
        f"- {w['name']}({w['code']}) {w['sector']}｜{w['grade']} {w['score']}分"
        f"｜觸發>{w['trigger_price']}｜{w['reason']}"
        for w in mls["watchlist"]) or "(無資料)"
    hp_txt = "\n".join(
        f"- {h['name']}({h['code']}) {h['sector']}｜收{h['close']} {h['chg']:+.1f}%"
        f"｜{h['quadrant']}/{h['trend']}｜健康{h['score']} {h['grade']}"
        for h in mls["health_top"]) or "(無資料)"
    liv_txt = "\n".join(
        f"- {C.NAME_MAP.get(x['code'], x['code'])}({x['code']}):{x['state']}"
        + (f"｜{x['pivot']}" if x.get("pivot") else "")
        for x in mls["livermore"]) or "(無資料)"
    stats_txt = "(樣本累積中)"
    if mls["stats"]:
        g = [x for x in mls["stats"]["grades"] if x["grade"] == "Ready"]
        if g and g[0]["total"]:
            stats_txt = (f"近30日 Ready 共{g[0]['total']}檔,命中率 {g[0]['hit_rate']}%,"
                         f"隔日收盤均 {g[0]['avg_next_close_pct']:+.2f}%")
    user_content = f"""今日日期:{_today()}(台北時間,開盤前)

【昨夜美股四大指數】
{us_txt}

【MLS 系統昨日隔日觀察清單(內部決策中心,50檔固定池)】
{wl_txt}

【MLS 個股健康指數前 20(昨日盤後)】
{hp_txt}

【李佛摩六欄狀態(昨日)】
{liv_txt}

【系統近期命中率】
{stats_txt}

注意:以上為 MLS 系統內部 50 檔固定觀察池的資料;你可以在此之外依題材族群補充台股其他符合條件的標的。缺「無資料」的段落請自行以你掌握的市場知識補足判斷,並在報告開頭先用美股指數對比預測今日台股輪動走勢,再輸出觀察池表格。"""
    return [{"role": "system", "content": SCREENER_PROMPT},
            {"role": "user", "content": user_content}], us


# ════════════════════════════════════════════════════════
# 二、OpenAI 官方 API 呼叫(金鑰留空位)
# ════════════════════════════════════════════════════════
def call_openai(messages, temperature=0.4, max_tokens=3500):
    """回傳 (content, err)。OPENAI_API_KEY 未設定時回設定指引,不假造。"""
    key = os.environ.get("OPENAI_API_KEY", OPENAI_API_KEY)
    if not key:
        return None, ("尚未設定 OPENAI_API_KEY。請在 .env 填入:\n"
                      "OPENAI_API_KEY=sk-...\n"
                      "OPENAI_MODEL=gpt-4o(可選,預設 gpt-4o)\n"
                      "填入後重啟 server,再按「產生盤前報告」。")
    body = json.dumps({"model": os.environ.get("OPENAI_MODEL", OPENAI_MODEL),
                       "messages": messages,
                       "temperature": temperature,
                       "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(
        OPENAI_URL, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"], None
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        return None, f"OpenAI API 錯誤 {e.code}:{detail or e.reason}"
    except Exception as e:
        return None, f"OpenAI 連線失敗:{e}"


# ════════════════════════════════════════════════════════
# 三、主流程 + Router
# ════════════════════════════════════════════════════════
def run_premarket(force=False):
    _init_tables()
    today = _today()
    if not force:
        with db._lock, db._conn() as c:
            r = c.execute("SELECT report_date FROM premarket_report WHERE report_date=?",
                          (today,)).fetchone()
            if r:
                return {"ok": True, "date": today, "note": "今日報告已存在(force=1 可重跑)"}
    messages, us = build_messages()
    content, err = call_openai(messages)
    note = "us_ok" if any(r["close"] is not None for r in us) else "us_missing"
    if err:
        return {"ok": False, "error": err, "date": today}
    with db._lock, db._conn() as c:
        c.execute("INSERT OR REPLACE INTO premarket_report VALUES (?,?,?,?,?)",
                  (today, content, os.environ.get("OPENAI_MODEL", OPENAI_MODEL),
                   note, datetime.now(TW_TZ).isoformat(timespec="seconds")))
    return {"ok": True, "date": today, "chars": len(content)}


try:
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter()

    @router.get("/api/premarket/latest")
    def api_latest():
        _init_tables()
        with db._lock, db._conn() as c:
            r = c.execute("""SELECT * FROM premarket_report
                             ORDER BY report_date DESC LIMIT 1""").fetchone()
        if not r:
            return {"date": None, "content": None,
                    "key_ready": bool(os.environ.get("OPENAI_API_KEY", OPENAI_API_KEY))}
        return {**dict(r),
                "key_ready": bool(os.environ.get("OPENAI_API_KEY", OPENAI_API_KEY))}

    @router.post("/api/premarket/run")
    def api_run(force: int = 0):
        try:
            return run_premarket(force=bool(force))
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

except Exception as _e:
    router = None
    print(f"[plugin/premarket] router 未載入:{_e}")


if __name__ == "__main__":
    # 冒煙:組 messages(不打 API),確認資料包與指令完整
    _init_tables() if db else None
    msgs, us = build_messages()
    print("system 指令字數:", len(msgs[0]["content"]))
    assert "台股觀察池篩選員" in msgs[0]["content"]
    assert "1000 股" in msgs[0]["content"]
    print("user 資料包預覽:\n", msgs[1]["content"][:600])
    c, err = call_openai(msgs)
    print("\n未設金鑰時的回覆(留空位驗證):", err)
