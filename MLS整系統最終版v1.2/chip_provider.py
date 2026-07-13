"""
MLS 插件 — chip_provider.py
可插拔籌碼引擎介面(資金健康度優化 v2.2 一環)
====================================================================
現況誠實揭露(對應使用者診斷):
  chips.py 目前只有 FinMind 免費層兩種資料:
    - inst_net_20d_lots / inst_streak  法人買賣超 → 真的是「日」資料
    - big_holder_pct / big_holder_trend 千張大戶  → 真的是「週」資料
  完全沒有:持股級距分布、主力分點、券商分點集中度、大戶集中度變化。
  這些屬於付費籌碼商(如 FindBillion 等)的資料,FinMind 免費層不提供。

本模組不假造分點資料。它只做一件事:提供一個統一入口
get_chip_data(code) → (data: dict, quality: str),
quality 誠實標記資料到底是 'finmind_basic' 還是 'premium',
report / API 一律把這個字串一起吐出去,絕不讓「近月+19,878張」
這種摘要看起來像分點等級的籌碼分析。

────────────────────────────────────────────────────────────────
之後要接真籌碼商時,只要做兩件事,不必改 money_health.py：
  1. 新增 chip_provider_premium.py,實作:
       def get_rich_chips(code) -> dict | None
       回傳應包含(至少一部分即可,缺的欄位給 None):
         holder_tiers            持股級距分布 dict
         broker_concentration    券商分點集中度 (0~1)
         main_branch_net         主力分點買賣超(張)
         holder_concentration_delta  大戶集中度變化(pp)
         inst_net_20d_lots / inst_streak / big_holder_pct / big_holder_trend
           (可沿用 FinMind 欄位,不必重複實作)
  2. 環境變數設 CHIP_PROVIDER=premium。
  本模組會自動改走 premium,拿不到才降級回 FinMind,並標記 quality。
"""

import os
import chips as _chips

PROVIDER = os.environ.get("CHIP_PROVIDER", "finmind_basic")

RICH_FIELDS = ("holder_tiers", "broker_concentration",
               "main_branch_net", "holder_concentration_delta")


def get_chip_data(code):
    """
    統一入口。回傳 (data: dict, quality: str)。
    quality: 'premium'(含分點/集中度等真籌碼商資料) 或
             'finmind_basic'(僅 FinMind 日法人 + 週大戶,目前預設)。
    """
    if PROVIDER == "premium":
        try:
            import chip_provider_premium as _prem
            data = _prem.get_rich_chips(code)
            if data:
                # 補齊基礎欄位缺值時退回 FinMind,分點欄位保留 premium 原值
                base = _chips.get_chips(code) or {}
                for k, v in base.items():
                    data.setdefault(k, v)
                has_rich = any(data.get(k) is not None for k in RICH_FIELDS)
                return data, ("premium" if has_rich else "finmind_basic")
        except Exception as e:
            print(f"[chip_provider] premium 取得失敗,降級 FinMind:{e}")
    try:
        return (_chips.get_chips(code) or {}), "finmind_basic"
    except Exception as e:
        print(f"[chip_provider] chips 取得失敗:{e}")
        return {}, "finmind_basic"


def has_rich_data(data):
    return any((data or {}).get(k) is not None for k in RICH_FIELDS)
