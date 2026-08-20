#!/usr/bin/env python3
"""部署前漂移檢查 —— 擋住「靜默回捲」與「靜默刪除」。

為什麼需要這支(兩次真實事故,都不會有任何錯誤訊息):

  1. 靜默刪除:`ops/mls_aflow_watchdog.py` 只存在於線上、從未進 git。
     2026-08-14 的 `rsync --delete` 把它刪了,timer 照樣每 30 秒觸發,
     空燒失敗兩天沒人發現 —— oneshot 失敗不影響任何服務狀態。

  2. 靜默回捲:`screen_post.py` 線上版有六態分類與籌碼背離救回,repo 沒有。
     從 repo 部署會把那些功能抹掉,而服務照樣 active、py_compile 照樣過。

兩者的共通點:**沒有任何一個健康檢查會亮紅燈。** 所以只能在部署「之前」擋。

規則:線上有、repo 沒有(或內容不同)的程式碼,一律先收進 git 再部署。
      沒有例外 —— 要退場舊檔案就明確 git rm,不能靠 rsync 順手刪。

用法:
    python3 ops/deploy_guard.py            # 檢查,有漂移回傳 1
    python3 ops/deploy_guard.py --adopt    # 把線上獨有/較新的檔案抓回本機待 commit
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SSH_HOST = "root@66.42.42.150"

# 本機目錄 → 線上目錄。篩選邏輯/ 是 8002 引擎的正本,不在 /opt/mls-intraday 底下,
# 所以必須分開對照 —— deploy_vps.sh 長年 --exclude 它,引擎因此從不隨部署更新。
ROOTS = [(".", "/opt/mls-intraday", ["篩選邏輯"]),
         ("篩選邏輯", "/opt/mls-screen", [])]

# 資料與產出,不是源碼:不比對、不搬運。與 deploy_vps.sh 的 --exclude 對齊。
SKIP_SUFFIX = (".db", ".db-wal", ".db-shm", ".log", ".pyc")
SKIP_PARTS = {".git", "__pycache__", "card_cache", "reports", "node_modules",
              ".venv", "venv", ".claude", "tests"}
SKIP_NAMES = {".env", ".DS_Store", "live_state.json", "chips_cache.json",
              "ma20_cache.json", "intraday_live_snapshot.json",
              # 執行期狀態與產出,不是源碼:線上會有、repo 不該有。
              "stage2-status.json", "source.manifest.sha256"}


def skip(rel: str) -> bool:
    p = Path(rel)
    # 任何以 . 開頭的路徑段都跳過 —— 這條同時擋掉線上的 .venv-eod/
    # (幾千個 site-packages 檔會把真正的漂移淹沒,讓這支工具沒人想看)。
    if any(part.startswith(".") or part in SKIP_PARTS for part in p.parts):
        return True
    # /opt/mls-screen 既有的 bak-* 是人工保留的部署備份，不是引擎源碼；
    # deploy_vps.sh 同樣排除它們，避免 rsync --delete 誤刪可回滾成果。
    if any(part.startswith("bak-") for part in p.parts):
        return True
    if p.name in SKIP_NAMES or p.name.startswith("backup_"):
        return True
    if ".bak" in p.name:
        return True
    return p.suffix in SKIP_SUFFIX


def remote_hashes(remote_dir: str, prune: list[str]) -> dict[str, str]:
    """線上該目錄下每個檔的 md5。找不到目錄回空 dict(視為全新部署)。"""
    prune_expr = "".join(f" -path './{d}' -prune -o" for d in prune)
    cmd = (f"cd {remote_dir} 2>/dev/null && find ."
           f"{prune_expr} -type f -print0 | xargs -0 md5sum 2>/dev/null")
    out = subprocess.run(["ssh", SSH_HOST, cmd], capture_output=True, text=True)
    result = {}
    for line in out.stdout.splitlines():
        digest, _, path = line.partition("  ")
        rel = path[2:] if path.startswith("./") else path
        if rel and not skip(rel):
            result[rel] = digest
    return result


def local_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def seen_in_git(rel: str, digest: str) -> bool:
    """線上那份內容,git 歷史裡出現過嗎?

    這是判斷「漂移方向」的唯一可靠依據 —— 光比 hash 只知道兩邊不同,
    不知道誰新誰舊,而方向錯了結論就完全相反:
      · 線上內容曾經是某個 commit  → 線上落後,部署會把它追上去 = 正常
      · 線上內容 git 從沒見過      → 線上有沒進版控的改動,部署會抹掉它 = 必須擋
    後者就是 screen_post.py(六態分類/背離救回)差點被回捲的情況。
    """
    log = subprocess.run(["git", "log", "--all", "--format=%H", "--", rel],
                         cwd=REPO, capture_output=True, text=True)
    for commit in log.stdout.split():
        blob = subprocess.run(["git", "show", f"{commit}:{rel}"],
                              cwd=REPO, capture_output=True)
        if blob.returncode == 0 and hashlib.md5(blob.stdout).hexdigest() == digest:
            return True
    return False


def check(adopt: bool) -> int:
    only_remote: list[tuple[str, str, str]] = []   # 線上獨有 → 部署會刪掉它
    ahead: list[tuple[str, str, str]] = []         # 線上有 git 沒見過的內容 → 會被抹掉
    behind: list[str] = []                         # 線上落後 → 部署追上去(正常)
    only_local: list[str] = []                     # 本機獨有 → 部署會新增(正常)

    for local_root, remote_dir, prune in ROOTS:
        base = REPO / local_root if local_root != "." else REPO
        remote = remote_hashes(remote_dir, prune)
        for rel, digest in sorted(remote.items()):
            target = base / rel
            if not target.exists():
                only_remote.append((f"{local_root}/{rel}".lstrip("./"),
                                    f"{remote_dir}/{rel}", digest))
            elif local_hash(target) != digest:
                repo_rel = f"{local_root}/{rel}".lstrip("./")
                if seen_in_git(repo_rel, digest):
                    behind.append(repo_rel)
                else:
                    ahead.append((repo_rel, f"{remote_dir}/{rel}", digest))
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = str(path.relative_to(base))
            if skip(rel) or any(rel.startswith(f"{d}/") for d in prune):
                continue
            if rel not in remote:
                only_local.append(f"{local_root}/{rel}".lstrip("./"))

    if only_local:
        print(f"ℹ️  本機獨有 {len(only_local)} 檔(部署會新增,正常):")
        for rel in sorted(only_local)[:10]:
            print(f"     + {rel}")
        if len(only_local) > 10:
            print(f"     … 另外 {len(only_local) - 10} 檔")
        print()

    if behind:
        print(f"ℹ️  線上落後 {len(behind)} 檔(內容是 git 歷史裡的舊版,部署會追上去):")
        for rel in sorted(behind):
            print(f"     ↑ {rel}")
        print()

    if not only_remote and not ahead:
        print("✅ 可以部署:線上沒有任何 git 未收錄的東西,不會回捲、不會誤刪。")
        return 0

    if only_remote:
        print(f"🔴 線上獨有 {len(only_remote)} 檔 —— rsync --delete 會直接刪掉:")
        for rel, remote_path, _ in only_remote:
            print(f"     {rel}   (線上: {remote_path})")
        print()
    if ahead:
        print(f"🔴 線上有 git 從沒見過的內容 {len(ahead)} 檔 —— 部署會靜默抹掉:")
        for rel, remote_path, _ in ahead:
            print(f"     {rel}   (線上: {remote_path})")
        print()

    if not adopt:
        print("這些是「只存在於線上」的成果,推下去就沒了。擇一處理:")
        print("  · 線上版要保留 → python3 ops/deploy_guard.py --adopt 抓回本機,檢查後 commit")
        print("  · 確定要退場   → git rm 明確刪除,不要靠 rsync --delete 順手刪")
        return 1

    print("--adopt:從線上抓回以下檔案(不會自動 commit,請自行檢查 git diff)")
    for rel, remote_path, _ in only_remote + ahead:
        target = REPO / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["scp", "-q", f"{SSH_HOST}:{remote_path}", str(target)],
                       check=True)
        print(f"     ↓ {rel}")
    print("\n抓回完成。請 `git status` / `git diff` 檢查後再 commit。")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="部署前漂移檢查")
    ap.add_argument("--adopt", action="store_true",
                    help="把線上獨有/內容不同的檔案抓回本機(不自動 commit)")
    return check(ap.parse_args().adopt)


if __name__ == "__main__":
    raise SystemExit(main())
