"""
phase.py — 時段唯一裁決者

鐵律(所有模組必須遵守,違反直接 raise,不准 fallback、不准回 None):

1. 全系統只有這一支能判斷「現在是哪個時段」。其他模組禁止自己看時鐘。
2. 所有取數函式必須帶明確 data_date 參數。禁止任何函式自己猜「今天是哪天」。
3. 在 PRE 時段查詢今日盤後資料 = 呼叫端寫錯,raise WrongPhaseError。
   不是 NO_DATA、不是空表、不是 None。讓它炸,在自檢時就該被擋掉。
4. 「已收盤」與「無資料」永遠是兩件事。收盤後正是盤後資料最完整的時刻。
   任何錯誤訊息不准出現「已收盤所以沒有資料」。

時段定義(台股):
  PRE      00:00–08:59  盤前。不重算、不重抓,直接讀昨日盤後名單。零 API。
  INTRADAY 09:00–13:30  盤中。Shioaji 訂閱串流 + DB 昨日死值。一次都不打 FinMind。
  POST     13:31–23:59  盤後。今日法人/融資 + 今日盤中累積。
"""

from __future__ import annotations

import datetime as _dt
import json as _json
from enum import Enum
from pathlib import Path as _Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")

# ---------------------------------------------------------------- 假日行事曆
# 真正休市日由 calendar_sync.py 從 TWSE 官方抓進 holidays.json。
# 週末永遠休市(不需列)。缺檔時退化為「只排除週末」。
_HOLIDAY_FILE = _Path(__file__).parent / "holidays.json"
_hcache: dict = {"mtime": None, "set": frozenset()}


def _holidays() -> frozenset:
    try:
        m = _HOLIDAY_FILE.stat().st_mtime
    except OSError:
        return frozenset()
    if _hcache["mtime"] != m:
        try:
            _hcache["set"] = frozenset(_json.loads(_HOLIDAY_FILE.read_text(encoding="utf-8")))
        except Exception:
            _hcache["set"] = frozenset()
        _hcache["mtime"] = m
    return _hcache["set"]

# ---------------------------------------------------------------- exceptions


class WrongPhaseError(RuntimeError):
    """呼叫端在錯誤的時段要求了不該存在的資料。這是程式錯誤,不是資料缺漏。"""


class Phase(str, Enum):
    PRE = "PRE"
    INTRADAY = "INTRADAY"
    POST = "POST"
    CLOSED = "CLOSED"   # 非交易日(週末/國定假日):不按時鐘啟動任何盤中/盤後觸發


# ---------------------------------------------------------------- boundaries

_OPEN = _dt.time(9, 0)
_CLOSE = _dt.time(13, 30)


def now_tw() -> _dt.datetime:
    return _dt.datetime.now(TZ)


def get_phase(at: _dt.datetime | None = None) -> Phase:
    """
    全系統唯一的時段判定。任何模組要知道現在是什麼時段,只能呼叫這支。

    鐵律:先問「今天股市有沒有開」,再看時鐘。
    非交易日(週末/國定假日)一律回 CLOSED —— 盤中/盤後觸發絕不按時鐘啟動。
    """
    at = at or now_tw()
    if not is_trading_day(at.date()):
        return Phase.CLOSED
    t = at.timetz().replace(tzinfo=None)
    if t < _OPEN:
        return Phase.PRE
    if t <= _CLOSE:
        return Phase.INTRADAY
    return Phase.POST


# ---------------------------------------------------------------- calendar

_WEEKEND = {5, 6}


def is_trading_day(d: _dt.date) -> bool:
    """排除週末 + TWSE 官方國定假日(holidays.json)。缺檔時退化為只排除週末。"""
    if d.weekday() in _WEEKEND:
        return False
    return d.isoformat() not in _holidays()


def prev_trading_day(d: _dt.date | None = None) -> _dt.date:
    d = d or now_tw().date()
    cur = d - _dt.timedelta(days=1)
    while not is_trading_day(cur):
        cur -= _dt.timedelta(days=1)
    return cur


def last_trading_day(d: _dt.date | None = None) -> _dt.date:
    """d(含)當天或往前回溯,最近一個交易日。用於休市時定位『上一交易日』。"""
    cur = d or now_tw().date()
    while not is_trading_day(cur):
        cur -= _dt.timedelta(days=1)
    return cur


def today_tw() -> _dt.date:
    return now_tw().date()


# ---------------------------------------------------------------- 資料日期解析

def resolve_data_date(phase: Phase | None = None) -> _dt.date:
    """
    這個時段「該用哪一天的盤後死值」。

    PRE      -> 昨日(上一個交易日)
    INTRADAY -> 昨日(盤中的法人/融資/MA20 全部是昨日死值)
    POST     -> 今日
    """
    phase = phase or get_phase()
    if phase is Phase.POST:
        return today_tw()
    if phase is Phase.CLOSED:
        return last_trading_day()   # 休市 → 上一交易日死值
    return prev_trading_day()


# ---------------------------------------------------------------- 守門函式

def assert_can_read(data_date: _dt.date, phase: Phase | None = None) -> None:
    """
    取數前的守門。呼叫端要讀 data_date 這天的盤後資料時先過這關。

    唯一會擋的情況:PRE / INTRADAY 時段要求「今日」的盤後資料。
    那天的資料在物理上還不存在,是呼叫端寫錯了。
    """
    phase = phase or get_phase()
    if phase is not Phase.POST and data_date >= today_tw():
        raise WrongPhaseError(
            f"時段 {phase.value} 不得讀取 data_date={data_date} 的盤後資料。"
            f"此時段應使用 {resolve_data_date(phase)}(上一交易日死值)。"
            f"這是呼叫端錯誤,不是資料缺漏。"
        )


def assert_can_fetch_finmind(phase: Phase | None = None) -> None:
    """盤中一次都不准打 FinMind。所有法人/融資欄位在盤中都是 DB 昨日死值。"""
    phase = phase or get_phase()
    if phase is Phase.INTRADAY:
        raise WrongPhaseError(
            "INTRADAY 時段禁止呼叫 FinMind。法人/融資/MA20 皆為昨日死值,"
            "應直接讀 SQLite。盤中唯一的即時來源是 Shioaji 訂閱。"
        )


def describe(phase: Phase | None = None) -> dict:
    """給前端顯示用。purpose 由後端決定,前端不准自己寫。"""
    phase = phase or get_phase()
    dd = resolve_data_date(phase)
    return {
        "phase": phase.value,
        "data_date": dd.isoformat(),
        "purpose": {
            Phase.PRE: f"今日盯盤名單(資料日 {dd})— 昨日盤後結果,開盤後觀察用",
            Phase.INTRADAY: f"盤中吸籌觀察(背景資料日 {dd})— 僅供記錄,不作進場依據",
            Phase.POST: f"明日進場清單(資料日 {dd})— 依此執行",
            Phase.CLOSED: f"休市中(今日非交易日)— 顯示上一交易日 {dd} 盤後名單,僅供參考",
        }[phase],
        "actionable": phase is Phase.POST,
    }
