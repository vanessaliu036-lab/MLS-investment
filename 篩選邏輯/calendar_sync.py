"""
calendar_sync.py — 交易日行事曆同步(唯一的假日資料來源)

痛點:phase.py 原本只排除週末,國定假日照樣被判成「盤中」。
這支從 TWSE 官方假日行事曆抓「真正休市日」寫進 holidays.json,
phase.py 讀它來判斷 is_trading_day。週末永遠休市(不需列)。

TWSE 清單同時含兩類:
  休市日      Description 有「放假」/「補假」,或 Name 有「無交易」→ 收進來
  交易日註記  「開始交易日」「最後交易日」→ 那天有開盤,不能當休市

由排程週期性呼叫(例:每週一次),不在服務啟動時打 API —— 尊重「啟動零重抓」。
官方端點一次只回當年度,跨年靠 merge 保留舊年份。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
OUT = Path(__file__).parent / "holidays.json"


def _roc_to_iso(roc: str) -> str:
    """民國日期 1150212 → 2026-02-12。"""
    y = int(roc[:3]) + 1911
    return f"{y}-{roc[3:5]}-{roc[5:7]}"


def fetch_closed() -> list[str]:
    """回傳 TWSE 官方本年度『休市日』ISO 清單。"""
    raw = urllib.request.urlopen(URL, timeout=20).read()
    data = json.loads(raw)
    closed = []
    for r in data:
        name = r.get("Name", "")
        desc = r.get("Description", "")
        is_closed = ("放假" in desc) or ("補假" in desc) or ("無交易" in name) or ("休市" in name)
        # 交易日註記排除(那天其實有開盤)
        is_trading_note = ("開始交易" in name) or ("最後交易" in name)
        if is_closed and not is_trading_note:
            closed.append(_roc_to_iso(r["Date"]))
    return sorted(set(closed))


def sync() -> list[str]:
    """抓取並與既有 holidays.json 合併(保留其他年份),寫回檔案。"""
    closed = fetch_closed()
    existing: list[str] = []
    if OUT.exists():
        try:
            existing = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            existing = []
    merged = sorted(set(existing) | set(closed))
    OUT.write_text(json.dumps(merged, ensure_ascii=False, indent=0), encoding="utf-8")
    return merged


if __name__ == "__main__":
    m = sync()
    print(f"holidays.json 已更新,共 {len(m)} 個休市日(不含週末)")
    for d in m:
        print(" ", d)
