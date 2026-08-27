"""Pre-Activation 規則版:四階段判定與「不放假分數」的約束。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import pre_activation as pa


def base(**kw):
    d = dict(close=102.0, ma5=100.0, volume=1000.0, vol_ma20=1000.0,
             foreign_days=0, prev_high=103.0, high5=110.0)
    d.update(kw)
    return pa.describe(**d)


def test_stage_ladder_follows_money_then_volume_then_price():
    assert base(foreign_days=4, volume=900)["stage"] == pa.EARLY
    assert base(foreign_days=4, volume=1400)["stage"] == pa.ARMED
    assert base(foreign_days=4, volume=1400, close=104)["stage"] == pa.TRIGGER


def test_overheat_always_wins_and_is_marked_do_not_chase():
    """距 MA5 對 T+3 是負向且單調(F1 效果量 -1.0),所以過熱是禁追條件。"""
    for kw in (dict(close=112), dict(volume=3000), dict(close=115, high5=105)):
        r = base(foreign_days=4, **kw)
        assert r["stage"] == pa.EXTENDED, kw
        assert r["do_not_chase"] is True


def test_no_unvalidated_scores_are_emitted():
    """上線版不得輸出 Activation / Tradeability / Entry Confidence ——
    沒有模型依據的分數比沒有分數更危險。"""
    r = base(foreign_days=4)
    banned = [k for k in r if "score" in k.lower() or "confidence" in k.lower()]
    assert not banned, f"不該出現未驗證分數:{banned}"


def test_missing_data_is_reported_as_none_not_guessed():
    r = pa.describe(close=None, ma5=100, volume=None, vol_ma20=1000, foreign_days=None)
    assert r["foreign_state"] is None and r["volume_state"] is None
    assert r["ma5_state"] is None and r["stage"] == pa.WATCH


def test_foreign_streak_must_be_the_foreign_one_not_combined():
    """欄位語意鎖定:吃的是 foreign_days(外資),不是 consecutive_days(三法人合計)。"""
    import inspect
    src = inspect.getsource(pa.describe)
    assert "foreign_days" in src and "consecutive_days" not in src


def test_thresholds_are_pinned():
    assert (pa.FOREIGN_STRONG_DAYS, pa.VOL_RISING, pa.MA5_HOT) == (2, 1.2, 0.07)
    assert pa.MA5_HOT == 0.07      # 與引擎 HIGH_BIAS_PCT 一致


def test_limit_up_with_unconfirmed_volume_is_not_early():
    """2026-08-27 回報的 bug:漲停但量比未達門檻,曾被印成 EARLY
    ('資金先到、價格未動'＋'等待量能抬升 → ARMED'),把已經啟動的價格講成
    還沒動。價格 Activation 一旦確認(含漲停),就不可再落回 EARLY/WATCH。"""
    r = base(foreign_days=4, volume=900, is_limit_up=True)
    assert r["stage"] == pa.ACTIVE
    assert r["price_state"] == "漲停"
    assert r["price_activated"] is True
    assert r["volume_state"] == "未啟動"
    assert r["volume_confirmed"] is False
    assert r["volume_confirmation_state"] == "尚未確認"
    assert r["do_not_chase"] is True
    assert "價格未動" not in r["stage_note"]
    assert "ARMED" not in r["next_step"]


def test_change_rate_near_limit_also_overrides_even_without_limit_up_flag():
    """使用者要求:漲幅 >= 9.5% 就算技術上還沒鎖死漲停(盤中早段快速鎖漲停/
    跳動可能讓 is_limit_up 判定有時差),也要視為價格已啟動,不能停在 EARLY。"""
    r = base(foreign_days=4, volume=900, close=95, change_rate=9.6)
    assert r["stage"] == pa.ACTIVE
    assert r["price_state"] == "漲停"
    assert r["price_activated"] is True


def test_price_activation_overrides_even_without_prev_high_data():
    """is_limit_up 必須能單獨判定價格已啟動,不依賴 prev_high/high5 是否齊全
    (盤中早段這些欄位常缺)。"""
    r = pa.describe(close=110.0, ma5=105.0, volume=900.0, vol_ma20=1000.0,
                     foreign_days=4, prev_high=None, high5=None, is_limit_up=True)
    assert r["stage"] == pa.ACTIVE
    assert r["stage"] != pa.EARLY


def test_live_price_overlay_upgrades_stale_snapshot_without_confirming_volume():
    """盤中 API 貼回盤後 snapshot 時,當下價格啟動要覆蓋舊 stage,但量能不變。"""
    snapshot = {
        "stage": "EARLY", "stage_note": "資金先到、價格未動",
        "next_step": "等待量能抬升 → ARMED", "price_state": "整理",
        "price_activated": False, "volume_state": "未啟動",
        "do_not_chase": False,
    }
    r = pa.overlay_live_price_activation(snapshot, is_limit_up=True,
                                          change_rate=9.2)
    assert r["stage"] == pa.ACTIVE
    assert r["price_state"] == "漲停"
    assert r["price_activated"] is True
    assert r["volume_state"] == "未啟動"
    assert r["next_step"] == "不追,觀察是否鎖停／隔日承接"
    assert r["do_not_chase"] is True
    assert snapshot["stage"] == "EARLY"  # 純函式不改寫盤後快照


def test_live_price_overlay_uses_change_rate_fallback_and_keeps_extended():
    r = pa.overlay_live_price_activation(
        {"stage": "ARMED", "volume_state": "未啟動"},
        is_limit_up=False, change_rate=9.5)
    assert r["stage"] == pa.ACTIVE

    r = pa.overlay_live_price_activation(
        {"stage": pa.EXTENDED, "volume_state": "未啟動"},
        is_limit_up=True, change_rate=10.0)
    assert r["stage"] == pa.EXTENDED


def test_foreign_confirmation_uses_finmind_foreign_streak_and_keeps_source_date():
    snapshot = {"stage": pa.EARLY, "foreign_state": None, "volume_state": "未啟動"}
    r = pa.overlay_foreign_confirmation(snapshot, {
        "foreign_days": 3,
        "foreign_net_d": 420,
        "foreign_net_20d": 1850,
        "source": "FinMind TaiwanStockInstitutionalInvestorsBuySell",
        "source_date": "2026-08-26",
    })
    assert r["foreign_state"] == "轉強"
    assert r["foreign_days"] == 3
    assert r["foreign_net_d"] == 420
    assert r["foreign_source_date"] == "2026-08-26"
    assert "FinMind" in r["foreign_source"]
    assert r["stage"] == pa.EARLY
    assert snapshot["foreign_state"] is None


def test_trigger_next_step_does_not_imply_entry():
    """2026-08-24 大樣本回測(n=555)證明 TRIGGER 對 51 檔基準線沒有 entry
    edge —— 文案不得再暗示可進場(曾寫「盤中確認後可進場」)。"""
    r = base(foreign_days=4, volume=1400, close=104)
    assert r["stage"] == pa.TRIGGER
    for banned in ("可進場", "可買", "buy", "entry"):
        assert banned not in r["next_step"], r["next_step"]
        assert banned not in r["stage_note"], r["stage_note"]


def test_snapshot_writes_facts_not_predictions():
    """快照只記 stage 與當下事實,不得寫入任何預測分數。"""
    import sqlite3, tempfile, os
    import pa_snapshot
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    pa_snapshot.ensure(db)
    cols = [r[1] for r in sqlite3.connect(db).execute("pragma table_info(pa_snapshot)")]
    banned = [c for c in cols if "score" in c or "confidence" in c or "prob" in c]
    assert not banned, f"快照表不該有預測欄位:{banned}"
    for need in ("stage", "entry_open", "net_t3", "net_t5", "net_t7", "mfe_t7", "mae_t7"):
        assert need in cols, need


def test_backfill_uses_next_day_open_as_entry():
    """盤後名單在 T0 收盤買不到 —— 進場價必須是 T+1 開盤,
    用收盤會把隔夜跳空(實測約 +0.94%/日)算成自己的績效。"""
    import sqlite3, datetime as dt, tempfile, os
    import pa_snapshot
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    pa_snapshot.ensure(db)
    c = sqlite3.connect(db)
    c.executescript("CREATE TABLE IF NOT EXISTS daily_bar (code TEXT, data_date TEXT,"
                    " open REAL, high REAL, low REAL, close REAL);")
    for i, d in enumerate(["2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
                           "2026-08-29", "2026-09-01", "2026-09-02", "2026-09-03"]):
        c.execute("INSERT INTO daily_bar VALUES (?,?,?,?,?,?)",
                  ("9999", d, 100.0 + i, 102.0 + i, 99.0 + i, 101.0 + i))
    c.commit()
    pa_snapshot.write_snapshot(dt.date(2026, 8, 24), [{
        "code": "9999", "close": 99.0, "continuation": 50.0, "legacy_rank": 1,
        "pre_activation": {"stage": "ARMED", "do_not_chase": False,
                           "rule_version": "v1"}}], db)
    pa_snapshot.backfill(db)
    row = dict(zip([r[1] for r in c.execute("pragma table_info(pa_snapshot)")],
                   c.execute("SELECT * FROM pa_snapshot").fetchone()))
    assert row["entry_open"] == 100.0                     # T+1 開盤,不是 T0 收盤 99
    assert round(row["ret_t3"], 3) == round((103.0 / 100.0 - 1) * 100, 3)
    assert round(row["net_t3"], 3) == round(row["ret_t3"] - 0.471, 3)


def test_report_refuses_to_conclude_on_thin_samples():
    """樣本不足時只列出、不下結論 —— live baseline 剛開始收,
    前幾週用十幾筆去判斷 stage 好壞會比沒有資料更糟。"""
    import tempfile, os, datetime as dt, sqlite3
    import pa_snapshot, pa_report
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    pa_snapshot.ensure(db)
    c = sqlite3.connect(db)
    for i in range(5):
        c.execute("INSERT INTO pa_snapshot (data_date,code,stage,net_t5) VALUES (?,?,?,?)",
                  (f"2026-08-{24+i}", f"900{i}", "ARMED", 1.0))
    c.commit()
    s = pa_report.by_stage(db)["ARMED"]["T+5"]
    assert s["n"] == 5 and s["enough"] is False
    assert "樣本不足" in pa_report.summary_text(db)
    assert pa_report.MIN_N == 20
