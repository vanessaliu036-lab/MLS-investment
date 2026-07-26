# -*- coding: utf-8 -*-
"""Phase 3/4 模型單測：T+1 分流驗證 + Radar 優先名單 + 落選留痕。

純函式（judge_watchlist_row / select_radar_watchlist）無副作用；
verify_today 走臨時 sqlite。不需 Shioaji：broker/chips/notifier 於匯入前 stub。
"""
import sys
import types
import tempfile
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "個股卡片相關檔案_20260722"
sys.path.insert(0, str(DIR))
for _m in ("broker", "chips", "notifier"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

import after_hours as ah   # noqa: E402
import db                  # noqa: E402


def _round(t):
    v, h, r = t
    return (v, h, None if r is None else round(r, 4))


def test_judge_split():
    j = ah.judge_watchlist_row
    assert _round(j("radar", 2.5, 1.0)) == ("A_突破成功", True, 0.025)
    assert _round(j("radar", 1.0, 0.5)) == ("B_續強", False, 0.01)
    assert _round(j("radar", 3.0, -0.2)) == ("C_未續強", False, 0.03)   # 相對族群為負
    assert _round(j("radar", 0.2, 1.0)) == ("C_未續強", False, 0.002)   # 未達續強
    assert _round(j("resilient", -1.8, 2.0)) == ("抗跌成立", True, -0.018)
    assert _round(j("resilient", -3.0, 2.0)) == ("抗跌失敗", False, -0.03)  # 破 -2%
    assert _round(j("resilient", -1.0, -0.5)) == ("抗跌失敗", False, -0.01)  # 相對負
    assert _round(j(None, 1.0, 1.0)) == (None, False, 0.01)            # 舊格式
    assert _round(j("radar", None, 1.0)) == ("待資料", False, None)


def test_select_radar_priority_then_resilient():
    radar = [
        {"code": "2455", "name": "全新", "sector": "光通訊", "group": "可操作", "score": 86, "price": 327.5, "score_pct": 86},
        {"code": "3374", "name": "精材", "sector": "封測", "group": "可操作", "score": 77, "price": 377.5, "score_pct": 77},
        {"code": "4919", "name": "新唐", "sector": "無人機", "group": "觀察", "score": 40, "price": 137, "score_pct": 40},
        {"code": "6182", "name": "合晶", "sector": "晶圓材料", "group": "排除", "score": 10, "price": 50, "score_pct": 10},
    ]
    resilient = [
        {"code": "2049", "name": "上銀", "sector": "無人機", "reason": "抗跌", "source": "resilient"},
        {"code": "3374", "name": "精材", "sector": "封測", "reason": "dup", "source": "resilient"},
    ]
    picks, rej = ah.select_radar_watchlist(radar, resilient, limit=3)
    assert [p["code"] for p in picks] == ["2455", "3374", "4919"]   # 可操作(分數)→觀察
    assert picks[0]["source"] == "radar" and picks[0]["factor_score"] == 86
    assert picks[0]["entry_ref"] == 327.5 and picks[0]["group_at_pick"] == "可操作"
    picks2, _ = ah.select_radar_watchlist(radar, resilient, limit=5)
    assert [p["code"] for p in picks2] == ["2455", "3374", "4919", "2049"]  # 補足去重
    assert "6182" in {r["code"] for r in rej}                       # 排除→落選留痕


def test_verify_today_integration():
    db.DB_PATH = str(Path(tempfile.mkdtemp()) / "t.db")
    db.init()
    db.today = lambda: "2026-07-27"   # 假裝今天=名單日
    db.save_watchlist("2026-07-27", [
        {"code": "2455", "name": "全新", "sector": "光通訊", "source": "radar",
         "entry_ref": 327.5, "factor_score": 86, "group_at_pick": "可操作", "reason": "可操作"},
        {"code": "2049", "name": "上銀", "sector": "無人機", "source": "resilient", "reason": "抗跌"},
        {"code": "9999", "name": "舊股", "sector": "其他", "reason": "無source"},  # 舊格式
    ])
    snaps = [
        {"code": "2455", "change_rate": 2.6, "price": 336, "volume_ratio": 1.8, "group": "可操作", "aflow": 2000},
        {"code": "2049", "change_rate": -1.5, "price": 300, "volume_ratio": 0.9, "group": "觀察"},
        {"code": "9999", "change_rate": 3.0, "price": 50, "volume_ratio": 2.0},
    ]
    sectors = [{"name": "光通訊", "pct": 0.5}, {"name": "無人機", "pct": -4.0}, {"name": "其他", "pct": 0.0}]
    res = ah.verify_today(snaps, sectors, {"9999"}, {"9999"})
    assert res["hit"] == 3                       # A + 抗跌成立 + 相容命中
    assert res["stats"]["max_return"] == 3.0 and res["stats"]["min_return"] == -1.5
    oc = {r["stock_id"]: r for r in db.load_watch_outcome("2026-07-27")}
    assert oc["2455"]["verdict"] == "A_突破成功"
    assert oc["2049"]["verdict"] == "抗跌成立"
    assert oc["9999"]["verdict"] == "相容命中"


if __name__ == "__main__":
    test_judge_split()
    test_select_radar_priority_then_resilient()
    test_verify_today_integration()
    print("ALL PASS ✅")
