"""
MLS 插件 — health_history.py
個股健康指數時間序列 + 命中率統計(2026-07-09 Vanessa 規格)
================================================================
兩個職責:
1. 每日存檔:把當日 /api/money_health 的每檔決策卡快照存到
   reports/health_score_history/YYYYMMDD.json,做為時間序列基準。
2. 命中率統計:依健康分 ≥65 / 50-64 / <50 三組別 + 四象限(in_up/in_down/out_up/out_down)
   計算「隔日報酬率」,用於評估模型預測力。

純插件延伸:不動主邏輯,不動 money_health.py。
"""

import os
import json
import glob
from datetime import datetime, timedelta

HISTORY_DIR = os.path.join(os.path.dirname(__file__), "reports", "health_score_history")


def _ensure_dir():
    os.makedirs(HISTORY_DIR, exist_ok=True)


def _today_filename():
    return datetime.now().strftime("%Y%m%d") + ".json"


def save_snapshot(cards, market_pct=0.0):
    """
    把當日 /api/money_health 的 cards 快照存到
    reports/health_score_history/YYYYMMDD.json。
    cards 結構(code/name/sector/price/change_rate/quadrant/label/health_score/.../chip/...)
    """
    _ensure_dir()
    fname = os.path.join(HISTORY_DIR, _today_filename())
    snapshot = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "as_of": datetime.now().strftime("%H:%M:%S"),
        "market_pct": market_pct,
        "count": len(cards),
        "cards": cards,
    }
    with open(fname, "w") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    return fname


def load_snapshot(date_str):
    """
    讀指定日期快照。date_str 格式 'YYYY-MM-DD' 或 'YYYYMMDD'。
    回傳 snapshot dict 或 None(該日無資料)。
    """
    if "-" in date_str:
        date_str = date_str.replace("-", "")
    fname = os.path.join(HISTORY_DIR, date_str + ".json")
    if not os.path.exists(fname):
        return None
    with open(fname) as f:
        return json.load(f)


def load_recent_snapshots(n_days=5):
    """
    讀最近 n 個交易日的快照(含當日)。
    回傳 list of snapshot dict,按日期舊→新排序。
    """
    _ensure_dir()
    files = sorted(glob.glob(os.path.join(HISTORY_DIR, "*.json")))
    recent = files[-n_days:] if len(files) >= n_days else files
    out = []
    for f in recent:
        try:
            with open(f) as fp:
                out.append(json.load(fp))
        except Exception:
            continue
    return out


def time_series_for_code(code, snapshots=None):
    """
    對單一個股,輸出健康分時間序列。
    回傳 list of {date, health_score, quadrant, change_rate, ...} 按日期舊→新排序。
    snapshots: 外部傳入的 list(預設 load_recent_snapshots(30))
    """
    if snapshots is None:
        snapshots = load_recent_snapshots(30)
    series = []
    for snap in snapshots:
        for c in (snap.get("cards") or []):
            if c.get("code") == code:
                series.append({
                    "date": snap.get("date"),
                    "health_score": c.get("health_score"),
                    "quadrant": c.get("quadrant"),
                    "change_rate": c.get("change_rate"),
                    "state": (c.get("decision") or {}).get("state"),
                    "ai_score": (c.get("decision") or {}).get("ai_score"),
                    "confidence": (c.get("decision") or {}).get("confidence"),
                    "chip_score": ((c.get("chip") or {}).get("chip_score") or {}).get("score"),
                })
                break
    return series


def hit_rate_stats(snapshots=None):
    """
    命中率統計:
      1) 健康分 ≥65 / 50-64 / <50 三組的「隔日報酬率」分布
      2) in_up / in_down / out_up / out_down 四象限的「隔日報酬率」分布

    算法:從 snapshots 取出連續兩個交易日 T-1 與 T 的 cards,
    比對同一股的 change_rate[T] - change_rate[T-1] 即為「隔日變動」。
    累計平均報酬、勝率(隔日變動 > 0 的機率)。
    """
    if snapshots is None:
        snapshots = load_recent_snapshots(60)
    if len(snapshots) < 2:
        return {
            "as_of": datetime.now().strftime("%Y-%m-%d"),
            "note": "資料不足,需要至少 2 個交易日的快照",
            "snapshots_n": len(snapshots),
        }

    # 取出所有 (date, code) → card 的索引,只算連續交易日對
    pairs = []
    for i in range(len(snapshots) - 1):
        prev = snapshots[i]
        cur = snapshots[i + 1]
        prev_by_code = {c["code"]: c for c in (prev.get("cards") or []) if c.get("code")}
        cur_by_code = {c["code"]: c for c in (cur.get("cards") or []) if c.get("code")}
        common = set(prev_by_code) & set(cur_by_code)
        for code in common:
            p = prev_by_code[code]
            c = cur_by_code[code]
            prev_chg = p.get("change_rate") or 0
            cur_chg = c.get("change_rate") or 0
            next_ret = cur_chg - prev_chg  # T 日相對 T-1 日的變動(代表 T-1 訊號下,T 日報酬)
            pairs.append({
                "code": code,
                "name": p.get("name"),
                "prev_date": prev.get("date"),
                "cur_date": cur.get("date"),
                "prev_hs": p.get("health_score"),
                "prev_quad": p.get("quadrant"),
                "prev_chg": prev_chg,
                "cur_chg": cur_chg,
                "next_ret": next_ret,
            })

    # 1) 三組健康分
    def _bucket_stats(pairs_in_bucket):
        if not pairs_in_bucket:
            return {"n": 0, "avg_ret": None, "win_rate": None, "median_ret": None}
        rets = [p["next_ret"] for p in pairs_in_bucket]
        wins = sum(1 for r in rets if r > 0)
        rets_sorted = sorted(rets)
        mid = rets_sorted[len(rets_sorted) // 2]
        return {
            "n": len(rets),
            "avg_ret": round(sum(rets) / len(rets), 3),
            "win_rate": round(wins / len(rets) * 100, 1),
            "median_ret": round(mid, 3),
            "max_ret": round(max(rets), 3),
            "min_ret": round(min(rets), 3),
        }

    hs_high = [p for p in pairs if (p["prev_hs"] or 0) >= 65]
    hs_mid = [p for p in pairs if (p["prev_hs"] or 0) >= 50 and (p["prev_hs"] or 0) < 65]
    hs_low = [p for p in pairs if (p["prev_hs"] or 0) < 50]

    # 2) 四象限
    quad_buckets = {"in_up": [], "in_down": [], "out_up": [], "out_down": []}
    for p in pairs:
        q = p["prev_quad"]
        if q in quad_buckets:
            quad_buckets[q].append(p)

    return {
        "as_of": datetime.now().strftime("%Y-%m-%d"),
        "snapshots_n": len(snapshots),
        "pairs_n": len(pairs),
        "by_health_score": {
            "high_ge_65": _bucket_stats(hs_high),
            "mid_50_64": _bucket_stats(hs_mid),
            "low_lt_50": _bucket_stats(hs_low),
        },
        "by_quadrant": {
            "in_up": _bucket_stats(quad_buckets["in_up"]),
            "in_down": _bucket_stats(quad_buckets["in_down"]),
            "out_up": _bucket_stats(quad_buckets["out_up"]),
            "out_down": _bucket_stats(quad_buckets["out_down"]),
        },
    }


def time_series_for_all(codes, snapshots=None):
    """
    批次:對一群個股,各跑 time_series_for_code。
    回傳 dict[code] = series
    """
    if snapshots is None:
        snapshots = load_recent_snapshots(30)
    return {code: time_series_for_code(code, snapshots) for code in codes}