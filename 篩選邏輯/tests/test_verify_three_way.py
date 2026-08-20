"""三分命中率 regression —— 用 2026-08-19 真實案例鎖住「絕對敗 ≠ 挑錯」。

固定這天的理由:08-19 命中率 30%,引擎軌 20 檔只中 6 檔,看起來像選股失敗;
實際上其中有數檔同時贏大盤也贏族群 —— 舊制把這兩類都記成「未命中」,
會讓人往錯的方向改篩選器。這支測試確保兩類永遠被分開。

fixture 由引擎正式庫 2026-08-18 候選池 + 08-18/08-19 daily_bar 產出,
不是手捏的數字。
"""
import datetime as _dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import screen_verify as sv
import store

FIX = Path(__file__).resolve().parent / "fixtures"
POOL = json.loads((FIX / "2026-08-18-pool.json").read_text(encoding="utf-8"))
EXPECT = json.loads((FIX / "2026-08-19-outcome.json").read_text(encoding="utf-8"))
POOL_DATE = _dt.date(2026, 8, 18)
DATA_DATE = _dt.date(2026, 8, 19)


def _seed_db(path: str):
    """把 fixture 灌成一個可跑的庫。schema 一律走 store.init_db(),
    不自己手刻 —— 手刻的 schema 會跟正本漂移,正是這個專案踩過的坑。"""
    store.init_db(path)
    with store.conn(path) as c:
        for n, it in enumerate(POOL["items"], 1):
            c.execute(
                "INSERT OR REPLACE INTO candidate_pool"
                " (data_date, code, rank, score, track, trigger_price, payload, generated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (POOL["pool_date"], it["code"], n, 0.0, it["track"],
                 it["trigger_price"], json.dumps(it["payload"], ensure_ascii=False), ""))
        for d, rows in POOL["daily_bar"].items():
            for b in rows:
                c.execute(
                    "INSERT OR REPLACE INTO daily_bar"
                    " (data_date, code, high, close, ma5, ma20) VALUES (?,?,?,?,?,?)",
                    (d, b["code"], b["high"], b["close"], b["ma5"], b["ma20"]))
        c.commit()


class ThreeWayHitTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls._tmp.name) / "fixture.db")
        _seed_db(cls.db)
        cls.res = sv.verify(cls.db, DATA_DATE)
        cls.by_code = {r["code"]: r for r in cls.res["items"]}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_legacy_hit_semantics_unchanged(self):
        """既有 hit / hit_rate 語意完全不變 —— 三分命中率是並列新增,不是取代。"""
        self.assertEqual(self.res["denom"], EXPECT["aggregate"]["denom"])
        self.assertEqual(self.res["hits"], EXPECT["aggregate"]["hits"])
        self.assertEqual(self.res["hit_rate"], EXPECT["aggregate"]["hit_rate"])

    def test_three_way_rates(self):
        for key in ("hit_abs_rate", "hit_vs_market_rate", "hit_vs_sector_rate"):
            self.assertEqual(self.res[key], EXPECT["aggregate"][key], key)

    def test_real_miss_fails_all_three(self):
        """南亞科 −6.6%:大盤/族群都輸 → 三項全敗 = 真的挑錯。"""
        r = self.by_code["2408"]
        self.assertLess(r["stock_ret_t1"], r["market_ret_t1"])
        self.assertLess(r["stock_ret_t1"], r["sector_ret_t1"])
        self.assertEqual((r["hit_abs"], r["hit_vs_market"], r["hit_vs_sector"]), (0, 0, 0))

    def test_resilient_stock_is_not_a_plain_failure(self):
        """上銀 −0.27%:絕對是跌的,但同時贏大盤與族群 → 不能歸類成普通選股失敗。"""
        r = self.by_code["2049"]
        self.assertEqual(r["hit_abs"], 0)          # 絕對:跌 = FAIL
        self.assertEqual(r["hit_vs_market"], 1)    # 相對大盤:WIN
        self.assertEqual(r["hit_vs_sector"], 1)    # 相對族群:WIN
        self.assertEqual(r["hit"], 0)              # 舊制:同樣是「未命中」

    def test_two_categories_are_distinguishable(self):
        """這兩類在舊制 hit 完全一樣,新制必須分得開 —— 否則這層白做。"""
        real_miss, resilient = self.by_code["2408"], self.by_code["2049"]
        self.assertEqual(real_miss["hit"], resilient["hit"])
        self.assertNotEqual(
            (real_miss["hit_abs"], real_miss["hit_vs_market"], real_miss["hit_vs_sector"]),
            (resilient["hit_abs"], resilient["hit_vs_market"], resilient["hit_vs_sector"]))

    def test_sector_baseline_excludes_self(self):
        """族群基準必須排除自己,否則小族群裡「贏過族群」變成部分在跟自己比。"""
        for code in ("2408", "2049"):
            r = self.by_code[code]
            self.assertIsNotNone(r["sector_peer_n_t1"])
            self.assertGreater(r["sector_peer_n_t1"], 0)
            peers = [x for x in self.res["items"]
                     if x["sector_name"] == r["sector_name"] and x["code"] != code]
            self.assertEqual(r["sector_peer_n_t1"], len(peers))

    def test_regime_snapshot_carried_from_pool_payload(self):
        """環境快照要從選股當時的 payload 帶過來,不是驗證當天重算。"""
        r = self.by_code["2408"]
        self.assertEqual(r["sector_regime_version"], "phase1-provisional-v1")
        self.assertIsNotNone(r["market_regime"])
        self.assertIsNotNone(r["sector_regime"])

    def test_row_level_fixture_lock(self):
        """逐列鎖住,任何一檔的三分結果變了都要爆。"""
        for exp in EXPECT["rows"]:
            got = self.by_code[exp["code"]]
            for k in ("stock_ret_t1", "market_ret_t1", "sector_ret_t1",
                      "hit_abs", "hit_vs_market", "hit_vs_sector", "hit", "verdict"):
                self.assertEqual(got.get(k), exp[k], f"{exp['code']}.{k}")


if __name__ == "__main__":
    unittest.main()
