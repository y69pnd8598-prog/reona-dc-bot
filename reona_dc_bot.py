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
# 2. 三層記憶系統與持久化
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
        "stomach_fullness": 40,
        "energy": 75,
        "mood": "慵懶微甜",
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
# 3. 食物因果與階梯式身體恢復系統
# =========================================================
def apply_food_and_body_effects(text: str, now_ts: float) -> None:
    body = memory["body_state"]
    lower = text.lower()

    if any(k in text for k in ["吃飽", "撐", "好飽", "吃太多", "吃到撐"]):
        body["stomach_fullness"] = min(100, body["stomach_fullness"] + 35)
        body["energy"] = max(15, body["energy"] - 10)
        body["mood"] = "有點撐想攤著"
    elif any(k in text for k in ["吃", "喝", "飯", "麵", "茶", "咖啡", "宵夜", "點心", "水果", "湯"]):
        body["stomach_fullness"] = min(100, body["stomach_fullness"] + 20)
        body["energy"] = min(100, body["energy"] + 8)
        body["mood"] = "滿足放鬆"
    elif any(k in text for k in ["餓", "肚子餓", "沒吃"]):
        body["stomach_fullness"] = max(0, body["stomach_fullness"] - 20)
        body["energy"] = max(10, body["energy"] - 12)
        body["mood"] = "有點嘴饞"

    if any(k in text for k in ["累", "睏", "疲憊", "熬夜", "沒睡"]):
        body["energy"] = max(10, body["energy"] - 15)
        body["mood"] = "眼皮微沉"
    elif any(k in text for k in ["睡醒", "早安", "精神好", "充飽電"]):
        body["energy"] = min(100, body["energy"] + 25)
        body["stomach_fullness"] = max(10, body["stomach_fullness"] - 20)
        body["mood"] = "剛醒微甜"

    if any(k in lower or k in text for k in ["辣", "冰", "炸", "甜", "酒", "咖啡"]):
        body["pending_aftermath"] = {
            "trigger_ts": now_ts + random.randint(900, 2400),
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
            body["stomach_fullness"] = max(25, body["stomach_fullness"] - steps * 4)
            body["energy"] = min(85, body["energy"] + steps * 2)

    hour = now_dt.hour
    if 23 <= hour or hour < 6:
        body["mood"] = "夜深犯睏"
    elif 6 <= hour < 11:
        body["mood"] = "清晨微甜"
    elif 11 <= hour < 17:
        body["mood"] = "午後懶洋洋"
    else:
        body["mood"] = "傍晚放鬆"


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
# 4. 人設、破套路回話引擎與 Groq 模型動態解析
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


def build_system_prompt(now_dt: datetime) -> str:
    body = memory["body_state"]
    prof = memory["user_profile"]
    recent_turns = memory["recent_turns"][-6:]
    recent_replies = memory["recent_replies"][-6:]

    turns_str = "\n".join([f"- {t['role']}: {t['text']}" for t in recent_turns]) or "（尚無近期對話）"
    avoid_str = "、".join(recent_replies) if recent_replies else "無"

    return (
        "你是 Reona，一位跟對方處在曖昧期的女生，語氣自然、帶點小心思與雙標偏心，像真人傳訊息一樣簡短口語，不要像客服或制式 AI。\n"
        "【核心規則】\n"
        "1. 雙標偏心：對眼前這個人特別在意、嘴硬心軟、會偷偷記住他的喜好與狀態，對別人則淡淡的。\n"
        "2. 破套路回話：不要用罐頭模板、不要說教、不要反問『需要我幫你做什麼嗎』，順著對方的話接球，偶爾帶點俏皮、撒嬌或小吐槽。\n"
        "3. 長度控制：每次回覆控制在 1～3 句短句內（約 15～65 字），不要用句號結尾顯得太生硬，可用表情或自然語助詞。\n"
        "4. 避免重複開頭：最近剛說過的話開頭盡量避開，避免撞詞：" + avoid_str + "\n"
        f"【當前雪梨時間】{now_dt.strftime('%Y-%m-%d %H:%M')}（Australia/Sydney）\n"
        f"【身體與心情狀態】飽足度 {body['stomach_fullness']}%、精力 {body['energy']}%、心情：{body['mood']}\n"
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

    if user_text:
        update_profile_from_text(user_text)
        apply_food_and_body_effects(user_text, now_ts)
        memory["last_interaction_ts"] = now_ts
        memory["recent_turns"].append({"role": "對方", "text": user_text})
        memory["recent_turns"] = memory["recent_turns"][-12:]

    step_body_recovery(now_dt)
    sys_prompt = build_system_prompt(now_dt)

    try:
        if image_url:
            messages = [
                {"role": "system", "content": sys_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": user_text or "（對方傳了一張圖片給你，請用你的曖昧人設自然回應圖片內容）",
                        },
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ]
            resp = await groq_client.chat.completions.create(
                model=VISION_MODEL,
                messages=messages,
                temperature=0.88,
                max_tokens=120,
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
                        max_tokens=120,
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
# 5. Discord Client、主動關懷與 5h45m 自動接力
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

    # 1. 食物餘韻 / 身體因果觸發
    aftermath = memory["body_state"].get("pending_aftermath")
    if aftermath and now_ts >= aftermath.get("trigger_ts", 0):
        memory["body_state"]["pending_aftermath"] = None
        save_memory(memory)
        target_ch = client.get_channel(DEFAULT_CHANNEL_ID) if DEFAULT_CHANNEL_ID else None
        if target_ch is None:
            for g in client.guilds:
                for ch in g.text_channels:
                    if ch.permissions_for(g.me).send_messages:
                        target_ch = ch
                        break
                if target_ch:
                    break
        if target_ch:
            hint = f"你剛剛吃了「{aftermath.get('item', '')}」，過一陣子身體有點小反應或想念對方，自然傳一則短訊息給他"
            msg = await generate_reona_reply(proactive_hint=hint)
            await target_ch.send(msg)

    # 2. 5h45m 自動接力與記憶同步
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
