"""
intraday_verdict.py — AI 盤中判讀(後台算結論,前台只印)

取代顯示端的「盤中篩選理由」。那一區的問題是**重複**:上半部已經寫了
「現價 173.5／+8.77%／資金 +4,450／突破昨高 169」,下面又把同一組欄位
串成句子再念一次,等於把欄位改寫成中文,沒有新增任何資訊。

這一區只回答原始數據沒有回答的三件事:
  1. 這些數據組合起來代表什麼      → lines(判讀)
  2. 為什麼它現在被放在這個分類    → lines(判讀,含歸類理由)
  3. 接下來什麼變化會升級/降級      → next_steps(條件式,帶價位)

**不重複原則(這支模組的存在意義,改壞就沒意義了)**:
  · lines 一律不出現現價、漲跌幅、資金流數字 —— 那些上半部都有了。
    要指涉時用「昨日高點」「關鍵價」「日內漲幅已大」這種語意詞。
  · 數字只准出現在 next_steps。那裡的價位是**條件**不是現況,是新資訊,
    使用者會照著它操作,所以一律給實數、不美化四捨五入。

鐵律(與 [說明語意層] 一致):只翻譯既有事實,不參與篩選、不改變去留。
缺資料就說沒有,不猜 —— 缺價或缺關鍵價位時回 pending,不寫「維持強勢」。
"""
from __future__ import annotations


def _f(x):
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def _n(x) -> str:
    """價位文字:整數不帶小數點,零頭保留 —— 169 而不是 169.0。"""
    v = _f(x)
    if v is None:
        return "—"
    return str(int(v)) if abs(v - int(v)) < 1e-9 else f"{v:g}"


# 日內漲幅超過這個數,重點就從「有沒有突破」換成「守不守得住」。
# 這是追價風險的分界,不是進出場訊號,也不影響任何篩選。
HOT_CHANGE_PCT = 5.0


def build(*, price=None, trigger=None, change_rate=None, aflow=None,
          intraday_high=None, track=None) -> dict:
    """把一檔的盤中事實翻成判讀 + 下一步。所有欄位皆可為 None。"""
    p, t = _f(price), _f(trigger)
    ch, fl, hi = _f(change_rate), _f(aflow), _f(intraday_high)
    is_engine = track in ("engine", "引擎軌")
    key_word = "關鍵價" if is_engine else "昨日高點"
    lines: list[dict] = []

    if p is None or t is None:
        if t is not None:
            return {"tone": "pending",
                    "lines": [{"tone": "pending",
                               "text": "尚未有盤中報價,今日先看開盤能否站上關鍵價,"
                                       "站上才進入啟動觀察。"}],
                    "next_steps": [
                        {"cond": f"開盤站上 {_n(t)}", "then": "進入啟動觀察"},
                        {"cond": f"開盤在 {_n(t)} 下方", "then": "續留觀察,不追"}]}
        return {"tone": "pending",
                "lines": [{"tone": "pending", "text": "盤中資料尚未就緒,無法判讀。"}],
                "next_steps": []}

    above = p >= t
    touched = hi is not None and hi >= t
    flow_in = fl is not None and fl > 0
    flow_out = fl is not None and fl < 0
    hot = ch is not None and ch >= HOT_CHANGE_PCT

    hold = {"cond": f"守住 {_n(t)}", "then": "維持強勢"}
    lose = {"cond": f"跌回 {_n(t)} 下方", "then": "突破失敗,降級觀察"}

    if above:
        if flow_out:
            # 價量背離:這是「為什麼被歸在這一類」最需要講清楚的一種。
            lines.append({"tone": "caution",
                          "text": f"價格站上{key_word},但主動資金同時流出 —— "
                                  "推升力道集中在少數買盤,有人趁高調節,"
                                  "屬價量背離而非乾淨啟動。"})
            lines.append({"tone": "caution",
                          "text": "在資金翻正之前,突破的可信度不足,不宜追價。"})
            return {"tone": "caution", "lines": lines,
                    "next_steps": [hold,
                                   {"cond": "資金流持續為負", "then": "視為假突破,降級觀察"}]}
        if flow_in:
            lines.append({"tone": "strong",
                          "text": f"價格與主動資金同步轉強,{key_word}已突破,"
                                  "目前屬於明確啟動型態。"})
        else:
            lines.append({"tone": "neutral",
                          "text": f"{key_word}已突破,結構成立;但主動資金尚未同步放大,"
                                  "動能還在確認階段。"})
        if hot:
            lines.append({"tone": "caution",
                          "text": "但日內漲幅已大,現在重點不是「是否突破」,"
                                  "而是突破後能否守住關鍵價,避免高位追價。"})
        return {"tone": "caution" if hot else ("strong" if flow_in else "neutral"),
                "lines": lines, "next_steps": [hold, lose]}

    if touched:
        lines.append({"tone": "caution",
                      "text": f"盤中曾觸及{key_word}但未能站穩,回落代表上方賣壓仍在,"
                              "突破尚未成立 —— 這是它留在觀察而非啟動的原因。"})
        return {"tone": "caution", "lines": lines,
                "next_steps": [{"cond": f"收盤站回 {_n(t)} 之上", "then": "突破重新成立"},
                               {"cond": f"持續在 {_n(t)} 下方", "then": "觸及失敗,降級觀察"}]}

    lines.append({"tone": "weak",
                  "text": f"尚未觸及{key_word};"
                          + ("主動資金同時流出,買盤未進場,結構與資金都不支持。"
                             if flow_out else
                             "主動資金已有流入,但價格尚未表態,屬醞釀未啟動。"
                             if flow_in else
                             "盤中量能未見放大,缺乏啟動跡象。")})
    return {"tone": "weak", "lines": lines,
            "next_steps": [{"cond": f"放量站上 {_n(t)}", "then": "升級為啟動觀察"},
                           {"cond": "續在關鍵價下方整理", "then": "維持觀察,不進場"}]}


def attach(items) -> int:
    """就地把判讀併進每一檔(唯讀衍生,不改任何既有欄位)。回傳成功筆數。"""
    n = 0
    for it in items:
        try:
            it["intraday_verdict"] = build(
                price=it.get("price") or it.get("close"),
                trigger=it.get("trigger_price") or it.get("entry_ref"),
                change_rate=it.get("change_rate"),
                aflow=it.get("aflow") or it.get("net_active"),
                intraday_high=it.get("intraday_high"),
                track=it.get("track"))
            n += 1
        except Exception:
            it["intraday_verdict"] = None
    return n
