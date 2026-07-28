"""
market_regime.py — Layer 0 市場環境（Risk On / Neutral / Off）

架構定案 Layer 0。核心鐵律（承 regime.py）：
  1. 市場寬度只有一個合法定義 = 上漲家數 / 總家數。aflow 正值占比不是寬度。
  2. Risk On/Off 由「指數 + 真實寬度」決定，永不由 aflow。
  3. 沒有大盤基準時一律不放行，不用 aflow 頂替。

資料源修正（2026-07-28）：原 regime/market.py 掃 TWSE MI_INDEX 抓漲跌家數，
  但現行 TWSE OpenAPI 的 MI_INDEX 只有指數、無家數。改用 STOCK_DAY_ALL（全上市）
  + TPEx mainboard（全上櫃）逐檔用 Change 數漲跌，自己算真實寬度。官方免費無上限。
"""

from __future__ import annotations

import json
import time
import urllib.request

# ---- 門檻（承 regime.py，可調） ----
INDEX_CRASH = -2.0      # 指數跌幅 → 系統性下跌
INDEX_WEAK = -1.0
BREADTH_CRASH = 0.25    # 真實寬度低於此 → 全面性下跌
BREADTH_WEAK = 0.40
BREADTH_HEALTHY = 0.55  # 健康行情下限

_SRC = {
    "TWSE": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    "TPEX": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes",
}
_TIMEOUT = 15
_CACHE = {"ts": 0.0, "data": None}
_CACHE_TTL = 180  # 秒。官方無上限，但沒必要每次請求都拉全市場。


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "MLS/4.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _count(rows, key):
    up = down = flat = 0
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        try:
            ch = float(str(v).replace(",", "").replace("+", ""))
        except (ValueError, TypeError):
            continue
        if ch > 0:
            up += 1
        elif ch < 0:
            down += 1
        else:
            flat += 1
    return up, down, flat


def fetch_breadth(force=False):
    """全市場（上市+上櫃）真實寬度。失敗回 None（規則3：不用 aflow 頂替）。"""
    now = time.time()
    if not force and _CACHE["data"] and (now - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["data"]
    up = down = flat = 0
    ok = False
    try:
        u, d, f = _count(_get(_SRC["TWSE"]), "Change")
        up += u; down += d; flat += f
        ok = True
    except Exception as e:
        print(f"[market_regime] TWSE 取數失敗: {type(e).__name__}: {e}", flush=True)
    try:
        u, d, f = _count(_get(_SRC["TPEX"]), "Change")
        up += u; down += d; flat += f
        ok = True
    except Exception as e:
        print(f"[market_regime] TPEx 取數失敗: {type(e).__name__}: {e}", flush=True)
    if not ok:
        return None
    total = up + down + flat
    row = {
        "advancing": up, "declining": down, "unchanged": flat, "total": total,
        "true_breadth": (up / total) if total else None,
        "fetched_at": now,
    }
    _CACHE.update({"ts": now, "data": row})
    return row


def assess(breadth_row=None, index_pct=None, aflow_ratio=None):
    """
    判定市場體制。承 regime.py。
      breadth_row: fetch_breadth() 結果（真實寬度、漲跌家數）
      index_pct:   加權指數漲跌幅（%）
      aflow_ratio: 51 池 aflow 正值占比（0~1），僅供診斷，不參與 Risk 判定
    回傳含 regime/risk/banner/position_advice/true_breadth 等。
    """
    if breadth_row is None:
        breadth_row = fetch_breadth()
    breadth = (breadth_row or {}).get("true_breadth")
    up = (breadth_row or {}).get("advancing")
    down = (breadth_row or {}).get("declining")

    diag = {
        "index_change_pct": index_pct,
        "true_breadth": breadth,
        "advancing": up, "declining": down,
        "total": (breadth_row or {}).get("total"),
        "aflow_pos_ratio": aflow_ratio,
        "aflow_note": "aflow 正值占比僅供診斷，不是市場寬度，不參與 Risk On/OFF 判定",
    }

    # 規則3：沒有大盤基準（指數與寬度皆缺）→ 不放行，不用 aflow 頂替
    if index_pct is None and breadth is None:
        return {"regime": "NO_DATA", "risk": "OFF", "allow": False,
                "title": "無法判讀", "advice": "無大盤基準，不建議加碼",
                "banner": "無大盤基準（指數/漲跌家數皆缺）— 不以 aflow 頂替。請確認 TWSE 取數。",
                "reasons": ["缺市場層級資料"], **diag}

    reasons, systemic = [], False
    if index_pct is not None and index_pct <= INDEX_CRASH:
        systemic = True
        reasons.append(f"指數 {index_pct:+.2f}%")
    if breadth is not None and breadth <= BREADTH_CRASH:
        systemic = True
        reasons.append(f"真實寬度 {breadth:.0%}（漲{up}/跌{down}）")

    if systemic:
        out = {"regime": "SYSTEMIC", "risk": "OFF", "allow": False,
               "title": "Risk Off", "advice": "不加碼，持股降至最低，等企穩",
               "banner": "大盤系統性下跌 — 個股訊號一律無效，等企穩。此時 aflow 正值不是承接。"}
    elif (index_pct is not None and index_pct <= INDEX_WEAK) or \
         (breadth is not None and breadth <= BREADTH_WEAK):
        out = {"regime": "WEAK", "risk": "OFF", "allow": False,
               "title": "偏空", "advice": "維持現有持股，不加碼",
               "banner": "大盤走弱 — 個股訊號降級為觀察，不作進場依據"}
        reasons.append("大盤走弱")
    elif breadth is not None and breadth < BREADTH_HEALTHY:
        out = {"regime": "NEUTRAL", "risk": "NEUTRAL", "allow": True,
               "title": "中性", "advice": "寬度不足，採防守，不積極加碼",
               "banner": (f"指數尚可但真實寬度僅 {breadth:.0%} — 漲勢集中在少數權值，慎防窄幅假行情")}
        reasons.append("寬度不足")
    else:
        out = {"regime": "NORMAL", "risk": "ON", "allow": True,
               "title": "Risk On", "advice": "可依個股條件操作", "banner": ""}

    out["reasons"] = reasons
    out.update(diag)
    return out
