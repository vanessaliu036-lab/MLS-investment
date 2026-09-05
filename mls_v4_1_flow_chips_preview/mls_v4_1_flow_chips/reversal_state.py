"""Persistent, evidence-first capital-flow state machine for Reversal Lab.

The live feed is a point-in-time view.  This module turns only changed,
time-stamped observations into a small event ledger so the lab can distinguish
an early reversal trigger from a confirmed reversal or a failed one.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import threading
from typing import Any


TW_TZ = timezone(timedelta(hours=8))
STATE_FILE = Path(os.environ.get(
    "MLS_REVERSAL_STATE_FILE",
    str(Path(__file__).resolve().parents[1] / "reversal_state.json"),
))
ABNORMAL_BUY_RATIO = 0.05
FLOW_FLIP_WINDOW_MINUTES = 120
MAX_EVENTS = 16
MAX_TRANSITIONS = 12

STATE_LABELS = {
    "CONFIRMED_REVERSAL": "Confirmed Reversal",
    "FAILED_REVERSAL": "Failed Reversal",
    "FLOW_FLIP": "Flow Flip",
    "ACCUMULATION": "Accumulation",
    "REVERSAL_TRIGGER": "等待延續確認",
    "OUTFLOW_BASELINE": "連賣觀察",
    "OBSERVING": "資料觀察中",
}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif value:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.now(TW_TZ)
    else:
        dt = datetime.now(TW_TZ)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TW_TZ)
    return dt.astimezone(TW_TZ)


def _baseline(card: dict) -> bool:
    """Use official prior-session chips only; the live A-flow is not chips."""
    foreign_days = _number(card.get("foreign_days"))
    net_5d = _number(card.get("foreign_net_5d"))
    net_20d = _number(card.get("foreign_net_20d"))
    return bool(
        (foreign_days is not None and foreign_days <= -2)
        or (net_5d is not None and net_5d < 0)
        or (net_20d is not None and net_20d < 0)
    )


def _flow_kind(card: dict) -> str:
    if str(card.get("aflow_status") or "").upper() not in {"", "LIVE"}:
        return "NO_DATA"
    aflow = _number(card.get("aflow"))
    if aflow is None:
        return "NO_DATA"
    if aflow < 0:
        return "SELL"
    if aflow == 0:
        return "FLAT"
    ratio = _number(card.get("aflow_ratio"))
    return "ABNORMAL_BUY" if ratio is not None and ratio >= ABNORMAL_BUY_RATIO else "BUY"


def _flow_sign(kind: str) -> str | None:
    if kind in {"BUY", "ABNORMAL_BUY"}:
        return "BUY"
    if kind == "SELL":
        return "SELL"
    return None


def _event(kind: str, observed_at: datetime, card: dict) -> dict:
    return {
        "kind": kind,
        "at": observed_at.isoformat(timespec="seconds"),
        "session": observed_at.date().isoformat(),
        "aflow": _number(card.get("aflow")),
        "aflow_ratio": _number(card.get("aflow_ratio")),
        "price": _number(card.get("price")),
    }


class ReversalStateMachine:
    """A tiny JSON-backed event ledger, intentionally isolated from main MLS."""

    def __init__(self, state_file: str | Path = STATE_FILE):
        self.state_file = Path(state_file)
        self._lock = threading.RLock()
        self._data = self._read()

    def _read(self) -> dict:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("stocks"), dict):
                return raw
        except (OSError, ValueError, TypeError):
            pass
        return {"version": 1, "stocks": {}}

    def _write(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.state_file)

    @staticmethod
    def _new_record() -> dict:
        return {
            "events": [],
            "flow_transitions": [],
            "trigger": None,
            "outcome": None,
            "last_kind": None,
            "last_session": None,
        }

    @staticmethod
    def _append_event(record: dict, item: dict) -> None:
        events = record.setdefault("events", [])
        previous = events[-1] if events else None
        # A new event is meaningful only if the signal changed or a new session began.
        if previous and previous.get("kind") == item["kind"] and previous.get("session") == item["session"]:
            previous.update(item)
        else:
            events.append(item)
        del events[:-MAX_EVENTS]

    @staticmethod
    def _apply_flow_transition(record: dict, kind: str, observed_at: datetime) -> bool:
        sign = _flow_sign(kind)
        if sign is None:
            return False
        transitions = record.setdefault("flow_transitions", [])
        if not transitions or transitions[-1].get("sign") != sign:
            transitions.append({"sign": sign, "at": observed_at.isoformat(timespec="seconds")})
        del transitions[:-MAX_TRANSITIONS]
        if len(transitions) < 3 or [item.get("sign") for item in transitions[-3:]] != ["BUY", "SELL", "BUY"]:
            return False
        started = _timestamp(transitions[-3].get("at"))
        return (observed_at - started).total_seconds() <= FLOW_FLIP_WINDOW_MINUTES * 60

    @staticmethod
    def _abnormal_buy_sessions(record: dict, *, no_prior_outflow: bool) -> list[str]:
        if not no_prior_outflow:
            return []
        return list(dict.fromkeys(
            event.get("session") for event in record.get("events", [])
            if event.get("kind") == "ABNORMAL_BUY" and event.get("session")
        ))

    @staticmethod
    def _state(card: dict, record: dict, *, baseline: bool, flow_flip: bool) -> tuple[str, list[str]]:
        outcome = record.get("outcome") or {}
        tags: list[str] = []
        if outcome.get("state") == "CONFIRMED_REVERSAL":
            tags.append("confirmed")
        elif outcome.get("state") == "FAILED_REVERSAL":
            tags.append("failed")
        if flow_flip:
            tags.append("flip")
        accumulation_sessions = ReversalStateMachine._abnormal_buy_sessions(
            record, no_prior_outflow=not baseline,
        )
        if len(accumulation_sessions) >= 2:
            tags.append("accumulation")

        if "confirmed" in tags:
            return "CONFIRMED_REVERSAL", tags
        if "failed" in tags:
            return "FAILED_REVERSAL", tags
        if "flip" in tags:
            return "FLOW_FLIP", tags
        if "accumulation" in tags:
            return "ACCUMULATION", tags
        if record.get("trigger"):
            return "REVERSAL_TRIGGER", tags
        if baseline:
            return "OUTFLOW_BASELINE", tags
        return "OBSERVING", tags

    @staticmethod
    def _path(record: dict, state: str, *, baseline: bool) -> list[dict]:
        trigger = record.get("trigger")
        if state == "ACCUMULATION":
            sessions = ReversalStateMachine._abnormal_buy_sessions(record, no_prior_outflow=True)
            return [
                {"label": "無前期流出", "status": "done"},
                {"label": "異常買", "status": "done"},
                {"label": f"連續買 {len(sessions)} 日", "status": "done"},
            ]
        third = "等待延續買"
        third_status = "pending"
        if state == "CONFIRMED_REVERSAL":
            third, third_status = "延續買", "done"
        elif state == "FAILED_REVERSAL":
            third, third_status = "再賣", "failed"
        return [
            {"label": "連賣", "status": "done" if baseline or trigger else "pending"},
            {"label": "異常買", "status": "done" if trigger else "pending"},
            {"label": third, "status": third_status},
        ]

    def apply(self, cards: list[dict], observed_at: str | datetime | None = None) -> list[dict]:
        observed = _timestamp(observed_at)
        with self._lock:
            changed = False
            for card in cards:
                symbol = str(card.get("symbol") or "")
                if not symbol:
                    continue
                record = self._data["stocks"].setdefault(symbol, self._new_record())
                baseline = _baseline(card)
                kind = _flow_kind(card)
                current = _event(kind, observed, card)

                if baseline:
                    baseline_event = _event("OUTFLOW_BASELINE", observed, card)
                    has_baseline = any(
                        event.get("kind") == "OUTFLOW_BASELINE"
                        and event.get("session") == baseline_event["session"]
                        for event in record.get("events", [])
                    )
                    if not has_baseline:
                        self._append_event(record, baseline_event)
                if kind != "NO_DATA":
                    self._append_event(record, current)
                    record["last_kind"] = kind
                    record["last_session"] = current["session"]

                trigger = record.get("trigger")
                if baseline and kind == "ABNORMAL_BUY" and not trigger:
                    trigger = {"session": current["session"], "at": current["at"], "event": current}
                    record["trigger"] = trigger
                    changed = True
                elif trigger and current["session"] > str(trigger.get("session") or ""):
                    if kind == "ABNORMAL_BUY":
                        record["outcome"] = {"state": "CONFIRMED_REVERSAL", "at": current["at"], "event": current}
                        changed = True
                    elif kind == "SELL":
                        record["outcome"] = {"state": "FAILED_REVERSAL", "at": current["at"], "event": current}
                        changed = True

                flow_flip = self._apply_flow_transition(record, kind, observed)
                state, tags = self._state(card, record, baseline=baseline, flow_flip=flow_flip)
                card.update({
                    "state_machine": state,
                    "state_machine_label": STATE_LABELS[state],
                    "state_tags": tags,
                    "state_path": self._path(record, state, baseline=baseline),
                    "state_events": list(record.get("events", []))[-4:],
                    "state_trigger": record.get("trigger"),
                    "state_outcome": record.get("outcome"),
                })
                changed = True
            if changed:
                self._write()
        return cards
