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
from enum import Enum
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")

# ---------------------------------------------------------------- exceptions


class WrongPhaseError(RuntimeError):
    """呼叫端在錯誤的時段要求了不該存在的資料。這是程式錯誤,不是資料缺漏。"""


class Phase(str, Enum):
    PRE = "PRE"
    INTRADAY = "INTRADAY"
    POST = "POST"


# ---------------------------------------------------------------- boundaries

_OPEN = _dt.time(9, 0)
_CLOSE = _dt.time(13, 30)


def now_tw() -> _dt.datetime:
    return _dt.datetime.now(TZ)


def get_phase(at: _dt.datetime | None = None) -> Phase:
    """全系統唯一的時段判定。任何模組要知道現在是什麼時段,只能呼叫這支。"""
    at = at or now_tw()
    t = at.timetz().replace(tzinfo=None)
    if t < _OPEN:
        return Phase.PRE
    if t <= _CLOSE:
        return Phase.INTRADAY
    return Phase.POST


# ---------------------------------------------------------------- calendar

_WEEKEND = {5, 6}


def is_trading_day(d: _dt.date) -> bool:
    """僅排除週末。國定假日由 store 層的 holiday 表覆蓋(缺表時視為交易日)。"""
    return d.weekday() not in _WEEKEND


def prev_trading_day(d: _dt.date | None = None) -> _dt.date:
    d = d or now_tw().date()
    cur = d - _dt.timedelta(days=1)
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
        }[phase],
        "actionable": phase is Phase.POST,
    }
