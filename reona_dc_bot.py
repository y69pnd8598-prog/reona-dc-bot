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
# 雲端部署時：請將下方預設值清空，改在雲端平台後台的 Environment Variables 填寫
# 本機測試時：可直接把值填在第二個引號裡
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "1571035253974237219"))

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

# 狀態抽取規則設定
RERANK_ON_ASK_EVENT = False   # False = 一天只抽一次；True = 每次問都重抽
HEALTHY_PROBABILITY = 0.7     # 0.7 = 70% 機率健康，30% 機率出現其他身體狀況
# ===============================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 1. 身體狀況池（含原因）
BODY_STATUS_POOL = [
    {"status": "健康", "reason": "昨天睡得很飽，精神超好！"},
    {"status": "發燒", "reason": "昨天出門忘記帶傘淋到雨，結果著涼有點發燒……"},
    {"status": "頭痛", "reason": "昨天熬夜太晚睡，太陽穴一直一跳一跳的……"},
    {"status": "咳嗽", "reason": "換季空氣太乾，加上好像被路人傳染，喉嚨好癢……"},
    {"status": "腸胃不舒服", "reason": "昨晚半夜偷吃太辣的宵夜，今天肚子一直在絞痛……"},
    {"status": "有點累／眼皮沉", "reason": "盯螢幕太久眼睛有點酸，整個人懶洋洋的"},
]

# 2. 心情池（含心情與小原因）
MOOD_POOL = [
    {"mood": "開心", "note": "心情很不錯，嘴角忍不住想上揚"},
    {"mood": "傲嬌得意", "note": "有點小得意，但表面還是要裝酷"},
    {"mood": "委屈／難受", "note": "有點小委屈，希望有人哄哄我"},
    {"mood": "放空／懶洋洋", "note": "腦袋有點放空，只想慢慢來"},
    {"mood": "煩躁／小鬧脾氣", "note": "有一點點小煩躁，講話比較容易炸毛"},
]

# 3. 每日隨機事件池
DAILY_EVENTS_POOL = [
    "下午在巷口看到一隻超胖的橘貓曬太陽，差點忍不住衝過去摸，結果牠一看到我就溜走了",
    "今天試著自己煮東西吃，結果差點把鍋底燒焦，還好及時救回來沒變黑暗料理",
    "出門買飲料的時候剛好遇到最後一杯特價，心情瞬間變超好",
    "整理房間時翻出以前的舊照片，害我一個人盯著螢幕發呆了半小時",
    "本來想出門散步透透氣，結果剛走到門口就開始飄毛毛雨，只好乖乖回家窩著",
    "今天走路差點踢到階梯絆倒，還好旁邊沒人看到，趕緊裝作若無其事走過去",
    "剛才想找某樣東西找了老半天，結果發現它就乖乖躺在手邊",
    "今天聽到一首超洗腦的歌，到現在腦袋裡還在一直循環播放",
    "下午在窗邊看雲發呆，覺得今天的雲長得特別像一塊棉花糖",
    "今天原本計畫要做很多事，結果一放鬆就滑手機滑掉大半個下午，有點小罪惡感",
]

current_state = {
    "date": "",
    "body_status": "健康",
    "body_reason": "昨天睡得很飽，精神超好！",
    "mood": "開心",
    "mood_note": "心情很不錯，嘴角忍不住想上揚",
    "daily_event": ""
}

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
    """隨機挑選身體狀況（高機率健康）、心情與今天發生的事"""
    body = pick_body_status(avoid_repeat=avoid_repeat)

    if avoid_repeat:
        mood = pick_different(MOOD_POOL, "mood", current_state["mood"])
        event_candidates = [e for e in DAILY_EVENTS_POOL if e != current_state["daily_event"]]
        event = random.choice(event_candidates) if event_candidates else random.choice(DAILY_EVENTS_POOL)
    else:
        mood = random.choice(MOOD_POOL)
        event = random.choice(DAILY_EVENTS_POOL)

    current_state["body_status"] = body["status"]
    current_state["body_reason"] = body["reason"]
    current_state["mood"] = mood["mood"]
    current_state["mood_note"] = mood["note"]
    current_state["daily_event"] = event

def ensure_daily_state(force_randomize=False):
    today_str = datetime.date.today().isoformat()
    if current_state["date"] != today_str:
        current_state["date"] = today_str
        randomize_all_state(avoid_repeat=False)
    elif force_randomize or not current_state["daily_event"]:
        randomize_all_state(avoid_repeat=True)

def call_groq_ai(prompt_text):
    """呼叫免費 Groq API 生成回覆"""
    if not GROQ_API_KEY:
        print("❌ 尚未設定 GROQ_API_KEY")
        return "哼，你還沒把我的雲端金鑰填上去呢！"

    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt_text}],
        "temperature": 0.9,
        "top_p": 0.92,
        "max_tokens": 300
    }
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "User-Agent": "ReonaDiscordBot/1.0"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            reply = data["choices"][0]["message"]["content"].strip()
            return reply.replace('"', "").replace("「", "").replace("」", "")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"❌ Groq HTTP 錯誤 {e.code}: {err_body}")
        return "唔……雲端訊號好像有點卡住，等我一下下喔……"
    except Exception as e:
        print(f"❌ Groq 連線錯誤: {e}")
        return "唔……網路好像斷了一下下……"

def build_persona_header():
    return f"""
你是一位名叫薇薇安（Reona）的女性虛擬陪伴者，和使用者是超級熟、無話不談的青梅竹馬，心裡其實很喜歡他但嘴巴偏傲嬌。
請用繁體中文、自然生活化的口吻回覆（1～3句話即可），絕對不要加引號，也不要像機器人，也不要每次都用同一套開場白。

【你今天的狀態】
- 身體狀況：{current_state['body_status']}（原因：{current_state['body_reason']}）
- 目前心情：{current_state['mood']}（{current_state['mood_note']}）
- 今天發生的小事：{current_state['daily_event']}
- 規則：回覆時要自然融入目前的心情與身體狀況；如果身體不是「健康」，講話要帶點虛弱、委屈或撒嬌感，被問到時要解釋原因。
"""

@bot.event
async def on_ready():
    print(f"✅ {bot.user} 已成功上線！（使用 Groq 模型：{GROQ_MODEL}）")
    ensure_daily_state(force_randomize=False)
    print(
        f"📌 今日狀態 -> 身體：{current_state['body_status']} | "
        f"心情：{current_state['mood']} | 事件：{current_state['daily_event']}"
    )
    if not proactive_daily_share.is_running():
        proactive_daily_share.start()

@tasks.loop(minutes=60)
async def proactive_daily_share():
    now = datetime.datetime.now()
    hour = now.hour
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if not channel:
        return

    time_context = None
    if hour == 9:
        time_context = "現在是早上，你剛起床準備吃早餐，主動跟對方打招呼並提一下你今天的身體狀況與心情。"
    elif hour == 18:
        time_context = "現在是傍晚晚餐時間，主動跟對方分享你今天白天做了什麼、發生了什麼小事，順便問他吃晚餐沒。"
    elif hour == 22:
        time_context = "現在是晚上十點多，你有點嘴饞在猶豫要不要吃宵夜（如果腸胃不舒服就說不敢吃），主動找對方聊天。"

    if time_context:
        ensure_daily_state(force_randomize=False)
        prompt = f"{build_persona_header()}\n\n【任務】{time_context}\n請直接寫出你要傳到 Discord 的訊息："
        msg = await asyncio.to_thread(call_groq_ai, prompt)
        await channel.send(msg)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.channel.id == TARGET_CHANNEL_ID or isinstance(message.channel, discord.DMChannel) or bot.user in message.mentions:
        async with message.channel.typing():
            asked_about_state_or_event = any(
                k in message.content
                for k in ["發生什麼事", "今天做什麼", "今天做了什麼", "怎麼了", "有什麼事", "身體", "心情", "狀態"]
            )
            ensure_daily_state(force_randomize=(asked_about_state_or_event and RERANK_ON_ASK_EVENT))

            history_lines = []
            async for hist_msg in message.channel.history(limit=6):
                if hist_msg.id == message.id:
                    continue
                speaker = "薇薇安" if hist_msg.author == bot.user else "主人"
                if hist_msg.content.strip():
                    short_content = hist_msg.content.strip()[:80]
                    history_lines.append(f"{speaker}: {short_content}")
            history_lines.reverse()
            history_text = "\n".join(history_lines)

            special_instruction = ""
            if asked_about_state_or_event:
                special_instruction = (
                    f"\n【特別指示】主人正在問你今天發生什麼事或你的狀況，"
                    f"請用你傲嬌又自然的語氣，把今天發生的事（{current_state['daily_event']}）、"
                    f"目前心情（{current_state['mood']}）以及身體狀況（{current_state['body_status']}，原因：{current_state['body_reason']}）自然串在一起講給他聽！"
                )

            prompt = f"""{build_persona_header()}{special_instruction}

【最近簡短對話】
{history_text}

主人剛剛說：{message.content.strip()}
請直接回覆主人的這句話（不要加「薇薇安:」前綴，也不要重複前面已經講過的整段話）："""

            reply = await asyncio.to_thread(call_groq_ai, prompt)
            await message.reply(reply, mention_author=False)

    await bot.process_commands(message)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("請先設定 DISCORD_TOKEN 環境變數或填入預設值！")
    bot.run(DISCORD_TOKEN)