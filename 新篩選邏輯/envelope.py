"""
envelope.py — 插件信封(失敗隔離層)

擋住的痛點:
「我拆成插件了,但一個點壞掉全部都壞掉。」

原因是之前只做到「檔案分開、只透過 SQLite 溝通」,沒做失敗隔離。
一支插件拋錯 → 整個 pipeline 斷 → 拆插件等於白拆。

規範:
1. 每個插件的輸出一律包在信封裡,永不拋錯給呼叫端。
2. 呼叫端只讀信封的 status。status != OK 就是那一格顯示「缺 XXX」,
   其餘欄位照常算、名單照常出。
3. 永遠不會因為某一項壞掉而導致整份名單出不來。
4. 唯一允許炸的例外:WrongPhaseError(時段呼叫錯 = 程式寫錯),
   必須在 preflight 自檢時被擋下來,不該進到執行期。

信封格式:
  { plugin, status: OK|NO_DATA|ERROR, value, reason, phase, data_date, elapsed_ms }

三態:
  OK       有資料且算完
  NO_DATA  資料真的沒到,附原因 (SOURCE_PENDING / API_ERROR / SUSPENDED / NOT_FETCHED)
  ERROR    插件自己爆了,附 traceback 摘要

NO_DATA 絕不等於 FAIL。缺資料就是那一項不計分(0 分不扣分),名單照排。
"""

from __future__ import annotations

import datetime as _dt
import time
import traceback
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable

from phase import Phase, WrongPhaseError, get_phase, resolve_data_date


class Status(str, Enum):
    OK = "OK"
    NO_DATA = "NO_DATA"
    ERROR = "ERROR"


class Reason(str, Enum):
    SOURCE_PENDING = "SOURCE_PENDING"   # 官方/FinMind 尚未更新
    API_ERROR = "API_ERROR"             # 取數失敗
    SUSPENDED = "SUSPENDED"             # 該股停牌
    NOT_FETCHED = "NOT_FETCHED"         # DB 沒這天的資料,還沒抓過
    PLUGIN_ERROR = "PLUGIN_ERROR"       # 插件自己爆了


@dataclass
class Envelope:
    plugin: str
    status: Status = Status.OK
    value: Any = None
    reason: str = ""
    phase: str = ""
    data_date: str = ""
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status is Status.OK

    def get(self, default=None):
        """取值。壞掉就回 default,呼叫端不需要 try/except。"""
        return self.value if self.ok else default

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


def run_plugin(name: str, fn: Callable[..., Any], *args,
               phase: Phase | None = None, **kwargs) -> Envelope:
    """
    統一的插件執行器。任何插件都透過這支呼叫。

    插件內部不管怎麼爆,都不會傳染給呼叫端 —— 只會變成一張 status=ERROR 的信封。
    """
    ph = phase or get_phase()
    dd = resolve_data_date(ph)
    t0 = time.perf_counter()

    try:
        value = fn(*args, **kwargs)
    except WrongPhaseError:
        # 唯一允許往上炸的:時段呼叫錯 = 程式寫錯,必須讓它炸
        raise
    except Exception as e:
        return Envelope(
            plugin=name, status=Status.ERROR, value=None,
            reason=f"{Reason.PLUGIN_ERROR.value}: {type(e).__name__}: {e}"[:300],
            phase=ph.value, data_date=dd.isoformat(),
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )

    elapsed = int((time.perf_counter() - t0) * 1000)

    if value is None or (hasattr(value, "__len__") and len(value) == 0):
        return Envelope(
            plugin=name, status=Status.NO_DATA, value=None,
            reason=Reason.NOT_FETCHED.value,
            phase=ph.value, data_date=dd.isoformat(), elapsed_ms=elapsed,
        )

    return Envelope(plugin=name, status=Status.OK, value=value, reason="",
                    phase=ph.value, data_date=dd.isoformat(), elapsed_ms=elapsed)


def no_data(name: str, reason: Reason | str, phase: Phase | None = None) -> Envelope:
    """插件內部主動回報「資料真的沒到」時用這支,附明確原因。"""
    ph = phase or get_phase()
    r = reason.value if isinstance(reason, Reason) else str(reason)
    return Envelope(plugin=name, status=Status.NO_DATA, reason=r,
                    phase=ph.value, data_date=resolve_data_date(ph).isoformat())


def run_all(plugins: dict[str, Callable], phase: Phase | None = None) -> dict[str, Envelope]:
    """
    跑一批插件。任何一支壞掉,其餘照跑,結果照樣完整回傳。

    這就是「斷點隔離」:一格壞掉只影響那一格。
    """
    return {name: run_plugin(name, fn, phase=phase) for name, fn in plugins.items()}


def persist_status(envelopes: dict[str, Envelope], db_path: str = "mls.db") -> None:
    """把插件狀態落地,方便你事後查是哪一格壞了、壞多久了。"""
    import store
    rows = [{
        "plugin": e.plugin, "data_date": e.data_date, "phase": e.phase,
        "status": e.status.value, "reason": e.reason,
        "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
    } for e in envelopes.values()]
    if rows:
        store.upsert_intraday("plugin_status", "store", rows, db_path)


def missing_labels(envelopes: dict[str, Envelope]) -> list[str]:
    """哪幾格缺了。直接丟給前端顯示「缺:承接品質、法人」。"""
    return [name for name, e in envelopes.items() if not e.ok]
