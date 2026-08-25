"""Opportunity scoring / snapshot 的行為鎖定。

重點鎖三件曾經或可能出錯的事:
  1. 55% 是主榜資格線,**不是刪除線** —— 高 payoff 低勝率股票不得被丟掉
  2. 進場價必須是 T+1 開盤,不是 T0 收盤
  3. 族群相對強度必須 leave-one-out,不得把自己算進去
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import opportunity_score as osc


def _bars(closes, opens=None, highs=None, lows=None):
    n = len(closes)
    opens = opens or closes
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    return [{"date": f"2026-01-{i+1:02d}", "open": opens[i], "high": highs[i],
             "low": lows[i], "close": closes[i]} for i in range(n)]


def test_low_win_rate_high_payoff_is_kept_not_deleted():
    """章程第 9 條:勝率低於 55% 但 payoff 結構強,必須留在 HIGH_POTENTIAL。
    這是使用者明確要求 —— 禁止因單一勝率門檻丟掉高 payoff 股票。"""
    stats = {"insufficient": False, "n": 200,
             "net_positive_rate": 46.0,          # 低於 55%
             "profit_factor": 2.2,               # 但 PF 很高
             "expected_upside": 7.5, "p_hit_3pct": 61.0,
             "net_expectancy": 2.5, "expected_downside": -4.0,
             "avg_win": 9.0, "avg_loss": -3.5}
    tier, reasons = osc.assign_tier(True, stats)
    assert tier == "HIGH_POTENTIAL", (tier, reasons)
    assert any("PF" in r or "payoff" in r for r in reasons)


def test_primary_requires_both_win_rate_and_sector():
    stats = {"insufficient": False, "n": 200, "net_positive_rate": 58.0,
             "profit_factor": 1.4, "expected_upside": 4.0, "p_hit_3pct": 60.0,
             "net_expectancy": 1.0, "expected_downside": -5.0,
             "avg_win": 6.0, "avg_loss": -5.0}
    assert osc.assign_tier(True, stats)[0] == "PRIMARY"
    # 族群訊號未觸發 → 不進主榜(唯一 replicated 的證據就是族群訊號)
    assert osc.assign_tier(False, stats)[0] == "WATCH"


def test_avoid_only_for_clearly_bad():
    """AVOID 要留給真正不利的,不能變成第二條刪除線。"""
    weak = {"insufficient": False, "n": 200, "net_positive_rate": 44.0,
            "profit_factor": 1.0, "expected_upside": 3.0, "p_hit_3pct": 50.0,
            "net_expectancy": 0.1, "expected_downside": -5.0,
            "avg_win": 5.0, "avg_loss": -5.0}
    assert osc.assign_tier(False, weak)[0] == "WATCH"
    assert osc.assign_tier(True, weak, excluded=True)[0] == "AVOID"


def test_entry_is_next_day_open_not_today_close():
    """盤後名單在 T0 收盤買不到。用收盤會把隔夜跳空算成自己的績效。"""
    closes = [100.0] * 30
    opens = [100.0] * 30
    # 第 1 天收 100,第 2 天開 110(跳空);之後高點都在 110 附近
    opens[1] = 110.0
    highs = [101.0] * 30
    for i in range(1, 30):
        highs[i] = 111.0
    lows = [99.0] * 30
    b = _bars(closes, opens, highs, lows)
    s = osc.realized_opportunity_stats(b, horizon=10, window=250)
    # 進場價若誤用 T0 收盤 100,MFE 會是 +11%;正確用 T+1 開盤 110 → 約 +0.9%
    assert s["insufficient"] or s["expected_upside"] < 5.0, s


def test_sector_rs_excludes_self():
    """LOO:自己不得進入自己的族群強度,否則是偽裝的個股動能。"""
    # 自己暴漲,同儕平盤 → LOO 後族群強度應接近 0
    seq_self = [100.0] * 10 + [200.0]
    seq_peer = [100.0] * 11
    bars = {"AAA": seq_self, "BBB": seq_peer, "CCC": seq_peer, "DDD": seq_peer}
    rs = osc.sector_rs_10d(bars, "AAA", 10)
    assert rs is not None and abs(rs) < 1e-9, rs
    # 反過來:同儕暴漲、自己平盤 → LOO 後應為正
    bars2 = {"AAA": seq_peer, "BBB": seq_self, "CCC": seq_self, "DDD": seq_self}
    assert osc.sector_rs_10d(bars2, "AAA", 10) > 0.9


def test_sector_rs_requires_minimum_peers():
    """同儕不足時回 None,不得用 2 檔推論族群狀態。"""
    bars = {"AAA": [100.0] * 11, "BBB": [100.0] * 11, "CCC": [100.0] * 11}
    assert osc.sector_rs_10d(bars, "AAA", 10) is None      # LOO 後只剩 2 檔


def test_all_six_metrics_present():
    """章程第 10 條:六項原始指標必須全部保留在資料層。"""
    b = _bars([100 + i for i in range(300)])
    s = osc.realized_opportunity_stats(b, horizon=10)
    for k in ("p_hit_3pct", "expected_upside", "expected_downside",
              "net_positive_rate", "profit_factor", "net_expectancy"):
        assert k in s, k


def test_extended_stage_goes_to_avoid():
    b = _bars([100 + i for i in range(300)])
    r = osc.score_one("9999", b, {}, sector_rank_pct=0.95, stage="EXTENDED")
    assert r["tier"] == "AVOID"


def test_evidence_level_is_not_a_buy_recommendation():
    """章程第 17 條:證據不足不得包裝成買進推薦,UI 必須顯示 evidence level。"""
    assert "PENDING LIVE" in osc.EVIDENCE_LEVEL
    b = _bars([100 + i for i in range(300)])
    r = osc.score_one("9999", b, {}, sector_rank_pct=0.95, stage=None)
    assert r["evidence_level"] == osc.EVIDENCE_LEVEL


def test_frozen_constants_pinned():
    """凍結參數不得被就地更動 —— 要改必須另開版本號。"""
    assert osc.COST == 0.00471
    assert osc.OPPORTUNITY_THRESHOLD == 0.03
    assert osc.SECTOR_TOP_PCT == 0.90
    assert osc.MIN_SECTOR_PEERS == 3
    assert osc.PRIMARY_POSITIVE_RATE == 55.0


# ══ Sidecar 架構(2026-08-24)════════════════════════════════════════

def test_insufficient_history_does_not_borrow_shared_constants():
    """個股歷史不足時,六項指標不得填入所有股票共用的條件值 ——
    那會讓 UI 看起來有個股分層,實際上沒有。"""
    short = _bars([100.0] * 15)          # 遠低於 MIN_TRAILING_N
    r = osc.score_one("9999", short, {}, sector_rank_pct=0.95, stage=None)
    assert r["display_stats_t10"]["stats_basis"] == "INSUFFICIENT_HISTORY"
    assert r["stock_level_available"] is False
    for k in ("p_hit_3pct", "expected_upside", "net_positive_rate", "profit_factor"):
        assert k not in r["conditional_stats_t10"], f"{k} 不該有值"
    # 只有族群層訊號是真實資訊,理由必須明說
    assert any("NOT YET AVAILABLE" in x for x in r["tier_reasons"])


def test_conditional_reference_is_reference_only():
    """條件參考值可保留供對照,但不得參與 ranking。"""
    short = _bars([100.0] * 15)
    r = osc.score_one("9999", short, {}, sector_rank_pct=0.95, stage=None)
    assert "conditional_reference" in r            # 保留供對照
    assert r["conditional_stats_t10"].get("p_hit_3pct") is None
    assert not hasattr(osc, "CONDITIONAL_FALLBACK")  # 舊的 ranking 用常數已移除


def test_coverage_contract_degrades_per_stock_not_pipeline():
    """契約失敗只降級該股票,不得讓整條盤後 pipeline 失敗。"""
    import tempfile, os
    import opportunity_history as oh
    db = os.path.join(tempfile.mkdtemp(), "h.db")
    # 日期必須貼近 production_date,否則會(正確地)觸發 staleness 檢查
    rows = [("AAA", f"2026-{m:02d}-{d:02d}", 10, 11, 9, 10, 1000, "t")
            for m in range(3, 9) for d in range(1, 21)]      # 3~8 月,120 天
    rows += [("BBB", "2026-08-20", 10, 11, 9, 10, 1000, "t")]   # 只有 1 天
    oh.rebuild_from_rows(rows, db)
    cov = oh.coverage_contract(["AAA", "BBB"], "2026-08-24", db)
    assert cov["AAA"]["ok"] is True
    assert cov["BBB"]["ok"] is False                 # 只有這檔被降級
    assert cov["_summary"]["ok_codes"] == 1          # 彙總照樣回傳,沒有拋例外


def test_sidecar_reads_oldest_to_newest():
    """sidecar 明確由舊到新 —— 與 store.read_recent 相反,這是曾出過的坑。"""
    import tempfile, os
    import opportunity_history as oh
    db = os.path.join(tempfile.mkdtemp(), "h.db")
    oh.rebuild_from_rows(
        [("AAA", f"2026-01-{d:02d}", 10, 11, 9, 10 + d, 1000, "t") for d in range(1, 6)], db)
    bars = oh.read_bars("AAA", "2026-01-31", 10, db)
    assert [b["data_date"] for b in bars] == sorted(b["data_date"] for b in bars)
    assert bars[-1]["close"] > bars[0]["close"]      # 最後一根是最新


def test_missing_sidecar_does_not_raise():
    """sidecar 不存在時回傳全部降級,不得拋例外拖垮盤後流程。"""
    import opportunity_history as oh
    cov = oh.coverage_contract(["AAA"], "2026-08-24", "/nonexistent/path.db")
    assert cov["_summary"]["store_missing"] is True
    assert cov["AAA"]["ok"] is False


# ══ As-of leakage 與 Static Stock Prior 防護(2026-08-24)═══════════

def test_unconditional_stats_never_decide_tier():
    """⚠ 最重要的一條:全歷史(unconditional)統計不得決定分層。
    「這檔過去勝率/PF 高 → 下一期仍強」= Static Stock Prior,已被否決。"""
    b = _bars([100 + i for i in range(300)])       # 一路上漲,unconditional 數字極好
    r = osc.score_one("9999", b, {}, sector_rank_pct=0.95, stage=None, signal_days=set())
    # unconditional 有值且標 DISPLAY_ONLY
    assert r["display_stats_t10"]["usage"] == "DISPLAY_ONLY"
    assert r["display_stats_t10"]["p_hit_3pct"] is not None
    # 但 conditional 無樣本 → 不得升級到 PRIMARY
    assert r["tier"] == "HIGH_POTENTIAL"
    assert any("NOT YET AVAILABLE" in x for x in r["tier_reasons"])


def test_conditioning_rule_is_reported():
    """每個指標都要能講出它的 conditioning 規則、n、horizon、成熟截止日。"""
    b = _bars([100 + i for i in range(300)])
    r = osc.score_one("9999", b, {}, 0.95, None, signal_days=set())
    d = r["display_stats_t10"]
    assert d["conditioning"] == "unconditional"
    assert d["horizon"] == 10
    assert d["outcome_matured_through"] is not None
    assert d["n"] > 0
    c = r["conditional_stats_t10"]
    assert c["conditioning"] == "conditional_on_frozen_signal"


def test_immature_samples_never_enter_statistics():
    """未走完 horizon 的樣本絕不進統計 —— 最後 horizon+1 根不得被計入。"""
    b = _bars([100 + i for i in range(100)])
    s = osc.realized_opportunity_stats(b, horizon=10)
    # 100 根、horizon=10:進場點 i=0..89(進場價取 bars[i+1].open,視窗 [i+1,i+11)),
    # 共 n-horizon = 90 個完整樣本。多一個就是把未成熟樣本算進去了。
    assert s["n"] == 100 - 10, s["n"]
    assert s["outcome_matured_through"] == b[100 - 10 - 1]["date"]
    # 再加 5 根未來 bar → 應多出 5 個成熟樣本,不多不少
    b2 = _bars([100 + i for i in range(105)])
    assert osc.realized_opportunity_stats(b2, horizon=10)["n"] == 105 - 10


def test_future_bars_do_not_change_todays_score():
    """as-of 不洩漏:把 score_date 之後的 bar 全部移除,當天分數必須不變。"""
    full = _bars([100 + i for i in range(200)])
    cut = 150
    truncated = full[:cut]
    a = osc.realized_opportunity_stats(truncated, horizon=10)
    b = osc.realized_opportunity_stats(full[:cut], horizon=10)
    assert a == b
    # 加上未來 bar 後,「截至同一天」的統計仍應相同
    c = osc.realized_opportunity_stats(full[:cut] , horizon=10)
    assert a["n"] == c["n"] and a["expected_upside"] == c["expected_upside"]


def _snap_row(code="9999", build="sidecar-1", hmax="2026-08-20"):
    return {"code": code, "tier": "WATCH", "tier_reasons": [],
            "conditional_stats_t10": {"n": 40, "outcome_matured_through": "2026-07-23"},
            "display_stats_t10": {}, "sidecar_build_id": build,
            "history_max_date": hmax, "score_date": "2026-08-24"}


def test_append_new_dates_is_never_blocked():
    """⚠ 已有 8/24 絕不得阻擋 8/25、8/26 寫入 —— 否則 live 累積會停住。"""
    import tempfile, os, datetime as dt
    import opportunity_snapshot as osnap
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    assert osnap.write_snapshot(dt.date(2026, 8, 24), [_snap_row()], db) == 1
    assert osnap.write_snapshot(dt.date(2026, 8, 25), [_snap_row()], db) == 1
    assert osnap.write_snapshot(dt.date(2026, 8, 26), [_snap_row()], db) == 1
    with osnap.store.conn(db) as c:
        n = c.execute(f"SELECT COUNT(DISTINCT data_date) FROM {osnap.TABLE}").fetchone()[0]
    assert n == 3


def test_same_day_rerun_with_identical_input_is_noop():
    """輸入完全相同 → idempotent no-op,不寫也不報錯。"""
    import tempfile, os, datetime as dt
    import opportunity_snapshot as osnap
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    assert osnap.write_snapshot(dt.date(2026, 8, 24), [_snap_row()], db) == 1
    assert osnap.write_snapshot(dt.date(2026, 8, 24), [_snap_row()], db) == 0   # no-op


def test_same_day_rerun_with_changed_sidecar_is_refused():
    """sidecar 版本或歷史截止日變了就重跑 → 拒絕,不得靜默覆寫當天樣本。"""
    import tempfile, os, datetime as dt
    import opportunity_snapshot as osnap
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    osnap.write_snapshot(dt.date(2026, 8, 24), [_snap_row(build="sidecar-1")], db)
    for changed in (_snap_row(build="sidecar-2"),
                    _snap_row(hmax="2026-08-22")):
        try:
            osnap.write_snapshot(dt.date(2026, 8, 24), [changed], db)
            assert False, "應該拒絕靜默覆寫"
        except osnap.SnapshotMutationRefused:
            pass


def test_retroactive_snapshot_write_is_refused():
    """舊日期不可變:已有更新的日期後,不得回頭改寫舊日期。"""
    import tempfile, os, datetime as dt
    import opportunity_snapshot as osnap
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    osnap.write_snapshot(dt.date(2026, 8, 24), [_snap_row()], db)
    osnap.write_snapshot(dt.date(2026, 8, 25), [_snap_row()], db)      # append 可以
    try:
        osnap.write_snapshot(dt.date(2026, 8, 24), [_snap_row(build="x")], db)
        assert False, "應該拒絕回溯覆寫"
    except osnap.RetroactiveWriteRefused:
        pass


def test_same_day_partial_backfill_appends_only_missing():
    """同日補跑:已存在的不動,只補當時漏掉的股票。"""
    import tempfile, os, datetime as dt
    import opportunity_snapshot as osnap
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    osnap.write_snapshot(dt.date(2026, 8, 24), [_snap_row("1111")], db)
    n = osnap.write_snapshot(dt.date(2026, 8, 24),
                             [_snap_row("1111"), _snap_row("2222")], db)
    assert n == 1        # 只寫入新的那檔


def test_audit_fields_are_stored():
    """五個不可變稽核欄位必須存進 snapshot。"""
    b = _bars([100 + i for i in range(300)])
    r = osc.score_one("9999", b, {}, 0.95, None, signal_days=set(),
                      audit={"score_date": "2026-08-24",
                             "history_max_date": "2026-08-20",
                             "sidecar_build_id": "sidecar-test"})
    assert r["score_date"] == "2026-08-24"
    assert r["history_max_date"] == "2026-08-20"
    assert r["sidecar_build_id"] == "sidecar-test"
    assert r["display_stats_t10"]["outcome_matured_through"] is not None
    assert r["conditional_stats_t10"]["n"] is not None


def test_snapshot_hash_covers_full_semantic_payload():
    """⚠ 只 hash tier + 六項指標不夠:底層 signal/mapping/scorer 改版但 tier
    恰好沒變時會假 no-op,歷史就被偷偷重寫。每一個語意欄位改動都必須讓
    hash 改變。"""
    import opportunity_snapshot as osnap
    base = {"data_date": "2026-08-24", "code": "9999",
            "frozen_signal_name": "sec_rs_10d@sector_median_rank_top10",
            "frozen_signal_version": "v1", "conditioning_version": "c1",
            "sector_id": "PCB材料", "sector_map_version": "map1",
            "sector_opportunity": 1, "raw_sector_signal": 0.0123,
            "sector_rank_pct": 1.0, "pa_stage": None,
            "tier": "PRIMARY", "tier_reasons": "x",
            "p_hit_3pct": 90.0, "expected_upside": 14.2,
            "expected_downside": -7.4, "net_positive_rate": 70.0,
            "profit_factor": 4.35, "net_expectancy": 6.3,
            "stats_sample_n": 40, "stats_basis": "per_stock_conditional_on_signal",
            "stats_conditioning": "conditional_on_frozen_signal",
            "stats_usage": "TIERING", "stock_level_available": 1,
            "sector_level_evidence": "REPLICATED", "stock_level_evidence": "DESCRIPTIVE_ONLY",
            "evidence_level": "REPLICATED — PENDING LIVE",
            "history_max_date": "2026-08-20", "outcome_matured_through": "2026-07-23",
            "sidecar_build_id": "sidecar-1", "score_version": "v1"}
    h0 = osnap._row_hash(base)

    # 每一個語意欄位單獨改動,hash 都必須不同
    for k in osnap._HASH_KEYS:
        mutated = dict(base)
        mutated[k] = "MUTATED" if not isinstance(base.get(k), (int, float)) else 999
        assert osnap._row_hash(mutated) != h0, f"{k} 改變後 hash 未變 —— 會造成假 no-op"

    # 關鍵情境:signal 改版但 tier 恰好相同 → 仍須偵測到
    same_tier_new_signal = dict(base, frozen_signal_version="v2")
    assert same_tier_new_signal["tier"] == base["tier"]
    assert osnap._row_hash(same_tier_new_signal) != h0

    # execution timestamp 不進 hash(否則永遠判不出 idempotent)
    assert osnap._row_hash(dict(base, created_at="2026-08-24T23:59:59")) == h0
