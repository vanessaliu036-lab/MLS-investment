"""line_b_layers_render.py — 七層交易狀態頁,純模板。

只吃 line_b_layers.compute() 的輸出。不判斷、不排序、不算。
與 line_b_ledger_render 完全分開,兩頁互不影響(共用的只有導覽列)。
"""
from __future__ import annotations
import html as _html

STATE_CSS = {"ACTIVE": "st-active", "EXTENDED": "st-ext", "ARMED": "st-armed",
             "FAILED": "st-failed", "WATCH": "st-watch", "REJECT": "st-reject"}
CHIP_CSS = {"CONFIRMED": "ok", "BULLISH_FOREIGN": "ok", "BULLISH_TRUST": "ok",
            "REVERSAL": "warn", "DIVERGENT": "warn", "BEARISH": "bad", "NO_DATA": ""}
FLOW_CSS = {"STRONG": "ok", "POSITIVE": "ok", "FLAT": "", "NEGATIVE": "bad", "NO_DATA": ""}


def _esc(s):
    return _html.escape(str(s)) if s is not None else ""


def _lots(v):
    if v is None:
        return "—"
    try:
        return f"{float(v):+,.0f}"
    except (TypeError, ValueError):
        return "—"


def _f(v, d=2, suffix=""):
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{d}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def _yn(v):
    return {"YES": "YES", "NO": "NO", "N/A": "—", "NO_DATA": "—"}.get(v, v or "—")


def _chip_cell(c):
    parts = [f"{_lots(c.get('total_5d'))}"]
    f5 = c.get("foreign_5d")
    if f5 is not None:
        parts.append(f"外資 {_lots(f5)}")
    fd = c.get("foreign_days")
    if fd:
        try:
            n = int(float(fd))
            if abs(n) >= 3:
                parts.append(("連買" if n > 0 else "連賣") + f"{abs(n)}日")
        except (TypeError, ValueError):
            pass
    return "；".join(parts)


def _row(r):
    st, chip, flow = r["state"], r["chip"], r["flow"]
    trig, vol, acc, ext, sec = r["trigger"], r["volume"], r["acceptance"], r["extension"], r["sector"]
    ext_cls = "bad" if ext["verdict"] == "HIGH" else "ok"
    vol_txt = vol["verdict"].replace("PASS_NO_ACCEL", "PASS*")
    rvol = f'RVOL {vol["rvol"]}x' if vol.get("rvol") is not None else "RVOL —"
    acc_detail = (f'{acc.get("held_minutes", 0)}分 / 回撤 {_f(acc.get("max_drawdown_pct"),2,"%")}'
                  if acc["verdict"] in ("YES", "NO") else "—")
    sec_txt = (f'{sec.get("verdict")} {_f(sec.get("breadth_pct"),0,"%")}'
               if sec.get("breadth_pct") is not None else "—")
    return f"""<tr>
  <td class="c-name"><b>{_esc(r['code'])}</b> {_esc(r['name'])}
    <div class="sub">{_f(r['price'])} / 觸發 {_f(r['trigger_price'])}
      · {_f(r['distance_pct'],2,'%')}</div></td>
  <td class="num">{_esc(_chip_cell(chip))}</td>
  <td><span class="tag {CHIP_CSS.get(chip['verdict'],'')}">{_esc(chip['verdict'])}</span>
    <div class="sub">{_esc(chip.get('summary'))}</div></td>
  <td><span class="tag {FLOW_CSS.get(flow['verdict'],'')}">{_esc(flow['verdict'])}</span>
    <div class="sub">{_lots(flow.get('net_active'))}</div></td>
  <td><span class="tag {'ok' if trig['verdict']=='YES' else ''}">{_esc(_yn(trig['verdict']))}</span>
    <div class="sub">站穩 {trig.get('hold_minutes',0)}分</div></td>
  <td><span class="tag {'ok' if vol['verdict'].startswith('PASS') else ''}">{_esc(vol_txt)}</span>
    <div class="sub">{_esc(rvol)} · n={vol.get('rvol_base_days',0)}日</div></td>
  <td><span class="tag {'ok' if acc['verdict']=='YES' else ''}">{_esc(_yn(acc['verdict']))}</span>
    <div class="sub">{_esc(acc_detail)}</div></td>
  <td><span class="tag {ext_cls}">{_esc(ext['verdict'])}</span>
    <div class="sub">{_esc('、'.join(ext.get('reasons') or []) or 'MA5 '+_f(ext.get('dist_ma5_pct'),1,'%'))}</div></td>
  <td><span class="tag">{_esc(sec_txt)}</span>
    <div class="sub">{_esc(sec.get('group') or '')}{' · 領漲' if sec.get('leadership') else ''}</div></td>
  <td><span class="state {STATE_CSS.get(st['state'],'')}">{_esc(st['state'])}</span></td>
  <td class="c-action"><b>{_esc(st['action'])}</b><div class="sub">{_esc(st.get('why'))}</div></td>
</tr>"""


CSS = """
<style>
:root{--bg:#f4f6fb;--panel:#fff;--line:#e5e9f2;--text:#182033;--muted:#73809a;
--navy:#17233f;--green:#0b9a6f;--green-soft:#e8f7f1;--red:#df4b67;--red-soft:#fff0f3;
--amber:#c78313;--amber-soft:#fff6df;--blue:#3b6eea;--shadow:0 12px 30px #19274714}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",Roboto,Arial,sans-serif}
.topbar{height:64px;background:#fff;border-bottom:1px solid var(--line);display:flex;
align-items:center;justify-content:space-between;padding:0 24px;position:sticky;top:0;z-index:20}
.brand{font-size:15px;font-weight:850;color:var(--navy)}
.brand small{display:block;font-size:10px;font-weight:700;color:var(--muted);letter-spacing:.09em;margin-top:2px}
.topbar-right{font-size:12px;color:var(--muted);font-weight:700}
.pagenav{background:#fff;border-bottom:1px solid var(--line);padding:8px 24px;display:flex;
align-items:center;flex-wrap:wrap;gap:2px;position:sticky;top:64px;z-index:19}
.pagenav a{padding:9px 13px;border-radius:10px;color:#65718a;font-weight:700;white-space:nowrap;
text-decoration:none;font-size:13px}
.pagenav a:hover{background:var(--bg)}
.pagenav a.active{background:var(--navy);color:#fff}
.wrap{max-width:1600px;margin:0 auto;padding:22px 20px 50px}
h1{font-size:23px;font-weight:800;margin:0 0 5px;color:var(--navy)}
.sub-title{font-size:13px;color:var(--muted);margin-bottom:16px}
.banner{background:var(--amber-soft);border:1px solid #f0dcac;color:#7a5406;border-radius:12px;
padding:12px 15px;font-size:12.5px;line-height:1.7;margin-bottom:16px;font-weight:600}
.banner b{color:#5c3f04}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.chip{background:#fff;border:1px solid var(--line);border-radius:999px;padding:7px 13px;
font-size:12px;font-weight:750;box-shadow:var(--shadow)}
.chip i{font-style:normal;color:var(--muted);font-weight:700}
.tbl-wrap{background:#fff;border:1px solid var(--line);border-radius:14px;overflow-x:auto;box-shadow:var(--shadow)}
table{border-collapse:collapse;width:100%;min-width:1500px;font-size:12px}
th{background:var(--bg);color:var(--muted);font-size:10.5px;font-weight:800;text-align:left;
padding:11px 10px;border-bottom:1px solid var(--line);white-space:nowrap;letter-spacing:.03em}
td{padding:11px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
.c-name{min-width:150px}
.c-action{min-width:190px}
.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.sub{font-size:10.5px;color:var(--muted);margin-top:3px;font-weight:600;line-height:1.5}
.tag{display:inline-block;font-size:10.5px;font-weight:800;padding:3px 7px;border-radius:6px;
background:var(--bg);border:1px solid var(--line);white-space:nowrap}
.tag.ok{background:var(--green-soft);color:var(--green);border-color:#9fd9c4}
.tag.warn{background:var(--amber-soft);color:var(--amber);border-color:#f0dcac}
.tag.bad{background:var(--red-soft);color:var(--red);border-color:#f3bcc7}
.state{display:inline-block;font-size:11px;font-weight:850;padding:5px 9px;border-radius:7px;white-space:nowrap}
.st-active{background:var(--green);color:#fff}
.st-ext{background:var(--amber);color:#fff}
.st-armed{background:#eaf0fe;color:var(--blue);border:1px solid #b9ccf7}
.st-failed{background:var(--red-soft);color:var(--red);border:1px solid #f3bcc7}
.st-watch{background:var(--bg);color:var(--muted);border:1px solid var(--line)}
.st-reject{background:#eef0f4;color:#8b94a6;border:1px solid var(--line)}
.foot{font-size:11px;color:var(--muted);margin-top:14px;line-height:1.8}
</style>
"""

NAV_PAGES = [("/", "決策首頁"), ("/opportunity-ledger", "機會分層榜"),
             ("/line-b-ledger", "買點監控"), ("/line-b-layers", "七層交易狀態")]


def _nav(active):
    return "".join('<a href="{h}"{c}>{t}</a>'.format(
        h=h, t=_esc(t), c=' class="active"' if h == active else "") for h, t in NAV_PAGES)


def render(ctx: dict) -> str:
    rows = ctx.get("rows") or []
    if not rows:
        body = '<div class="foot">尚無資料（%s）。</div>' % _esc(ctx.get("skipped") or "no rows")
    else:
        counts = ctx.get("counts") or {}
        chips = "".join(
            f'<div class="chip">{_esc(k)} <i>{v}</i></div>'
            for k, v in sorted(counts.items(), key=lambda x: -x[1]))
        fresh = rows[0]["freshness"]
        body = f"""
  <div class="chips">{chips}</div>
  <div class="tbl-wrap"><table>
    <thead><tr>
      <th>股票 / 現價・觸發</th><th style="text-align:right">FINMIND 5D 籌碼</th>
      <th>CHIP<br><i>中期籌碼</i></th><th>FLOW<br><i>今日資金</i></th>
      <th>PRICE TRIGGER<br><i>是否突破</i></th><th>VOLUME<br><i>有無真量</i></th>
      <th>ACCEPTANCE<br><i>是否站穩</i></th><th>EXTENSION RISK<br><i>是否太晚</i></th>
      <th>SECTOR<br><i>族群支持</i></th><th>TRADE STATE</th><th>ACTION</th>
    </tr></thead>
    <tbody>{''.join(_row(r) for r in rows)}</tbody>
  </table></div>
  <div class="foot">
    資料新鮮度：FINMIND 三大法人至 <b>{_esc(fresh.get('inst_flow_through'))}</b>
    · 日線基準 T-1 <b>{_esc(fresh.get('t1_bar_date'))}</b>
    · 報價 <b>{_esc(fresh.get('quote_updated_at') or '—')}</b>
    · A-flow <b>{_esc(fresh.get('aflow_updated_at') or '—')}</b><br>
    狀態機：WATCH → ARMED → ACTIVE（Trigger + Volume + Acceptance）。
    EXTENSION 不參與 ACTIVE 判定，只在 ACTIVE 成立後做交易覆寫：
    EXTENSION HIGH → TRADE STATE 顯示 EXTENDED、ACTION 禁追。跌回觸發價/VWAP → FAILED。<br>
    observation version：{_esc(ctx.get('observation_version'))}
  </div>"""

    return CSS + f"""
<div class="topbar">
  <div class="brand">MLS<small>SEVEN-LAYER TRADE STATE</small></div>
  <div class="topbar-right">{_esc(ctx.get('T') or '')}</div>
</div>
<nav class="pagenav">{_nav('/line-b-layers')}</nav>
<main class="wrap">
  <h1>七層交易狀態</h1>
  <div class="sub-title">CHIP／FLOW／TRIGGER／VOLUME／ACCEPTANCE／EXTENSION／SECTOR — 每欄只回答一件事。</div>
  <div class="banner">
    <b>DESCRIPTIVE ONLY — 這一頁不是買進推薦。</b>
    所有門檻（RVOL 1.5x、回撤 1%、乖離 8%/15% 等）都是暫定觀察值，<b>沒有經過回測驗證</b>。
    本頁目的是累積 20–30 個交易日的 forward 樣本，之後才用 T+1／T+3／MFE／MAE 判斷哪些訊號值得留。<br>
    已知限制：<b>Turnover 無資料源</b>（沒有流通股數）一律顯示 —；<b>RVOL 母體只有 b_snapshot 累積的交易日</b>
    （每列都標 n=幾日），天數少時基準不穩；<b>Gap 不計算</b>（沒有真開盤價，不用 proxy 硬湊）。
    這一頁與「買點監控」是兩套獨立計算，<b>不共用那張 77/11 校準表</b>，也不影響它。
  </div>
  {body}
</main>"""
