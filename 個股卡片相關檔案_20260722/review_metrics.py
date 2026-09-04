# -*- coding: utf-8 -*-
"""後驗驗證的唯一指標語意層。

所有 API、盤後流程與報表都應透過這裡判定命中，避免不同版本的 verdict
被不同端點各自解讀。這支模組只含純函式，不讀寫資料庫。
"""

METRIC_V41_A = "v4.1_A"
METRIC_V41_AB = "v4.1_AB"
METRIC_LEGACY = "legacy_0.3pct"

V41_A_HITS = frozenset(("A_突破成功", "突破延續", "抗跌成立", "相容命中"))
V41_AB_HITS = frozenset(("A_突破成功", "突破延續", "B_續強", "抗跌成立", "相容命中"))
LEGACY_HITS = frozenset(("兌現", "命中", "相容命中"))
REVERSE_VERDICTS = frozenset(("反向", "突破失敗", "抗跌失敗"))
PENDING_VERDICTS = frozenset((None, "", "待驗證", "待資料"))


def is_hit(verdict, metric=METRIC_V41_A, source=None):
    """依指定 metric 判定單筆是否命中。

    v4.1 的 resilient 分流只有「抗跌成立」算命中；source 缺失時仍接受
    canonical verdict，讓歷史資料可相容讀取。"""
    if metric == METRIC_LEGACY:
        return verdict in LEGACY_HITS
    if metric == METRIC_V41_AB:
        return verdict in V41_AB_HITS
    if metric == METRIC_V41_A:
        return verdict in V41_A_HITS
    raise ValueError(f"unknown review metric: {metric}")


def is_reverse(verdict):
    return verdict in REVERSE_VERDICTS


def infer_metric(rows, stored=None):
    """讀取歷史列時保留已存 metric，否則依 verdict 推斷舊/新版。"""
    if stored in (METRIC_V41_A, METRIC_V41_AB, METRIC_LEGACY):
        return stored
    verdicts = {r.get("verdict") for r in (rows or []) if isinstance(r, dict)}
    if verdicts & (V41_A_HITS | V41_AB_HITS | {"C_未續強", "突破站穩", "未突破"}):
        return METRIC_V41_A
    return METRIC_LEGACY


def _has_close(row):
    return row.get("close_price") is not None and row.get("verdict") not in PENDING_VERDICTS


def summarize(rows, metric=METRIC_V41_A, expected_total=None):
    """彙總逐檔結果。

    `expected_total` 用於偵測 watchlist 有 10 檔但只蓋到部分 outcome 的情況。
    不完整或零分母一律不產生命中率，避免把資料問題寫成 0%。"""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    total = int(expected_total if expected_total is not None else len(rows))
    total = max(0, total)
    valid_rows = [r for r in rows if _has_close(r)]
    hit = sum(1 for r in valid_rows if is_hit(r.get("verdict"), metric, r.get("source")))
    if total == 0:
        status = "NO_WATCHLIST"
        rate = None
    elif len(valid_rows) < total:
        status = "DATA_INCOMPLETE"
        rate = None
    else:
        status = "VERIFIED"
        rate = round(hit / total * 100, 1)
    return {
        "metric": metric,
        "status": status,
        "total": total,
        "valid_total": len(valid_rows),
        "hit": hit,
        "hit_rate": rate,
    }
