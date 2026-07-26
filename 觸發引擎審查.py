"""
觸發引擎審查 - 一鍵執行版
對應你系統既有的 engine_review.py / /api/roles/review /api/roles/apply
（這不是新邏輯，是呼叫你 VPS 上已經寫好的功能，讓「模型紀錄」頁面現在就有資料，
  不用等到週五盤後自動跑。）

使用方式：
1. 先安裝 requests（如果還沒裝過）：pip install requests
2. 直接執行：python 觸發引擎審查.py
3. 程式會：
   - 呼叫你系統的 GET /api/roles/review?run=1，立即跑一次引擎角色審查
   - 印出這次審查的建議（升轉 / 降轉 / 全部維持）
   - 詢問你要不要現在套用建議；輸入 y 才會真的呼叫 POST /api/roles/apply 改寫名單
   - 什麼都不輸入或輸入 n，就只是看結果，不會改動你系統任何東西

跑完之後回去網頁重新整理「模型紀錄」頁面，就會看到剛剛這次的審查結果。
"""

import requests

BASE_URL = "http://66.42.42.150:8000"


def trigger_review():
    print("正在觸發引擎審查...\n")
    resp = requests.get(f"{BASE_URL}/api/roles/review", params={"run": 1}, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    print(f"審查日期：{data.get('date')}")
    print(f"目前引擎名單：{data.get('engines_now', [])}")
    print(f"自動套用模式：{'開啟' if data.get('auto_apply') else '關閉（需手動確認）'}\n")

    print(data.get("summary", ""))
    return data


def apply_if_confirmed():
    ans = input("\n要現在套用這次的建議嗎？(y/N)：").strip().lower()
    if ans != "y":
        print("已略過套用，系統名單維持不變。")
        return

    resp = requests.post(f"{BASE_URL}/api/roles/apply", timeout=30)
    resp.raise_for_status()
    result = resp.json()

    if result.get("ok"):
        print(f"\n套用完成，變更了 {result.get('changed', 0)} 項。")
        print(f"最新引擎名單：{result.get('engines')}")
    else:
        print(f"\n套用失敗：{result.get('error')}")


def main():
    data = trigger_review()
    suggestions = data.get("suggestions") or data.get("rows", [])
    has_change = any(r.get("suggest") not in (None, "keep") for r in suggestions)

    if has_change:
        apply_if_confirmed()
    else:
        print("\n本次審查沒有升轉/降轉建議，全部維持現狀，不需要套用。")


if __name__ == "__main__":
    main()
