# -*- coding: utf-8 -*-
"""Regression test for the 2026-09-01 chip QA incident.

Two real extreme cases (國巨 2327 heavy institutional sell-off, 全新 2455
strong institutional buying) were cross-checked against TWSE's own T86 feed.
The 5D/20D window sums and foreign/trust/dealer breakdowns were already
exactly correct; the one confirmed defect was ``inst_net_20d_lots`` staying
None in the FinMind fallback path even though its own foreign/trust/dealer
components were present and summed correctly. This locks the invariant that
bug violated: institution == foreign + trust + dealer, for every window,
on both a heavy-sell and a heavy-buy fixture, so a future regression that
only breaks one derived field (not the window selection itself) gets caught
immediately instead of surfacing as a silent "—" on a stock card.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

CHIPS_PATH = Path(__file__).resolve().parent.parent / "個股卡片相關檔案_20260722" / "chips.py"


def _load_chips_module(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("chips_under_test", CHIPS_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["chips_under_test"] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "CACHE_FILE", str(tmp_path / "chips_cache.json"))
    return mod


# 20 synthetic trading days (oldest first), modeled on 2327's real 08/04-08/31
# pattern: a large sell-off skewed toward foreign investors.
SELLOFF_DAYS = [f"2026-08-{d:02d}" for d in list(range(4, 22)) if d not in (8, 9, 15, 16)][:20]
BUY_SURGE_DAYS = SELLOFF_DAYS  # same calendar skeleton, opposite sign fixture


def _finmind_rows(dates, foreign_per_day, trust_per_day, dealer_per_day):
    rows = []
    for d, f, t, dl in zip(dates, foreign_per_day, trust_per_day, dealer_per_day):
        rows.append({"date": d, "name": "Foreign_Investor", "buy": max(f, 0) * 1000, "sell": max(-f, 0) * 1000})
        rows.append({"date": d, "name": "Investment_Trust", "buy": max(t, 0) * 1000, "sell": max(-t, 0) * 1000})
        rows.append({"date": d, "name": "Dealer_self", "buy": max(dl, 0) * 1000, "sell": max(-dl, 0) * 1000})
    return rows


@pytest.mark.parametrize("sign", [-1, 1], ids=["selloff_like_2327", "buy_surge_like_2455"])
def test_finmind_fallback_inst_20d_matches_component_sum(tmp_path, monkeypatch, sign):
    chips = _load_chips_module(tmp_path, monkeypatch)
    n = 20
    foreign = [sign * (1000 + 50 * i) for i in range(n)]
    trust = [sign * (200 + 5 * i) for i in range(n)]
    dealer = [sign * -30 for _ in range(n)]
    rows = _finmind_rows(SELLOFF_DAYS, foreign, trust, dealer)
    monkeypatch.setattr(chips, "_finmind", lambda dataset, code, start: rows)

    detail = chips.get_chips_detail("TESTCODE", asof="2026-08-31")

    assert detail["source"] == "FinMind 盤後法人"
    for window in ("5d", "20d"):
        f = detail[f"foreign_net_{window}"]
        t = detail[f"trust_net_{window}"]
        d = detail[f"dealer_net_{window}"]
        assert None not in (f, t, d), f"{window} components must not be None"
    assert detail["inst_net_5d_lots"] == (
        detail["foreign_net_5d"] + detail["trust_net_5d"] + detail["dealer_net_5d"])
    # This is the field that regressed: it must equal the sum of its own
    # components, not silently stay None while the components are present.
    assert detail["inst_net_20d_lots"] is not None, (
        "inst_net_20d_lots regressed to None in the FinMind fallback path")
    assert detail["inst_net_20d_lots"] == (
        detail["foreign_net_20d"] + detail["trust_net_20d"] + detail["dealer_net_20d"])
    assert (detail["foreign_net_20d"] > 0) == (sign > 0)


def test_save_disk_does_not_clobber_other_codes(tmp_path, monkeypatch):
    """chips_official.build_cache() and get_chips() write the same on-disk
    file independently; a save from one code must not erase another code's
    richer entry, nor another code entirely (the 2026-09-01 incident: the
    first get_chips() call after a date rollover wiped the whole cache down
    to just the one code it was computing)."""
    chips = _load_chips_module(tmp_path, monkeypatch)
    import json
    seed = {"date": "2026-09-01", "stocks": {
        "2327": {"foreign_net_20d": -88471, "trust_net_20d": -14376,
                  "dealer_net_20d": -982, "inst_net_20d_lots": -103829,
                  "source": "TWSE T86 / TPEx 官方三大法人", "source_date": "2026-08-31",
                  "inst_streak": -3},
    }}
    with open(chips.CACHE_FILE, "w") as f:
        json.dump(seed, f)

    monkeypatch.setattr(chips, "_cache", {"date": "2026-09-01", "stocks": {
        "2455": {"inst_net_20d_lots": 111, "inst_streak": 6,
                  "big_holder_pct": None, "big_holder_trend": None},
    }})
    chips._save_disk()

    with open(chips.CACHE_FILE) as f:
        on_disk = json.load(f)
    assert on_disk["stocks"]["2327"]["trust_net_20d"] == -14376, (
        "an unrelated code's cache entry was wiped by a narrower save")
    assert on_disk["stocks"]["2455"]["inst_net_20d_lots"] == 111
