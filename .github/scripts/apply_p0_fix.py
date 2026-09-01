from pathlib import Path
import re

BASE = Path("MLS整系統最終版v1.2")


def sub(path, pattern, repl, flags=re.S):
    p = BASE / path
    text = p.read_text(encoding="utf-8")
    out, count = re.subn(pattern, repl, text, flags=flags)
    if count != 1:
        raise RuntimeError(f"{path}: expected 1 replacement, got {count}")
    p.write_text(out, encoding="utf-8")


# 1) Snapshot API must never synchronously fetch full-day ticks.
sub(
    "broker.py",
    r"def batch_snapshots\(codes\):.*?\n\ndef index_snapshot\(\):",
    '''def batch_snapshots(codes):
    """Batch snapshots with unambiguous data semantics.

    Snapshot.buy_volume/sell_volume are best bid/ask quote-queue quantities,
    not active-trade flow. Never overload them as A-flow, and never call the
    full-day ticks endpoint synchronously from the card/request path.
    """
    api = get_api()
    contracts = []
    for c in codes:
        try:
            contracts.append(api.Contracts.Stocks[c])
        except Exception:
            continue

    out = []
    for i in range(0, len(contracts), 400):
        try:
            snaps = api.snapshots(contracts[i:i + 400])
        except Exception as e:
            print(f"[broker] snapshots 批次失敗: {e}")
            time.sleep(1)
            continue
        for s in snaps:
            quote_buy = getattr(s, "buy_volume", 0) or 0
            quote_sell = getattr(s, "sell_volume", 0) or 0
            out.append({
                "code": s.code,
                "price": s.close,
                "open": s.open, "high": s.high, "low": s.low,
                "change_rate": s.change_rate,
                "volume_ratio": getattr(s, "volume_ratio", 0) or 0,
                "total_volume": (s.total_volume or 0),
                "total_amount": (s.total_amount or 0),
                "avg_price": getattr(s, "average_price", None),
                "tick_type": getattr(s, "tick_type", None),
                "bid_volume": quote_buy,
                "ask_volume": quote_sell,
                # Legacy ambiguous fields are deliberately unavailable.
                "buy_volume": None,
                "sell_volume": None,
                # Canonical A-flow is injected by a dedicated verified source.
                "active_buy_volume": None,
                "active_sell_volume": None,
                "active_flow_diff": None,
                "active_flow_source": None,
            })
        time.sleep(0.3)
    return out


def index_snapshot():''',
)


# 2) A-flow scoring accepts only explicit verified active-trade sources.
sub(
    "scoring.py",
    r"_prev_vol = \{\}.*?\n\ndef push_flow_ratio",
    '''VERIFIED_AFLOW_SOURCES = {"shioaji_ticks", "canonical_active_flow"}
_aflow = {}           # code -> verified active buy - active sell
_ratio_hist = {}      # code -> recent verified aflow_ratio


def update_aflow(code, total_volume, tick_type=None,
                 buy_volume=None, sell_volume=None, source=None):
    """Update canonical A-flow only from an explicitly verified source.

    Missing data and snapshot quote queues are not zero flow. They are
    unavailable and cannot satisfy an entry/BS gate.
    """
    if source not in VERIFIED_AFLOW_SOURCES or buy_volume is None or sell_volume is None:
        _aflow.pop(code, None)
        return None
    try:
        value = int(buy_volume or 0) - int(sell_volume or 0)
    except (TypeError, ValueError):
        _aflow.pop(code, None)
        return None
    _aflow[code] = value
    return value


def push_flow_ratio''',
)

sub(
    "scoring.py",
    r"def reset_aflow\(\):.*?\n\ndef get_aflow\(code\):\n    return _aflow\.get\(code, 0\)",
    '''def reset_aflow():
    """Clear verified A-flow state before each trading day."""
    _aflow.clear()
    _ratio_hist.clear()


def get_aflow(code):
    return _aflow.get(code)''',
)

sub(
    "scoring.py",
    r"def bs_recent\(code, buy_vol, sell_vol\):.*?\n\ndef reset_bs",
    '''def bs_recent(code, buy_vol, sell_vol):
    """Recent active buy/sell ratio. Missing data stays unavailable."""
    if buy_vol is None or sell_vol is None:
        _bs_prev.pop(code, None)
        return None
    pb, ps = _bs_prev.get(code, (buy_vol, sell_vol))
    _bs_prev[code] = (buy_vol or 0, sell_vol or 0)
    db = max(0, (buy_vol or 0) - pb)
    ds = max(0, (sell_vol or 0) - ps)
    tot = db + ds
    if tot <= 0:
        return None
    return round(db / tot * 100, 1)


def reset_bs''',
)

p = BASE / "scoring.py"
text = p.read_text(encoding="utf-8")
old = '''    if not total_volume:
        return None, ""
    ratio = aflow / total_volume'''
new = '''    if aflow is None or not total_volume:
        return None, ""
    ratio = aflow / total_volume'''
if old not in text:
    raise RuntimeError("scoring.py: divergence guard not found")
p.write_text(text.replace(old, new, 1), encoding="utf-8")


# 3) Stock card uses only explicit verified active-trade fields.
p = BASE / "stock_card.py"
text = p.read_text(encoding="utf-8")
text = text.replace(
    "主動買/賣% = 快照 buy_volume/sell_volume(外/內盤累積);",
    "主動買/賣% = 僅限已驗證 active_buy_volume/active_sell_volume;",
)
old = '''    bv = (snap or {}).get("buy_volume") or 0
    sv = (snap or {}).get("sell_volume") or 0
    tot = bv + sv
    flow_block = {
        "active_buy_pct": round(bv / tot * 100, 1) if tot else None,
        "active_sell_pct": round(sv / tot * 100, 1) if tot else None,
        "flow_5d": _flow_days(bars, 5),
        "flow_10d": _flow_days(bars, 10),
    }'''
new = '''    flow_source = (snap or {}).get("active_flow_source")
    verified_flow = flow_source in {"shioaji_ticks", "canonical_active_flow"}
    bv = (snap or {}).get("active_buy_volume") if verified_flow else None
    sv = (snap or {}).get("active_sell_volume") if verified_flow else None
    tot = ((bv or 0) + (sv or 0)) if bv is not None and sv is not None else 0
    flow_block = {
        "active_buy_pct": round(bv / tot * 100, 1) if tot else None,
        "active_sell_pct": round(sv / tot * 100, 1) if tot else None,
        "flow_5d": _flow_days(bars, 5),
        "flow_10d": _flow_days(bars, 10),
        "source": flow_source if verified_flow else None,
    }'''
if old not in text:
    raise RuntimeError("stock_card.py: active flow block not found")
p.write_text(text.replace(old, new, 1), encoding="utf-8")


# 4) Engine never passes quote queues into A-flow/BS entry gates.
p = BASE / "engine.py"
text = p.read_text(encoding="utf-8")
old = '''    aflow = scoring.update_aflow(s["code"], s.get("total_volume"),
                                 s.get("tick_type"),
                                 buy_volume=s.get("buy_volume"),
                                 sell_volume=s.get("sell_volume"))'''
new = '''    aflow = scoring.update_aflow(
        s["code"], s.get("total_volume"), s.get("tick_type"),
        buy_volume=s.get("active_buy_volume"),
        sell_volume=s.get("active_sell_volume"),
        source=s.get("active_flow_source"))'''
if old not in text:
    raise RuntimeError("engine.py: A-flow call not found")
text = text.replace(old, new, 1)
old = '''    _bs_recent = scoring.bs_recent(s["code"], s.get("buy_volume"), s.get("sell_volume"))
    bs_pass, bs_detail = scoring.bs_filter(
        s.get("buy_volume"), s.get("sell_volume"), market_pct,
        intraday=True, recent_pct=_bs_recent)'''
new = '''    active_buy = s.get("active_buy_volume")
    active_sell = s.get("active_sell_volume")
    verified_flow = s.get("active_flow_source") in scoring.VERIFIED_AFLOW_SOURCES
    if not verified_flow:
        active_buy = active_sell = None
    _bs_recent = scoring.bs_recent(s["code"], active_buy, active_sell)
    bs_pass, bs_detail = scoring.bs_filter(
        active_buy, active_sell, market_pct,
        intraday=True, recent_pct=_bs_recent)'''
if old not in text:
    raise RuntimeError("engine.py: BS call not found")
p.write_text(text.replace(old, new, 1), encoding="utf-8")

print("P0 patch applied")
