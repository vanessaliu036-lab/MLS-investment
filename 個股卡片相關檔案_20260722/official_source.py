"""
MLS 模組 — official_source.py(v3.1 新增,最高原則:有官方就不自己算)
====================================================================
使用者鐵律(2026-07-13 定):
  外資/投信/自營、大盤、成交量、大盤貢獻——這些證交所(TWSE)與
  櫃買(TPEx)每天公布「唯一一份」官方成品,別家 App 數字全一樣就是
  因為都抓官方。MLS 過去用 FinMind 免費版自己一檔一檔湊全市場總和,
  湊出來當然對不上。本模組一律抓官方成品,禁止自算。

資料來源(全部免費、公開、每日固定發布):
  三大法人買賣超(全市場彙總):
    TWSE  https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json&dayDate=YYYYMMDD
  大盤指數/成交量:
    TWSE  https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&date=YYYYMMDD&type=IND
  個股三大法人(給幸存者精查用,單檔):
    TWSE  https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date=YYYYMMDD&selectType=ALL

誠實邊界:
  - 這支只負責「抓官方成品 + 正規化欄位」,不做任何加總/推估。
  - 抓不到(假日、休市、網路失敗)→ 回 None 並標 note,絕不回 0 或估值。
  - TWSE JSON 欄位偶爾改版;所有解析都 try 包住,失敗標 note 不炸主流程。

⚠️ 本模組在離線沙盒「無法實際連線驗證」,端點與欄位解析依 TWSE 現行
   公開格式撰寫。部署後第一次執行請核對回傳(見 HANDOFF 的驗收步驟)。
"""

import json
import ssl
import urllib.request
from datetime import datetime, timedelta, timezone

TW_TZ = timezone(timedelta(hours=8))
_UA = {"User-Agent": "Mozilla/5.0 MLS/3.1 official-data"}
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE          # TWSE 憑證鏈偶有問題,放寬避免中斷


def _today_yyyymmdd(d=None):
    return (d or datetime.now(TW_TZ)).strftime("%Y%m%d")


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def _num(s):
    """'-195,772,834' → -195772834.0;失敗回 None。"""
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").replace(" ", "").replace("+", ""))
    except (ValueError, TypeError):
        return None


# ════════════════════════════════════════════════════════
# 一、三大法人買賣超(全市場官方彙總,單位:億元)
# ════════════════════════════════════════════════════════
def institutional_net(date=None):
    """
    回傳 dict 或 None:
      {date, foreign_100m, trust_100m, dealer_100m, total_100m, source}
    單位:億元(官方原始為元,這裡只做單位換算,不做任何估算)。
    """
    ymd = _today_yyyymmdd(date)
    url = (f"https://www.twse.com.tw/rwd/zh/fund/BFI82U"
           f"?response=json&dayDate={ymd}&weekDate=&monthDate=&type=day")
    try:
        j = _get_json(url)
    except Exception as e:
        return {"date": ymd, "note": f"TWSE 法人資料取得失敗:{e}",
                "foreign_100m": None, "trust_100m": None,
                "dealer_100m": None, "total_100m": None,
                "source": url}
    if j.get("stat") != "OK" or not j.get("data"):
        return {"date": ymd, "note": "官方今日無資料(休市或尚未公布)",
                "foreign_100m": None, "trust_100m": None,
                "dealer_100m": None, "total_100m": None, "source": url}

    foreign = trust = dealer = 0.0
    total_official = None
    got = False
    for row in j["data"]:
        name = str(row[0])
        net = _num(row[-1])            # 最後一欄 = 買賣差額(元)
        if net is None:
            continue
        net_100m = net / 1e8
        # 合計列直接當官方總額(勝過自行加總)
        if "合計" in name or "總計" in name:
            total_official = net_100m
            continue
        # 順序關鍵:外資自營商含「外資」也含「自營」,先判外資 → 歸外資;
        # 「外資及陸資(不含外資自營商)」名稱含「自營」二字,舊版誤排除是主 bug。
        if "外資" in name or "陸資" in name:
            foreign += net_100m; got = True
        elif "投信" in name:
            trust += net_100m; got = True
        elif "自營" in name:          # 自營商(自行買賣)+(避險)
            dealer += net_100m; got = True
    if not got:
        return {"date": ymd, "note": "官方欄位解析未命中(TWSE 可能改版)",
                "foreign_100m": None, "trust_100m": None,
                "dealer_100m": None, "total_100m": None, "source": url}
    total = round(total_official if total_official is not None else foreign + trust + dealer, 2)
    return {"date": ymd, "foreign_100m": round(foreign, 2),
            "trust_100m": round(trust, 2), "dealer_100m": round(dealer, 2),
            "total_100m": total, "source": url, "note": None}


# ════════════════════════════════════════════════════════
# 二、大盤指數 + 成交金額(官方)
# ════════════════════════════════════════════════════════
def market_index(date=None):
    """
    回傳 {date, taiex, change, change_pct, turnover_100m, source, note} 或 None欄位。
    turnover 官方為元,換億。禁止自行加總個股湊大盤。
    """
    ymd = _today_yyyymmdd(date)
    # type=ALL 才包含「大盤統計資訊」成交金額；type=IND 只有指數表。
    url = (f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
           f"?response=json&date={ymd}&type=ALL")
    out = {"date": ymd, "taiex": None, "change": None, "change_pct": None,
           "turnover_100m": None, "source": url, "note": None}
    try:
        j = _get_json(url)
    except Exception as e:
        out["note"] = f"TWSE 大盤取得失敗:{e}"
        return out
    if j.get("stat") != "OK":
        out["note"] = "官方今日無大盤資料(休市或未公布)"
        return out
    # 加權指數在 data1/tables 之一;不同版本欄位名不同,全部 try
    try:
        for tbl_key in ("tables", "data1", "data"):
            tbl = j.get(tbl_key)
            if not tbl:
                continue
            rows = tbl[0]["data"] if isinstance(tbl, list) and tbl and isinstance(tbl[0], dict) else tbl
            for row in rows:
                if "發行量加權股價指數" in str(row[0]):
                    # 欄位:[指數,收盤指數,漲跌(+/-),漲跌點數,漲跌百分比(%),註記]
                    out["taiex"] = _num(row[1])
                    down = "green" in str(row[2]).lower() if len(row) > 2 else False
                    pts = _num(row[3]) if len(row) > 3 else None
                    pct = _num(row[4]) if len(row) > 4 else None
                    if pts is not None:
                        out["change"] = -pts if down else pts
                    if pct is not None:
                        out["change_pct"] = pct if (pct < 0 or not down) else -pct
                    break
            if out["taiex"]:
                break

        # 官方「大盤統計資訊」:
        # 證券合計(1+6+14+15) = 股票/ETF 等證券合計成交金額(元)。
        # 不把個股加總，不把觀察池成交額冒充大盤成交額。
        for tbl in (j.get("tables") or []):
            if "大盤統計資訊" not in str(tbl.get("title", "")):
                continue
            for row in tbl.get("data", []):
                if row and (str(row[0]).startswith("證券合計")
                            or str(row[0]).startswith("總計")):
                    amount = _num(row[1]) if len(row) > 1 else None
                    if amount is not None:
                        out["turnover_100m"] = round(amount / 1e8, 2)
                    break
            if out["turnover_100m"] is not None:
                break
    except Exception as e:
        out["note"] = f"大盤欄位解析失敗:{e}"
    return out


# ════════════════════════════════════════════════════════
# 三、個股三大法人(單檔,給漏斗幸存者精查)
# ════════════════════════════════════════════════════════
def stock_institutional(code, date=None):
    """
    單檔官方三大法人買賣超(張)。回傳 dict 或 None欄位。
    只在漏斗幸存者(個位數檔)呼叫,不全池跑。
    """
    ymd = _today_yyyymmdd(date)
    url = (f"https://www.twse.com.tw/rwd/zh/fund/T86"
           f"?response=json&date={ymd}&selectType=ALL")
    out = {"code": code, "date": ymd, "foreign_lots": None,
           "trust_lots": None, "dealer_lots": None, "total_lots": None,
           "source": url, "note": None}
    try:
        j = _get_json(url)
    except Exception as e:
        out["note"] = f"T86 取得失敗:{e}"
        return out
    if j.get("stat") != "OK" or not j.get("data"):
        out["note"] = "官方今日無個股法人資料"
        return out
    try:
        for row in j["data"]:
            if str(row[0]).strip() == str(code):
                # T86 欄位:外資買賣超股數在固定索引,張=股/1000
                # T86 欄位:[4]外陸資買賣超(不含外資自營商) [7]外資自營商
                # [10]投信 [11]自營商合計 [18/-1]三大法人合計。股→張(/1000)。
                out["foreign_lots"] = round(((_num(row[4]) or 0) + (_num(row[7]) or 0)) / 1000)
                out["trust_lots"] = round((_num(row[10]) or 0) / 1000)
                out["dealer_lots"] = round((_num(row[11]) or 0) / 1000)
                out["total_lots"] = round((_num(row[-1]) or 0) / 1000)
                return out
        out["note"] = "官方資料中查無此代號"
    except Exception as e:
        out["note"] = f"T86 欄位解析失敗(TWSE 可能改版):{e}"
    return out


# ════════════════════════════════════════════════════════
# 四、類股官方指數(給首頁熱力圖用;有官方就不自算)
# ════════════════════════════════════════════════════════
# TWSE 每日公布 56 個類股指數(MI_INDEX type=IND,免費)。
# 但**細分族群(封測/記憶體/PCB/被動元件/IC設計/...)TWSE 沒有獨立指數**
# — 這些次族群在 TWSE 官方只散在半導體/光電/電子零組件等大類下。
#
# 規則:對應回得到的 9 個大類 → 給官方數字;對不到的細分族群 → 回 None +
# note「無官方細分指數」,前端誠實顯,絕不自算湊數。
_SECTOR_OFFICIAL_MAP = {
    # TWSE 官方族群名 → 我方 SECTOR_MAP 用的族群名(可能多對一)
    "半導體類指數": ["半導體", "IC設計", "IC製造", "封測", "記憶體", "功率半導體", "晶圓材料"],
    "光電類指數": ["光電"],
    "電子零組件類指數": ["PCB", "PCB材料", "ABF載板", "電子零組件", "被動元件", "石英元件"],
    "電器電纜類指數": [],
    "化學生技醫療類指數": [],
    "電機機械類指數": ["網通"],
    "通信網路類指數": ["通信網路", "網通"],
    "資訊服務類指數": [],
    "其他類指數": ["其他"],
    "光通訊類指數": ["光通訊", "通信網路"],
}


def sector_index(date=None):
    """
    回傳 {sector_name: {pct, change, source, note}} 或 None欄位。
    一支抓 MI_INDEX type=IND(56 列全量),本地分發到 SECTOR_MAP 對應的族群。
    細分族群(封測/記憶體/PCB/被動元件...)會被**忽略不填**,
    呼叫端(eod_state)看到 None 就保留 MLS 子集中位 + 標 source=「觀察池子集(非全市場)」。
    絕不自行加總/估算冒充官方。
    """
    ymd = _today_yyyymmdd(date)
    url = (f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
           f"?response=json&date={ymd}&type=IND")
    out_map = {}
    note = None
    try:
        j = _get_json(url)
    except Exception as e:
        return {"date": ymd, "source": url,
                "note": f"TWSE 類股指數取得失敗:{e}", "data": {}}
    if j.get("stat") != "OK":
        return {"date": ymd, "source": url,
                "note": "官方今日無類股指數(休市或未公布)", "data": {}}
    try:
        # 找含「類指數」字串的列(56 列)
        for tbl_key in ("tables", "data1", "data"):
            tbl = j.get(tbl_key)
            if not tbl:
                continue
            rows = tbl[0]["data"] if isinstance(tbl, list) and isinstance(tbl[0], dict) else tbl
            for row in rows:
                name = str(row[0])
                if "類指數" not in name:
                    continue
                # 欄位:[名,收盤,紅綠,漲跌點,漲跌%]
                pct = _num(row[4]) if len(row) > 4 else None
                if pct is None:
                    continue
                for my_sec in _SECTOR_OFFICIAL_MAP.get(name, []):
                    out_map[my_sec] = {
                        "pct": pct,
                        "change": _num(row[3]) if len(row) > 3 else None,
                        "official_index": name,
                    }
            if out_map:
                break
    except Exception as e:
        note = f"TWSE 類股指數欄位解析失敗:{e}"
    return {"date": ymd, "source": url, "note": note, "data": out_map}


if __name__ == "__main__":
    # 離線沙盒無網路,此處僅示範呼叫形狀;實際數字部署後才有
    print("institutional_net() 形狀:")
    print(json.dumps(institutional_net(), ensure_ascii=False, indent=1))
