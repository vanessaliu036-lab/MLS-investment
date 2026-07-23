"""
MLS v4.0 — config.py
全域設定。所有可調參數集中於此，環境變數優先。
未設任何金鑰時，系統以 DATA_MODE=demo 啟動（假資料），確保可先跑起來。
"""
import os

# ── 資料模式 ──────────────────────────────────────────
# real: 接 Shioaji/FinMind 真實 API
# demo: 用內建示意資料（未設金鑰時自動降級，確保系統可啟動不崩）
DATA_MODE = os.environ.get("MLS_DATA_MODE", "demo")

# ── Shioaji（永豐）──
SHIOAJI_API_KEY = os.environ.get("SHIOAJI_API_KEY", "")
SHIOAJI_SECRET_KEY = os.environ.get("SHIOAJI_SECRET_KEY", "")
SHIOAJI_CA_PATH = os.environ.get("SHIOAJI_CA_PATH", "")
SHIOAJI_CA_PASSWD = os.environ.get("SHIOAJI_CA_PASSWD", "")
SHIOAJI_PERSON_ID = os.environ.get("SHIOAJI_PERSON_ID", "")

# ── FinMind ──
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

# ── DB ──
DB_PATH = os.environ.get("MLS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "data", "mls.db"))

# ── 門檻常數（對齊 decision_v22 實作）──
READY_MIN = int(os.environ.get("MLS_READY_MIN", "65"))
WATCH_MIN = int(os.environ.get("MLS_WATCH_MIN", "50"))
ENGINE_READY_MIN = int(os.environ.get("MLS_ENGINE_READY_MIN", "60"))
SUCCESS_PCT = float(os.environ.get("MLS_SUCCESS_PCT", "2.0"))   # 攻擊軌隔日達標%
STATS_DAYS = int(os.environ.get("MLS_STATS_DAYS", "30"))        # 滾動統計視窗
WATCHLIST_MAX = int(os.environ.get("MLS_WATCHLIST_MAX", "12"))  # 觀察清單上限
ABAB_MIN_ABS = 0.5

# ── 承接品質門檻 ──
ABSORPTION_GATE_MIN = int(os.environ.get("MLS_ABSORPTION_MIN", "3"))  # 漏斗第四關：★≥此值
L1_HEALTH_MIN = int(os.environ.get("MLS_L1_HEALTH_MIN", "75"))
MARGIN_SURGE_TH = int(os.environ.get("MLS_MARGIN_SURGE_TH", "500"))
WASH_BONUS = int(os.environ.get("MLS_WASH_BONUS", "5"))

# ── 觀察池（10 大族群成分股，可依需要增減）──
# 格式: code -> (name, sector, sector_type)  sector_type: engine 主引擎 / attack 攻擊部隊
UNIVERSE = {
    "1815": ("富喬", "PCB材料", "attack"),
    "2049": ("上銀", "無人機", "attack"),
    "2303": ("聯電", "功率半導體", "engine"),
    "2327": ("國巨", "被動元件", "attack"),
    "2337": ("旺宏", "記憶體", "attack"),
    "2344": ("華邦電", "記憶體", "attack"),
    "2359": ("所羅門", "無人機", "attack"),
    "2383": ("台光電", "PCB材料", "attack"),
    "2408": ("南亞科", "記憶體", "attack"),
    "2455": ("全新", "光通訊", "attack"),
    "2464": ("盟立", "無人機", "attack"),
    "2481": ("強茂", "功率半導體", "attack"),
    "2484": ("希華", "石英元件", "attack"),
    "2492": ("華新科", "被動元件", "attack"),
    "3006": ("晶豪科", "記憶體", "attack"),
    "3016": ("嘉晶", "晶圓材料", "attack"),
    "3026": ("禾伸堂", "被動元件", "attack"),
    "3037": ("欣興", "ABF載板", "attack"),
    "3042": ("晶技", "石英元件", "attack"),
    "3189": ("景碩", "ABF載板", "attack"),
    "3221": ("台嘉碩", "石英元件", "attack"),
    "3264": ("欣銓", "封測", "attack"),
    "3317": ("尼克森", "功率半導體", "attack"),
    "3363": ("上詮", "光通訊", "attack"),
    "3374": ("精材", "封測", "attack"),
    "3450": ("聯鈞", "光通訊", "attack"),
    "3532": ("台勝科", "晶圓材料", "attack"),
    "3707": ("漢磊", "晶圓材料", "attack"),
    "3711": ("日月光投控", "封測", "attack"),
    "4919": ("新唐", "無人機", "attack"),
    "4979": ("華星光", "光通訊", "attack"),
    "5328": ("華容", "被動元件", "attack"),
    "5347": ("世界", "功率半導體", "engine"),
    "5425": ("台半", "功率半導體", "attack"),
    "5483": ("中美晶", "晶圓材料", "attack"),
    "6173": ("信昌電", "被動元件", "attack"),
    "6174": ("安碁", "石英元件", "attack"),
    "6182": ("合晶", "晶圓材料", "attack"),
    "6213": ("聯茂", "PCB材料", "attack"),
    "6223": ("旺矽", "晶圓材料", "attack"),
    "6239": ("力成", "封測", "attack"),
    "6274": ("台燿", "PCB材料", "attack"),
    "6451": ("訊芯-KY", "光通訊", "attack"),
    "6488": ("環球晶", "晶圓材料", "attack"),
    "8028": ("昇陽半導體", "記憶體", "attack"),
    "8043": ("蜜望實", "被動元件", "attack"),
    "8046": ("南電", "ABF載板", "attack"),
    "8150": ("南茂", "封測", "attack"),
    "8182": ("加高", "石英元件", "attack"),
    "8261": ("富鼎", "功率半導體", "attack"),
    "8358": ("金居", "PCB材料", "attack"),
}

NAME_MAP = {code: v[0] for code, v in UNIVERSE.items()}
SECTOR_MAP = {code: v[1] for code, v in UNIVERSE.items()}
TYPE_MAP = {code: v[2] for code, v in UNIVERSE.items()}
ENGINE_STOCKS = {code for code, v in UNIVERSE.items() if v[2] == "engine"}

# 族群 → 類型
SECTORS = {}
for code, (name, sec, typ) in UNIVERSE.items():
    SECTORS.setdefault(sec, typ)

# ── Telegram（選用）──
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── 報告輸出目錄 ──
REPORT_DIR = os.environ.get("MLS_REPORT_DIR", os.path.join(
    os.path.dirname(__file__), "data", "reports"))
