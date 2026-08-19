"""
rule_attribution.py — 規則級歸因表(2026-08-19 定案)

背景:複盤第三次出現「篩掉的隔天反而最強」,單看「被淘汰 → T+1 漲跌」不夠,
要知道「因為哪條規則被降級/淘汰」,才能查出是哪條規則方向錯了,而不是猜門檻
該從 50 調到 45。

這支不重算任何東西,只是把已經存在的兩層量測合流成一張表:
  reject_verify.stats()  → 真淘汰(四閘全中,dropped_pool)那批,依 fail_layer 分組
  screen_verify.stats()  → 沒被淘汰但被規則降級(tier / verification_status)那批,
                            依 tier、verification_status 分組
兩者用同一個判準對齊:avg_ret(或 avg_high_ret)明顯是正的、且 n 夠大的規則,
就是複盤要優先檢討方向、不是微調門檻的規則。

owner 規範:不寫表,純讀 reject_outcome / pool_outcome 彙總,可以隨時重跑、爆掉不影響任何名單。
"""

from __future__ import annotations

import reject_verify
import screen_verify


def attribution(days: int = 30, db_path: str = "mls.db") -> dict:
    """合流版規則歸因表:一條規則一列,無論它是造成淘汰還是降級。"""
    rv = reject_verify.stats(days, db_path)
    sv = screen_verify.stats(days, db_path)

    rows: list[dict] = []
    for f in rv["by_factor"]:
        rows.append({
            "rule": f["fail_layer"] or "（無因子）",
            "final_state": "❌ 結構失效(真淘汰)",
            "n": f["n"], "avg_ret": f["avg_high_ret"],
            "miss_rate": f["miskill_rate"],
            "basis": "T+1 盤中最高漲幅(誤刪率口徑)",
        })
    for t in sv["by_tier"]:
        rows.append({
            "rule": t["tier"] or "（無 tier)",
            "final_state": t["tier"] or "（無 tier)",
            "n": t["n"], "avg_ret": t["avg_ret"],
            "miss_rate": t["up5_rate"],
            "basis": "T+1 收盤報酬(全體,不篩 hit)",
        })
    for v in sv["by_verification"]:
        rows.append({
            "rule": f"B鏈驗證={v['verification_status']}",
            "final_state": v["verification_status"] or "（無驗證狀態）",
            "n": v["n"], "avg_ret": v["avg_ret"],
            "miss_rate": v["up5_rate"],
            "basis": "T+1 收盤報酬(全體,不篩 hit)",
        })

    rows.sort(key=lambda r: -(r["avg_ret"] if r["avg_ret"] is not None else -999))

    suspects = [r for r in rows
                if r["n"] >= 5 and r["avg_ret"] is not None and r["avg_ret"] > 0
                and r["final_state"] not in ("命中", "排對")]

    return {
        "window_days": days,
        "purpose": ("規則歸因表:每條造成淘汰/降級的規則,配上它那批股票實際的 T+1 表現。"
                    "avg_ret 為正且樣本數不小的降級/淘汰規則 = 嫌疑犯,不是門檻該往哪調,"
                    "是這條規則的方向可能反了。"),
        "rows": rows,
        "suspects": suspects,
        "reject_stats": rv,
        "verify_stats": sv,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(attribution(), ensure_ascii=False, indent=2))
