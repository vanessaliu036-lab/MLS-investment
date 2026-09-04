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
   已確認(CONFIRMED)、PRICE TRIGGER 已發生或盤中發現(INTRADAY_DISCOVERY,母體不同不能套用同一張
   校準表)一律不顯示這個數字。有效樣本數是 77(stock-day)/11(day)層級,
   不是 1710——snapshot 之間同股同天高度相關,不當獨立樣本數用。
"""
from __future__ import annotations
import html as _html

from navigation import NAV_CSS, nav_html
import line_b_monitor as _monitor


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

STATUS_CSS = {"WAIT": "", "WATCH_CLOSELY": "watch", "CONFIRMED": "ok",
              "PRICE_TRIGGERED": "price-trigger", "GIVE_UP": "give-up"}
STATUS_BADGE_TEXT = {"WAIT": "等待資金", "WATCH_CLOSELY": "接近確認",
                     "CONFIRMED": "A-flow 已確認", "PRICE_TRIGGERED": "PRICE TRIGGER 已發生",
                     "GIVE_UP": "暫時放棄"}


def _esc(s) -> str:
    return _html.escape(str(s)) if s is not None else ""


def _fmt(v, decimals=1) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def _direction_class(value) -> str:
    """台股方向色:上漲／流入為紅,下跌／流出為綠,零值不帶方向色。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number > 0:
        return "up"
    if number < 0:
        return "down"
    return ""


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
        return (f'現價 <strong>{_fmt(cur)}</strong>｜壓力 <strong>{_fmt(res)}</strong>｜'
                f'<span class="quote-direction up">已站上 +{dist:.2f}%</span>')
    d = f'{abs(dist):.2f}%' if dist is not None else '—'
    return (f'現價 <strong>{_fmt(cur)}</strong>｜壓力 <strong>{_fmt(res)}</strong>｜'
            f'<span class="quote-direction near">差 {d}</span>')


def _flow_display_html(r: dict) -> str:
    """保留 explain 的單一文字來源,只在渲染層補上方向色。"""
    exp = r["explain"]
    direction = _direction_class(r.get("flow_confirm_magnitude"))
    cls = f' class="value {direction}"' if direction else ' class="value"'
    return f'<span{cls}>{_esc(exp["flow_display"])}</span>'


def _change_pct_html(exp: dict) -> str:
    value = exp.get("change_pct")
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    direction = "up" if number > 0 else "down" if number < 0 else ""
    cls = f' class="price-change {direction}"' if direction else ' class="price-change"'
    return f'<span{cls}>漲跌 {number:+.2f}%</span>'


def _prob_block(exp: dict, discovery: bool, monitor_bucket: str = "") -> str:
    status = monitor_bucket or exp["status"]
    prob = exp.get("activation_prob")
    bar_class = ""

    if discovery:
        prob_num_html = '<span class="prob-num">LIVE</span>'
        bar_pct = _bar_pct(exp.get("distance_pct"))
        ref_line = "INTRADAY DISCOVERY：不套用 64.1% / 89.9% 歷史參考(母體不同)"
        title = "距離關鍵價"
    elif status in ("CONFIRMED", "PRICE_TRIGGERED"):
        # CONFIRMED 是 A-flow/Watch Mode 已確認,不自動代表價格站上關鍵價。
        # 價格低於 resistance 時,顯示仍差多少,避免把兩個 activation 混成一個。
        #
        # 2026-08-28 修:「已站上」是價格方向(站上關鍵價=漲),原本硬寫
        # var(--green) 違反台股漲紅跌綠。這個 span 跟下面的進度條(.bar)是同一
        # 塊「目前狀態」在講同一件事,兩者必須同色,不能一個紅一個綠自相矛盾。
        price_above = status == "PRICE_TRIGGERED"
        prob_num_html = (('<span class="prob-num price-trigger up">已站上</span>'
                          if price_above else
                          '<span class="prob-num flow-confirmed">A-flow 已確認</span>'))
        bar_pct = 100 if price_above else _bar_pct(exp.get("distance_pct"))
        bar_class = "price-trigger" if price_above else "flow-confirmed"
        # 2026-08-28 修:原本每張 CONFIRMED 卡片都印「歷史參考 89.9%」,51 檔
        # 看起來就是「每一檔都有 89.9% 勝率」。89.9% 是整個歷史母體的一個數字,
        # 不是任何個股的勝率,依定案規則只保留在頁首總覽,個股卡片不再出現。
        ref_line = 'A-flow 已完成確認（歷史母體統計見頁首，非本檔勝率）'
        title = "目前狀態"
    elif status == "FAILED":
        prob_num_html = '<span class="prob-num down">已失效</span>'
        bar_pct = 0
        ref_line = '今日資金／價格結構轉弱，保留在監控頁供複盤'
        title = "目前狀態"
    else:
        # 2026-08-27 修正:資金「已經」確認的卡片不得再寫「若 A-flow 完成確認」——
        # 它已經完成了,那句話會讓使用者把校準值(例:62%)跟歷史母體(89.9%)讀成
        # 模型自相矛盾。已確認時 89.9% 降級成純歷史母體參考;未確認才是條件句。
        confirmed = bool(exp.get("confirmed_so_far"))
        pct = round(prob * 100) if prob is not None else None
        # 同上:高機率=看漲訊號強,跟 CONFIRMED 分支的「已站上」是同一件事的
        # 不同階段,顏色必須一致,不能這裡綠、那裡紅。
        cls = ' class="prob-num up"' if (pct is not None and pct >= 70) else ' class="prob-num"'
        num = f'<span{cls}>{pct}%</span>' if pct is not None else '<span class="prob-num">—</span>'
        state = "資金已確認" if confirmed else "尚待資金確認"
        prob_num_html = f'{num}<span class="prob-state">｜{_esc(state)}</span>'
        bar_pct = pct if pct is not None else 0
        # 同上:個股卡片不再印 89.9%(頁首才是它的位置)。這一格的大數字 pct 是
        # 校準表算出來的「這一檔」的機率,那個是個股層級的、可以留;89.9% 不是。
        ref_line = ('資金已確認（歷史母體統計見頁首，非本檔勝率）' if confirmed
                    else '若 A-flow 完成確認 → 見頁首歷史母體統計')
        title = "目前預估啟動機率"

    return f"""<div class="prob">
        <div class="prob-top"><span class="prob-title">{title}</span>{prob_num_html}</div>
        <div class="bar {bar_class}"><i style="width:{bar_pct}%"></i></div>
        <div class="confirm-line">{ref_line}</div>
      </div>"""


def _stock_card(r: dict, discovery: bool = False) -> str:
    code = r["code"]
    name = NAME.get(code, code)
    exp = r["explain"]
    bucket = r.get("monitor_bucket") or ("DISCOVERY" if discovery else "")
    status = exp["status"]
    css = {"PRICE_TRIGGERED": "price-trigger", "CONFIRMED": "ok", "APPROACHING": "watch", "WAITING_FUNDS": "",
           "FAILED": "give-up", "DISCOVERY": "discovery"}.get(bucket,
           STATUS_CSS.get(status, ""))
    badge_text = _monitor.BUCKET_LABELS.get(bucket) or (
        "INTRADAY DISCOVERY" if discovery else STATUS_BADGE_TEXT.get(status, exp["status_label"]))
    card_css = ("price-triggered" if bucket == "PRICE_TRIGGERED" else
                "confirmed" if bucket == "CONFIRMED" else "")
    action_css = ("price-trigger" if bucket == "PRICE_TRIGGERED" else
                  "ok" if bucket == "CONFIRMED" else "")

    return f"""
    <article class="stock-card {card_css}">
      <div class="stock-top">
        <div class="stock-name"><small>{_esc(code)}</small>{_esc(name)}{_change_pct_html(exp)}</div>
        <div class="status {'discovery' if discovery else css}">{_esc(badge_text)}</div>
      </div>
      <div class="action {action_css}">{_esc(exp["system_sentence"])}</div>
      <div class="quote">{_quote_line(exp)}</div>
      <div class="row"><span class="label">昨日</span><span class="value">{_esc(exp["chip_summary"])}</span></div>
      <div class="row"><span class="label">今日</span>{_flow_display_html(r)}</div>
      {_prob_block(exp, discovery or bucket == "DISCOVERY", bucket)}
    </article>"""


def _top3_row(rank: int, r: dict) -> str:
    exp = r["explain"]
    code, name = r["code"], NAME.get(r["code"], r["code"])
    flow_css = _direction_class(r.get("flow_confirm_magnitude"))
    return (f'<div class="top3-row"><div class="rank">{rank}</div>'
           f'<div><b>{_esc(code)} {_esc(name)}</b></div>'
           f'<div>{_fmt(exp.get("current"))} / {_fmt(exp.get("resistance"))}</div>'
           f'<div class="{flow_css}">{_fmt(r.get("flow_confirm_magnitude"), 0)}</div>'
           f'<div>{_esc(exp["system_sentence"])}</div></div>')


def _discovery_row(r: dict) -> str:
    """⚠ 2026-08-27 修正:原本只印「距壓力 {abs(dist)}%」,把正負號吃掉了。
    盤中發現的股票幾乎都是「已經站上」關鍵價(distance_pct > 0),印成「距壓力
    4.79%」會被讀成「還要再漲 4.79% 才到」——方向剛好相反,看的人會以為還沒到、
    可以慢慢等,實際上它早就突破在跑。改用跟主卡片同一支 `_quote_line()`:
    現價/壓力都印出實際價格,並且區分「已站上 +X%」與「差 X%」。
    要判斷何時行動,看的人需要的是真實價格,不是一個沒有方向的百分比。"""
    exp = r["explain"]
    code, name = r["code"], NAME.get(r["code"], r["code"])
    return f"""
    <div class="discovery-item">
      <div class="disc-main">
        <div class="disc-name"><b>{_esc(code)} {_esc(name)}</b>
          <span class="badge">INTRADAY DISCOVERY</span></div>
        <div class="disc-quote">{_quote_line(exp)}</div>
        <div class="disc-meta">今日 A-flow {_fmt(r.get("flow_confirm_magnitude"), 0)}
          · {_esc(exp["system_sentence"])} · 收盤重算</div>
      </div>
    </div>"""


_REQUESTED_TAB_LABELS = {
    "PRICE_TRIGGERED": "PRICE TRIGGER 已發生",
    "CONFIRMED": "A-flow 已確認",
    "WAITING_FUNDS": "等待資金",
    "DISCOVERY": "盤中發現",
}
_OPTIONAL_TAB_LABELS = {
    "APPROACHING": "接近確認",
    "FAILED": "失敗／轉弱",
}


def _monitor_tabs_html(sections: list[dict]) -> str:
    present = {section.get("bucket") for section in sections}
    buckets = [bucket for bucket in _REQUESTED_TAB_LABELS if bucket in present]
    buckets += [bucket for bucket in _OPTIONAL_TAB_LABELS if bucket in present]
    # Keep the controls available even when a bucket is empty today.
    if not buckets:
        buckets = list(_REQUESTED_TAB_LABELS)
    counts = {section.get("bucket"): len(section.get("rows") or []) for section in sections}
    active = next((bucket for bucket in buckets if counts.get(bucket, 0)), buckets[0])
    labels = {**_REQUESTED_TAB_LABELS, **_OPTIONAL_TAB_LABELS}
    buttons = "".join(
        f'<button type="button" class="monitor-tab{" active" if bucket == active else ""}" '
        f'role="tab" aria-selected="{"true" if bucket == active else "false"}" '
        f'data-monitor-tab="{_esc(bucket)}">{_esc(labels[bucket])}'
        f'<span>{counts.get(bucket, 0)}</span></button>'
        for bucket in buckets
    )
    return f'''<div class="monitor-tabs" role="tablist" aria-label="買點監控分頁">
      {buttons}
    </div>
    <script>
      function selectMonitorTab(bucket) {{
        document.querySelectorAll('[data-monitor-tab]').forEach(function (tab) {{
          var selected = tab.dataset.monitorTab === bucket;
          tab.classList.toggle('active', selected);
          tab.setAttribute('aria-selected', selected ? 'true' : 'false');
        }});
        document.querySelectorAll('[data-monitor-panel]').forEach(function (panel) {{
          panel.hidden = panel.dataset.monitorPanel !== bucket;
        }});
      }}
      document.querySelectorAll('[data-monitor-tab]').forEach(function (tab) {{
        tab.addEventListener('click', function () {{ selectMonitorTab(tab.dataset.monitorTab); }});
      }});
    </script>'''


def _monitor_section_html(section: dict, active_bucket: str) -> str:
    rows = section.get("rows") or []
    cards = "".join(_stock_card(r) for r in rows)
    bucket = section.get("bucket")
    hidden = "" if bucket == active_bucket else " hidden"
    return f'''<section class="monitor-section" data-bucket="{_esc(bucket)}"
      data-monitor-panel="{_esc(bucket)}" role="tabpanel"{hidden}>
      <div class="section-title"><h3>{_esc(section.get("label"))}</h3><span>{len(rows)} 檔</span></div>
      <div class="grid">{cards}</div>
    </section>'''


PAGE_CSS = """
<style>
/* 色票直接沿用主站 intraday_decision_dataflow.html 的 :root,不另立一套色系。
   改了主站就要同步改這裡(兩邊都是手寫 CSS,沒有共用 build 流程)。 */
:root{--bg:#f4f6fb;--panel:#fff;--panel2:#f7f9fd;--line:#e5e9f2;--text:#182033;--muted:#73809a;
--navy:#17233f;--green:#0b9a6f;--green-soft:#e8f7f1;--red:#df4b67;--red-soft:#fff0f3;
--amber:#c78313;--amber-soft:#fff6df;--blue:#3b6eea;--shadow:0 12px 30px #19274714}
*{box-sizing:border-box}
body{margin:0;padding-top:122px;background:var(--bg);color:var(--text);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",Roboto,Arial,sans-serif}
html,body{overflow-x:hidden}
/* 固定選單:topbar + 分頁列都黏在頂端,捲動時不消失 */
.topbar{height:64px;background:#fff;border-bottom:1px solid var(--line);display:flex;
align-items:center;justify-content:space-between;padding:0 24px;position:fixed;top:0;left:0;right:0;z-index:20}
.brand{font-size:15px;font-weight:850;letter-spacing:.02em;color:var(--navy)}
.brand small{display:block;font-size:10px;font-weight:700;color:var(--muted);letter-spacing:.09em;margin-top:2px}
.topbar-right{font-size:12px;color:var(--muted);font-weight:700}
.pagenav{background:#fff;border-bottom:1px solid var(--line);padding:8px 24px;display:flex;
align-items:center;flex-wrap:wrap;gap:2px;position:fixed;top:64px;left:0;right:0;z-index:19}
.pagenav a{padding:9px 13px;border-radius:10px;color:#65718a;font-weight:700;white-space:nowrap;
text-decoration:none;font-size:13px}
.pagenav a:hover{background:var(--bg)}
.pagenav a.active{background:var(--navy);color:#fff}
.wrap{max-width:1600px;margin:0 auto;padding:24px 20px 50px}
.header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:22px}
.title{font-size:25px;font-weight:800;letter-spacing:-.3px;margin:0 0 6px;color:var(--navy)}
.sub{font-size:13px;color:var(--muted)}
.live{font-size:12px;color:var(--green);padding-top:4px;font-weight:800}
.summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:20px}
.summary-card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:15px 17px;box-shadow:var(--shadow)}
.summary-label{font-size:11px;color:var(--muted);margin-bottom:7px}
.summary-value{font-size:27px;font-weight:800;line-height:1}
.summary-hint{font-size:11px;color:var(--muted);margin-top:6px}
.section-title{display:flex;justify-content:space-between;align-items:end;margin:24px 0 12px}
.section-title h2{font-size:14px;margin:0;font-weight:750}
.section-title span{font-size:11px;color:var(--muted)}
.monitor-tabs{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px;padding:4px;background:var(--panel);border:1px solid var(--line);border-radius:13px}
.monitor-tab{border:0;border-radius:9px;padding:9px 13px;background:transparent;color:var(--muted);font:inherit;font-size:12px;font-weight:800;cursor:pointer}
.monitor-tab span{display:inline-grid;place-items:center;min-width:22px;margin-left:6px;padding:2px 6px;border-radius:999px;background:var(--bg);font-size:10px}
.monitor-tab:hover{background:var(--bg);color:var(--navy)}
.monitor-tab.active{background:var(--navy);color:#fff;box-shadow:0 3px 8px #17233f22}
.monitor-tab.active span{background:#dff7ec;color:#087f5b}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.monitor-radar{display:grid;gap:18px}
.monitor-section{display:grid;gap:0}
.monitor-section .section-title{margin:0 0 10px}
.monitor-section h3{font-size:13px;margin:0;font-weight:750}
.stock-card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;box-shadow:var(--shadow)}
.stock-card.confirmed{border-color:#9fd9c4;box-shadow:inset 0 0 0 1px #e8f7f1,var(--shadow)}
.stock-card.price-triggered{border-color:#f0a0ae;box-shadow:inset 0 0 0 1px #fff0f3,var(--shadow)}
.stock-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:15px}
.stock-name{font-size:18px;font-weight:800;letter-spacing:.1px}
.stock-name small{font-size:12px;color:var(--muted);font-weight:600;margin-right:7px}
.price-change{display:inline-block;margin-left:9px;font-size:12px;font-weight:850;letter-spacing:0}
.price-change.up{color:var(--red)}
.price-change.down{color:var(--green)}
.status{font-size:10px;font-weight:800;border:1px solid var(--line);border-radius:999px;padding:5px 8px;white-space:nowrap;color:var(--amber);background:var(--amber-soft)}
.status.ok{color:var(--green);background:var(--green-soft);border-color:#9fd9c4}
.status.price-trigger{color:#b52f4a;background:var(--red-soft);border-color:#f3bcc7}
.status.watch{color:var(--blue);background:#eaf0fe;border-color:#b9ccf7}
.status.give-up{color:var(--red);background:var(--red-soft);border-color:#f3bcc7}
.status.discovery{color:var(--blue);background:#eaf0fe;border-color:#b9ccf7}
.quote{font-size:13px;color:#4a5570;margin-bottom:13px;line-height:1.65}
.quote strong{font-size:15px;color:var(--navy)}
.row{display:flex;gap:8px;align-items:center;font-size:13px;line-height:1.8;flex-wrap:wrap}
.row .label{color:var(--muted);min-width:44px}
.row .value{font-weight:650}
/* 台股慣例:漲紅跌綠(與歐美相反)。方向色只套在價格／資金數值；
   .status.ok 與 .stock-card.confirmed 是流程狀態,維持綠色以表示確認成功。 */
.up{color:var(--red)}
.down{color:var(--green)}
.quote-direction{font-weight:800;white-space:nowrap}
.quote-direction.near{color:var(--amber)}
.row .value.up,.top3-row .up{color:var(--red)}
.row .value.down,.top3-row .down{color:var(--green)}
.prob{margin-top:15px;padding:13px 14px;background:var(--panel2);border:1px solid var(--line);border-radius:12px}
.prob-top{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:9px}
.prob-title{font-size:12px;color:var(--muted)}
.prob-num{font-size:19px;font-weight:850}
.prob-num.up{color:var(--red)}
.prob-num.flow-confirmed{color:var(--green)}
.prob-state{font-size:11px;color:var(--muted);font-weight:700;margin-left:2px}
.bar{height:6px;background:#e8ecf4;border-radius:999px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,#9fb0c4,var(--red))}
.bar.price-trigger i{background:linear-gradient(90deg,#f3a7b6,var(--red))}
.bar.flow-confirmed i{background:linear-gradient(90deg,#9fd9c4,var(--green))}
.confirm-line{font-size:12px;color:var(--muted);margin-top:9px}
.confirm-line b{color:var(--green);font-size:14px}
.action{margin:0 0 14px;padding:11px 12px;border:1px solid #f0dcac;background:var(--amber-soft);color:#7a5406;border-radius:11px;font-size:14px;font-weight:780;line-height:1.55}
.action.ok{border-color:#9fd9c4;background:var(--green-soft);color:#07714f}
.action.price-trigger{border-color:#f3bcc7;background:var(--red-soft);color:#b52f4a}
.top3{margin-top:10px;background:var(--panel);border:1px solid var(--line);border-radius:16px;overflow-x:auto}
.top3-row{display:grid;grid-template-columns:48px 1.3fr .9fr .9fr 1.4fr;gap:12px;align-items:center;padding:13px 16px;border-bottom:1px solid var(--line);font-size:12px;min-width:620px}
.top3-row:last-child{border-bottom:0}
.top3-head{color:var(--muted);background:var(--bg);font-size:10px;font-weight:700}
.rank{width:26px;height:26px;border-radius:8px;display:flex;align-items:center;justify-content:center;background:var(--bg);font-weight:800}
.discovery{margin-top:18px;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px}
.discovery h3{font-size:13px;margin:0 0 5px}
.discovery p{font-size:11px;color:var(--muted);margin:0 0 12px}
.discovery-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.discovery-item{padding:14px;border:1px solid var(--line);border-radius:12px;background:var(--panel2);font-size:12px}
.disc-name{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:14px;margin-bottom:6px}
.disc-quote{font-size:13px;color:#4a5570;line-height:1.6;margin-bottom:5px}
.disc-quote strong{font-size:15px;color:var(--navy)}
.disc-meta{font-size:11px;color:var(--muted);line-height:1.6}
.badge{font-size:10px;border-radius:999px;padding:4px 7px;background:#eaf0fe;color:var(--blue);border:1px solid #b9ccf7}
.empty-note{color:var(--muted);font-size:12px;padding:10px 0}
@media(max-width:780px){
  body{padding-top:118px}
  .topbar{height:62px;padding:0 14px}
  .pagenav{top:62px;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;padding:6px 12px;gap:3px}
  .pagenav::-webkit-scrollbar{display:none}
  .pagenav a{min-height:44px;display:inline-flex;align-items:center;padding:9px 13px;font-size:12px}
  .wrap{padding:14px 13px 40px}.header{flex-direction:column}
  .summary{grid-template-columns:1fr 1fr}.grid,.discovery-grid{grid-template-columns:1fr}
  .filters{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;margin:0 -13px 12px;padding:0 13px}
  .filters::-webkit-scrollbar{display:none}.filter{min-height:44px;flex:0 0 auto;display:inline-flex;align-items:center}
  .stock-card{padding:14px}.title{font-size:22px}
  .top3{display:block;overflow-x:auto}.top3-row{min-width:560px}
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
@media(max-width:780px){body{padding-top:120px}.topbar{height:64px;padding:0 14px}.pagenav{top:64px;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;padding:6px 12px;gap:3px}.pagenav::-webkit-scrollbar{display:none}.pagenav a{min-height:44px;padding:9px 12px;font-size:12px}.pagenav .nav-label{flex:none;padding:0 9px 0 2px}}
</style>
"""


# 固定選單(topbar + 分頁列)。連結沿用主站側欄同一組頁面,讓兩邊可以互相切換。
# 用相對路徑,所以本機/VPS 都通;不像主站那樣需要判斷 file: 協定(這頁一定是
# server 端算的,不會被當本機檔案直接開)。
_NAV_PAGES = [
    ("/", "決策首頁", "OFF"), ("/", "機會雷達", "51"),
    ("/chips", "觀察池 51 檔", "51"), ("/chips", "籌碼", "51"),
    ("/reversal-lab", "資金反轉驗證", "LIVE"),
    ("/", "盤後驗證", None), ("/opportunity-ledger", "機會分層榜", None),
    ("/line-b-ledger", "買點監控", None), ("/line-b-layers", "七層交易狀態", None),
]


def _shell(ctx: dict, inner: str) -> str:
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>MLS 買點監控</title>
  {PAGE_CSS}{NAV_CSS}
</head>
<body>
{nav_html("line-b")}
{inner}
</body>
</html>"""


def render(ctx: dict) -> str:
    if not ctx.get("has_data"):
        return _shell(ctx, '<main class="wrap"><div class="empty-note">No Line B ledger data yet.</div></main>')

    labels = ctx["labels"]
    key_hold = ctx.get("key_price_hold") or {}
    key_hold_rate = key_hold.get("hold_rate")
    key_hold_value = f'{key_hold_rate:.1f}%' if key_hold_rate is not None else '資料累積中'
    key_hold_note = (
        f'觸及 {key_hold.get("triggered", 0)} 檔、守住 {key_hold.get("held", 0)} 檔 · '
        f'{key_hold.get("n_days", 0)} 個交易日 · 同條件 C1+C2 母體'
        + (' · 單日初始統計' if key_hold.get("n_days", 0) <= 1 else '')
    ) if key_hold.get("scored", 0) else '尚無足夠的關鍵價觸發收盤資料'
    monitor_sections = ctx.get("monitor_sections") or []
    if monitor_sections:
        active_bucket = next((section.get("bucket") for section in monitor_sections
                              if section.get("rows")), monitor_sections[0].get("bucket"))
        observation_html = (_monitor_tabs_html(monitor_sections) +
                            "".join(_monitor_section_html(section, active_bucket)
                                    for section in monitor_sections))
    else:
        observation_rows = ctx.get("observation_list", ctx["c1_c2_list"])
        observation_html = "".join(
            _stock_card(r, discovery=r.get("source") == "INTRADAY_DISCOVERY")
            for r in observation_rows
        ) or '<div class="empty-note">今晚無可監控資料。</div>'
    top3_rows = "".join(_top3_row(i + 1, r) for i, r in enumerate(ctx["flow_confirmed_top3"]))
    discovery_note = (
        '<section class="discovery discovery-note">'
        '<h3>盤中發現說明</h3>'
        '<p>已將盤中突破但昨晚未通過 C1+C2 的股票併入上方觀察清單；'
        '仍保留 INTRADAY DISCOVERY 標籤，且不納入 64.1%／89.9% 歷史統計。</p>'
        '</section>'
        if ctx["intraday_discovery"] else ''
    )

    return _shell(ctx, f"""
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
      <div class="summary-label">{_esc(labels.get("flow_confirmed_label", "A-flow 確認後累積命中率"))}</div>
      <div class="summary-value" style="color:var(--green)">{_esc(labels["flow_confirmed_rate"])}</div>
      <div class="summary-hint">OPEN_POSITIVE / FLOW_FLIP · {_esc(labels.get("flow_confirmed_hint", labels.get("flow_confirmed_sample_note", "資料累積中")))}</div>
    </div>
    <div class="summary-card">
      <div class="summary-label">關鍵價觸發後收盤守住率</div>
      <div class="summary-value" style="color:var(--green)">{_esc(key_hold_value)}</div>
      <div class="summary-hint">{_esc(key_hold_note)} · 非本檔個股勝率</div>
    </div>
  </section>

  <div class="section-title"><h2>今日買點雷達</h2><span>C1/C2 研究來源不變；監控呈現補上接近、等待、發現與失效</span></div>
  <section class="monitor-radar">{observation_html}</section>

  <div class="section-title"><h2>A-flow CONFIRMED TOP 3</h2><span>從全部監控列挑選，依 A-flow 幅度排序</span></div>
  <section class="top3">
    <div class="top3-row top3-head"><div>排名</div><div>股票</div><div>現價 / 壓力</div><div>A-flow</div><div>現在動作</div></div>
    {top3_rows or '<div class="empty-note" style="padding:16px">尚無確認候選。</div>'}
  </section>

  {discovery_note}
</main>""")
