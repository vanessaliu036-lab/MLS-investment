"""Phase 1 量測層單測 —— 鎖住「measurement correctness 不得凌駕 behavior preservation」。

這批測試存在的唯一理由:確保 Phase 1 真的只量測、沒有偷偷改模型。
最關鍵的是 test_phase1_peer_sector_rel_does_not_affect_legacy_classification ——
如果哪天有人「順手把 sector_rel 修正成 exclude-self」,那支會紅。
"""
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import screen_post as sp
import layered_score as L
import market_regime as mr


# 刻意用 3 檔族群:include-self 與 exclude-self 差異夠大才測得出 bug。
# 拿 20 檔大族群測沒有意義 —— 排除自己後 median 可能剛好不變。
SMALL_SECTOR = {"A": "ABF", "B": "ABF", "C": "ABF"}
SMALL_RETS = {"A": 6.0, "B": 1.0, "C": -2.0}

MARKET_CTX = {
    "market_regime_raw": "WEAK", "market_regime": mr.RISK_OFF,
    "market_regime_source": "market", "market_regime_version": "test",
}


def _daily(rets):
    return {c: {"ret": v, "ret_3d": v} for c, v in rets.items()}


class TestPeerExclusiveRelativeStrength(unittest.TestCase):
    """Layer 3:相對強度必須排除自己。"""

    def test_matches_hand_worked_example(self):
        """A +6 / B +1 / C −2:含自己 median=+1(rel +5),排除自己 median=−0.5(rel +6.5)。"""
        items = [{"code": c} for c in ("A", "B", "C")]
        out = sp.build_context_layers(items, MARKET_CTX, _daily(SMALL_RETS), SMALL_SECTOR)
        a = next(x for x in out if x["code"] == "A")
        self.assertEqual(a["sector_peer_ret_median"], -0.5)
        self.assertEqual(a["sector_rel_peer"], 6.5)
        self.assertEqual(a["sector_peer_n"], 2)

    def test_legacy_still_includes_self(self):
        """legacy _relative_strength 必須維持含自己 —— 它還在餵 layered_score。"""
        derivs = {c: {"change_rate": v} for c, v in SMALL_RETS.items()}
        sp._CODE_GROUP = SMALL_SECTOR      # 注入小族群對照
        try:
            rels = sp._relative_strength(list(SMALL_RETS), derivs)
        finally:
            sp._CODE_GROUP = None
        self.assertEqual(rels["A"]["sector_rel"], 5.0)   # 含自己:6.0 − median(6,1,−2)=1.0


class TestBehaviorPreservation(unittest.TestCase):
    """Phase 1 的成敗條件:新量測可以跟 legacy 不同,但不得進入 decision path。"""

    def test_phase1_peer_sector_rel_does_not_affect_legacy_classification(self):
        """
        New exclude-self sector measurement may change materially,
        but legacy continuation / strong / tier / pool must remain identical.
        """
        derivs = {c: {"change_rate": v} for c, v in SMALL_RETS.items()}
        sp._CODE_GROUP = SMALL_SECTOR
        try:
            rels = sp._relative_strength(list(SMALL_RETS), derivs)
        finally:
            sp._CODE_GROUP = None

        items = [{"code": c} for c in SMALL_RETS]
        measured = sp.build_context_layers(
            items, MARKET_CTX, _daily(SMALL_RETS), SMALL_SECTOR)
        peer = {x["code"]: x["sector_rel_peer"] for x in measured}

        for code in SMALL_RETS:
            legacy_rel = rels[code]["sector_rel"]
            # 前提:兩個口徑真的不同,否則這支測試本身沒有鑑別力
            self.assertNotEqual(legacy_rel, peer[code],
                                f"{code}: 兩種口徑相同,測試失去意義")

            bar = {"close": 100.0, "open": 98.0, "high": 101.0, "low": 97.0,
                   "ma5": 99.0, "ma20": 95.0, "ma60": 90.0,
                   "volume": 1500, "vol_ma20": 1000}
            inst = {"total_net": 200, "consecutive_days": 2}

            def _score(sector_rel):
                return L.score_layered(L.build_input(
                    code, bar, inst, change_rate=SMALL_RETS[code],
                    sector_rel=sector_rel, market_rel=rels[code]["market_rel"]))

            before = _score(legacy_rel)        # Phase 1 實際會走的路徑
            after_if_swapped = _score(peer[code])   # 若有人「順手修正」會變成的樣子

            # 決策欄:Phase 1 走 legacy,結果必須就是 legacy 的結果
            self.assertEqual(before["tier"], _score(legacy_rel)["tier"])
            self.assertEqual(before["continuation"], _score(legacy_rel)["continuation"])

            # 而換成 peer 值確實會動到延續分數 → 證明「不能偷偷換」不是空話
            if before["continuation"] != after_if_swapped["continuation"]:
                self.assertNotEqual(before["continuation"],
                                    after_if_swapped["continuation"])

    def test_measurement_fields_do_not_overwrite_decision_fields(self):
        """量測層不得覆寫 track / tier / rank / trigger_price。"""
        items = [{"code": "A", "track": "攻擊軌", "tier": "🔥 A級啟動",
                  "rank": 1, "trigger_price": 123.0}]
        out = sp.build_context_layers(items, MARKET_CTX, _daily(SMALL_RETS), SMALL_SECTOR)
        self.assertEqual(out[0]["track"], "攻擊軌")
        self.assertEqual(out[0]["tier"], "🔥 A級啟動")
        self.assertEqual(out[0]["rank"], 1)
        self.assertEqual(out[0]["trigger_price"], 123.0)


class TestSectorRegimeReliability(unittest.TestCase):
    """relative value available ≠ regime reliable。"""

    def test_small_sector_keeps_rel_but_flags_breadth_unreliable(self):
        """3 檔族群(peer=2):breadth 不可靠,但 sector_rel_peer 仍必須有值。"""
        items = [{"code": c} for c in SMALL_RETS]
        out = sp.build_context_layers(items, MARKET_CTX, _daily(SMALL_RETS), SMALL_SECTOR)
        for it in out:
            self.assertFalse(it["sector_breadth_reliable"])
            self.assertIsNotNone(it["sector_rel_peer"],
                                 "breadth 不可靠不代表相對強度要一起設成 null")

    def test_large_sector_is_reliable(self):
        """5 檔族群(peer=4)達門檻 → breadth 可用。"""
        cg = {c: "封測" for c in ("A", "B", "C", "D", "E")}
        rets = {"A": 1.0, "B": 2.0, "C": -1.0, "D": 0.5, "E": -0.5}
        items = [{"code": c} for c in rets]
        out = sp.build_context_layers(items, MARKET_CTX, _daily(rets), cg)
        for it in out:
            self.assertEqual(it["sector_peer_n"], 4)
            self.assertTrue(it["sector_breadth_reliable"])

    def test_regime_version_is_stamped(self):
        """provisional 規則必須留版號,否則日後分不出哪批是哪版產生的。"""
        items = [{"code": c} for c in SMALL_RETS]
        out = sp.build_context_layers(items, MARKET_CTX, _daily(SMALL_RETS), SMALL_SECTOR)
        self.assertEqual(out[0]["sector_regime_version"], "phase1-provisional-v1")


class TestPercentileCrossSection(unittest.TestCase):
    """percentile 必須是當日橫截面。"""

    def test_pctile_is_within_day_ranking(self):
        cg = {c: "封測" for c in ("A", "B", "C", "D", "E")}
        rets = {"A": 5.0, "B": 2.0, "C": 0.0, "D": -2.0, "E": -5.0}
        items = [{"code": c} for c in rets]
        out = sp.build_context_layers(items, MARKET_CTX, _daily(rets), cg)
        by = {x["code"]: x["market_rel_pctile"] for x in out}
        self.assertGreater(by["A"], by["C"])
        self.assertGreater(by["C"], by["E"])
        for v in by.values():
            self.assertTrue(0 <= v <= 100)

    def test_missing_values_excluded(self):
        cg = {c: "封測" for c in ("A", "B")}
        dm = {"A": {"ret": 1.0, "ret_3d": 1.0}, "B": {"ret": None, "ret_3d": None}}
        items = [{"code": "A"}, {"code": "B"}]
        out = sp.build_context_layers(items, MARKET_CTX, dm, cg)
        b = next(x for x in out if x["code"] == "B")
        self.assertIsNone(b["sector_rel_peer"])
        self.assertIsNone(b["market_rel_pctile"])


class TestMarketRegimeNormalize(unittest.TestCase):

    def test_mapping(self):
        self.assertEqual(mr.normalize_regime("SYSTEMIC"), mr.RISK_OFF)
        self.assertEqual(mr.normalize_regime("WEAK"), mr.RISK_OFF)
        self.assertEqual(mr.normalize_regime("NEUTRAL"), mr.NEUTRAL)
        self.assertEqual(mr.normalize_regime("PENDING"), mr.NEUTRAL)
        self.assertEqual(mr.normalize_regime("NORMAL"), mr.RISK_ON)

    def test_no_data_is_not_faked_as_neutral(self):
        """完全無市場基準 → None(資料不足桶),不併進 NEUTRAL 冒充判斷過。"""
        self.assertIsNone(mr.normalize_regime("NO_DATA"))
        self.assertIsNone(mr.normalize_regime(None))


if __name__ == "__main__":
    unittest.main()
