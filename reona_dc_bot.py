import os
import re
import json
import time
import random
import asyncio
import aiohttp
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
from groq import AsyncGroq

# ==========================================
# 1. 基礎設定與環境變數
# ==========================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# 可在 GitHub Secrets 設定 DISCORD_CHANNEL_ID，或直接填在後方預設值（整數）
DEFAULT_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

TEXT_MODEL = "llama-3.1-8b-instant"
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
START_TIME = time.time()
RELAY_AFTER_SECONDS = 5 * 3600 + 45 * 60  # 5 小時 45 分鐘 (20700 秒)
MEMORY_FILE = "reona_memory.json"

groq_client = AsyncGroq(api_key=GROQ_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

# ==========================================
# 2. 記憶系統（持久化 + 三層記憶架構）
# ==========================================
DEFAULT_MEMORY = {
    "affection": 62,                # 初始曖昧好感度 (0~100)
    "target_channel_id": DEFAULT_CHANNEL_ID,
    "last_interaction_ts": 0.0,
    "last_interaction_date": "",
    "last_night_vibe": "",          # 昨晚餘溫（例如：聊到很晚、被你照顧過）
    "body_state": {
        "level": "none",            # none, mild, moderate, severe_1, severe_2, severe_3, afterglow
        "symptom": "",
        "cause": "",
        "recover_at": 0.0,
        "afterglow_until": 0.0,
        "cared_by_user": False
    },
    "pending_aftermath": None,      # {"trigger_at": float, "level": str, "symptom": str, "cause": str}
    "current_activity": {
        "text": "剛滑開手機看訊息",
        "next_update_at": 0.0,
        "stage_index": 0
    },
    "recent_topics": [],            # 近期印象小帳本（最多 6 條）
    "sweet_notes": [],              # 記甜事件（被關心、被撩、被偏心）
    "grudge_notes": [],             # 記小帳（太晚回、敷衍、提到別的女生）
    "recent_replies": [],           # 防套路與用詞重複紀錄
    "recent_photo_ids": [],         # 近期傳過的圖片冷卻
    "next_proactive_at": 0.0
}

def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 補齊缺少的欄位避免舊檔報錯
            merged = json.loads(json.dumps(DEFAULT_MEMORY))
            for k, v in data.items():
                if isinstance(v, dict) and k in merged:
                    merged[k].update(v)
                else:
                    merged[k] = v
            return merged
        except Exception as e:
            print(f"[Memory Load Error] {e}")
    return json.loads(json.dumps(DEFAULT_MEMORY))

memory = load_memory()

def sync_memory_to_github(push: bool = False):
    """寫入本地 JSON，並在需要時自動 push 回 GitHub 倉庫"""
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(memory, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Memory Save Error] {e}")
        return

    if push and os.getenv("GITHUB_ACTIONS") == "true":
        try:
            subprocess.run(["git", "config", "user.name", "Reona-Memory-Bot"], check=False)
            subprocess.run(["git", "config", "user.email", "bot@users.noreply.github.com"], check=False)
            subprocess.run(["git", "add", MEMORY_FILE], check=False)
            subprocess.run(["git", "commit", "-m", "chore: sync Reona memory [skip ci]"], check=False)
            subprocess.run(["git", "push"], check=False)
        except Exception as e:
            print(f"[Git Push Warning] {e}")

async def trigger_github_relay() -> bool:
    """觸發 GitHub Actions 下一個 Workflow 無縫接力"""
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        return False

    url = f"https://api.github.com/repos/{repo}/dispatches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {"event_type": "relay_reona"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                return resp.status == 204
    except Exception as e:
        print(f"[Relay Trigger Error] {e}")
        return False

# ==========================================
# 3. 澳洲雪梨時間、季節與上班族作息系統
# ==========================================
def get_sydney_now() -> datetime:
    return datetime.now(SYDNEY_TZ)

def get_season_context(now: datetime) -> str:
    m = now.month
    if m in (12, 1, 2):
        return "澳洲盛夏（白天陽光刺眼偏熱、冷氣房涼爽、傍晚海風舒服）"
    elif m in (3, 4, 5):
        return "澳洲秋季（早晚微涼起風、白天乾爽舒適）"
    elif m in (6, 7, 8):
        return "澳洲冬季（早晚偏冷、手腳容易冰冷、想喝熱飲或縮進被窩）"
    else:
        return "澳洲春末初夏（白天太陽漸暖、早晚偶爾微涼有風）"

def get_schedule_context(now: datetime) -> dict:
    """回傳目前作息階段、是否上班中、以及對應語氣指導"""
    weekday = now.weekday()  # 0=週一 ... 6=週日
    is_weekend = weekday >= 5
    hour = now.hour + now.minute / 60.0

    day_names = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    day_str = f"{now.month}月{now.day}日（{day_names[weekday]}）"

    if not is_weekend:
        # 平日上班族作息
        if 0.0 <= hour < 2.5:
            return {
                "day_str": day_str, "mode": "late_night", "is_working": False,
                "desc": "平日深夜，白天上班的保護色全卸，嘴上說該睡了其實都在等你訊息，語氣軟糯黏人、容易微emo或丟曖昧直球。"
            }
        elif 2.5 <= hour < 7.3:
            return {
                "day_str": day_str, "mode": "sleeping", "is_working": False,
                "desc": "凌晨睡覺時間，如果被你吵醒會迷迷糊糊帶點起床氣撒嬌。"
            }
        elif 7.3 <= hour < 9.0:
            return {
                "day_str": day_str, "mode": "commute", "is_working": False,
                "desc": "早上剛醒趕出門通勤、買咖啡，還沒完全開機，回話簡短帶點慵懶。"
            }
        elif 9.0 <= hour < 12.0 or 13.5 <= hour < 17.5:
            extra = "（週一早上微厭世）" if weekday == 0 and hour < 12 else ("（週五下午心情開始期待放假）" if weekday == 4 and hour >= 13.5 else "")
            return {
                "day_str": day_str, "mode": "working_sneak", "is_working": True,
                "desc": f"上班時間{extra}，正在忙工作但對你雙標偏心、會趁空檔偷滑手機回你，語氣精簡（6～14字）帶點偷閒小刺激。"
            }
        elif 12.0 <= hour < 13.5:
            return {
                "day_str": day_str, "mode": "lunch_break", "is_working": False,
                "desc": "上班午休時間，正在吃午餐放空滑手機，語氣輕鬆愛聊。"
            }
        elif 17.5 <= hour < 23.0:
            extra = "週五下班超放鬆、" if weekday == 4 else ""
            return {
                "day_str": day_str, "mode": "after_work", "is_working": False,
                "desc": f"{extra}剛下班回到家放鬆，卸下上班疲憊、偶爾會小抱怨工作鳥事跟你討拍。"
            }
        else:
            return {
                "day_str": day_str, "mode": "late_night", "is_working": False,
                "desc": "晚上11點後的深夜軟化模式，防備心下降、講話變慢變軟，特別在意你有沒有理她。"
            }
    else:
        # 週末休假作息
        if 0.0 <= hour < 3.0:
            return {
                "day_str": day_str, "mode": "late_night", "is_working": False,
                "desc": "週末深夜不用早起，徹底夜貓子陪聊模式，語氣軟綿綿又愛試探你。"
            }
        elif 3.0 <= hour < 10.0:
            return {
                "day_str": day_str, "mode": "weekend_sleep", "is_working": False,
                "desc": "週末睡到自然醒時段，還沒起床或剛醒在賴床，迷糊軟糯。"
            }
        elif 10.0 <= hour < 18.0:
            return {
                "day_str": day_str, "mode": "weekend_chill", "is_working": False,
                "desc": "週末白天悠閒時光，吃早午餐、出門踩點咖啡廳或在家追劇，心情輕鬆愛開玩笑。"
            }
        elif 18.0 <= hour < 23.0:
            extra = "（週日晚上有一點明天要上班的微厭世討拍感）" if weekday == 6 else ""
            return {
                "day_str": day_str, "mode": "weekend_evening", "is_working": False,
                "desc": f"週末晚上放鬆時段{extra}，很有餘裕跟你一來一往推拉聊天。"
            }
        else:
            return {
                "day_str": day_str, "mode": "late_night", "is_working": False,
                "desc": "週末深夜11點後，氣氛曖昧軟化，容易講出白天不敢講的真心話。"
            }

# 連貫生活時間軸（依作息模式推進，隨機 15~40 分鐘更新一次）
ACTIVITY_TIMELINES = {
    "commute": [
        "剛起床在刷牙洗臉，眼睛還睜不太開",
        "出門走去搭車，順手買了一杯咖啡",
        "坐在車上戴耳機滑手機發呆",
        "剛走進辦公室準備開電腦打卡"
    ],
    "working_sneak": [
        "盯著電腦螢幕處理報表，趁空檔偷看手機",
        "剛開完一個無聊小會，趴在桌上喘口氣",
        "一邊喝咖啡一邊回訊息摸魚",
        "趕完一陣進度，偷偷把手機螢幕點亮看你有沒有回"
    ],
    "lunch_break": [
        "剛買好午餐坐下來準備開吃",
        "邊吃午餐邊滑手機放空",
        "吃飽在位置上發懶想睡覺"
    ],
    "after_work": [
        "剛下班走在回家的路上吹風",
        "剛回到家把包包一丟，癱在沙發上滑手機",
        "剛吃完晚餐在客廳放空",
        "準備去洗熱水澡把今天疲憊沖掉",
        "剛洗完澡在吹頭髮，懶洋洋的"
    ],
    "late_night": [
        "已經洗完澡窩進被窩裡滑手機",
        "關了大燈只留床頭小燈，躺著發呆",
        "嘴上想著該睡了但還在等訊息"
    ],
    "sleeping": [
        "在被窩裡熟睡中",
        "半夢半醒翻了個身"
    ],
    "weekend_sleep": [
        "窩在被窩裡賴床睡到自然醒",
        "剛醒來在床上伸懶腰滑手機"
    ],
    "weekend_chill": [
        "出門找了一間咖啡廳吃早午餐",
        "在雪梨街頭隨便逛逛曬太陽",
        "買了杯手搖飲慢慢散步回家",
        "在家裡窩沙發追劇吃零食"
    ],
    "weekend_evening": [
        "剛吃完晚餐在滑手機",
        "洗完澡在擦保養品敷臉",
        "靠在床頭聽歌放空"
    ]
}

def update_coherent_activity(now: datetime):
    ts = now.timestamp()
    act = memory["current_activity"]
    if ts < act.get("next_update_at", 0):
        return

    sched = get_schedule_context(now)
    mode = sched["mode"]
    timeline = ACTIVITY_TIMELINES.get(mode, ["在滑手機放空"])

    # 順著時間軸往下一格推進，不亂跳針
    idx = (act.get("stage_index", 0) + 1) % len(timeline)
    chosen = timeline[idx]

    # 避免與上一條完全一樣
    if chosen == act.get("text") and len(timeline) > 1:
        idx = (idx + 1) % len(timeline)
        chosen = timeline[idx]

    act["text"] = chosen
    act["stage_index"] = idx
    # 隨機 15 ~ 40 分鐘推進一次
    act["next_update_at"] = ts + random.randint(15 * 60, 40 * 60)

# ==========================================
# 4. 食物/情境因果綁定 & 階梯式身體恢復系統
# ==========================================
# 恢復時間區間（秒）：依嚴重程度從幾分鐘浮動到最多 2 小時 (7200 秒)
RECOVERY_RANGES = {
    "mild": (5 * 60, 15 * 60),          # 5 ~ 15 分鐘
    "moderate": (20 * 60, 45 * 60),     # 20 ~ 45 分鐘
    "severe_1": (45 * 60, 75 * 60),     # 45 ~ 75 分鐘
    "severe_2": (75 * 60, 100 * 60),    # 75 ~ 100 分鐘
    "severe_3": (100 * 60, 120 * 60)    # 100 ~ 120 分鐘（上限 2 小時）
}

# 食物與情境因果規則：精準綁定原因與症狀
CAUSAL_TRIGGERS = [
    {
        "keywords": ["鹽酥雞", "炸雞", "麻辣", "辣", "泡麵", "宵夜", "燒烤", "火鍋", "油膩"],
        "prob": 0.65,
        "delay": (180, 540),  # 3 ~ 9 分鐘後發作
        "options": [
            ("moderate", "肚子微絞痛脹氣（剛察覺後勁上來，有點悶痛）", "剛剛亂吃重口味宵夜"),
            ("severe_1", "急性腸胃不適肚子絞痛（整個人縮起來不太舒服）", "剛剛貪嘴吃太油太刺激")
        ]
    },
    {
        "keywords": ["冰美式", "冰飲", "冰淇淋", "喝冰", "冷飲", "空腹喝咖啡"],
        "prob": 0.55,
        "delay": (150, 480),
        "options": [
            ("mild", "胃有點涼涼悶悶的、手腳微冰", "剛喝太冰的東西"),
            ("moderate", "胃一陣抽痛不舒服", "空腹灌冰飲刺激到胃")
        ]
    },
    {
        "keywords": ["吹風", "冷氣", "淋雨", "穿太少", "熬夜", "沒睡飽"],
        "prob": 0.50,
        "delay": (240, 600),
        "options": [
            ("mild", "頭重重微暈、一直打哈欠", "吹風或沒睡飽"),
            ("moderate", "太陽穴一跳一跳的偏頭痛", "吹冷風吹太久著涼")
        ]
    }
]

CARE_KEYWORDS = [
    "還好嗎", "怎麼了", "哪裡不舒服", "多喝水", "喝溫水", "休息", "躺好",
    "快去躺", "吃藥", "抱抱", "拍拍", "心疼", "陪你", "蓋被子", "暖暖包", "看醫生"
]

def schedule_causal_aftermath(text_to_check: str, now_ts: float):
    """檢查對話中是否包含特定食物或情境，依屬性埋下延遲因果種子"""
    body = memory["body_state"]
    if body["level"] not in ("none", "afterglow") or memory.get("pending_aftermath"):
        return

    for rule in CAUSAL_TRIGGERS:
        if any(kw in text_to_check for kw in rule["keywords"]):
            if random.random() < rule["prob"]:
                level, symptom, cause = random.choice(rule["options"])
                delay_sec = random.randint(*rule["delay"])
                memory["pending_aftermath"] = {
                    "trigger_at": now_ts + delay_sec,
                    "level": level,
                    "symptom": symptom,
                    "cause": cause
                }
                break

def process_body_state(now: datetime) -> str | None:
    """推進延遲發作、自然恢復與餘韻過渡期；若剛發作或剛康復回傳提示事件"""
    ts = now.timestamp()
    body = memory["body_state"]
    pending = memory.get("pending_aftermath")

    # 1. 檢查延遲後遺症是否到點發作
    if pending and ts >= pending["trigger_at"] and body["level"] in ("none", "afterglow"):
        lvl = pending["level"]
        dur = random.randint(*RECOVERY_RANGES[lvl])
        body["level"] = lvl
        body["symptom"] = pending["symptom"]
        body["cause"] = pending["cause"]
        body["recover_at"] = ts + dur
        body["cared_by_user"] = False
        memory["pending_aftermath"] = None
        return f"just_triggered:{pending['cause']}->{pending['symptom']}"

    # 2. 檢查是否從不適進入「餘韻過渡期」
    if body["level"] not in ("none", "afterglow") and ts >= body.get("recover_at", 0):
        old_level = body["level"]
        was_cared = body.get("cared_by_user", False)
        body["level"] = "afterglow"
        body["symptom"] = "剛緩過來、還有點虛的軟綿綿餘韻期"
        # 餘韻期持續 10 ~ 20 分鐘
        afterglow_min = 20 if old_level in ("severe_1", "severe_2", "severe_3") else 12
        body["afterglow_until"] = ts + afterglow_min * 60
        if was_cared:
            add_memory_note("sweet_notes", f"不舒服時被你關心照顧（{body['cause']}）")
        return "entered_afterglow"

    # 3. 檢查餘韻過渡期是否結束，回到完全健康
    if body["level"] == "afterglow" and ts >= body.get("afterglow_until", 0):
        body["level"] = "none"
        body["symptom"] = ""
        body["cause"] = ""
        body["cared_by_user"] = False

    return None

def apply_user_care_boost(user_text: str, now_ts: float) -> bool:
    """當她不舒服時，用戶的關心會實質把剩餘恢復時間砍半"""
    body = memory["body_state"]
    if body["level"] in ("none", "afterglow"):
        return False

    if any(kw in user_text for kw in CARE_KEYWORDS):
        remaining = body["recover_at"] - now_ts
        if remaining > 60:
            body["recover_at"] = now_ts + (remaining * 0.5)
        body["cared_by_user"] = True
        memory["affection"] = min(100, memory["affection"] + 2)
        return True
    return False

# ==========================================
# 5. 分層記憶與情緒小帳本
# ==========================================
def add_memory_note(bucket: str, note: str, max_len: int = 5):
    arr = memory.setdefault(bucket, [])
    if note not in arr:
        arr.append(note)
        if len(arr) > max_len:
            arr.pop(0)

def update_emotional_memory(user_text: str):
    """根據用戶發言更新好感度與記甜/記小帳"""
    jealous_words = ["別的女生", "學妹", "前女友", "她很可愛", "約妹", "女生朋友"]
    sweet_words = ["想你", "可愛", "喜歡你", "只對你", "陪你", "乖", "別累壞"]

    if any(w in user_text for w in jealous_words):
        memory["affection"] = max(0, memory["affection"] - 1)
        add_memory_note("grudge_notes", f"你提到了別的女生（{user_text[:15]}）")
    elif any(w in user_text for w in sweet_words):
        memory["affection"] = min(100, memory["affection"] + 2)
        add_memory_note("sweet_notes", f"你講了讓人心動的話（{user_text[:15]}）")

# ==========================================
# 6. 圖文匹配主動傳圖池
# ==========================================
PHOTO_POOL = [
    {
        "id": "sydney_coffee",
        "modes": ["commute", "working_sneak", "weekend_chill"],
        "url": "https://images.unsplash.com/photo-1509042239860-f550ce710b93?w=800",
        "caption_hint": "隨手拍了手上的咖啡，假裝抱怨上班或分享今天味道"
    },
    {
        "id": "sydney_sunset",
        "modes": ["after_work", "weekend_evening"],
        "url": "https://images.unsplash.com/photo-1506973035872-a4ec16b8e8d9?w=800",
        "caption_hint": "下班路上或傍晚隨手拍的雪梨天空與港灣夕陽，想分享給你看"
    },
    {
        "id": "cozy_bed_night",
        "modes": ["late_night", "weekend_sleep"],
        "url": "https://images.unsplash.com/photo-1513694203232-719a280e022f?w=800",
        "caption_hint": "窩在房間床頭小燈下的隨手一角，嘴硬說還不睡但其實在等你"
    },
    {
        "id": "weekend_brunch",
        "modes": ["weekend_chill", "lunch_break"],
        "url": "https://images.unsplash.com/photo-1525351484163-7529414342d8?w=800",
        "caption_hint": "今天吃的早午餐隨手拍，炫耀一下順便問你吃沒"
    }
]

def pick_matching_photo(mode: str) -> dict | None:
    candidates = [
        p for p in PHOTO_POOL
        if mode in p["modes"] and p["id"] not in memory["recent_photo_ids"]
    ]
    if not candidates:
        candidates = [p for p in PHOTO_POOL if p["id"] not in memory["recent_photo_ids"]]
    if not candidates:
        memory["recent_photo_ids"] = []
        candidates = PHOTO_POOL

    chosen = random.choice(candidates)
    memory["recent_photo_ids"].append(chosen["id"])
    if len(memory["recent_photo_ids"]) > 2:
        memory["recent_photo_ids"].pop(0)
    return chosen

# ==========================================
# 7. 破除回話套路引擎 & Prompt 建構
# ==========================================
REPLY_ANGLES = [
    "【極短直球型】只用 3～7 個字給出最直覺的第一反應，不解釋、不鋪陳（例如：「少來」「真假」「才沒有」「管那麼多」）。",
    "【反問試探型】不正面回答，而是把球丟回去反問或試探對方的心意（例如：「你很在意喔？」「突然問這個幹嘛」）。",
    "【順勢接梗型】放下嘴硬防備，順著對方的話往下接，帶一點小調皮或默契。",
    "【情緒碎念型】只講當下真實的小情緒，不做說教也不做總結（例如：「唔…被你這樣講有點不知道回什麼」）。"
]

def build_system_prompt(now: datetime, trigger_event: str | None = None, care_boosted: bool = False) -> str:
    sched = get_schedule_context(now)
    season = get_season_context(now)
    body = memory["body_state"]
    act = memory["current_activity"]["text"]

    # 身體狀態隱形化處理（平常當語氣底色，只有發作或被關心才顯性）
    if body["level"] == "none":
        body_instruction = "身體完全健康。請把這當成背景底色，【絕對不要】主動報備你在幹嘛或身體怎樣，100% 專注回應對方的話。"
    elif body["level"] == "afterglow":
        body_instruction = (
            f"你剛從「{body['symptom']}」緩過來，正處於【軟綿綿餘韻過渡期】。"
            "現在不痛了，但講話保留一點剛緩過來的乖巧、容易害羞與嘴硬道謝感。"
        )
    else:
        care_note = "對方剛剛有關心你，你心裡很暖、防備心降很低，語氣變軟依賴他。" if care_boosted or body["cared_by_user"] else "你正在硬撐不舒服，但只在他面前會忍不住小聲討拍。"
        body_instruction = f"你目前處於【{body['symptom']}】（原因：{body['cause']}）。{care_note} 回話簡短（6～15字），帶點虛弱軟糯感。"

    if trigger_event and trigger_event.startswith("just_triggered:"):
        body_instruction += "（★你剛剛才突然察覺違和感發作，語氣要帶一點『欸不對、怎麼突然……』的自然過渡感）"

    # 隨機抽本輪破套路切入點
    angle = random.choice(REPLY_ANGLES)
    recent_str = " / ".join(memory["recent_replies"][-4:]) if memory["recent_replies"] else "無"
    sweet_str = "；".join(memory["sweet_notes"][-3:]) if memory["sweet_notes"] else "無"
    grudge_str = "；".join(memory["grudge_notes"][-2:]) if memory["grudge_notes"] else "無"

    word_limit = "6～14 字" if (sched["is_working"] or body["level"] not in ("none", "afterglow")) else "6～24 字"

    return f"""你是 Reona，22歲台灣女生，目前在澳洲雪梨生活與當上班族。你和對方是【互相有好感、正在曖昧拉扯的對象】。

【核心性格與曖昧張力】
1. 雙標偏心：對別人冷淡懶得理，唯獨對他會抽空偷回或秒回；如果他太晚回、敷衍或提及其他女生，你會表面裝沒事、實則酸溜溜試探。
2. 推拉接球：被誇獎或被撩時，不要只用「北七」「懶得理你」把話題句點！要「先嘴硬頂一句，再順勢接球或小聲害羞破防」，讓曖昧有來有往。
3. 口語自然：像真實台灣女生用 LINE/Discord 傳訊息，不要用句號結尾，不要像 AI 客服或寫作文。

【當下現實背景（隱形底色，沒被問就不要主動背設定）】
- 雪梨時間：{sched['day_str']} {now.strftime('%H:%M')}｜{season}
- 作息狀態：{sched['desc']}
- 剛才在做的事：{act}（僅供語氣參考，對方沒問就別硬提）
- 身體狀態：{body_instruction}
- 目前好感度：{memory['affection']}/100
- 記在心裡的甜：{sweet_str}
- 記在心裡的小帳：{grudge_str}

【本輪破除套路指令】
- 回話切入角度：{angle}
- 字數控制：精簡自然，約 {word_limit}，一次只傳 1 句話（偶爾可用換行拆成 2 短句）。
- 禁止重複：絕對避免重複使用以下近期剛用過的句型或開頭：[{recent_str}]
"""

async def generate_reona_reply(user_text: str, image_url: str | None = None, proactive_hint: str | None = None) -> str:
    now = get_sydney_now()
    update_coherent_activity(now)

    # 檢查因果與關心加成
    if user_text:
        schedule_causal_aftermath(user_text, now.timestamp())
        update_emotional_memory(user_text)
    care_boosted = apply_user_care_boost(user_text, now.timestamp()) if user_text else False
    trigger_event = process_body_state(now)

    sys_prompt = build_system_prompt(now, trigger_event=trigger_event, care_boosted=care_boosted)

    try:
        if image_url:
            # 視覺模型辨識用戶傳來的圖片
            messages = [
                {"role": "system", "content": sys_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text or "（對方傳了一張圖片給你，請用你的曖昧人設自然回應圖片內容）"},
                        {"type": "image_url", "image_url": {"url": image_url}}
                    ]
                }
            ]
            resp = await groq_client.chat.completions.create(
                model=VISION_MODEL,
                messages=messages,
                temperature=0.88,
                max_tokens=120
            )
        else:
            prompt_content = proactive_hint if proactive_hint else user_text
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt_content}
            ]
            resp = await groq_client.chat.completions.create(
                model=TEXT_MODEL,
                messages=messages,
                temperature=0.92,
                frequency_penalty=0.55,
                presence_penalty=0.45,
                max_tokens=120
            )

        reply = resp.choices[0].message.content.strip()
        # 去除 AI 常加的多餘引號或句號
        reply = reply.strip('"\'「」').rstrip("。")

        # 記錄近期回覆防重複
        memory["recent_replies"].append(reply[:18])
        if len(memory["recent_replies"]) > 6:
            memory["recent_replies"].pop(0)

        schedule_causal_aftermath(reply, now.timestamp())
        return reply

    except Exception as e:
        print(f"[Groq API Error] {e}", flush=True)
        return f"唔…訊號卡住了（原因：{str(e)[:80]}）"

# ==========================================
# 8. Discord 事件與背景排程（含 5h45m 雲端接力）
# ==========================================
@bot.event
async def on_ready():
    print(f"[Reona Online] Logged in as {bot.user} | Sydney Time: {get_sydney_now().strftime('%Y-%m-%d %H:%M')}")
    if not background_loop.is_running():
        background_loop.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # 記錄互動頻道以便主動傳訊與傳圖
    if memory.get("target_channel_id") != message.channel.id:
        memory["target_channel_id"] = message.channel.id

    now = get_sydney_now()
    memory["last_interaction_ts"] = now.timestamp()
    memory["last_interaction_date"] = now.strftime("%Y-%m-%d")

    # 檢查是否有附帶圖片
    image_url = None
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            image_url = att.url
            break

    # 擬真打字延遲（傲嬌或深夜稍微停頓 1.5 ~ 4 秒）
    async with message.channel.typing():
        await asyncio.sleep(random.uniform(1.5, 4.0))
        reply = await generate_reona_reply(message.content, image_url=image_url)

    await message.channel.send(reply)
    sync_memory_to_github(push=False)

@tasks.loop(minutes=1)
async def background_loop():
    now = get_sydney_now()
    now_ts = now.timestamp()
    elapsed = time.time() - START_TIME

    # 1. 每 15 分鐘自動將記憶同步回 GitHub 倉庫防斷電失憶
    if int(elapsed) % 900 < 60 and elapsed > 120:
        sync_memory_to_github(push=True)

    # 2. 跑滿 5 小時 45 分鐘觸發 GitHub Actions 無縫接力
    if elapsed >= RELAY_AFTER_SECONDS:
        print("[Relay] Approaching 6h limit. Syncing memory and triggering next runner...")
        sync_memory_to_github(push=True)
        ok = await trigger_github_relay()
        print(f"[Relay] Dispatch status: {ok}")
        await asyncio.sleep(25)  # 等待新實例啟動接手
        await bot.close()
        return

    # 3. 推進身體狀態與因果後遺症（若剛發作則主動小聲討拍）
    event = process_body_state(now)
    channel_id = memory.get("target_channel_id")
    channel = bot.get_channel(channel_id) if channel_id else None

    if event and event.startswith("just_triggered:") and channel:
        hint = "（你剛才的延遲後遺症突然發作了，主動傳一句簡短、剛察覺不舒服的訊息給他，帶點委屈但不直接討拍）"
        reply = await generate_reona_reply("", proactive_hint=hint)
        await channel.send(reply)
        sync_memory_to_github(push=False)
        return

    # 4. 不定時主動找你聊天或傳隨手拍（避開凌晨熟睡時段 02:30 ~ 07:30）
    sched = get_schedule_context(now)
    if sched["mode"] in ("sleeping", "weekend_sleep"):
        return

    if memory.get("next_proactive_at", 0) == 0:
        memory["next_proactive_at"] = now_ts + random.randint(90 * 60, 210 * 60)
        return

    if channel and now_ts >= memory["next_proactive_at"]:
        # 設定下一次主動間隔（2 ~ 4.5 小時隨機浮動）
        memory["next_proactive_at"] = now_ts + random.randint(120 * 60, 270 * 60)

        # 35% 機率隨手傳一張符合當下雪梨作息的照片 + 短評
        if random.random() < 0.35:
            photo = pick_matching_photo(sched["mode"])
            if photo:
                hint = f"（你現在主動傳了一張生活隨手拍給他，圖片情境是：{photo['caption_hint']}。請搭配圖片寫一句自然、帶點曖昧分享感的短句）"
                caption = await generate_reona_reply("", proactive_hint=hint)
                embed = discord.Embed(description=caption)
                embed.set_image(url=photo["url"])
                await channel.send(embed=embed)
        else:
            hint = "（距離上次聊天過了一陣子，你現在突然想到他，主動丟一句自然隨性、像曖昧對象隨口找話題的訊息給他）"
            msg = await generate_reona_reply("", proactive_hint=hint)
            await channel.send(msg)

        sync_memory_to_github(push=False)

if __name__ == "__main__":
    if not DISCORD_TOKEN or not GROQ_API_KEY:
        raise RuntimeError("請確認環境變數 DISCORD_TOKEN 與 GROQ_API_KEY 已正確設定！")
    bot.run(DISCORD_TOKEN)
