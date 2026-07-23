"""
盤後驗證系統 - 一鍵執行版
用途：比對「盤中決策」與「實際走勢」，累積命中率資料，找出哪些規則準、哪些不準。
這是讓 AI 判斷邏輯從「顯示分類」進化到「接近預測」的資料基礎。

使用方式：
1. 準備一個 CSV 檔（例如 今日決策.csv），欄位需要有：
   股票代號,股票名稱,分類,子分類,aflow,訊號價,訊號時間
   （這些是從 MLS 盤中決策中心複製當天的訊號清單）

2. 收盤後，在同一個 CSV 裡補上兩個欄位：
   收盤價,當日最高,當日最低
   （沒有的欄位留空也沒關係，程式會自動跳過無法判定的項目）

3. 在終端機執行（需要先安裝 pandas：pip install pandas）：
   python 盤後驗證.py 今日決策.csv

4. 程式會自動：
   - 判定每一筆訊號是「命中」還是「未命中」
   - 把結果累加進 validation_log.csv（歷史資料庫，會越存越多，不會覆蓋舊資料）
   - 印出各分類的累積命中率統計

5. 每天重複這個流程。累積 2-4 週後，你就能看出哪個分類/門檻準、哪個常誤判，
   再回頭調整下面的 RULES 數字或 MLS 系統的分類邏輯。
"""

import sys
import os
import pandas as pd
from datetime import datetime

LOG_FILE = "validation_log.csv"

# ------------------ 判定規則（想調整靈敏度只改這裡的數字就好） ------------------
RULES = {
    "可進場":   {"type": "up",       "threshold": 0.015},   # 收盤漲幅 >= 1.5% 視為命中
    "等待突破": {"type": "breakout", "threshold": 0.0},     # 收盤價 > 訊號價 視為命中
    "風險警報": {"type": "down",     "threshold": -0.015},  # 收盤跌幅 <= -1.5% 視為命中
}
# --------------------------------------------------------------------------------


def judge(row):
    category = str(row.get("分類", "")).strip()
    rule = RULES.get(category)
    if rule is None:
        return "無規則"

    try:
        signal_price = float(row["訊號價"])
        close_price = float(row["收盤價"])
    except (KeyError, ValueError, TypeError):
        return "資料不足"

    change = (close_price - signal_price) / signal_price

    if rule["type"] == "up":
        return "命中" if change >= rule["threshold"] else "未命中"
    elif rule["type"] == "down":
        return "命中" if change <= rule["threshold"] else "未命中"
    elif rule["type"] == "breakout":
        return "命中" if close_price > signal_price else "未命中"
    return "無規則"


def main():
    if len(sys.argv) < 2:
        print("用法：python 盤後驗證.py 今日決策.csv")
        return

    input_file = sys.argv[1]
    if not os.path.exists(input_file):
        print(f"找不到檔案：{input_file}")
        return

    df = pd.read_csv(input_file)
    df["驗證日期"] = datetime.now().strftime("%Y-%m-%d")
    df["驗證結果"] = df.apply(judge, axis=1)

    # 累加進歷史資料庫（不覆蓋舊資料）
    if os.path.exists(LOG_FILE):
        history = pd.read_csv(LOG_FILE)
        combined = pd.concat([history, df], ignore_index=True)
    else:
        combined = df
    combined.to_csv(LOG_FILE, index=False, encoding="utf-8-sig")

    print(f"\n今日驗證完成，共 {len(df)} 筆，已累加進 {LOG_FILE}\n")

    # 用全部歷史資料統計命中率，天數越多統計越準
    valid = combined[combined["驗證結果"].isin(["命中", "未命中"])]
    if valid.empty:
        print("目前沒有足夠資料可以統計命中率。")
        return

    summary = valid.groupby("分類")["驗證結果"].apply(
        lambda x: (x == "命中").sum() / len(x) * 100
    ).round(1)

    print("===== 累積命中率統計（依分類） =====")
    for category, rate in summary.items():
        count = len(valid[valid["分類"] == category])
        print(f"{category}：{rate}%（共 {count} 筆）")


if __name__ == "__main__":
    main()
