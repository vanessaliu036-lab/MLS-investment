"""快照覆蓋率守門:當日 daily_bar 沒補齊就不准寫。

2026-08-24 首日真實踩到:FinMind 晚出,daily_bar 只有 30/51,
run_pa_snapshot 照樣寫了 51 列 —— 21 檔用前一交易日價量算 stage 卻蓋當日 data_date。
live observation 是不能污染的前瞻樣本,寧可缺一天。
"""
import sys, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import run_pa_snapshot as R


class _FakeStore:
    def __init__(self, n): self.n = n
    def has_date(self, table, d, *a, **k): return self.n


def _patch(monkeypatch, bars, items=None, written=None):
    items = items if items is not None else [{"code": str(1000 + i)} for i in range(51)]
    monkeypatch.setattr(R, "config", type("C", (), {"UNIVERSE": [i["code"] for i in items]}))
    monkeypatch.setattr(R, "screen_post", type("S", (), {
        "build": staticmethod(lambda codes: {"data_date": "2026-08-24", "items": items})}))
    monkeypatch.setattr(R, "store", _FakeStore(bars))
    calls = written if written is not None else []
    monkeypatch.setattr(R, "pa_snapshot", type("P", (), {
        "write_snapshot": staticmethod(lambda d, rows, *a, **k: (calls.append((d, len(rows))), len(rows))[1]),
        "backfill": staticmethod(lambda *a, **k: 0)}))
    return calls


def test_refuses_to_write_when_daily_bar_incomplete(monkeypatch):
    calls = _patch(monkeypatch, bars=30)          # 2026-08-24 真實情境
    assert R.main() == 2
    assert calls == [], "資料只有 30/51 卻仍寫入 —— 守門沒擋住"


def test_writes_when_daily_bar_complete(monkeypatch):
    calls = _patch(monkeypatch, bars=51)
    assert R.main() == 0
    assert calls and calls[0][1] == 51


def test_threshold_is_tunable_by_env(monkeypatch):
    monkeypatch.setattr(R, "MIN_BAR_COVERAGE", 0.5)
    calls = _patch(monkeypatch, bars=30)
    assert R.main() == 0, "門檻放寬到 5 成時 30/51 應該可寫"
    assert calls and calls[0][1] == 51
