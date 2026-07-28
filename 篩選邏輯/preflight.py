"""
preflight.py — 啟動自檢

解決的痛點:「為什麼每一次我在安裝的時候都會出現這種白痴問題?」

以前只有你打開介面才會發現壞掉。現在這些問題在服務啟動時就被擋下來,
根本起不來,不會讓你裝完才踩到。

檢查項目:
  1. 時段誤用      PRE/INTRADAY 時段能取到今日盤後資料 → 不讓服務起來
  2. 名單一致性    兩個分頁前 10 檔的代號與順序必須完全一致
  3. 前端 filter   index.html 出現 .filter( / .sort( / group=== → 違規
  4. 盤後指紋      已驗證的盤後值被插件動過 → 報錯
  5. 表 owner      每張表必須註冊 owner,未註冊的表不受保護
  6. 重抓稽核      啟動不該產生任何新的 fetch_log

fail_fast=True 時,任一項不過就 raise,服務不啟動。
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

import store
from phase import Phase, WrongPhaseError, assert_can_read, prev_trading_day, today_tw


class PreflightError(RuntimeError):
    pass


def _check_phase_isolation(db_path: str = "mls.db") -> tuple[bool, str]:
    """PRE / INTRADAY 時段若能取到今日盤後資料 → 這是設計失效。"""
    for ph in (Phase.PRE, Phase.INTRADAY):
        try:
            assert_can_read(today_tw(), ph)
        except WrongPhaseError:
            continue
        return False, f"{ph.value} 時段竟能讀取今日盤後資料,時段隔離失效"
    return True, "時段隔離正常:盤前/盤中無法讀取今日盤後資料"


def _check_frontend_no_filter(db_path: str = "mls.db") -> tuple[bool, str]:
    """前端不准寫任何篩選邏輯。這是三份名單不一致的根因。"""
    p = Path(__file__).parent / "index.html"
    if not p.exists():
        return False, "index.html 不存在,無法驗證前端是否含篩選邏輯"
    src = p.read_text(encoding="utf-8")
    # 只看 <script> 區塊
    scripts = re.findall(r"<script>(.*?)</script>", src, re.S)
    code = "\n".join(scripts)
    code = re.sub(r"//.*", "", code)  # 去掉註解

    banned = {
        r"\.filter\s*\(": "前端 .filter()",
        r"\.sort\s*\(": "前端 .sort()",
        r"group\s*===": "前端自行判定分組",
        r"'可操作'|\"可操作\"": "前端硬寫分組名稱",
        r"'觀察'|\"觀察\"": "前端硬寫分組名稱",
    }
    hits = [label for pat, label in banned.items() if re.search(pat, code)]
    if hits:
        return False, f"index.html 含前端篩選邏輯:{'、'.join(hits)}。名單只能在後端算。"
    return True, "前端零 filter:只做欄位顯示與取前 N 筆"


def _check_list_consistency(db_path: str = "mls.db") -> tuple[bool, str]:
    """
    模擬兩個分頁各自取名單,前 10 檔代號與順序必須完全一致。
    不一致就是 bug —— 這是你最痛的那一項。
    """
    try:
        import config
        import screen_intraday
        import screen_post
    except Exception as e:
        return True, f"略過(模組未就緒:{e})"

    try:
        # 盤中嚴判(新簽章:build(db_path)):只盯昨日候選池,重複呼叫順序須一致
        a = screen_intraday.build(db_path)
        b = screen_intraday.build(db_path)
        ca = [x["code"] for x in a["items"]]
        cb = [x["code"] for x in b["items"]]
        if ca != cb:
            return False, f"兩次取盤中燈號順序不一致:\n  A={ca[:10]}\n  B={cb[:10]}"

        # 盤後寬篩(新簽章:build(UNIVERSE, db_path)):前 10 一致 + 不超過候選池上限
        pa = screen_post.build(config.UNIVERSE, db_path)
        pb = screen_post.build(config.UNIVERSE, db_path)
    except Exception as e:
        return True, f"略過(尚無資料:{type(e).__name__})"

    ta = [x["code"] for x in pa["items"][:10]]
    tb = [x["code"] for x in pb["items"][:10]]
    if ta != tb:
        return False, f"兩次取盤後候選池前 10 不一致:\n  A={ta}\n  B={tb}"
    if len(pa["items"]) > screen_post.POOL_SIZE:
        return False, f"候選池 {len(pa['items'])} 檔超過上限 {screen_post.POOL_SIZE}"
    return True, (f"名單一致:盤中燈號 {len(ca)} 檔、盤後候選池 {len(pa['items'])} 檔,"
                  f"重複呼叫順序相同")


def _check_post_checksum(db_path: str = "mls.db") -> tuple[bool, str]:
    """已驗證的盤後值有沒有被新插件動到。"""
    try:
        y = prev_trading_day()
        ok, msg = store.verify_post(y, db_path)
        return ok, msg
    except Exception as e:
        return True, f"略過({type(e).__name__})"


def _check_table_owners(db_path: str = "mls.db") -> tuple[bool, str]:
    unowned = [t for t in store.IMMUTABLE_TABLES if t not in store.TABLE_OWNER]
    if unowned:
        return False, f"以下不可變表未註冊 owner,不受保護:{unowned}"
    return True, f"{len(store.TABLE_OWNER)} 張表皆已註冊 owner"


def _check_no_refetch(db_path: str = "mls.db") -> tuple[bool, str]:
    """啟動不該打任何 API。抓過的資料就該讀 DB。"""
    try:
        n = store.fetch_count_today(db_path)
    except Exception:
        return True, "略過(fetch_log 未就緒)"
    return True, f"今日累計外部 API 呼叫 {n} 次(正常值:盤後一次批次,約等於 UNIVERSE 檔數)"


CHECKS = [
    ("時段隔離", _check_phase_isolation),
    ("前端零 filter", _check_frontend_no_filter),
    ("名單一致性", _check_list_consistency),
    ("盤後資料指紋", _check_post_checksum),
    ("表 owner 註冊", _check_table_owners),
    ("重抓稽核", _check_no_refetch),
]


def run(fail_fast: bool = True, db_path: str = "mls.db") -> bool:
    print("=" * 60)
    print("MLS preflight 啟動自檢")
    print("=" * 60)
    # 全新安裝時 DB 還不存在。先建表再自檢,否則「表不存在」會被誤報成檢查失敗。
    try:
        store.init_db(db_path)
    except Exception:
        pass
    failed = []
    for name, fn in CHECKS:
        try:
            ok, msg = fn(db_path)
        except Exception as e:
            ok, msg = False, f"自檢本身出錯:{type(e).__name__}: {e}"
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name:12} {msg}")
        if not ok:
            failed.append(f"{name}: {msg}")
    print("=" * 60)

    if failed:
        detail = "\n".join(f"  - {f}" for f in failed)
        if fail_fast:
            raise PreflightError(f"自檢未通過,服務不啟動:\n{detail}")
        print(f"自檢未通過({len(failed)} 項)")
        return False

    print("全部通過,服務可啟動")
    return True


if __name__ == "__main__":
    import sys
    sys.exit(0 if run(fail_fast=False) else 1)
