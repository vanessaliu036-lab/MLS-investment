from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOME = (ROOT / "intraday_decision_dataflow.html").read_text(encoding="utf-8")
VPS = (ROOT / "vps_intraday_test.py").read_text(encoding="utf-8")
WATCH = (ROOT / "個股第一層ＵＩ.html").read_text(encoding="utf-8")
DETAIL = (ROOT / "5483_中美晶_個股決策UI.html").read_text(encoding="utf-8")
SERVER = (ROOT / "個股卡片相關檔案_20260722" / "server.py").read_text(encoding="utf-8")


def test_home_table_stays_seven_fixed_cells_after_post_render_cleanup():
    assert "const headings=['狀態','股票','現價','漲跌','漲幅','成交量','盤中判讀']" in HOME
    assert "tr.innerHTML=`<td class=\"home-state\"" in HOME
    assert "直接依表頭建立六欄" not in HOME


def test_homepage_does_not_start_intraday_fetch_when_market_is_closed():
    assert "async function load(){if(paused||marketClosed())return;" in HOME
    assert "loadAfterHoursSnapshot()" in HOME
    assert "setInterval(forceOff,500)" not in HOME


def test_homepage_uses_compact_aligned_breadth_and_quote_layout():
    assert "data-mls-home-layout" in HOME
    assert 'grid-template-areas:"hero narrow" "gauge narrow" "meaning meaning" "triple triple"' in HOME
    assert "#rows>tr{min-height:0;height:auto" in HOME
    assert "#rows .home-state{display:flex;align-items:center;justify-content:flex-start;gap:5px;flex-wrap:nowrap" in HOME


def test_after_hours_intraday_endpoint_does_not_touch_live_broker_buffer():
    route = VPS[VPS.index('@router.get("/api/intraday-test")'):]
    guard = route.index('raw = broker.raw_buffer_snapshots()')
    assert "_intraday_session_open()" in route[:guard]


def test_homepage_uses_the_shared_navigation_and_is_not_a_second_overlay_page():
    assert 'id="reviewNav" class="mls-nav review-nav"' in HOME
    assert 'href="/line-b-layers">七層交易狀態</a>' in HOME
    assert 'html.home-route body>.topbar' in HOME
    assert 'html.review-route body>.layout>.content' in HOME
    assert "location.href=API_BASE+'/review'" in HOME
    assert "if(location.pathname!=='/review')" in HOME


def test_radar_view_waits_for_home_snapshot_before_rendering():
    assert "window._homepageDataReady=_startupLoad" in HOME
    assert "Promise.resolve(window._homepageDataReady).catch(()=>{}).then(()=>openPanel(m[1]))" in HOME


def test_radar_deep_link_hides_home_before_panel_rendering():
    assert "view==='radar'?'radar-route'" in HOME
    assert "html.radar-route body>.layout>.content{display:none!important}" in HOME
    assert "html.radar-route #sidePanel" in HOME
    assert "openPanel('radar')" in HOME


def test_watchpool_stock_navigation_preserves_watch_context():
    assert "'/api/card_page?code='+encodeURIComponent(code)+'&from=watch'" in WATCH


def test_stock_detail_marks_watchpool_as_the_active_context():
    assert 'href="/?view=watch" aria-current="page"' in DETAIL
    assert 'href="/" aria-current="page"' not in DETAIL


def test_card_page_without_a_stock_returns_to_watchpool():
    assert 'if not code: return RedirectResponse("/watch-first-layer", status_code=307)' in SERVER


def test_standalone_stock_detail_without_a_stock_returns_to_watchpool():
    assert "if(!code){location.replace(location.protocol==='file:'?'個股第一層ＵＩ.html':'/watch-first-layer');return;}" in DETAIL


def test_shared_navigation_does_not_render_the_non_actionable_section_label():
    navigation = (ROOT / "篩選邏輯" / "navigation.py").read_text(encoding="utf-8")
    assert 'return \'<aside class="mls-nav"' in navigation
    assert "盤中決策</span>" not in navigation[navigation.index("def nav_html"):]
