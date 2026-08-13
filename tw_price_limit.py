"""Taiwan stock daily price-limit helpers (tick-aware, deterministic)."""

from __future__ import annotations

import math


def tick_size(price: float) -> float:
    price = float(price)
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def _round_tick(value: float, mode: str = "nearest") -> float:
    tick = tick_size(value)
    units = value / tick
    if mode == "down":
        units = math.floor(units + 1e-9)
    elif mode == "up":
        units = math.ceil(units - 1e-9)
    else:
        units = math.floor(units + 0.5 + 1e-9)
    decimals = 2 if tick < 0.1 else 1 if tick < 1 else 0
    return round(units * tick, decimals)


def infer_reference_price(price, change_rate):
    if price is None or change_rate is None:
        return None
    price, change_rate = float(price), float(change_rate)
    if price <= 0 or change_rate <= -100:
        return None
    return _round_tick(price / (1 + change_rate / 100), "nearest")


def limit_up_price(reference_price):
    if reference_price is None or float(reference_price) <= 0:
        return None
    return _round_tick(float(reference_price) * 1.10, "down")


def is_limit_up(price, *, reference_price=None, change_rate=None) -> bool:
    if price is None:
        return False
    ref = reference_price
    if ref is None:
        ref = infer_reference_price(price, change_rate)
    upper = limit_up_price(ref)
    if upper is None:
        return False
    return float(price) >= upper - tick_size(upper) / 10
