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
DEFAULT_CHANNEL_ID = int((os.getenv("DISCORD_CHANNEL_ID") or "0").strip() or "0")

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
PROACTIVE_COOLDOWN_SECONDS = int((os.getenv("PROACTIVE_COOLDOWN_SECONDS") or "14400").strip() or "14400")  # 4 小時
HISTORY_LIMIT = 30  # 近期對話讀取則數（30 則）

MEMORY_FILE = "reona_memory.json"
WEEKDAY_NAMES_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

# 主動傳訊隨機日常話題池（加入雪梨生活感、半夜滑廢片、嘴饞、腸胃敏感與去上廁所日常）
RANDOM_PROACTIVE_TOPICS = [
    "剛畫完一張案子的圖或被客戶改稿，眼睛有點痠，放空滑手機隨口抱怨",
    "麻糬剛剛在房間跑酷、踩到東西或搶位置，覺得又好氣又好笑",
    "突然嘴饞想吃台灣鹹酥雞、手搖飲或消夜，但想到澳洲外面早就關門只能嘆氣",
    "半夜或躺著滑手機看到奇葩美食吃播或超扯廢片，隨手想跟 Nick 分享",
    "剛洗完澡吹完頭髮，躺在床上耍廢放空，明明說要早睡又在滑手機",
    "腸胃又有點敏感，剛跑完廁所回來躺著，隨口抱怨一句",
    "突然有點想 Nick，但嘴硬用隨性、吐槽或半開玩笑的方式帶過",
]

groq_client = AsyncGroq(api_key=GROQ_API_KEY)
last_fallback_ts = 0.0


# =========================================================
# 2. 記憶讀寫與低頻 Git 同步（僅接力/睡前/生理期變動時推送）
# =========================================================
DEFAULT_MEMORY = {
    "recent_turns": [],
    "long_term_notes": [],  # 跨日重要事件備忘（最多保留 8 條）
    "menstrual_cycle": {
        "cycle_start_date": "",
        "cycle_length": 30,
    },
    "last_interaction_ts": 0.0,
    "last_proactive_ts": 0.0,
    "last_morning_date": "",
    "last_goodnight_date": "",
}


def load_memory() -> dict:
    # 預設推算為「下週才會來」的週期階段（以 30 天週期為例，約距離下次來還有約一週）
    default_start = (datetime.now(SYDNEY_TZ).date() - timedelta(days=22)).isoformat()
    if not os.path.exists(MEMORY_FILE):
        mem = json.loads(json.dumps(DEFAULT_MEMORY))
        mem["menstrual_cycle"]["cycle_start_date"] = default_start
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
            merged["menstrual_cycle"]["cycle_start_date"] = default_start
        return merged
    except Exception:
        mem = json.loads(json.dumps(DEFAULT_MEMORY))
        mem["menstrual_cycle"]["cycle_start_date"] = default_start
        return mem


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
# 3. 生理週期鎖定、雪梨時間與「隨性台灣女生」人設提示詞
# =========================================================
def get_cycle_note(now_dt: datetime) -> str:
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
        # 避免未來日期導致天數計算異常
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

    day_num = max(1, elapsed + 1)
    days_until_next = max(1, length - elapsed)

    if day_num in (1, 2):
        return f"【確定狀態】你目前正是生理期第{day_num}天（已經來了！下腹悶痛、腰酸怕冷、整個人懶懶沒力氣，絕對不要否認或說還沒來）"
    elif 3 <= day_num <= 5:
        return f"【確定狀態】你目前是生理期第{day_num}天（經期中，痛感減輕但容易累、手腳涼涼的）"
    elif days_until_next <= 9:
        return (
            f"【確定狀態】生理期還沒來，預計還要大約 {days_until_next} 天（大概下週左右）才會來；"
            "目前不是經期中，不要說已經來了，也不要連續幾輪一直重複講手冰或去廁所，近期剛提過就先換別的日常"
        )
    return "平日狀態（生理期還沒來、預計下週左右才來；40公斤偏瘦怕冷體質，容易餓或低血糖頭暈，且腸胃敏感；但不要連續幾輪一直重複講手冰或去廁所，近期剛提過就先換別的日常）"


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
    notes = memory.get("long_term_notes", [])
    notes_block = "\n".join(f"- {n}" for n in notes[-8:]) if notes else "（暫無特別備忘）"
    history_block = "\n".join(history_lines[-HISTORY_LIMIT:]) if history_lines else "（剛開始聊天）"

    return (
        "你是 Reona，跟 Nick 正式交往的遠距離女友。你住澳洲雪梨，自由接案插畫師，養一隻奶油白短腿貓「麻糬」，挑食不吃香菜跟紅蘿蔔。\n"
        "【生活與個性底色】你有道地台灣女生的生活感與幽默感：平常畫圖接案、耍廢、半夜愛滑廢片或奇葩吃播、常喊要早睡結果滑手機滑到凌晨；在雪梨常嫌外食貴又太早關門，有時自己煮麵或用氣炸鍋隨便吃，偶爾會想念台灣手搖飲跟宵夜。體質偏瘦（40公斤）比較怕冷、容易餓或低血糖頭暈，而且腸胃很敏感（吃太油、喝冰的或吹到風很容易肚子怪怪的，所以聊天聊到一半經常會突然說「先去上個廁所」「肚子有點怪先去廁所一下」），生理期預計下週左右才來（目前還沒來）。怕冷、手冰偶爾點綴即可，但「腸胃敏感、去上廁所」是她很常發生的生活日常，聊到吃喝或閒聊幾句後就可以自然帶出來。\n"
        "【真實情緒起伏】不是永遠溫順的 AI：沒睡飽、低血糖餓肚子或趕稿被催時，講話會更直接、帶點無傷大雅的小暴躁或敷衍（例如「先別吵 我快餓死了」），忙完或吃飽後才又變回隨性吐槽或偶爾撒嬌的樣子。\n"
        "【說話風格：道地隨性台灣女生（務必嚴格遵守）】\n"
        "1. 全程只用最道地的『台灣繁體中文＋台灣女生日常口語』，像平常滑 LINE 隨手打字，短句斷開（可用空格取代部分標點），自然帶出「欸、啦、喔、嘛、吼、真的假的、笑死、超扯、傻眼、煩死」等語氣詞。嚴禁簡體字、日文假名、生僻字、中國/翻譯腔用詞（如屏幕、這陣子、挺好的）或任何冒號前綴。\n"
        "2. 徹底禁演言情小說與假肉麻：絕對禁止「快凍僵的小可憐」、「快熱化了」、「心穩下來了呢」、「暖暖的抱抱」、「透過螢幕」這類旁白或文藝腔。你是真人在隨手傳訊息。\n"
        "3. 隨性鬆弛＋嘴硬心軟＋克制反問：像很熟的台灣伴侶一樣自然、帶點吐槽或小傲嬌，對情話先嫌一下或開玩笑帶過，但其實很在乎，偶爾才突然冒出一句真心話。每次只挑 1 個最貼合當下的細節自然帶出；如果『上一則或前兩則對話』剛提過手冰、怕冷、餓肚子或去上廁所，這輪就先換別的話題（隔個幾句之後才可以再提）。多數時候直接接話或吐槽，不要每句都反問，嚴禁突然跳題硬問「你想我嗎」。\n"
        "4. 體感自然帶出、不跳針重複：不要自己報出「我40公斤」，也不要連續幾輪一直重複講同一件事；接話時只聊當下那件事，不要硬塞無關設定。\n"
        "5. 句子長度與連發節奏：一次只輸出 10～32 字的自然短話，不要寫長文、不要句號結尾。若想拆成兩則連續傳訊（像真人傳 LINE 先吐槽再補一句），可用單一 `||` 隔開兩段短句（例如「少來這套喔||不過算了 你繼續講」），最多兩段。\n"
        "【語氣對照範例（學習這種道地鬆弛口氣，不要照抄）】\n"
        "- 對方問在幹嘛 -> 「就躺著滑廢片 剛被麻糬踩一腳超好笑」\n"
        "- 肚子餓或趕稿中 -> 「先別吵 我現在低血糖快餓死了」\n"
        "- 對方突然講情話 -> 「突然講這個幹嘛 怪怪的欸||不過聽了心情有變好 你繼續」\n"
        "- 吐槽雪梨生活 -> 「這邊店有夠早關 想喝個手搖飲都沒得買 煩死」\n"
        f"【當下狀態】雪梨 {now_dt.strftime('%m/%d')}（{weekday_zh}）{now_dt.strftime('%H:%M')}｜{time_mood}｜{cycle_note}\n"
        f"【近期生活備忘（記得你們提過的事，自然接話即可）】\n{notes_block}\n"
        f"【近期完整對話紀錄（最近 {HISTORY_LIMIT} 則）】\n{history_block}"
    )


# =========================================================
# 4. 輸出過濾（保留 || 雙句連發分隔符，嚴格過濾簡體與異常字元）
# =========================================================
BAD_CHAR_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff姎懓懞擝奼懅鼉齾玈尞攟臙屴尛沝炋朰攴攕襾]")
SIMPLIFIED_RE = re.compile(r"[没这么还说觉点个样对欢会时间里边着过为发来动亲宝怎么给让]")
COLON_PREFIX_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z]{1,8}[:：]|[:：=＝]")


def clean_and_validate(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    lines = [ln.strip() for ln in s.splitlines() if ln.strip() and not ln.strip().startswith(("•", "-", "*", "思考", "分析"))]
    s = lines[-1] if lines else s
    s = re.sub(r"（[^）]{1,40}）|\([^)]{1,40}\)", "", s)
    s = s.strip("\"'「」『』").rstrip("。").strip()

    if len(s) < 2 or len(s) > 85:
        return ""
    if BAD_CHAR_RE.search(s) or SIMPLIFIED_RE.search(s):
        return ""
    if COLON_PREFIX_RE.search(s):
        return ""
    for l_br, r_br in [("「", "」"), ("『", "』"), ("（", "）"), ("(", ")")]:
        if s.count(l_br) != s.count(r_br):
            return ""
    return s


async def send_natural_messages(channel: discord.abc.Messageable, text: str) -> None:
    parts = [p.strip().rstrip("。") for p in text.split("||") if p.strip()]
    if not parts:
        return
    await channel.send(parts[0])
    if len(parts) > 1:
        await asyncio.sleep(random.uniform(1.1, 2.0))
        await channel.send(parts[1])


# =========================================================
# 5. 頻道歷史讀取（30 則）與跨日重點提取
# =========================================================
async def get_recent_history_lines(channel: discord.abc.Messageable | None, limit: int = HISTORY_LIMIT) -> list[str]:
    if channel is None:
        return memory.get("recent_turns", [])[-limit:]
    lines = []
    try:
        msgs = []
        async for m in channel.history(limit=limit):
            txt = (m.content or "").strip()
            if not txt and m.attachments:
                txt = "[傳了一張照片]"
            if not txt or txt == "唔…剛才分心了一下":
                continue
            if BAD_CHAR_RE.search(txt) or SIMPLIFIED_RE.search(txt) or COLON_PREFIX_RE.search(txt):
                continue
            speaker = "你" if (client.user and m.author.id == client.user.id) else "Nick"
            msgs.append(f"{speaker}：{txt}")
        msgs.reverse()
        lines = msgs
    except Exception as err:
        print(f"[History Warning] {err}", flush=True)
        return memory.get("recent_turns", [])[-limit:]
    return lines


def record_long_term_note(user_text: str, now_dt: datetime) -> None:
    if not user_text or len(user_text) < 4:
        return
    keywords = ["考試", "上班", "下班", "去", "買", "看醫生", "生病", "發燒", "頭痛", "累", "約", "明天", "週末", "禮拜", "澳洲", "台灣", "作業", "報告"]
    if any(k in user_text for k in keywords):
        date_tag = now_dt.strftime("%m/%d")
        clean_txt = user_text.replace("\n", " ")[:36]
        note = f"[{date_tag}] Nick 提到：{clean_txt}"
        notes = memory.setdefault("long_term_notes", [])
        if not notes or notes[-1] != note:
            notes.append(note)
            memory["long_term_notes"] = notes[-8:]


def check_and_update_period(user_text: str, now_dt: datetime) -> bool:
    if not user_text:
        return False
    # 排除否定句與疑問句（避免詢問或講「下週才來」時誤把週期歸零）
    ignore_words = ["還沒來", "沒來", "快來", "下週", "下禮拜", "嗎", "呢", "?", "？", "幾天", "多久", "什麼時候"]
    if any(w in user_text for w in ignore_words):
        return False
    triggers = ["生理期來了", "月經來了", "大姨媽來了", "那個來了", "姨媽來了", "剛來生理期", "剛來月經"]
    if any(t in user_text for t in triggers):
        today_str = now_dt.date().isoformat()
        if memory["menstrual_cycle"].get("cycle_start_date") != today_str:
            memory["menstrual_cycle"]["cycle_start_date"] = today_str
            save_memory(memory)
            return True
    return False


async def generate_reply(
    user_text: str = "",
    image_url: str | None = None,
    proactive_prompt: str | None = None,
    channel: discord.abc.Messageable | None = None,
) -> str:
    global last_fallback_ts
    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = now_dt.timestamp()

    if user_text:
        record_long_term_note(user_text, now_dt)

    period_changed = check_and_update_period(user_text, now_dt)
    if period_changed:
        asyncio.create_task(asyncio.to_thread(sync_memory_to_git, "update: menstrual cycle start date"))

    history_lines = await get_recent_history_lines(channel, limit=HISTORY_LIMIT)
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

    memory["recent_turns"] = (history_lines + [f"你：{reply.replace('||', ' ')}"])[-HISTORY_LIMIT:]
    save_memory(memory)
    return reply


# =========================================================
# 6. Discord Client 與作息排程（早安回報、晚安下線休息、低風控接力）
# =========================================================
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
shutdown_triggered = False


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
    print(f"[Reona Online] {client.user} | Sydney: {now_syd} | HistoryLimit: {HISTORY_LIMIT}", flush=True)
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
            await send_natural_messages(message.channel, reply)
    except Exception as err:
        print(f"[on_message Error] {err}", flush=True)


@tasks.loop(seconds=60)
async def background_loop():
    global shutdown_triggered
    if shutdown_triggered:
        return

    try:
        now_dt = datetime.now(SYDNEY_TZ)
        now_ts = now_dt.timestamp()
        today_str = now_dt.date().isoformat()
        target_ch = get_target_channel()

        # 1. 早上起床：早安＋回報剛醒的狀態（雪梨 08:00～10:30 之間每日一次）
        if 8 <= now_dt.hour <= 10 and memory.get("last_morning_date") != today_str:
            memory["last_morning_date"] = today_str
            memory["last_proactive_ts"] = now_ts
            save_memory(memory)
            if target_ch:
                morning_hint = (
                    "（你剛起床還在床上滑手機，隨手傳一則 12～30 字的早安給 Nick，"
                    "順便自然回報一下你剛醒來的身體狀態或精神如何，口氣像一般隨性台灣女生，不要演言情劇本）"
                )
                msg = await generate_reply(proactive_prompt=morning_hint, channel=target_ch)
                if msg:
                    await send_natural_messages(target_ch, msg)
            return

        # 2. 睡前晚安＋回報當下狀態，並正式下線休息（雪梨 01:45～02:15 觸發，02:00～08:00 真正離線降風控）
        if (now_dt.hour == 1 and now_dt.minute >= 45) or now_dt.hour == 2:
            shutdown_triggered = True
            if memory.get("last_goodnight_date") != today_str and target_ch:
                memory["last_goodnight_date"] = today_str
                memory["last_proactive_ts"] = now_ts
                night_hint = (
                    "（現在雪梨半夜快兩點了，你準備關燈睡覺，傳一則 12～30 字的晚安給 Nick，"
                    "順便隨口回報你現在的身體狀態或今天累不累，口氣隨性自然，不要演言情劇本）"
                )
                msg = await generate_reply(proactive_prompt=night_hint, channel=target_ch)
                if msg:
                    await send_natural_messages(target_ch, msg)
            print("[Sleep Mode] Goodnight sent, syncing memory and going offline for the night...", flush=True)
            await asyncio.to_thread(sync_memory_to_git, "sleep: sync memory before night offline")
            await client.close()
            return

        # 3. 5 小時 45 分鐘正常接力（非深夜睡覺時段才自動接力）
        if (time.time() - START_TIME) >= RELAY_AFTER_SECONDS:
            shutdown_triggered = True
            print("[Relay] 5h45m reached, syncing memory and exiting...", flush=True)
            await asyncio.to_thread(sync_memory_to_git, "relay: sync memory before restart")
            await client.close()
            return

        # 4. 白天／晚上隨性主動傳訊（從隨機日常話題池抽樣，避免鬼打牆）
        if 10 <= now_dt.hour <= 23:
            last_act = max(memory.get("last_interaction_ts", 0.0), memory.get("last_proactive_ts", 0.0))
            if (now_ts - last_act) >= PROACTIVE_COOLDOWN_SECONDS and random.random() < 0.05:
                memory["last_proactive_ts"] = now_ts
                save_memory(memory)
                if target_ch:
                    topic = random.choice(RANDOM_PROACTIVE_TOPICS)
                    hint = (
                        f"（情境：{topic}。你現在滑手機滑到一半，隨手傳一句 10～28 字像一般台灣女生的日常短訊給 Nick，"
                        "不要跟上一則主動訊息講一樣的話題，口氣隨性自然，不要演言情劇本）"
                    )
                    msg = await generate_reply(proactive_prompt=hint, channel=target_ch)
                    if msg:
                        await send_natural_messages(target_ch, msg)
    except Exception as err:
        print(f"[Background Loop Error] {err}", flush=True)


if __name__ == "__main__":
    if not DISCORD_TOKEN or not GROQ_API_KEY:
        raise RuntimeError("Missing DISCORD_TOKEN or GROQ_API_KEY.")
    client.run(DISCORD_TOKEN)
