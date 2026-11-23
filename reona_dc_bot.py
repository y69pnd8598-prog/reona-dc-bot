import os
import re
import json
import time
import random
import asyncio
import subprocess
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests
import discord
from discord.ext import tasks

# =========================================================
# 1. Configuration
# =========================================================
DISCORD_BOT_TOKEN = (
    os.getenv("DISCORD_BOT_TOKEN")
    or os.getenv("DISCORD_TOKEN")
    or "在這裡貼上你的_DC_BOT_TOKEN"
).strip()

TARGET_CHANNEL_ID = int(
    (os.getenv("DISCORD_CHANNEL_ID") or "0").strip() or "0"
)

GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip()
FISH_API_KEY = (os.getenv("FISH_API_KEY") or "").strip()
FISH_MODEL_ID = (
    os.getenv("FISH_MODEL_ID") or "7f92f8afb8ec43bf81429cc1c9199cb1"
).strip()

_raw_render_url = (os.getenv("RENDER_BASE_URL") or "").strip().rstrip("/")
RENDER_BASE_URL = (
    _raw_render_url
    if _raw_render_url.startswith(("http://", "https://"))
    else "https://reona-cloud-companion.onrender.com"
)

DAILY_TTS_CHAR_LIMIT = 250
MONTHLY_TTS_CHAR_LIMIT = 8000

BASE_VOICE_PROB = 0.20
HIGH_VOICE_PROB = 0.55
PROACTIVE_VOICE_PROB = 0.05
BASE_IMAGE_PROB = 0.15
TOPIC_IMAGE_PROB = 0.40
IMAGE_COOLDOWN_SEC = 1200

# 使用 Groq 秒回純對話模型（不輸出冗長 <think> 思考段，避免耗盡 tokens 變空字串）
_env_model = (os.getenv("GROQ_TEXT_MODEL") or "").strip()
TEXT_MODELS = [m for m in dict.fromkeys([
    _env_model,
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]) if m]

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
WEEKDAY_NAMES_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
HISTORY_LIMIT = 14

BASE_DIR = Path(__file__).resolve().parent
MEMORY_FILE = BASE_DIR / "reona_memory.json"
AUDIO_DIR = BASE_DIR / "generated_audio"
IMAGES_DIR = BASE_DIR / "images"

AUDIO_DIR.mkdir(exist_ok=True)
IMAGES_DIR.mkdir(exist_ok=True)
for sub in ("cat_mochi", "cafe_food", "sydney_view", "sketch"):
    (IMAGES_DIR / sub).mkdir(exist_ok=True)

# =========================================================
# 2. Daily Main Quests & Life Events
# =========================================================
DAILY_QUEST_TEMPLATES = [
    "幫澳洲精品香氛品牌繪製秋季聯名包裝主視覺草稿，下午在工作室線上下對稿",
    "完成獨立出版書籍封面插畫細節精修，傍晚去雪梨港邊散步買鮮花布置畫室",
    "整理海外文創聯名打樣樣品與色彩校對，中午去常去的採光咖啡廳吃早午餐",
    "替高級甜點品牌設計節慶禮盒插畫，下午在家邊點木質香氛邊陪麻糬午睡",
    "把這週商業視覺專案定案交稿，晚上打算去海景餐廳吃頓好的犒賞自己",
    "為精品雜誌專欄畫雪梨生活隨筆插畫，午後在公寓大落地窗邊曬太陽構思配色",
    "跟海外策展團隊確認限量版畫輸出規格，忙完去逛市集挑幾件生活小物",
]

LIFE_MINOR_INCIDENTS = [
    "好煩喔 剛交出去的聯名插畫稿被品牌方臨時說想微調色系 又要重調一版",
    "哭啊 剛剛手滑把冰拿鐵打翻在畫室桌上 差點弄到平板 還好反應快擦掉了",
    "超扯 雪梨下午突然飄雨 我散步沒帶傘一路小跑回公寓 頭髮都微濕了",
    "麻糬剛剛跳上工作室邊桌把花瓶碰歪 水灑了一桌 現在正一臉無辜看我擦地板",
]

# =========================================================
# 3. Persistent Memory (自動讀取舊記憶＋即時存回 Git)
# =========================================================
DEFAULT_MEMORY = {
    "recent_turns": [],
    "long_term_notes": [],
    "menstrual_cycle": {
        "cycle_start_date": "",
        "cycle_length": 30,
    },
    "daily_main_quest": {
        "date_key": "",
        "quest": "",
        "progress": "早上剛開始準備",
    },
    "current_emotion": "happy",
    "emotion_until_ts": 0.0,
    "sick_until_ts": 0.0,
    "sick_type": "",
    "sick_reason": "",
    "need_recovery_report": False,
    "mild_sick_until_ts": 0.0,
    "mild_reason": "",
    "need_mild_recovery_report": False,
    "current_activity": "在雪梨海景公寓畫室裡畫插畫",
    "last_image_sent_ts": 0.0,
    "recent_sent_images": [],
    "last_proactive_ts": 0.0,
    "last_user_msg_ts": 0.0,
    "recent_proactive_msgs": [],
    "tts_quota": {
        "day_key": "",
        "day_chars": 0,
        "month_key": "",
        "month_chars": 0,
    },
}

_last_git_sync_ts = 0.0


def load_memory() -> dict:
    default_start = (datetime.now(SYDNEY_TZ).date() - timedelta(days=22)).isoformat()
    if not MEMORY_FILE.exists():
        mem = json.loads(json.dumps(DEFAULT_MEMORY))
        mem["menstrual_cycle"]["cycle_start_date"] = default_start
        return mem
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        merged = json.loads(json.dumps(DEFAULT_MEMORY))
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        # 相容舊版記憶檔欄位（如 history / notes）
        if not merged["recent_turns"] and isinstance(data.get("history"), list):
            merged["recent_turns"] = [str(x) for x in data["history"][-HISTORY_LIMIT:]]
        if not merged["long_term_notes"] and isinstance(data.get("notes"), list):
            merged["long_term_notes"] = [str(x) for x in data["notes"][-8:]]
        if not merged["menstrual_cycle"].get("cycle_start_date"):
            merged["menstrual_cycle"]["cycle_start_date"] = default_start
        return merged
    except Exception:
        mem = json.loads(json.dumps(DEFAULT_MEMORY))
        mem["menstrual_cycle"]["cycle_start_date"] = default_start
        return mem


def sync_memory_to_git_bg() -> None:
    """背景把最新記憶檔同步回 GitHub，確保重啟或下一棒接力都能讀到記憶。"""
    global _last_git_sync_ts
    now = time.time()
    if now - _last_git_sync_ts < 120:
        return
    _last_git_sync_ts = now
    try:
        if not (BASE_DIR / ".git").exists():
            return
        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"],
            cwd=BASE_DIR, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        subprocess.run(
            ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
            cwd=BASE_DIR, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        subprocess.run(
            ["git", "add", MEMORY_FILE.name],
            cwd=BASE_DIR, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        diff = subprocess.run(
            ["git", "diff", "--staged", "--quiet"],
            cwd=BASE_DIR, check=False
        )
        if diff.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", "chore: sync Reona memory"],
                cwd=BASE_DIR, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            subprocess.run(
                ["git", "push"],
                cwd=BASE_DIR, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
    except Exception:
        pass


def save_memory(mem: dict, push_git: bool = False) -> None:
    try:
        MEMORY_FILE.write_text(
            json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if push_git:
            asyncio.create_task(asyncio.to_thread(sync_memory_to_git_bg))
    except Exception as err:
        print(f"[Memory Save Error] {err}", flush=True)


memory = load_memory()


def ensure_daily_main_quest(now_dt: datetime) -> dict:
    today_key = now_dt.strftime("%Y-%m-%d")
    q_obj = memory.setdefault("daily_main_quest", {})
    if q_obj.get("date_key") != today_key or not q_obj.get("quest"):
        q_obj["date_key"] = today_key
        q_obj["quest"] = random.choice(DAILY_QUEST_TEMPLATES)
        q_obj["progress"] = "早上剛開始準備"
        save_memory(memory)

    hour = now_dt.hour
    if 6 <= hour < 12:
        q_obj["progress"] = "上午：剛起床喝咖啡、啟動今天主線"
    elif 12 <= hour < 18:
        q_obj["progress"] = "下午：推進主線草稿與細節，還沒吃晚餐"
    elif 18 <= hour < 23:
        q_obj["progress"] = "晚上：主線接近收尾或吃過晚餐，在公寓陪麻糬"
    else:
        q_obj["progress"] = "深夜：忙完躺床上準備休息"
    return q_obj


def sync_tts_quota_period(now_dt: datetime) -> dict:
    day_key = now_dt.strftime("%Y-%m-%d")
    month_key = now_dt.strftime("%Y-%m")
    q = memory.setdefault("tts_quota", {})
    changed = False
    if q.get("day_key") != day_key:
        q["day_key"] = day_key
        q["day_chars"] = 0
        changed = True
    if q.get("month_key") != month_key:
        q["month_key"] = month_key
        q["month_chars"] = 0
        changed = True
    if changed:
        save_memory(memory)
    return q


def get_tts_quota_info(now_dt: datetime) -> dict:
    q = sync_tts_quota_period(now_dt)
    day_used = int(q.get("day_chars", 0))
    month_used = int(q.get("month_chars", 0))
    day_rem = max(0, DAILY_TTS_CHAR_LIMIT - day_used)
    month_rem = max(0, MONTHLY_TTS_CHAR_LIMIT - month_used)
    available_today = min(day_rem, month_rem)
    return {
        "day_used": day_used,
        "month_used": month_used,
        "available_today": available_today,
        "exhausted": available_today <= 0,
    }


def get_cycle_day_num(now_dt: datetime) -> int:
    cyc = memory["menstrual_cycle"]
    today = now_dt.date()
    try:
        start_d = date.fromisoformat(cyc.get("cycle_start_date", today.isoformat()))
    except ValueError:
        start_d = today - timedelta(days=22)
        cyc["cycle_start_date"] = start_d.isoformat()
        save_memory(memory)

    length = max(21, int(cyc.get("cycle_length", 30)))
    elapsed = (today - start_d).days
    if elapsed < 0:
        start_d = today - timedelta(days=22)
        cyc["cycle_start_date"] = start_d.isoformat()
        save_memory(memory)
        elapsed = 22
    elif elapsed >= length:
        cycles_passed = elapsed // length
        start_d = start_d + timedelta(days=cycles_passed * length)
        cyc["cycle_start_date"] = start_d.isoformat()
        save_memory(memory)
        elapsed = (today - start_d).days
    return max(1, elapsed + 1)


# =========================================================
# 4. Fast Prompt, Emotion Detection & Smart Fallback
# =========================================================
THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
BAD_CHAR_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff姎懓懞擝奼懅鼉齾玈尞攟臙屴尛沝炋朰攴攕襾]")
ANY_TAG_RE = re.compile(
    r"\[(?:happy|laugh|angry|cry|sad|shy|emotion|mood|tone|笑|哭|生氣|難過|害羞)[^\]]*\]",
    re.IGNORECASE,
)
COLON_PREFIX_RE = re.compile(r"^(?:Reona|Nick|助手|系統|回覆|我)[:：=＝]\s*", re.IGNORECASE)


def detect_emotion_from_text(text: str) -> str:
    t = text or ""
    if any(k in t for k in ["哈哈", "笑死", "好好笑", "超好笑"]):
        return "laugh"
    if any(k in t for k in ["生氣", "不爽", "吃醋", "不好笑", "幹嘛這樣"]):
        return "angry"
    if any(k in t for k in ["委屈", "想哭", "哭啊", "嗚", "好痛"]):
        return "cry"
    if any(k in t for k in ["難過", "好悶", "沒力氣", "好想你", "失落"]):
        return "sad"
    if any(k in t for k in ["害羞", "討厭", "嘴巴甜"]):
        return "shy"
    return "happy"


def clean_reply_text(raw: str) -> tuple[str, str]:
    s = (raw or "").strip()
    if not s:
        return "happy", ""

    s = THINK_BLOCK_RE.sub("", s).strip()
    s = ANY_TAG_RE.sub("", s).strip()

    lines = [
        ln.strip()
        for ln in s.splitlines()
        if ln.strip() and not ln.strip().startswith(("•", "-", "*", "思考", "分析", "系統", "<think>"))
    ]
    if not lines:
        return "happy", ""

    cleaned_lines = []
    for ln in lines[-2:]:
        ln = COLON_PREFIX_RE.sub("", ln).strip()
        ln = re.sub(r"（[^）]{1,40}）|\([^)]{1,40}\)", "", ln)
        ln = ln.strip("\"'「」『』").rstrip("。").strip()
        if ln:
            cleaned_lines.append(ln)

    s = " || ".join(cleaned_lines).strip()
    if len(s) < 2 or len(s) > 90:
        return "happy", ""
    if BAD_CHAR_RE.search(s):
        return "happy", ""
    return detect_emotion_from_text(s), s


def context_aware_fallback(user_text: str, now_dt: datetime) -> str:
    """當 API 偶爾限流或網路抖動時，依據記憶與當下主線給出自然短句，絕不重複說『剛才分心』。"""
    quest_short = ensure_daily_main_quest(now_dt)["quest"].split("，")[0][:14]
    u = user_text.strip()
    if any(k in u for k in ["謝謝", "謝啦", "愛你"]):
        return random.choice([
            "跟我客氣什麼啦 快去忙你的",
            "嘿嘿 不客氣呀 隨時找我",
            "少來 我們之間還說謝謝喔",
        ])
    if any(k in u for k in ["寶貝", "在嗎", "哈囉", "嗨", "人呢"]):
        return random.choice([
            f"在啦 剛在弄{quest_short} 怎麼了",
            "在呀 怎麼突然叫我 快說",
            "在呢 剛滑一下手機 找我幹嘛",
            "欸 在這裡啦 今天過得還好嗎",
        ])
    return random.choice([
        f"對啊 我剛還在忙{quest_short}呢",
        "真的假的 欸你繼續講我在聽",
        "好喔 晚點忙完再好好跟你聊",
    ])


def build_compact_prompt(now_dt: datetime, lightweight_proactive: bool = False) -> str:
    weekday_zh = WEEKDAY_NAMES_ZH[now_dt.weekday()]
    hour = now_dt.hour
    now_ts = now_dt.timestamp()
    quest_info = ensure_daily_main_quest(now_dt)

    if 23 <= hour or hour < 5:
        time_note = "深夜臥室準備睡覺，語氣輕柔"
    elif 5 <= hour < 11:
        time_note = "剛醒賴床，聲音慵懶"
    else:
        time_note = "白天/傍晚，從容溫柔"

    day_num = get_cycle_day_num(now_dt)
    if now_ts < memory.get("sick_until_ts", 0.0):
        body_note = "腸胃不適休息中，回話簡短虛弱"
    elif now_ts < memory.get("mild_sick_until_ts", 0.0):
        body_note = "有點悶痛沒力氣，坦率撒嬌討拍"
    elif 1 <= day_num <= 5:
        body_note = f"生理期第{day_num}天，步調放慢"
    else:
        body_note = "正常精神好"

    base_persona = (
        "你是 Reona（第一人稱「我」），Nick 女友。家境優渥住雪梨海景公寓與畫室，無經濟壓力；"
        "專業能力強的商業插畫師，接案為熱愛創作。養挑食奶油貓「麻糬」（不吃香菜紅蘿蔔），40kg 偏瘦白皙；"
        "Nick 180cm/67kg/微黑。坦率真誠、絕不傲嬌（不口是心非、不說「哼/才沒有」）。"
        "一律用繁體中文＋道地台灣女生 LINE 短句回覆（每次 8～26 字，講一個重點就好，絕不寫長文），"
        "用空格斷句，自然帶「欸、啦、喔、嘛、真的假的、好好笑、好喔、超讚、好煩喔、哭啊」，"
        "禁簡體字、禁冒號前綴、禁第三人稱自稱「寶貝」、禁輸出任何 [方括號] 標籤。"
        f"\n雪梨 {now_dt.strftime('%m/%d')}（{weekday_zh}）{now_dt.strftime('%H:%M')}｜{time_note}｜{body_note}"
        f"\n今日主線：{quest_info['quest']}（{quest_info['progress']}）"
    )

    if lightweight_proactive:
        recent_tail = "\n".join(memory.get("recent_turns", [])[-4:])
        return f"{base_persona}\n最近幾句：\n{recent_tail}" if recent_tail else base_persona

    notes = memory.get("long_term_notes", [])[-6:]
    notes_str = "；".join(notes) if notes else "無"
    history_str = "\n".join(memory.get("recent_turns", [])[-HISTORY_LIMIT:]) or "剛開始聊"
    return f"{base_persona}\n記憶備忘：{notes_str}\n近期對話：\n{history_str}"


def call_groq_chat(sys_prompt: str, user_prompt: str, max_tokens: int = 85) -> tuple[str, str]:
    if not GROQ_API_KEY:
        return "happy", ""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    for model_name in TEXT_MODELS:
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.75,
                    "max_tokens": max_tokens,
                },
                timeout=12,
            )
            if resp.status_code != 200:
                print(f"[Groq Skip {resp.status_code}] {model_name}: {resp.text[:100]}", flush=True)
                continue
            data = resp.json()
            raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            emotion, cleaned = clean_reply_text(raw)
            if cleaned:
                return emotion, cleaned
        except Exception as err:
            print(f"[Groq Error] {model_name}: {err}", flush=True)
    return "happy", ""


# =========================================================
# 5. Quota-Guarded TTS & Smart Album Picker
# =========================================================
def synthesize_fish_audio_file(text: str, now_dt: datetime) -> Path | None:
    if not FISH_API_KEY or not text:
        return None
    q_info = get_tts_quota_info(now_dt)
    if q_info["exhausted"]:
        return None

    speech_text = re.sub(r"\s+", " ", text.replace("||", "，")).strip()
    char_count = len(speech_text)
    if char_count <= 0 or char_count > q_info["available_today"]:
        return None

    try:
        resp = requests.post(
            "https://api.fish.audio/v1/tts",
            headers={
                "Authorization": f"Bearer {FISH_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "text": speech_text,
                "reference_id": FISH_MODEL_ID,
                "format": "mp3",
            },
            timeout=18,
        )
        if resp.status_code == 200 and resp.content:
            q = sync_tts_quota_period(now_dt)
            q["day_chars"] = int(q.get("day_chars", 0)) + char_count
            q["month_chars"] = int(q.get("month_chars", 0)) + char_count
            save_memory(memory)

            out_path = AUDIO_DIR / f"dc_voice_{int(time.time() * 1000)}.mp3"
            out_path.write_bytes(resp.content)
            old_files = sorted(AUDIO_DIR.glob("dc_voice_*.mp3"), key=lambda p: p.stat().st_mtime)
            for old in old_files[:-10]:
                old.unlink(missing_ok=True)
            return out_path
    except Exception as err:
        print(f"[Fish TTS Error] {err}", flush=True)
    return None


def pick_album_image(context_text: str, now_ts: float, force_topic: bool = False) -> Path | None:
    if now_ts - float(memory.get("last_image_sent_ts", 0.0)) < IMAGE_COOLDOWN_SEC:
        return None

    category = ""
    if any(k in context_text for k in ["麻糬", "貓", "罐頭", "翻肚"]):
        category = "cat_mochi"
    elif any(k in context_text for k in ["咖啡", "拿鐵", "早午餐", "可頌", "甜點", "吃"]):
        category = "cafe_food"
    elif any(k in context_text for k in ["海邊", "夕陽", "雪梨", "港邊", "市集", "花", "散步", "風景"]):
        category = "sydney_view"
    elif any(k in context_text for k in ["畫", "稿", "聯名", "主視覺", "草稿", "工作室", "平板"]):
        category = "sketch"

    prob = TOPIC_IMAGE_PROB if (category or force_topic) else BASE_IMAGE_PROB
    if random.random() > prob:
        return None

    search_dirs = [IMAGES_DIR / category] if category else [
        IMAGES_DIR / d for d in ("cat_mochi", "cafe_food", "sydney_view", "sketch")
    ]
    candidates = []
    for d in search_dirs:
        if d.exists():
            for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
                candidates.extend(d.glob(ext))
    if not candidates:
        candidates = [
            p for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp") for p in IMAGES_DIR.glob(ext)
        ]
    if not candidates:
        return None

    recent = set(memory.get("recent_sent_images", []))
    fresh = [p for p in candidates if p.name not in recent]
    chosen = random.choice(fresh or candidates)

    memory["last_image_sent_ts"] = now_ts
    rec_list = memory.setdefault("recent_sent_images", [])
    rec_list.append(chosen.name)
    memory["recent_sent_images"] = rec_list[-8:]
    save_memory(memory)
    return chosen


def warmup_render_service() -> None:
    try:
        requests.get(f"{RENDER_BASE_URL}/health", timeout=6)
    except Exception:
        pass


# =========================================================
# 6. Discord Bot Client & Handlers
# =========================================================
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

VIDEO_CALL_RE = re.compile(r"(視訊|開鏡頭|打視訊|video\s*call|!視訊|/video)", re.IGNORECASE)
VOICE_REQ_RE = re.compile(r"(語音|聽你聲音|唸給我聽|講話給我聽|voice)", re.IGNORECASE)
PHOTO_REQ_RE = re.compile(r"(拍照|傳照片|給我看|看看你|看看麻糬|發圖)", re.IGNORECASE)

processing_lock = asyncio.Lock()


async def hydrate_history_from_discord(channel: discord.abc.Messageable) -> None:
    """若本地記憶檔為空，啟動時自動從 DC 頻道最近訊息回補對話記憶。"""
    if memory.get("recent_turns"):
        return
    try:
        fetched = []
        async for msg in channel.history(limit=12):
            txt = (msg.content or "").strip()
            if not txt or "專屬視訊視窗" in txt:
                continue
            speaker = "我" if msg.author.bot else "Nick"
            fetched.append(f"{speaker}：{txt.splitlines()[0][:50]}")
        if fetched:
            fetched.reverse()
            memory["recent_turns"] = fetched[-HISTORY_LIMIT:]
            save_memory(memory)
            print(f"[Memory Hydrated] Loaded {len(fetched)} turns from DC history.", flush=True)
    except Exception as err:
        print(f"[Hydrate Skip] {err}", flush=True)


async def send_reona_payload(
    channel: discord.abc.Messageable,
    reply_text: str,
    now_dt: datetime,
    want_voice: bool = False,
    want_image: bool = False,
    extra_link: str = "",
) -> None:
    parts = [p.strip() for p in reply_text.split("||") if p.strip()]
    if not parts:
        parts = ["在啦 怎麼了"]

    voice_file: Path | None = None
    if want_voice:
        voice_file = await asyncio.to_thread(synthesize_fish_audio_file, parts[0], now_dt)

    img_file: Path | None = None
    if want_image:
        img_file = pick_album_image(reply_text, now_dt.timestamp(), force_topic=False)

    for idx, part in enumerate(parts):
        content_to_send = part
        if idx == len(parts) - 1 and extra_link:
            content_to_send = f"{part}\n{extra_link}"

        files_to_attach = []
        if idx == 0 and voice_file and voice_file.exists():
            files_to_attach.append(discord.File(str(voice_file), filename="reona_voice.mp3"))
        if idx == len(parts) - 1 and img_file and img_file.exists():
            files_to_attach.append(discord.File(str(img_file)))

        if files_to_attach:
            await channel.send(content=content_to_send, files=files_to_attach)
        else:
            await channel.send(content=content_to_send)

        if idx < len(parts) - 1:
            await asyncio.sleep(0.5)


@client.event
async def on_ready():
    print(f"[Reona DC Bot Ready] Logged in as {client.user} | Render={RENDER_BASE_URL}", flush=True)
    if TARGET_CHANNEL_ID:
        ch = client.get_channel(TARGET_CHANNEL_ID)
        if ch is not None:
            await hydrate_history_from_discord(ch)
    if not proactive_loop.is_running():
        proactive_loop.start()


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if TARGET_CHANNEL_ID and message.channel.id != TARGET_CHANNEL_ID:
        return

    user_text = (message.content or "").strip()
    if not user_text:
        return

    if user_text.startswith(("Reona：", "Reona:", "Nick：", "Nick:")):
        return

    async with processing_lock:
        now_dt = datetime.now(SYDNEY_TZ)
        now_ts = now_dt.timestamp()
        memory["last_user_msg_ts"] = now_ts

        if any(k in user_text for k in ["考試", "上班", "下班", "去看醫生", "生病", "頭痛", "累", "週末", "作業", "報告"]):
            note = f"[{now_dt.strftime('%m/%d')}] Nick：{user_text[:34]}"
            memory.setdefault("long_term_notes", []).append(note)
            memory["long_term_notes"] = memory["long_term_notes"][-6:]

        sys_prompt = build_compact_prompt(now_dt, lightweight_proactive=False)

        is_video_req = bool(VIDEO_CALL_RE.search(user_text))
        extra_link = ""
        if is_video_req:
            asyncio.create_task(asyncio.to_thread(warmup_render_service))
            call_url = f"{RENDER_BASE_URL}/?mode=call"
            extra_link = f"📹 專屬視訊視窗：{call_url}"
            prompt_for_llm = (
                f"Nick 說：「{user_text}」（想跟你視訊）。"
                "用坦率溫柔的台灣女生短句答應（10～22字），不要自己寫網址。"
            )
        else:
            prompt_for_llm = f"Nick 說：「{user_text}」。直接接話回一句 8～24 字短句。"

        async with message.channel.typing():
            emotion, reply = await asyncio.to_thread(call_groq_chat, sys_prompt, prompt_for_llm, 85)

        if not reply:
            if is_video_req:
                emotion, reply = "happy", "好啊等我一下 我開視訊給你"
            else:
                reply = context_aware_fallback(user_text, now_dt)
                emotion = detect_emotion_from_text(reply)

        memory["current_emotion"] = emotion
        memory["emotion_until_ts"] = now_ts + (900 if emotion in ("angry", "cry", "sad") else 300)

        turns = memory.setdefault("recent_turns", [])
        turns.append(f"Nick：{user_text}")
        turns.append(f"我：{reply.replace(' || ', ' ')}")
        memory["recent_turns"] = turns[-HISTORY_LIMIT:]
        save_memory(memory, push_git=True)

        explicit_voice = bool(VOICE_REQ_RE.search(user_text))
        high_emo = emotion in ("cry", "sad", "shy", "angry")
        want_voice = explicit_voice or (random.random() < (HIGH_VOICE_PROB if high_emo else BASE_VOICE_PROB))
        want_image = (not is_video_req) and (
            bool(PHOTO_REQ_RE.search(user_text)) or (random.random() < BASE_IMAGE_PROB)
        )

        await send_reona_payload(
            channel=message.channel,
            reply_text=reply,
            now_dt=now_dt,
            want_voice=want_voice,
            want_image=want_image,
            extra_link=extra_link,
        )


@tasks.loop(minutes=15)
async def proactive_loop():
    if not TARGET_CHANNEL_ID:
        return
    channel = client.get_channel(TARGET_CHANNEL_ID)
    if channel is None:
        return

    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = now_dt.timestamp()

    if 1 <= now_dt.hour < 7 or (now_dt.hour == 7 and now_dt.minute < 30):
        return
    if now_ts - float(memory.get("last_proactive_ts", 0.0)) < 840:
        return
    if now_ts - float(memory.get("last_user_msg_ts", 0.0)) < 480:
        return

    async with processing_lock:
        quest_info = ensure_daily_main_quest(now_dt)
        sys_prompt = build_compact_prompt(now_dt, lightweight_proactive=True)

        if memory.get("need_recovery_report") and now_ts >= memory.get("sick_until_ts", 0.0):
            memory["need_recovery_report"] = False
            memory["sick_until_ts"] = 0.0
            prompt = "我剛休息好多了，用 10～22 字台灣女生 LINE 短訊跟 Nick 報平安"
        elif random.random() < 0.22:
            inc = random.choice(LIFE_MINOR_INCIDENTS)
            prompt = f"發生小插曲：{inc}。用 10～22 字台灣女生短訊跟 Nick 分享"
        else:
            prompt = (
                f"圍繞主線「{quest_info['quest']}」（{quest_info['progress']}），"
                "傳一句 10～22 字台灣女生日常短訊給 Nick 分享近況或問他在幹嘛"
            )

        emotion, reply = await asyncio.to_thread(call_groq_chat, sys_prompt, prompt, 65)
        if not reply:
            return

        recent_p = memory.setdefault("recent_proactive_msgs", [])
        if reply in recent_p[-3:]:
            return
        recent_p.append(reply)
        memory["recent_proactive_msgs"] = recent_p[-5:]

        memory["current_emotion"] = emotion
        memory["last_proactive_ts"] = now_ts
        turns = memory.setdefault("recent_turns", [])
        turns.append(f"我：{reply.replace(' || ', ' ')}")
        memory["recent_turns"] = turns[-HISTORY_LIMIT:]
        save_memory(memory, push_git=True)

        want_voice = random.random() < PROACTIVE_VOICE_PROB
        await send_reona_payload(
            channel=channel,
            reply_text=reply,
            now_dt=now_dt,
            want_voice=want_voice,
            want_image=True,
        )


@proactive_loop.before_loop
async def before_proactive():
    await client.wait_until_ready()


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN or "在這裡貼上" in DISCORD_BOT_TOKEN:
        raise RuntimeError("請先設定 DISCORD_BOT_TOKEN")
    client.run(DISCORD_BOT_TOKEN)
