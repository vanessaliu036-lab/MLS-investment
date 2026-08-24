"""現價不得落在自己的今日高低之外 —— 壞輪詢用昨收冒充現價的護欄。

2026-08-24 09:36 實測:3532 台勝科漲停 376.5,/ab/watchlist 一輪回
price=342.5(昨收)、change_rate=0.00,而同一列 intraday_high=376.5、
intraday_low=344.5 —— 現價比自己的最低價還低。使用者看到的是「平盤 342.5」。

上游路徑:MIS 那一輪 z='-'(漲停無成交)且五檔同時空 → mis_source._parse_row
退到昨收 → quote_health 覆蓋 price/change_rate → feed_bridge 寫進 AB。
兩層都補:mis_source 不回這種列,feed_bridge 也不寫這種列。
"""
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent / "個股卡片相關檔案_20260722"))

import mis_source


class MisPrevCloseGuardTests(unittest.TestCase):

    def _raw(self, **kw):
        row = {"c": "3532", "y": "342.5000", "z": "-", "a": "", "b": "",
               "h": "376.5000", "l": "344.5000", "o": "350.0000", "v": "5344"}
        row.update(kw)
        return row

    def test_prev_close_outside_today_range_is_rejected(self):
        self.assertIsNone(mis_source._parse_row(self._raw()))

    def test_prev_close_kept_when_stock_has_not_traded(self):
        # 今天還沒成交:沒有高低價 → 昨收是唯一能講的,允許
        r = mis_source._parse_row(self._raw(h="-", l="-", v="0"))
        self.assertIsNotNone(r)
        self.assertEqual(r["price_kind"], "prev_close")

    def test_real_trade_price_always_wins(self):
        r = mis_source._parse_row(self._raw(z="376.5000"))
        self.assertEqual(r["price"], 376.5)
        self.assertEqual(r["price_kind"], "trade")
        self.assertEqual(r["change_rate"], 9.93)

    def test_book_mid_used_before_prev_close(self):
        r = mis_source._parse_row(self._raw(b="376.5000_376.0000_", a="-"))
        self.assertEqual(r["price"], 376.5)
        self.assertEqual(r["price_kind"], "book_mid")


class FeedBridgeRowGuardTests(unittest.TestCase):
    """feed_bridge.once() 不把內部矛盾的列寫進 quote_snap。"""

    def setUp(self):
        import feed_bridge, json, io as _io, urllib.request
        self.fb = feed_bridge
        self.written = []
        self._orig_up = feed_bridge.store.upsert_intraday
        self._orig_open = feed_bridge.urllib.request.urlopen
        feed_bridge.store.upsert_intraday = (
            lambda table, plugin, rows, db: self.written.append((table, rows)))

        payload = {"ok": True, "source": "test", "rows": [
            # 壞列:現價比自己的今日最低還低(昨收冒充)
            {"code": "3532", "price": 342.5, "change_rate": 0.0,
             "high": 376.5, "low": 344.5, "total_volume": 5344},
            # 好列
            {"code": "2303", "price": 125.0, "change_rate": 7.29,
             "high": 127.0, "low": 117.5, "total_volume": 90117},
        ]}

        class _Resp:
            def __init__(self, data): self._d = data
            def read(self): return self._d
            def __enter__(self): return self
            def __exit__(self, *a): return False

        feed_bridge.urllib.request.urlopen = (
            lambda *a, **k: _Resp(json.dumps(payload).encode()))

    def tearDown(self):
        self.fb.store.upsert_intraday = self._orig_up
        self.fb.urllib.request.urlopen = self._orig_open

    def test_bad_row_skipped_good_row_written(self):
        n, _src = self.fb.once()
        codes = [r["code"] for t, rows in self.written if t == "quote_snap"
                 for r in rows]
        self.assertEqual(codes, ["2303"], "現價落在自己高低之外的列不得寫入")
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
