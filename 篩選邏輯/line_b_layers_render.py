"""line_b_layers_render.py — 七層交易狀態頁,純模板。

只吃 line_b_layers.compute() 的輸出。不判斷、不排序、不算。
與 line_b_ledger_render 完全分開,兩頁互不影響(共用的只有導覽列)。
"""
from __future__ import annotations
import html as _html

from navigation import NAV_CSS, nav_html

STATE_CSS = {"ACTIVE": "st-active", "EXTENDED": "st-ext", "ARMED": "st-armed",
             "FAILED": "st-failed", "WATCH": "st-watch", "REJECT": "st-reject",
             "DATA_BLOCKED": "st-blocked"}
CHIP_CSS = {"CONFIRMED": "ok", "BULLISH_FOREIGN": "ok", "BULLISH_TRUST": "ok",
            "REVERSAL": "warn", "DIVERGENT": "warn", "BEARISH": "bad", "NO_DATA": ""}
FLOW_CSS = {"STRONG": "ok", "POSITIVE": "ok", "FLAT": "", "NEGATIVE": "bad", "NO_DATA": ""}
STATE_ORDER = ("ACTIVE", "EXTENDED", "ARMED", "WATCH", "FAILED", "REJECT")


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


def _tone(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ""
    return "num-up" if n > 0 else "num-down" if n < 0 else ""


def _signed_pct(v, d=2):
    if v is None:
        return "—"
    try:
        n = float(v)
        return f"{n:+.{d}f}%"
    except (TypeError, ValueError):
        return "—"


def _yn(v):
    return {"YES": "YES", "NO": "NO", "N/A": "—", "NO_DATA": "—"}.get(v, v or "—")


def _judgment(r):
    """新交易判斷；舊快照沒有欄位時退回 state,保持歷史頁可渲染。"""
    return r.get("trade_judgment") or r.get("state") or {}


def _chip_cell(c):
    """回傳已上色的 HTML —— 每個數字各自判斷正負。

    ⚠ 2026-08-28 修:這一格同時放「三大法人合計」與「外資」兩個獨立數字,
    兩者符號可能相反(實例:4979 華星光 合計 +4,126、外資 -3,937)。原本整個
    <td> 只用 total_5d 一個值套 _tone(),外資的負值被迫跟著變紅,違反漲紅跌綠。
    連買/連賣同理:連買=資金流入=紅,連賣=流出=綠。"""
    def _num(v):
        return f'<span class="{_tone(v)}">{_esc(_lots(v))}</span>'

    # 兩個「5日」數字要各自掛清楚的標籤，不能留一個裸數字——使用者看不出
    # 第一個數字是三大法人合計還是別的東西（2026-09-02 使用者回報看不懂）。
    parts = [f"合計 {_num(c.get('total_5d'))}"]
    f5 = c.get("foreign_5d")
    if f5 is not None:
        parts.append(f"外資 {_num(f5)}")
    five_day = "；".join(parts)
    fd = c.get("foreign_days")
    streak = ""
    if fd:
        try:
            n = int(float(fd))
            if abs(n) >= 3:
                label = ("連買" if n > 0 else "連賣") + f"{abs(n)}日"
                cls = "num-up" if n > 0 else "num-down"
                # 連買/連賣是「最近連續天數」，跟前面「5日合計」是不同時間窗口的
                # 兩件事（合計可能被更早一天的大單抵銷）——分行呈現，不跟 5 日數字
                # 用同一個分號並排，避免看起來像同一組數字互相矛盾。
                streak = f'<br><span class="{cls}" style="font-size:11px">近期{_esc(label)}</span>'
        except (TypeError, ValueError):
            pass
    return five_day + streak


def _row(r):
    st, chip, flow = r["state"], r["chip"], r["flow"]
    trig, vol, acc, ext, sec = r["trigger"], r["volume"], r["acceptance"], r["extension"], r["sector"]
    judgment = _judgment(r)
    ext_cls = "bad" if ext["verdict"] == "HIGH" else "ok"
    vol_txt = vol["verdict"].replace("PASS_NO_ACCEL", "PASS*")
    rvol = f'RVOL {vol["rvol"]}x' if vol.get("rvol") is not None else "RVOL —"
    tno = (f'週轉 {vol["turnover_pct"]}%' if vol.get("turnover_pct") is not None else "週轉 —")
    acc_detail = (f'{acc.get("held_minutes", 0)}分 / 回撤 {_f(acc.get("max_drawdown_pct"),2,"%")}'
                  if acc["verdict"] in ("YES", "NO") else "—")
    sec_txt = (f'{sec.get("verdict")} {_f(sec.get("breadth_pct"),0,"%")}'
               if sec.get("breadth_pct") is not None else "—")
    change = ext.get("change_rate")
    failures = judgment.get("failure_conditions") or ("跌 VWAP", "A-flow 翻負", "爆量滯漲", "跌破關鍵價")
    alerts = judgment.get("failure_alerts") or []
    return f"""<tr class="layer-row" data-state="{_esc(st['state'])}" data-stock-code="{_esc(r['code'])}" role="link" tabindex="0" title="點擊查看個股決策卡">
  <td class="c-name"><b>{_esc(r['code'])}</b> {_esc(r['name'])}
    <div class="quote-line">
      <span>現價 <b class="{_tone(change)}">{_f(r['price'])}</b></span>
      <span>漲跌 <b class="{_tone(change)}">{_signed_pct(change)}</b></span>
      <span>觸發 <b>{_f(r['trigger_price'])}</b></span>
    </div>
    <div class="sub">距觸發 <span class="{_tone(r['distance_pct'])}">{_signed_pct(r['distance_pct'])}</span></div></td>
  <td class="num">{_chip_cell(chip)}</td>
  <td><span class="tag {CHIP_CSS.get(chip['verdict'],'')}">{_esc(chip['verdict'])}</span>
    <div class="sub">{_esc(chip.get('summary'))}</div></td>
  <td><span class="tag {FLOW_CSS.get(flow['verdict'],'')}">{_esc(flow['verdict'])}</span>
    <div class="sub {_tone(flow.get('net_active'))}">{_lots(flow.get('net_active'))}</div></td>
  <td><span class="tag {'ok' if trig['verdict']=='YES' else ''}">{_esc(_yn(trig['verdict']))}</span>
    <div class="sub">站穩 {trig.get('hold_minutes',0)}分</div></td>
  <td><span class="tag {'ok' if vol['verdict'].startswith('PASS') else ''}">{_esc(vol_txt)}</span>
    <div class="sub">{_esc(rvol)} · n={vol.get('rvol_base_days',0)}日<br>{_esc(tno)}</div></td>
  <td><span class="tag {'ok' if acc['verdict']=='YES' else ''}">{_esc(_yn(acc['verdict']))}</span>
    <div class="sub">{_esc(acc_detail)}</div></td>
  <td><span class="tag {ext_cls}">{_esc(ext['verdict'])}</span>
    <div class="sub">{_esc('、'.join(ext.get('reasons') or []) or 'MA5 '+_f(ext.get('dist_ma5_pct'),1,'%'))}<br>Gap {_f(ext.get('gap_pct'),2,'%')}</div></td>
  <td><span class="tag">{_esc(sec_txt)}</span>
    <div class="sub">{_esc(sec.get('group') or '')}{' · 領漲' if sec.get('leadership') else ''}</div></td>
  <td><span class="state {STATE_CSS.get(st['state'],'')}">{_esc(st['state'])}</span></td>
  <td class="c-action"><b>{_esc(judgment.get('chase_permission') or st.get('action') or '—')}</b>
    <div class="sub judgment-stage">{_esc(judgment.get('trend_stage') or '—')} · {_esc(judgment.get('flow_state') or '—')}</div>
    <div class="sub">進場 {_esc(judgment.get('entry_method') or '—')}</div>
    <div class="sub">失敗：{_esc('、'.join(failures))}{' · 目前：' + _esc('、'.join(alerts)) if alerts else ''}</div></td>
</tr>"""


def _mobile_card(r):
    """手機版卡片：只放可快速判讀的訊號，其餘七層收進 details。"""
    st, chip, flow = r["state"], r["chip"], r["flow"]
    trig, vol, acc, ext, sec = r["trigger"], r["volume"], r["acceptance"], r["extension"], r["sector"]
    judgment = _judgment(r)
    change = ext.get("change_rate")
    state = st.get("state") or "—"
    flow_value = flow.get("net_active")
    foreign_value = chip.get("foreign_5d")
    hold_minutes = acc.get("held_minutes", trig.get("hold_minutes", 0))
    volume_verdict = vol["verdict"].replace("PASS_NO_ACCEL", "PASS*")

    def detail(label, value, cls="", number=None):
        numeric = (f' <span class="{_tone(number)}">{_esc(_lots(number))}</span>'
                   if number is not None else "")
        return f'<div class="mobile-detail-row"><span>{_esc(label)}</span><b class="{cls}">{_esc(value)}{numeric}</b></div>'

    chip_cls = CHIP_CSS.get(chip["verdict"], "")
    flow_cls = FLOW_CSS.get(flow["verdict"], "")
    trigger_cls = "ok" if trig["verdict"] == "YES" else ""
    volume_cls = "ok" if vol["verdict"].startswith("PASS") else ""
    acceptance_cls = "ok" if acc["verdict"] == "YES" else ""
    extension_cls = "bad" if ext["verdict"] == "HIGH" else "ok"
    sector_cls = "ok" if sec.get("verdict") == "STRONG" else "warn" if sec.get("verdict") == "MIXED" else ""
    state_cls = STATE_CSS.get(state, "")

    failures = judgment.get("failure_conditions") or ("跌 VWAP", "A-flow 翻負", "爆量滯漲", "跌破關鍵價")
    alerts = judgment.get("failure_alerts") or []
    return f'''<article class="mobile-card" data-state="{_esc(state)}" data-stock-code="{_esc(r["code"])}" role="link" tabindex="0" title="點擊查看個股決策卡">
  <div class="mobile-card-top">
    <div class="mobile-identity"><b>{_esc(r["code"])}</b><span>{_esc(r["name"])}</span></div>
    <div class="mobile-quote"><b>{_f(r["price"])}</b><strong class="{_tone(change)}">{_signed_pct(change)}</strong></div>
  </div>
  <div class="mobile-statuses" aria-label="核心狀態">
    <span class="tag {chip_cls}">{_esc(chip["verdict"])}</span>
    <span class="tag {flow_cls}">{_esc(flow["verdict"])}</span>
    <span class="tag {trigger_cls}">{_esc(_yn(trig["verdict"]))}</span>
    <span class="state {state_cls}">{_esc(state)}</span>
  </div>
  <div class="mobile-judgment"><b>{_esc(judgment.get("trend_stage") or "—")}</b><span>{_esc(judgment.get("chase_permission") or st.get("action") or "—")}</span></div>
  <div class="mobile-metrics">
    <div class="mobile-metric"><span>資金</span><b class="{_tone(flow_value)}">{_lots(flow_value)}</b><small>外資 {_lots(foreign_value)}</small></div>
    <div class="mobile-metric"><span>站穩時間</span><b>{_esc(str(hold_minutes) + "分")}</b><small>{_esc(_yn(acc["verdict"]))}</small></div>
    <div class="mobile-metric"><span>RVOL</span><b>{_esc((str(vol.get("rvol")) + "x") if vol.get("rvol") is not None else "—")}</b><small>n={_esc(vol.get("rvol_base_days", 0))}日</small></div>
  </div>
  <details class="mobile-details">
    <summary>查看完整七層條件 <span aria-hidden="true">〉</span></summary>
    <div class="mobile-detail-grid">
      {detail("CHIP 中期籌碼", chip.get("summary") or chip["verdict"], chip_cls)}
      {detail("FLOW 今日資金", flow["verdict"], flow_cls, flow_value)}
      {detail("PRICE TRIGGER", f'{_yn(trig["verdict"])} · 觸發 {_f(r.get("trigger_price"))}', trigger_cls)}
      {detail("VOLUME QUALITY", f'{volume_verdict} · {_esc("RVOL " + str(vol.get("rvol")) + "x") if vol.get("rvol") is not None else "RVOL —"}', volume_cls)}
      {detail("ACCEPTANCE", f'{_yn(acc["verdict"])} · 回撤 {_f(acc.get("max_drawdown_pct"), 2, "%")}', acceptance_cls)}
      {detail("EXTENSION RISK", f'{ext["verdict"]} · {"、".join(ext.get("reasons") or []) or "位置正常"}', extension_cls)}
      {detail("SECTOR", f'{sec.get("verdict") or "—"} · {sec.get("group") or "—"} {_f(sec.get("breadth_pct"), 0, "%")}', sector_cls)}
      {detail("追價許可", judgment.get("chase_permission") or st.get("action") or "—")}
      {detail("進場方式", judgment.get("entry_method") or "—")}
      {detail("失敗條件", "、".join(failures))}
    </div>
    <div class="mobile-detail-note">{_esc(st.get("why") or "")} · 資金 {_esc(judgment.get("flow_state") or "—")}{' · 目前失敗：' + _esc('、'.join(alerts)) if alerts else ''} · MA5 {_f(ext.get("dist_ma5_pct"), 1, "%")} · MA20 {_f(ext.get("dist_ma20_pct"), 1, "%")} · Gap {_f(ext.get("gap_pct"), 2, "%")}</div>
  </details>
</article>'''


CSS = """
<style>
:root{--bg:#f4f6fb;--panel:#fff;--line:#e5e9f2;--text:#182033;--muted:#5f6d86;
--navy:#17233f;--green:#0b9a6f;--green-soft:#e8f7f1;--red:#df4b67;--red-soft:#fff0f3;
--amber:#c78313;--amber-soft:#fff6df;--blue:#3b6eea;--shadow:0 12px 30px #19274714}
*{box-sizing:border-box}
body{margin:0;padding-top:122px;background:var(--bg);color:var(--text);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",Roboto,Arial,sans-serif}
html,body{overflow-x:hidden}
.topbar{height:64px;background:#fff;border-bottom:1px solid var(--line);display:flex;
align-items:center;justify-content:space-between;padding:0 24px;position:fixed;top:0;left:0;right:0;z-index:20}
.brand{font-size:15px;font-weight:850;color:var(--navy)}
.brand small{display:block;font-size:10px;font-weight:700;color:var(--muted);letter-spacing:.09em;margin-top:2px}
.topbar-right{font-size:12px;color:var(--muted);font-weight:700}
.pagenav{background:#fff;border-bottom:1px solid var(--line);padding:8px 24px;display:flex;
align-items:center;flex-wrap:wrap;gap:2px;position:fixed;top:64px;left:0;right:0;z-index:19}
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
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.chip{background:#fff;border:1px solid var(--line);border-radius:999px;padding:7px 13px;
font:inherit;font-size:12px;font-weight:750;box-shadow:var(--shadow);cursor:pointer}
.chip i{font-style:normal;color:var(--muted);font-weight:700}
.chip:hover,.chip.active{background:#eaf0fe;border-color:#b9ccf7;color:var(--blue)}
.chip:focus-visible{outline:3px solid #b9ccf7;outline-offset:2px}
.desktop-hint{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:11px;font-weight:700;margin:0 2px 10px}
.desktop-hint::before{content:'↔';display:grid;place-items:center;width:20px;height:20px;border-radius:6px;
background:#eaf0fe;color:var(--blue);font-size:12px;font-weight:900}
.tbl-wrap{width:100%;max-width:100%;background:#fff;border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:var(--shadow)}
table{border-collapse:collapse;width:100%;max-width:100%;min-width:0;table-layout:fixed;font-size:12px}
th{background:var(--bg);color:var(--muted);font-size:10.5px;font-weight:800;text-align:left;
padding:11px 8px;border-bottom:1px solid var(--line);white-space:normal;line-height:1.25;letter-spacing:.03em}
td{min-width:0;padding:11px 8px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere;word-break:break-word}
tr:last-child td{border-bottom:0}
.layer-table th,.layer-table td{min-width:0}
.c-name{min-width:0}
.c-action{min-width:0;overflow-wrap:anywhere}
.layer-row{cursor:pointer}.layer-row:hover td{background:#f8faff}.layer-row:focus-visible td{outline:2px solid var(--blue);outline-offset:-2px}
.layer-row.is-filtered,.mobile-card.is-filtered{display:none!important}
.num{text-align:left;white-space:normal;font-variant-numeric:tabular-nums;line-height:1.45}
.sub{font-size:10.5px;color:var(--muted);margin-top:3px;font-weight:600;line-height:1.5}
.quote-line{display:flex;gap:7px;flex-wrap:wrap;margin-top:4px;font-size:10.5px;color:var(--muted);font-weight:650}
.quote-line span{white-space:normal;overflow-wrap:anywhere}.quote-line b{font-size:12px;color:var(--navy);font-variant-numeric:tabular-nums}
.num-up{color:var(--red)!important}.num-down{color:var(--green)!important}
.tag{display:inline-block;font-size:10.5px;font-weight:800;padding:3px 7px;border-radius:6px;
background:var(--bg);border:1px solid var(--line);white-space:normal;line-height:1.25;overflow-wrap:anywhere}
.tag.ok{background:var(--green-soft);color:var(--green);border-color:#9fd9c4}
.tag.warn{background:var(--amber-soft);color:var(--amber);border-color:#f0dcac}
.tag.bad{background:var(--red-soft);color:var(--red);border-color:#f3bcc7}
.state{display:inline-block;font-size:11px;font-weight:850;padding:5px 8px;border-radius:7px;white-space:normal;line-height:1.25;overflow-wrap:anywhere}
.st-active{background:var(--green);color:#fff}
.st-ext{background:var(--amber);color:#fff}
.st-armed{background:#eaf0fe;color:var(--blue);border:1px solid #b9ccf7}
.st-failed{background:var(--red-soft);color:var(--red);border:1px solid #f3bcc7}
.st-watch{background:var(--bg);color:var(--muted);border:1px solid var(--line)}
.st-reject{background:#eef0f4;color:#8b94a6;border:1px solid var(--line)}
.st-blocked{background:#fff1e6;color:#a35a16;border:1px solid #f0c28b}
.foot{font-size:11px;color:var(--muted);margin-top:14px;line-height:1.8}
.mobile-card{display:none}
.mobile-detail-row{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--line);font-size:12px}
.mobile-detail-row:last-child{border-bottom:0}.mobile-detail-row span{color:var(--muted)}
.mobile-detail-row b{max-width:68%;text-align:right;overflow-wrap:anywhere}
.mobile-detail-row b.ok{color:var(--green)}.mobile-detail-row b.warn{color:var(--amber)}.mobile-detail-row b.bad{color:var(--red)}
.mobile-detail-note{color:var(--muted);font-size:11px;line-height:1.6;padding-top:8px}
.mobile-judgment{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:5px;padding:7px 9px;border-radius:8px;background:var(--green-soft);color:var(--green);font-size:12px}
.mobile-judgment b{font-weight:850}.mobile-judgment span{font-weight:850;white-space:nowrap}
@media (max-width: 900px){
  :root{--bg:#f5f7fb;--shadow:0 6px 18px #19274712}
  body{padding-top:118px}
  .topbar{height:62px;padding:0 16px}
  .brand{font-size:15px}.brand small{font-size:9px}.topbar-right{font-size:11px}
  .pagenav{top:62px;display:flex;flex-wrap:nowrap;overflow-x:auto;overscroll-behavior-x:contain;
    -webkit-overflow-scrolling:touch;scrollbar-width:none;padding:6px 12px;gap:3px}
  .pagenav::-webkit-scrollbar{display:none}
  .pagenav a{min-height:44px;display:inline-flex;align-items:center;padding:9px 13px;font-size:12px}
  .wrap{padding:14px 12px 40px}
  h1{font-size:21px;margin-bottom:3px}.sub-title{font-size:12px;margin-bottom:12px}
  .banner{font-size:11px;line-height:1.6;padding:10px 11px;margin-bottom:12px}
  .chips{margin:0 -12px 12px;padding:0 12px;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;gap:6px}
  .chips::-webkit-scrollbar{display:none}
  .chip{flex:0 0 auto;min-height:44px;padding:9px 13px;display:inline-flex;align-items:center}
  .desktop-hint{display:none}
  .tbl-wrap{display:none}
  .mobile-card{display:block;background:var(--panel);border:1px solid var(--line);border-radius:14px;
    padding:10px;margin-bottom:10px;box-shadow:var(--shadow);min-height:150px}
  .mobile-card-top{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:26px}
  .mobile-identity{display:flex;align-items:baseline;gap:7px;min-width:0}
  .mobile-identity b{font-size:16px;letter-spacing:.02em}.mobile-identity span{font-size:14px;font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .mobile-quote{display:flex;align-items:baseline;gap:7px;white-space:nowrap;font-variant-numeric:tabular-nums}
  .mobile-quote b{font-size:18px;color:var(--navy)}.mobile-quote strong{font-size:14px}
  .mobile-statuses{display:flex;align-items:center;gap:5px;min-height:27px;margin-top:4px;overflow:hidden}
  .mobile-statuses .tag{font-size:10px;padding:4px 7px}.mobile-statuses .state{font-size:10px;padding:4px 7px;margin-left:auto}
  .mobile-metrics{display:grid;grid-template-columns:1.18fr 1fr .9fr;border-top:1px solid var(--line);margin-top:4px;padding-top:5px;gap:7px}
  .mobile-metric{min-width:0;display:grid;grid-template-columns:1fr;gap:1px}
  .mobile-metric span,.mobile-metric small{color:var(--muted);font-size:9px;font-weight:700;white-space:nowrap;line-height:1.25}
  .mobile-metric b{font-size:13px;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.25}
  .mobile-metric small{font-size:9px;font-weight:600;overflow:hidden;text-overflow:ellipsis}
  .mobile-details{margin-top:4px;border-top:1px solid var(--line)}
  .mobile-details summary{min-height:44px;display:flex;align-items:center;justify-content:space-between;cursor:pointer;color:var(--blue);font-size:12px;font-weight:800;list-style:none}
  .mobile-details summary::-webkit-details-marker{display:none}.mobile-details summary span{font-size:18px;line-height:1}
  .mobile-detail-grid{border-top:1px solid var(--line)}
  .foot{font-size:10.5px;margin-top:12px}
}
/* 全站固定主選單：與決策首頁、Reversal Lab 使用同一版型。 */
body{padding-top:128px}
.topbar{height:72px;padding:0 28px}
.brand{display:flex;align-items:center;gap:12px;font-size:15px;color:var(--navy)}
.brand .mark{width:34px;height:34px;border-radius:11px;background:linear-gradient(135deg,#17233f,#4f6ea8);display:grid;place-items:center;color:#fff;font-size:13px}
.brand small{font-size:11px;letter-spacing:.04em}
.pagenav{top:72px;padding:8px 28px;flex-wrap:wrap;gap:2px}
.pagenav a{min-height:42px;display:inline-flex;align-items:center;padding:9px 13px;border-radius:10px;font-size:12px}
.pagenav .nav-label{font-size:11px;color:#9aa4b8;padding:0 12px 0 2px;font-weight:800;letter-spacing:.08em}
.pagenav .nav-item{font-size:12px;font-weight:700}
.pagenav .nav-item.active{background:var(--navy);color:#fff}
.pagenav .count{margin-left:6px;font-size:11px;padding:2px 7px;border-radius:99px;background:#eef1f7}
.pagenav .count.live{background:#dff7ec;color:#087f5b;border:1px solid #a9e8cc}
@media(max-width:900px){body{padding-top:120px}.topbar{height:64px;padding:0 14px}.pagenav{top:64px;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;padding:6px 12px;gap:3px}.pagenav::-webkit-scrollbar{display:none}.pagenav a{min-height:44px;padding:9px 12px;font-size:12px}.pagenav .nav-label{flex:none;padding:0 9px 0 2px}}
</style>
"""

NAV_PAGES = [("/", "決策首頁", "OFF"), ("/", "機會雷達", "51"),
             ("/chips", "觀察池 51 檔", "51"), ("/chips", "籌碼", "51"),
             ("/reversal-lab", "資金反轉驗證", "LIVE"),
             ("/", "盤後驗證", None), ("/opportunity-ledger", "機會分層榜", None),
             ("/line-b-ledger", "買點監控", None), ("/line-b-layers", "七層交易狀態", None)]


def _nav(active):
    def badge(value):
        if not value:
            return ""
        klass = "count live" if value == "LIVE" else "count"
        return f'<span class="{klass}">{_esc(value)}</span>'
    return "".join(
        '<a class="nav-item{active}" href="{h}">{t}{badge}</a>'.format(
            h=h, t=_esc(t), active=' active' if h == active else "", badge=badge(b))
        for h, t, b in NAV_PAGES)


def render(ctx: dict) -> str:
    rows = ctx.get("rows") or []
    if not rows:
        body = '<div class="foot">尚無資料（%s）。</div>' % _esc(ctx.get("skipped") or "no rows")
    else:
        counts = ctx.get("counts") or {}
        chips = (
            f'<button type="button" class="chip active" data-state-filter="ALL" '
            f'aria-pressed="true">全部 <i>{len(rows)}</i></button>'
            + "".join(
                f'<button type="button" class="chip" data-state-filter="{_esc(state)}" '
                f'aria-pressed="false">{_esc(state)} <i>{counts.get(state, 0)}</i></button>'
                for state in STATE_ORDER if counts.get(state, 0)
            )
        )
        fresh = rows[0]["freshness"]
        body = f"""
  <div class="chips">{chips}</div>
  <div class="desktop-hint" role="note">桌面版已固定欄寬，七層訊號可在同一視窗判讀；點擊列可開啟個股決策卡。</div>
  <div class="tbl-wrap"><table class="layer-table">
    <colgroup>
      <col style="width:18%"><col style="width:9%"><col style="width:9%"><col style="width:7%">
      <col style="width:8%"><col style="width:9%"><col style="width:8%"><col style="width:9%">
      <col style="width:8%"><col style="width:7%"><col style="width:18%">
    </colgroup>
    <thead><tr>
      <th>股票／現價／漲跌／觸發</th><th style="text-align:right">FINMIND 5D 籌碼</th>
      <th>CHIP<br><i>中期籌碼</i></th><th>FLOW<br><i>今日資金</i></th>
      <th>PRICE TRIGGER<br><i>是否突破</i></th><th>VOLUME<br><i>有無真量</i></th>
      <th>ACCEPTANCE<br><i>是否站穩</i></th><th>EXTENSION RISK<br><i>是否太晚</i></th>
      <th>SECTOR<br><i>族群支持</i></th><th>TRADE STATE</th><th>交易判斷<br><i>階段／追價／進場／失敗</i></th>
    </tr></thead>
    <tbody>{''.join(_row(r) for r in rows)}</tbody>
  </table></div>
  <div class="mobile-list">{''.join(_mobile_card(r) for r in rows)}</div>
  <div class="foot">
    資料新鮮度：FINMIND 三大法人至 <b>{_esc(fresh.get('inst_flow_through'))}</b>
    · 日線基準 T-1 <b>{_esc(fresh.get('t1_bar_date'))}</b>
    · 報價 <b>{_esc(fresh.get('quote_updated_at') or '—')}</b>
    · A-flow <b>{_esc(fresh.get('aflow_updated_at') or '—')}</b><br>
    狀態機：WATCH → ARMED → ACTIVE（Trigger + Volume + Acceptance）。
    EXTENSION 不參與 ACTIVE 判定，只調整追價部位：高位延伸仍可依資金與價格接受度小部位參與。
    跌回觸發價/VWAP、A-flow 翻負或爆量滯漲 → 失敗條件觸發。<br>
    observation version：{_esc(ctx.get('observation_version'))}
  </div>"""

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>MLS 七層交易狀態</title>
  {CSS}{NAV_CSS}
</head>
<body>
{nav_html("layers")}
<main class="wrap">
  <h1>七層交易狀態</h1>
  <div class="sub-title">七層條件 + 交易判斷：趨勢階段／資金狀態／追價許可／進場方式／失敗條件。</div>
  <div class="banner">
    <b>DESCRIPTIVE ONLY — 這一頁不是買進推薦。</b>
    所有門檻（RVOL 1.5x、回撤 1%、乖離 8%/15% 等）都是暫定觀察值，<b>沒有經過回測驗證</b>。
    本頁目的是累積 20–30 個交易日的 forward 樣本，之後才用 T+1／T+3／MFE／MAE 判斷哪些訊號值得留。<br>
    資料說明：<b>Turnover</b> = 當日成交股數 ÷ 已發行普通股數，股數取自 TWSE／TPEx 官方免費 OpenAPI
    （51 檔全涵蓋）；<b>Gap</b> 用 daily_bar 真開盤價，當日 daily_bar 收盤後才寫入，<b>盤中顯示 —</b>
    （不用 09:15 快照價當 proxy 硬湊）；<b>RVOL 母體只有 b_snapshot 累積的交易日</b>
    （每列都標 n=幾日），天數少時基準不穩。
    這一頁與「買點監控」是兩套獨立計算，<b>不共用那張 77/11 校準表</b>，也不影響它。
  </div>
  {body}
</main>
<script>
(() => {{
  const filters = [...document.querySelectorAll('[data-state-filter]')];
  const rows = [...document.querySelectorAll('tbody tr[data-state]')];
  const cards = [...document.querySelectorAll('.mobile-card[data-state]')];
  const tableWrap = document.querySelector('.tbl-wrap');
  if (tableWrap) tableWrap.scrollLeft = 0;
  const openCard = code => {{
    const match = String(code || '').match(/\\d{{4}}/);
    const clean = match ? match[0] : '';
    if (clean) window.location.href = '/api/card_page?code=' + encodeURIComponent(clean);
  }};
  document.querySelectorAll('[data-stock-code]').forEach(item => item.addEventListener('click', event => {{
    // details 的 summary 要能展開，不應該被整張卡的導流事件攔截。
    if (event.target.closest('summary, a, button, input, select')) return;
    openCard(item.dataset.stockCode);
  }}));
  document.querySelectorAll('[data-stock-code]').forEach(item => item.addEventListener('keydown', event => {{
    if (event.key === 'Enter' || event.key === ' ') {{ event.preventDefault(); openCard(item.dataset.stockCode); }}
  }}));
  const applyFilter = (selected, button) => {{
    rows.forEach(row => {{
      const filtered = selected !== 'ALL' && row.dataset.state !== selected;
      row.hidden = filtered;
      row.classList.toggle('is-filtered', filtered);
    }});
    cards.forEach(card => {{
      const filtered = selected !== 'ALL' && card.dataset.state !== selected;
      card.hidden = filtered;
      card.classList.toggle('is-filtered', filtered);
    }});
    filters.forEach(item => {{
      const active = item === button;
      item.classList.toggle('active', active);
      item.setAttribute('aria-pressed', active ? 'true' : 'false');
    }});
  }};
  filters.forEach(button => button.addEventListener('click', event => {{
    event.preventDefault();
    const selected = button.dataset.stateFilter;
    applyFilter(selected, button);
  }}));
}})();
</script>
</body>
</html>"""
