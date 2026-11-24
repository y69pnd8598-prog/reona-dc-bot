import asyncio
import io
import json
import os
import random
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
import requests
from discord.ext import tasks

from reona_bg_db import get_gi_causes, get_restroom_line, init_bg_db, pick_relevant_snippet

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
MEMORY_FILE = "reona_memory.json"
MEMORY_RESET_TOKEN = "reset_20261125_sqlite_slim_v2"

DISCORD_TOKEN = (os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or "").strip()
TARGET_CHANNEL_ID = int((os.getenv("TARGET_CHANNEL_ID") or os.getenv("DISCORD_CHANNEL_ID") or "0").strip() or "0")
GROQ_KEYS = [k.strip() for k in re.split(r"[,;\s]+", (os.getenv("GROQ_API_KEY") or "").strip()) if k.strip()]
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
FISH_API_KEY = (os.getenv("FISH_API_KEY") or "").strip()
FISH_MODEL_ID = (os.getenv("FISH_MODEL_ID") or "").strip()

HISTORY_LIMIT = 4
DEBOUNCE_SECONDS = 2.5
GROQ_MODELS = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "qwen/qwen3.8-27b"]
GEMINI_MODELS = ["gemini-2.5-flash-lite", "gemini-2.5-flash"]
WEEKDAYS = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

RESTROOM_PROBS = {"normal": 0.10, "mild": 0.20, "moderate": 0.30, "severe": 0.36}
RESTROOM_COOLDOWNS = {"normal": 1500, "mild": 1080, "moderate": 720, "severe": 600}
RESTROOM_MID_PROBS = {"normal": 0.30, "mild": 0.50, "moderate": 0.68, "severe": 0.80}

_key_idx = 0
_cooldowns: dict[str, float] = {}
_last_user_ts = 0.0
_next_proactive_ts = 0.0
_last_voice_ts = 0.0
_in_restroom = False
_pending_msgs: list[str] = []
_debounce_task: asyncio.Task | None = None
_api_lock = asyncio.Lock()


def default_mem() -> dict:
    return {
        "reset_token": MEMORY_RESET_TOKEN,
        "turn_counter": 0,
        "recent_turns": [],
        "gi_level": "normal",
        "gi_until": 0.0,
        "gi_cause": "",
        "restroom_date": "",
        "restroom_daily": 0,
        "restroom_total": 0,
        "last_restroom_ts": 0.0,
    }


def save_mem(mem: dict) -> None:
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(mem, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Mem Save Err] {e}", flush=True)


def load_mem() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            data = json.load(open(MEMORY_FILE, encoding="utf-8"))
            if isinstance(data, dict) and data.get("reset_token") == MEMORY_RESET_TOKEN:
                base = default_mem()
                base.update(data)
                return base
        except Exception as e:
            print(f"[Mem Load Err] {e}", flush=True)
    fresh = default_mem()
    save_mem(fresh)
    return fresh


mem = load_mem()


def active_gi(now_ts: float) -> tuple[str, str]:
    if now_ts < float(mem.get("gi_until", 0.0)) and mem.get("gi_level") != "normal":
        return mem["gi_level"], (mem.get("gi_cause") or "").strip()
    return "normal", ""


def update_gi(text: str, now_ts: float) -> str:
    cur_lvl, _ = active_gi(now_ts)

    # 吃藥、益生菌或喝溫水自動緩解一階
    if cur_lvl != "normal" and any(k in text for k in ("吃藥", "腸胃藥", "益生菌", "胃藥", "止瀉藥", "喝溫水", "喝熱水", "保溫瓶")):
        downgrade = {"severe": "moderate", "moderate": "mild", "mild": "normal"}
        new_lvl = downgrade[cur_lvl]
        mem["gi_level"] = new_lvl
        mem["gi_until"] = 0.0 if new_lvl == "normal" else now_ts + max(600.0, (float(mem["gi_until"]) - now_ts) * 0.5)
        if new_lvl == "normal":
            mem["gi_cause"] = ""
        save_mem(mem)
        return new_lvl

    lvl = "normal"
    if any(k in text for k in ("水瀉", "劇痛", "腿軟", "虛脫")):
        lvl = "severe"
    elif any(k in text for k in ("拉肚子", "腹瀉", "胃痛", "腹痛")):
        lvl = "moderate"
    elif any(k in text for k in ("胃悶", "反酸", "脹氣", "肚子", "腸胃")):
        lvl = "mild"

    ranks = {"normal": 0, "mild": 1, "moderate": 2, "severe": 3}
    if lvl != "normal" and ranks[lvl] >= ranks[cur_lvl]:
        holds = {"mild": 7200, "moderate": 14400, "severe": 25200}
        mem["gi_level"] = lvl
        mem["gi_until"] = now_ts + holds[lvl]
        if cur_lvl == "normal" or not mem.get("gi_cause"):
            mem["gi_cause"] = random.choice(get_gi_causes(lvl))
        save_mem(mem)
        return lvl
    return cur_lvl


def record_restroom(now_dt: datetime, now_ts: float) -> None:
    today = now_dt.strftime("%Y-%m-%d")
    if mem.get("restroom_date") != today:
        mem["restroom_date"] = today
        mem["restroom_daily"] = 0
    mem["restroom_daily"] = int(mem.get("restroom_daily", 0)) + 1
    mem["restroom_total"] = int(mem.get("restroom_total", 0)) + 1
    mem["last_restroom_ts"] = now_ts
    save_mem(mem)


def append_turn(role: str, text: str) -> None:
    clean = re.sub(r"\s+", " ", (text or "").strip())[:60]
    if not clean:
        return
    turns = mem.setdefault("recent_turns", [])
    turns.append(f"{role}:{clean}")
    mem["recent_turns"] = turns[-HISTORY_LIMIT:]
    save_mem(mem)


def is_bad_reply(txt: str) -> bool:
    if not txt or len(txt) < 2 or len(txt) > 60:
        return True
    if re.search(r"[a-zA-Z]{6,}|([\u4e00-\u9fff])\1{3,}", txt):
        return True
    return bool(re.search(r"單愛|盡力保護它安全|正在客廳餐桌上\s*等|無擔於|綠元|趕去選洗手間", txt))


def clean_reply(raw: str, strip_q: bool) -> str:
    if not raw:
        return ""
    txt = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE).strip()
    txt = re.sub(r"^(Reona|我)\s*[:：]\s*", "", txt)
    txt = re.sub(r"\bkitty\b", "麻糬", txt, flags=re.IGNORECASE)
    for ch in ("「", "」", '"', "。", "，"):
        txt = txt.replace(ch, " ")
    if strip_q:
        txt = re.sub(r"[^ ！？?]*[？?].*$", "", txt)
        txt = re.sub(r"\s+\S*(嗎|呢|還是|好不好|要不要|什麼|沒)$", "", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    if is_bad_reply(txt):
        print(f"[Blocked Bad Reply] {txt}", flush=True)
        return ""
    return txt


def build_prompt(now_dt: datetime, user_text: str, allow_q: bool) -> str:
    gi_lvl, gi_cause = active_gi(time.time())
    snippet = pick_relevant_snippet(user_text=user_text, gi_level=gi_lvl, has_active_action=_in_restroom)
    hist = " / ".join(mem.get("recent_turns", [])[-HISTORY_LIMIT:]) or "無"
    loc = "洗手間裡" if _in_restroom else ("房間床上" if now_dt.hour < 8 or now_dt.hour >= 23 else "客廳或畫室")
    q_rule = "可自然問一句。" if allow_q else "用陳述句自然接話，不要問問題。"
    gi_note = f"｜身體:{gi_lvl}({gi_cause})" if gi_lvl != "normal" else ""
    bg_note = f"\n背景線索：{snippet}" if snippet else ""

    return (
        "你是Reona(自稱我)，從台北來雪梨的自由插畫師，Nick的女友，只養一隻貓叫麻糬(不叫Kitty)。"
        f"溫柔黏人怕冷挑嘴，用自然台灣女生口語回覆(10~20字)，空格斷句，繁體中文。{q_rule}"
        f"\n時間：{now_dt.strftime('%m/%d')}{WEEKDAYS[now_dt.weekday()]}{now_dt.strftime('%H:%M')}｜位置：{loc}{gi_note}"
        f"{bg_note}\n近期對話：{hist}"
    )


def call_llm_with_retry(sys_p: str, user_p: str, strip_q: bool, attempts: int = 2) -> str:
    global _key_idx
    for attempt in range(attempts):
        now_ts = time.time()
        for step in range(len(GROQ_KEYS)):
            idx = (_key_idx + step) % len(GROQ_KEYS)
            key = GROQ_KEYS[idx]
            for mid in GROQ_MODELS:
                cd_key = f"{idx}:{mid}"
                if now_ts < _cooldowns.get(cd_key, 0.0):
                    continue
                try:
                    r = requests.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={
                            "model": mid,
                            "messages": [{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
                            "temperature": 0.65,
                            "max_tokens": 85,
                        },
                        timeout=9,
                    )
                    if r.status_code == 429:
                        _cooldowns[cd_key] = now_ts + 10.0
                        continue
                    if r.status_code == 200:
                        raw = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                        out = clean_reply(raw, strip_q)
                        if out:
                            _key_idx = idx
                            return out
                except Exception as e:
                    print(f"[Groq Err] {mid}: {e}", flush=True)

        if GEMINI_API_KEY:
            for mid in GEMINI_MODELS:
                try:
                    r = requests.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{mid}:generateContent",
                        params={"key": GEMINI_API_KEY},
                        headers={"Content-Type": "application/json"},
                        json={
                            "system_instruction": {"parts": [{"text": sys_p}]},
                            "contents": [{"role": "user", "parts": [{"text": user_p}]}],
                            "generationConfig": {"temperature": 0.65, "maxOutputTokens": 85},
                        },
                        timeout=8,
                    )
                    if r.status_code == 200:
                        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
                        raw = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                        out = clean_reply(raw, strip_q)
                        if out:
                            return out
                except Exception as e:
                    print(f"[Gemini Err] {mid}: {e}", flush=True)

        if attempt < attempts - 1:
            time.sleep(3.5)

    return "等我一下下 剛在發呆沒看清"


def synth_voice(text: str) -> bytes | None:
    global _last_voice_ts
    if not FISH_API_KEY or not FISH_MODEL_ID:
        return None
    spoken = "，".join(p.strip() for p in text.split(" ") if p.strip())[:65]
    if not spoken:
        return None
    try:
        r = requests.post(
            "https://api.fish.audio/v1/tts",
            headers={"Authorization": f"Bearer {FISH_API_KEY}", "Content-Type": "application/json"},
            json={"text": spoken, "reference_id": FISH_MODEL_ID, "format": "mp3"},
            timeout=15,
        )
        if r.status_code == 200 and r.content:
            _last_voice_ts = time.time()
            return r.content
    except Exception:
        pass
    return None


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@tasks.loop(minutes=1)
async def proactive_loop():
    global _next_proactive_ts
    if not TARGET_CHANNEL_ID or not client.is_ready():
        return
    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = time.time()
    if 1 <= now_dt.hour < 8:
        return
    if _next_proactive_ts == 0.0:
        _next_proactive_ts = now_ts + random.uniform(2700, 5400)
        return
    if now_ts < _next_proactive_ts or _pending_msgs or _in_restroom or _api_lock.locked() or (now_ts - _last_user_ts < 720):
        return
    _next_proactive_ts = now_ts + random.uniform(2700, 5400)
    ch = client.get_channel(TARGET_CHANNEL_ID) or await client.fetch_channel(TARGET_CHANNEL_ID)
    async with _api_lock:
        reply = await asyncio.to_thread(
            call_llm_with_retry, build_prompt(now_dt, "日常 麻糬 畫畫", False), "自然跟Nick分享你現在的日常或撒嬌一句。", True, 1
        )
    if reply:
        append_turn("Reona", reply)
        await ch.send(reply)


async def handle_batched_messages(channel: discord.abc.Messageable) -> None:
    global _last_user_ts, _in_restroom
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return
    if not _pending_msgs:
        return

    batch = _pending_msgs[:]
    _pending_msgs.clear()
    combined = "，".join(batch)

    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = time.time()
    _last_user_ts = now_ts
    append_turn("Nick", combined)

    if ("洗手間" in combined or "廁所" in combined) and ("幾次" in combined or "次數" in combined):
        ans = f"我今天去洗手間 {mem.get('restroom_daily', 0)} 次 累計 {mem.get('restroom_total', 0)} 次"
        append_turn("Reona", ans)
        await channel.send(ans)
        return

    lvl = update_gi(combined, now_ts)
    returned_from_restroom = False

    if not _in_restroom and (now_ts - float(mem.get("last_restroom_ts", 0.0)) > RESTROOM_COOLDOWNS[lvl]) and random.random() < RESTROOM_PROBS[lvl]:
        _in_restroom = True
        record_restroom(now_dt, now_ts)
        teaser = get_restroom_line("teaser", lvl)
        away_sec = random.uniform(55.0, 120.0)
        append_turn("Reona", teaser)
        await channel.send(teaser)

        # 洗手間途中提醒（依腸胃嚴重程度機率觸發）
        if random.random() < RESTROOM_MID_PROBS[lvl]:
            first_half = away_sec * random.uniform(0.42, 0.58)
            await asyncio.sleep(first_half)
            mid_msg = get_restroom_line("mid_ping", lvl)
            append_turn("Reona", mid_msg)
            await channel.send(mid_msg)
            await asyncio.sleep(max(15.0, away_sec - first_half))
        else:
            await asyncio.sleep(away_sec)

        # 若去洗手間期間 Nick 又傳了訊息，合併進來一起回覆
        if _pending_msgs:
            extra = "，".join(_pending_msgs)
            _pending_msgs.clear()
            combined = f"{combined}，{extra}"
            append_turn("Nick", extra)

        _in_restroom = False
        returned_from_restroom = True

    now_dt = datetime.now(SYDNEY_TZ)
    mem["turn_counter"] = int(mem.get("turn_counter", 0)) + 1
    allow_q = (mem["turn_counter"] % 4 == 0)
    save_mem(mem)

    user_prompt = (
        f"你剛從洗手間出來，Nick說：「{combined}」。自然接話回覆他。"
        if returned_from_restroom
        else f"Nick說：「{combined}」。自然回覆他。"
    )

    async with _api_lock:
        async with channel.typing():
            reply = await asyncio.to_thread(call_llm_with_retry, build_prompt(now_dt, combined, allow_q), user_prompt, not allow_q, 2)

    append_turn("Reona", reply)

    wants_voice = "語音" in combined or (time.time() - _last_voice_ts > 900 and random.random() < 0.08)
    voice = await asyncio.to_thread(synth_voice, reply) if wants_voice else None
    if voice:
        await channel.send(content=reply, file=discord.File(io.BytesIO(voice), filename="reona_voice.mp3"))
    else:
        await channel.send(reply)


@client.event
async def on_ready():
    init_bg_db().close()
    if not proactive_loop.is_running():
        proactive_loop.start()
    print(f"[Reona Slim Ready] {client.user} | SQLite=ON", flush=True)


@client.event
async def on_message(message: discord.Message):
    global _debounce_task, _last_user_ts
    if message.author.bot or (TARGET_CHANNEL_ID and message.channel.id != TARGET_CHANNEL_ID):
        return
    text = (message.content or "").strip() or ("[傳了照片]" if message.attachments else "")
    if not text:
        return

    _last_user_ts = time.time()
    _pending_msgs.append(text)
    if _in_restroom:
        return
    if _debounce_task and not _debounce_task.done():
        _debounce_task.cancel()
    _debounce_task = asyncio.create_task(handle_batched_messages(message.channel))


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("缺少 DISCORD_BOT_TOKEN")
    client.run(DISCORD_TOKEN)
