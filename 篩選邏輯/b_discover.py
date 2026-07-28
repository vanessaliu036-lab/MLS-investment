"""
b_discover.py — B 鏈:13:20 最終掃描(四判準)

⚠ B 鏈,與 A 鏈完全獨立。
   不讀 A 鏈候選池、不寫 A 鏈任何表、不出燈號、不進盤中畫面。
   這支爆掉,A 鏈盤中燈號照常。

任務:從 51 檔全集找出「今天盤中自己冒出來」的標的,標記起來,
      交給盤後法人驗證。產出的不是進場訊號,是明日候選池的新血。

===== 為什麼是 13:20,不是早盤 =====

尾盤前 10 分鐘,主力當日的意圖已經表態,但還沒進入最後撮合的雜訊。
早盤的異動一半是當沖,尾盤還在的才是真的要貨。

時間軸:
    09:00–09:15  只存快照,不做任何判斷(鐵律1:開盤負值不算數)
    09:15–13:00  持續累積時序,不出結論
    13:20        執行本掃描,產出標記名單
    13:31 後     交給 b_verify 做法人驗證

===== 為什麼是這四項,不是量增/aflow轉正那種 =====

量增 2 倍、aflow 轉正,當沖客一擁而上也長這樣。
單點的量級分不出主力和散戶。真正能分辨的是「行為的形狀」:

  1. 持續性        主力吸籌是連續的,當沖是尖刺的
  2. 下殺承接      上漲誰都能推,下殺敢接的才是要貨的人(6/17 的形狀)
  3. 相對族群強度  分辨「大盤帶的」和「被特定資金選中的」
  4. 量增但價穩    吸籌 vs 追高的分水嶺

四項中兩項成立才標記。每一項都在測行為模式,不是測量級。
"""

from __future__ import annotations

import datetime as _dt
import json

import b_snapshot
import store
from envelope import run_all, persist_status, missing_labels
from phase import Phase, today_tw

PLUGIN = "b_discover"
TABLE = "b_discovery"

SCAN_HOUR, SCAN_MIN = 13, 20
HITS_REQUIRED = 2          # 四項中兩項成立才標記
BLIND_MIN = 15             # 鐵律1:開盤 15 分鐘的資料不納入判斷

# 判準門檻
PERSIST_RATIO = 0.70       # 持續性:aflow 正值區間占比
SINGLE_SLOT_CAP = 0.40     # 單一區間佔全天量上限(超過 = 單筆大單,不是吸籌)
DIP_MIN = 1.5              # 下殺承接:回落幅度下限 %
DIP_VOL_SHRINK = 0.60      # 回落期間量能萎縮到高點時的比例以下
DIP_RECOVER = 0.50         # 收復回落幅度的比例
REL_STRENGTH = 1.5         # 相對族群強度 %
VOL_SURGE = 1.8            # 量增倍數
STABLE_LO, STABLE_HI = 1.0, 5.0   # 價穩區間 %


def _usable(series: list[dict]) -> list[dict]:
    """濾掉開盤 15 分鐘(鐵律1)。09:00/09:05/09:10 一律不納入判斷。"""
    return [s for s in series
            if int(s["slot"][:2]) * 60 + int(s["slot"][2:]) >= 9 * 60 + BLIND_MIN]


# ---------------------------------------------------------------- 判準1:持續性

def c1_persistence(series: list[dict]) -> tuple[bool | None, str]:
    """
    主力吸籌是連續的,當沖是尖刺的。
    看 aflow 在多個區間裡「持續為正」的比例,不看某一刻的絕對值。
    """
    s = _usable(series)
    vals = [x["net_active"] for x in s if x["net_active"] is not None]
    if len(vals) < 6:
        return None, "時序不足"

    pos = sum(1 for v in vals if v > 0)
    ratio = pos / len(vals)
    if ratio < PERSIST_RATIO:
        return False, f"aflow 正值占比 {ratio:.0%} < {PERSIST_RATIO:.0%}"

    # 排除單筆大單撐起來的假象
    vols = [x["volume"] for x in s if x["volume"] is not None]
    if len(vols) >= 2:
        deltas = [max(0, vols[i] - vols[i - 1]) for i in range(1, len(vols))]
        total = sum(deltas)
        if total > 0 and max(deltas) / total > SINGLE_SLOT_CAP:
            return False, f"單一區間佔全天量 {max(deltas)/total:.0%},疑似單筆大單"

    return True, f"aflow 持續為正 {ratio:.0%},分布均勻"


# ---------------------------------------------------------------- 判準2:下殺承接

def c2_dip_absorption(series: list[dict]) -> tuple[bool | None, str]:
    """
    真吸籌會在下殺時顯形:價格回落但量縮、不破前低、隨後 V 轉。
    這比上漲時的量能有意義得多 —— 上漲誰都能推,下殺敢接的才是要貨的人。
    6/17 被動元件就是這個形狀。
    """
    s = _usable(series)
    pts = [(x["slot"], x["price"], x["volume"]) for x in s
           if x["price"] is not None]
    if len(pts) < 8:
        return None, "時序不足"

    prices = [p for _, p, _ in pts]
    peak_i = max(range(len(prices)), key=lambda i: prices[i])
    if peak_i >= len(prices) - 3:
        return False, "高點在尾段,尚未出現回落與收復"

    peak = prices[peak_i]
    after = prices[peak_i:]
    trough_i = peak_i + min(range(len(after)), key=lambda i: after[i])
    trough = prices[trough_i]

    dip_pct = (peak - trough) / peak * 100
    if dip_pct < DIP_MIN:
        return False, f"回落僅 {dip_pct:.1f}%,未達 {DIP_MIN}%"

    day_low = min(prices)
    if trough <= day_low * 1.0005:
        return False, "回落破當日低點,不是承接"

    # 回落期間量能是否萎縮
    vols = [v for _, _, v in pts if v is not None]
    if len(vols) == len(pts) and trough_i > peak_i:
        rise_v = [max(0, vols[i] - vols[i - 1]) for i in range(1, peak_i + 1)]
        dip_v = [max(0, vols[i] - vols[i - 1]) for i in range(peak_i + 1, trough_i + 1)]
        if rise_v and dip_v:
            ra = sum(rise_v) / len(rise_v)
            da = sum(dip_v) / len(dip_v)
            if ra > 0 and da / ra > DIP_VOL_SHRINK:
                return False, f"回落期間量能未萎縮({da/ra:.0%}),像是真賣壓"

    last = prices[-1]
    recover = (last - trough) / (peak - trough) if peak > trough else 0
    if recover < DIP_RECOVER:
        return False, f"回落後僅收復 {recover:.0%},未達 {DIP_RECOVER:.0%}"

    return True, f"回落 {dip_pct:.1f}% 量縮不破低,收復 {recover:.0%}"


# ---------------------------------------------------------------- 判準3:相對族群強度

def c3_relative_strength(code: str, series: list[dict],
                         group_avg: dict[str, float],
                         code_group: dict[str, str]) -> tuple[bool | None, str]:
    """
    整個族群都漲 = 輪動;族群平平但這檔獨強 = 有人特定在買它。
    """
    s = _usable(series)
    if not s or s[-1]["change_rate"] is None:
        return None, "無漲跌幅"
    g = code_group.get(code)
    if g is None or g not in group_avg:
        return None, "無族群資料"

    cr = s[-1]["change_rate"]
    diff = cr - group_avg[g]
    if diff < REL_STRENGTH:
        return False, f"僅強於族群 {diff:+.1f}%"
    return True, f"強於族群均值 {diff:+.1f}%(族群 {group_avg[g]:+.1f}%)"


# ---------------------------------------------------------------- 判準4:量增但價穩

def c4_volume_stable(series: list[dict], y_volume: int | None,
                     at: _dt.datetime) -> tuple[bool | None, str]:
    """
    量放大而價格橫住 = 有人承接賣壓;量放大且價格直衝 = 追價,隔天容易被倒。
    所以不是越漲越好,漲太兇反而不算。
    """
    s = _usable(series)
    if not s or s[-1]["volume"] is None or not y_volume:
        return None, "無量能基準"

    mins = (at.hour - 9) * 60 + at.minute
    frac = min(1.0, max(0.05, mins / 270))
    pace = s[-1]["volume"] / max(1.0, y_volume * frac)
    if pace < VOL_SURGE:
        return False, f"量能 {pace:.1f} 倍,未達 {VOL_SURGE}"

    cr = s[-1]["change_rate"]
    if cr is None:
        return None, "無漲跌幅"
    if not (STABLE_LO <= cr <= STABLE_HI):
        return False, f"量增但漲幅 {cr:+.1f}% 不在價穩區間"
    return True, f"量能 {pace:.1f} 倍且漲幅 {cr:+.1f}% 價穩"


# ---------------------------------------------------------------- 主流程

def scan(universe: list[str], code_group: dict[str, str],
         db_path: str = "mls.db", at: _dt.datetime | None = None) -> dict:
    """
    13:20 執行。從 51 檔全集掃,不排除 A 鏈候選 ——
    候選池裡的股票一樣可能盤中冒出新異動,那也是有效資訊。
    """
    at = at or _dt.datetime.now()
    d = today_tw()

    envs = run_all({
        "snapshots": lambda: b_snapshot.series_all(d, db_path),
        "bar_y": lambda: store.read_date("daily_bar", d - _dt.timedelta(days=1), db_path),
    }, phase=Phase.INTRADAY)
    persist_status(envs, db_path)

    snaps = envs["snapshots"].get({}) or {}
    bar_y = envs["bar_y"].get({}) or {}

    if not snaps:
        return {
            "chain": "B", "data_date": d.isoformat(),
            "purpose": "B鏈掃描 — 無時序快照,請確認 b_snapshot 有在跑",
            "degraded": ["時序快照"], "items": [], "marked": 0,
        }

    # 族群平均漲幅
    gsum: dict[str, list[float]] = {}
    for c, ser in snaps.items():
        u = _usable(ser)
        if u and u[-1]["change_rate"] is not None:
            gsum.setdefault(code_group.get(c, "?"), []).append(u[-1]["change_rate"])
    group_avg = {g: sum(v) / len(v) for g, v in gsum.items() if v}

    items = []
    for code in universe:
        ser = snaps.get(code, [])
        if not ser:
            continue
        yv = (bar_y.get(code) or {}).get("volume")

        checks = {
            "持續性": c1_persistence(ser),
            "下殺承接": c2_dip_absorption(ser),
            "相對族群強度": c3_relative_strength(code, ser, group_avg, code_group),
            "量增價穩": c4_volume_stable(ser, yv, at),
        }
        hits = sum(1 for ok, _ in checks.values() if ok is True)
        nodata = [k for k, (ok, _) in checks.items() if ok is None]

        if hits < HITS_REQUIRED:
            continue

        last = _usable(ser)[-1] if _usable(ser) else ser[-1]
        items.append({
            "code": code, "group": code_group.get(code),
            "hits": hits,
            "criteria": {k: {"pass": ok, "why": why} for k, (ok, why) in checks.items()},
            "passed": [k for k, (ok, _) in checks.items() if ok is True],
            "missing": nodata,
            "price": last.get("price"), "change_rate": last.get("change_rate"),
            "net_active": last.get("net_active"),
        })

    items.sort(key=lambda x: (-x["hits"], x["code"]))
    now = at.isoformat(timespec="seconds")

    store.upsert_intraday(TABLE, PLUGIN, [{
        "data_date": d.isoformat(), "code": it["code"],
        "hits": it["hits"],
        "criteria": json.dumps(it["passed"], ensure_ascii=False),
        "detail": json.dumps(it, ensure_ascii=False),
        "scanned_at": now,
    } for it in items], db_path)

    return {
        "chain": "B", "data_date": d.isoformat(), "scanned_at": now,
        "purpose": (f"B鏈盤中發現({len(items)} 檔)— 非進場訊號,"
                    f"待盤後法人驗證後成為明日候選池新血"),
        "actionable": False,
        "degraded": missing_labels(envs),
        "scanned": len(snaps), "marked": len(items),
        "items": items,
    }
