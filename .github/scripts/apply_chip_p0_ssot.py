from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
MOD = ROOT / "個股卡片相關檔案_20260722"


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{path}: expected block not found")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def regex_once(path: Path, pattern: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    out, n = re.subn(pattern, new, text, count=1, flags=re.S)
    if n != 1:
        raise RuntimeError(f"{path}: expected one regex replacement, got {n}")
    path.write_text(out, encoding="utf-8")


# ---------------------------------------------------------------------------
# P0-2: canonical institutional schema.  A 三大法人 total always includes
# foreign + trust + dealer.  inst_streak is explicitly foreign streak.
# ---------------------------------------------------------------------------
chips = MOD / "chips.py"
regex_once(
    chips,
    r"def summarize_finmind_institutional\(rows, inst_days=INST_DAYS\):.*?\n\ndef _official_detail",
    '''def summarize_finmind_institutional(rows, inst_days=INST_DAYS):
    """Normalize FinMind institutional rows to canonical lots.

    Canonical definitions:
      foreign = Foreign_Investor
      trust   = Investment_Trust
      dealer  = Dealer_self + Dealer_Hedging + other Dealer* rows
      institution = foreign + trust + dealer

    All net values are lots (張). ``inst_streak`` is kept for compatibility
    but its semantic is FOREIGN streak; callers must label it 外資連買/連賣.
    """
    by_date = {}
    for row in rows or []:
        date = row.get("date")
        name = row.get("name") or ""
        if not date:
            continue
        try:
            buy = float(row.get("buy") or 0)
            sell = float(row.get("sell") or 0)
        except (TypeError, ValueError):
            continue
        net = (buy - sell) / 1000.0
        item = by_date.setdefault(date, {
            "foreign": 0.0, "trust": 0.0, "dealer": 0.0,
            "dealer_self": 0.0, "dealer_hedge": 0.0,
        })
        if name == "Foreign_Investor":
            item["foreign"] += net
        elif name == "Investment_Trust":
            item["trust"] += net
        elif name == "Dealer_self":
            item["dealer"] += net
            item["dealer_self"] += net
        elif name == "Dealer_Hedging":
            item["dealer"] += net
            item["dealer_hedge"] += net
        elif name.startswith("Dealer") or name == "Foreign_Dealer_Self":
            item["dealer"] += net

    dates = sorted(by_date)[-int(inst_days):]
    if not dates:
        return {}

    def institutional(date):
        x = by_date[date]
        return x["foreign"] + x["trust"] + x["dealer"]

    def sum_field(field, count):
        return round(sum(by_date[d][field] for d in dates[-count:]))

    def sum_inst(count):
        return round(sum(institutional(d) for d in dates[-count:]))

    streak = 0
    for date in reversed(dates):
        value = by_date[date]["foreign"]
        if value > 0:
            if streak < 0:
                break
            streak += 1
        elif value < 0:
            if streak > 0:
                break
            streak -= 1
        else:
            break

    latest = by_date[dates[-1]]
    return {
        "inst_net_20d_lots": sum_inst(inst_days),
        "inst_net_5d_lots": sum_inst(5),
        "inst_net_3d_lots": sum_inst(3),
        "inst_streak": streak,
        "foreign_days": streak,
        "foreign_net_d": round(latest["foreign"]),
        "trust_net_d": round(latest["trust"]),
        "dealer_net_d": round(latest["dealer"]),
        "dealer_self_d": round(latest["dealer_self"]),
        "dealer_hedge_d": round(latest["dealer_hedge"]),
        "foreign_net_3d": sum_field("foreign", 3),
        "trust_net_3d": sum_field("trust", 3),
        "dealer_net_3d": sum_field("dealer", 3),
        "foreign_net_5d": sum_field("foreign", 5),
        "trust_net_5d": sum_field("trust", 5),
        "dealer_net_5d": sum_field("dealer", 5),
        "foreign_net_20d": sum_field("foreign", inst_days),
        "trust_net_20d": sum_field("trust", inst_days),
        "dealer_net_20d": sum_field("dealer", inst_days),
        "source": "FinMind TaiwanStockInstitutionalInvestorsBuySell",
        "source_date": dates[-1],
        "days_used": len(dates),
        "unit": "lots",
        "schema_version": "chip_ssot_v1",
    }


def _official_detail''',
)

# Never relabel institutional lots as holder percentage.  True holder data
# comes from its own source/date; no fabricated proxy is allowed into scoring.
regex_once(
    chips,
    r"    # ── 大戶比例\(股權分散,週資料\).*?\n    if _cache.get\(\"date\"\) != today:",
    '''    # ── 大戶持股：禁止用法人張數冒充百分比 ────────────────
    # get_chips() is the lightweight scoring path and does not fetch TDCC here.
    # Keep the field unavailable rather than pollute the schema with a lot-count
    # proxy.  UI detail uses its dedicated source/date path when available.
    result["big_holder_pct"] = None
    result["big_holder_trend"] = None

    if _cache.get("date") != today:''',
)

# Fix legacy doc label in get_chips.
text = chips.read_text(encoding="utf-8")
text = text.replace("法人(外資+投信)近20日合計買賣超", "三大法人(外資+投信+自營)近20日合計買賣超")
chips.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# P0-4 + latest-source-date contract on stock card.
# ---------------------------------------------------------------------------
stock_card = MOD / "stock_card.py"
replace_once(
    stock_card,
    '        "source_date": cd.get("source_date"),\n        "sources": cd.get("sources"),',
    '        "source_date": cd.get("source_date"),\n'
    '        "chip_data_date": cd.get("source_date"),\n'
    '        "chip_source_table": "chips_cache.json",\n'
    '        "chip_source_version": cd.get("schema_version") or "chip_ssot_v1",\n'
    '        "sources": cd.get("sources"),',
)


# ---------------------------------------------------------------------------
# P0-3: /api/watchpool must reuse the canonical intraday endpoint instead of
# independently sampling broker.raw_buffer_snapshots at a different instant.
# ---------------------------------------------------------------------------
extras = MOD / "extras.py"
replace_once(
    extras,
    '''def _raw_rows() -> List[Dict[str, Any]]:
    """從 broker buffer 拿真實 snap、餵給 vps_intraday_test._row 計算 group/aflow。
    跟 /api/intraday-test endpoint 共用同一邏輯。"""
    try:
        raw = broker.raw_buffer_snapshots()
        return [VIT._row(item) for item in raw]
    except Exception:
        return []''',
    '''def _raw_rows() -> List[Dict[str, Any]]:
    """Read the canonical intraday snapshot used by every MLS screen.

    No screen is allowed to resample broker independently: that was the source
    of same-symbol / same-time A-flow contradictions.
    """
    try:
        payload = VIT.intraday_test() or {}
        return list(payload.get("rows") or [])
    except Exception as exc:
        print(f"[extras] canonical intraday snapshot 讀取失敗: {exc}", flush=True)
        return []''',
)

# The 3532 bug: foreign_net_20d and foreign streak were labelled as generic
# institutional facts.  Make the semantics explicit.
text = extras.read_text(encoding="utf-8")
text = text.replace("parts.append(f\"法人近月{'買超' if f20 > 0 else '賣超'} {abs(int(f20)):,} 張\")",
                    "parts.append(f\"外資20日{'買超' if f20 > 0 else '賣超'} {abs(int(f20)):,} 張\")")
text = text.replace("parts.append(f\"法人連{'買' if streak >= 0 else '賣'} {abs(int(streak))} 日\")",
                    "parts.append(f\"外資連{'買' if streak >= 0 else '賣'} {abs(int(streak))} 日\")")
extras.write_text(text, encoding="utf-8")

# NEXORA decision output explicitly exposes the net_active data gate.
replace_once(
    extras,
    '''    return {"factors": factors, "score": score, "score_raw": raw_score, "score_max": 100,
            "score_available": available,
            "missing": missing, "confidence": confidence,
            "rule": "只以已取得因子正規化計分；缺資料降低可信度，不當作 0 分。"}''',
    '''    net_active_ok = active is not None
    return {"factors": factors, "score": score, "score_raw": raw_score, "score_max": 100,
            "score_available": available,
            "missing": missing, "confidence": confidence,
            "data_status": "OK" if net_active_ok else "DATA_INCOMPLETE",
            "action_gate": "ALLOW" if net_active_ok else "WATCH",
            "gate_reason": None if net_active_ok else "缺 net_active，禁止正式進場",
            "rule": "只以已取得因子正規化計分；net_active 缺失時正式 Action 強制 WATCH。"}''',
)

# Watchpool exposes a named chip date, rather than a vague foreign date.
replace_once(
    extras,
    '            "foreign_source_date": chip.get("source_date"),\n            "inst_streak": chip.get("inst_streak"),',
    '            "foreign_source_date": chip.get("source_date"),\n'
    '            "chip_data_date": chip.get("source_date"),\n'
    '            "chip_source_table": "chips_cache.json",\n'
    '            "chip_source_version": chip.get("schema_version") or "chip_ssot_v1",\n'
    '            "inst_streak": chip.get("inst_streak"),',
)


# ---------------------------------------------------------------------------
# P0-1 + P0-3 + P0-5: one A-flow semantic, one snapshot identity, fail closed
# when the Shioaji active-side flow is unavailable.
# ---------------------------------------------------------------------------
vit = ROOT / "vps_intraday_test.py"
replace_once(
    vit,
    '''    price = float(raw.get("price") or 0)
    change = float(raw.get("change_rate") or 0)
    aflow = int(raw.get("buy_volume") or 0) - int(raw.get("sell_volume") or 0)
    volume = int(raw.get("total_volume") or 0)''',
    '''    price = float(raw.get("price") or 0)
    change = float(raw.get("change_rate") or 0)
    aflow_unavailable = (bool(raw.get("_aflow_unavailable")) or
                         raw.get("buy_volume") is None or
                         raw.get("sell_volume") is None)
    aflow = None if aflow_unavailable else (
        int(raw.get("buy_volume") or 0) - int(raw.get("sell_volume") or 0))
    volume = int(raw.get("total_volume") or 0)''',
)
replace_once(vit, '    ratio = (aflow / volume) if volume > 0 else None',
             '    ratio = (aflow / volume) if aflow is not None and volume > 0 else None')

# Guard all directional comparisons in the seven-factor classifier.
text = vit.read_text(encoding="utf-8")
text = text.replace("fake_red = change > 0 and aflow < 0", "fake_red = aflow is not None and change > 0 and aflow < 0")
text = text.replace("resting = change <= 0 and aflow < 0", "resting = aflow is not None and change <= 0 and aflow < 0")
text = text.replace("elif (aflow > 0 and change <= 0) or (fake_red and change >= 0):",
                    "elif (aflow is not None and aflow > 0 and change <= 0) or (fake_red and change >= 0):")
text = text.replace("if aflow > 0:\n            facts.append", "if aflow is not None and aflow > 0:\n            facts.append")
# The compatibility field inst_streak is foreign streak; fix every user-facing label.
text = text.replace("法人連買", "外資連買")
text = text.replace("法人連賣", "外資連賣")
text = text.replace("法人今日買超", "外資今日買超")
text = text.replace("法人今日賣超", "外資今日賣超")
text = text.replace("法人中性", "外資中性")
vit.write_text(text, encoding="utf-8")

# _row must propagate the unavailable state instead of silently turning it into 0.
replace_once(vit, '        aflow=aflow,\n        total_volume=int(raw.get("total_volume") or 0),',
                  '        aflow=aflow_out,\n        total_volume=int(raw.get("total_volume") or 0),')
replace_once(vit,
             '        "aflow": aflow_out,\n        "quadrant": F.proxy_quadrant(aflow_out if aflow_out is not None else 0, change),',
             '        "aflow": aflow_out,\n'
             '        "aflow_source": "Shioaji TickSTKv1 bid_side_total_vol - ask_side_total_vol",\n'
             '        "aflow_unit": "張",\n'
             '        "quadrant": F.proxy_quadrant(aflow_out, change) if aflow_out is not None else None,')

# Give every row in the canonical response the exact same snapshot identity.
replace_once(
    vit,
    '''        rows = [_row(item) for item in raw]
        _pa_date, _pa_n = _attach_pre_activation(rows)''',
    '''        rows = [_row(item) for item in raw]
        snapshot_time = datetime.now(TW_TZ).isoformat(timespec="seconds")
        snapshot_id = f"{_trade_date()}:{snapshot_time}:aflow_tick_side_v1"
        for row in rows:
            row["data_date"] = _trade_date()
            row["snapshot_time"] = snapshot_time
            row["snapshot_id"] = snapshot_id
            row["source_table"] = "broker._QUOTE_BUF"
            row["source_version"] = "aflow_tick_side_v1"
        _pa_date, _pa_n = _attach_pre_activation(rows)''',
)
replace_once(
    vit,
    '''            "updated_at": datetime.now(TW_TZ).isoformat(timespec="seconds"),
            "trade_date": _trade_date(),
            "count": len(rows),''',
    '''            "updated_at": snapshot_time,
            "trade_date": _trade_date(),
            "data_date": _trade_date(),
            "snapshot_time": snapshot_time,
            "snapshot_id": snapshot_id,
            "source_table": "broker._QUOTE_BUF",
            "source_version": "aflow_tick_side_v1",
            "count": len(rows),''',
)


# ---------------------------------------------------------------------------
# P0-4/P0-5 in NEXORA: correct labels and hard-cap Ready when net_active is
# missing/degraded.  Scores may still be shown for diagnostics, but Action may
# not become formal entry.
# ---------------------------------------------------------------------------
mh = MOD / "money_health_api.py"
replace_once(
    mh,
    '''    note = f"法人近月{(net or 0):+,}張"
    if streak:
        note += f",連{'買' if streak > 0 else '賣'}{abs(streak)}日"''',
    '''    note = f"三大法人20日合計{(net or 0):+,}張"
    if streak:
        note += f",外資連{'買' if streak > 0 else '賣'}{abs(streak)}日"''',
)
replace_once(
    mh,
    '''    data_incomplete = int(any(v != "ok" for v in data_quality.values()))

    return {''',
    '''    data_incomplete = int(any(v != "ok" for v in data_quality.values()))
    net_active_missing = int(data_quality.get("capital") != "ok")

    return {''',
)
replace_once(
    mh,
    '        "data_incomplete": data_incomplete,\n    }',
    '        "data_incomplete": data_incomplete,\n        "net_active_missing": net_active_missing,\n    }',
)
replace_once(
    mh,
    '''def hard_hits(risk: Dict) -> List[str]:
    names = {"ma_break": "跌破 MA20", "divergence": "量價背離", "proxy": "資金為代理"}''',
    '''def hard_hits(risk: Dict) -> List[str]:
    names = {"ma_break": "跌破 MA20", "divergence": "量價背離", "proxy": "資金為代理",
             "net_active_missing": "缺 net_active"}''',
)
replace_once(
    mh,
    '''def grade_and_reason(health: int, quad: str, chip_ok: Optional[int],
                     risk: Dict, track: str, above_ma20: bool = False) -> tuple:
    hard = hard_hits(risk)''',
    '''def grade_and_reason(health: int, quad: str, chip_ok: Optional[int],
                     risk: Dict, track: str, above_ma20: bool = False) -> tuple:
    hard = hard_hits(risk)
    if risk.get("net_active_missing"):
        return "Watch", True, "DATA_INCOMPLETE｜缺 net_active，禁止正式進場", hard''',
)
replace_once(
    mh,
    '        "grade_reason": reason,\n        "_capped": capped,',
    '        "grade_reason": reason,\n'
    '        "data_status": "DATA_INCOMPLETE" if risk.get("net_active_missing") else "OK",\n'
    '        "_capped": capped,',
)


# ---------------------------------------------------------------------------
# P0/P1 semantic safety in popup: lots stay lots.  A historical money flow may
# only be added later from per-day net lots × that day\'s price, never today\'s
# price multiplied by a 5D/20D accumulated quantity.
# ---------------------------------------------------------------------------
popup = ROOT / "個股籌碼彈窗UI.html"
text = popup.read_text(encoding="utf-8")
text = text.replace("const money=(v,price)=>v==null||price==null?'—':`${Number(v)>0?'+':''}${(Number(v)*1000*Number(price)/1e8).toFixed(2)} 億`;",
                    "const lots=v=>v==null?'—':`${Number(v)>0?'+':''}${Number(v).toLocaleString('en-US')} 張`; ")
text = text.replace("法人當日買賣超 <span", "三大法人當日買賣超（張） <span")
text = text.replace("近 5 日買賣超　›", "三大法人近 5 日（張）　›")
text = text.replace("近 20 日累計　›", "三大法人近 20 日（張）　›")
text = text.replace("${money(daily,price)}", "${lots(daily)}")
text = text.replace("${money(five,price)}", "${lots(five)}")
text = text.replace("${money(twenty,price)}", "${lots(twenty)}")
popup.write_text(text, encoding="utf-8")

print("Applied MLS chip/A-flow P0 SSOT patch")
