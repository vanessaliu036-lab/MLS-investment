from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "intraday_decision_dataflow.html").read_text(encoding="utf-8")
SERVER = (ROOT / "個股卡片相關檔案_20260722" / "server.py").read_text(encoding="utf-8")


def test_review_deep_link_uses_a_review_route_and_opens_review_panel():
    assert '@app.get("/review")' in SERVER
    assert "content.replace('href=\"/?view=review\"', 'href=\"/review\"')" in SERVER
    assert "const path=location.pathname" in HTML
    wrapper_start = HTML.index("window.openFeature=async function(kind)")
    wrapper = HTML[wrapper_start : wrapper_start + 1300]
    assert "if(kind==='review'&&location.pathname!=='/review')" in wrapper
    assert "await originalOpenFeature(kind);" in wrapper
    assert "if(kind==='review'&&typeof window.renderDualReview==='function')" in wrapper
    assert "await window.renderDualReview();" in wrapper
    assert "return;" in wrapper
    assert "async function openReview()" in HTML
    assert "async function loadWatchVerify()" in HTML
    assert "當時篩選理由" in HTML
    assert "T+1收盤" in HTML
    assert "判定" in HTML
    assert "九欄合併表" not in HTML
