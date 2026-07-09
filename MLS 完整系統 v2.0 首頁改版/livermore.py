"""
MLS 插件 — livermore.py
李佛摩價格紀錄法 · 主流產業與關鍵點偵測引擎 v1.0
====================================================================
純插件:只讀主系統既有資料(state / broker 日K / config / db),
不改任何主系統邏輯。掛法與 nexora 完全相同 —
由 after_hours 盤後掛鉤呼叫 run_report(state),產出報告 + summary。

────────────────────────────────────────────────────────────────
李佛摩《股票作手操盤術》價格紀錄法核心(本引擎忠實還原):
  1. 追蹤同產業「姊妹股」兩檔龍頭,個別記錄每日高低。
  2. 六種狀態(六色):
       上升趨勢 UPTREND / 下降趨勢 DOWNTREND
       自然回檔 NAT_REACTION / 自然反彈 NAT_RALLY
       次級回檔 SEC_REACTION / 次級反彈 SEC_RALLY
     以「約 6% 擺動」為狀態切換門檻(李佛摩原著精神:重要股價
     波動約六點;此處用百分比化,PIVOT_SWING_PCT 可調)。
  3. 關鍵點(Pivotal Point):
       (a) 反轉關鍵點 — 價格於自然回檔後重返並「突破前一上升趨勢
           高點」→ 多方關鍵點;反向為空方關鍵點。
       (b) 續勢關鍵點 — 自然回檔在前一次自然回檔低點「之上」止穩
           後再創高 → 趨勢延續確認點。
  4. 主流產業 = 同族群兩檔姊妹股「同步」進入/維持上升趨勢,
     且同步創階段新高的族群(李佛摩:龍頭與姊妹股必須共同確認,
     單獨一檔動不算數)。

────────────────────────────────────────────────────────────────
與 MLS 整合鐵律(Rule 0,最高優先,置於李佛摩訊號之前):
  • 主引擎(ENGINE_STOCKS,IC代工/製造)出現關鍵點 →
    僅輸出為「環境溫度計」,標註「不列入進場」,永不發進場訊號。
  • 攻擊部隊出現多方關鍵點 → 才輸出 ABAB 快打進場提示,
    並強制標註「嚴格停損 / 破均線即出 / 外資轉賣即走」。
  • 盤中資金流 ≠ 法人;本引擎只用『價格結構』判關鍵點,
    法人一律留待 chips 盤後確認(與 NEXORA Hard Rule 一致)。
"""

import os
import json
from datetime import datetime, timezone, timedelta

import config as C

try:
    import broker
except Exception:      # 測試環境無 Shioaji 時容錯
    broker = None
try:
    import db
except Exception:
    db = None

TW_TZ = timezone(timedelta(hours=8))
REPORT_DIR = os.path.join(os.path.dirname(__file__), "reports")

# ── 可調參數(集中此處,對接時只改這裡) ────────────────────
PIVOT_SWING_PCT = 6.0      # 狀態切換門檻(%);李佛摩原著約六點擺動
SEC_SWING_PCT = 3.0        # 次級波動門檻(%),小於此視為次級
KBAR_DAYS = 70             # 取多少日日K重建價格紀錄
SYNC_LOOKBACK = 20         # 姊妹股同步性回看交易日
SYNC_MIN_RATIO = 0.55      # 同步比例門檻:兩股同向天數比 ≥ 此→主流
LEADERS_PER_SECTOR = 2     # 每族群取幾檔姊妹股(李佛摩法:2)

# 六狀態
UPTREND, DOWNTREND = "上升趨勢", "下降趨勢"
NAT_REACTION, NAT_RALLY = "自然回檔", "自然反彈"
SEC_REACTION, SEC_RALLY = "次級回檔", "次級反彈"

STATE_COLOR = {  # 對應李佛摩六色欄位(前端用)
    UPTREND: "#c62828", DOWNTREND: "#2e7d32",     # 台股慣例:紅漲綠跌
    NAT_RALLY: "#ef6c00", NAT_REACTION: "#1565c0",
    SEC_RALLY: "#8a6d1a", SEC_REACTION: "#5e35b1",
}


# ════════════════════════════════════════════════════════
# 一、李佛摩價格紀錄狀態機(單檔)
# ════════════════════════════════════════════════════════
class LivermoreRecord:
    """
    對單一檔股票的日K序列,重建李佛摩六欄價格紀錄,
    並偵測關鍵點。日K輸入:list[dict(date, high, low, close)](舊→新)。
    """

    def __init__(self, code, name, bars):
        self.code = code
        self.name = name
        self.bars = bars or []
        self.state = None
        self.pivot_up = None       # 最近一次上升趨勢高點(關鍵點基準)
        self.pivot_down = None     # 最近一次下降趨勢低點
        self.last_nat_rally_high = None
        self.last_nat_react_low = None
        self.trend_high = None     # 當前趨勢累計高
        self.trend_low = None      # 當前趨勢累計低
        self.pivots = []           # 偵測到的關鍵點事件
        self.history = []          # 每日狀態(供前端六色渲染)
        self._build()

    @staticmethod
    def _pct(a, b):
        if not b:
            return 0.0
        return (a - b) / b * 100.0

    def _build(self):
        for bar in self.bars:
            hi = bar.get("high")
            lo = bar.get("low")
            cl = bar.get("close", hi)
            if hi is None or lo is None:
                continue
            self._step(bar.get("date"), hi, lo, cl)

    def _step(self, date, hi, lo, cl):
        # 首根:初始化為上升趨勢基準
        if self.state is None:
            self.state = UPTREND
            self.trend_high = hi
            self.trend_low = lo
            self.pivot_up = hi
            self._log(date, cl, note="初始化")
            return

        # 依當前狀態與擺動幅度推進狀態機
        if self.state in (UPTREND, SEC_RALLY, NAT_RALLY):
            # 續創高 → 維持/回到上升趨勢
            if hi >= (self.trend_high or hi):
                prev_high = self.trend_high
                self.trend_high = hi
                # 是否處於「未收復的下降趨勢」中:若確認過空方低點且價格
                # 尚未突破前一上升高點(pivot_up),此波僅為下降中的反彈,
                # 不得升格為上升趨勢,也不得發多方續勢關鍵點。
                in_downtrend_bounce = (
                    self.pivot_down is not None and
                    (self.pivot_up is None or hi <= self.pivot_up)
                )
                if in_downtrend_bounce:
                    # 維持反彈態(自然/次級),等待是否突破 pivot_up 才轉多
                    self.state = NAT_RALLY if self.state != SEC_RALLY else SEC_RALLY
                else:
                    # 續勢關鍵點:自然回檔後重返創高,且守住前一回檔低點
                    if self.state in (NAT_RALLY, SEC_RALLY):
                        if (self.last_nat_react_low is None or
                                lo > self.last_nat_react_low):
                            self._mark_pivot(date, cl, "多方續勢關鍵點",
                                             "自然回檔守穩前低後再創高,趨勢延續確認")
                    # 反轉/突破關鍵點:突破前一上升趨勢高點
                    if self.pivot_up and hi > self.pivot_up * (1 + 1e-9):
                        if prev_high is not None and prev_high < self.pivot_up:
                            self._mark_pivot(date, cl, "多方突破關鍵點",
                                             "突破前一上升趨勢高點,買方掌控")
                        self.pivot_up = hi
                        self.pivot_down = None      # 已收復,解除下降確認
                    self.state = UPTREND
                    self.trend_low = lo
            else:
                drop = -self._pct(cl, self.trend_high)
                if drop >= PIVOT_SWING_PCT:
                    # 轉入自然回檔;記錄前一上升高點為關鍵點基準,
                    # 並確立下降參考低點(pivot_down),使後續跌破可判關鍵點。
                    self.pivot_up = self.trend_high
                    self.last_nat_rally_high = self.trend_high
                    self.state = NAT_REACTION
                    self.trend_low = lo
                    self.last_nat_react_low = lo
                    if self.pivot_down is None:
                        self.pivot_down = lo
                elif drop >= SEC_SWING_PCT:
                    self.state = SEC_REACTION
                    self.trend_low = lo

        elif self.state in (DOWNTREND, SEC_REACTION, NAT_REACTION):
            if lo <= (self.trend_low or lo):
                prev_low = self.trend_low
                self.trend_low = lo
                # 空方續勢關鍵點:須「已確認下降趨勢」(pivot_down 已存在)
                # 才成立,避免上升趨勢中的第一段自然回檔被誤判為做空點。
                if (self.state in (NAT_REACTION, SEC_REACTION)
                        and self.pivot_down is not None):
                    if (self.last_nat_rally_high is None or
                            hi < self.last_nat_rally_high):
                        self._mark_pivot(date, cl, "空方續勢關鍵點",
                                         "自然反彈未過前高即再破底,弱勢延續")
                if self.pivot_down is None:
                    # 首次確立下降參考低點(初次進入下降趨勢)
                    self.pivot_down = lo
                elif lo < self.pivot_down * (1 - 1e-9):
                    # 曾反彈後再破前低 → 空方跌破關鍵點
                    if prev_low is not None and prev_low > self.pivot_down:
                        self._mark_pivot(date, cl, "空方跌破關鍵點",
                                         "跌破前一下降趨勢低點,賣方掌控")
                    self.pivot_down = lo
                self.state = DOWNTREND
                self.trend_high = hi
            else:
                rise = self._pct(cl, self.trend_low)
                if rise >= PIVOT_SWING_PCT:
                    self.pivot_down = self.trend_low
                    self.last_nat_react_low = self.trend_low
                    self.state = NAT_RALLY
                    self.trend_high = hi
                    self.last_nat_rally_high = hi
                elif rise >= SEC_SWING_PCT:
                    self.state = SEC_RALLY
                    self.trend_high = hi

        self._log(date, cl)

    def _mark_pivot(self, date, price, kind, reason):
        self.pivots.append({
            "date": str(date), "price": round(price, 2),
            "kind": kind, "reason": reason,
        })

    def _log(self, date, price, note=""):
        self.history.append({
            "date": str(date), "state": self.state,
            "price": round(price or 0, 2),
            "color": STATE_COLOR.get(self.state, "#1a1d23"),
            "note": note,
        })

    # ── 對外查詢 ──
    def latest_state(self):
        return self.state

    def latest_pivot(self):
        return self.pivots[-1] if self.pivots else None

    def is_uptrend(self):
        return self.state in (UPTREND, NAT_RALLY, SEC_RALLY)

    def made_new_high(self, lookback=SYNC_LOOKBACK):
        """近 lookback 日內收盤是否創期間新高。"""
        if len(self.history) < 2:
            return False
        seg = self.history[-lookback:]
        prices = [h["price"] for h in seg]
        return prices[-1] >= max(prices)

    def direction_series(self, lookback=SYNC_LOOKBACK):
        """回傳最近 lookback 日的方向序列(+1 上升系 / -1 下降系)。"""
        seg = self.history[-lookback:]
        return [1 if h["state"] in (UPTREND, NAT_RALLY, SEC_RALLY) else -1
                for h in seg]


# ════════════════════════════════════════════════════════
# 二、日K 取得(優先用 broker;測試/降級時可注入)
# ════════════════════════════════════════════════════════
def _get_bars(code, days=KBAR_DAYS, injected=None):
    """
    回傳 list[dict(date, high, low, close)](舊→新)。
    broker.daily_kbars 只給 (date, close, high, volume) → 用 close 補 low
    的保守近似(low 不影響上升系關鍵點主邏輯,僅影響擺動幅度精度;
    若日後 broker 提供 low 欄位可直接帶入,無需改本引擎)。
    """
    if injected is not None:
        return injected
    if broker is None:
        return []
    try:
        raw = broker.daily_kbars(code, days=days)
    except Exception as e:
        print(f"[plugin/livermore] {code} 日K取得失敗:{e}")
        return []
    bars = []
    for r in raw:
        hi = r.get("high")
        cl = r.get("close")
        lo = r.get("low", cl if cl is not None else hi)  # 保守補值
        bars.append({"date": r.get("date"), "high": hi,
                     "low": lo, "close": cl})
    return bars


# ════════════════════════════════════════════════════════
# 三、主流產業偵測(姊妹股同步性 — 李佛摩龍頭共振)
# ════════════════════════════════════════════════════════
def _sector_members():
    """由 config.SECTOR_MAP 反建 {族群: [(code, type), ...]}。"""
    groups = {}
    for code, (sector, typ) in C.SECTOR_MAP.items():
        groups.setdefault(sector, []).append((code, typ))
    return groups


def _pick_sisters(members, snaps_by_code):
    """
    每族群挑 2 檔姊妹股:以當日成交金額排名取龍頭兩檔
    (李佛摩:追蹤族群中最活躍的兩檔)。無快照時退回代碼序。
    """
    def amount(code):
        s = snaps_by_code.get(code, {})
        return s.get("total_amount", 0) or 0
    ranked = sorted(members, key=lambda ct: -amount(ct[0]))
    return ranked[:LEADERS_PER_SECTOR]


def _sync_ratio(rec_a, rec_b, lookback=SYNC_LOOKBACK):
    """兩檔姊妹股方向序列的同步比例。"""
    a = rec_a.direction_series(lookback)
    b = rec_b.direction_series(lookback)
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    a, b = a[-n:], b[-n:]
    same = sum(1 for x, y in zip(a, b) if x == y)
    return same / n


def detect_leading_sectors(snaps, injected_bars=None):
    """
    回傳主流產業列表(含姊妹股價格紀錄與關鍵點)。
    injected_bars: {code: bars} 供測試注入;正式環境為 None 走 broker。
    """
    snaps_by_code = {s["code"]: s for s in snaps} if snaps else {}
    groups = _sector_members()
    results = []

    for sector, members in groups.items():
        sisters = _pick_sisters(members, snaps_by_code)
        if len(sisters) < 2:
            continue
        recs = []
        for code, typ in sisters:
            name = C.NAME_MAP.get(code, code)
            inj = (injected_bars or {}).get(code)
            bars = _get_bars(code, injected=inj)
            if not bars:
                continue
            rec = LivermoreRecord(code, name, bars)
            rec.stock_type = typ                      # attack / engine
            recs.append(rec)
        if len(recs) < 2:
            continue

        ra, rb = recs[0], recs[1]
        sync = _sync_ratio(ra, rb)
        both_up = ra.is_uptrend() and rb.is_uptrend()
        both_new_high = ra.made_new_high() and rb.made_new_high()
        # 主流判定:同步比例達標 且 兩檔同處上升系
        is_leading = (sync >= SYNC_MIN_RATIO) and both_up
        # 強度:同步 + 雙創高 = 最強
        strength = round(sync * (1.0 + (0.5 if both_new_high else 0)
                                 + (0.3 if both_up else 0)), 3)

        results.append({
            "sector": sector,
            "sisters": [
                {"code": r.code, "name": r.name, "type": r.stock_type,
                 "state": r.latest_state(),
                 "pivot": r.latest_pivot(),
                 "new_high": r.made_new_high(),
                 "history": r.history[-SYNC_LOOKBACK:],
                 "pivots": r.pivots[-5:]}
                for r in recs[:2]
            ],
            "sync": round(sync, 2),
            "both_uptrend": both_up,
            "both_new_high": both_new_high,
            "is_leading": is_leading,
            "strength": strength,
        })

    results.sort(key=lambda x: (-x["is_leading"], -x["strength"]))
    return results


# ════════════════════════════════════════════════════════
# 四、MLS Rule 0 整合 — 關鍵點 → 進場提示 / 溫度計
# ════════════════════════════════════════════════════════
def _is_engine(code):
    return code in C.ENGINE_STOCKS


def build_signals(leading):
    """
    把主流產業中的關鍵點,依 Rule 0 轉為輸出:
      • 主引擎關鍵點 → 溫度計(不列入進場)
      • 攻擊部隊多方關鍵點 → ABAB 快打提示(強制停損話術)
    """
    entry_signals = []
    thermometer = []
    for sec in leading:
        if not sec["is_leading"]:
            continue
        for sis in sec["sisters"]:
            piv = sis.get("pivot")
            if not piv:
                continue
            code, name, typ = sis["code"], sis["name"], sis["type"]
            bullish = piv["kind"].startswith("多方")
            if _is_engine(code) or typ == "engine":
                thermometer.append({
                    "code": code, "name": name, "sector": sec["sector"],
                    "pivot": piv["kind"], "date": piv["date"],
                    "note": "主引擎關鍵點=環境溫度計,不列入進場;"
                            "外資在場則環境穩,離場即警訊。",
                })
            elif bullish:
                entry_signals.append({
                    "code": code, "name": name, "sector": sec["sector"],
                    "type": "attack", "pivot": piv["kind"],
                    "date": piv["date"], "price": piv["price"],
                    "action": "ABAB 快打進場觀察",
                    "discipline": "嚴格停損:跌破關鍵點價或 MA20 即出;"
                                  "外資盤後轉賣立即離場;絕不留倉過度。",
                    "reason": piv["reason"],
                })
    return entry_signals, thermometer


# ════════════════════════════════════════════════════════
# 五、插件入口 run_report(state) — 與 nexora 相同契約
# ════════════════════════════════════════════════════════
def run_report(state, rotation_reports=None, injected_bars=None):
    snaps = [s for s in state.get("_snaps", []) if s.get("sector")] \
        if state else []
    leading = detect_leading_sectors(snaps, injected_bars=injected_bars)
    entry_signals, thermometer = build_signals(leading)

    lead_names = [s["sector"] for s in leading if s["is_leading"]]
    d = datetime.now(TW_TZ)

    L = []
    L.append(f"# 李佛摩價格紀錄法報告 {d:%Y-%m-%d}")
    L.append("\n## 1. 主流產業(姊妹股同步共振)")
    if lead_names:
        L.append("**當前主流:** " + "、".join(lead_names))
    else:
        L.append("(今日無族群達成姊妹股同步上升條件;市場無明確主流。)")
    for sec in leading[:8]:
        flag = "🔥主流" if sec["is_leading"] else "—"
        s0, s1 = sec["sisters"][0], sec["sisters"][1]
        L.append(
            f"- [{flag}] **{sec['sector']}** 同步{sec['sync']:.0%} "
            f"強度{sec['strength']} | "
            f"{s0['name']}({s0['code']}) {s0['state']}"
            f"{'✦新高' if s0['new_high'] else ''} · "
            f"{s1['name']}({s1['code']}) {s1['state']}"
            f"{'✦新高' if s1['new_high'] else ''}")

    L.append("\n## 2. 關鍵點 → 攻擊部隊快打提示(Rule 0 已過濾)")
    if entry_signals:
        for sg in entry_signals:
            L.append(f"- **{sg['name']}({sg['code']})** {sg['sector']}｜"
                     f"{sg['pivot']} @ {sg['price']}({sg['date']})")
            L.append(f"    → {sg['action']}｜{sg['reason']}")
            L.append(f"    ⚠ {sg['discipline']}")
    else:
        L.append("(主流族群中攻擊部隊無新多方關鍵點,不發進場提示。)")

    L.append("\n## 3. 主引擎關鍵點(僅環境溫度計,不列入進場)")
    if thermometer:
        for t in thermometer:
            L.append(f"- {t['name']}({t['code']}) {t['sector']}｜"
                     f"{t['pivot']}({t['date']})")
            L.append(f"    🌡 {t['note']}")
    else:
        L.append("(主引擎無關鍵點變化。)")

    L.append("\n## 4. 鐵律備註")
    L.append("- 主流判定採李佛摩龍頭共振:單一檔動作不算,姊妹股"
             "同步同向且同步創高方為主流。")
    L.append("- 關鍵點僅由『價格結構』判定;法人買賣一律留待 chips "
             "盤後資料確認(NEXORA Hard Rule 一致)。")
    L.append(f"- 狀態切換門檻 {PIVOT_SWING_PCT}%,次級門檻 {SEC_SWING_PCT}%;"
             f"同步回看 {SYNC_LOOKBACK} 日,同步門檻 {SYNC_MIN_RATIO:.0%}。")

    report_md = "\n".join(L)

    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"LIVERMORE_{d:%Y%m%d}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report_md)

    summary = (f"📈 李佛摩｜主流:{'、'.join(lead_names) if lead_names else '無'}"
               f"｜攻擊部隊關鍵點 {len(entry_signals)} 檔"
               f"｜溫度計 {len(thermometer)} 檔")

    return {
        "path": path, "summary": summary, "report": report_md,
        "leading_sectors": lead_names,
        "detail": leading,
        "entry_signals": entry_signals,
        "thermometer": thermometer,
    }


# 允許獨立冒煙測試:python livermore.py
if __name__ == "__main__":
    import math
    # 合成兩檔同步上升 + 一次回檔後突破的姊妹股,驗證關鍵點偵測
    def synth(start, phase):
        bars, p = [], start
        for i in range(60):
            drift = math.sin((i + phase) / 6.0) * 4 + i * 0.6
            p = start + drift
            bars.append({"date": f"2026-04-{(i % 28) + 1:02d}",
                         "high": p * 1.01, "low": p * 0.99, "close": p})
        return bars
    inj = {"6451": synth(100, 0), "4979": synth(80, 0),
           "3363": synth(60, 3), "3450": synth(50, 3)}
    fake_snaps = [{"code": c, "sector": "光通訊",
                   "total_amount": 9e8 - i * 1e8}
                  for i, c in enumerate(inj)]
    out = run_report({"_snaps": fake_snaps}, injected_bars=inj)
    print(out["summary"])
    print("---")
    print(out["report"][:1500])
