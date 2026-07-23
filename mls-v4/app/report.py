"""
MLS v4.0 — report.py
報告產生：每日報告、週追蹤報告。
檔名規則：每日 每日報告_MMDD.html；週報 半導體鏈資金熱度_週追蹤_MMDD.html
一天一檔，永不覆蓋。
"""
import os
import config as C
import db
import decision


def _ensure_dir():
    os.makedirs(C.REPORT_DIR, exist_ok=True)


def daily_report(trade_date=None):
    """產生當日報告 HTML，回傳檔案路徑。"""
    _ensure_dir()
    trade_date = trade_date or db.today()
    rows = db.load_dec_health(trade_date)
    wl = db.load_watchlist(db.today())
    mmdd = trade_date[5:].replace("-", "")
    path = os.path.join(C.REPORT_DIR, f"每日報告_{mmdd}.html")

    ready = [r for r in rows if r["grade"] == "Ready"]
    in_n = sum(1 for r in rows if r["quad"].startswith("in"))

    body = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>每日報告 {trade_date}</title>
<style>body{{font-family:-apple-system,"PingFang TC",system-ui;background:#f4f1ea;
color:#1a1d23;max-width:800px;margin:0 auto;padding:20px;line-height:1.7}}
h1{{color:#8a6d1a;font-size:22px}}h2{{color:#8a6d1a;font-size:17px;border-bottom:2px solid #e4e0d6;padding-bottom:6px;margin-top:24px}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0}}
th{{background:#1a1d23;color:#fff;padding:7px;text-align:left}}
td{{padding:7px;border-bottom:1px solid #efece4}}
.up{{color:#c62828;font-weight:700}}.down{{color:#1b7d3f;font-weight:700}}</style></head><body>
<h1>📊 MLS 每日報告 · {trade_date}</h1>
<p>觀察池 {len(rows)} 檔 · 資金流入 {in_n} 檔 · Ready {len(ready)} 檔</p>
<h2>1. 今日 Ready 標的</h2>
<table><tr><th>代號</th><th>名稱</th><th>族群</th><th>象限</th><th>健康分</th><th>承接★</th><th>法人淨</th></tr>
{''.join(f'<tr><td>{r["code"]}</td><td>{r["name"]}</td><td>{r["sector"]}</td>'
         f'<td>{decision.QUAD_NAME.get(r["quad"],r["quad"])}</td><td>{r["score"]}</td>'
         f'<td>{"★"*r["stars"]}</td><td>{r["inst_net_20d"]:+,}張</td></tr>' for r in ready) or '<tr><td colspan=7>今日無 Ready 標的，休息也是部位。</td></tr>'}
</table>
<h2>2. 明日觀察清單</h2>
<table><tr><th>代號</th><th>名稱</th><th>軌道</th><th>分級</th><th>理由</th></tr>
{''.join(f'<tr><td>{w["code"]}</td><td>{w["name"]}</td><td>{"引擎軌" if w["track"]=="engine" else "攻擊軌"}</td>'
         f'<td>{w["grade"]}</td><td>{w.get("reason","")}</td></tr>' for w in wl) or '<tr><td colspan=5>無</td></tr>'}
</table>
<p style="color:#2a2e36;font-size:12px;margin-top:24px">本報告由 MLS v4.0 自動生成 · 資料階段 eod_final（盤後蓋章）</p>
</body></html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def list_reports():
    """列出報告庫所有檔案（按日期新→舊）。"""
    _ensure_dir()
    files = []
    for fn in sorted(os.listdir(C.REPORT_DIR), reverse=True):
        if fn.endswith(".html"):
            kind = "週報" if "週追蹤" in fn else "每日"
            files.append({"name": fn, "kind": kind,
                          "path": os.path.join(C.REPORT_DIR, fn)})
    return files
