"""Fetch live VPS data once and generate evidence JSON + static HTML."""
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


def _card(c: dict, reversal: bool = False) -> str:
    state = c["reversal_state"] if reversal else c["flow_state"]
    reasons = c["reversal_reason_codes"] if reversal else c["reason_codes"]
    af = c.get("aflow")
    change = c.get("change_rate")
    return f"""
    <article class='card'>
      <div class='head'><div><b>{html.escape(c.get('symbol',''))} {html.escape(c.get('name') or '')}</b><small>{html.escape(c.get('sector') or '')}</small></div>
      <div class='right'><strong>{_fmt(c.get('price'))}</strong><span class='chg'>{'+' if (change or 0)>0 else ''}{_fmt(change)}%</span></div></div>
      <div class='flow'>A-flow <b>{'+' if (af or 0)>0 else ''}{_fmt(af,0)}</b> · ratio {_fmt((c.get('aflow_ratio') or 0)*100)}% · 均價 {_fmt(c.get('avg_price'))}</div>
      <div class='state'>{html.escape(state)}</div>
      <div class='action'>{html.escape(c.get('action') or 'OBSERVE_ONLY')}</div>
      <div class='reasons'>{' · '.join(html.escape(x) for x in reasons)}</div>
      <div class='chips'>外資D {_fmt(c.get('foreign_net_d'),0)} · 5D {_fmt(c.get('foreign_net_5d'),0)} · 20D {_fmt(c.get('foreign_net_20d'),0)}<br>
      source: {html.escape(c.get('foreign_source') or '—')} @ {html.escape(c.get('foreign_source_date') or '—')} · price: {html.escape(c.get('price_source') or '—')} / {html.escape(c.get('quote_status') or '—')}</div>
    </article>"""


def render_html(view: dict) -> str:
    css = """
    body{font-family:-apple-system,BlinkMacSystemFont,'PingFang TC',sans-serif;background:#f7f8fb;color:#172033;max-width:820px;margin:auto;padding:18px}
    h1{font-size:22px;margin:0}.meta{color:#71809b;font-size:12px;margin:6px 0 14px}.tabs{display:flex;gap:6px;margin:14px 0;position:sticky;top:0;background:#f7f8fb;padding:8px 0}.tabs a{flex:1;text-align:center;padding:10px;border-radius:10px;background:#e9edf5;color:#172033;text-decoration:none;font-weight:800}.section{scroll-margin-top:60px}.card{background:white;border:1px solid #dfe5ef;border-radius:16px;padding:14px;margin:10px 0;box-shadow:0 3px 14px rgba(35,53,84,.05)}.head{display:flex;justify-content:space-between}.head b{font-size:18px}.head small{display:block;color:#74829b;margin-top:3px}.right{text-align:right}.right strong{font-size:20px}.chg{display:block;color:#e14d68;font-weight:800}.flow{margin-top:10px;color:#59677e}.state{font-size:12px;font-weight:900;color:#8b6c12;margin-top:10px}.action{font-size:16px;font-weight:900;margin-top:2px}.reasons{margin-top:6px;color:#4f5d74;font-size:12px}.chips{margin-top:10px;padding:9px;background:#f8f6ef;border-radius:10px;color:#6b6251;font-size:11px;line-height:1.6}
    """
    return f"""<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>MLS v4.1 LIVE</title><style>{css}</style></head><body>
    <h1>MLS v4.1 · Flow × Chips LIVE</h1><div class='meta'>VPS read-only snapshot · {html.escape(str(view.get('updated_at') or view.get('snapshot') or '—'))}</div>
    <div class='tabs'><a href='#in'>流入</a><a href='#out'>流出</a><a href='#rev'>反轉</a></div>
    <section id='in' class='section'><h2>資金流入 TOP10</h2>{''.join(_card(c) for c in view['inflow'])}</section>
    <section id='out' class='section'><h2>資金流出 TOP10</h2>{''.join(_card(c) for c in view['outflow'])}</section>
    <section id='rev' class='section'><h2>反轉／失敗原因</h2><p class='meta'>Persistence 需 30–90 分鐘歷史 snapshot；目前 live endpoint 僅最新 snapshot，因此一律不偽造。</p>{''.join(_card(c, True) for c in view['reversal'])}</section>
    </body></html>"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=None)
    p.add_argument("--json", default="live_vps_result.json")
    p.add_argument("--html", default="live_vps_snapshot.html")
    args = p.parse_args()
    payload = fetch_live_rows(args.url) if args.url else fetch_live_rows()
    view = build_live_view(payload)
    Path(args.json).write_text(json.dumps(view, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(args.html).write_text(render_html(view), encoding="utf-8")
    print(json.dumps({
        "snapshot": view.get("updated_at") or view.get("snapshot"),
        "inflow": [(c["symbol"], c["aflow"], c["flow_state"]) for c in view["inflow"]],
        "outflow": [(c["symbol"], c["aflow"], c["flow_state"]) for c in view["outflow"]],
        "reversal": [(c["symbol"], c["reversal_state"]) for c in view["reversal"][:12]],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
