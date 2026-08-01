# -*- coding: utf-8 -*-
"""盤後第二段的驗籌碼＋匯流（給 deploy/run_stage2.sh 在 collect 之後呼叫）。

單獨成檔（不與 collect 同 process）避免 collect.py 把 mls-v4 插 sys.path 最前
造成的 config 撞名——本檔的 import 都解析到本地 /opt/mls-screen。
"""
from phase import today_tw, get_phase, Phase

if get_phase() is Phase.CLOSED:
    print("[stage2] 休市，略過驗籌碼/匯流")
else:
    import b_verify
    import merge_pool
    import screen_verify
    d = today_tw()
    bv = b_verify.verify("mls.db", d)
    print(f"[stage2] b_verify passed={len(bv.get('passed', []))} "
          f"failed={len(bv.get('failed', []))} pending={len(bv.get('pending', []))}")
    mg = merge_pool.merge("mls.db", d)
    print(f"[stage2] 匯流定案：{mg.get('purpose')}")
    # T+1 回測：驗證日＝「最後一個有收盤 bar 的交易日」（不用 today_tw，避免清晨/盤前
    # today 尚無收盤資料而驗錯日）。screen_verify 再往前一交易日抓被盯的那份池。
    import sqlite3
    _c = sqlite3.connect("mls.db")
    _row = _c.execute("SELECT MAX(data_date) FROM daily_bar").fetchone()
    _c.close()
    import datetime as _dt2
    vdate = _dt2.date.fromisoformat(_row[0]) if _row and _row[0] else d
    sv = screen_verify.verify("mls.db", vdate)
    print(f"[stage2] 回測驗證（驗證日 {vdate}）：{sv.get('purpose')}")
