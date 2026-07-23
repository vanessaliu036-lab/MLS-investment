"""
MLS v4.0 — chips.py
FinMind 籌碼封裝（法人買賣超 / 融資 / 大戶集保）。

穩定優先：未設 FINMIND_TOKEN 或 DATA_MODE!=real → demo 模式回穩定示意值。
回傳欄位（對齊 decision 需求）：
  inst_net_20d_lots  法人近20日淨買賣超（張）
  inst_streak        法人連續買/賣天數（正買負賣）
  big_holder_trend   大戶集保趨勢（百分點 pp，週資料）
  foreign_lots       外資近20日淨（張）
  invest_lots        投信近20日淨（張）
  margin_trend       融資變化（'增加'/'下降'/'持平'）
  inst_5d_net_lots   近5日三大法人淨買賣超（張）
  trust_5d_lots      近5日投信淨買賣超（張）
  margin_5d_chg      近5日融資餘額變化（張）
  quality            資料品質標記
"""
import random
import config as C


def _shares_to_lots(shares):
    """Convert shares to lots using truncation toward zero."""
    return int(shares / 1000)

_DEMO_CHIPS = {
    "2327": (8500, 3, 0.4, 6200, 2300, "下降"),
    "2383": (5200, 2, 0.2, 3800, 1400, "下降"),
    "5347": (1800, 1, 0.1, 1500, 300, "下降"),
    "8150": (2400, 2, 0.3, 1900, 500, "持平"),
    "2303": (600, 1, 0.0, 400, 200, "持平"),
    "2337": (-800, -2, -0.2, -600, -200, "增加"),
    "2344": (-3200, -4, -0.5, -2800, -400, "增加"),
    "6147": (-5400, -5, -0.6, -4900, -500, "增加"),
    "6488": (4200, 4, 0.5, 3600, 600, "下降"),
}


def _demo_chip(code):
    if code in _DEMO_CHIPS:
        net, streak, trend, fr, inv, margin = _DEMO_CHIPS[code]
    else:
        random.seed(hash(code) & 0xffff)
        net = random.randint(-3000, 5000)
        streak = random.randint(-3, 4)
        trend = round(random.uniform(-0.5, 0.5), 1)
        fr = int(net * 0.8)
        inv = net - fr
        margin = random.choice(["增加", "下降", "持平"])
    return {
        "inst_net_20d_lots": net, "inst_streak": streak,
        "big_holder_trend": trend, "foreign_lots": fr, "invest_lots": inv,
        "margin_trend": margin, "inst_5d_net_lots": round(net / 4),
        "trust_5d_lots": round(inv / 4),
        "margin_5d_chg": -561 if margin == "下降" else (600 if margin == "增加" else 0),
        "quality": "demo",
    }


def get_chips(code):
    """回傳籌碼 dict。優先 DB cache (inst_daily) → 沒有走 demo。
    cache 來源：data_collector.fetch_today_all_to_db 每日盤後寫入。"""
    import db as _db
    rows = _db.load_inst_recent(code, days=20)
    if not rows:
        return _demo_chip(code)
    rows = list(reversed(rows))  # 舊 → 新
    foreign_20 = sum((r["foreign_lots"] or 0) for r in rows)
    invest_20 = sum((r["invest_lots"] or 0) for r in rows)
    dealer_20 = sum((r["dealer_lots"] or 0) for r in rows)
    net = foreign_20 + invest_20 + dealer_20
    # 5 日
    last5 = rows[-5:] if len(rows) >= 5 else rows
    inst_5d = sum((r["foreign_lots"] or 0) + (r["invest_lots"] or 0) +
                  (r["dealer_lots"] or 0) for r in last5)
    trust_5d = sum((r["invest_lots"] or 0) for r in last5)
    # 連續天數：最近一日開始算同方向
    streak = 0
    for r in reversed(rows):
        d = (r["foreign_lots"] or 0) + (r["invest_lots"] or 0) + (r["dealer_lots"] or 0)
        if d == 0:
            break
        s = 1 if d > 0 else -1
        if streak == 0:
            streak = s
        elif (streak > 0) == (s > 0):
            streak += s
        else:
            break
    return {
        "inst_net_20d_lots": net,
        "inst_streak": streak,
        "big_holder_trend": None,  # 大戶集保仍需 FinMind 週資料，暫不接
        "foreign_lots": foreign_20,
        "invest_lots": invest_20,
        "inst_5d_net_lots": inst_5d,
        "trust_5d_lots": trust_5d,
        "margin_5d_chg": 0,  # 融資券另抓
        "margin_trend": "持平",
        "quality": f"db_cache_{len(rows)}d",
    }
