# -*- coding: utf-8 -*-
"""
twse_fetch.py — TWSE 官方盤後資料抓取（三大法人 / 融資融券）

為什麼你以前抓不到 TWSE —— 這裡把常見雷解掉：
  1. User-Agent 被擋：TWSE 舊 CSV endpoint 擋無 UA 請求，回 HTML 或空字串。
     → 一律帶瀏覽器 UA。
  2. endpoint 用錯：舊 www.twse.com.tw/fund/T86 CSV 格式常變、會擋爬蟲；
     新 openapi.twse.com.tw/v1/ 是 keyless JSON gateway，穩定得多。→ 本模組用新的。
  3. 日期格式：舊 endpoint 要 yyyyMMdd 或民國年，搞混就回空。
     openapi gateway 多為「當日全市場」快照，不需帶日期，收盤後更新。
  4. 收盤前抓 = 空：法人資料約 15:00 後才出，太早抓是空的，非程式錯。

鐵律：這是盤後查詢型資料，只在收盤後（建議 15:30）抓一次，不可盤中輪詢。
本環境無外網，故 fetch 實作在 VPS 執行；此處給正確 URL/UA/解析與降級。

準度備註（小字）：
  - openapi gateway 為「當日全市場」快照，個股需自行 filter code。
  - 連買天數需自行累計歷史（單日資料不含），見 accumulate_streak()。
"""

import json
import urllib.request
from typing import Dict, List, Optional

# 新版 keyless OpenAPI gateway（穩定，優先用）
TWSE_OPENAPI = "https://openapi.twse.com.tw/v1"
# 三大法人買賣超（個股，當日全市場）
EP_T86 = f"{TWSE_OPENAPI}/fund/T86"
# 融資融券（個股，當日全市場）
EP_MARGIN = f"{TWSE_OPENAPI}/exchangeReport/MI_MARGN"

# 必帶：不帶 UA 會被擋
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "application/json",
}


def _get_json(url: str, timeout: int = 15) -> Optional[list]:
    """
    抓 JSON。任何失敗（網路/格式/空）回 None，呼叫端降級標 NO_DATA、不補造。
    這是「抓不到不要讓整個流程崩」的關鍵。
    """
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, list) else None
    except Exception:
        return None


def fetch_institutional(universe: Optional[List[str]] = None) -> Dict[str, dict]:
    """
    抓當日三大法人買賣超，回 {code: {foreign, trust, dealer, total}}（單位：股）。
    universe 給定則只留這些代碼。抓不到回 {}（呼叫端標 NO_DATA）。

    T86 欄位（openapi gateway，中文鍵）：
      證券代號 / 外陸資買賣超股數(不含外資自營商) / 投信買賣超股數 /
      自營商買賣超股數 / 三大法人買賣超股數
    欄位名 TWSE 偶有微調，取值用容錯 _pick()。
    """
    rows = _get_json(EP_T86)
    if not rows:
        return {}

    def _pick(row, *keys):
        for k in keys:
            if k in row:
                try:
                    return int(str(row[k]).replace(",", "").strip() or 0)
                except ValueError:
                    return 0
        return 0

    out: Dict[str, dict] = {}
    uni = set(universe) if universe else None
    for row in rows:
        code = str(row.get("證券代號") or row.get("Code") or "").strip()
        if not code or (uni and code not in uni):
            continue
        foreign = _pick(row, "外陸資買賣超股數(不含外資自營商)", "外資買賣超股數")
        trust = _pick(row, "投信買賣超股數")
        dealer = _pick(row, "自營商買賣超股數", "自營商買賣超股數(自行買賣)")
        total = _pick(row, "三大法人買賣超股數")
        out[code] = {"foreign": foreign, "trust": trust,
                     "dealer": dealer, "total": total or foreign + trust + dealer}
    return out


def fetch_margin(universe: Optional[List[str]] = None) -> Dict[str, dict]:
    """
    抓當日融資融券，回 {code: {margin_change, short_change}}（單位：張）。
    鐵律對齊：融資減=好（散戶洗出）、融資增=不利。
    抓不到回 {}。
    """
    rows = _get_json(EP_MARGIN)
    if not rows:
        return {}

    def _num(v):
        try:
            return int(str(v).replace(",", "").strip() or 0)
        except ValueError:
            return 0

    out: Dict[str, dict] = {}
    uni = set(universe) if universe else None
    for row in rows:
        code = str(row.get("股票代號") or row.get("Code") or "").strip()
        if not code or (uni and code not in uni):
            continue
        # 融資今日餘額 - 前日餘額 = 增減；欄名依 gateway 實際微調
        margin_today = _num(row.get("融資今日餘額"))
        margin_prev = _num(row.get("融資前日餘額"))
        short_today = _num(row.get("融券今日餘額"))
        short_prev = _num(row.get("融券前日餘額"))
        out[code] = {
            "margin_change": margin_today - margin_prev,
            "short_change": short_today - short_prev,
        }
    return out


def accumulate_streak(today_net: int, prev_streak: int) -> int:
    """
    連買天數累計（單日資料不含，需自己累）。
    今日淨買>0 → 延續或起算正連買；<0 → 轉負；=0 → 中斷歸零。
    prev_streak 從盤後歷史表讀昨天的值。
    """
    if today_net > 0:
        return prev_streak + 1 if prev_streak >= 0 else 1
    if today_net < 0:
        return prev_streak - 1 if prev_streak <= 0 else -1
    return 0
