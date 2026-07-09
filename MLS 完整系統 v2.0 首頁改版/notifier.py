"""
MLS 標準版 — notifier.py
Telegram 分級推播。環境變數:
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
未設定時自動降級為 console 輸出(dry-run),系統照常運作。

冷卻(交接規格書 v2):
    高確定進場 entry_high  同一股 10 分鐘 1 次
    一般進場   entry       同一股 10 分鐘 1 次
    高潛力     potential   同一股 30 分鐘 1 次
    風險       risk        同一股 10 分鐘 1 次
    族群鎖定   sector_lock 同一族群 5 分鐘 1 次
"""

import os
import json
import time
import urllib.request
import urllib.parse

COOLDOWN_SEC = {
    "entry_high": 600, "entry": 600,
    "potential": 1800, "risk": 600, "sector_lock": 300,
}

_last_push = {}   # key=(kind, id) → epoch


def _cooldown_ok(kind, key_id):
    k = (kind, key_id)
    now = time.time()
    if now - _last_push.get(k, 0) < COOLDOWN_SEC.get(kind, 600):
        return False
    _last_push[k] = now
    return True


def _send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print(f"[notifier/dry-run] {text}")
        return True
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": text, "parse_mode": "Markdown",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception as e:
        print(f"[notifier] Telegram 失敗: {e}")
        return False


# ── 事件模板 ──────────────────────────────────────────
def push_signal(sig):
    """個股訊號。回傳是否實際推播(冷卻中則 False)。"""
    kind = sig.get("event_class", "entry")
    if not _cooldown_ok(kind, sig["code"]):
        return False
    icon = {"entry_high": "🔴", "entry": "🔺",
            "potential": "🟡", "risk": "🟢"}.get(kind, "▫️")
    head = {"entry_high": "高確定進場", "entry": "進場訊號",
            "potential": "高潛力觀察", "risk": "出場/風險"}.get(kind, "訊號")
    hit = " ★預判命中" if sig.get("is_watchlist_hit") else ""
    lines = [
        f"{icon} *{head}*{hit}",
        f"*{sig['name']}* `{sig['code']}` {sig['sector']}",
        f"價 *{sig.get('price')}*  ({'+' if (sig.get('change_rate') or 0) > 0 else ''}{sig.get('change_rate')}%)  量比 {sig.get('volume_ratio')}",
        f"規則:{'、'.join(sig.get('rules', []))}",
    ]
    if sig.get("suggested_stop") and kind != "risk":
        lines.append(f"建議停損 *{sig['suggested_stop']}*")
    if sig.get("gate_note"):
        lines.append(f"⚠️ {sig['gate_note']}")
    return _send("\n".join(lines))


def push_sector_lock(sector):
    if not _cooldown_ok("sector_lock", sector["name"]):
        return False
    return _send(
        f"🔵 *族群鎖定* {sector['name']} (#{sector['rank']})\n"
        f"中位漲幅 {'+' if sector['pct'] > 0 else ''}{sector['pct']}%  "
        f"Flow {sector['flow_score']}  成交佔比 {sector['amount_share']}%")


def push_summary(text):
    return _send(text)
