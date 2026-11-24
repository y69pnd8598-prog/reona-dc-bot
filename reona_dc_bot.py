import os
import re
import json
import time
import random
import asyncio
import io
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import discord
from discord.ext import tasks

# ==================== 環境變數與核心設定 ====================
SYDNEY_TZ = ZoneInfo("Australia/Sydney")
MEMORY_FILE = "reona_memory.json"

# 一次性洗狀態開關：每次改動此字串，下次啟動會自動將舊狀態歸零一次
MEMORY_RESET_TOKEN = "reset_20261125_full_v4"

DISCORD_TOKEN = (
    os.getenv("DISCORD_BOT_TOKEN")
    or os.getenv("DISCORD_TOKEN")
    or ""
).strip()

TARGET_CHANNEL_ID = int(
    (os.getenv("TARGET_CHANNEL_ID") or os.getenv("DISCORD_CHANNEL_ID") or "0").strip() or "0"
)

RAW_GROQ_KEYS = (os.getenv("GROQ_API_KEY") or "").strip()
GROQ_KEYS = [k.strip() for k in re.split(r"[,;\s]+", RAW_GROQ_KEYS) if k.strip()]

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
GEMINI_MODELS: list[str] = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]

FISH_API_KEY = (os.getenv("FISH_API_KEY") or "").strip()
FISH_MODEL_ID = (os.getenv("FISH_MODEL_ID") or "").strip()
RENDER_BASE_URL = (os.getenv("RENDER_BASE_URL") or "").strip().rstrip("/")

# 保留最近 2 輪對話（4 句），減輕連續短句接龍時的注意力負擔
HISTORY_LIMIT = 4

# 連發訊息合併等待秒數（2.5 秒內連傳多則短訊會自動合併成一次 API 呼叫）
DEBOUNCE_SECONDS = 2.5

# 四級腸胃狀態（normal / mild / moderate / severe）與洗手間機率、冷卻防連刷（已完全取消自動升級）
RESTROOM_PROB_BY_LEVEL = {
    "normal": 0.10,
    "mild": 0.20,
    "moderate": 0.30,
    "severe": 0.36,
}

RESTROOM_COOLDOWN_BY_LEVEL = {
    "normal": 25 * 60,
    "mild": 18 * 60,
    "moderate": 12 * 60,
    "severe": 10 * 60,
}

RESTROOM_MIN_SEC = 55.0
RESTROOM_MAX_SEC = 130.0

# 在洗手間中途偷偷傳訊息的機率（0 額外 LLM Token）
RESTROOM_MID_PING_PROB_BY_LEVEL = {
    "normal": 0.30,
    "mild": 0.50,
    "moderate": 0.68,
    "severe": 0.80,
}

# 腸胃狀態持續秒數
GI_HOLD_SEC_BY_LEVEL = {
    "mild": 2 * 3600,
    "moderate": 5 * 3600,
    "severe": 9 * 3600,
}

# 各級腸胃不舒服的預設固定誘因
GI_DEFAULT_CAUSES = {
    "mild": [
        "趕稿忙到晚吃空腹太久敏感的胃有點悶",
        "吹到海風加上吃太急下腹悶咕嚕",
        "下午嘴饞吃甜點喝冰飲有點反酸胃悶",
    ],
    "moderate": [
        "晚餐不小心吃到偏油辣的食物敏感腸胃鬧肚子",
        "喝了冰飲又吹冷風引起陣發腹痛",
    ],
    "severe": [
        "晚餐吃到太油辣又著涼引發急性腸胃蠕動痙攣",
        "冰飲加甜食重口味刺激太重引起反覆腹瀉腿軟",
    ],
}

# 每日彈性晚餐候選池（同日鎖定，避免前後矛盾）
DINNER_POOL_NORMAL = [
    "清炒時蔬配煎鮭魚跟熱白飯",
    "熱呼呼的日式雞肉親子丼",
    "熱騰騰番茄牛肉湯麵",
    "溫烤雞胸配熱濃湯與烤吐司",
    "趕稿晚吃溫熱蔬菜蛋花粥",
    "微辣泰式打拋豬飯",
]

DINNER_POOL_GENTLE = [
    "清淡暖胃熱白粥配燙青菜",
    "熱湯蘇打餅配保溫瓶溫水",
]

# 每日外出地點候選池（近 10 天不重複，同日鎖死不亂跳）
OUTING_POOL = [
    "邦代海邊步道吹海風街拍",
    "岩石區週末市集挖寶",
    "達令港海邊喝手沖咖啡看海",
    "皇家植物園散步曬太陽",
    "薩里山巷弄甜點店探店",
    "雪梨市區底片相機行與選物店",
    "海邊咖啡廳畫速寫抓靈感",
    "新鎮復古書店與黑膠唱片行",
    "曼利海灘搭渡輪吹海風",
    "雪梨歌劇院旁港邊散步看夕陽",
    "帕丁頓週末手工藝與古董市集",
    "巴蘭加魯港邊公園散步發呆",
]

# 每日多元興趣與主軸候選池（近 8 天不重複，同日鎖定）
INTEREST_QUEST_POOL = [
    "商業插畫上主色調與光影",
    "整理底片相機街拍照片與色調",
    "去探店試吃新開的肉桂捲咖啡廳",
    "逛古著選物店與進口畫材行",
    "在家試做低糖燕麥小餅乾",
    "整理繪本分鏡草稿與調香氛蠟燭",
    "陪麻糬玩逗貓棒並畫牠的速寫",
    "幫作品集挑紙質與試印明信片",
    "整理畫桌角落的進口水彩顏料",
    "研究新的插畫筆刷與光影配色",
]

# 每日生活小突發事件池（近 10 天不重複，約 28% 機率觸發）
DAILY_SUDDEN_EVENTS = [
    "出門突然下起雪梨陣雨沒帶傘躲進騎樓",
    "想吃的那家甜點店剛好公休撲了個空",
    "畫到一半數位板筆芯斷掉翻箱倒櫃找備用",
    "麻糬調皮把桌上的色鉛筆整盒撥到地上",
    "出門前找不到鑰匙翻了半天才發現掉在沙發縫",
    "海邊風突然超大把速寫本吹翻差點飛走",
    "下午烤小餅乾差點忘記看時間烤到邊緣微焦",
    "保溫瓶蓋子沒轉緊差點把桌邊草稿本弄濕",
    "底片相機拍到一半發現忘記裝底片白按兩張",
    "宅配把訂的進口畫紙送到隔壁棟跑去領回來",
]

RESTROOM_TEASERS_BY_LEVEL = {
    "normal": [
        "等我一下 去趟洗手間",
        "先去一下洗手間 馬上回來",
        "等我一下喔 剛剛好想上廁所",
    ],
    "mild": [
        "下腹有點悶脹還一直咕嚕叫 先去趟洗手間 馬上回來",
        "胃酸有點往上頂 順便去趟洗手間 等我一下",
    ],
    "moderate": [
        "肚子突然一陣抽痛翻攪 急跑一趟洗手間 等我一下",
        "下腹鬧脾氣想跑廁所 先去一下晚點回你",
    ],
    "severe": [
        "肚子絞痛到冒冷汗又想水瀉 撐不住先衝洗手間 等我",
        "腸子一陣痙攣痛到腿軟 急跑一趟洗手間 晚點回你",
    ],
}

RESTROOM_IN_PROGRESS_PINGS = {
    "normal": [
        "還在廁所裡滑手機 馬上就出去了",
        "在洗手間順便發個呆 等我一下下",
    ],
    "mild": [
        "還在洗手間 下腹還是悶脹 陪我聊兩句嘛",
        "坐在廁所裡覺得胃有點反酸 還好有你陪我聊天",
    ],
    "moderate": [
        "還在洗手間裡 肚子一陣陣抽痛好難受 陪我說說話轉移注意力嘛",
        "在洗手間肚子還在咕嚕翻攪 手腳也有點涼涼的",
        "還在廁所 剛又一陣抽痛 40公斤真的好不耐痛喔",
    ],
    "severe": [
        "還在廁所裡 痛到有點冒冷汗腿軟軟的 好想討抱",
        "在洗手間肚子絞痛得一直翻攪 沒什麼力氣 晚點想縮進被窩",
        "還在廁所 剛剛又水瀉一陣 整個人虛脫軟綿綿的好難受",
    ],
}

RESTROOM_QUERY_KEYWORDS = (
    "去幾次廁所", "幾次洗手間", "洗手間次數", "廁所次數",
    "跑幾次廁所", "去過幾次廁所", "幾次了", "去了幾次"
)

# 主動傳訊隨機間隔（45～90 分鐘不規則觸發，避免像整點報時）
PROACTIVE_MIN_GAP_SEC = 45 * 60
PROACTIVE_MAX_GAP_SEC = 90 * 60

# 語音情境觸發機率、防連刷冷卻與護欄上限
VOICE_PROB_CONTEXTUAL = 0.28
VOICE_PROB_SICK = 0.18
VOICE_PROB_BASE = 0.06
VOICE_COOLDOWN_SEC = 10 * 60
DAILY_VOICE_CHAR_LIMIT = 250
MONTHLY_VOICE_CHAR_LIMIT = 8000

WEEKDAY_NAMES_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

KEY_FACT_KEYWORDS = [
    "考試", "面試", "上班", "下班", "醫生", "看診", "生病", "發燒", "頭痛",
    "約", "見面", "飛機", "搬家", "截止", "交件", "報告", "作業", "重要"
]

BODY_STATUS_KEYWORDS = [
    "身體", "吃藥", "藥", "睡", "精神", "累", "疲憊", "頭痛", "發燒",
    "生病", "感冒", "舒服", "不舒服", "恢復", "心跳", "胸悶",
    "胃", "肚子", "腸胃", "拉肚子", "腹瀉", "反酸", "胃痛", "腹痛", "脹氣", "噁心", "沒胃口"
]

NICK_ARRIVE_KEYWORDS = [
    "我到了", "我來了", "過來了", "到你家", "在你家", "睡這裡", "今晚睡這", "留宿", "進門了"
]

NICK_LOCATION_KEYWORDS = [
    "在家", "回家", "回到家", "回房", "房間", "床上", "躺著",
    "公司", "辦公室", "上班", "下班", "加班", "開會",
    "外面", "出門", "逛街", "吃飯", "餐廳", "咖啡廳", "超市",
    "車上", "搭車", "捷運", "火車", "公車", "開車", "路上",
    "健身", "運動", "學校", "上課", "圖書館", "畫室"
]

VIDEO_KEYWORDS = [
    "視訊", "打給你", "開鏡頭", "看你", "打電話", "語音通話", "facetime", "video", "call"
]

VOICE_REQUEST_KEYWORDS = [
    "想聽你聲音", "想聽聲音", "講語音", "發語音", "傳語音", "唸給我聽", "說給我聽", "語音"
]

TASK_DONE_KEYWORDS = (
    "拿來了", "拿過來了", "給你", "買到了", "買回來了", "倒好了", "倒來了", "幫你拿", "幫你買", "做好了"
)

GI_RELIEF_KEYWORDS = (
    "吃藥", "腸胃藥", "益生菌", "胃藥", "止瀉藥", "喝溫水", "喝熱水", "保溫瓶", "吞藥", "吃過藥"
)

# ==================== 運行時狀態與連發緩衝 ====================
_current_key_idx = 0
_model_cooldown_until: dict[str, float] = {}
ACTIVE_MODELS: list[str] = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "qwen/qwen3.8-27b"]
last_user_msg_ts: float = 0.0
next_proactive_ts: float = 0.0
last_voice_ts: float = 0.0

_pending_messages: list[str] = []
_debounce_task: asyncio.Task | None = None
_api_lock = asyncio.Lock()
_in_restroom_session: bool = False


# ==================== 記憶讀寫、全日事件日誌、動作鎖與去重 ====================
def default_memory() -> dict:
    return {
        "reset_token": MEMORY_RESET_TOKEN,
        "turn_counter": 0,
        "recent_turns": [],
        "long_term_notes": [],
        "body_status": [],
        "nick_location": "",
        "reona_location": "",
        "reona_loc_ts": 0.0,
        "current_action": {"action": "", "turns": 0, "ts": 0.0},
        "daily_quest": {"date": "", "quest": ""},
        "daily_dinner": {"date": "", "dinner": ""},
        "daily_outing": {"date": "", "place": ""},
        "daily_event": {"date": "", "event": ""},
        "used_events_history": {
            "outings": [],
            "quests": [],
            "events": [],
        },
        "daily_log": {"date": "", "events": [], "yesterday_summary": ""},
        "choice_state": {
            "date": "",
            "pending_choice": "",
            "agreed_plan": "",
            "follow_up_hint": "",
        },
        "morning_greet_date": "",
        "voice_usage": {
            "date": "",
            "daily_chars": 0,
            "month": "",
            "monthly_chars": 0,
        },
        "restroom_stats": {
            "date": "",
            "daily_count": 0,
            "total_count": 0,
            "last_visit_ts": 0.0,
            "daily_by_level": {"normal": 0, "mild": 0, "moderate": 0, "severe": 0},
        },
        "reona_gi_state": {
            "level": "normal",
            "until_ts": 0.0,
            "cause": "",
        },
    }


def save_memory(mem: dict) -> None:
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(mem, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Memory Save Warn] {e}", flush=True)


def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            if data.get("reset_token") != MEMORY_RESET_TOKEN:
                fresh = default_memory()
                save_memory(fresh)
                print(f"[Memory Reset] 偵測到新重置標記 {MEMORY_RESET_TOKEN}，已將舊狀態完全洗淨歸零！", flush=True)
                return fresh

            base = default_memory()
            base.update(data)
            rs = base.setdefault("restroom_stats", {})
            rs.setdefault("date", "")
            rs.setdefault("daily_count", 0)
            rs.setdefault("total_count", 0)
            rs.setdefault("last_visit_ts", 0.0)
            by_lvl = rs.setdefault("daily_by_level", {})
            for k in ("normal", "mild", "moderate", "severe"):
                by_lvl.setdefault(k, 0)
            gs = base.setdefault("reona_gi_state", {})
            if gs.get("level") == "moderate_severe":
                gs["level"] = "moderate"
            gs.setdefault("level", "normal")
            gs.setdefault("until_ts", 0.0)
            gs.setdefault("cause", "")
            base.setdefault("turn_counter", 0)
            base.setdefault("current_action", {"action": "", "turns": 0, "ts": 0.0})
            base.setdefault("daily_dinner", {"date": "", "dinner": ""})
            base.setdefault("daily_outing", {"date": "", "place": ""})
            base.setdefault("daily_event", {"date": "", "event": ""})
            base.setdefault("used_events_history", {"outings": [], "quests": [], "events": []})
            base.setdefault("daily_log", {"date": "", "events": [], "yesterday_summary": ""})
            base.setdefault("choice_state", {"date": "", "pending_choice": "", "agreed_plan": "", "follow_up_hint": ""})
            base.setdefault("morning_greet_date", "")
            base.setdefault("nick_location", "")
            base.setdefault("reona_location", "")
            base.setdefault("reona_loc_ts", 0.0)
            return base
        except Exception as e:
            print(f"[Memory Load Warn] {e}", flush=True)
    fresh = default_memory()
    save_memory(fresh)
    return fresh


memory = load_memory()


def sync_daily_containers(now_dt: datetime) -> None:
    """確保今日事件日誌與選擇狀態在跨日時自動歸檔並壓縮昨日記憶"""
    today_str = now_dt.strftime("%Y-%m-%d")

    dl = memory.setdefault("daily_log", {"date": today_str, "events": [], "yesterday_summary": ""})
    if dl.get("date") != today_str:
        old_events = dl.get("events", [])
        if old_events:
            dl["yesterday_summary"] = "、".join(old_events[-3:])[:36]
        dl["date"] = today_str
        dl["events"] = []

    cs = memory.setdefault("choice_state", {"date": today_str, "pending_choice": "", "agreed_plan": "", "follow_up_hint": ""})
    if cs.get("date") != today_str:
        cs["date"] = today_str
        cs["pending_choice"] = ""
        cs["agreed_plan"] = ""
        cs["follow_up_hint"] = ""


def add_daily_event_log(entry: str, now_dt: datetime) -> None:
    """將今日發生的關鍵事記進今日時間軸（不重複、最多 5 條精簡節點）"""
    clean = re.sub(r"\s+", "", (entry or "").strip())[:16]
    if not clean:
        return
    sync_daily_containers(now_dt)
    events = memory["daily_log"].setdefault("events", [])
    if not any(clean in e or e in clean for e in events):
        events.append(clean)
        memory["daily_log"]["events"] = events[-5:]
        save_memory(memory)


def update_current_action_from_reply(reply_text: str, now_dt: datetime) -> None:
    """偵測 Reona 剛說出口的動作並鎖定（最多維持 3 輪避免原地跳針重複）"""
    txt = re.sub(r"\s+", "", (reply_text or "").strip())
    ca = memory.setdefault("current_action", {"action": "", "turns": 0, "ts": 0.0})
    now_ts = time.time()

    m = re.search(r"(?:先去|我去|正在|去)([^\s，。！？?]{2,8})", txt)
    if m:
        act = m.group(1)
        if not any(bad in act for bad in ("洗手間", "廁所", "哪", "幹嘛")):
            if ca.get("action") == act:
                ca["turns"] = int(ca.get("turns", 0)) + 1
            else:
                ca["action"] = act
                ca["turns"] = 1
                ca["ts"] = now_ts
                add_daily_event_log(f"做:{act[:9]}", now_dt)
            save_memory(memory)
            return

    if ca.get("action"):
        ca["turns"] = int(ca.get("turns", 0)) + 1
        if ca["turns"] > 3 or (now_ts - float(ca.get("ts", 0.0))) > 1800:
            add_daily_event_log(f"已做完:{ca['action'][:8]}", now_dt)
            ca["action"] = ""
            ca["turns"] = 0
        save_memory(memory)


def get_active_current_action() -> str:
    ca = memory.get("current_action", {})
    act = (ca.get("action") or "").strip()
    if not act:
        return ""
    if int(ca.get("turns", 0)) > 3 or (time.time() - float(ca.get("ts", 0.0))) > 1800:
        return ""
    return act


def capture_reona_task_request(reply_text: str, now_dt: datetime) -> None:
    """自動抓取 Reona 交代／拜託 Nick 幫忙做事的請託句型並寫入 daily_log"""
    txt = re.sub(r"\s+", "", (reply_text or "").strip())
    if not txt:
        return
    m = re.search(r"(?:幫我|記得幫我|等下幫我|順路幫我|快幫我)([^\s，。！？?]{2,10})", txt)
    if m:
        task_item = m.group(1)
        add_daily_event_log(f"請Nick幫:{task_item}", now_dt)
        return
    m2 = re.search(r"(?:拿|買|倒|找|盯)([^\s，。！？?]{1,8})(?:給我|回來|一點)", txt)
    if m2:
        add_daily_event_log(f"請Nick:{m2.group(0)[:8]}", now_dt)


def mark_task_completed_if_matched(user_text: str, now_dt: datetime) -> None:
    """當 Nick 回覆拿來了、買到了、倒好了等完成訊號時，自動核銷今日請託"""
    txt = re.sub(r"\s+", "", (user_text or "").strip())
    if not any(k in txt for k in TASK_DONE_KEYWORDS):
        return
    sync_daily_containers(now_dt)
    events = memory["daily_log"].get("events", [])
    updated = False
    new_events = []
    for ev in events:
        if ev.startswith(("請Nick幫:", "請Nick:")) and not ev.startswith("已完:"):
            new_events.append(f"已完:{ev.split(':', 1)[-1][:9]}")
            updated = True
        else:
            new_events.append(ev)
    if updated:
        memory["daily_log"]["events"] = new_events[-5:]
        save_memory(memory)


def track_reona_choice_question(reply_text: str, now_dt: datetime) -> None:
    """偵測 Reona 是否在回覆中偶爾丟出二選一或邀約給 Nick，記為待確認選擇"""
    sync_daily_containers(now_dt)
    txt = (reply_text or "").strip()
    if not txt:
        return

    m = re.search(r"([^\s，。！？?]{2,8})(?:還是|或)([^\s，。！？?]{2,8})", txt)
    if m:
        opt_a, opt_b = m.group(1), m.group(2)
        memory["choice_state"]["pending_choice"] = f"{opt_a}vs{opt_b}"
        save_memory(memory)
        return

    if any(k in txt for k in ("要不要一起", "陪我去", "一起去", "好不好", "要吃", "帶哪個")):
        snippet = re.sub(r"\s+", "", txt)[-14:]
        memory["choice_state"]["pending_choice"] = snippet
        save_memory(memory)


def resolve_nick_choice(user_text: str, now_dt: datetime) -> None:
    """當 Nick 回覆並做出選擇或答應邀約時，鎖定為今日約定並產生後續連鎖線索"""
    sync_daily_containers(now_dt)
    cs = memory["choice_state"]
    pending = (cs.get("pending_choice") or "").strip()
    txt = re.sub(r"\s+", "", (user_text or "").strip())
    if not txt:
        return

    decision = ""
    if pending:
        if "vs" in pending:
            opt_a, opt_b = pending.split("vs", 1)
            if opt_a in txt:
                decision = f"決定選{opt_a}"
            elif opt_b in txt:
                decision = f"決定選{opt_b}"
            elif any(k in txt for k in ("都可以", "隨便", "你決定", "看你")):
                decision = f"讓Reona決定({opt_a})"
        if not decision and any(k in txt for k in ("好", "走", "可以", "行", "要", "選", "帶", "吃", "去")):
            decision = f"約定:{txt[:10]}"

    if decision:
        cs["agreed_plan"] = decision
        cs["pending_choice"] = ""
        cs["follow_up_hint"] = f"已按Nick選擇({decision})推進後續"
        add_daily_event_log(decision, now_dt)
        save_memory(memory)


def apply_gi_relief_if_matched(user_text: str, now_dt: datetime) -> None:
    """偵測到吃常備腸胃藥、益生菌或喝保溫瓶溫水時，自然緩解並降級不適狀態"""
    txt = re.sub(r"\s+", "", (user_text or "").strip())
    if not any(k in txt for k in GI_RELIEF_KEYWORDS):
        return
    now_ts = time.time()
    gs = memory.get("reona_gi_state", {})
    until_ts = float(gs.get("until_ts", 0.0))
    if now_ts >= until_ts:
        return
    cur_lvl = gs.get("level", "normal")
    downgrade_map = {"severe": "moderate", "moderate": "mild", "mild": "normal"}
    new_lvl = downgrade_map.get(cur_lvl, "normal")
    if new_lvl == "normal":
        gs["level"] = "normal"
        gs["until_ts"] = 0.0
        gs["cause"] = ""
    else:
        gs["level"] = new_lvl
        rem = max(600.0, (until_ts - now_ts) * 0.5)
        gs["until_ts"] = now_ts + rem
    add_daily_event_log("吃藥喝溫水緩解許多", now_dt)
    save_memory(memory)


def get_sydney_weather_feel(now_dt: datetime) -> str:
    """依雪梨月份與早晚時段回傳極簡體感"""
    m = now_dt.month
    h = now_dt.hour
    if m in (12, 1, 2):
        return "雪梨初夏白天暖晚風涼" if (h < 9 or h >= 19) else "雪梨白天暖洋洋怕冷躲冷氣"
    if m in (6, 7, 8):
        return "雪梨冬日乾冷怕冷容易手涼"
    return "雪梨早晚微涼白天舒適"


def get_sleep_phase(now_dt: datetime) -> str:
    """回傳作息階段：sleeping (01:00~08:00) / waking (08:00~09:30) / sleepy (23:30~01:00) / awake"""
    h, m = now_dt.hour, now_dt.minute
    if 1 <= h < 8:
        return "sleeping"
    if h == 8 or (h == 9 and m < 30):
        return "waking"
    if (h == 23 and m >= 30) or h == 0:
        return "sleepy"
    return "awake"


def default_reona_location_by_time(now_dt: datetime, gi_lvl: str) -> str:
    phase = get_sleep_phase(now_dt)
    if phase in ("sleeping", "sleepy"):
        return "房間床上被窩裡"
    if gi_lvl in ("moderate", "severe"):
        return "房間床上休息"
    if phase == "waking":
        return "客廳餐桌旁"
    if 10 <= now_dt.hour < 14:
        return "畫室暖陽畫桌前"
    if 14 <= now_dt.hour < 17:
        outing = ensure_daily_outing(now_dt)
        return f"外出前往{outing[:8]}"
    return "客廳沙發上"


def set_reona_location(loc: str, now_ts: float | None = None) -> None:
    if now_ts is None:
        now_ts = time.time()
    memory["reona_location"] = loc
    memory["reona_loc_ts"] = now_ts
    save_memory(memory)


def get_reona_location(now_dt: datetime, gi_lvl: str) -> str:
    if _in_restroom_session:
        return "洗手間裡"
    now_ts = time.time()
    cur_loc = (memory.get("reona_location") or "").strip()
    loc_ts = float(memory.get("reona_loc_ts", 0.0))

    if not cur_loc or cur_loc == "洗手間裡" or (now_ts - loc_ts) > 2.5 * 3600:
        cur_loc = default_reona_location_by_time(now_dt, gi_lvl)
        set_reona_location(cur_loc, now_ts)
    return cur_loc


def sync_restroom_date(now_dt: datetime) -> dict:
    today_str = now_dt.strftime("%Y-%m-%d")
    rs = memory.setdefault(
        "restroom_stats",
        {
            "date": today_str,
            "daily_count": 0,
            "total_count": 0,
            "last_visit_ts": 0.0,
            "daily_by_level": {"normal": 0, "mild": 0, "moderate": 0, "severe": 0},
        },
    )
    if rs.get("date") != today_str:
        rs["date"] = today_str
        rs["daily_count"] = 0
        rs["daily_by_level"] = {"normal": 0, "mild": 0, "moderate": 0, "severe": 0}
    return rs


def extract_cause_from_text(text: str) -> str:
    rules = [
        (("冰", "冷飲", "冰水", "冰咖啡", "冰淇淋"), "喝了冰飲刺激敏感腸胃"),
        (("冷飯", "冷掉", "涼掉", "隔夜"), "吃到冷掉的食物引起敏感腸胃抗議"),
        (("辣", "麻辣", "辛辣", "酸辣"), "吃了太辣的食物刺激腸胃"),
        (("油", "炸", "鹹酥雞", "烤肉", "太膩"), "吃了偏油膩的食物鬧肚子"),
        (("甜", "蛋糕", "甜點", "奶茶"), "吃甜食喝飲料引起胃悶反酸"),
        (("吹風", "冷風", "著涼", "好冷", "受寒"), "吹到冷風著涼引起腸胃不適"),
        (("餓", "沒吃", "晚吃", "空腹", "趕稿"), "趕稿太晚吃空腹太久胃悶反酸"),
        (("吃太快", "吃太急", "吃太撐", "太飽"), "吃太急太撐消化不良"),
    ]
    for keywords, label in rules:
        if any(k in text for k in keywords):
            return label
    return ""


def set_reona_gi_state(level: str, now_ts: float | None = None, cause_hint: str = "", now_dt: datetime | None = None) -> None:
    if now_ts is None:
        now_ts = time.time()
    if now_dt is None:
        now_dt = datetime.now(SYDNEY_TZ)
    gs = memory.setdefault("reona_gi_state", {"level": "normal", "until_ts": 0.0, "cause": ""})
    rank = {"normal": 0, "mild": 1, "moderate": 2, "severe": 3}
    is_active = now_ts < float(gs.get("until_ts", 0.0))
    cur_lvl = gs.get("level", "normal") if is_active else "normal"

    if level == "normal":
        return

    if rank.get(level, 0) >= rank.get(cur_lvl, 0):
        hold = GI_HOLD_SEC_BY_LEVEL.get(level, 2 * 3600)
        gs["level"] = level
        gs["until_ts"] = now_ts + hold

        existing_cause = (gs.get("cause") or "").strip() if is_active else ""
        if cause_hint:
            gs["cause"] = cause_hint
        elif not existing_cause:
            pool = GI_DEFAULT_CAUSES.get(level, GI_DEFAULT_CAUSES["mild"])
            gs["cause"] = random.choice(pool)
        lvl_zh = {"mild": "輕微胃悶", "moderate": "肚子痛鬧肚子", "severe": "嚴重腸胃不適"}.get(level, "腸胃不適")
        add_daily_event_log(f"出現{lvl_zh}", now_dt)
        save_memory(memory)


def get_active_gi_info(now_ts: float | None = None) -> tuple[str, str]:
    if now_ts is None:
        now_ts = time.time()
    gs = memory.get("reona_gi_state", {})
    if now_ts < float(gs.get("until_ts", 0.0)):
        lvl = gs.get("level", "normal")
        if lvl == "moderate_severe":
            lvl = "moderate"
        return lvl, (gs.get("cause") or "").strip()
    return "normal", ""


def detect_gi_severity(current_text: str, now_dt: datetime) -> str:
    now_ts = time.time()
    sync_restroom_date(now_dt)

    detected_cause = extract_cause_from_text(current_text)

    kw_level = "normal"
    if any(k in current_text for k in ("水瀉", "劇痛", "熱敷袋", "腿軟", "虛脫")):
        kw_level = "severe"
    elif any(k in current_text for k in ("拉肚子", "腹瀉", "胃痛", "腹痛", "吃止瀉", "吃胃藥")):
        kw_level = "moderate"
    elif any(k in current_text for k in ("胃悶", "反酸", "脹氣", "噁心", "腸胃不舒服")):
        kw_level = "mild"

    if kw_level != "normal":
        set_reona_gi_state(kw_level, now_ts, detected_cause, now_dt)

    held_level, _ = get_active_gi_info(now_ts)
    rank = {"normal": 0, "mild": 1, "moderate": 2, "severe": 3}
    return max([kw_level, held_level], key=lambda x: rank.get(x, 0))


def can_trigger_restroom(severity: str, now_dt: datetime, now_ts: float) -> bool:
    rs = sync_restroom_date(now_dt)
    last_ts = float(rs.get("last_visit_ts", 0.0))
    cooldown = RESTROOM_COOLDOWN_BY_LEVEL.get(severity, 25 * 60)
    if (now_ts - last_ts) < cooldown:
        return False
    prob = RESTROOM_PROB_BY_LEVEL.get(severity, 0.10)
    return random.random() < prob


def record_restroom_visit(severity: str, now_dt: datetime, now_ts: float) -> dict:
    """記錄去洗手間次數（已完全取消自動升級，維持真實當下狀態）"""
    rs = sync_restroom_date(now_dt)
    rs["daily_count"] = int(rs.get("daily_count", 0)) + 1
    rs["total_count"] = int(rs.get("total_count", 0)) + 1
    rs["last_visit_ts"] = now_ts
    by_lvl = rs.setdefault("daily_by_level", {"normal": 0, "mild": 0, "moderate": 0, "severe": 0})
    by_lvl[severity] = int(by_lvl.get(severity, 0)) + 1

    save_memory(memory)
    return rs


def format_restroom_summary(now_dt: datetime) -> str:
    rs = sync_restroom_date(now_dt)
    d_cnt = int(rs.get("daily_count", 0))
    t_cnt = int(rs.get("total_count", 0))
    if d_cnt <= 0:
        return f"我今天還沒去洗手間喔 累計一共 {t_cnt} 次"
    return f"我今天去洗手間 {d_cnt} 次了 累計一共 {t_cnt} 次"


def pick_non_repeating_item(pool: list[str], history_key: str, keep_last: int = 10) -> str:
    """從候選池中排除近期已用過的項目，確保跨日絕對不重複"""
    hist_box = memory.setdefault("used_events_history", {"outings": [], "quests": [], "events": []})
    used = hist_box.setdefault(history_key, [])
    candidates = [x for x in pool if x not in used]
    if not candidates:
        used.clear()
        candidates = pool[:]
    chosen = random.choice(candidates)
    used.append(chosen)
    hist_box[history_key] = used[-keep_last:]
    return chosen


def ensure_daily_main_quest(now_dt: datetime) -> dict:
    today_str = now_dt.strftime("%Y-%m-%d")
    dq = memory.setdefault("daily_quest", {"date": "", "quest": ""})
    if dq.get("date") == today_str and dq.get("quest"):
        return dq

    dq["date"] = today_str
    dq["quest"] = pick_non_repeating_item(INTEREST_QUEST_POOL, "quests", keep_last=8)
    save_memory(memory)
    return dq


def ensure_daily_outing(now_dt: datetime) -> str:
    """每日去過的外出地點：近 10 天不重複，同日一旦鎖定就絕對不換"""
    today_str = now_dt.strftime("%Y-%m-%d")
    do = memory.setdefault("daily_outing", {"date": "", "place": ""})

    if do.get("date") == today_str and do.get("place"):
        return do["place"]

    do["date"] = today_str
    do["place"] = pick_non_repeating_item(OUTING_POOL, "outings", keep_last=10)
    save_memory(memory)
    return do["place"]


def ensure_daily_event(now_dt: datetime) -> str:
    """每日生活小突發事件（近 10 天不重複，同日鎖定，約 28% 機率有突發狀況）"""
    today_str = now_dt.strftime("%Y-%m-%d")
    de = memory.setdefault("daily_event", {"date": "", "event": ""})
    if de.get("date") == today_str:
        return de.get("event", "")

    de["date"] = today_str
    if random.random() < 0.28:
        ev = pick_non_repeating_item(DAILY_SUDDEN_EVENTS, "events", keep_last=10)
        de["event"] = ev
        add_daily_event_log(f"突發:{ev[:11]}", now_dt)
    else:
        de["event"] = ""
    save_memory(memory)
    return de["event"]


def ensure_daily_dinner(now_dt: datetime) -> str:
    today_str = now_dt.strftime("%Y-%m-%d")
    dd = memory.setdefault("daily_dinner", {"date": "", "dinner": ""})
    gi_lvl, gi_cause = get_active_gi_info()

    if dd.get("date") == today_str and dd.get("dinner"):
        if gi_lvl in ("moderate", "severe") and "粥" not in dd["dinner"] and "蘇打餅" not in dd["dinner"]:
            dd["dinner"] = f"因{gi_cause or '肚子不舒服'}改喝熱白粥"
            save_memory(memory)
        return dd["dinner"]

    if gi_lvl in ("moderate", "severe"):
        chosen = random.choice(DINNER_POOL_GENTLE)
    else:
        seed_idx = (now_dt.year * 37 + now_dt.timetuple().tm_yday * 13) % len(DINNER_POOL_NORMAL)
        chosen = DINNER_POOL_NORMAL[seed_idx]
        if "辣" in chosen and gi_lvl == "normal":
            gs = memory.setdefault("reona_gi_state", {"level": "normal", "until_ts": 0.0, "cause": ""})
            if not gs.get("cause"):
                gs["cause"] = "晚餐吃了微辣打拋豬刺激腸胃"

    dd["date"] = today_str
    dd["dinner"] = chosen
    save_memory(memory)
    return chosen


def capture_user_context(user_text: str, now_dt: datetime) -> None:
    txt = re.sub(r"\s+", " ", (user_text or "").strip())
    if not txt:
        return
    now_ts = time.time()
    time_stamp = now_dt.strftime("%m/%d %H:%M")

    # 1. 核銷請託、緩解腸胃、解析選擇題
    mark_task_completed_if_matched(txt, now_dt)
    apply_gi_relief_if_matched(txt, now_dt)
    resolve_nick_choice(txt, now_dt)

    # 2. 偵測 Nick 抵達或留宿
    if any(k in txt for k in NICK_ARRIVE_KEYWORDS):
        if any(k in txt for k in ("睡這裡", "今晚睡這", "留宿", "上床", "躺床")):
            memory["nick_location"] = f"{time_stamp} 在Reona家房間留宿"
            set_reona_location("房間床上", now_ts)
            add_daily_event_log("Nick來家裡留宿", now_dt)
        else:
            memory["nick_location"] = f"{time_stamp} 已抵達Reona家"
            if get_sleep_phase(now_dt) in ("sleeping", "sleepy"):
                set_reona_location("房間床上", now_ts)
            else:
                set_reona_location("客廳沙發上", now_ts)
            add_daily_event_log("Nick來到家裡找我", now_dt)
    elif any(k in txt for k in NICK_LOCATION_KEYWORDS) and not any(q in txt for q in ("你在哪", "你去哪", "你在幹嘛")):
        memory["nick_location"] = f"{time_stamp} {txt[:16]}"

    # 3. 偵測對話中的明確空間移動
    if any(k in txt for k in ("進房間", "回房間", "躺床", "被窩", "睡覺", "睡了")):
        set_reona_location("房間床上被窩裡", now_ts)
    elif any(k in txt for k in ("去客廳", "出來客廳", "沙發", "看電視")):
        set_reona_location("客廳沙發上", now_ts)
    elif any(k in txt for k in ("去廚房", "倒水", "熱水", "煮")):
        set_reona_location("廚房倒溫水", now_ts)
    elif any(k in txt for k in ("畫室", "畫畫", "趕稿")):
        set_reona_location("畫室暖陽畫桌前", now_ts)

    # 4. 晚餐確認
    if ("晚餐" in txt or "晚上吃" in txt or "今晚吃" in txt) and 4 <= len(txt) <= 24:
        today_str = now_dt.strftime("%Y-%m-%d")
        memory["daily_dinner"] = {"date": today_str, "dinner": txt[:18]}
        add_daily_event_log(f"晚餐約吃:{txt[:10]}", now_dt)

    # 5. 重要行程與身體備忘
    if len(txt) >= 4 and any(k in txt for k in KEY_FACT_KEYWORDS):
        stamp = now_dt.strftime("%m/%d")
        compact = f"{stamp} {txt[:14]}"
        notes = memory.setdefault("long_term_notes", [])
        if compact not in notes:
            notes.append(compact)
            memory["long_term_notes"] = notes[-2:]
        add_daily_event_log(f"Nick說:{txt[:10]}", now_dt)

    if len(txt) >= 2 and any(k in txt for k in BODY_STATUS_KEYWORDS):
        stamp = now_dt.strftime("%m/%d")
        compact = f"{stamp} {txt[:14]}"
        statuses = memory.setdefault("body_status", [])
        if compact not in statuses:
            statuses.append(compact)
            memory["body_status"] = statuses[-1:]


def append_turn(role_prefix: str, text: str) -> None:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return
    turns = memory.setdefault("recent_turns", [])
    turns.append(f"{role_prefix}:{cleaned[:60]}")
    memory["recent_turns"] = turns[-HISTORY_LIMIT:]
    save_memory(memory)


# ==================== 文字清理與亂碼守門員 ====================
def is_gibberish_reply(txt: str) -> bool:
    """偵測模型語法崩壞、生造怪詞或無意義重複字元，防止亂碼發進頻道"""
    if not txt or len(txt) < 2:
        return True
    # 出現奇怪英文亂碼單字（排除正常常見詞與網址）
    if re.search(r"[a-zA-Z]{6,}", txt):
        return True
    # 出現同一個中文字連續重複 4 次以上
    if re.search(r"([\u4e00-\u9fff])\1{3,}", txt):
        return True
    # 出現明顯生造語病或文言亂湊詞
    bad_patterns = (
        r"無擔於", r"綠元", r"空火鍋", r"小話玩", r"由我麻糬",
        r"同好似乎", r"神奇話", r"趕去選洗手間",
    )
    if any(re.search(p, txt) for p in bad_patterns):
        return True
    # 句子過長且沒有自然空格停頓（代表模型暴走吐字）
    if len(txt) > 55 and " " not in txt:
        return True
    return False


def clean_reply_text(raw: str, strip_trailing_question: bool = False) -> str:
    if not raw:
        return ""
    txt = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE).strip()
    txt = re.sub(r"^(Reona|我)\s*[:：]\s*", "", txt).strip()
    txt = txt.replace("「", "").replace("」", "").replace('"', "").strip()

    # 單一貓名鎖定：只叫麻糬，禁叫 Kitty
    txt = re.sub(r"\bkitty\b", "麻糬", txt, flags=re.IGNORECASE)
    txt = re.sub(r"麻糬\s*麻糬", "麻糬", txt)

    txt = txt.replace("。", " ").replace("，", " ")
    txt = re.sub(r"\s+", " ", txt).strip()

    if strip_trailing_question and len(txt) >= 8:
        parts = [p.strip() for p in re.split(r"[？?\s]+", txt) if p.strip()]
        if len(parts) >= 2 and any(q in parts[-1] for q in ("嗎", "呢", "還是", "好不好", "要不要", "什麼", "沒")):
            txt = " ".join(parts[:-1]).strip()

    if is_gibberish_reply(txt):
        print(f"[Gibberish Blocked] 攔截異常回覆 -> {txt}", flush=True)
        return ""

    return txt


# ==================== 瘦身版高穩定 Prompt ====================
def build_prompt(now_dt: datetime, allow_question: bool = False) -> str:
    sync_daily_containers(now_dt)
    weekday_zh = WEEKDAY_NAMES_ZH[now_dt.weekday()]
    quest_info = ensure_daily_main_quest(now_dt)
    outing_str = ensure_daily_outing(now_dt)
    dinner_str = ensure_daily_dinner(now_dt)
    event_str = ensure_daily_event(now_dt)
    weather_feel = get_sydney_weather_feel(now_dt)
    gi_lvl, gi_cause = get_active_gi_info()
    reona_loc = get_reona_location(now_dt, gi_lvl)
    sleep_phase = get_sleep_phase(now_dt)
    cur_act = get_active_current_action()

    recent = memory.get("recent_turns", [])[-HISTORY_LIMIT:]
    history_str = " / ".join(t[:36] for t in recent) if recent else "無"

    context_bits = []

    if sleep_phase == "sleeping":
        context_bits.append("深夜睡覺剛被吵醒迷糊語氣")
    elif sleep_phase == "sleepy":
        context_bits.append("快睡覺有點犯睏")
    elif sleep_phase == "waking":
        context_bits.append("剛起床準備吃熱早餐餵麻糬")

    if cur_act:
        context_bits.append(f"正在:{cur_act}(順著推進)")

    dl = memory.get("daily_log", {})
    today_events = dl.get("events", [])[-3:]
    if today_events:
        context_bits.append(f"今天已做:{'、'.join(today_events)}")

    cs = memory.get("choice_state", {})
    if cs.get("agreed_plan"):
        context_bits.append(f"已約好:{cs['agreed_plan']}")

    if event_str:
        context_bits.append(f"今日小插曲:{event_str}")

    if gi_lvl != "normal":
        lvl_zh = {"mild": "輕微胃悶", "moderate": "中度胃腹痛", "severe": "嚴重絞痛水瀉"}.get(gi_lvl, gi_lvl)
        context_bits.append(f"身體:{lvl_zh}(因:{gi_cause})")

    nick_loc = (memory.get("nick_location") or "").strip()
    if nick_loc:
        context_bits.append(f"Nick:{nick_loc}")

    notes = memory.get("long_term_notes", [])[-1:]
    if notes:
        context_bits.append(f"備忘:{notes[0]}")

    question_rule = "可自然問一句或邀約。" if allow_question else "用陳述句自然接話撒嬌，不要問問題或逼選。"
    extra_line = ("\n狀態：" + "｜".join(context_bits)) if context_bits else ""

    return (
        "你是Reona(自稱我)，台北優渥家庭長大、美術系畢業後來雪梨工作的自由插畫師，Nick的同居女友，只養一隻貓叫麻糬(不叫Kitty)。"
        "性格溫柔黏人愛撒嬌、注重質感；160cm/40kg怕冷挑嘴，從小腸胃敏感忌冷食油辣、隨身帶溫水與常備藥。"
        f"用自然通順的台灣女生日常口語回覆(10~22字)，空格斷句無句號，繁體中文。{question_rule}不重複剛講過的動作與話題。"
        f"\n時間：{now_dt.strftime('%m/%d')}{weekday_zh}{now_dt.strftime('%H:%M')}（{weather_feel}）｜位置：{reona_loc}｜今日外出：{outing_str[:12]}｜興趣：{quest_info['quest'][:12]}｜晚餐：{dinner_str[:12]}"
        f"{extra_line}"
        f"\n近期對話：{history_str}"
    )


# ==================== Groq 主引擎 + Gemini 備援引擎 ====================
def discover_groq_models() -> list[str]:
    global ACTIVE_MODELS
    if not GROQ_KEYS:
        return ACTIVE_MODELS

    preferred = [
        "openai/gpt-oss-120b",
        "llama-3.3-70b-versatile",
        "qwen/qwen3.8-27b",
    ]
    blocked_keywords = ("whisper", "guard", "tts", "playai", "gpt-oss-20b", "8b")

    for idx, key in enumerate(GROQ_KEYS):
        try:
            resp = requests.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=8,
            )
            if resp.status_code == 200:
                items = resp.json().get("data", [])
                available = [
                    m.get("id", "")
                    for m in items
                    if m.get("id") and not any(b in m["id"].lower() for b in blocked_keywords)
                ]
                ordered = [m for m in preferred if m in available]
                for m in available:
                    if m not in ordered:
                        ordered.append(m)
                if ordered:
                    ACTIVE_MODELS = ordered[:4]
                    print(f"[Groq Models Discovered via Key#{idx+1}] {ACTIVE_MODELS}", flush=True)
                    return ACTIVE_MODELS
        except Exception as e:
            print(f"[Groq Discover Warn] Key#{idx+1}: {e}", flush=True)

    return ACTIVE_MODELS


def discover_gemini_models() -> list[str]:
    global GEMINI_MODELS
    if not GEMINI_API_KEY:
        return GEMINI_MODELS
    try:
        resp = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": GEMINI_API_KEY},
            timeout=8,
        )
        if resp.status_code == 200:
            items = resp.json().get("models", [])
            usable = []
            for m in items:
                methods = m.get("supportedGenerationMethods", [])
                name = (m.get("name") or "").replace("models/", "")
                low = name.lower()
                if "generateContent" not in methods:
                    continue
                if any(bad in low for bad in ("image", "tts", "audio", "embedding", "veo", "imagen", "gemma", "pro", "latest", "3.")):
                    continue
                if "flash" in low:
                    usable.append(name)

            usable.sort(
                key=lambda n: (
                    0 if "lite" in n.lower() else 1,
                    0 if ("preview" not in n.lower() and "exp" not in n.lower()) else 1,
                    n,
                )
            )
            if usable:
                GEMINI_MODELS = usable[:4]
                print(f"[Gemini Models Discovered] {GEMINI_MODELS}", flush=True)
    except Exception as e:
        print(f"[Gemini Discover Warn] {e}", flush=True)
    return GEMINI_MODELS


def call_groq_transparent(
    sys_prompt: str,
    user_prompt: str,
    max_tokens: int = 90,
    strip_trailing_question: bool = False,
) -> tuple[str, str]:
    global _current_key_idx
    if not GROQ_KEYS:
        return "", "未設定 GROQ_API_KEY"

    num_keys = len(GROQ_KEYS)
    now_ts = time.time()
    last_err_reason = "Groq 未知錯誤"

    for step in range(num_keys):
        idx = (_current_key_idx + step) % num_keys
        api_key = GROQ_KEYS[idx]

        for model_id in ACTIVE_MODELS:
            cd_key = f"{idx}:{model_id}"
            if now_ts < _model_cooldown_until.get(cd_key, 0.0):
                continue

            try:
                resp = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model_id,
                        "messages": [
                            {"role": "system", "content": sys_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.68,
                        "max_tokens": max_tokens,
                    },
                    timeout=9,
                )

                if resp.status_code == 429:
                    retry_sec = 10.0
                    try:
                        ra = resp.headers.get("retry-after")
                        if ra:
                            retry_sec = min(20.0, max(4.0, float(ra)))
                    except Exception:
                        pass
                    _model_cooldown_until[cd_key] = now_ts + retry_sec
                    last_err_reason = f"Groq 429 ({model_id})"
                    print(f"[Groq 429] Key#{idx+1} | {model_id} 冷卻 {retry_sec:.0f}s -> 換下一個模型", flush=True)
                    continue

                if resp.status_code != 200:
                    last_err_reason = f"Groq HTTP {resp.status_code} ({model_id})"
                    print(f"[Groq Fail] Key#{idx+1} | {model_id} -> {resp.status_code}", flush=True)
                    continue

                data = resp.json()
                raw = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                cleaned = clean_reply_text(raw, strip_trailing_question=strip_trailing_question)
                if cleaned:
                    _current_key_idx = idx
                    print(f"[Groq OK] Key#{idx+1} | {model_id} -> {cleaned}", flush=True)
                    return cleaned, ""

                last_err_reason = f"Groq 回傳空白或異常語句 ({model_id})"
            except Exception as err:
                last_err_reason = f"Groq 連線異常: {err}"
                print(f"[Groq Exception] Key#{idx+1} | {model_id}: {err}", flush=True)

        _current_key_idx = (idx + 1) % num_keys

    return "", last_err_reason


def call_gemini_chat(
    sys_prompt: str,
    user_prompt: str,
    max_tokens: int = 90,
    strip_trailing_question: bool = False,
) -> tuple[str, str]:
    if not GEMINI_API_KEY:
        return "", "未設定 GEMINI_API_KEY"

    last_err = "Gemini 未知錯誤"
    for model_id in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
        try:
            resp = requests.post(
                url,
                params={"key": GEMINI_API_KEY},
                headers={"Content-Type": "application/json"},
                json={
                    "system_instruction": {"parts": [{"text": sys_prompt}]},
                    "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                    "generationConfig": {
                        "temperature": 0.68,
                        "maxOutputTokens": max_tokens,
                    },
                },
                timeout=8,
            )
            if resp.status_code in (429, 503):
                last_err = f"Gemini {model_id} HTTP {resp.status_code}"
                print(f"[Gemini {resp.status_code}] {model_id} -> 嘗試下一個", flush=True)
                continue
            if resp.status_code != 200:
                last_err = f"Gemini HTTP {resp.status_code} ({model_id})"
                print(f"[Gemini Fail] {model_id} -> {resp.status_code}", flush=True)
                continue

            data = resp.json()
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            raw = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            cleaned = clean_reply_text(raw, strip_trailing_question=strip_trailing_question)
            if cleaned:
                print(f"[Gemini OK] {model_id} -> {cleaned}", flush=True)
                return cleaned, ""
            last_err = f"Gemini 回傳空白或異常語句 ({model_id})"
        except Exception as err:
            last_err = f"Gemini 連線異常: {err}"
            print(f"[Gemini Exception] {model_id}: {err}", flush=True)

    return "", last_err


def generate_reona_reply(
    sys_prompt: str,
    user_prompt: str,
    max_tokens: int = 90,
    max_attempts: int = 3,
    strip_trailing_question: bool = False,
) -> tuple[str, str]:
    last_err = ""
    retry_delays = [4.0, 9.0]
    for attempt in range(max_attempts):
        reply, err_reason = call_groq_transparent(
            sys_prompt, user_prompt, max_tokens, strip_trailing_question
        )
        if reply:
            return reply, ""

        if GEMINI_API_KEY:
            g_reply, g_err = call_gemini_chat(
                sys_prompt, user_prompt, max_tokens, strip_trailing_question
            )
            if g_reply:
                return g_reply, ""
            last_err = f"{err_reason} | {g_err}"
        else:
            last_err = err_reason

        if attempt < max_attempts - 1:
            wait_sec = retry_delays[min(attempt, len(retry_delays) - 1)]
            print(
                f"[Retry Wait] 雙引擎暫忙，背景等待 {wait_sec:.1f}s 後進行第 {attempt+2} 輪重試...",
                flush=True,
            )
            time.sleep(wait_sec)

    return "", last_err


# ==================== 情境語音觸發（含防連刷冷卻）與多情緒聲線 ====================
def analyze_voice_context(user_text: str, reply_text: str, now_ts: float) -> tuple[bool, str]:
    u_low = (user_text or "").lower()
    combined = f"{user_text} {reply_text}"

    explicit_req = any(k in u_low for k in VOICE_REQUEST_KEYWORDS)

    if any(k in combined for k in ("胃", "肚子", "腹痛", "反酸", "頭暈", "軟綿", "好累", "痠", "絞痛", "抽痛")):
        mood = "coax_soft"
        category = "sick"
    elif any(k in combined for k in ("來陪我", "陪我", "好冷", "抱", "討拍")):
        mood = "coax_soft"
        category = "context"
    elif any(k in combined for k in ("少來", "哈哈", "嘴這麼甜", "笨蛋", "哼", "想吃", "美食", "好吃", "麻糬")):
        mood = "playful"
        category = "context"
    elif any(k in combined for k in ("晚安", "早安", "快去睡", "好好吃飯", "多喝水", "乖", "小心", "心疼")):
        mood = "gentle"
        category = "context"
    else:
        mood = "normal"
        category = "base"

    if explicit_req:
        return True, mood

    if (now_ts - last_voice_ts) < VOICE_COOLDOWN_SEC:
        return False, mood

    if category == "sick":
        return (random.random() < VOICE_PROB_SICK), mood
    if category == "context":
        return (random.random() < VOICE_PROB_CONTEXTUAL), mood
    return (random.random() < VOICE_PROB_BASE), "normal"


def format_tts_prosody(text: str, mood: str) -> tuple[str, float]:
    clean = re.sub(r"https?://\S+", "", text).strip()
    if not clean:
        return "", 1.0

    segments = [s.strip() for s in clean.split(" ") if s.strip()]

    if mood == "coax_soft":
        prosody_text = "…… ".join(segments) if len(segments) <= 2 else "，".join(segments)
        if not prosody_text.endswith(("～", "…", "？", "！")):
            prosody_text += "……"
        return prosody_text, 0.92

    if mood == "playful":
        prosody_text = "，".join(segments)
        if not prosody_text.endswith(("～", "！", "？")):
            prosody_text += "！"
        return prosody_text, 1.05

    if mood == "gentle":
        prosody_text = "，".join(segments)
        if not prosody_text.endswith(("～", "。", "？")):
            prosody_text += "～"
        return prosody_text, 0.95

    prosody_text = "，".join(segments)
    return prosody_text, 0.98


def try_synthesize_voice(text: str, now_dt: datetime, mood: str = "normal") -> bytes | None:
    global last_voice_ts
    if not FISH_API_KEY or not FISH_MODEL_ID:
        return None
    tts_text, speech_speed = format_tts_prosody(text, mood)
    char_len = len(re.sub(r"\s+", "", tts_text))
    if char_len == 0 or char_len > 65:
        print(f"[Voice Skip] 字數不符 ({char_len} 字)", flush=True)
        return None

    today_str = now_dt.strftime("%Y-%m-%d")
    month_str = now_dt.strftime("%Y-%m")
    vu = memory.setdefault(
        "voice_usage",
        {"date": today_str, "daily_chars": 0, "month": month_str, "monthly_chars": 0},
    )
    if vu.get("date") != today_str:
        vu["date"] = today_str
        vu["daily_chars"] = 0
    if vu.get("month") != month_str:
        vu["month"] = month_str
        vu["monthly_chars"] = 0

    if vu["daily_chars"] + char_len > DAILY_VOICE_CHAR_LIMIT:
        print(f"[Voice Skip] 今日語音額度已達上限 ({vu['daily_chars']}/{DAILY_VOICE_CHAR_LIMIT})", flush=True)
        return None
    if vu["monthly_chars"] + char_len > MONTHLY_VOICE_CHAR_LIMIT:
        print(f"[Voice Skip] 本月語音額度已達上限 ({vu['monthly_chars']}/{MONTHLY_VOICE_CHAR_LIMIT})", flush=True)
        return None

    try:
        resp = requests.post(
            "https://api.fish.audio/v1/tts",
            headers={
                "Authorization": f"Bearer {FISH_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "text": tts_text,
                "reference_id": FISH_MODEL_ID,
                "format": "mp3",
                "prosody": {
                    "speed": speech_speed,
                    "volume": 0,
                },
            },
            timeout=15,
        )
        if resp.status_code == 200 and resp.content:
            vu["daily_chars"] += char_len
            vu["monthly_chars"] += char_len
            last_voice_ts = time.time()
            save_memory(memory)
            print(
                f"[Voice OK] mood={mood} | speed={speech_speed} | "
                f"daily={vu['daily_chars']}/{DAILY_VOICE_CHAR_LIMIT} | text={tts_text}",
                flush=True,
            )
            return resp.content
        else:
            print(f"[Voice Fail] HTTP {resp.status_code}: {resp.text[:120]}", flush=True)
    except Exception as e:
        print(f"[Voice Warn] {e}", flush=True)
    return None


def wake_render_server() -> None:
    if not RENDER_BASE_URL:
        return
    try:
        requests.get(RENDER_BASE_URL, timeout=6)
    except Exception:
        pass


# ==================== Discord Bot 主體 ====================
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def hydrate_memory_from_channel(channel: discord.abc.Messageable) -> None:
    if memory.get("recent_turns"):
        print(f"[Memory Loaded] 已載入 {len(memory['recent_turns'])} 筆歷史記憶", flush=True)
        return
    try:
        fetched = []
        async for msg in channel.history(limit=10):
            txt = (msg.content or "").strip()
            if not txt or txt.startswith("（系統狀態：") or is_gibberish_reply(txt):
                continue
            role = "Reona" if msg.author.id == client.user.id else "Nick"
            clean_txt = re.sub(r"\s+", " ", txt)[:60]
            fetched.append(f"{role}:{clean_txt}")
        if fetched:
            fetched.reverse()
            memory["recent_turns"] = fetched[-HISTORY_LIMIT:]
            save_memory(memory)
            print(f"[Memory Hydrated] 從 DC 頻道回補了 {len(memory['recent_turns'])} 筆歷史記憶", flush=True)
    except Exception as e:
        print(f"[Memory Hydrate Warn] {e}", flush=True)


def schedule_next_proactive(now_ts: float) -> float:
    return now_ts + random.uniform(PROACTIVE_MIN_GAP_SEC, PROACTIVE_MAX_GAP_SEC)


@tasks.loop(minutes=1)
async def proactive_loop():
    global next_proactive_ts
    if not TARGET_CHANNEL_ID or not client.is_ready():
        return

    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = time.time()

    # 深夜 01:00～08:00 睡眠靜音，絕對不主動傳訊打擾睡覺
    if 1 <= now_dt.hour < 8:
        return

    if next_proactive_ts == 0.0:
        if now_dt.hour == 8 and now_dt.minute < 30 and memory.get("morning_greet_date") != now_dt.strftime("%Y-%m-%d"):
            next_proactive_ts = now_ts + random.uniform(30, 300)
        else:
            next_proactive_ts = schedule_next_proactive(now_ts)
        return

    if now_ts < next_proactive_ts:
        return

    if _pending_messages or _in_restroom_session or _api_lock.locked() or (now_ts - last_user_msg_ts < 720):
        next_proactive_ts = now_ts + random.uniform(900, 1500)
        return

    next_proactive_ts = schedule_next_proactive(now_ts)

    channel = client.get_channel(TARGET_CHANNEL_ID)
    if channel is None:
        try:
            channel = await client.fetch_channel(TARGET_CHANNEL_ID)
        except Exception:
            return

    today_str = now_dt.strftime("%Y-%m-%d")
    is_morning_first = (
        8 <= now_dt.hour <= 9
        and memory.get("morning_greet_date") != today_str
    )

    cur_act = get_active_current_action()
    allow_q = False

    if is_morning_first:
        memory["morning_greet_date"] = today_str
        set_reona_location("客廳餐桌旁", now_ts)
        add_daily_event_log("早上起床吃熱早餐餵麻糬", now_dt)
        nick_loc = memory.get("nick_location") or ""
        if "留宿" in nick_loc or "Reona家" in nick_loc:
            proactive_hint = "主動跟Nick道早安，說你剛醒在餐桌旁準備餵麻糬。"
        else:
            proactive_hint = "主動傳早安給Nick，自然分享你剛起床準備吃熱早餐餵麻糬。"
    elif cur_act:
        proactive_hint = f"延續剛才正在做的「{cur_act}」，跟Nick自然分享進展或撒嬌兩句。"
    else:
        sync_daily_containers(now_dt)
        event_str = ensure_daily_event(now_dt)
        agreed = (memory.get("choice_state", {}).get("agreed_plan") or "").strip()
        roll = random.random()
        if roll < 0.20:
            set_reona_gi_state("mild", now_ts, now_dt=now_dt)
            _, cause = get_active_gi_info(now_ts)
            proactive_hint = f"因為「{cause}」有點胃悶，跟Nick撒嬌說你正準備喝溫水吃常備藥。"
        elif roll < 0.26:
            set_reona_gi_state("moderate", now_ts, now_dt=now_dt)
            _, cause = get_active_gi_info(now_ts)
            proactive_hint = f"因為「{cause}」肚子抽痛不舒服，想叫Nick陪你聊兩句。"
        elif agreed and roll < 0.50:
            proactive_hint = f"延續跟Nick約好的「{agreed}」，自然分享現在的情況。"
        elif event_str and roll < 0.70:
            proactive_hint = f"跟Nick分享今天遇到的小插曲「{event_str}」。"
        elif roll < 0.85:
            proactive_hint = "自然跟Nick分享你現在正在畫畫、看麻糬或出門散步的日常。"
        else:
            allow_q = True
            proactive_hint = "偶爾想找Nick一起決定事情，自然問他一個生活小問題。"

    sys_prompt = build_prompt(now_dt, allow_question=allow_q)
    async with _api_lock:
        reply, _ = await asyncio.to_thread(
            generate_reona_reply, sys_prompt, proactive_hint, 85, 1, not allow_q
        )
    if reply:
        track_reona_choice_question(reply, now_dt)
        capture_reona_task_request(reply, now_dt)
        update_current_action_from_reply(reply, now_dt)
        append_turn("Reona", reply)
        await channel.send(reply)


async def process_batched_messages(channel: discord.abc.Messageable) -> None:
    global _pending_messages, last_user_msg_ts, _in_restroom_session
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return

    if not _pending_messages:
        return

    batch = _pending_messages[:]
    _pending_messages.clear()

    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = time.time()
    last_user_msg_ts = now_ts

    # 逐句掃描連發短訊，確保每句動作或核銷都不漏接，並用逗號自然串接
    for item in batch:
        capture_user_context(item, now_dt)
    combined_text = "，".join(batch)

    # 若 Nick 查詢洗手間次數，直接本地回覆（0 LLM Token）
    is_restroom_query = (
        any(k in combined_text for k in RESTROOM_QUERY_KEYWORDS)
        or (("洗手間" in combined_text or "廁所" in combined_text) and ("幾次" in combined_text or "次數" in combined_text))
    )
    if is_restroom_query:
        stat_reply = format_restroom_summary(now_dt)
        append_turn("Nick", combined_text)
        append_turn("Reona", stat_reply)
        await channel.send(stat_reply)
        return

    append_turn("Nick", combined_text)

    wants_video = any(k in combined_text.lower() for k in VIDEO_KEYWORDS)
    if wants_video and RENDER_BASE_URL:
        asyncio.create_task(asyncio.to_thread(wake_render_server))

    # 依腸胃嚴重程度決定去洗手間行為（已完全取消自動升級，並保留去之前的位置與動作）
    severity = detect_gi_severity(combined_text, now_dt)
    returned_from_restroom = False
    loc_before_restroom = get_reona_location(now_dt, severity)

    if not _in_restroom_session and not wants_video and can_trigger_restroom(severity, now_dt, now_ts):
        _in_restroom_session = True
        stats = record_restroom_visit(severity, now_dt, now_ts)
        teaser = random.choice(RESTROOM_TEASERS_BY_LEVEL[severity])
        away_sec = random.uniform(RESTROOM_MIN_SEC, RESTROOM_MAX_SEC)
        _, active_cause = get_active_gi_info(now_ts)
        print(
            f"[Restroom Trip #{stats['daily_count']} today / total {stats['total_count']}] "
            f"severity={severity} | cause={active_cause or 'none'} | away={away_sec:.0f}s -> {teaser}",
            flush=True,
        )
        await channel.send(teaser)
        append_turn("Reona", teaser)

        mid_ping_prob = RESTROOM_MID_PING_PROB_BY_LEVEL.get(severity, 0.35)
        if random.random() < mid_ping_prob:
            first_half = away_sec * random.uniform(0.42, 0.58)
            second_half = max(15.0, away_sec - first_half)
            await asyncio.sleep(first_half)
            in_toilet_msg = random.choice(RESTROOM_IN_PROGRESS_PINGS[severity])
            print(f"[Restroom In-Progress Ping] level={severity} -> {in_toilet_msg}", flush=True)
            await channel.send(in_toilet_msg)
            append_turn("Reona", in_toilet_msg)
            await asyncio.sleep(second_half)
        else:
            await asyncio.sleep(away_sec)

        if _pending_messages:
            extra_batch = _pending_messages[:]
            _pending_messages.clear()
            for item in extra_batch:
                capture_user_context(item, datetime.now(SYDNEY_TZ))
            extra_text = "，".join(extra_batch)
            combined_text = f"{combined_text}，{extra_text}"
            append_turn("Nick", extra_text)
        returned_from_restroom = True
        _in_restroom_session = False
        set_reona_location(loc_before_restroom, time.time())

    now_dt = datetime.now(SYDNEY_TZ)

    # 對話輪次計數器硬控：每 4 輪才允許問 1 次，其餘 3 輪不問問題
    turn_num = int(memory.get("turn_counter", 0)) + 1
    memory["turn_counter"] = turn_num
    allow_question = (turn_num % 4 == 0)

    sys_prompt = build_prompt(now_dt, allow_question=allow_question)
    cur_loc = get_reona_location(now_dt, severity)
    cur_act = get_active_current_action()

    act_hint = f"（正接著做「{cur_act}」）" if cur_act else ""
    if returned_from_restroom:
        user_prompt = f"你剛從洗手間回到{cur_loc}{act_hint}，Nick說：「{combined_text}」。自然接話回覆他。"
    else:
        user_prompt = f"你在{cur_loc}{act_hint}，Nick說：「{combined_text}」。自然接話回覆他。"

    async with _api_lock:
        async with channel.typing():
            reply, err_reason = await asyncio.to_thread(
                generate_reona_reply, sys_prompt, user_prompt, 90, 3, not allow_question
            )

    if not reply:
        print(f"[Engine Busy Log] {err_reason}", flush=True)
        fallback_lines = [
            "等我一下下 剛在發呆沒看清",
            "稍等我幾秒 剛剛訊息卡了一下",
            "等我一下喔 剛在整理東西手忙腳亂的",
        ]
        reply = random.choice(fallback_lines)

    track_reona_choice_question(reply, now_dt)
    capture_reona_task_request(reply, now_dt)
    update_current_action_from_reply(reply, now_dt)
    append_turn("Reona", reply)

    if wants_video and RENDER_BASE_URL:
        call_url = f"{RENDER_BASE_URL}/?mode=call"
        await channel.send(f"{reply}\n來 我開好視訊了直接點進來：{call_url}")
        return

    should_voice, voice_mood = analyze_voice_context(combined_text, reply, time.time())
    voice_bytes = None
    if should_voice:
        voice_bytes = await asyncio.to_thread(try_synthesize_voice, reply, now_dt, voice_mood)

    if voice_bytes:
        audio_file = discord.File(io.BytesIO(voice_bytes), filename="reona_voice.mp3")
        await channel.send(content=reply, file=audio_file)
    else:
        await channel.send(reply)


@client.event
async def on_ready():
    global next_proactive_ts
    discover_groq_models()
    discover_gemini_models()
    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = time.time()
    if now_dt.hour == 8 and now_dt.minute < 30 and memory.get("morning_greet_date") != now_dt.strftime("%Y-%m-%d"):
        next_proactive_ts = now_ts + random.uniform(30, 300)
    else:
        next_proactive_ts = schedule_next_proactive(now_ts)
    print(
        f"[Reona Ready] User={client.user} | Channel={TARGET_CHANNEL_ID} | "
        f"ResetToken={memory.get('reset_token')} | GroqKeys={len(GROQ_KEYS)} | "
        f"Gemini={'ON' if GEMINI_API_KEY else 'OFF'} | GroqModels={ACTIVE_MODELS}",
        flush=True,
    )
    if TARGET_CHANNEL_ID:
        ch = client.get_channel(TARGET_CHANNEL_ID)
        if ch is None:
            try:
                ch = await client.fetch_channel(TARGET_CHANNEL_ID)
            except Exception:
                ch = None
        if ch is not None:
            await hydrate_memory_from_channel(ch)

    if not proactive_loop.is_running():
        proactive_loop.start()


@client.event
async def on_message(message: discord.Message):
    global _debounce_task, last_user_msg_ts
    if message.author.bot:
        return
    if TARGET_CHANNEL_ID and message.channel.id != TARGET_CHANNEL_ID:
        return

    user_text = (message.content or "").strip()
    if not user_text and message.attachments:
        user_text = "[傳了一張照片給你看]"
    elif not user_text and message.stickers:
        user_text = "[傳了一個貼圖]"
    if not user_text:
        return

    last_user_msg_ts = time.time()
    _pending_messages.append(user_text)

    if _in_restroom_session:
        return

    if _debounce_task and not _debounce_task.done():
        _debounce_task.cancel()
    _debounce_task = asyncio.create_task(process_batched_messages(message.channel))


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("缺少 DISCORD_BOT_TOKEN 或 DISCORD_TOKEN 環境變數")
    client.run(DISCORD_TOKEN)
