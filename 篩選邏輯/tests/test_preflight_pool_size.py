import unittest

import preflight
import screen_post


class PreflightPoolSizeTest(unittest.TestCase):
    def test_unbounded_pool_size_does_not_fail_consistency_check(self):
        original_pool_size = screen_post.POOL_SIZE
        screen_post.POOL_SIZE = None
        try:
            try:
                ok, message = preflight._check_list_consistency()
            except Exception as exc:
                self.fail(f"POOL_SIZE=None must not raise: {exc}")
        finally:
            screen_post.POOL_SIZE = original_pool_size

        self.assertTrue(ok, message)


if __name__ == "__main__":
    unittest.main()
