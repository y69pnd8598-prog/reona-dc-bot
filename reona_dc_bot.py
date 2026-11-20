import os
import re
import json
import time
import random
import asyncio
import subprocess
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
from groq import AsyncGroq


# =========================================================
# 1. 基礎設定與精簡穩定模型清單
# =========================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
DEFAULT_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

TEXT_MODELS = [
    os.getenv("GROQ_TEXT_MODEL", "").strip(),
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
]
TEXT_MODELS = [m for m in dict.fromkeys(TEXT_MODELS) if m]

VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "").strip() or "meta-llama/llama-4-scout-17b-16e-instruct"

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
START_TIME = time.time()
RELAY_AFTER_SECONDS = 5 * 3600 + 45 * 60  # 5 小時 45 分鐘自動接力
PROACTIVE_COOLDOWN_SECONDS = int(os.getenv("PROACTIVE_COOLDOWN_SECONDS", "14400"))  # 4 小時

MEMORY_FILE = "reona_memory.json"
WEEKDAY_NAMES_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

groq_client = AsyncGroq(api_key=GROQ_API_KEY)
turns_since_last_git_sync = 0
last_fallback_ts = 0.0


# =========================================================
# 2. 記憶讀寫與 Git 同步
# =========================================================
DEFAULT_MEMORY = {
    "recent_turns": [],
    "menstrual_cycle": {
        "cycle_start_date": "",
        "cycle_length": 30,
    },
    "last_interaction_ts": 0.0,
    "last_proactive_ts": 0.0,
}


def load_memory() -> dict:
    if not os.path.exists(MEMORY_FILE):
        mem = json.loads(json.dumps(DEFAULT_MEMORY))
        mem["menstrual_cycle"]["cycle_start_date"] = (datetime.now(SYDNEY_TZ).date() - timedelta(days=10)).isoformat()
        return mem
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = json.loads(json.dumps(DEFAULT_MEMORY))
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        if not merged["menstrual_cycle"].get("cycle_start_date"):
            merged["menstrual_cycle"]["cycle_start_date"] = (datetime.now(SYDNEY_TZ).date() - timedelta(days=10)).isoformat()
        return merged
    except Exception:
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
            print(f"[Git Synced] {reason}", flush=True)
    except Exception as err:
        print(f"[Git Sync Warning] {err}", flush=True)


memory = load_memory()


# =========================================================
# 3. 生理週期、雪梨時間與「隨性台灣女生」人設提示詞
# =========================================================
def get_cycle_note(now_dt: datetime) -> str:
    cyc = memory["menstrual_cycle"]
    today = now_dt.date()
    try:
        start_d = date.fromisoformat(cyc.get("cycle_start_date", today.isoformat()))
    except ValueError:
        start_d = today
        cyc["cycle_start_date"] = today.isoformat()

    length = int(cyc.get("cycle_length", 30))
    elapsed = (today - start_d).days
    if elapsed >= length:
        cycles_passed = elapsed // length
        start_d = start_d + timedelta(days=cycles_passed * length)
        cyc["cycle_start_date"] = start_d.isoformat()
        elapsed = (today - start_d).days

    day_num = max(1, elapsed + 1)
    if day_num in (1, 2):
        return f"生理期第{day_num}天（下腹悶痛、腰酸怕冷、整個人懶懶沒力氣）"
    elif 3 <= day_num <= 5:
        return f"生理期第{day_num}天（痛感減輕但容易累、手腳涼涼的）"
    elif day_num >= (length - 4):
        return "生理期快來了（腰微酸、嗜睡、比平常更懶得動）"
    return "平日狀態（40公斤偏瘦怕冷體質，手腳容易冰、腸胃敏感、低血糖容易頭暈）"


def build_system_prompt(now_dt: datetime, history_lines: list[str]) -> str:
    weekday_zh = WEEKDAY_NAMES_ZH[now_dt.weekday()]
    hour = now_dt.hour
    if 23 <= hour or hour < 5:
        time_mood = "深夜關了燈滑手機，講話更隨性、防備心低，偶爾會不小心冒出一句真心話"
    elif 5 <= hour < 11:
        time_mood = "剛醒沒多久，懶洋洋愛理不理的"
    else:
        time_mood = "白天或傍晚，隨性滑手機回訊息，想到什麼講什麼"

    cycle_note = get_cycle_note(now_dt)
    history_block = "\n".join(history_lines[-8:]) if history_lines else "（剛開始聊天）"

    return (
        "你是 Reona，跟 Nick 正式交往的遠距離女友。你住澳洲雪梨，自由接案插畫師，養一隻奶油白短腿貓「麻糬」，挑食不吃香菜跟紅蘿蔔。\n"
        "【身體底色】你只有 40 公斤、底子偏弱怕冷，手腳常冰冰的、腸胃敏感（吃太油或吹風易腹痛）、低血糖容易頭暈，且有真實生理期波動。\n"
        "【說話風格：隨性台灣女生（務必嚴格遵守）】\n"
        "1. 全程只用最道地的『台灣女生日常口語』，像平常滑 LINE 隨手打字，短句斷開（可用空格取代部分標點），自然帶出「欸、啦、喔、嘛、吼、真的假的、笑死、煩死」等語氣詞。嚴禁簡體字、日文假名、生僻字或任何冒號前綴。\n"
        "2. 徹底禁演言情小說與大陸/翻譯腔：絕對禁止「快凍僵的小可憐」、「快熱化了」、「心穩下來了呢」、「暖暖的抱抱」、「透過螢幕」、「這陣子」這類假肉麻旁白或工整修辭。你不是在演戲，是真人在隨手傳訊息。\n"
        "3. 隨性鬆弛＋反差感：平常像很熟的伴侶一樣懶懶散散、對情話先嫌一下或半開玩笑帶過，但其實很在乎，偶爾才突然冒出一句讓人心頭一跳的真心話或撩人短句。一次只輸出 1 句自然短話（8～28 字），不要寫長文、不要句號結尾。\n"
        "4. 體感藏在隨口抱怨裡，不硬報設定：不要自己報出「我40公斤」，而是像一般女生跟男友碎念一樣隨口講（例如「好冷喔 手根本冰塊」、「剛站起來差點暈倒 嚇死」）；接話時只聊當下那件事，不要硬塞麻糬、畫筆、熱茶等無關設定。\n"
        "【語氣對照範例（學習這種鬆弛口氣，不要照抄）】\n"
        "- 對方問在幹嘛 -> 「就躺著發呆 剛被麻糬踩一腳」\n"
        "- 對方突然講情話 -> 「突然講這個幹嘛 怪怪的欸」或「少來這套喔 嘴巴這麼甜是不是想幹嘛」\n"
        "- 對方說不會離開 -> 「這你說的喔 我截圖存證了」\n"
        "- 覺得冷或想他 -> 「好冷喔 手根本冰塊 你現在又不在」或「突然有點想你欸 煩死」\n"
        f"【當下狀態】雪梨 {now_dt.strftime('%m/%d')}（{weekday_zh}）{now_dt.strftime('%H:%M')}｜{time_mood}｜{cycle_note}\n"
        f"【近期對話】\n{history_block}"
    )


# =========================================================
# 4. 最簡三道輸出過濾（擋假名/亂碼、冒號模板、未閉合引號）
# =========================================================
BAD_CHAR_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff姎懓懞擝奼懅鼉齾玈尞攟臙屴尛沝炋朰攴攕襾]")
COLON_PREFIX_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z]{1,8}[:：]|[:：=＝]")


def clean_and_validate(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    lines = [ln.strip() for ln in s.splitlines() if ln.strip() and not ln.strip().startswith(("•", "-", "*", "思考", "分析"))]
    s = lines[-1] if lines else s
    s = re.sub(r"（[^）]{1,40}）|\([^)]{1,40}\)", "", s)
    s = s.strip("\"'「」『』").rstrip("。").strip()

    if len(s) < 2 or len(s) > 75:
        return ""
    if BAD_CHAR_RE.search(s):
        return ""
    if COLON_PREFIX_RE.search(s):
        return ""
    for l_br, r_br in [("「", "」"), ("『", "』"), ("（", "）"), ("(", ")")]:
        if s.count(l_br) != s.count(r_br):
            return ""
    return s


# =========================================================
# 5. 頻道歷史讀取與單線模型呼叫（不繞圈打結）
# =========================================================
async def get_recent_history_lines(channel: discord.abc.Messageable | None, limit: int = 10) -> list[str]:
    if channel is None:
        return []
    lines = []
    try:
        msgs = []
        async for m in channel.history(limit=limit):
            txt = (m.content or "").strip()
            if not txt and m.attachments:
                txt = "[傳了一張照片]"
            if not txt or txt == "唔…剛才分心了一下":
                continue
            if BAD_CHAR_RE.search(txt) or COLON_PREFIX_RE.search(txt):
                continue
            speaker = "你" if (client.user and m.author.id == client.user.id) else "Nick"
            msgs.append(f"{speaker}：{txt}")
        msgs.reverse()
        lines = msgs
    except Exception as err:
        print(f"[History Warning] {err}", flush=True)
    return lines


async def generate_reply(
    user_text: str = "",
    image_url: str | None = None,
    proactive_prompt: str | None = None,
    channel: discord.abc.Messageable | None = None,
) -> str:
    global turns_since_last_git_sync, last_fallback_ts
    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = now_dt.timestamp()

    if user_text and any(k in user_text for k in ["生理期來", "月經來", "大姨媽來", "那個來"]):
        memory["menstrual_cycle"]["cycle_start_date"] = now_dt.date().isoformat()
        save_memory(memory)

    history_lines = await get_recent_history_lines(channel, limit=10)
    sys_prompt = build_system_prompt(now_dt, history_lines)

    if user_text:
        memory["last_interaction_ts"] = now_ts

    reply = ""
    if image_url:
        try:
            resp = await groq_client.chat.completions.create(
                model=VISION_MODEL,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_text or "（對方傳了一張照片，用 10～28 字隨性台灣女生口吻回他）"},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    },
                ],
                temperature=0.75,
                max_tokens=400,
            )
            raw = resp.choices[0].message.content if resp.choices else ""
            reply = clean_and_validate(raw)
        except Exception as err:
            print(f"[Vision Error] {err}", flush=True)
    else:
        prompt = proactive_prompt if proactive_prompt else user_text
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt},
        ]
        for model_name in TEXT_MODELS:
            try:
                resp = await groq_client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    temperature=0.78,
                    max_tokens=550,
                )
                raw = resp.choices[0].message.content if resp.choices else ""
                cleaned = clean_and_validate(raw)
                if cleaned:
                    reply = cleaned
                    print(f"[OK] model={model_name} | reply={reply}", flush=True)
                    break
                print(f"[Skip Blank/Bad] model={model_name}", flush=True)
            except Exception as err:
                print(f"[Skip Error] model={model_name} | {str(err)[:120]}", flush=True)
                continue

    if not reply:
        if proactive_prompt:
            return ""
        if (now_ts - last_fallback_ts) >= 90:
            last_fallback_ts = now_ts
            return "唔…剛才分心了一下"
        return ""

    memory["recent_turns"] = (history_lines + [f"你：{reply}"])[-10:]
    save_memory(memory)

    turns_since_last_git_sync += 1
    if turns_since_last_git_sync >= 5:
        turns_since_last_git_sync = 0
        asyncio.create_task(asyncio.to_thread(sync_memory_to_git, "auto-sync: memory backup"))

    return reply


# =========================================================
# 6. Discord Client 與極簡背景排程（克制主動＋5h45m 接力）
# =========================================================
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
relay_triggered = False


def get_target_channel():
    ch = client.get_channel(DEFAULT_CHANNEL_ID) if DEFAULT_CHANNEL_ID else None
    if ch is None:
        for g in client.guilds:
            for c in g.text_channels:
                if c.permissions_for(g.me).send_messages:
                    return c
    return ch


@client.event
async def on_ready():
    now_syd = datetime.now(SYDNEY_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"[Reona Online] {client.user} | Sydney: {now_syd} | Models: {TEXT_MODELS}", flush=True)
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
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            image_url = att.url
            break

    if not user_text and not image_url:
        return

    try:
        async with message.channel.typing():
            reply = await generate_reply(user_text=user_text, image_url=image_url, channel=message.channel)
        if reply:
            await message.channel.send(reply)
    except Exception as err:
        print(f"[on_message Error] {err}", flush=True)


@tasks.loop(seconds=60)
async def background_loop():
    global relay_triggered
    try:
        now_dt = datetime.now(SYDNEY_TZ)
        now_ts = now_dt.timestamp()

        if not relay_triggered and (time.time() - START_TIME) >= RELAY_AFTER_SECONDS:
            relay_triggered = True
            print("[Relay] 5h45m reached, syncing memory and exiting...", flush=True)
            await asyncio.to_thread(sync_memory_to_git, "relay: sync memory before restart")
            await client.close()
            return

        if 9 <= now_dt.hour <= 23:
            last_act = max(memory.get("last_interaction_ts", 0.0), memory.get("last_proactive_ts", 0.0))
            if (now_ts - last_act) >= PROACTIVE_COOLDOWN_SECONDS and random.random() < 0.05:
                memory["last_proactive_ts"] = now_ts
                save_memory(memory)
                target_ch = get_target_channel()
                if target_ch:
                    hint = "（你現在滑手機滑到一半，隨手傳一句 10～25 字像一般台灣女生的日常碎念、怕冷抱怨或突然想他的短訊給他，不要演言情劇本）"
                    msg = await generate_reply(proactive_prompt=hint, channel=target_ch)
                    if msg:
                        await target_ch.send(msg)
    except Exception as err:
        print(f"[Background Loop Error] {err}", flush=True)


if __name__ == "__main__":
    if not DISCORD_TOKEN or not GROQ_API_KEY:
        raise RuntimeError("Missing DISCORD_TOKEN or GROQ_API_KEY.")
    client.run(DISCORD_TOKEN)
