"""2026-08-27 修正:dropped_pool 是「if dropped:」條件式寫入(screen_post.py),
零真淘汰的交易日不會有那天的列。舊版 _rejects_on 直接斷言 dropped_pool 本身的
新鮮度,把「今天剛好 0 筆真淘汰」誤判成「接到停更表」,連環拖垮整條 stage2。
正確訊號應該是 candidate_pool(screen_post 每天無條件寫)。"""
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import store
import reject_verify


class RejectVerifyFreshnessTests(unittest.TestCase):
    def _seed(self, db_path, pool_date, dropped_last_date):
        store.init_db(db_path)
        store.upsert_intraday("candidate_pool", "screen_post", [
            {"data_date": pool_date, "code": "2330", "rank": 1, "score": 90.0,
             "payload": "{}", "generated_at": "2026-01-01T00:00:00"},
        ], db_path)
        store.upsert_intraday("dropped_pool", "screen_post", [
            {"data_date": dropped_last_date, "code": "9999",
             "payload": "{}", "generated_at": "2026-01-01T00:00:00"},
        ], db_path)

    def test_zero_drops_today_is_not_treated_as_stale_source(self):
        """dropped_pool 最後一筆是幾天前(因為那之後每天都 0 筆真淘汰),
        但 candidate_pool 有今天 → 不該拋 StaleSourceError。"""
        db = "/tmp/test_reject_verify_freshness_ok.db"
        self._seed(db, pool_date="2026-08-26", dropped_last_date="2026-08-24")
        result = reject_verify._rejects_on(
            __import__("datetime").date(2026, 8, 26), db)
        self.assertEqual(result, {})  # 今天 0 筆真淘汰,合理空結果,不是例外

    def test_candidate_pool_itself_stale_still_raises(self):
        """screen_post 真的沒跑(candidate_pool 也沒有今天)才該被攔下。"""
        db = "/tmp/test_reject_verify_freshness_stale.db"
        self._seed(db, pool_date="2026-08-24", dropped_last_date="2026-08-24")
        with self.assertRaises(reject_verify.StaleSourceError):
            reject_verify._rejects_on(
                __import__("datetime").date(2026, 8, 26), db)


if __name__ == "__main__":
    unittest.main()
