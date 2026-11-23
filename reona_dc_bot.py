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
# 1. Configuration & Key Pool
# =========================================================
DISCORD_BOT_TOKEN = (
    os.getenv("DISCORD_BOT_TOKEN")
    or os.getenv("DISCORD_TOKEN")
    or ""
).strip()

TARGET_CHANNEL_ID = int(
    (os.getenv("TARGET_CHANNEL_ID") or os.getenv("DISCORD_CHANNEL_ID") or "0").strip() or "0"
)


def load_groq_key_pool() -> list[str]:
    keys = []
    raw_multi = os.getenv("GROQ_API_KEY") or ""
    for part in re.split(r"[,;\s\n]+", raw_multi.strip()):
        if part.strip():
            keys.append(part.strip())
    for idx in range(1, 6):
        k = (os.getenv(f"GROQ_API_KEY_{idx}") or "").strip()
        if k:
            keys.append(k)
    return list(dict.fromkeys(keys))


GROQ_KEY_POOL = load_groq_key_pool()
_current_key_idx = 0
_key_cooldown_until: dict[str, float] = {}

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

# 預設候選模型，開機時還會自動向 Groq API 查詢實際可用模型來補強
ACTIVE_MODELS = [
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
]

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
WEEKDAY_NAMES_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
HISTORY_LIMIT = 16

BASE_DIR = Path(__file__).resolve().parent
MEMORY_FILE = BASE_DIR / "reona_memory.json"
AUDIO_DIR = BASE_DIR / "generated_audio"
AUDIO_DIR.mkdir(exist_ok=True)

# =========================================================
# 2. Daily Quests & Memory Management
# =========================================================
DAILY_QUEST_TEMPLATES = [
    "幫澳洲精品香氛品牌畫秋季聯名草稿，下午線上對稿",
    "完成獨立出版書籍封面精修，傍晚去雪梨港邊散步買花",
    "整理文創聯名打樣樣品與校色，中午去採光咖啡廳",
    "替高級甜點品牌設計禮盒插畫，下午在家邊點香氛邊畫圖",
    "把這週商業視覺專案定案交稿，晚上去海景餐廳吃晚餐",
    "為精品雜誌專欄畫雪梨生活插畫，午後在落地窗邊構思配色",
    "跟海外策展團隊確認限量版畫輸出規格，忙完去逛市集",
]

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
        if not merged["menstrual_cycle"].get("cycle_start_date"):
            merged["menstrual_cycle"]["cycle_start_date"] = default_start
        return merged
    except Exception as err:
        print(f"[Memory Load Warning] {err}", flush=True)
        mem = json.loads(json.dumps(DEFAULT_MEMORY))
        mem["menstrual_cycle"]["cycle_start_date"] = default_start
        return mem


def sync_memory_to_git_bg() -> None:
    global _last_git_sync_ts
    now = time.time()
    if now - _last_git_sync_ts < 90:
        return
    _last_git_sync_ts = now
    try:
        if not (BASE_DIR / ".git").exists():
            return
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=BASE_DIR, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], cwd=BASE_DIR, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "add", MEMORY_FILE.name], cwd=BASE_DIR, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=BASE_DIR, check=False)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", "chore: sync Reona memory"], cwd=BASE_DIR, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "push"], cwd=BASE_DIR, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def save_memory(mem: dict, push_git: bool = False) -> None:
    try:
        MEMORY_FILE.write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")
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
        save_memory(memory)

    hour = now_dt.hour
    if 6 <= hour < 12:
        q_obj["progress"] = "上午：剛起床喝咖啡、準備開始工作"
    elif 12 <= hour < 18:
        q_obj["progress"] = "下午：正在畫主線草稿，還沒吃晚餐"
    elif 18 <= hour < 23:
        q_obj["progress"] = "晚上：工作告一段落或已吃過晚餐，在公寓休息"
    else:
        q_obj["progress"] = "深夜：躺床上準備睡覺"
    return q_obj


def discover_available_groq_models() -> None:
    """開機時直接向 Groq API 查詢目前真正可用的聊天模型，避免 404"""
    global ACTIVE_MODELS
    if not GROQ_KEY_POOL:
        print("[Startup Error] GROQ_API_KEY 為空！", flush=True)
        return
    for idx, key in enumerate(GROQ_KEY_POOL):
        try:
            resp = requests.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                chat_ids = [
                    m["id"] for m in data
                    if isinstance(m.get("id"), str)
                    and not any(bad in m["id"].lower() for bad in ["whisper", "guard", "tts", "embed"])
                ]
                if chat_ids:
                    # 優先保留我們偏好的模型順序，其餘可用的排在後面備援
                    preferred = [m for m in ACTIVE_MODELS if m in chat_ids]
                    others = [m for m in chat_ids if m not in preferred][:4]
                    ACTIVE_MODELS = preferred + others
                    print(f"[Groq Models Discovered via Key#{idx+1}] {ACTIVE_MODELS}", flush=True)
                    return
            else:
                print(f"[Model Discovery Key#{idx+1} HTTP {resp.status_code}] {resp.text[:120]}", flush=True)
        except Exception as err:
            print(f"[Model Discovery Error Key#{idx+1}] {err}", flush=True)


# =========================================================
# 3. Clean Reply Parser & Transparent Groq Caller
# =========================================================
THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
ANY_TAG_RE = re.compile(r"\[[^\]]{1,20}\]")
COLON_PREFIX_RE = re.compile(r"^(?:Reona|Nick|助手|系統|回覆|我)[:：=＝]\s*", re.IGNORECASE)


def clean_reply_text(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    s = THINK_BLOCK_RE.sub("", s).strip()
    s = ANY_TAG_RE.sub("", s).strip()

    lines = [
        ln.strip()
        for ln in s.splitlines()
        if ln.strip() and not ln.strip().startswith(("•", "-", "*", "思考", "分析", "系統", "<think>"))
    ]
    if not lines:
        return ""

    cleaned = []
    for ln in lines[-2:]:
        ln = COLON_PREFIX_RE.sub("", ln).strip()
        ln = re.sub(r"（[^）]{1,40}）|\([^)]{1,40}\)", "", ln)
        ln = ln.strip("\"'「」『』").rstrip("。").strip()
        if ln:
            cleaned.append(ln)

    return " ".join(cleaned).strip()


def build_prompt(now_dt: datetime) -> str:
    weekday_zh = WEEKDAY_NAMES_ZH[now_dt.weekday()]
    quest_info = ensure_daily_main_quest(now_dt)
    notes = memory.get("long_term_notes", [])[-6:]
    notes_str = "；".join(notes) if notes else "無"
    history_str = "\n".join(memory.get("recent_turns", [])[-HISTORY_LIMIT:]) or "（尚無對話紀錄）"

    return (
        "你是 Reona（第一人稱「我」），Nick 的女友。家境優渥住雪梨海景公寓與畫室，無經濟壓力；"
        "專業能力強的商業插畫師，養挑食奶油貓「麻糬」。Nick 180cm/67kg/微黑。"
        "個性坦率真誠、絕不傲嬌（不說「哼/才沒有」）。"
        "請直接針對 Nick 的話做真實回應（禁止敷衍或跳針），一律用繁體中文＋道地台灣女生 LINE 短句（10～32字），"
        "用空格斷句，自然帶「欸、啦、喔、嘛、真的假的、好好笑、好喔、超讚」，禁簡體字、禁冒號前綴、禁第三人稱自稱「寶貝」。"
        f"\n雪梨現在時間：{now_dt.strftime('%m/%d')}（{weekday_zh}）{now_dt.strftime('%H:%M')}｜主線：{quest_info['quest']}（{quest_info['progress']}）"
        f"\n長期記憶：{notes_str}"
        f"\n先前對話記憶：\n{history_str}"
    )


def call_groq_transparent(sys_prompt: str, user_prompt: str, max_tokens: int = 220) -> tuple[str, str]:
    """回傳 (回覆文字, 失敗原因摘要)。絕不用假備援句敷衍。"""
    global _current_key_idx
    if not GROQ_KEY_POOL:
        return "", "未設定 GROQ_API_KEY"

    now_ts = time.time()
    num_keys = len(GROQ_KEY_POOL)
    last_err_reason = "未知錯誤"

    for step in range(num_keys):
        idx = (_current_key_idx + step) % num_keys
        api_key = GROQ_KEY_POOL[idx]
        if now_ts < _key_cooldown_until.get(api_key, 0.0):
            last_err_reason = f"Key#{idx+1} 冷卻中"
            continue

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        for model_name in ACTIVE_MODELS:
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
                    timeout=15,
                )
                if resp.status_code == 429:
                    _key_cooldown_until[api_key] = now_ts + 600
                    _current_key_idx = (idx + 1) % num_keys
                    last_err_reason = f"Key#{idx+1} 429 額度/速率上限"
                    print(f"[Groq 429] Key#{idx+1} 切換至 Key#{_current_key_idx+1}", flush=True)
                    break
                if resp.status_code != 200:
                    err_snippet = resp.text[:140].replace("\n", " ")
                    last_err_reason = f"HTTP {resp.status_code} ({model_name})"
                    print(f"[Groq Fail] Key#{idx+1} {model_name} -> {resp.status_code}: {err_snippet}", flush=True)
                    continue

                data = resp.json()
                raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                cleaned = clean_reply_text(raw)
                if cleaned:
                    _current_key_idx = idx
                    print(f"[Groq OK] Key#{idx+1} | {model_name} -> {cleaned}", flush=True)
                    return cleaned, ""
                else:
                    last_err_reason = f"模型回傳空白 ({model_name})"
                    print(f"[Groq Empty] Key#{idx+1} {model_name} raw={raw[:100]!r}", flush=True)
            except Exception as err:
                last_err_reason = f"連線異常: {err}"
                print(f"[Groq Exception] Key#{idx+1} {model_name}: {err}", flush=True)

    return "", last_err_reason


# =========================================================
# 4. Quota-Guarded TTS & Video Call Helper
# =========================================================
def synthesize_fish_audio_file(text: str, now_dt: datetime) -> Path | None:
    if not FISH_API_KEY or not text:
        return None
    day_key = now_dt.strftime("%Y-%m-%d")
    month_key = now_dt.strftime("%Y-%m")
    q = memory.setdefault("tts_quota", {})
    if q.get("day_key") != day_key:
        q["day_key"] = day_key
        q["day_chars"] = 0
    if q.get("month_key") != month_key:
        q["month_key"] = month_key
        q["month_chars"] = 0

    avail = min(
        max(0, DAILY_TTS_CHAR_LIMIT - int(q.get("day_chars", 0))),
        max(0, MONTHLY_TTS_CHAR_LIMIT - int(q.get("month_chars", 0))),
    )
    speech_text = re.sub(r"\s+", " ", text).strip()
    if not speech_text or len(speech_text) > avail:
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
            q["day_chars"] = int(q.get("day_chars", 0)) + len(speech_text)
            q["month_chars"] = int(q.get("month_chars", 0)) + len(speech_text)
            save_memory(memory)
            out_path = AUDIO_DIR / f"dc_voice_{int(time.time() * 1000)}.mp3"
            out_path.write_bytes(resp.content)
            for old in sorted(AUDIO_DIR.glob("dc_voice_*.mp3"), key=lambda p: p.stat().st_mtime)[:-10]:
                old.unlink(missing_ok=True)
            return out_path
    except Exception as err:
        print(f"[Fish TTS Error] {err}", flush=True)
    return None


def warmup_render_service() -> None:
    try:
        requests.get(f"{RENDER_BASE_URL}/health", timeout=6)
    except Exception:
        pass


# =========================================================
# 5. Discord Client
# =========================================================
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

VIDEO_CALL_RE = re.compile(r"(視訊|開鏡頭|打視訊|video\s*call|!視訊|/video)", re.IGNORECASE)
VOICE_REQ_RE = re.compile(r"(語音|聽你聲音|唸給我聽|講話給我聽|voice)", re.IGNORECASE)
processing_lock = asyncio.Lock()


async def hydrate_history_from_discord(channel: discord.abc.Messageable) -> None:
    if memory.get("recent_turns"):
        print(f"[Memory Loaded] 已載入 {len(memory['recent_turns'])} 筆歷史記憶", flush=True)
        return
    try:
        fetched = []
        async for msg in channel.history(limit=16):
            txt = (msg.content or "").strip()
            if not txt or "專屬視訊視窗" in txt or txt.startswith("（系統狀態"):
                continue
            speaker = "我" if msg.author.bot else "Nick"
            fetched.append(f"{speaker}：{txt.splitlines()[0][:60]}")
        if fetched:
            fetched.reverse()
            memory["recent_turns"] = fetched[-HISTORY_LIMIT:]
            save_memory(memory)
            print(f"[Memory Hydrated] 從 DC 頻道回補了 {len(fetched)} 筆歷史記憶", flush=True)
    except Exception as err:
        print(f"[Hydrate Error] {err}", flush=True)


@client.event
async def on_ready():
    await asyncio.to_thread(discover_available_groq_models)
    print(
        f"[Reona Ready] User={client.user} | Channel={TARGET_CHANNEL_ID} | Keys={len(GROQ_KEY_POOL)} | Models={ACTIVE_MODELS}",
        flush=True,
    )
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

    async with processing_lock:
        now_dt = datetime.now(SYDNEY_TZ)
        memory["last_user_msg_ts"] = now_dt.timestamp()

        if any(k in user_text for k in ["考試", "上班", "下班", "醫生", "生病", "頭痛", "累", "週末", "作業", "報告"]):
            note = f"[{now_dt.strftime('%m/%d')}] Nick：{user_text[:40]}"
            memory.setdefault("long_term_notes", []).append(note)
            memory["long_term_notes"] = memory["long_term_notes"][-8:]

        sys_prompt = build_prompt(now_dt)
        is_video_req = bool(VIDEO_CALL_RE.search(user_text))

        if is_video_req:
            asyncio.create_task(asyncio.to_thread(warmup_render_service))
            user_prompt = f"Nick 說：「{user_text}」（想跟你視訊），用自然溫柔台灣女生短句答應他，不要寫網址。"
        else:
            user_prompt = f"Nick 說：「{user_text}」"

        async with message.channel.typing():
            reply, err_reason = await asyncio.to_thread(call_groq_transparent, sys_prompt, user_prompt, 220)

        if not reply:
            # 透明回報狀態，絕不再用假裝聊天的敷衍句糊弄
            await message.channel.send(f"（系統狀態：目前所有 Groq 鑰匙皆無法取得回覆 — {err_reason}）")
            return

        # 更新並保存真實記憶
        turns = memory.setdefault("recent_turns", [])
        turns.append(f"Nick：{user_text}")
        turns.append(f"我：{reply}")
        memory["recent_turns"] = turns[-HISTORY_LIMIT:]
        save_memory(memory, push_git=True)

        final_content = reply
        if is_video_req:
            final_content = f"{reply}\n📹 專屬視訊視窗：{RENDER_BASE_URL}/?mode=call"

        want_voice = bool(VOICE_REQ_RE.search(user_text)) or (random.random() < BASE_VOICE_PROB)
        voice_file = await asyncio.to_thread(synthesize_fish_audio_file, reply, now_dt) if want_voice else None

        if voice_file and voice_file.exists():
            await message.channel.send(content=final_content, file=discord.File(str(voice_file), filename="reona_voice.mp3"))
        else:
            await message.channel.send(content=final_content)


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
        sys_prompt = build_prompt(now_dt)
        prompt = f"主動傳一句 10～24 字台灣女生 LINE 短訊給 Nick，自然分享你現在在忙「{quest_info['quest']}」或問他在幹嘛。"

        reply, _ = await asyncio.to_thread(call_groq_transparent, sys_prompt, prompt, 180)
        if not reply:
            return

        recent_p = memory.setdefault("recent_proactive_msgs", [])
        if reply in recent_p[-3:]:
            return
        recent_p.append(reply)
        memory["recent_proactive_msgs"] = recent_p[-5:]
        memory["last_proactive_ts"] = now_ts

        turns = memory.setdefault("recent_turns", [])
        turns.append(f"我：{reply}")
        memory["recent_turns"] = turns[-HISTORY_LIMIT:]
        save_memory(memory, push_git=True)

        want_voice = random.random() < PROACTIVE_VOICE_PROB
        voice_file = await asyncio.to_thread(synthesize_fish_audio_file, reply, now_dt) if want_voice else None
        if voice_file and voice_file.exists():
            await channel.send(content=reply, file=discord.File(str(voice_file), filename="reona_voice.mp3"))
        else:
            await channel.send(content=reply)


@proactive_loop.before_loop
async def before_proactive():
    await client.wait_until_ready()


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("請先設定 DISCORD_BOT_TOKEN 或 DISCORD_TOKEN")
    client.run(DISCORD_BOT_TOKEN)
