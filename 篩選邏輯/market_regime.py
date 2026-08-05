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
from datetime import datetime
from zoneinfo import ZoneInfo

_TW_TZ = ZoneInfo("Asia/Taipei")


def _today_tw():
    return datetime.now(_TW_TZ).date().isoformat()


def _roc_to_iso(d):
    """TWSE/TPEx 的 ROC 日期字串（如 1150730）→ 西元 ISO（2026-07-30）。"""
    s = str(d or "").strip()
    if len(s) >= 7 and s.isdigit():
        return f"{int(s[:3]) + 1911:04d}-{s[3:5]}-{s[5:7]}"
    return None

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
    data_date = None
    for r in rows:
        if data_date is None:
            data_date = _roc_to_iso(r.get("Date"))
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
    return up, down, flat, data_date


def fetch_breadth(force=False):
    """全市場（上市+上櫃）真實寬度。失敗回 None（規則3：不用 aflow 頂替）。

    註記資料日：TWSE STOCK_DAY_ALL / TPEx 收盤檔只在收盤後發布，盤中拿到的是
    「前一交易日」的收盤數。回傳 data_date（西元）與 is_stale（資料日≠今天），
    讓上層盤中不要把昨收寬度當今天用。"""
    now = time.time()
    if not force and _CACHE["data"] and (now - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["data"]
    up = down = flat = 0
    ok = False
    data_date = None
    try:
        u, d, f, dd = _count(_get(_SRC["TWSE"]), "Change")
        up += u; down += d; flat += f; data_date = data_date or dd
        ok = True
    except Exception as e:
        print(f"[market_regime] TWSE 取數失敗: {type(e).__name__}: {e}", flush=True)
    try:
        u, d, f, dd = _count(_get(_SRC["TPEX"]), "Change")
        up += u; down += d; flat += f; data_date = data_date or dd
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
        "data_date": data_date,
        "is_stale": bool(data_date) and data_date != _today_tw(),
        "source": "eod_official",
    }
    _CACHE.update({"ts": now, "data": row})
    return row


def breadth_from_snapshots(snaps):
    """B：盤中即時全市場寬度。用即時報價逐檔數漲跌，資料日＝今天。

    snaps: 可迭代，每筆是 dict 或物件，需含漲跌幅欄位；優先讀
      change_rate / pct_chg / change_price / change，>0 漲、<0 跌、==0 平盤。
    回傳與 fetch_breadth 相同介面（is_stale=False、source='intraday_live'）。
    無有效樣本回 None，讓上層退回 EOD。"""
    def _chg(s):
        get = s.get if isinstance(s, dict) else (lambda k, d=None: getattr(s, k, d))
        for k in ("change_rate", "pct_chg", "change_price", "change"):
            v = get(k)
            if v is None:
                continue
            try:
                return float(str(v).replace(",", "").replace("+", ""))
            except (ValueError, TypeError):
                continue
        return None

    up = down = flat = 0
    for s in snaps or []:
        c = _chg(s)
        if c is None:
            continue
        if c > 0:
            up += 1
        elif c < 0:
            down += 1
        else:
            flat += 1
    total = up + down + flat
    if total == 0:
        return None
    return {
        "advancing": up, "declining": down, "unchanged": flat, "total": total,
        "true_breadth": up / total,
        "fetched_at": time.time(),
        "data_date": _today_tw(),
        "is_stale": False,
        "source": "intraday_live",
    }


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
    stale = bool((breadth_row or {}).get("is_stale"))
    breadth_date = (breadth_row or {}).get("data_date")
    # 盤中拿到的若是「前一交易日」的收盤寬度(is_stale)，一律不得驅動 Risk 判定：
    # 昨天的下跌日不能把今天定調成系統性下殺、封殺今天的個股訊號。
    # 該寬度僅留作診斷顯示，regime 只吃「今天」的指數/寬度。
    breadth_live = None if stale else breadth

    diag = {
        "index_change_pct": index_pct,
        "true_breadth": breadth,
        "advancing": up, "declining": down,
        "total": (breadth_row or {}).get("total"),
        "breadth_date": breadth_date,
        "breadth_stale": stale,
        "aflow_pos_ratio": aflow_ratio,
        "aflow_note": "aflow 正值占比僅供診斷，不是市場寬度，不參與 Risk On/OFF 判定",
    }

    # 盤中缺當日寬度（EOD 尚未發布、只有昨收）且無當日指數：不放行，但也不封殺。
    # 退為 PENDING 中性——不像 SYSTEMIC 那樣宣告「個股訊號一律無效」。
    if index_pct is None and breadth_live is None:
        if stale:
            return {"regime": "PENDING", "risk": "NEUTRAL", "allow": True,
                    "title": "待當日寬度", "advice": "當日全市場寬度尚未發布，依個股與盤中資金判讀",
                    "banner": (f"全市場真實寬度為 {breadth_date or '昨收'} 收盤值（今日盤中尚未產生）"
                               "— 已不以昨收定調今天，個股訊號照常，收盤後再校準大盤。"),
                    "reasons": ["當日寬度待收盤"], **diag}
        return {"regime": "NO_DATA", "risk": "OFF", "allow": False,
                "title": "無法判讀", "advice": "無大盤基準，不建議加碼",
                "banner": "無大盤基準（指數/漲跌家數皆缺）— 不以 aflow 頂替。請確認 TWSE 取數。",
                "reasons": ["缺市場層級資料"], **diag}

    reasons, systemic = [], False
    if index_pct is not None and index_pct <= INDEX_CRASH:
        systemic = True
        reasons.append(f"指數 {index_pct:+.2f}%")
    if breadth_live is not None and breadth_live <= BREADTH_CRASH:
        systemic = True
        reasons.append(f"真實寬度 {breadth_live:.0%}（漲{up}/跌{down}）")

    if systemic:
        out = {"regime": "SYSTEMIC", "risk": "OFF", "allow": False,
               "title": "Risk Off", "advice": "不加碼，持股降至最低，等企穩",
               "banner": "大盤系統性下跌 — 個股訊號一律無效，等企穩。此時 aflow 正值不是承接。"}
    elif (index_pct is not None and index_pct <= INDEX_WEAK) or \
         (breadth_live is not None and breadth_live <= BREADTH_WEAK):
        out = {"regime": "WEAK", "risk": "OFF", "allow": False,
               "title": "偏空", "advice": "維持現有持股，不加碼",
               "banner": "大盤走弱 — 個股訊號降級為觀察，不作進場依據"}
        reasons.append("大盤走弱")
    elif breadth_live is not None and breadth_live < BREADTH_HEALTHY:
        out = {"regime": "NEUTRAL", "risk": "NEUTRAL", "allow": True,
               "title": "中性", "advice": "寬度不足，採防守，不積極加碼",
               "banner": (f"指數尚可但真實寬度僅 {breadth_live:.0%} — 漲勢集中在少數權值，慎防窄幅假行情")}
        reasons.append("寬度不足")
    elif breadth_live is None and index_pct is not None:
        # 有當日指數、但當日寬度尚未發布(昨收 stale)：依指數判、寬度待收盤校準
        out = {"regime": "PENDING", "risk": "NEUTRAL", "allow": True,
               "title": "待當日寬度", "advice": "依指數與個股操作，全市場寬度收盤後校準",
               "banner": (f"當日全市場寬度尚未發布（{breadth_date or '昨收'} 為最近收盤值）"
                          "— 暫依當日指數判讀，不以昨收定調今天")}
        reasons.append("當日寬度待收盤")
    else:
        out = {"regime": "NORMAL", "risk": "ON", "allow": True,
               "title": "Risk On", "advice": "可依個股條件操作", "banner": ""}

    out["reasons"] = reasons
    out.update(diag)
    return out
