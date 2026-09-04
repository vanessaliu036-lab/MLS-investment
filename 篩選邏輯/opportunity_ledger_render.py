"""Opportunity Ledger —— HTML 渲染,純模板。

只吃 `opportunity_ledger_view.build_ledger_context()` 的輸出,逐欄位貼進
HTML。不判斷 tier、不算 evidence、不比大小、不排序 —— 這些全部已經在
context 裡決定好了。這支檔案唯一的職責是「怎麼排版」。
"""
from __future__ import annotations
import html as _html
from typing import Optional

from navigation import NAV_CSS, nav_html

def _load_name_map() -> dict:
    """用檔案路徑明確載入 config.py 的 NAME,不用 `import config`。

    ⚠ 8000 站(個股卡片相關檔案_20260722/)自己也有一支 config.py。若這裡
    寫 `import config`,一旦該進程先載入過同名模組,Python 的 sys.modules
    快取會讓這裡拿到「別人的」config,不是這個目錄的——結果是 NAME 查不到
    半檔,UI 顯示成「代號 代號」而不是公司名。用 importlib 指名檔案路徑
    載入,完全繞開模組名稱衝突。
    """
    import importlib.util
    from pathlib import Path
    path = Path(__file__).with_name("config.py")
    try:
        spec = importlib.util.spec_from_file_location("_opportunity_ledger_config", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.NAME
    except Exception:
        return {}


NAME = _load_name_map()


def _esc(s) -> str:
    return _html.escape(str(s)) if s is not None else ""


def _card_html(card: dict) -> str:
    code = card["code"]
    name = NAME.get(code, code)
    tier_class = card["tier"].lower()

    ev_badges = (
        f'<span class="ev-badge ev-sector"><span class="dot"></span>'
        f'Sector Evidence: {_esc(card["sector_level_evidence"])}</span>'
        f'<span class="ev-badge ev-stock"><span class="dot"></span>'
        f'Stock Evidence: {_esc(card["stock_level_evidence"])}</span>'
    )

    op_note = ""
    if card.get("operational_note"):
        op_note = (f'<div class="op-note">{_esc(card["tier_label"])} · '
                   f'{_esc(card["operational_note"])}</div>')

    if card["stock_level_state"] == "available":
        metric_cells = "".join(
            f'<div class="metric{" lead" if i < 2 else ""}">'
            f'<span class="m-label">{_esc(m["label"])}</span>'
            f'<span class="m-value">{_esc(m["value"])}</span></div>'
            for i, m in enumerate(card["metrics"])
        )
        metrics_block = (f'<div class="metrics">{metric_cells}</div>'
                         f'<div class="stock-caveat">{_esc(card["stock_level_caveat"])}</div>')
    else:
        metrics_block = f'<div class="insufficient-note">{_esc(card["insufficient_note"])}</div>'

    reasons_html = "<br>".join(_esc(r) for r in card["tier_reasons"] if r)

    return f'''
      <article class="card" data-tier="{tier_class}">
        <div class="card-id">
          <span class="name">{_esc(name)}</span><span class="code">{_esc(code)}</span>
          <span class="sector">{_esc(card["sector_id"])}</span>
        </div>
        {op_note}
        <div class="evidence-row">{ev_badges}</div>
        {metrics_block}
        <div class="why">{reasons_html}</div>
      </article>'''


def _tier_section_html(tier_block: dict) -> str:
    tier = tier_block["tier"]
    cards = tier_block["cards"]
    if not cards:
        cards_html = '<p class="empty-tier">今日無此分層</p>'
    else:
        cards_html = '<div class="card-grid">' + "".join(_card_html(c) for c in cards) + "</div>"
    return f'''
    <section class="tier-section" data-tier="{tier.lower()}">
      <div class="tier-head">
        <h2>{_esc(tier_block["label"])}</h2>
        <span class="count">{len(cards)} 檔</span>
      </div>
      {cards_html}
    </section>'''


def _live_evidence_html(le: Optional[dict]) -> str:
    if not le:
        return ""
    h10, h15 = le["horizons"][10], le["horizons"][15]
    return f'''
  <div class="live-strip">
    <div class="live-cell">
      <span class="label">Live Since</span>
      <span class="value">{_esc(le["live_since"])}</span>
    </div>
    <div class="live-cell">
      <span class="label">Matured T+10</span>
      <span class="value">n = {h10["n"]}</span>
      <span class="sub">{_esc(h10["status"])}</span>
    </div>
    <div class="live-cell">
      <span class="label">Matured T+15</span>
      <span class="value">n = {h15["n"]}</span>
      <span class="sub">{_esc(h15["status"])}</span>
    </div>
    <div class="live-cell">
      <span class="label">Frozen Signal</span>
      <span class="value" style="font-size:.92rem">{_esc(le["frozen_signal_name"])}</span>
      <span class="sub">{_esc(le["frozen_signal_version"])}</span>
    </div>
  </div>'''


_STYLE = '''
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600;8..60,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
  :root{
    --bg:#f4f6fb; --surface:#ffffff; --surface-2:#f8f9fc;
    --ink:#182033; --ink-soft:#65718a; --ink-faint:#8a96aa;
    --line:#e5e9f2; --line-strong:#cfd7e5;
    --accent:#c78313; --accent-soft:#fff6df; --accent-ink:#68551a;
    --avoid-wash:#f3f5f8;
    --shadow:0 12px 30px rgba(25,39,71,.08);
  }
  *{box-sizing:border-box}
  html,body{max-width:100%; min-width:0; overflow-x:hidden}
  body{margin:0; background:var(--bg); color:var(--ink); font-family:"IBM Plex Sans",system-ui,sans-serif; line-height:1.5; padding:2.5rem 1.5rem 5rem}
  .wrap{width:min(100%,1180px); margin:0 auto}
  h1,h2{font-family:"Source Serif 4",Georgia,serif; text-wrap:balance; margin:0}
  .kicker{font-family:"IBM Plex Mono",monospace; font-size:.72rem; letter-spacing:.09em; text-transform:uppercase; color:var(--ink-faint)}
  header.page{margin-bottom:1.75rem}
  header.page h1{font-size:2rem; font-weight:600; margin-top:.35rem}
  header.page p{color:var(--ink-soft); max-width:62ch; margin:.6rem 0 0; font-size:.95rem}
  .live-strip{display:grid; grid-template-columns:repeat(4,1fr); background:var(--surface); border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow); margin:1.75rem 0 2.5rem; overflow:hidden}
  .live-cell{min-width:0; padding:1rem 1.25rem; border-left:1px solid var(--line)}
  .live-cell:first-child{border-left:none}
  .live-cell .label{font-family:"IBM Plex Mono",monospace; font-size:.68rem; letter-spacing:.07em; text-transform:uppercase; color:var(--ink-faint); display:block; margin-bottom:.35rem}
  .live-cell .value{font-family:"IBM Plex Mono",monospace; font-size:1.15rem; font-weight:500; font-variant-numeric:tabular-nums; overflow-wrap:anywhere}
  .live-cell .sub{display:block; font-size:.78rem; color:var(--ink-soft); margin-top:.2rem}
  .tier-section{margin-bottom:2.75rem}
  .tier-head{display:flex; align-items:baseline; gap:.85rem; padding-bottom:.6rem; border-bottom:2px solid var(--ink); margin-bottom:1.1rem}
  .tier-head h2{font-size:1.3rem}
  .tier-head .count{font-family:"IBM Plex Mono",monospace; font-size:.8rem; color:var(--ink-faint)}
  .tier-section[data-tier="avoid"] .tier-head{border-bottom-style:dashed; border-bottom-color:var(--line-strong)}
  .tier-section[data-tier="avoid"] .card-grid{opacity:.72}
  .card-grid{display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:1rem}
  .empty-tier{color:var(--ink-faint); font-size:.85rem; font-style:italic}
  .card{background:var(--surface); border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow); padding:1.1rem 1.2rem 1.25rem; display:flex; flex-direction:column; gap:.65rem; border-left-width:4px; border-left-style:solid}
  .card[data-tier="primary"]{border-left-color:var(--ink)}
  .card[data-tier="high_potential"]{border-left-color:var(--ink-soft)}
  .card[data-tier="watch"]{border-left-color:var(--line-strong); border-left-style:dashed}
  .card[data-tier="avoid"]{border-left-color:var(--line); background:var(--avoid-wash)}
  .card-id{display:flex; align-items:baseline; gap:.5rem}
  .card-id .code{font-family:"IBM Plex Mono",monospace; font-size:.82rem; color:var(--ink-faint)}
  .card-id .name{font-family:"Source Serif 4",serif; font-size:1.15rem; font-weight:600}
  .card-id .sector{margin-left:auto; font-size:.76rem; color:var(--ink-soft)}
  .op-note{font-size:.72rem; color:var(--accent-ink); background:var(--accent-soft); border:1px solid var(--accent); border-radius:3px; padding:.3rem .5rem; font-family:"IBM Plex Mono",monospace; letter-spacing:.01em}
  .evidence-row{display:flex; gap:.4rem; flex-wrap:wrap}
  .ev-badge{display:inline-flex; align-items:center; gap:.35rem; font-family:"IBM Plex Mono",monospace; font-size:.64rem; letter-spacing:.02em; padding:.28rem .5rem; border-radius:3px; line-height:1.25}
  .ev-badge .dot{width:.5rem; height:.5rem; border-radius:50%; flex:none}
  .ev-sector{background:var(--accent-soft); color:var(--accent-ink); border:1px solid var(--accent)}
  .ev-sector .dot{background:var(--accent)}
  .ev-stock{background:transparent; color:var(--ink-soft); border:1px solid var(--line-strong)}
  .ev-stock .dot{border:1.5px solid var(--ink-soft); background:transparent}
  .metrics{display:grid; grid-template-columns:1fr 1fr; gap:.5rem .6rem; padding:.7rem 0 .4rem; border-top:1px solid var(--line)}
  .metric .m-label{display:block; font-family:"IBM Plex Mono",monospace; font-size:.62rem; letter-spacing:.03em; text-transform:uppercase; color:var(--ink-faint); margin-bottom:.15rem}
  .metric .m-value{font-family:"IBM Plex Mono",monospace; font-variant-numeric:tabular-nums; font-weight:500; font-size:1rem}
  .metric.lead .m-value{font-size:1.2rem; font-weight:600}
  .stock-caveat{font-family:"IBM Plex Mono",monospace; font-size:.66rem; color:var(--ink-faint); padding-bottom:.3rem; border-bottom:1px solid var(--line)}
  .insufficient-note{font-size:.8rem; color:var(--ink-faint); font-style:italic; padding:.6rem 0; border-top:1px solid var(--line); border-bottom:1px solid var(--line)}
  .why{font-size:.8rem; color:var(--ink-soft)}
  footer{margin-top:3rem; padding-top:1.25rem; border-top:1px solid var(--line); font-size:.78rem; color:var(--ink-faint); max-width:70ch}
  @media (max-width:720px){ body{padding:1rem .75rem 2rem} .live-strip{grid-template-columns:1fr 1fr} .live-cell:nth-child(3){border-left:none} .live-cell:nth-child(n+3){border-top:1px solid var(--line)} .live-cell .value{font-size:1rem} .card-id{align-items:flex-start; flex-wrap:wrap} .card-id .sector{margin-left:0; width:100%} }
</style>'''


def render_ledger_html(ctx: dict) -> str:
    if not ctx.get("data_date"):
        return f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>機會分層觀察榜</title>{_STYLE}{NAV_CSS}</head><body>{nav_html("opportunity")}
<main class="wrap"><p>尚無 opportunity_snapshot 資料。</p></main>
</body></html>'''

    tiers_html = "".join(_tier_section_html(t) for t in ctx["tiers"])
    live_html = _live_evidence_html(ctx["live_evidence"])

    return f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>機會分層觀察榜</title>{_STYLE}{NAV_CSS}</head><body>
{nav_html("opportunity")}
<div class="wrap">
  <header class="page">
    <span class="kicker">MLS · Opportunity Ledger · {_esc(ctx["data_date"])}</span>
    <h1>機會分層觀察榜</h1>
    <p>族群訊號(sec_rs_10d)已獨立窗複現;個股層六項數字是訊號觸發當下的
    conditional historical statistic,尚未經過走勢外驗證,一律標示為
    DESCRIPTIVE ONLY,不是預測機率,也不是買進建議。</p>
  </header>
  {live_html}
  {tiers_html}
</div></body></html>'''
