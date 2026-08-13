import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
SCREEN_DIR = BASE / "screen-src" if (BASE / "screen-src").exists() else BASE
sys.path.insert(0, str(SCREEN_DIR))

import preflight


class PreflightUnboundedPoolTests(unittest.TestCase):
    def test_list_consistency_accepts_unbounded_pool(self):
        payload = {"items": [{"code": str(i)} for i in range(30)]}
        with patch("preflight.today_tw") as today, \
                patch("preflight.store.conn") as db_conn, \
                patch("screen_post.load_last_post", return_value=payload), \
                patch("screen_post.POOL_SIZE", None):
            today.return_value.isoformat.return_value = "2026-08-13"
            db_conn.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = []
            ok, message = preflight._check_list_consistency("unused.db")

        self.assertTrue(ok, message)


if __name__ == "__main__":
    unittest.main()
