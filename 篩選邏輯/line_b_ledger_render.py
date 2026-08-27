"""Line B Watch Ledger — HTML 渲染,純模板。

只吃 `line_b_ledger_view.build_ledger_context()` 的輸出(已經呼叫過
`line_b_explain.explain()` 產生白話句子、status、activation_prob),逐欄位
貼進 HTML。不判斷、不排序、不算——這支唯一職責是排版。

⚠ 「目前預估啟動機率」是 `line_b_explain.activation_probability()` 從 77 個
   C1+C2 stock-day、11 個乾淨交易日、1710 個 pre-activation snapshot 直接對
   /opt/mls-screen/mls.db 產本資料重算出的真實查表值,point-in-time causal
   正確(逐格用當下已知的資金狀態,不拿收盤後才知道的整天結論回頭貼標),
   **不是**憑感覺給的分數。它回答的唯一問題是「今天最終會不會進入
   WATCH MODE」,跟「啟動後會不會賺」是兩件不能混的事(2026-08-26 Vanessa
   明確區分)——後者要等 forward MFE/MAE 累積才能做,現在完全不顯示。
   已確認(CONFIRMED)或盤中發現(INTRADAY_DISCOVERY,母體不同不能套用同一張
   校準表)一律不顯示這個數字。有效樣本數是 77(stock-day)/11(day)層級,
   不是 1710——snapshot 之間同股同天高度相關,不當獨立樣本數用。
"""
from __future__ import annotations
import html as _html


def _load_name_map() -> dict:
    import importlib.util
    from pathlib import Path
    path = Path(__file__).with_name("config.py")
    try:
        spec = importlib.util.spec_from_file_location("_line_b_ledger_config", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.NAME
    except Exception:
        return {}


NAME = _load_name_map()

STATUS_CSS = {"WAIT": "", "WATCH_CLOSELY": "watch", "CONFIRMED": "ok", "GIVE_UP": "give-up"}
STATUS_BADGE_TEXT = {"WAIT": "等待資金", "WATCH_CLOSELY": "接近確認",
                     "CONFIRMED": "A-flow 已確認", "GIVE_UP": "暫時放棄"}


def _esc(s) -> str:
    return _html.escape(str(s)) if s is not None else ""


def _fmt(v, decimals=1) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def _bar_pct(distance_pct) -> int:
    """距離關鍵價轉成 0-100 的長條寬度。>=0(已站上)封頂 100;
    <=-6% 記為 0;中間線性內插。純距離,不是機率。"""
    if distance_pct is None:
        return 0
    if distance_pct >= 0:
        return 100
    return max(0, round(100 + distance_pct / 6.0 * 100))


def _quote_line(exp: dict) -> str:
    cur, res, dist = exp.get("current"), exp.get("resistance"), exp.get("distance_pct")
    if res is None:
        return f'現價 <strong>{_fmt(cur)}</strong>'
    if dist is not None and dist >= 0:
        return f'現價 <strong>{_fmt(cur)}</strong>｜壓力 <strong>{_fmt(res)}</strong>｜已站上 <strong>+{dist:.2f}%</strong>'
    d = f'{abs(dist):.2f}%' if dist is not None else '—'
    return f'現價 <strong>{_fmt(cur)}</strong>｜壓力 <strong>{_fmt(res)}</strong>｜差 <strong>{d}</strong>'


def _prob_block(exp: dict, discovery: bool) -> str:
    status = exp["status"]
    prob = exp.get("activation_prob")

    if discovery:
        prob_num_html = '<span class="prob-num">LIVE</span>'
        bar_pct = _bar_pct(exp.get("distance_pct"))
        ref_line = "INTRADAY DISCOVERY：不套用 64.1% / 89.9% 歷史參考(母體不同)"
        title = "距離關鍵價"
    elif status == "CONFIRMED":
        prob_num_html = '<span class="prob-num" style="color:var(--green)">已站上</span>'
        bar_pct = 100
        ref_line = 'A-flow 已完成確認：歷史參考 <b>89.9%</b>'
        title = "目前狀態"
    else:
        pct = round(prob * 100) if prob is not None else None
        color = ' style="color:var(--green)"' if (pct is not None and pct >= 70) else ""
        prob_num_html = f'<span class="prob-num"{color}>{pct}%</span>' if pct is not None else '<span class="prob-num">—</span>'
        bar_pct = pct if pct is not None else 0
        ref_line = f'若 A-flow 完成確認：歷史參考 <b>89.9%</b>'
        title = "目前預估啟動機率"

    return f"""<div class="prob">
        <div class="prob-top"><span class="prob-title">{title}</span>{prob_num_html}</div>
        <div class="bar"><i style="width:{bar_pct}%"></i></div>
        <div class="confirm-line">{ref_line}</div>
      </div>"""


def _stock_card(r: dict, discovery: bool = False) -> str:
    code = r["code"]
    name = NAME.get(code, code)
    exp = r["explain"]
    status = exp["status"]
    css = STATUS_CSS.get(status, "")
    badge_text = "盤中發現" if discovery else STATUS_BADGE_TEXT.get(status, exp["status_label"])
    card_css = "confirmed" if status == "CONFIRMED" else ""
    action_css = "ok" if status == "CONFIRMED" else ""

    return f"""
    <article class="stock-card {card_css}">
      <div class="stock-top">
        <div class="stock-name"><small>{_esc(code)}</small>{_esc(name)}</div>
        <div class="status {css}">{_esc(badge_text)}</div>
      </div>
      <div class="action {action_css}">{_esc(exp["system_sentence"])}</div>
      <div class="quote">{_quote_line(exp)}</div>
      <div class="row"><span class="label">昨日</span><span class="value">{_esc(exp["chip_summary"])}</span></div>
      <div class="row"><span class="label">今日</span><span class="value">{_esc(exp["flow_display"])}</span></div>
      {_prob_block(exp, discovery)}
    </article>"""


def _top3_row(rank: int, r: dict) -> str:
    exp = r["explain"]
    code, name = r["code"], NAME.get(r["code"], r["code"])
    flow_css = "up" if (r.get("flow_confirm_magnitude") or 0) >= 0 else "down"
    return (f'<div class="top3-row"><div class="rank">{rank}</div>'
           f'<div><b>{_esc(code)} {_esc(name)}</b></div>'
           f'<div>{_fmt(exp.get("current"))} / {_fmt(exp.get("resistance"))}</div>'
           f'<div class="{flow_css}">{_fmt(r.get("flow_confirm_magnitude"), 0)}</div>'
           f'<div>{_esc(exp["system_sentence"])}</div></div>')


def _discovery_row(r: dict) -> str:
    exp = r["explain"]
    code, name = r["code"], NAME.get(r["code"], r["code"])
    dist = exp.get("distance_pct")
    dist_txt = f'{abs(dist):.2f}%' if dist is not None else "—"
    return f"""
    <div class="discovery-item">
      <div><b>{_esc(code)} {_esc(name)}</b><br>
        <span style="color:var(--muted)">A-flow {_fmt(r.get("flow_confirm_magnitude"),0)} · 距壓力 {dist_txt}</span></div>
      <span class="badge">INTRADAY DISCOVERY</span>
      <span>收盤重算</span>
    </div>"""


PAGE_CSS = """
<style>
:root{--bg:#0a0c0f;--panel:#11151a;--panel2:#151a20;--line:#252c34;--text:#f4f6f8;--muted:#8b96a3;
--green:#56d992;--green-soft:rgba(86,217,146,.10);--amber:#f2b95f;--amber-soft:rgba(242,185,95,.10);
--blue:#74b8ff;--red:#ff7777;--red-soft:rgba(255,119,119,.10)}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(180deg,#090b0e 0%,#0d1014 100%);color:var(--text);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",Roboto,Arial,sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding:28px 20px 50px}
.header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:22px}
.title{font-size:25px;font-weight:760;letter-spacing:-.3px;margin:0 0 6px}
.sub{font-size:13px;color:var(--muted)}
.live{font-size:12px;color:var(--green);padding-top:4px}
.summary{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px}
.summary-card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:15px 17px}
.summary-label{font-size:11px;color:var(--muted);margin-bottom:7px}
.summary-value{font-size:27px;font-weight:800;line-height:1}
.summary-hint{font-size:11px;color:var(--muted);margin-top:6px}
.section-title{display:flex;justify-content:space-between;align-items:end;margin:24px 0 12px}
.section-title h2{font-size:14px;margin:0;font-weight:750}
.section-title span{font-size:11px;color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.stock-card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;box-shadow:0 16px 30px rgba(0,0,0,.14)}
.stock-card.confirmed{border-color:rgba(86,217,146,.42);box-shadow:inset 0 0 0 1px rgba(86,217,146,.06),0 16px 30px rgba(0,0,0,.14)}
.stock-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:15px}
.stock-name{font-size:18px;font-weight:800;letter-spacing:.1px}
.stock-name small{font-size:12px;color:var(--muted);font-weight:600;margin-right:7px}
.status{font-size:10px;font-weight:800;border:1px solid var(--line);border-radius:999px;padding:5px 8px;white-space:nowrap;color:var(--amber);background:var(--amber-soft)}
.status.ok{color:var(--green);background:var(--green-soft);border-color:rgba(86,217,146,.28)}
.status.watch{color:var(--blue);background:rgba(116,184,255,.10);border-color:rgba(116,184,255,.28)}
.status.give-up{color:var(--red);background:var(--red-soft);border-color:rgba(255,119,119,.28)}
.quote{font-size:13px;color:#dce2e8;margin-bottom:13px;line-height:1.65}
.quote strong{font-size:15px;color:#fff}
.row{display:flex;gap:8px;align-items:center;font-size:13px;line-height:1.8;flex-wrap:wrap}
.row .label{color:var(--muted);min-width:44px}
.row .value{font-weight:650}
.up{color:var(--green)}
.down{color:var(--red)}
.prob{margin-top:15px;padding:13px 14px;background:var(--panel2);border:1px solid var(--line);border-radius:12px}
.prob-top{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:9px}
.prob-title{font-size:12px;color:var(--muted)}
.prob-num{font-size:19px;font-weight:850}
.bar{height:6px;background:#252b32;border-radius:999px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,#6d7b89,#56d992)}
.confirm-line{font-size:12px;color:var(--muted);margin-top:9px}
.confirm-line b{color:var(--green);font-size:14px}
.action{margin:0 0 14px;padding:11px 12px;border:1px solid rgba(245,181,80,.28);background:var(--amber-soft);border-radius:11px;font-size:14px;font-weight:780;line-height:1.55}
.action.ok{border-color:rgba(86,217,146,.30);background:var(--green-soft);color:var(--green)}
.top3{margin-top:10px;background:var(--panel);border:1px solid var(--line);border-radius:16px;overflow:hidden}
.top3-row{display:grid;grid-template-columns:48px 1.3fr .9fr .9fr 1.4fr;gap:12px;align-items:center;padding:13px 16px;border-bottom:1px solid var(--line);font-size:12px}
.top3-row:last-child{border-bottom:0}
.top3-head{color:var(--muted);background:#0f1317;font-size:10px;font-weight:700}
.rank{width:26px;height:26px;border-radius:8px;display:flex;align-items:center;justify-content:center;background:#1b2128;font-weight:800}
.discovery{margin-top:18px;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px}
.discovery h3{font-size:13px;margin:0 0 5px}
.discovery p{font-size:11px;color:var(--muted);margin:0 0 12px}
.discovery-item{display:grid;grid-template-columns:1fr auto auto;gap:12px;align-items:center;padding:10px 0;border-top:1px solid var(--line);font-size:12px}
.discovery-item:first-of-type{border-top:0}
.badge{font-size:10px;border-radius:999px;padding:4px 7px;background:rgba(116,184,255,.08);color:var(--blue);border:1px solid rgba(116,184,255,.22)}
.empty-note{color:var(--muted);font-size:12px;padding:10px 0}
@media(max-width:780px){.wrap{padding:20px 13px 40px}.header{flex-direction:column}
.summary{grid-template-columns:1fr 1fr}.grid{grid-template-columns:1fr}.top3{display:none}.title{font-size:22px}}
</style>
"""


def render(ctx: dict) -> str:
    if not ctx.get("has_data"):
        return PAGE_CSS + '<main class="wrap"><div class="empty-note">No Line B ledger data yet.</div></main>'

    labels = ctx["labels"]
    c1c2_html = "".join(_stock_card(r) for r in ctx["c1_c2_list"]) or \
        '<div class="empty-note">今晚無 C1+C2 通過名單。</div>'
    top3_rows = "".join(_top3_row(i + 1, r) for i, r in enumerate(ctx["flow_confirmed_top3"]))
    discovery_html = "".join(_discovery_row(r) for r in ctx["intraday_discovery"]) or \
        '<div class="empty-note">今日無盤中額外發現。</div>'

    return PAGE_CSS + f"""
<main class="wrap">
  <header class="header">
    <div>
      <h1 class="title">LIVE BUY POINT MONITOR</h1>
      <div class="sub">只看現在多少、要過多少、資金有沒有來、現在該不該盯。</div>
    </div>
    <div class="live">{('LIVE · ' if ctx.get('is_live') else '') + _esc(ctx["data_date"])}</div>
  </header>

  <section class="summary">
    <div class="summary-card">
      <div class="summary-label">盤後候選歷史啟動率</div>
      <div class="summary-value">{_esc(labels["c1_c2_rate"])}</div>
      <div class="summary-hint">C1 + C2 通過後的歷史參考 · {_esc(labels["sample_note"])}</div>
    </div>
    <div class="summary-card">
      <div class="summary-label">A-flow 確認後歷史啟動率</div>
      <div class="summary-value" style="color:var(--green)">{_esc(labels["flow_confirmed_rate"])}</div>
      <div class="summary-hint">OPEN_POSITIVE / FLOW_FLIP · NO_FLIP 僅 {_esc(labels["flow_no_flip_rate"])}</div>
    </div>
  </section>

  <div class="section-title"><h2>今日重點觀察</h2><span>排序：即時資金強度 + 距離關鍵價</span></div>
  <section class="grid">{c1c2_html}</section>

  <div class="section-title"><h2>A-flow CONFIRMED TOP 3</h2><span>只看已確認，依 A-flow 幅度排序</span></div>
  <section class="top3">
    <div class="top3-row top3-head"><div>排名</div><div>股票</div><div>現價 / 壓力</div><div>A-flow</div><div>現在動作</div></div>
    {top3_rows or '<div class="empty-note" style="padding:16px">尚無確認候選。</div>'}
  </section>

  <section class="discovery">
    <h3>盤中資金強勢補充</h3>
    <p>未在盤後主名單也可進來；只作盤中發現，收盤後寫入 ledger 並重算 EOD C1/C2，不混入既有歷史啟動率。</p>
    {discovery_html}
  </section>
</main>"""
