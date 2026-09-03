"""Shared MLS navigation contract for every rendered page."""
from __future__ import annotations

from html import escape
from typing import Optional


NAV_ITEMS = (
    ("home", "決策首頁", "/", "OFF"),
    ("radar", "機會雷達", "/?view=radar", "51"),
    ("watch", "觀察池 51 檔", "/?view=watch", "51"),
    ("chips", "籌碼", "/chips", "51"),
    ("reversal", "資金反轉驗證", "/reversal-lab", "LIVE"),
    ("review", "盤後驗證", "/review", ""),
    ("opportunity", "機會分層榜", "/opportunity-ledger", ""),
    ("line-b", "買點監控", "/line-b-ledger", ""),
    ("layers", "七層交易狀態", "/line-b-layers", ""),
)


NAV_CSS = """
<style data-mls-navigation>
html,body{min-height:100%}
body{padding:0 0 40px!important}
.topbar,.pagenav,.global-topbar,.global-nav{display:none!important}
.mls-nav{position:sticky;top:0;z-index:100;display:flex;align-items:center;gap:6px 10px;
  width:100vw;max-width:100vw;min-width:0;min-height:72px;margin-left:calc(50% - 50vw);margin-right:calc(50% - 50vw);
  box-sizing:border-box;padding:12px 28px;background:#fff;border-bottom:1px solid #e5e9f2;
  box-shadow:0 2px 10px rgba(23,35,63,.04);overflow-x:auto;overflow-y:hidden;flex-wrap:nowrap;
  overscroll-behavior-x:contain;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.mls-nav::-webkit-scrollbar{display:none}
.mls-nav-label{display:inline-flex;align-items:center;flex:none;margin-right:8px;color:#8a96aa;font-size:14px;font-weight:850;letter-spacing:.04em;text-decoration:none;cursor:pointer}
.mls-nav-label:hover{color:#17233f}
.mls-nav-link{display:inline-flex;align-items:center;gap:7px;min-height:40px;margin:0;padding:9px 12px;
  border:0;border-radius:8px;color:#65718a;background:transparent;font:inherit;font-size:16px;font-weight:800;
  line-height:1.2;text-decoration:none;white-space:nowrap;flex:0 0 auto}
.mls-nav-link:hover{background:#f4f6fb;color:#17233f}
.mls-nav-link.active{background:#17233f;color:#fff}
.mls-nav-text{min-width:0}
.mls-nav-count{flex:none;padding:3px 7px;border-radius:999px;background:#eef1f7;color:#65718a;
  font-size:12px;font-weight:850;line-height:1}
.mls-nav-link.active .mls-nav-count{background:#dff7ec;color:#087f5b}
.mls-nav-count.live{background:#dff7ec;color:#087f5b;border:1px solid #a9e8cc}
/* 所有分頁共用同一條全視窗導覽；內容頁可窄，但導覽不跟著內容欄縮窄。 */
@media(max-width:1100px){
  .mls-nav{min-height:64px;padding:10px 14px;gap:5px 7px}
  .mls-nav-label{display:none}.mls-nav-link{min-height:44px;padding:9px 10px;font-size:14px;flex:0 0 auto}
}
@media(max-width:560px){
  .mls-nav{min-height:58px;padding:7px 12px;gap:4px;flex-wrap:wrap;overflow-x:visible;overflow-y:visible;align-content:center}
  .mls-nav-link{min-height:44px;padding:9px 10px;font-size:13px}
}
</style>
"""


def nav_html(active: Optional[str] = None) -> str:
    links = []
    for key, label, href, badge in NAV_ITEMS:
        active_class = " active" if key == active else ""
        badge_html = (
            f'<span class="mls-nav-count{" live" if badge == "LIVE" else ""}">{escape(badge)}</span>'
            if badge else ""
        )
        link_content = f'<span class="mls-nav-text">{escape(label)}</span>{badge_html}'
        if key == active:
            # 目前所在頁不要再導向自己，避免整頁重載、捲軸跳回頂端。
            links.append(
                f'<span class="mls-nav-link{active_class}" aria-current="page">'
                f'{link_content}</span>'
            )
        else:
            links.append(
                f'<a class="mls-nav-link{active_class}" href="{escape(href, quote=True)}">'
                f'{link_content}</a>'
            )
    return '<aside class="mls-nav" aria-label="主要選單"><a class="mls-nav-label" href="/">盤中決策</a>' + "".join(links) + "</aside>"
