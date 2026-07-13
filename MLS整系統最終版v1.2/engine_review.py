"""
MLS 插件 — engine_review.py(v2.4.2 新增)
主引擎資格動態審查:跟著主流走,引擎名單不再寫死
====================================================================
設計修正(回應使用者指正):
  「引擎股只當溫度計」是角色規則(Rule 0),保留。
  「聯電/世界永遠是引擎」是成員清單,凍結它是 bug——主流會輪動,
  今天的引擎明天可能變成攻擊標的,反之亦然。
  本模組把成員資格改為數據驅動:

引擎行為的定義(可量化):
  ① 低波動:60日日報酬標準差落在觀察池後 25%(錢停泊,不是錢衝鋒)
  ② 法人持續佈局:近月法人淨買超 > 0 或連買 ≥ 3 日
  ③ 大資金量體:60日平均成交金額落在觀察池前 50%
  三者同時成立 = 引擎行為;現任引擎若波動竄升至前 40%,
  代表它已經「動起來」= 攻擊行為,應降轉攻擊部隊(變成可交易!)。

審查節奏:每週五盤後自動審查一次(after_hours 掛鉤),平日不動——
  角色是結構性判斷,不能被單日行情牽著跑。

套用模式(AUTO_APPLY_ROLES 環境變數):
  false(預設)= 只產建議 + Telegram 通知,妳確認後 POST /api/roles/apply
  true         = 審查通過即自動改寫 engine_roles.json 並通知(全自動跟主流)

不論名單怎麼變,角色規則不變:當下是引擎的,全系統(訊號版/六點/
決策中心/篩選器)一律只當溫度計——改的是「誰」,不是「規矩」。
"""

import os
import json
from datetime import datetime, timedelta, timezone
from statistics import stdev, mean

import config as C

try:
    import broker
except Exception:
    broker = None
try:
    import db
except Exception:
    db = None

TW_TZ = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(__file__)
ROLE_FILE = os.path.join(BASE_DIR, "engine_roles.json")

LOOKBACK = 60          # 行為觀察窗(日)
VOL_ENGINE_PCTL = 0.25 # 波動 ≤ 池內 25 分位 = 停泊行為
VOL_ATTACK_PCTL = 0.40 # 現任引擎波動 ≥ 40 分位 = 已動起來,建議降轉
AMT_MIN_PCTL = 0.50    # 成交金額 ≥ 池內中位 = 大資金量體
AUTO_APPLY = os.environ.get("AUTO_APPLY_ROLES", "false").lower() == "true"


def _init_tables():
    with db._lock, db._conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS engine_review(
          review_date TEXT, code TEXT,
          vol REAL, vol_pctl REAL, amt_pctl REAL,
          inst_ok INTEGER, engine_like INTEGER,
          current_role TEXT, suggest TEXT,      -- promote / demote / keep
          note TEXT,
          PRIMARY KEY(review_date, code)
        );""")


def _metrics(code, injected=None):
    """60日行為指標:波動、平均成交金額(缺日K回 None)。"""
    bars = injected
    if bars is None:
        if broker is None:
            return None
        try:
            bars = broker.daily_kbars(code, days=LOOKBACK + 5)
        except Exception:
            return None
    closes = [b.get("close") for b in bars if b.get("close")]
    if len(closes) < 30:
        return None
    rets = [(b - a) / a * 100 for a, b in zip(closes, closes[1:])]
    amt = mean((b.get("volume") or 0) * (b.get("close") or 0)
               for b in bars[-LOOKBACK:])
    return {"vol": round(stdev(rets), 3), "amt": amt}


def _inst_ok(code):
    try:
        import chips
        ch = chips.get_chips(code) or {}
        return 1 if ((ch.get("inst_net_20d_lots") or 0) > 0
                     or (ch.get("inst_streak") or 0) >= 3) else 0
    except Exception:
        return None    # 無籌碼資料時不作為否決條件


def current_roles():
    """讀取生效名單(engine_roles.json 優先,否則 config 預設)。"""
    if os.path.exists(ROLE_FILE):
        try:
            return set(json.load(open(ROLE_FILE, encoding="utf-8"))["engines"])
        except Exception:
            pass
    return set(C.ENGINE_STOCKS)


def review(injected_map=None):
    """
    全池審查。回傳 {date, rows, suggestions, applied}。
    建議規則:
      現任引擎 且 vol_pctl >= VOL_ATTACK_PCTL           → demote(已動起來,可轉攻擊)
      非引擎  且 vol_pctl <= VOL_ENGINE_PCTL
              且 amt_pctl >= AMT_MIN_PCTL 且 inst_ok≠0  → promote(停泊行為,建議轉引擎)
      其餘 keep。
    """
    _init_tables()
    rdate = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    engines = current_roles()
    mets = {}
    for code in C.UNIVERSE:
        m = _metrics(code, injected=(injected_map or {}).get(code))
        if m:
            mets[code] = m
    if len(mets) < 10:
        return {"date": rdate, "rows": [], "suggestions": [],
                "applied": False, "note": "日K覆蓋不足,跳過本次審查"}
    vols = sorted(m["vol"] for m in mets.values())
    amts = sorted(m["amt"] for m in mets.values())

    def pctl(sorted_vals, v):
        return round(sum(1 for x in sorted_vals if x <= v) / len(sorted_vals), 2)

    rows, suggestions = [], []
    for code, m in mets.items():
        vp = pctl(vols, m["vol"])
        ap = pctl(amts, m["amt"])
        iok = _inst_ok(code)
        engine_like = 1 if (vp <= VOL_ENGINE_PCTL and ap >= AMT_MIN_PCTL
                            and iok != 0) else 0
        role = "engine" if code in engines else "attack"
        suggest, note = "keep", ""
        if role == "engine" and vp >= VOL_ATTACK_PCTL:
            suggest = "demote"
            note = (f"現任引擎但60日波動已達池內{vp:.0%}分位,行為=攻擊,"
                    f"建議降轉攻擊部隊(轉為可交易)")
        elif role == "attack" and engine_like:
            suggest = "promote"
            note = (f"波動{vp:.0%}分位(停泊)+金額{ap:.0%}分位+法人佈局,"
                    f"行為=引擎,建議轉溫度計")
        r = {"review_date": rdate, "code": code,
             "name": C.NAME_MAP.get(code, code),
             "vol": m["vol"], "vol_pctl": vp, "amt_pctl": ap,
             "inst_ok": iok, "engine_like": engine_like,
             "current_role": role, "suggest": suggest, "note": note}
        rows.append(r)
        if suggest != "keep":
            suggestions.append(r)
        with db._lock, db._conn() as c:
            c.execute("""INSERT OR REPLACE INTO engine_review VALUES
              (?,?,?,?,?,?,?,?,?,?)""",
              (rdate, code, m["vol"], vp, ap, iok, engine_like,
               role, suggest, note))

    applied = False
    if suggestions and AUTO_APPLY:
        apply_suggestions(suggestions)
        applied = True
    return {"date": rdate, "rows": rows, "suggestions": suggestions,
            "applied": applied}


def apply_suggestions(suggestions):
    """把建議寫入 engine_roles.json(重啟或下次 import 後全系統生效)。"""
    engines = current_roles()
    for s in suggestions:
        if s["suggest"] == "demote":
            engines.discard(s["code"])
        elif s["suggest"] == "promote":
            engines.add(s["code"])
    data = {"engines": sorted(engines),
            "updated": datetime.now(TW_TZ).isoformat(timespec="seconds"),
            "note": "由 engine_review 產生;角色規則(引擎=溫度計)不變,變的是成員"}
    with open(ROLE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    try:
        C.reload_roles()
    except Exception:
        pass
    return data


def summary_text(out):
    if out.get("note"):
        return f"🧭 引擎審查:{out['note']}"
    if not out["suggestions"]:
        return (f"🧭 引擎審查 {out['date']}:全數 keep,"
                f"現任引擎行為仍符合溫度計定義")
    lines = [f"🧭 引擎審查 {out['date']}:{len(out['suggestions'])} 項角色變更建議"
             + ("(已自動套用)" if out["applied"] else "(待確認 /api/roles/apply)")]
    for s in out["suggestions"]:
        arrow = "引擎→攻擊" if s["suggest"] == "demote" else "攻擊→引擎"
        lines.append(f"· {s['name']}({s['code']}){arrow}:{s['note']}")
    return "\n".join(lines)


try:
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter()

    @router.get("/api/roles/review")
    def api_review(run: int = 0):
        _init_tables()
        if run:
            out = review()
            return {**out, "summary": summary_text(out)}
        with db._lock, db._conn() as c:
            r = c.execute("SELECT MAX(review_date) d FROM engine_review").fetchone()
            rows = [dict(x) for x in c.execute(
                "SELECT * FROM engine_review WHERE review_date=?", (r["d"],))] \
                if r and r["d"] else []
        return {"date": r["d"] if r else None, "rows": rows,
                "engines_now": sorted(current_roles()),
                "auto_apply": AUTO_APPLY}

    @router.post("/api/roles/apply")
    def api_apply():
        """套用最近一次審查的建議(手動確認模式)。"""
        _init_tables()
        with db._lock, db._conn() as c:
            r = c.execute("SELECT MAX(review_date) d FROM engine_review").fetchone()
            if not (r and r["d"]):
                return JSONResponse({"ok": False, "error": "尚無審查紀錄"}, status_code=400)
            sug = [dict(x) for x in c.execute(
                """SELECT * FROM engine_review
                   WHERE review_date=? AND suggest!='keep'""", (r["d"],))]
        if not sug:
            return {"ok": True, "changed": 0, "note": "本次審查無變更建議"}
        data = apply_suggestions(sug)
        return {"ok": True, "changed": len(sug), "engines": data["engines"],
                "note": "已寫入 engine_roles.json;新角色立即生效(config 已熱載)"}

except Exception as _e:
    router = None
    print(f"[plugin/engine_review] router 未載入:{_e}")


if __name__ == "__main__":
    import random
    random.seed(9)
    inj = {}
    for i, code in enumerate(C.UNIVERSE):
        bars, p = [], 100.0
        # 讓 2303 波動竄升(引擎變攻擊)、讓 2330 假設性低波大額(攻擊變引擎)
        hot = code == "2303"
        calm = code == C.UNIVERSE[5] and code not in C.ENGINE_STOCKS
        for d in range(70):
            amp = 0.035 if hot else (0.004 if calm else random.uniform(0.008, 0.022))
            p *= 1 + random.uniform(-amp, amp)
            bars.append({"date": f"D{d:02d}", "close": round(p, 2),
                         "high": round(p * 1.01, 2), "low": round(p * 0.99, 2),
                         "volume": 900000 if calm else random.randint(3000, 40000)})
        inj[code] = bars
    out = review(injected_map=inj)
    print(summary_text(out))
    assert any(s["code"] == "2303" and s["suggest"] == "demote"
               for s in out["suggestions"]), "2303 高波動應被建議降轉"
    data = apply_suggestions(out["suggestions"])
    print("套用後引擎名單:", data["engines"])
    assert "2303" not in data["engines"]
    print("動態角色審查 OK")
