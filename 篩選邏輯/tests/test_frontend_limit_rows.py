import unittest
from pathlib import Path


class FrontendLimitRowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = Path(__file__).resolve().parents[1]
        path = base / "intraday_decision_dataflow.html"
        if not path.exists():
            path = base.parent / "intraday_decision_dataflow.html"
        if not path.exists():
            raise unittest.SkipTest("8000 main-site HTML is not installed in the AB engine tree")
        cls.html = path.read_text(encoding="utf-8")

    def test_shared_exact_limit_predicate_exists(self):
        self.assertIn("const exactLimitUp=", self.html)
        self.assertIn("row?.is_limit_up===true", self.html)

    def test_dropped_t1_rows_receive_limit_up_class(self):
        self.assertIn("exactLimitUp(x,x.t1_price,x.t1_change)?'limit-up-row'", self.html)

    def test_limit_rows_use_solid_taiwan_market_red(self):
        self.assertIn(".limit-up-row td,", self.html)
        self.assertIn("background:#e53935!important", self.html)

    def test_percentage_only_guess_is_removed(self):
        self.assertNotIn(">=9.8", self.html)

    def test_individual_stock_rows_show_price_points_next_to_percent(self):
        self.assertIn("const chgFull=", self.html)
        self.assertIn("${chgFull(x.change_rate,x.price)}", self.html)
        self.assertIn("${esc(chgFull(row.change_rate,row.price))}", self.html)

    def test_first_layer_stock_cards_use_the_same_price_point_format(self):
        path = Path(__file__).resolve().parents[1].parent / "個股第一層ＵＩ.html"
        self.assertTrue(path.exists(), "first-layer observation-pool UI is missing")
        html = path.read_text(encoding="utf-8")
        self.assertIn("const chgFull=", html)
        self.assertIn("base[1].innerHTML=chgFull(x.change_rate,x.price)", html)
        self.assertIn("today.innerHTML=chgFull(x.change_rate,x.price)", html)

    def test_negative_change_cannot_reuse_stale_limit_flag(self):
        exact = self.html.split("const exactLimitUp=", 1)[1].split("};", 1)[0]
        self.assertLess(exact.index("c<=0"), exact.index("row?.is_limit_up===true"))

    def test_percent_string_cannot_reuse_stale_limit_flag(self):
        self.assertIn("const numericValue=", self.html)
        self.assertIn("v.replace(/[,%\\s]/g,'')", self.html)
        exact = self.html.split("const exactLimitUp=", 1)[1].split("};", 1)[0]
        self.assertIn("numericValue(change)", exact)
        self.assertIn("if(c<=0)return false", exact)

    def test_positive_but_non_limit_change_cannot_reuse_stale_limit_flag(self):
        # 2026-08-25 production bug: 台勝科(+0.40%) / 晶豪科(-4.26%) both still carried a
        # is_limit_up=true flag frozen from the pool/selection day. The old predicate only
        # rejected c<=0 before trusting the flag, so a small *positive* stale change (like
        # +0.40%) still fell through to `row.is_limit_up===true` and showed a false 漲停
        # badge. Whenever price+change are both available, the tick-based self-check must
        # be the only source of truth — the raw boolean flags may only be trusted as a
        # fallback when price or change is missing (old historical rows).
        exact = self.html.split("const exactLimitUp=", 1)[1].split("};", 1)[0]
        self.assertLess(exact.index("p!=null&&c!=null"), exact.index("row?.is_limit_up===true"))
        self.assertLess(exact.index("c<=0"), exact.index("row?.is_limit_up===true"))

    def test_day_position_is_recomputed_and_clamped(self):
        self.assertIn("const dayPositionPct=", self.html)
        self.assertIn("Math.max(0,Math.min(100", self.html)
        self.assertIn("row.day_position_pct=dayPositionPct", self.html)

    def test_limit_pullback_copy_uses_reactivation_language(self):
        for text in ("漲停後震盪・待突破", "重新啟動｜突破", "短線防守｜MA5", "⚠️ 賣壓增加"):
            self.assertIn(text, self.html)

    def test_post_verify_shows_institution_breakdown(self):
        for text in ("mr-chip-detail", "三大法人", "外資", "投信", "自營"):
            self.assertIn(text, self.html)
        for field in ("base_foreign_net", "base_trust_net", "base_dealer_net",
                      "t1_foreign_net", "t1_trust_net", "t1_dealer_net"):
            self.assertIn(field, self.html)

    def test_close_verification_distinguishes_strategy_hit_from_price_prediction(self):
        for text in ("策略條件通過", "核心條件通過", "核心通過・確認轉弱",
                     "非上漲預測命中", "這裡是策略條件驗證，不等於預測上漲勝率"):
            self.assertIn(text, self.html)
        self.assertNotIn("完整命中", self.html)

    def test_rejected_table_shows_recovery_fields(self):
        for label in ("淘汰原因", "救援訊號", "Recovery Score", "隔日觸發條件"):
            self.assertIn(label, self.html)

    def test_mobile_decision_ui_uses_four_expandable_pools(self):
        for pool in ("core", "reversal", "pullback", "watch"):
            self.assertIn(f"key:'{pool}'", self.html)
        self.assertIn('data-decision-pool="${p.key}"', self.html)
        self.assertIn("decision-pool-summary", self.html)

    def test_ui_uses_canonical_entry_state_and_upgrade_fields(self):
        for field in ("entry_state_label", "next_upgrade_condition", "reason_tags",
                      "potential_grade", "entry_quality", "chip_tags",
                      "decision_summary", "priority_label"):
            self.assertIn(field, self.html)
        for label in ("尚未觸發", "接近觸發", "已觸發", "觸發後失敗", "禁止追價", "等待回測"):
            self.assertIn(label, self.html)

    def test_uniform_source_column_is_removed_from_mobile_decision_table(self):
        decision_block = self.html.split("// Canonical Decision View", 1)[1].split("observeSection=()=>''", 1)[0]
        self.assertNotIn("<th>來源</th>", decision_block)
        self.assertNotIn("decision-source", decision_block)

    def test_decision_home_keeps_the_server_rank_contract(self):
        # 首頁不得另建一套獨立的資料排序（不重新賦值 data 本身），只能在既有
        # 資料上做展示用的視覺排序：漲停置頂 → 盤中觸發燈強度 → 當日漲跌。
        self.assertNotIn("data.sort(", self.html)
        self.assertIn("排序:漲停最強、一律置頂 → 再看盤中觸發強度(已站上>曾觸及) → 再依當日漲幅高到低。",
                       self.html)

    def test_plain_reading_only_claims_real_buying_when_group_is_actionable(self):
        # 「量價同步走強，買氣真實」是對進場品質的斷言，只有後台已判「可操作」
        # 才能講；否則只是價漲+資金翻正，量能/承接都還沒驗證，不能講「真實」。
        plain = self.html.split("function plain(row){", 1)[1].split("\n  function cleanRows", 1)[0]
        self.assertIn("row?.group==='可操作'", plain)
        self.assertIn("量價同步走強，買氣真實", plain)
        self.assertIn("量能品質與突破承接尚待確認", plain)

    def test_opportunity_radar_uses_the_home_canonical_snapshot(self):
        radar = self.html.split("if(kind==='radar'){", 1)[1].split("}else if(kind==='tomorrow')", 1)[0]
        self.assertIn("const items=[...data]", radar)
        self.assertNotIn("/api/intraday-watchpool", radar)
        self.assertIn("首頁同步快照", radar)


if __name__ == "__main__":
    unittest.main()
