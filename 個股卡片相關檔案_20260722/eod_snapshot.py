"""Single local, read-only EOD snapshot for the after-hours UI."""

import json
from pathlib import Path

STAGE_PRELIMINARY = "preliminary"
STAGE_FINAL = "final"
_STAGE_RANK = {STAGE_PRELIMINARY: 1, STAGE_FINAL: 2}
DEFAULT_FILENAME = "eod_snapshot.json"


def snapshot_path(base_dir):
    return Path(base_dir) / DEFAULT_FILENAME


def _section(payload, name, loader):
    try:
        value = loader()
        payload["sections"][name] = "ok" if value else "empty"
        return value
    except Exception as exc:
        payload["sections"][name] = f"error: {exc}"
        return None


def build(trade_date, stage, load_rows, load_market, generated_at,
          trading_day=True):
    payload = {
        "ok": True, "trade_date": trade_date, "stage": stage,
        "generated_at": generated_at, "trading_day": trading_day,
        "session_closed": True, "sections": {}, "rows": [], "count": 0,
        "category_counts": {}, "regime": None, "breadth": None,
        "market": None, "intraday_updated_at": None,
    }
    result = _section(payload, "rows", load_rows) or {}
    rows = [r for r in (result.get("rows") or []) if r.get("code")]
    payload["rows"] = rows
    payload["count"] = len(rows)
    payload["category_counts"] = result.get("category_counts") or {}
    payload["regime"] = result.get("regime")
    payload["breadth"] = result.get("breadth")
    payload["intraday_updated_at"] = result.get("updated_at")
    payload["market"] = _section(payload, "market", load_market)
    payload["ok"] = bool(rows) or bool(payload["market"])
    return payload


def quality(payload):
    payload = payload or {}
    return (str(payload.get("trade_date") or ""),
            _STAGE_RANK.get(payload.get("stage"), 0),
            len(payload.get("rows") or []),
            1 if payload.get("market") else 0)


def should_replace(old, new):
    if not old:
        return True
    if not (new or {}).get("rows") and (old or {}).get("rows"):
        return False
    old_date = str((old or {}).get("trade_date") or "")
    new_date = str((new or {}).get("trade_date") or "")
    if new_date != old_date:
        return new_date > old_date
    return quality(new) >= quality(old)


def write(path, payload):
    path = Path(path)
    try:
        old = None
        if path.exists():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        if not should_replace(old, payload):
            print("[eod_snapshot] 新快照品質不優於既有快照，保留原檔", flush=True)
            return False
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        print(f"[eod_snapshot] {payload.get('trade_date')} stage={payload.get('stage')} "
              f"rows={payload.get('count')}", flush=True)
        return True
    except Exception as exc:
        print(f"[eod_snapshot] 寫入失敗: {exc}", flush=True)
        return False


def read(path, trade_date=None):
    path = Path(path)
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not payload.get("trade_date"):
            return None
        payload = dict(payload)
        payload["data_date"] = payload.get("trade_date")
        payload["stale"] = bool(trade_date and payload["trade_date"] != trade_date)
        return payload
    except Exception as exc:
        print(f"[eod_snapshot] 讀取失敗: {exc}", flush=True)
        return None
