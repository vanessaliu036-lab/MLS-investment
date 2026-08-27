"""test_line_b.py — Line B 上 production 前的完整測試(2026-08-27)。

需要一份含真實 b_snapshot/daily_bar/inst_flow 的 db 當 fixture(來自
/opt/mls-screen/mls.db 的唯讀複本)。用環境變數 LINE_B_TEST_DB 指定路徑;
沒設就用預設路徑,檔案不存在的測試會自動 skip(不假裝通過)。

跑法: LINE_B_TEST_DB=/path/to/prod_fixture.db python3 -m pytest test_line_b.py -v
"""
from __future__ import annotations

import datetime as _dt
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import line_b_explain as explain
import line_b_watch_ledger as ledger
import line_b_live as live
import line_b_ledger_view as view
import run_line_b_ledger as runner

FIXTURE_DB = os.environ.get(
    "LINE_B_TEST_DB",
    "/private/tmp/claude-501/-Users-vanessaliu-Desktop-mls-intraday/"
    "3fbb0dc2-6628-45ac-a960-676f97255e14/scratchpad/prod_fixture.db",
)

_have_fixture = Path(FIXTURE_DB).exists()
needs_fixture = pytest.mark.skipif(not _have_fixture, reason=f"no fixture db at {FIXTURE_DB}")


@pytest.fixture
def db_copy(tmp_path):
    dst = tmp_path / "mls.db"
    shutil.copy(FIXTURE_DB, dst)
    return str(dst)


# ───────────────────────── 1. calibration / bucket boundary ─────────────────

def test_bucket_boundaries_are_half_open_correctly():
    # bins = [-100,-6,-3,-1.5,-0.5,0], (lo, hi] 半開區間——邊界值(-6/-3/-1.5/-0.5)
    # 都落在「離 0 較遠」那一格(lo < x <= hi 的 hi 端)。
    assert explain.bucket_label(-6.0) == "<-6%"
    assert explain.bucket_label(-5.999) == "-6~-3%"
    assert explain.bucket_label(-3.0) == "-6~-3%"
    assert explain.bucket_label(-2.999) == "-3~-1.5%"
    assert explain.bucket_label(-1.5) == "-3~-1.5%"
    assert explain.bucket_label(-1.499) == "-1.5~-0.5%"
    assert explain.bucket_label(-0.5) == "-1.5~-0.5%"
    assert explain.bucket_label(-0.4999) == "-0.5~0%"
    assert explain.bucket_label(-0.0001) == "-0.5~0%"
    assert explain.bucket_label(0) is None      # 已站上,不適用
    assert explain.bucket_label(0.5) is None
    assert explain.bucket_label(None) is None


def test_activation_probability_matches_table_exactly():
    assert explain.activation_probability(-0.3, True) == 0.640
    assert explain.activation_probability(-0.3, False) == 0.345
    assert explain.activation_probability(-2.0, True) == 0.142
    assert explain.activation_probability(-2.0, False) == 0.074
    assert explain.activation_probability(-10, False) == 0.009
    assert explain.activation_probability(0, True) is None
    assert explain.activation_probability(None, True) is None


def test_non_monotonic_cell_preserved_not_smoothed():
    """-1.5~-0.5% 這格 confirmed 反而比 unconfirmed 低——鎖死不准被「修好」。"""
    lo_conf = explain.activation_probability(-1.0, True)
    lo_unconf = explain.activation_probability(-1.0, False)
    assert lo_conf < lo_unconf, "非單調格被動過手腳,違反 2026-08-26/27 鎖定的口徑"


# ───────────────────────── 2. explain(): confirmed 不顯示機率 ────────────────

def test_confirmed_status_hides_activation_prob():
    row = dict(t1_prior_high=100, t1_ma20=95, current_price=101,
              flow_class="OPEN_POSITIVE", flow_confirm_magnitude=500,
              watch_mode_activated=1)
    exp = explain.explain(row, is_eod=False)
    assert exp["status"] == "CONFIRMED"
    assert exp["activation_prob"] is None
    assert "啟動已發生" in exp["system_sentence"]
    assert "已站上" in exp["system_sentence"]


def test_watch_closely_shows_live_calibrated_prob_not_mock():
    row = dict(t1_prior_high=432, t1_ma20=420, current_price=428,
              flow_class="OPEN_POSITIVE", flow_confirm_magnitude=820,
              watch_mode_activated=0)
    exp = explain.explain(row, is_eod=False)
    assert exp["status"] == "WATCH_CLOSELY"
    assert abs(exp["distance_pct"] - (-0.93)) < 0.01
    # distance -0.93% 落在 -1.5~-0.5% 格,confirmed=True → 0.139(13.9%),不是 mockup 的 35.3%
    assert exp["activation_prob"] == pytest.approx(0.139)
    assert exp["calibration_bucket"] == "-1.5~-0.5%"
    assert exp["calibration_version"] == explain.CALIBRATION_VERSION


# ───────────────────────── 3. flow staleness ─────────────────────────────────

def test_flow_stale_flag_changes_display_not_underlying_state():
    row = dict(t1_prior_high=432, t1_ma20=420, current_price=428,
              flow_class="OPEN_POSITIVE", flow_confirm_magnitude=820,
              watch_mode_activated=0)
    fresh = explain.explain(row, is_eod=False, flow_stale=False)
    stale = explain.explain(row, is_eod=False, flow_stale=True)
    assert "待更新" in stale["flow_display"]
    assert "待更新" not in fresh["flow_display"]
    # status/activation_prob 是已經凍結的 point-in-time 事實,不因為現在斷流而消失
    assert stale["status"] == fresh["status"] == "WATCH_CLOSELY"
    assert stale["activation_prob"] == fresh["activation_prob"]


def test_is_aflow_stale_thresholds():
    now = _dt.datetime(2026, 8, 27, 10, 0, 0)
    fresh_q = (now - _dt.timedelta(seconds=10)).isoformat()
    fresh_a = (now - _dt.timedelta(seconds=20)).isoformat()
    old_a = (now - _dt.timedelta(seconds=300)).isoformat()
    assert live.is_aflow_stale(fresh_q, fresh_a, now) is False
    assert live.is_aflow_stale(fresh_q, old_a, now) is True      # 超過 180 秒
    assert live.is_aflow_stale(fresh_q, None, now) is True        # 缺值視為 stale
    desynced_a = (now - _dt.timedelta(seconds=30)).isoformat()
    desynced_q = (now - _dt.timedelta(seconds=250)).isoformat()   # q/a 差超過 180 秒
    assert live.is_aflow_stale(desynced_q, desynced_a, now) is True


# ───────────────────────── 4. sort order (locked spec) ───────────────────────

def _row(code, status, magnitude, dist):
    return dict(code=code, source="C1C2_PASS", flow_class="OPEN_POSITIVE",
               flow_confirm_magnitude=magnitude,
               explain=dict(status=status, distance_pct=dist))


def test_group_sort_is_status_then_magnitude_not_distance():
    rows = [
        _row("A", "WATCH_CLOSELY", 100, -0.1),   # 近但資金小
        _row("B", "WATCH_CLOSELY", 900, -3.0),   # 遠但資金大 → 必須排在 A 前面
        _row("C", "CONFIRMED", 50, 0.5),
    ]
    ctx = view._finalize(rows, "2026-08-27")
    ordered_codes = [r["code"] for r in ctx["c1_c2_list"]]
    assert ordered_codes == ["C", "B", "A"], (
        "分區內主排序必須是 flow_confirm_magnitude,不是距離——排序規格被改回舊版了")


def test_top3_is_pure_magnitude_rank():
    rows = [
        _row("X", "WAIT", 300, -0.2),
        _row("Y", "WAIT", 999, -5.0),
        _row("Z", "WAIT", 10, -0.05),
    ]
    for r in rows:
        r["flow_class"] = "FLOW_FLIP"
    ctx = view._finalize(rows, "2026-08-27")
    assert [r["code"] for r in ctx["flow_confirmed_top3"]] == ["Y", "X", "Z"]


# ───────────────────────── 5. Intraday Discovery isolation ───────────────────

def test_intraday_discovery_excluded_from_c1_c2_bucket_and_labels_fixed():
    rows = [
        _row("D1", "WAIT", 500, -1.0),
    ]
    rows[0]["source"] = "INTRADAY_DISCOVERY"
    ctx = view._finalize(rows, "2026-08-27")
    assert ctx["c1_c2_list"] == []
    assert len(ctx["intraday_discovery"]) == 1
    # 64.1%/89.9% 是固定歷史母體標籤,不因為畫面上有沒有 discovery 列而變動
    assert ctx["labels"]["c1_c2_rate"] == "64.1%"
    assert ctx["labels"]["flow_confirmed_rate"] == "89.9%"


# ───────────────────────── 6. ledger append-only guards ─────────────────────

def test_ledger_noop_retroactive_and_mutation_guards(tmp_path):
    db = str(tmp_path / "guard.db")
    row = dict(code="2455", source="C1C2_PASS", c1_structure_intact=1,
              c2_selling_weak_price_resp=1, t1_close=100, t1_ma20=95,
              t1_prior_high=102, t1_inst_5d=-500, t1_price_5d=3.0,
              t1_close_position=0.8, flow_class="OPEN_POSITIVE",
              flow_confirm_magnitude=500, watch_mode_activated=0,
              activation_slot=None, t_high=101, t_low=98, t_close=100,
              eod_c1=1, eod_c2=1, enters_next_day_watchlist=1)

    r1 = ledger.write_rows(_dt.date(2026, 8, 13), [row], db)
    assert r1 == {"written": 1, "noop": 0}

    # same day, identical semantics → no-op
    r2 = ledger.write_rows(_dt.date(2026, 8, 13), [row], db)
    assert r2 == {"written": 0, "noop": 1}

    # same day, semantics changed → refused, not silently overwritten
    changed = dict(row, watch_mode_activated=1)
    with pytest.raises(ledger.LedgerMutationRefused):
        ledger.write_rows(_dt.date(2026, 8, 13), [changed], db)

    # older date than what's already committed → refused
    with pytest.raises(ledger.RetroactiveWriteRefused):
        ledger.write_rows(_dt.date(2026, 8, 12), [row], db)


# ───────────────────────── 7. real-data replay (needs fixture) ──────────────

@needs_fixture
@pytest.mark.parametrize("day", ["2026-08-13", "2026-08-14"])
def test_eod_replay_deterministic_against_real_data(db_copy, day):
    d = _dt.date.fromisoformat(day)
    r1 = runner.run(d, db_copy)
    assert "skipped" not in r1, f"{day}: {r1}"
    assert r1["candidates"] >= 0
    r2 = runner.run(d, db_copy)
    assert r2["written"] == 0
    assert r2["noop"] == r1["written"]


@needs_fixture
def test_live_merge_runs_against_real_data_without_writing(db_copy):
    before = Path(db_copy).stat().st_mtime_ns
    result = live.build_live_rows(db_copy, T="2026-08-25")
    after = Path(db_copy).stat().st_mtime_ns
    assert after == before, "live 合成層不准寫 DB"
    assert isinstance(result["rows"], list)
    for r in result["rows"]:
        assert r["source"] in ("C1C2_PASS", "INTRADAY_DISCOVERY")
        assert "explain" in r


@needs_fixture
def test_live_buffer_carries_freshness_fields(db_copy):
    """回歸測試:線上曾因為載到 2026-08-04 舊版 snapshot_producer(沒有 updated_at
    欄位)而讓 aflow_updated_at 恆為 None → 每一檔都被判 stale。live_buffer 現在
    直接下 SQL,必須永遠帶回 freshness 欄位。"""
    import sqlite3 as _s
    conn = _s.connect(db_copy)
    conn.execute("INSERT OR REPLACE INTO quote_snap (code,data_date,price,updated_at)"
                " VALUES ('2330','2026-08-27',1000.0,'2026-08-27T03:00:00')")
    conn.execute("INSERT OR REPLACE INTO aflow (code,data_date,net_active,method,updated_at)"
                " VALUES ('2330','2026-08-27',500.0,'bridge_8000','2026-08-27T03:00:00')")
    conn.commit(); conn.close()

    buf = live.live_buffer(db_copy, "2026-08-27")
    assert "2330" in buf
    tick = buf["2330"]
    assert tick["quote_updated_at"] == "2026-08-27T03:00:00"
    assert tick["aflow_updated_at"] == "2026-08-27T03:00:00", (
        "aflow_updated_at 掉了 → freshness 閘門會把所有股票誤判成 stale")
    assert tick["net_active"] == 500.0
    # 同一時刻不得判 stale
    assert live.is_aflow_stale(tick["quote_updated_at"], tick["aflow_updated_at"],
                              _dt.datetime(2026, 8, 27, 3, 0, 30)) is False


def test_discovery_row_shows_real_prices_and_keeps_direction_sign():
    """盤中發現的股票多半『已經站上』關鍵價。舊版只印 abs(distance) 的
    「距壓力 X%」會讓人以為還沒到、還要再漲——方向剛好相反。現在必須印出
    真實現價/壓力,而且區分『已站上』與『差』。"""
    import line_b_ledger_render as render

    above = {"code": "2408", "flow_confirm_magnitude": 7391,
            "explain": dict(current=547.0, resistance=522.0, distance_pct=4.79,
                           status="CONFIRMED", system_sentence="已站上關鍵價 522.0｜啟動已發生")}
    row = render._discovery_row(above)
    assert "547" in row and "522" in row, "必須印出真實現價與壓力價,不能只給百分比"
    assert "已站上" in row
    assert "差 <strong>4.79%" not in row, "已站上卻印成『差 4.79%』=方向講反"

    below = {"code": "2359", "flow_confirm_magnitude": 100,
            "explain": dict(current=147.0, resistance=147.5, distance_pct=-0.34,
                           status="CONFIRMED", system_sentence="已站上關鍵價 147.5｜啟動已發生")}
    row2 = render._discovery_row(below)
    assert "147" in row2
    assert "差" in row2 and "已站上 <strong>+" not in row2


def test_confirmed_flow_card_does_not_say_if_aflow_completes():
    """資金已確認的卡片不得再出現「若 A-flow 完成確認」——那會讓校準值與 89.9%
    讀起來像模型自相矛盾(Vanessa 2026-08-26 明確要求)。"""
    import line_b_ledger_render as render

    confirmed_exp = dict(status="WATCH_CLOSELY", activation_prob=0.621,
                        distance_pct=-0.3, confirmed_so_far=True)
    block = render._prob_block(confirmed_exp, discovery=False)
    assert "若 A-flow 完成確認" not in block
    assert "歷史母體參考" in block
    assert "資金已確認" in block

    unconfirmed_exp = dict(status="WAIT", activation_prob=0.345,
                          distance_pct=-0.3, confirmed_so_far=False)
    block2 = render._prob_block(unconfirmed_exp, discovery=False)
    assert "若 A-flow 完成確認" in block2
    assert "尚待資金確認" in block2


def test_confirmed_activated_card_shows_no_probability_number():
    import line_b_ledger_render as render
    exp = dict(status="CONFIRMED", activation_prob=None, distance_pct=1.2,
              confirmed_so_far=True)
    block = render._prob_block(exp, discovery=False)
    assert "已站上" in block
    assert "%</span>" not in block.split("confirm-line")[0].replace("89.9%", "")


@needs_fixture
def test_api_html_and_json_smoke(db_copy, monkeypatch):
    """掛進一個乾淨的 FastAPI app(不是整個 server.py,避免拉進其他外部依賴),
    確認 HTML 頁與 JSON 端點都回 200,且拿的到指定歷史日的資料。"""
    import sys as _sys
    for name in list(_sys.modules):
        if name == "line_b_ledger_api":
            del _sys.modules[name]
    # 先把 2026-08-25 的 EOD ledger 寫進這份 db 複本,API 才有東西可讀。
    r = runner.run(_dt.date(2026, 8, 25), db_copy)
    assert "skipped" not in r, r

    monkeypatch.setenv("MLS_LINE_B_DB_PATH", db_copy)
    monkeypatch.setenv("MLS_SCREEN_DIR", str(Path(__file__).parent))

    api_dir = Path(__file__).parent.parent / "個股卡片相關檔案_20260722"
    _sys.path.insert(0, str(api_dir))
    import line_b_ledger_api as api
    assert api.router is not None, "router 建置失敗,檢查 import 是否炸掉"

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)

    r_html = client.get("/line-b-ledger", params={"date": "2026-08-25"})
    assert r_html.status_code == 200
    assert "LIVE BUY POINT MONITOR" in r_html.text

    r_json = client.get("/line-b-ledger.json", params={"date": "2026-08-25"})
    assert r_json.status_code == 200
    body = r_json.json()
    assert body["data_date"] == "2026-08-25"
    assert body["is_live"] is False


@needs_fixture
def test_candidate_count_matches_independently_verified_sample(db_copy):
    """跟 2026-08-26 獨立重跑校準時的每日候選數對一次帳(那次是純讀,這次走
    production 的 run_line_b_ledger.run() 本尊)——用來確認兩條路徑一致,不是
    各自表述。"""
    total = 0
    for day in ("2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14",
               "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20",
               "2026-08-21", "2026-08-24", "2026-08-25"):
        r = runner.run(_dt.date.fromisoformat(day), db_copy)
        assert "skipped" not in r, f"{day}: {r}"
        total += r["candidates"]
    # 2026-08-26 獨立重跑校準時测到 77 個 C1+C2 stock-day(run_line_b_ledger.run
    # 的 candidates 計數口徑含 C1C2_PASS + INTRADAY_DISCOVERY,通常會 >= 77)
    assert total >= 77, f"候選數 {total} 明顯少於獨立校準的 77,兩套邏輯對不上帳"


# ─────────────── 七層交易狀態(獨立頁,DESCRIPTIVE ONLY)────────────────────

def test_extension_does_not_block_active_but_overrides_action():
    """2026-08-27 Vanessa 明確修正:漲太高不代表沒啟動。EXTENSION 不參與 ACTIVE
    判定,只在 ACTIVE 成立後把 TRADE STATE 覆寫成 EXTENDED、ACTION 改禁追。"""
    import line_b_layers as L
    trig = {"verdict": "YES", "hold_slots": 3}
    vol = {"verdict": "PASS"}
    acc = {"verdict": "YES", "vwap_held": True}
    chip, flow = {"verdict": "CONFIRMED"}, {"verdict": "STRONG"}

    normal = L.trade_state(chip, flow, trig, vol, acc,
                          {"verdict": "NORMAL", "reasons": []}, True, 1.0)
    assert normal["state"] == "ACTIVE"
    assert normal["action_code"] == "ENTRY_ELIGIBLE"

    high = L.trade_state(chip, flow, trig, vol, acc,
                        {"verdict": "HIGH", "reasons": ["today +9.8%"]}, True, 1.0)
    assert high["state"] == "EXTENDED"
    assert high["action_code"] == "NO_CHASE"
    # 關鍵:啟動事實仍然成立,不因為不能買就說它沒啟動
    assert high.get("activated") is True


def test_no_armed_high_state_exists():
    """ARMED 是生命週期、HIGH 是價格風險,不得黏在同一欄。"""
    import line_b_layers as L
    st = L.trade_state({"verdict": "CONFIRMED"}, {"verdict": "STRONG"},
                      {"verdict": "NO", "hold_slots": 0}, {"verdict": "THIN"},
                      {"verdict": "N/A"}, {"verdict": "HIGH", "reasons": ["x"]}, True, -1.0)
    assert st["state"] == "ARMED"
    assert "HIGH" not in st["state"]


def test_failed_when_price_falls_back_below_trigger():
    import line_b_layers as L
    st = L.trade_state({"verdict": "CONFIRMED"}, {"verdict": "STRONG"},
                      {"verdict": "NO", "hold_slots": 4},  # 曾突破,現在沒有
                      {"verdict": "PASS"}, {"verdict": "NO"},
                      {"verdict": "NORMAL", "reasons": []}, True, -0.5)
    assert st["state"] == "FAILED"


def test_turnover_is_never_fabricated_when_shares_unknown():
    """2026-08-27:Turnover 已改為實算(股數來自 TWSE/TPEx OpenAPI)。但抓不到
    股數時必須回 None 顯示「—」,絕不可用估算值頂替。"""
    import line_b_layers as L
    # 沒有股數 → 不得編
    v = L.volume_layer([{"slot": "1000", "volume": 500}], {}, "1000", issued_shares=None)
    assert v["turnover_pct"] is None
    assert v["turnover_note"]

    # 有股數 → 實算(500 張 = 500,000 股 ÷ 10,000,000 股 = 5%)
    v2 = L.volume_layer([{"slot": "1000", "volume": 500}], {}, "1000",
                       issued_shares=10_000_000)
    assert v2["turnover_pct"] == pytest.approx(5.0)
    assert v2["turnover_note"] is None


@needs_fixture
def test_layers_compute_runs_on_real_data_and_reports_rvol_base_days(db_copy):
    import line_b_layers as L
    res = L.compute(db_copy, T="2026-08-26")
    assert res["rows"], "應該要有列"
    for r in res["rows"]:
        assert r["state"]["state"] in {"WATCH", "ARMED", "ACTIVE", "EXTENDED", "FAILED", "REJECT"}
        # RVOL 母體天數一定要回報,不能讓人以為基準比實際可靠
        assert "rvol_base_days" in r["volume"]
        # 有股數就要算得出來;沒有就必須是 None,不得捏造
        v = r["volume"]
        if v.get("issued_shares"):
            assert v["turnover_pct"] is not None
        else:
            assert v["turnover_pct"] is None
