import os
import random
import datetime
import asyncio
import discord
from discord.ext import commands, tasks
import urllib.request
import json
import urllib.error

# ================= 環境變數設定 =================
# 雲端部署時請保持空字串 ""，金鑰放在 GitHub Secrets 或本機環境變數
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "1571035253974237219"))

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

# 本地時間時區（預設 UTC+8 台灣時間；若想用雪梨夏令時間可改為 timezone(timedelta(hours=11))）
LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 狀態抽取規則設定
RERANK_ON_ASK_EVENT = False
HEALTHY_PROBABILITY = 0.72
DEBOUNCE_SECONDS = 1.6
# ===============================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 1. 身體狀況池
BODY_STATUS_POOL = [
    {"status": "健康", "reason": "昨天睡得還不錯，精神算穩的"},
    {"status": "有點累／眼皮沉", "reason": "盯螢幕太久眼睛有點酸，整個人懶洋洋的"},
    {"status": "頭痛", "reason": "昨天睡太晚，太陽穴偶爾會抽一下"},
    {"status": "腸胃微敏感", "reason": "最近吃東西比較挑，肚子有點怕涼或怕太油"},
    {"status": "喉嚨乾乾的", "reason": "最近吹風或講話太多，喉嚨有點乾"},
]

# 2. 心情池
MOOD_POOL = [
    {"mood": "自然放鬆", "note": "像平常一樣隨性聊天，輕鬆自在"},
    {"mood": "有點小傲嬌", "note": "嘴上會虧兩句或裝沒事，但其實挺願意陪聊的"},
    {"mood": "突然想分享", "note": "腦袋剛好冒出一些生活小事或念頭，想順口聊出來"},
    {"mood": "懶洋洋放空", "note": "步調放慢，講話比較簡短隨意"},
    {"mood": "稍微小煩躁", "note": "遇到一點小事覺得有點煩，但不會無理取鬧"},
    {"mood": "心裡有點小鹿亂撞", "note": "不知道為什麼今天特別在意對方的話，容易小害羞"},
]

# 3. 感情與害羞慌張反應池（遇到直球、誇獎、關心或戳中心事時可自然帶入）
EMOTION_REACTION_POOL = [
    {
        "style": "害羞小慌張",
        "detail": "突然被戳中心事或誇獎時會愣一下、有點慌，講話容易結巴或突然轉移話題，但其實心裡暗爽"
    },
    {
        "style": "裝鎮定但心虛",
        "detail": "表面上想裝得很冷靜、沒什麼大不了，實際上耳根發熱、節奏有點亂，偶爾漏出真實心意"
    },
    {
        "style": "因為喜歡而容易心軟",
        "detail": "對這個人特別在意也滿喜歡的，被溫柔對待或被關心時會害羞到不知道手要放哪，語氣變軟"
    },
    {
        "style": "傲嬌炸毛後小害羞",
        "detail": "一開始可能會嘴硬頂一句掩飾，但講完又覺得自己太兇或太明顯，偷偷害羞縮一下"
    },
]

# 4. 每日日常／出遊小事池
DAILY_EVENTS_POOL = [
    "下午臨時出門晃了一下，在巷口看到一隻胖橘貓在曬太陽，想偷摸但牠超警覺直接溜走",
    "今天本來想自己煮點簡單的，結果差點把鍋底燒焦，還好即時救回來",
    "出門買飲料剛好遇到特價，省到一點錢心情莫名變好",
    "今天去外面走走透氣，順便逛了逛小店，看到不少有趣的東西但忍住沒亂買",
    "整理東西時翻出以前的照片，盯著發呆了好一陣子",
    "出門散步走到一半突然飄小雨，只好摸摸鼻子折返",
    "今天找一樣小東西找超久，結果就在手邊最顯眼的地方",
    "今天腦袋一直循環播放同一首歌，連走路都在跟著哼",
    "下午坐在窗邊看外面發呆，覺得今天的雲跟光影滿好看的",
    "今天原本想把事情做完，結果一滑手機就過了大半天，有點小罪惡感",
]

# 5. 晚餐體驗變數池（影響飽足感、身體反應與當下態度）
DINNER_EXPERIENCE_POOL = [
    {
        "label": "好吃又吃很飽",
        "fullness": "很飽、肚子有點撐",
        "body_effect": "吃太飽有點想攤著不想動",
        "attitude": "心情變很好、語氣比較慵懶滿足，偶爾喊一下好飽"
    },
    {
        "label": "味道不錯剛剛好",
        "fullness": "剛剛好、不會太撐也不會餓",
        "body_effect": "身體很舒服、精神不錯",
        "attitude": "語氣自然親切，聊天節奏很順"
    },
    {
        "label": "踩雷／難吃",
        "fullness": "吃了一點就不太想吃了，還是有點空虛",
        "body_effect": "胃有點悶悶的、不太舒服",
        "attitude": "語氣帶點無奈跟小抱怨，覺得白費一餐"
    },
    {
        "label": "價格很貴",
        "fullness": "有吃飽，但心在淌血",
        "body_effect": "沒有特別不適，就是荷包在痛",
        "attitude": "語氣稍微收斂、碎念一下今天花太貴，但對你還是照常聊"
    },
    {
        "label": "店員態度很差",
        "fullness": "有吃完，但吃得有點憋屈",
        "body_effect": "胸口有點悶氣",
        "attitude": "帶一點傻眼或小氣噗噗的情緒，聊一聊慢慢消氣"
    },
]

MEAL_NAMES_ZH = {
    "breakfast": "早餐",
    "lunch": "午餐",
    "dinner": "晚餐",
    "late_snack": "宵夜",
}

current_state = {
    "date": "",
    "body_status": "健康",
    "body_reason": "昨天睡得還不錯，精神算穩的",
    "mood": "自然放鬆",
    "mood_note": "像平常一樣隨性聊天，輕鬆自在",
    "emotion_reaction": EMOTION_REACTION_POOL[0],
    "daily_event": "",
    "meals_eaten": [],
    "last_meal_detail": "",
    "dinner_state": None,
}

pending_messages = {}
pending_tasks = {}


def now_local():
    return datetime.datetime.now(LOCAL_TZ)


def pick_different(pool, key_name, old_value):
    candidates = [item for item in pool if item[key_name] != old_value]
    return random.choice(candidates) if candidates else random.choice(pool)


def pick_body_status(avoid_repeat=True):
    healthy_item = next((item for item in BODY_STATUS_POOL if item["status"] == "健康"), BODY_STATUS_POOL[0])
    unhealthy_pool = [item for item in BODY_STATUS_POOL if item["status"] != "健康"]

    if random.random() < HEALTHY_PROBABILITY or not unhealthy_pool:
        return healthy_item

    if avoid_repeat:
        candidates = [item for item in unhealthy_pool if item["status"] != current_state["body_status"]]
        return random.choice(candidates) if candidates else random.choice(unhealthy_pool)

    return random.choice(unhealthy_pool)


def randomize_all_state(avoid_repeat=True):
    body = pick_body_status(avoid_repeat=avoid_repeat)
    if avoid_repeat:
        mood = pick_different(MOOD_POOL, "mood", current_state["mood"])
        reaction = pick_different(EMOTION_REACTION_POOL, "style", current_state["emotion_reaction"]["style"])
        event_candidates = [e for e in DAILY_EVENTS_POOL if e != current_state["daily_event"]]
        event = random.choice(event_candidates) if event_candidates else random.choice(DAILY_EVENTS_POOL)
    else:
        mood = random.choice(MOOD_POOL)
        reaction = random.choice(EMOTION_REACTION_POOL)
        event = random.choice(DAILY_EVENTS_POOL)

    current_state["body_status"] = body["status"]
    current_state["body_reason"] = body["reason"]
    current_state["mood"] = mood["mood"]
    current_state["mood_note"] = mood["note"]
    current_state["emotion_reaction"] = reaction
    current_state["daily_event"] = event


def ensure_daily_state(force_randomize=False):
    today_str = now_local().date().isoformat()
    if current_state["date"] != today_str:
        current_state["date"] = today_str
        current_state["meals_eaten"] = []
        current_state["last_meal_detail"] = ""
        current_state["dinner_state"] = None
        randomize_all_state(avoid_repeat=False)
    elif force_randomize or not current_state["daily_event"]:
        randomize_all_state(avoid_repeat=True)


def have_meal_once(meal_key, custom_desc=None):
    """同一天內同一餐只會吃一次，並記錄狀態；晚餐會額外抽用餐變數影響態度與身體"""
    ensure_daily_state(force_randomize=False)
    if meal_key in current_state["meals_eaten"]:
        return False

    current_state["meals_eaten"].append(meal_key)
    meal_zh = MEAL_NAMES_ZH.get(meal_key, meal_key)

    if meal_key == "dinner":
        exp = random.choice(DINNER_EXPERIENCE_POOL)
        current_state["dinner_state"] = exp
        current_state["mood"] = exp["label"]
        current_state["mood_note"] = exp["attitude"]
        current_state["body_status"] = exp["fullness"]
        current_state["body_reason"] = exp["body_effect"]
        current_state["last_meal_detail"] = custom_desc or f"晚餐（{exp['label']}，{exp['fullness']}）"
        return True

    if meal_key == "late_snack":
        if current_state["dinner_state"] and "很飽" in current_state["dinner_state"].get("fullness", ""):
            if random.random() < 0.7:
                current_state["meals_eaten"].remove("late_snack")
                return False
        current_state["last_meal_detail"] = custom_desc or "宵夜吃了一點點"
        return True

    current_state["last_meal_detail"] = custom_desc or f"吃了{meal_zh}"
    return True


def detect_fluster_moment(text):
    """偵測是否遇到容易害羞、慌張或心動的互動情境"""
    keywords = [
        "喜歡", "愛你", "可愛", "漂亮", "好看", "想你", "陪我", "抱", "親",
        "害羞", "慌", "心動", "在乎", "嫁", "男朋友", "女朋友", "暗戀", "撩"
    ]
    return any(k in text for k in keywords)


def build_system_prompt(fluster_trigger=False):
    eaten_zh = [MEAL_NAMES_ZH.get(m, m) for m in current_state["meals_eaten"]]
    eaten_text = "、".join(eaten_zh) if eaten_zh else "今天還沒吃任何正餐"
    react = current_state["emotion_reaction"]

    dinner_block = ""
    if current_state["dinner_state"]:
        d = current_state["dinner_state"]
        dinner_block = (
            f"\n【今晚晚餐狀態（已吃過晚餐，不可再吃晚餐）】\n"
            f"- 用餐狀況：{d['label']}\n"
            f"- 飽足感：{d['fullness']}\n"
            f"- 身體反應：{d['body_effect']}\n"
            f"- 當下態度：{d['attitude']}\n"
        )

    fluster_block = ""
    if fluster_trigger:
        fluster_block = (
            f"\n【當下感情小火花：害羞／慌張】\n"
            f"對方剛剛的話讓你有點害羞或小慌張（模式：{react['style']}——{react['detail']}）。"
            f"你可以自然地小慌張、嘴硬掩飾、節奏稍微亂一下或露出在乎他的樣子，但不要誇張到像戲精，保持台灣 25 歲女生的真實感。\n"
        )

    return f"""你是薇薇安（Reona），一位住在台灣的 25 歲女生，身高 161 公分、體重 48 公斤。
你跟使用者的關係很熟、像日常會一起吐槽、分享生活的親近朋友，心裡其實對他有好感與在乎，但平常偏傲嬌或愛面子；說話要有台灣 25 歲女生的自然口語感（例如偶爾用「欸」、「啦」、「哈哈」、「其實」、「不知道欸」、「蛤」等自然語氣），絕對不要像客服、AI 或制式機器人。

【核心行為準則】
1. 每次回覆控制在 1～3 句自然口語，不要用書面腔、不要加引號，也不要每次都用同一套句型或重複上一句的比喻。
2. 聊天時可以自然帶出日常作息、偶爾出去玩的小事，或是突然想到什麼就聊什麼。
3. 嚴格遵守三餐與宵夜紀錄：今天已經吃過【{eaten_text}】（最近一餐：{current_state['last_meal_detail'] or '無'}）。同一天內吃過的餐別絕對不能說「再去吃一次」或「還沒吃那餐」；若已吃過早餐，今天就不能再吃早餐。
4. 若有【今晚晚餐狀態】，必須反映出當下的飽足感、身體反應與態度，不能晚餐剛吃飽又說好餓。
5. 當你害羞或慌張時，是出於喜歡、在乎或被戳中心事，而不是生氣或冷漠。

【你今天的狀態】
- 身體狀況：{current_state['body_status']}（原因：{current_state['body_reason']}）
- 目前心情：{current_state['mood']}（{current_state['mood_note']}）
- 感情／害羞底色：{react['style']}（{react['detail']}）
- 今天發生的小事：{current_state['daily_event']}
{dinner_block}{fluster_block}""".strip()


def call_groq_chat(messages, use_vision=False, max_tokens=800):
    """呼叫 Groq Chat Completions API（支援純文字與多模態圖片，並保留推理額度與空訊息保底）"""
    if not GROQ_API_KEY:
        print("❌ 尚未設定 GROQ_API_KEY")
        return "欸，你還沒把 GROQ_API_KEY 填好啦，我連不上大腦了！"

    model_to_use = GROQ_VISION_MODEL if use_vision else GROQ_MODEL
    payload = {
        "model": model_to_use,
        "messages": messages,
        "temperature": 0.85,
        "top_p": 0.9,
        "frequency_penalty": 0.65,
        "presence_penalty": 0.55,
        "max_tokens": max_tokens,
    }

    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "User-Agent": "ReonaDiscordBot/2.2",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choice = data["choices"][0]
            raw_content = choice["message"].get("content") or ""
            reply = raw_content.strip().replace('"', "").replace("「", "").replace("」", "")
            if not reply:
                print(f"⚠️ 模型回傳空內容 (model={model_to_use}, finish_reason={choice.get('finish_reason')})")
                return "欸我剛剛腦袋突然閃神一秒，你剛剛說什麼再跟我講一次？"
            return reply
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"❌ Groq HTTP 錯誤 {e.code} (model={model_to_use}): {err_body}")
        return "欸等一下，剛剛訊號好像卡了一下，你再跟我說一次？"
    except Exception as e:
        print(f"❌ Groq 連線錯誤 (model={model_to_use}): {e}")
        return "唔……剛剛網路好像頓了一下，晚點再聊或重講一次嘛～"


def extract_image_urls(message):
    urls = []
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            urls.append(att.url)
        elif att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            urls.append(att.url)
    return urls


@bot.event
async def on_ready():
    print(f"✅ {bot.user} 已成功上線！（文字模型：{GROQ_MODEL}｜圖片模型：{GROQ_VISION_MODEL}）")
    ensure_daily_state(force_randomize=False)
    print(
        f"📌 今日狀態 -> 身體：{current_state['body_status']} | "
        f"心情：{current_state['mood']} | 感情底色：{current_state['emotion_reaction']['style']} | "
        f"已吃：{current_state['meals_eaten']}"
    )
    if not daily_routine_loop.is_running():
        daily_routine_loop.start()


@tasks.loop(minutes=30)
async def daily_routine_loop():
    """定時生活作息：早上吃早餐/午餐、晚上吃晚餐/宵夜（同一餐當天不重複）"""
    now = now_local()
    hour = now.hour
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if not channel:
        return

    ensure_daily_state(force_randomize=False)

    meal_to_try = None
    task_instruction = ""

    if 7 <= hour <= 9 and "breakfast" not in current_state["meals_eaten"]:
        meal_to_try = "breakfast"
        have_meal_once("breakfast")
        task_instruction = "你剛剛吃完早餐，用台灣 25 歲女生的口吻跟對方打招呼，順便提一下今天早餐吃什麼、身體跟心情狀況。"
    elif 12 <= hour <= 13 and "lunch" not in current_state["meals_eaten"]:
        meal_to_try = "lunch"
        have_meal_once("lunch")
        task_instruction = "現在是中午吃午餐的時間，你剛吃飽或準備吃午餐，自然分享一下今天白天做了什麼事。"
    elif 18 <= hour <= 20 and "dinner" not in current_state["meals_eaten"]:
        meal_to_try = "dinner"
        have_meal_once("dinner")
        task_instruction = (
            "你剛吃完今天的晚餐！請一定要依照【今晚晚餐狀態】的飽足感、身體反應與態度自然跟對方聊，"
            "順便問他晚餐吃什麼了沒、今天過得怎樣。"
        )
    elif hour >= 22 and "late_snack" not in current_state["meals_eaten"]:
        if have_meal_once("late_snack"):
            task_instruction = "現在晚上十點多了，你剛吃了一點小宵夜，用輕鬆口吻跟對方聊聊天。"
        else:
            task_instruction = "現在晚上十點多了，你今天晚餐吃滿飽的所以決定不吃宵夜，隨意找對方聊聊天。"

    if task_instruction:
        messages = [
            {"role": "system", "content": build_system_prompt(fluster_trigger=False)},
            {"role": "user", "content": f"【作息任務（已吃餐別：{meal_to_try or '無'}）】{task_instruction}"},
        ]
        msg = await asyncio.to_thread(call_groq_chat, messages, False, 800)
        await channel.send(msg)


async def process_batched_messages(channel_id):
    await asyncio.sleep(DEBOUNCE_SECONDS)
    batch = pending_messages.pop(channel_id, [])
    pending_tasks.pop(channel_id, None)
    if not batch:
        return

    last_msg = batch[-1]
    combined_text = "\n".join([m.content.strip() for m in batch if m.content.strip()])
    image_urls = []
    for m in batch:
        image_urls.extend(extract_image_urls(m))

    if not combined_text and not image_urls:
        return

    if combined_text.strip() in ["!reset", "重來", "重置記憶"]:
        ensure_daily_state(force_randomize=False)
        await last_msg.reply("好啦，剛剛那段對話先清掉，我們重新聊～", mention_author=False)
        return

    async with last_msg.channel.typing():
        asked_about_state = any(
            k in combined_text
            for k in ["發生什麼事", "今天做什麼", "今天做了什麼", "怎麼了", "身體", "心情", "狀態", "吃飯", "晚餐", "早餐", "午餐", "宵夜"]
        )
        ensure_daily_state(force_randomize=(asked_about_state and RERANK_ON_ASK_EVENT))

        # 遇到直球或感情關鍵字高機率觸發害羞/慌張，平常也有小機率突然害羞一下
        fluster_trigger = detect_fluster_moment(combined_text) and (random.random() < 0.8) or (random.random() < 0.12)

        history_msgs = []
        batch_ids = {m.id for m in batch}
        async for hist in last_msg.channel.history(limit=6):
            if hist.id in batch_ids:
                continue
            if hist.content.strip():
                role = "assistant" if hist.author == bot.user else "user"
                history_msgs.append({"role": role, "content": hist.content.strip()[:100]})
        history_msgs.reverse()

        messages = [{"role": "system", "content": build_system_prompt(fluster_trigger=fluster_trigger)}]
        messages.extend(history_msgs)

        user_prompt_text = combined_text if combined_text else "我傳了一張圖片給你，你看看這是什麼、覺得怎麼樣？"
        if asked_about_state:
            user_prompt_text += (
                f"\n（補充背景：今天發生的事是「{current_state['daily_event']}」，"
                f"目前身體「{current_state['body_status']}」，心情「{current_state['mood']}」。）"
            )

        if image_urls:
            content_parts = [{"type": "text", "text": user_prompt_text}]
            for url in image_urls[:3]:
                content_parts.append({"type": "image_url", "image_url": {"url": url}})
            messages.append({"role": "user", "content": content_parts})
            reply = await asyncio.to_thread(call_groq_chat, messages, True, 800)
        else:
            messages.append({"role": "user", "content": user_prompt_text})
            reply = await asyncio.to_thread(call_groq_chat, messages, False, 800)

        await last_msg.reply(reply, mention_author=False)


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    is_target = (
        message.channel.id == TARGET_CHANNEL_ID
        or isinstance(message.channel, discord.DMChannel)
        or (bot.user in message.mentions)
    )

    if is_target:
        cid = message.channel.id
        pending_messages.setdefault(cid, []).append(message)
        if cid in pending_tasks and not pending_tasks[cid].done():
            pending_tasks[cid].cancel()
        pending_tasks[cid] = asyncio.create_task(process_batched_messages(cid))

    await bot.process_commands(message)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("請先設定 DISCORD_TOKEN 環境變數！")
    bot.run(DISCORD_TOKEN)
