"""Fetch live VPS data once and generate Reversal Lab evidence JSON + HTML."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from .live_bridge import build_live_view, fetch_live_rows


def _fmt(v, digits=2):
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:,.{digits}f}"
    return str(v)


def _badge(text, kind="neutral"):
    return f"<span class='badge {kind}'>{html.escape(str(text))}</span>"


def _pipeline(c: dict) -> str:
    prior_outflow = (c.get("foreign_net_5d") or 0) < 0 or (c.get("foreign_net_20d") or 0) < 0
    flow_flip = "YES" if prior_outflow and (c.get("aflow") or 0) > 0 else "NO"
    persistence = c.get("flow_persistence") or "NO_DATA"
    price_conf = c.get("price_confirmation") or "NO_DATA"
    sector_conf = c.get("sector_confirmation") or "NO_DATA"
    day2 = c.get("day2_ready") or "N/A"
    return f"""
      <div class='pipeline'>
        <div><small>Flow Flip</small><b>{html.escape(flow_flip)}</b></div>
        <div><small>Flow Persistence</small><b>{html.escape(str(persistence))}</b></div>
        <div><small>Price Confirmation</small><b>{html.escape(str(price_conf))}</b></div>
        <div><small>Sector Confirmation</small><b>{html.escape(str(sector_conf))}</b></div>
        <div><small>Day-2 Ready</small><b>{html.escape(str(day2))}</b></div>
      </div>"""


def _card(c: dict, reversal: bool = False) -> str:
    state = c["reversal_state"] if reversal else c["flow_state"]
    reasons = c["reversal_reason_codes"] if reversal else c["reason_codes"]
    af = c.get("aflow")
    change = c.get("change_rate")
    role = c.get("lab_role") or "—"
    grade = c.get("reversal_grade")
    role_badges = _badge(role, "lab") + (" " + _badge(grade, "grade") if grade else "")
    return f"""
    <article class='card'>
      <div class='head'><div><b>{html.escape(c.get('symbol',''))} {html.escape(c.get('name') or '')}</b><small>{html.escape(c.get('sector') or '')}</small></div>
      <div class='right'><strong>{_fmt(c.get('price'))}</strong><span class='chg {'up' if (change or 0) >= 0 else 'down'}'>{'+' if (change or 0)>0 else ''}{_fmt(change)}%</span></div></div>
      <div class='badges'>{role_badges}</div>
      <div class='flow'>A-flow <b>{'+' if (af or 0)>0 else ''}{_fmt(af,0)}</b> · ratio {_fmt((c.get('aflow_ratio') or 0)*100)}% · VWAP/均價 {_fmt(c.get('avg_price'))}</div>
      {_pipeline(c) if reversal else ''}
      <div class='state'>{html.escape(state)}</div>
      <div class='action'>{html.escape(c.get('action') or 'OBSERVE_ONLY')}</div>
      <div class='reasons'>{' · '.join(html.escape(x) for x in reasons)}</div>
      <div class='chips'>T-1 外資籌碼：D {_fmt(c.get('foreign_net_d'),0)} · 5D {_fmt(c.get('foreign_net_5d'),0)} · 20D {_fmt(c.get('foreign_net_20d'),0)}<br>
      source: {html.escape(c.get('foreign_source') or '—')} @ {html.escape(c.get('foreign_source_date') or '—')} · intraday: {html.escape(c.get('price_source') or '—')} / {html.escape(c.get('quote_status') or '—')}</div>
    </article>"""


def render_html(view: dict) -> str:
    lab_name = view.get("lab_name") or "資金反轉驗證 / Reversal Lab"
    scope = view.get("model_scope") or "FORWARD_TEST_ONLY"
    css = """
    *{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'PingFang TC','Noto Sans TC',sans-serif;background:#f6f8fc;color:#172033;max-width:860px;margin:auto;padding:18px;line-height:1.35}
    h1{font-size:24px;margin:0}.sub{color:#66758f;font-size:13px;margin:5px 0 0}.notice{background:#fff8df;border:1px solid #efd888;border-radius:14px;padding:12px 14px;margin:14px 0;color:#68551a;font-size:13px}.notice b{display:block;color:#302c20;margin-bottom:3px}.tabs{display:flex;gap:7px;margin:14px 0;position:sticky;top:0;z-index:10;background:#f6f8fc;padding:8px 0}.tabs a{flex:1;text-align:center;padding:11px;border-radius:11px;background:#e8edf6;color:#172033;text-decoration:none;font-weight:800}.section{scroll-margin-top:70px}.section h2{font-size:18px;margin:18px 0 8px}.meta{color:#71809b;font-size:12px;margin:6px 0 14px}.card{background:white;border:1px solid #dfe5ef;border-radius:16px;padding:14px;margin:10px 0;box-shadow:0 3px 14px rgba(35,53,84,.05)}.head{display:flex;justify-content:space-between;gap:12px}.head b{font-size:18px}.head small{display:block;color:#74829b;margin-top:3px}.right{text-align:right;white-space:nowrap}.right strong{font-size:20px}.chg{display:block;font-weight:800}.chg.up{color:#e14d68}.chg.down{color:#05966f}.badges{margin:9px 0 2px}.badge{display:inline-block;padding:4px 7px;border-radius:7px;font-size:11px;font-weight:850;border:1px solid #d6ddec;background:#f7f9fc}.badge.lab{color:#7a5b09;background:#fff8df;border-color:#efd888}.badge.grade{color:#7d2db1;background:#f7edff;border-color:#dcbcf2}.flow{margin-top:9px;color:#59677e}.pipeline{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-top:12px}.pipeline div{background:#f7f9fc;border:1px solid #e2e7f0;border-radius:10px;padding:8px;min-width:0}.pipeline small{display:block;color:#7a879d;font-size:9px;line-height:1.2}.pipeline b{display:block;margin-top:3px;font-size:11px;overflow-wrap:anywhere}.state{font-size:12px;font-weight:900;color:#8b6c12;margin-top:10px}.action{font-size:16px;font-weight:900;margin-top:2px}.reasons{margin-top:6px;color:#4f5d74;font-size:12px}.chips{margin-top:10px;padding:9px;background:#f8f6ef;border-radius:10px;color:#6b6251;font-size:11px;line-height:1.6}@media(max-width:640px){body{padding:12px}.pipeline{grid-template-columns:1fr 1fr}.pipeline div:last-child{grid-column:1/-1}.head b{font-size:17px}.right strong{font-size:18px}}
    """
    return f"""<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(lab_name)}</title><style>{css}</style></head><body>
    <h1>{html.escape(lab_name)}</h1><div class='sub'>T-1 前期資金流出 → T 日資金翻正 → 股價轉強 → T+1～T+3 延續驗證</div>
    <div class='meta'>VPS read-only snapshot · {html.escape(str(view.get('updated_at') or view.get('snapshot') or '—'))}</div>
    <div class='notice'><b>{html.escape(scope.replace('_',' '))} · 不影響正式 Trend / Entry</b>此頁只做 forward test；不套用「CHIP NEGATIVE 就剔除」，前期大流出股票會保留到 Reversal Lab 觀察。</div>
    <div class='tabs'><a href='#in'>流入</a><a href='#out'>流出</a><a href='#rev'>反轉</a></div>
    <section id='in' class='section'><h2>資金流入 TOP10</h2>{''.join(_card(c) for c in view['inflow'])}</section>
    <section id='out' class='section'><h2>資金流出 TOP10</h2>{''.join(_card(c) for c in view['outflow'])}</section>
    <section id='rev' class='section'><h2>反轉驗證 / Reversal Lab</h2><p class='meta'>固定流程：Flow Flip → Flow Persistence → Price Confirmation → Sector Confirmation → Day-2 Ready。Flow Persistence 必須有多時點 A-flow；單一 snapshot 絕不補造。</p>{''.join(_card(c, True) for c in view['reversal'])}</section>
    </body></html>"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=None)
    p.add_argument("--json", default="reversal_lab_live.json")
    p.add_argument("--html", default="reversal_lab_live.html")
    args = p.parse_args()
    payload = fetch_live_rows(args.url) if args.url else fetch_live_rows()
    view = build_live_view(payload)
    Path(args.json).write_text(json.dumps(view, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(args.html).write_text(render_html(view), encoding="utf-8")
    print(json.dumps({
        "lab": view.get("lab_name"),
        "scope": view.get("model_scope"),
        "snapshot": view.get("updated_at") or view.get("snapshot"),
        "inflow": [(c["symbol"], c["aflow"], c["flow_state"]) for c in view["inflow"]],
        "outflow": [(c["symbol"], c["aflow"], c["flow_state"]) for c in view["outflow"]],
        "reversal": [(c["symbol"], c["lab_role"], c.get("reversal_grade"), c["reversal_state"]) for c in view["reversal"][:12]],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
