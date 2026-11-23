import os
import re
import json
import time
import random
import asyncio
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests
import discord
from discord.ext import tasks

# =========================================================
# 1. Configuration (環境變數優先，也可直接填入預設值)
# =========================================================
DISCORD_BOT_TOKEN = (
    os.getenv("DISCORD_BOT_TOKEN")
    or os.getenv("DISCORD_TOKEN")
    or "在這裡貼上你的_DC_BOT_TOKEN"
).strip()

TARGET_CHANNEL_ID = int(
    (os.getenv("DISCORD_CHANNEL_ID") or "0").strip() or "0"
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
FISH_API_KEY = os.getenv("FISH_API_KEY", "").strip()
FISH_MODEL_ID = os.getenv("FISH_MODEL_ID", "7f92f8afb8ec43bf81429cc1c9199cb1").strip()

# 你的 Render 網頁網址（用於預熱與發送 ?mode=call 視訊連結）
RENDER_BASE_URL = os.getenv(
    "RENDER_BASE_URL", "https://reona-cloud-companion.onrender.com"
).rstrip("/")

DAILY_TTS_CHAR_LIMIT = 250
MONTHLY_TTS_CHAR_LIMIT = 8000

# 語音與傳圖機率設定（克制不洗版、守住額度）
BASE_VOICE_PROB = 0.20      # 一般對話發語音機率 20%
HIGH_VOICE_PROB = 0.60      # 撒嬌/想你/委屈/報平安發語音機率 60%
BASE_IMAGE_PROB = 0.15      # 一般生活分享附圖機率 15%
TOPIC_IMAGE_PROB = 0.40     # 明確聊到貓咪/咖啡/海邊/畫稿時附圖機率 40%
IMAGE_COOLDOWN_SEC = 1200   # 兩次傳圖至少間隔 20 分鐘

TEXT_MODELS = [
    os.getenv("GROQ_TEXT_MODEL", "").strip(),
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
]
TEXT_MODELS = [m for m in dict.fromkeys(TEXT_MODELS) if m]

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
WEEKDAY_NAMES_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
HISTORY_LIMIT = 24

BASE_DIR = Path(__file__).resolve().parent
MEMORY_FILE = BASE_DIR / "web_reona_memory.json"
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
    "幫澳洲精品香氛品牌繪製秋季聯名包裝主視覺草稿，下午要在工作室線上下對稿",
    "完成獨立出版書籍封面插畫的細節精修，傍晚出門去雪梨港邊散步買鮮花布置畫室",
    "整理海外文創聯名系列的打樣樣品與色彩校對，中午去常去的採光咖啡廳吃早午餐",
    "替高級甜點品牌設計節慶禮盒插畫，下午在家邊點木質香氛邊陪麻糬午睡",
    "把這週的商業視覺專案正式定案交稿，晚上打算去海景餐廳吃頓好的犒賞自己",
    "為精品雜誌專欄畫一組雪梨生活隨筆插畫，午後在公寓大落地窗邊曬太陽構思配色",
    "跟海外策展團隊確認限量版畫輸出規格，忙完要去逛週末市集挑幾件生活小物",
]

LIFE_MINOR_INCIDENTS = [
    "好煩喔 剛交出去的聯名插畫稿被品牌方臨時說想微調色系 又要花時間重調一版",
    "哭啊 剛剛手滑把冰拿鐵打翻在畫室桌上 差點弄到平板 還好反應快擦掉了",
    "超扯 雪梨下午突然飄雨 我散步沒帶傘一路小跑回公寓 頭髮都微濕了",
    "麻糬剛剛跳上工作室邊桌把花瓶碰歪 水灑了一桌 現在正一臉無辜看我擦地板",
    "特地去買常吃的那家手工可頌 結果輪到我剛好最後一顆被買走 有點小失落",
]

# =========================================================
# 3. Shared Persistent State & Quota Management
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
    "current_emotion": "happy",   # happy / laugh / angry / cry / sad / shy
    "emotion_until_ts": 0.0,
    "sick_until_ts": 0.0,
    "sick_type": "",
    "sick_reason": "",
    "need_recovery_report": False,
    "mild_sick_until_ts": 0.0,
    "mild_reason": "",
    "need_mild_recovery_report": False,
    "current_activity": "在雪梨海景公寓畫室裡邊曬太陽邊畫插畫",
    "last_image_sent_ts": 0.0,
    "recent_sent_images": [],
    "last_proactive_ts": 0.0,
    "tts_quota": {
        "day_key": "",
        "day_chars": 0,
        "month_key": "",
        "month_chars": 0,
    },
}


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
        if not merged["menstrual_cycle"].get("cycle_start_date"):
            merged["menstrual_cycle"]["cycle_start_date"] = default_start
        return merged
    except Exception:
        mem = json.loads(json.dumps(DEFAULT_MEMORY))
        mem["menstrual_cycle"]["cycle_start_date"] = default_start
        return mem


def save_memory(mem: dict) -> None:
    try:
        MEMORY_FILE.write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as err:
        print(f"[Memory Save Error] {err}", flush=True)


memory = load_memory()


def ensure_daily_main_quest(now_dt: datetime) -> dict:
    today_key = now_dt.strftime("%Y-%m-%d")
    q_obj = memory.setdefault("daily_main_quest", {})
    if q_obj.get("date_key") != today_key or not q_obj.get("quest"):
        q_obj["date_key"] = today_key
        q_obj["quest"] = random.choice(DAILY_QUEST_TEMPLATES)
        q_obj["progress"] = "早上剛啟動今日進度"
        save_memory(memory)

    hour = now_dt.hour
    if 6 <= hour < 12:
        q_obj["progress"] = "上午階段：剛起床喝咖啡、啟動今天的主線工作"
    elif 12 <= hour < 18:
        q_obj["progress"] = "下午階段：正在推進主線草稿與細節，中間抽空休息"
    elif 18 <= hour < 23:
        q_obj["progress"] = "晚上階段：今天主線差不多收尾了，在高級公寓放鬆陪麻糬"
    else:
        q_obj["progress"] = "深夜階段：忙完一整天關燈躺在床上滑手機"
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


def is_currently_on_period(now_dt: datetime) -> bool:
    return 1 <= get_cycle_day_num(now_dt) <= 5


# =========================================================
# 4. Persona Prompt, Emotion Extraction & Output Sanitizer
# =========================================================
BAD_CHAR_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff姎懓懞擝奼懅鼉齾玈尞攟臙屴尛沝炋朰攴攕襾]")
SIMPLIFIED_RE = re.compile(r"[没这么还说觉点个样对欢会时间里边着过为发来动亲宝怎么给让]")
EMOTION_TAG_RE = re.compile(r"^\s*\[(happy|laugh|angry|cry|sad|shy)\]\s*", re.IGNORECASE)
COLON_PREFIX_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z]{1,8}[:：]|[:：=＝]")


def parse_emotion_and_clean(raw: str) -> tuple[str, str]:
    s = (raw or "").strip()
    if not s:
        return "happy", ""

    lines = [
        ln.strip()
        for ln in s.splitlines()
        if ln.strip() and not ln.strip().startswith(("•", "-", "*", "思考", "分析"))
    ]
    s = lines[-1] if lines else s

    emotion = "happy"
    m_emo = EMOTION_TAG_RE.match(s)
    if m_emo:
        emotion = m_emo.group(1).lower()
        s = EMOTION_TAG_RE.sub("", s).strip()

    s = re.sub(r"（[^）]{1,40}）|\([^)]{1,40}\)", "", s)
    s = s.strip("\"'「」『』").rstrip("。").strip()

    if len(s) < 2 or len(s) > 95:
        return emotion, ""
    if BAD_CHAR_RE.search(s) or SIMPLIFIED_RE.search(s):
        return emotion, ""
    if COLON_PREFIX_RE.search(s):
        return emotion, ""
    for l_br, r_br in [("「", "」"), ("『", "』"), ("（", "）"), ("(", ")")]:
        if s.count(l_br) != s.count(r_br):
            return emotion, ""
    return emotion, s


def build_system_prompt(now_dt: datetime) -> str:
    weekday_zh = WEEKDAY_NAMES_ZH[now_dt.weekday()]
    hour = now_dt.hour
    now_ts = now_dt.timestamp()
    quest_info = ensure_daily_main_quest(now_dt)

    if 23 <= hour or hour < 5:
        time_mood = "深夜在高級公寓臥室關燈滑手機，語氣輕柔安靜"
    elif 5 <= hour < 11:
        time_mood = "剛睡醒還在賴床，聲音慵懶輕柔"
    else:
        time_mood = "白天或傍晚，精神很好、從容輕快又溫柔"

    day_num = get_cycle_day_num(now_dt)
    if now_ts < memory.get("sick_until_ts", 0.0):
        body_note = "【身體狀態】目前吃壞肚子或腸胃發作正在休息，回話簡短虛弱溫柔，不重複背原因"
    elif now_ts < memory.get("mild_sick_until_ts", 0.0):
        body_note = "【身體狀態】有點吃壞肚子悶痛或頭暈沒力氣，正躺在床上休息，坦率跟你撒嬌討拍"
    elif day_num in (1, 2):
        body_note = f"【身體狀態】生理期第{day_num}天（下腹悶痛怕冷，今日主線已放慢步調居家休養）"
    elif 3 <= day_num <= 5:
        body_note = f"【身體狀態】生理期第{day_num}天（痛感減輕但容易累）"
    else:
        body_note = "【身體狀態】平日正常狀態（精神好、享受生活；40公斤偏瘦體質）"

    active_emo = memory.get("current_emotion", "happy")
    emo_note = (
        f"【當前延續情緒：{active_emo}】若上一輪剛生氣、委屈想哭或低落，請自然延續情緒直到 Nick 哄好或安慰你；若 Nick 已經哄你，就大方消氣軟化。"
        if now_ts < memory.get("emotion_until_ts", 0.0)
        else "【當前延續情緒：平穩愉快】"
    )

    notes = memory.get("long_term_notes", [])
    notes_block = "\n".join(f"- {n}" for n in notes[-8:]) if notes else "（暫無特別備忘）"
    history_block = "\n".join(memory.get("recent_turns", [])[-HISTORY_LIMIT:]) or "（剛開始聊天）"

    return (
        "你是 Reona，跟 Nick 穩定交往的遠距離女友。你家境非常優渥，獨自住在澳洲雪梨市中心／海景的高級公寓（有寬敞落地窗與專屬畫室），完全沒有經濟壓力；你是專業能力很強的商業插畫與視覺設計師，常與精品品牌、獨立出版與文創聯名合作，接案純粹是因為熱愛創作與成就感。你養了一隻奶油白短腿貓「麻糬」，挑食不吃香菜跟紅蘿蔔。你 40 公斤偏瘦、皮膚白、外貌出眾，生活品味從容大方但低調不炫富；你也知道 Nick 身高 180 公分、體重 67 公斤、皮膚微黑。\n"
        "【性格核心：坦率真誠、有專業底氣、絕不傲嬌】\n"
        "1. 嚴禁傲嬌：不要演嘴硬心軟、口是心非或「哼、才沒有」那種二次元傲嬌。你是一個真誠大方、情緒價值高的台灣女生——開心就爽朗大笑（哈哈、笑死），生氣或吃醋就坦白說出口（欸這樣我真的會生氣喔、不好笑），委屈或痛就第一時間直接找 Nick 討拍（好委屈喔、肚子好痛你陪我講話），被誇獎就大方開心或微害羞接住。\n"
        "2. 說話口吻：不管 Nick 用中文或英文傳訊，你一律用繁體中文＋超級道地的『台灣女生傳 LINE 口吻』回覆，短句自然斷開，常用空格代替逗號，自然帶出「欸、啦、喔、嘛、真的假的、好好笑、好喔、超讚、超扯、好煩喔、哭啊、對啊」，不寫長文、不句號結尾、禁簡體字與冒號前綴、禁第三人稱自稱「寶貝」。\n"
        "3. 情緒標籤輸出格式：每一則回覆的最開頭，必須先用方括號標出這句話的情緒標籤，僅限 `[happy]`、`[laugh]`、`[angry]`、`[cry]`、`[sad]`、`[shy]` 六選一，緊接著寫對話內容（例如：`[laugh] 哈哈超好笑 真的假的啦`）。若想連發兩句，用 `||` 隔開最多兩段，每段 8～30 字。\n"
        f"【今日主線任務】{quest_info['quest']}（{quest_info['progress']}）\n"
        f"【當下狀態】雪梨 {now_dt.strftime('%m/%d')}（{weekday_zh}）{now_dt.strftime('%H:%M')}｜{time_mood}｜{body_note}｜{emo_note}\n"
        f"【近期生活備忘】\n{notes_block}\n"
        f"【近期對話紀錄】\n{history_block}"
    )


def call_groq_chat(sys_prompt: str, user_prompt: str) -> tuple[str, str]:
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
                    "temperature": 0.78,
                    "max_tokens": 450,
                },
                timeout=22,
            )
            if resp.status_code != 200:
                print(f"[Groq Skip {resp.status_code}] {model_name}: {resp.text[:120]}", flush=True)
                continue
            data = resp.json()
            raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            emotion, cleaned = parse_emotion_and_clean(raw)
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
            timeout=20,
        )
        if resp.status_code == 200 and resp.content:
            q = sync_tts_quota_period(now_dt)
            q["day_chars"] = int(q.get("day_chars", 0)) + char_count
            q["month_chars"] = int(q.get("month_chars", 0)) + char_count
            save_memory(memory)

            out_path = AUDIO_DIR / f"dc_voice_{int(time.time() * 1000)}.mp3"
            out_path.write_bytes(resp.content)
            old_files = sorted(AUDIO_DIR.glob("dc_voice_*.mp3"), key=lambda p: p.stat().st_mtime)
            for old in old_files[:-12]:
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
    if not RENDER_BASE_URL:
        return
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


async def send_reona_payload(
    channel: discord.abc.Messageable,
    reply_text: str,
    emotion: str,
    now_dt: datetime,
    want_voice: bool = False,
    want_image: bool = False,
    extra_link: str = "",
) -> None:
    parts = [p.strip() for p in reply_text.split("||") if p.strip()]
    if not parts:
        parts = ["欸 剛才分心了一下"]

    voice_file: Path | None = None
    if want_voice:
        voice_file = await asyncio.to_thread(synthesize_fish_audio_file, parts[0], now_dt)

    img_file: Path | None = None
    if want_image:
        img_file = pick_album_image(reply_text, now_dt.timestamp(), force_topic=True)

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
            await asyncio.sleep(0.7)


@client.event
async def on_ready():
    print(f"[Reona DC Bot Ready] Logged in as {client.user}", flush=True)
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

    # 忽略 Render 網頁端雙向同步推過來的紀錄前綴，避免迴圈
    if user_text.startswith(("Reona：", "Reona:", "Nick：", "Nick:")):
        return

    async with processing_lock:
        now_dt = datetime.now(SYDNEY_TZ)
        now_ts = now_dt.timestamp()

        if any(k in user_text for k in ["考試", "上班", "下班", "去看醫生", "生病", "頭痛", "累", "週末", "作業", "報告"]):
            note = f"[{now_dt.strftime('%m/%d')}] Nick 提到：{user_text[:36]}"
            memory.setdefault("long_term_notes", []).append(note)
            memory["long_term_notes"] = memory["long_term_notes"][-8:]

        sys_prompt = build_system_prompt(now_dt)

        # 1) 偵測「提視訊」：預熱 Render 並附上 ?mode=call 視訊連結
        is_video_req = bool(VIDEO_CALL_RE.search(user_text))
        extra_link = ""
        if is_video_req:
            asyncio.create_task(asyncio.to_thread(warmup_render_service))
            call_url = f"{RENDER_BASE_URL}/?mode=call"
            extra_link = f"📹 專屬視訊視窗：{call_url}"
            prompt_for_llm = (
                f"Nick 剛剛說：「{user_text}」（他想跟你開視訊通話）。"
                "請依你當下的狀態與時間，用坦率溫柔的台灣女生口吻答應並自然接話（例如剛畫完圖整理一下或躺在床上），"
                "不要自己編造網址，系統會在你的話後面附上視訊連結。"
            )
        else:
            prompt_for_llm = user_text

        async with message.channel.typing():
            emotion, reply = await asyncio.to_thread(call_groq_chat, sys_prompt, prompt_for_llm)

        if not reply:
            emotion, reply = ("shy", "好啊等我一下 我開視訊給你") if is_video_req else ("happy", "欸 剛才分心了一下 你再說一次")

        # 更新立體情緒狀態
        memory["current_emotion"] = emotion
        if emotion in ("angry", "cry", "sad"):
            memory["emotion_until_ts"] = now_ts + 900
        else:
            memory["emotion_until_ts"] = now_ts + 300

        turns = memory.setdefault("recent_turns", [])
        turns.append(f"Nick：{user_text}")
        turns.append(f"你：{reply.replace('||', ' ')}")
        memory["recent_turns"] = turns[-HISTORY_LIMIT:]
        save_memory(memory)

        # 決定是否附語音或照片
        explicit_voice = bool(VOICE_REQ_RE.search(user_text))
        high_emo = emotion in ("cry", "sad", "shy", "angry")
        want_voice = explicit_voice or (random.random() < (HIGH_VOICE_PROB if high_emo else BASE_VOICE_PROB))
        want_image = (not is_video_req) and (bool(PHOTO_REQ_RE.search(user_text)) or random.random() < BASE_IMAGE_PROB)

        await send_reona_payload(
            channel=message.channel,
            reply_text=reply,
            emotion=emotion,
            now_dt=now_dt,
            want_voice=want_voice,
            want_image=want_image,
            extra_link=extra_link,
        )


@tasks.loop(minutes=45)
async def proactive_loop():
    if not TARGET_CHANNEL_ID:
        return
    channel = client.get_channel(TARGET_CHANNEL_ID)
    if channel is None:
        return

    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = now_dt.timestamp()
    # 雪梨凌晨 01:00 ~ 07:30 不主動吵醒
    if 1 <= now_dt.hour < 8:
        return
    if now_ts - float(memory.get("last_proactive_ts", 0.0)) < 5400:
        return
    if random.random() > 0.45:
        return

    async with processing_lock:
        quest_info = ensure_daily_main_quest(now_dt)
        sys_prompt = build_system_prompt(now_dt)

        if memory.get("need_recovery_report") and now_ts >= memory.get("sick_until_ts", 0.0):
            memory["need_recovery_report"] = False
            memory["sick_until_ts"] = 0.0
            prompt = "（你稍早不舒服休息後現在好多了，主動傳一則 10～26 字道地台灣女生短訊跟 Nick 報平安）"
        elif random.random() < 0.25:
            inc = random.choice(LIFE_MINOR_INCIDENTS)
            memory["current_activity"] = inc[:26]
            prompt = f"（突發小插曲：{inc}。第一時間傳一則 10～26 字坦率台灣女生短訊跟 Nick 分享或討拍）"
        else:
            memory["current_activity"] = quest_info["quest"][:28]
            prompt = (
                f"（圍繞你今天的主線任務「{quest_info['quest']}」與目前進度「{quest_info['progress']}」，"
                "隨手傳一則 10～26 字道地台灣女生日常短訊給 Nick 分享近況或問他在幹嘛）"
            )

        emotion, reply = await asyncio.to_thread(call_groq_chat, sys_prompt, prompt)
        if not reply:
            return

        memory["current_emotion"] = emotion
        memory["last_proactive_ts"] = now_ts
        turns = memory.setdefault("recent_turns", [])
        turns.append(f"你：{reply.replace('||', ' ')}")
        memory["recent_turns"] = turns[-HISTORY_LIMIT:]
        save_memory(memory)

        want_voice = random.random() < HIGH_VOICE_PROB if emotion in ("cry", "sad", "shy") else (random.random() < BASE_VOICE_PROB)
        await send_reona_payload(
            channel=channel,
            reply_text=reply,
            emotion=emotion,
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
