# -*- coding: utf-8 -*-
"""盤後第二段的驗籌碼＋匯流（給 deploy/run_stage2.sh 在 collect 之後呼叫）。

單獨成檔（不與 collect 同 process）避免 collect.py 把 mls-v4 插 sys.path 最前
造成的 config 撞名——本檔的 import 都解析到本地 /opt/mls-screen。
"""
import argparse

from phase import today_tw, get_phase, Phase


def run_stage2(db_path: str = "mls.db") -> dict:
    """執行盤後第二段；由 collect 完成後呼叫，避免只靠漏裝的獨立 timer。"""
    phase = get_phase()
    if phase is not Phase.POST:
        print(f"[stage2] {phase.value} 時段，略過盤後驗證/匯流")
        return {"skipped": phase.value}

    # stage2 也可能被單獨 timer 或人工重跑；不能假設 collect 先初始化過 DB。
    import store
    store.init_db(db_path)

    import b_discover
    import b_verify
    import merge_pool
    import screen_verify
    import config
    d = today_tw()
    # A二 第3站：盤中發現 —— 把當日累積的 b_snapshot 掃成 b_discovery。
    # 必須排在 b_verify 之前（b_verify 驗的就是這份發現）。少了這步 →
    # b_discovery 恆空、merge 永遠 0 B新血、A二 循環斷掉（2026-08-05 補接）。
    dsc = b_discover.scan(config.UNIVERSE, config.CODE_GROUP)
    print(f"[stage2] b_discover 盤中發現={len(dsc.get('items', []))} 檔")
    bv = b_verify.verify(db_path, d)
    print(f"[stage2] b_verify confirmed={len(bv.get('confirmed', []))} "
          f"partial={len(bv.get('partial', []))} "
          f"unconfirmed={len(bv.get('unconfirmed', []))} "
          f"no_data={len(bv.get('no_data', []))}(不淘汰,全數進池)")
    mg = merge_pool.merge(db_path, d)
    print(f"[stage2] 匯流定案：{mg.get('purpose')}")
    # T+1 回測：驗證日＝「最後一個有收盤 bar 的交易日」（不用 today_tw，避免清晨/盤前
    # today 尚無收盤資料而驗錯日）。screen_verify 再往前一交易日抓被盯的那份池。
    import sqlite3
    _c = sqlite3.connect(db_path)
    _row = _c.execute("SELECT MAX(data_date) FROM daily_bar").fetchone()
    _c.close()
    import datetime as _dt2
    vdate = _dt2.date.fromisoformat(_row[0]) if _row and _row[0] else d
    sv = screen_verify.verify(db_path, vdate)
    print(f"[stage2] 回測驗證（驗證日 {vdate}）：{sv.get('purpose')}")
    # 排除名單 T+1 錯殺率量測：讀 funnel_result 被排除那批，回填當日收盤判錯殺。
    # 與 screen_verify 同驗證日、同一趟落庫，兩週後看各因子錯殺率決定放寬哪一層。
    import reject_verify
    rv = reject_verify.verify(db_path, vdate)
    print(f"[stage2] 排除錯殺率量測（驗證日 {vdate}）：{rv.get('purpose')}")
    return {"b_verify": bv, "merge": mg, "screen_verify": sv, "reject_verify": rv}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="盤後 B 鏈驗證、A/B 匯流與收盤復盤")
    ap.add_argument("--db", default="mls.db")
    args = ap.parse_args()
    run_stage2(args.db)
