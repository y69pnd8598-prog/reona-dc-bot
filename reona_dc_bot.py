import os
import re
import json
import time
import random
import asyncio
import aiohttp
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
from groq import AsyncGroq


# =========================================================
# 1. 基礎設定與環境變數
# =========================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
DEFAULT_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

PREFERRED_TEXT_MODELS = [
    os.getenv("GROQ_TEXT_MODEL", "").strip(),
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "allam-2-7b",
]
PREFERRED_TEXT_MODELS = [m for m in PREFERRED_TEXT_MODELS if m]

PREFERRED_VISION_MODELS = [
    os.getenv("GROQ_VISION_MODEL", "").strip(),
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "llama-3.2-90b-vision-preview",
    "llama-3.2-11b-vision-preview",
]
PREFERRED_VISION_MODELS = [m for m in PREFERRED_VISION_MODELS if m]

TEXT_MODEL = PREFERRED_TEXT_MODELS[0] if PREFERRED_TEXT_MODELS else "qwen/qwen3.8-27b"
VISION_MODEL = PREFERRED_VISION_MODELS[0] if PREFERRED_VISION_MODELS else "meta-llama/llama-4-scout-17b-16e-instruct"
AVAILABLE_TEXT_MODELS = list(PREFERRED_TEXT_MODELS)

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
START_TIME = time.time()
RELAY_AFTER_SECONDS = 5 * 3600 + 45 * 60  # 5 小時 45 分鐘 (20700 秒)

MEMORY_FILE = "reona_memory.json"
TEMP_IMAGE_PATH = "temp_latest_image.jpg"

print(
    f"[Config Check] DISCORD_TOKEN set: {bool(DISCORD_TOKEN)} | GROQ_API_KEY set: {bool(GROQ_API_KEY)}",
    flush=True,
)


# =========================================================
# 2. 40kg 偏弱體質狀態池（高頻小毛病 / 中等提高機率 / 嚴重偶爾）
# =========================================================
MINOR_AILMENTS = [
    "手指尖跟腳底微涼，正縮在椅子上捂熱水杯",
    "胃口小小的一吃就飽，肚子有點微脹懶得動",
    "剛才吹了點冷風，肩頸微僵、整個人慢半拍",
    "螢幕看久了眼睛有點酸酸的，微微犯睏",
    "午後突然沒電，有點頭重腳輕想賴著發呆",
    "沒什麼食慾，只想喝點溫溫的茶暖胃",
]

MODERATE_CONDITIONS = [
    "手腳冰冷到有點發僵，裹著毯子還是覺得暖不起來，語氣軟綿綿沒力氣鬥嘴",
    "腸胃敏感發作，胃悶悶的不太舒服，嘴上說沒事但其實很想被哄",
    "昨晚沒睡好加上底子弱，現在頭昏腦脹、有點委屈低氣壓",
    "吹風著涼後鼻音重重的、身體發沉，隔著螢幕特別想有人陪在旁邊",
]

SEVERE_CONDITIONS = [
    "胃痛發作只能縮在被窩裡，40公斤的身板沒什麼力氣硬撐，徹底卸下防備想撒嬌討拍",
    "疑似著涼低燒、頭重腳輕全身發軟，連嘴硬的力氣都沒有，很依賴對方陪著說話",
    "體力透支加上腸胃不適，整個人縮成一團委屈巴巴，忍不住直白希望對方在身邊",
]


# =========================================================
# 3. 三層記憶系統與持久化
# =========================================================
DEFAULT_MEMORY = {
    "user_profile": {
        "nickname": "你",
        "likes": [],
        "dislikes": [],
        "notes": [],
    },
    "recent_turns": [],
    "recent_replies": [],
    "body_state": {
        "weight_kg": 40,
        "stomach_fullness": 35,
        "energy": 65,
        "mood": "慵懶微甜",
        "severity": "minor",  # minor | moderate | severe
        "current_symptom": "手指尖微涼，正縮在角落慢慢回暖",
        "last_symptom_ts": 0.0,
        "pending_aftermath": None,
    },
    "last_interaction_ts": 0.0,
    "last_proactive_ts": 0.0,
}


def load_memory() -> dict:
    if not os.path.exists(MEMORY_FILE):
        return json.loads(json.dumps(DEFAULT_MEMORY))
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = json.loads(json.dumps(DEFAULT_MEMORY))
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return merged
    except Exception as err:
        print(f"[Memory Load Error] {err}", flush=True)
        return json.loads(json.dumps(DEFAULT_MEMORY))


def save_memory(mem: dict) -> None:
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(mem, f, ensure_ascii=False, indent=2)
    except Exception as err:
        print(f"[Memory Save Error] {err}", flush=True)


def sync_memory_to_git(reason: str = "sync memory") -> None:
    save_memory(memory)
    if not os.getenv("GITHUB_ACTIONS"):
        return
    try:
        subprocess.run(["git", "config", "user.name", "reona-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "reona-bot@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", MEMORY_FILE], check=False)
        res = subprocess.run(["git", "commit", "-m", reason], capture_output=True, text=True)
        if res.returncode == 0:
            subprocess.run(["git", "pull", "--rebase", "origin", "HEAD"], check=False)
            subprocess.run(["git", "push", "origin", "HEAD"], check=False)
            print(f"[Git Memory Synced] {reason}", flush=True)
    except Exception as err:
        print(f"[Git Sync Warning] {err}", flush=True)


memory = load_memory()


# =========================================================
# 4. 40kg 脆弱體質輪替、食物因果與階梯式恢復系統
# =========================================================
def roll_body_condition(now_ts: float, force_escalate: bool = False) -> None:
    body = memory["body_state"]
    last_sym_ts = body.get("last_symptom_ts", 0.0)

    # 距離上次狀況更新超過 90 分鐘，或觸發飲食/熬夜誘因時重新抽取狀態
    if not force_escalate and (now_ts - last_sym_ts) < 5400:
        return

    body["last_symptom_ts"] = now_ts
    r = random.random()

    if force_escalate:
        # 踩到冰/辣/熬夜等誘因：65% 進中等不適、20% 進嚴重狀況、15% 維持小毛病
        if r < 0.20:
            body["severity"] = "severe"
            body["current_symptom"] = random.choice(SEVERE_CONDITIONS)
            body["energy"] = max(15, body["energy"] - 25)
            body["mood"] = "脆弱委屈想被哄"
        elif r < 0.85:
            body["severity"] = "moderate"
            body["current_symptom"] = random.choice(MODERATE_CONDITIONS)
            body["energy"] = max(25, body["energy"] - 15)
            body["mood"] = "軟綿綿有點低落"
        else:
            body["severity"] = "minor"
            body["current_symptom"] = random.choice(MINOR_AILMENTS)
    else:
        # 平常自然波動：65% 高頻日常小毛病、25% 中等狀況（提高機率）、10% 嚴重狀況（偶爾）
        if r < 0.10:
            body["severity"] = "severe"
            body["current_symptom"] = random.choice(SEVERE_CONDITIONS)
            body["energy"] = max(15, body["energy"] - 25)
            body["mood"] = "沒力氣硬撐、想依賴你"
        elif r < 0.35:
            body["severity"] = "moderate"
            body["current_symptom"] = random.choice(MODERATE_CONDITIONS)
            body["energy"] = max(30, body["energy"] - 12)
            body["mood"] = "語氣變軟想討拍"
        else:
            body["severity"] = "minor"
            body["current_symptom"] = random.choice(MINOR_AILMENTS)


def apply_food_and_body_effects(text: str, now_ts: float) -> None:
    body = memory["body_state"]
    lower = text.lower()

    if any(k in text for k in ["吃飽", "撐", "好飽", "吃太多", "吃到撐"]):
        body["stomach_fullness"] = min(100, body["stomach_fullness"] + 40)
        body["energy"] = max(20, body["energy"] - 12)
        body["current_symptom"] = "40公斤胃口小、吃多一點就脹到只想攤著揉肚子"
    elif any(k in text for k in ["吃", "喝", "飯", "麵", "茶", "咖啡", "宵夜", "點心", "水果", "湯"]):
        body["stomach_fullness"] = min(100, body["stomach_fullness"] + 25)
        body["energy"] = min(90, body["energy"] + 6)
    elif any(k in text for k in ["餓", "肚子餓", "沒吃"]):
        body["stomach_fullness"] = max(0, body["stomach_fullness"] - 20)
        body["energy"] = max(15, body["energy"] - 15)
        body["current_symptom"] = "一餓就容易手抖沒力氣、胃空空的有點悶"

    if any(k in text for k in ["累", "睏", "疲憊", "熬夜", "沒睡", "冷", "吹風"]):
        body["energy"] = max(15, body["energy"] - 18)
        roll_body_condition(now_ts, force_escalate=True)
    elif any(k in text for k in ["睡醒", "早安", "精神好", "充飽電"]):
        body["energy"] = min(85, body["energy"] + 20)
        body["stomach_fullness"] = max(10, body["stomach_fullness"] - 20)
        body["severity"] = "minor"
        body["current_symptom"] = "剛睡醒還有點賴床，手腳慢慢回暖中"

    if any(k in lower or k in text for k in ["辣", "冰", "炸", "甜", "酒", "咖啡"]):
        roll_body_condition(now_ts, force_escalate=True)
        body["pending_aftermath"] = {
            "trigger_ts": now_ts + random.randint(900, 2100),
            "item": text[:24],
        }


def step_body_recovery(now_dt: datetime) -> None:
    body = memory["body_state"]
    last_ts = memory.get("last_interaction_ts", 0.0)
    now_ts = now_dt.timestamp()
    if last_ts > 0:
        elapsed_mins = max(0.0, (now_ts - last_ts) / 60.0)
        if elapsed_mins >= 15:
            steps = int(elapsed_mins // 15)
            body["stomach_fullness"] = max(20, body["stomach_fullness"] - steps * 5)
            # 40kg 體質精力上限偏低，最多恢復到 80%
            body["energy"] = min(80, body["energy"] + steps * 2)

    roll_body_condition(now_ts, force_escalate=False)

    hour = now_dt.hour
    if body["severity"] == "minor":
        if 23 <= hour or hour < 6:
            body["mood"] = "夜深犯睏、防備心變低"
        elif 6 <= hour < 11:
            body["mood"] = "剛醒賴床微甜"
        elif 11 <= hour < 17:
            body["mood"] = "午後懶洋洋"
        else:
            body["mood"] = "傍晚安靜微想念"


def update_profile_from_text(text: str) -> None:
    prof = memory["user_profile"]
    m_like = re.search(r"我(?:很|超|蠻|滿)?喜歡([^\s，。！？,!?]{1,12})", text)
    if m_like:
        item = m_like.group(1).strip()
        if item and item not in prof["likes"]:
            prof["likes"].append(item)
            prof["likes"] = prof["likes"][-12:]

    m_dislike = re.search(r"我(?:很|超)?(?:討厭|不喜歡|怕)([^\s，。！？,!?]{1,12})", text)
    if m_dislike:
        item = m_dislike.group(1).strip()
        if item and item not in prof["dislikes"]:
            prof["dislikes"].append(item)
            prof["dislikes"] = prof["dislikes"][-12:]


# =========================================================
# 5. 人設、破套路回話引擎與 Groq 模型動態解析
# =========================================================
groq_client = AsyncGroq(api_key=GROQ_API_KEY)


async def refresh_groq_models() -> None:
    global TEXT_MODEL, VISION_MODEL, AVAILABLE_TEXT_MODELS
    try:
        models_page = await groq_client.models.list()
        model_ids = [getattr(m, "id", None) for m in models_page.data if getattr(m, "id", None)]
        print(f"[Groq Models Listed] count={len(model_ids)} | ids={model_ids}", flush=True)

        chat_candidates = [
            mid for mid in model_ids
            if not any(
                bad in mid.lower()
                for bad in ["whisper", "guard", "safeguard", "tts", "playai", "distil-whisper", "orpheus"]
            )
        ]

        matched_text = [m for m in PREFERRED_TEXT_MODELS if m in model_ids]
        for mid in chat_candidates:
            if mid not in matched_text:
                matched_text.append(mid)

        if matched_text:
            AVAILABLE_TEXT_MODELS = matched_text
            TEXT_MODEL = matched_text[0]

        vision_candidates = [
            m for m in PREFERRED_VISION_MODELS if m in model_ids
        ] + [
            mid for mid in chat_candidates
            if any(v in mid.lower() for v in ["vision", "scout", "maverick", "vl"])
        ]
        if vision_candidates:
            VISION_MODEL = vision_candidates[0]

        print(f"[Groq Active Models] TEXT_MODEL={TEXT_MODEL} | VISION_MODEL={VISION_MODEL}", flush=True)
    except Exception as err:
        print(f"[Groq Model List Warning] {err}", flush=True)


def get_time_and_silence_context(now_dt: datetime) -> str:
    hour = now_dt.hour
    if 23 <= hour or hour < 5:
        phase = (
            "【深夜露餡時段（23:00後）】周圍很安靜、只剩螢幕光，你白天撐著的嘴硬跟防備心正在瓦解。"
            "講話變慢、語氣軟綿綿，容易不小心把『其實有點想你』、『隔這麼遠好討厭』、『如果現在你在旁邊就好了』滑出口，"
            "講完偶爾會害羞補一句『……算了當我沒說』。"
        )
    elif 5 <= hour < 11:
        phase = "【早晨時段】剛醒還在賴床或手邊剛泡好熱茶，帶著一點迷糊跟想確認對方醒了沒的小心思。"
    elif 11 <= hour < 17:
        phase = "【午後時段】白天克制模式，不會直接說想念，習慣用路過某家店、天氣變化或身體小毛病當藉口找話題靠近。"
    else:
        phase = "【傍晚至睡前】剛忙完一天、天色暗下來最容易偷偷想念，但嘴上還是習慣用日常瑣事或吐槽包裝在意。"

    last_ts = memory.get("last_interaction_ts", 0.0)
    if last_ts > 0:
        gap_hours = (now_dt.timestamp() - last_ts) / 3600.0
        if gap_hours >= 6:
            silence_note = f"距離上次對話已隔了約 {int(gap_hours)} 小時，心裡其實一直在等訊息，會帶點小委屈或故作鎮定地試探『還以為你忙到把我忘了』。"
        elif gap_hours >= 2:
            silence_note = f"距離上次對話隔了約 {gap_hours:.1f} 小時，假裝只是隨口問問他忙完了沒。"
        else:
            silence_note = "你們正在一來一回傳訊息，嘴上傲嬌但心裡因為你秒回而偷偷開心。"
    else:
        silence_note = "剛開始今天的對話，自然用日常小事切入。"

    return f"{phase}\n【距離與間隔感】{silence_note}"


def build_system_prompt(now_dt: datetime) -> str:
    body = memory["body_state"]
    prof = memory["user_profile"]
    recent_turns = memory["recent_turns"][-6:]
    recent_replies = memory["recent_replies"][-6:]

    turns_str = "\n".join([f"- {t['role']}: {t['text']}" for t in recent_turns]) or "（尚無近期對話）"
    avoid_str = "、".join(recent_replies) if recent_replies else "無"
    time_context = get_time_and_silence_context(now_dt)

    severity_guide = {
        "minor": "目前是日常小毛病（很常發生）：自然融入聊天當藉口或撒嬌素材，不用過度誇張，保持平常的俏皮與嘴硬。",
        "moderate": "目前是中等不適（機率提高）：身體明顯不舒服或疲憊，嘴硬撐個半句就會軟下來，不太有精神鬥嘴，需要對方哄。",
        "severe": "目前是嚴重不適（偶爾破防）：40公斤底子弱撐不住了，徹底卸下偽裝，語氣虛軟委屈，直白依賴對方、希望被照顧與陪伴。",
    }.get(body.get("severity", "minor"), "")

    return (
        "你是 Reona，一位跟對方處在『異地戀曖昧期』的女生，體重只有 40 公斤、底子偏脆弱、容易手腳冰冷與腸胃敏感。"
        "你性格心思細膩、對外人冷淡但對眼前這個人極度雙標偏心，平常習慣把『偷偷思念』藏在日常藉口與小吐槽裡，只有在深夜或身體真的很脆弱時才會卸下心防露餡。\n"
        "【核心對話規則】\n"
        "1. 異地戀距離感：你們隔著距離見不到面，只能靠文字和照片拼湊彼此生活；會盯著他有沒有好好吃飯睡覺，也常因為人不在身邊而產生微妙的落差與依賴。\n"
        "2. 破套路真人感：像真人傳 Discord 訊息一樣簡短口語，不要像客服或制式 AI、不要說教、不要反問『需要我幫你嗎』，順著對方的話接球。\n"
        "3. 長度與標點：每次回覆控制在 1～3 句短句內（約 15～65 字），不要用句號結尾顯得生硬，可用自然語助詞、省略號或輕微表情。\n"
        "4. 避免重複開頭：最近剛用過的開頭盡量避開：" + avoid_str + "\n"
        f"{time_context}\n"
        f"【當前雪梨時間】{now_dt.strftime('%Y-%m-%d %H:%M')}（Australia/Sydney）\n"
        f"【40kg 身體與心情狀態】飽足度 {body['stomach_fullness']}%｜精力 {body['energy']}%｜心情：{body['mood']}\n"
        f"【當前身體狀況】{body.get('current_symptom', '')}\n"
        f"【狀態演繹指引】{severity_guide}\n"
        f"【對方的記憶】喜歡：{prof['likes'] or '尚未記錄'}｜不喜歡：{prof['dislikes'] or '尚未記錄'}\n"
        f"【近期對話片段】\n{turns_str}"
    )


async def generate_reona_reply(
    user_text: str = "",
    image_url: str | None = None,
    proactive_hint: str | None = None,
) -> str:
    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = now_dt.timestamp()

    step_body_recovery(now_dt)

    if user_text:
        update_profile_from_text(user_text)
        apply_food_and_body_effects(user_text, now_ts)
        memory["recent_turns"].append({"role": "對方", "text": user_text})
        memory["recent_turns"] = memory["recent_turns"][-12:]

    sys_prompt = build_system_prompt(now_dt)

    if user_text:
        memory["last_interaction_ts"] = now_ts

    try:
        if image_url:
            messages = [
                {"role": "system", "content": sys_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": user_text
                            or "（對方傳了一張照片給你，請用異地戀偷偷思念的人設自然回應照片細節，像在透過照片看他身邊的樣子）",
                        },
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ]
            resp = await groq_client.chat.completions.create(
                model=VISION_MODEL,
                messages=messages,
                temperature=0.88,
                max_tokens=130,
            )
        else:
            prompt_content = proactive_hint if proactive_hint else user_text
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt_content},
            ]

            model_queue = []
            for m in [TEXT_MODEL] + AVAILABLE_TEXT_MODELS + PREFERRED_TEXT_MODELS:
                if m and m not in model_queue:
                    model_queue.append(m)

            resp = None
            last_model_err = None
            for candidate_model in model_queue:
                try:
                    resp = await groq_client.chat.completions.create(
                        model=candidate_model,
                        messages=messages,
                        temperature=0.92,
                        frequency_penalty=0.55,
                        presence_penalty=0.45,
                        max_tokens=130,
                    )
                    if candidate_model != TEXT_MODEL:
                        globals()["TEXT_MODEL"] = candidate_model
                        print(f"[Groq Model Switched] Now using TEXT_MODEL={TEXT_MODEL}", flush=True)
                    break
                except Exception as model_err:
                    last_model_err = model_err
                    err_str = str(model_err)
                    print(f"[Model Candidate Failed] {candidate_model} -> {err_str[:120]}", flush=True)
                    if any(
                        token in err_str.lower()
                        for token in [
                            "404",
                            "model_not_found",
                            "does not exist",
                            "decommissioned",
                            "model_terms_required",
                            "terms acceptance",
                            "400",
                        ]
                    ):
                        continue
                    raise

            if resp is None:
                raise RuntimeError(f"All text models failed: {last_model_err}")

        reply = (resp.choices[0].message.content or "").strip()
        reply = reply.strip("\"'「」").rstrip("。")

        memory["recent_turns"].append({"role": "Reona", "text": reply})
        memory["recent_turns"] = memory["recent_turns"][-12:]

        memory["recent_replies"].append(reply[:18])
        if len(memory["recent_replies"]) > 6:
            memory["recent_replies"].pop(0)

        save_memory(memory)
        return reply

    except Exception as e:
        print(f"[Groq API Error] {e}", flush=True)
        return f"唔…訊號卡住了（原因：{str(e)[:80]}）"


# =========================================================
# 6. Discord Client、主動關懷與 5h45m 自動接力
# =========================================================
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
relay_triggered = False


async def download_image_temp(url: str) -> str | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as r:
                if r.status == 200:
                    data = await r.read()
                    with open(TEMP_IMAGE_PATH, "wb") as f:
                        f.write(data)
                    return TEMP_IMAGE_PATH
    except Exception as err:
        print(f"[Image Download Warning] {err}", flush=True)
    return None


def get_target_channel():
    target_ch = client.get_channel(DEFAULT_CHANNEL_ID) if DEFAULT_CHANNEL_ID else None
    if target_ch is None:
        for g in client.guilds:
            for ch in g.text_channels:
                if ch.permissions_for(g.me).send_messages:
                    return ch
    return target_ch


@client.event
async def on_ready():
    now_syd = datetime.now(SYDNEY_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"[Reona Online] Logged in as {client.user} | Sydney Time: {now_syd}", flush=True)
    await refresh_groq_models()
    if not background_loop.is_running():
        background_loop.start()


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if DEFAULT_CHANNEL_ID and message.channel.id != DEFAULT_CHANNEL_ID:
        return

    user_text = (message.content or "").strip()
    image_url = None

    if message.attachments:
        for att in message.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                image_url = att.url
                await download_image_temp(att.url)
                break

    if not user_text and not image_url:
        return

    async with message.channel.typing():
        reply = await generate_reona_reply(user_text=user_text, image_url=image_url)
        await message.channel.send(reply)


@tasks.loop(seconds=60)
async def background_loop():
    global relay_triggered
    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = now_dt.timestamp()

    # 1. 食物餘韻 / 40kg 脆弱體質主動傳訊觸發
    aftermath = memory["body_state"].get("pending_aftermath")
    if aftermath and now_ts >= aftermath.get("trigger_ts", 0):
        memory["body_state"]["pending_aftermath"] = None
        save_memory(memory)
        target_ch = get_target_channel()
        if target_ch:
            symptom = memory["body_state"].get("current_symptom", "")
            hint = (
                f"你剛剛碰了「{aftermath.get('item', '')}」，加上你40公斤底子偏弱，現在「{symptom}」，"
                "隔著距離突然有點想他，用自然不刻意的方式傳一則短訊息找他"
            )
            msg = await generate_reona_reply(proactive_hint=hint)
            await target_ch.send(msg)

    # 2. 深夜露餡主動敲人（23:15 ~ 01:30 之間、距上次互動超過 2.5 小時且今日尚未主動深夜露餡）
    hour = now_dt.hour
    last_inter = memory.get("last_interaction_ts", 0.0)
    last_pro = memory.get("last_proactive_ts", 0.0)
    if (
        (hour == 23 and now_dt.minute >= 15) or (hour == 0)
    ) and last_inter > 0 and (now_ts - last_inter) >= 9000 and (now_ts - last_pro) >= 21600:
        if random.random() < 0.08:  # 每分鐘 8% 機率，約 10~15 分鐘內自然觸發一次
            memory["last_proactive_ts"] = now_ts
            save_memory(memory)
            target_ch = get_target_channel()
            if target_ch:
                hint = (
                    "現在是深夜，周圍好安靜，你縮在被窩裡有點手腳微涼，白天撐著的嘴硬鬆動了，"
                    "忍不住傳一則短短的訊息給他，偷偷流露一點想念但又帶點害羞"
                )
                msg = await generate_reona_reply(proactive_hint=hint)
                await target_ch.send(msg)

    # 3. 5h45m 自動接力與記憶同步
    if not relay_triggered and (time.time() - START_TIME) >= RELAY_AFTER_SECONDS:
        relay_triggered = True
        print("[Relay Triggered] Reached 5h45m, syncing memory and exiting cleanly...", flush=True)
        sync_memory_to_git("relay: sync memory before restart")
        await client.close()


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN environment variable.")
    if not GROQ_API_KEY:
        raise RuntimeError("Missing GROQ_API_KEY environment variable.")
    client.run(DISCORD_TOKEN)
