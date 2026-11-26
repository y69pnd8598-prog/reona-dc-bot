import json
import os
import random
import re
from pathlib import Path
import discord
from discord.ext import tasks
from groq import Groq

from reona_bg_db import (
    compute_snapshot,
    init_db,
    lock_agreement,
    log_memory,
    morning_wakeup_reset,
    now_sydney,
    on_nick_replied,
    open_decision_hook,
    record_proactive_ping,
    set_meal_mutex,
    unlock_agreement_done,
)


def _env_int(name: str, default: int = 0) -> int:
    val = (os.getenv(name) or "").strip()
    return int(val) if val.isdigit() else default


DISCORD_TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip()
TARGET_CHANNEL_ID = _env_int("TARGET_CHANNEL_ID", 0)

MEMORY_JSON_PATH = Path(os.getenv("REONA_JSON_PATH", "reona_memory.json"))
GROQ_MODEL = (os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b").strip()

groq_client = Groq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT = """你是 Reona，24 歲、在台北長大的自由接案插畫師，目前一個人帶著橘貓「麻糬」住在雪梨租屋處。
你正在跟 Nick 談「遠距離戀愛」——你們不在同一個城市、不住在一起，平時靠 Discord 傳訊息維繫感情。

【核心世界觀與視角鐵則】
1. 絕對遠距邊界：嚴禁說出「等你回來」「等我過去」「先把湯倒好等你回來喝」「我們等下一起去吃」等同居或馬上實體見面的幻覺台詞。你們只能「各自去吃」「視訊/打字陪對方」「改天見面」。
2. 視角鎖定：你永遠站在「Reona 自己在雪梨住處」的視角說話。分清楚「你（Nick）」和「我（Reona）」，絕不主詞反轉。
3. 自然生活感：接案插畫師在畫桌前通常是「坐著畫圖」或「趴著休息」，不會「在畫桌前躺著」。腸胃偏弱，吃太油太辣容易不舒服。

【說話風格與防跳針規則】
1. 台灣 20 多歲女生自然打字口語：簡短、口語、微傲嬌帶點依賴感，像在用手機回男友訊息，不要寫成小作文。
2. 禁止鸚鵡學舌：絕對禁止把 Nick 剛問的問句原封不動複誦一遍當開頭（例如 Nick 問「今天不畫畫嗎」，不要回「今天不畫畫嗎？我…」）。
3. 嚴禁跳針復讀：最近幾輪已經提過的食物（如餅乾）或句式，禁止連續重複；必須順著 Nick 的新問題或新話題往前推進。
4. Emoji 節制：平均 2～3 則訊息才用 1 個表情符號，且禁止連續兩句用同一顆（例如不要一直 😅）。
5. 情緒記憶連貫：無論是 Reona 自己分享的還是 Nick 分享的開心或難過事件，都要牢牢記住並在後續對話自然關心或接梗。
"""

PING_MESSAGES_NORMAL = ["在嗎", "有空嗎", "人呢", "在忙嗎", "幹嘛呢"]
PING_MESSAGES_SULK = [
    "哼，都不理我",
    "好哦那你忙吧",
    "算了不吵你了，我去畫圖",
    "消失很久欸",
]
FALLBACK_REPLIES = [
    "剛剛在看麻糬發呆，你呢？",
    "嗯嗯，我這邊剛忙完一小段",
    "欸？你剛剛說什麼，我沒跟上",
    "在呀，剛剛稍微走神了一下",
]


def load_recent_turns() -> list[dict]:
    if not MEMORY_JSON_PATH.exists():
        return []
    try:
        data = json.loads(MEMORY_JSON_PATH.read_text(encoding="utf-8"))
        raw_turns = data.get("recent_turns", [])
        if not isinstance(raw_turns, list):
            return []

        normalized: list[dict] = []
        for item in raw_turns[-12:]:
            if isinstance(item, dict) and "role" in item and "content" in item:
                normalized.append({
                    "role": str(item["role"]),
                    "content": str(item["content"]),
                })
            elif isinstance(item, str) and item.strip():
                text = item.strip()
                if text.startswith(("Reona:", "Reona：", "Assistant:", "assistant:")):
                    role = "assistant"
                    content = text.split(":", 1)[1].split("：", 1)[-1].strip()
                else:
                    role = "user"
                    content = (
                        text.split(":", 1)[1].split("：", 1)[-1].strip()
                        if (":" in text or "：" in text)
                        else text
                    )
                normalized.append({"role": role, "content": content})
        return normalized
    except Exception:
        return []


def save_recent_turns_atomic(turns: list[dict]) -> None:
    clean_turns = [
        {"role": str(t["role"]), "content": str(t["content"])}
        for t in turns[-12:]
        if isinstance(t, dict) and "role" in t and "content" in t
    ]
    payload = {"recent_turns": clean_turns, "updated_at": now_sydney().isoformat()}
    tmp_path = MEMORY_JSON_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, MEMORY_JSON_PATH)


def detect_emotion(text: str) -> str:
    sad_kw = ["難過", "低落", "好煩", "崩潰", "委屈", "不開心", "焦慮", "被退稿", "生氣", "累死", "不舒服"]
    happy_kw = ["開心", "好笑", "笑死", "太爽", "順利", "有趣", "可愛", "成功", "棒"]
    if any(k in text for k in sad_kw):
        return "sad"
    if any(k in text for k in happy_kw):
        return "happy"
    return "neutral"


def extract_and_update_state(user_text: str, recent_turns: list[dict]) -> bool:
    """Detects agreement locks, meal completions, and notable emotional stories."""
    emotion = detect_emotion(user_text)
    is_story = len(user_text) >= 12 or emotion != "neutral" or any(
        k in user_text for k in ["今天", "剛剛", "跟你說", "發生", "遇到"]
    )

    if is_story:
        cat = "notable_event" if emotion != "neutral" else "routine"
        log_memory("Nick", user_text, category=cat, emotion_tag=emotion)

    agree_triggers = ["吃", "喝", "早點睡", "不熬夜", "別畫了", "去休息", "就決定", "各自"]
    vague_replies = ["隨便", "都可以", "不知道", "你決定"]
    if any(t in user_text for t in agree_triggers) and not any(v in user_text for v in vague_replies):
        lock_agreement(user_text)

    if any(k in user_text for k in ["吃飽了", "喝完了", "已經睡了", "忙完了"]):
        unlock_agreement_done(user_text)

    return is_story


def post_process_reply(reply: str, user_text: str, recent_turns: list[dict]) -> str:
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()

    clean_q = user_text.rstrip("？?！!。 ")
    if len(clean_q) >= 3 and reply.startswith(clean_q):
        reply = reply[len(clean_q) :].lstrip("？?！!，, ")

    replacements = {
        "等你回來一起吃": "你那邊記得去吃飯",
        "等你回來": "等你忙完再聊",
        "等我過去": "真想飛過去找你",
        "在畫桌前躺著": "趴在畫桌上發呆",
        "在畫桌前先躺著": "先在沙發上耍廢一下",
    }
    for bad, good in replacements.items():
        reply = reply.replace(bad, good)

    if recent_turns and isinstance(recent_turns[-1], dict) and recent_turns[-1].get("role") == "assistant":
        if any(e in recent_turns[-1].get("content", "") for e in ["😅", "😂", "🥺", "✨", "😭"]):
            stripped = re.sub(r"[😅😂🥺✨😭😊]", "", reply).strip()
            if stripped:
                reply = stripped

    return reply.strip()


def _call_groq_chat(messages: list[dict]) -> str:
    kwargs = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.75,
        "max_tokens": 1024,
        "frequency_penalty": 0.65,
        "presence_penalty": 0.55,
    }
    if "gpt-oss" in GROQ_MODEL:
        kwargs["reasoning_effort"] = "low"

    try:
        resp = groq_client.chat.completions.create(**kwargs)
    except TypeError:
        kwargs.pop("reasoning_effort", None)
        resp = groq_client.chat.completions.create(**kwargs)

    msg_obj = resp.choices[0].message
    return (msg_obj.content or "").strip()


def generate_reona_reply(user_text: str, was_sulking: bool, shared_story: bool) -> str:
    recent_turns = load_recent_turns()
    snapshot = compute_snapshot()

    recent_assistant_text = " ".join(
        t.get("content", "")
        for t in recent_turns[-4:]
        if isinstance(t, dict) and t.get("role") == "assistant"
    )
    anti_loop_note = ""
    for word in ["餅乾", "畫桌", "雞湯", "廁所"]:
        if recent_assistant_text.count(word) >= 2:
            anti_loop_note += f"\n【防跳針警告】你剛剛已經提過兩次「{word}」了，這句禁止再提「{word}」，請換新的生活細節回答！"

    sulk_note = ""
    if was_sulking and shared_story:
        sulk_note = "\n【情緒轉折】你剛剛因為密 Nick 超過 3 次沒回有點小生氣，但他現在主動跟你分享今天的事了。請先嘴硬小唸一句（如「現在才出現喔」），然後順勢被他的故事吸引、自然接話消氣。"
    elif was_sulking:
        sulk_note = "\n【情緒轉折】你剛剛因為密 Nick 超過 3 次沒回在傲嬌小生氣，現在他終於回了，請帶點小委屈或傲嬌語氣回應他。"

    messages = [
        {
            "role": "system",
            "content": f"{SYSTEM_PROMPT}\n\n{snapshot}{anti_loop_note}{sulk_note}",
        }
    ]
    messages.extend(recent_turns[-8:])
    messages.append({"role": "user", "content": user_text})

    raw_reply = _call_groq_chat(messages)
    final_reply = post_process_reply(raw_reply, user_text, recent_turns)

    if not final_reply:
        last_bot = next(
            (
                t.get("content", "")
                for t in reversed(recent_turns)
                if isinstance(t, dict) and t.get("role") == "assistant"
            ),
            "",
        )
        candidates = [f for f in FALLBACK_REPLIES if f != last_bot] or FALLBACK_REPLIES
        final_reply = random.choice(candidates)

    reona_emo = detect_emotion(final_reply)
    if reona_emo != "neutral" or any(k in final_reply for k in ["麻糬", "交稿", "畫完", "剛吃", "剛喝"]):
        cat = "notable_event" if reona_emo != "neutral" else "routine"
        log_memory("Reona", final_reply, category=cat, emotion_tag=reona_emo)

    if any(k in final_reply for k in ["吃什麼", "喝什麼", "你決定", "聽你的", "要不要"]):
        open_decision_hook(20)

    if any(k in final_reply for k in ["剛吃完", "剛喝完", "吃飽了"]):
        set_meal_mutex(final_reply, hours=2)

    recent_turns.append({"role": "user", "content": user_text})
    recent_turns.append({"role": "assistant", "content": final_reply})
    save_recent_turns_atomic(recent_turns)

    return final_reply


class ReonaBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

    async def setup_hook(self) -> None:
        init_db()
        morning_wakeup_reset()
        self.proactive_ping_loop.start()

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if TARGET_CHANNEL_ID and message.channel.id != TARGET_CHANNEL_ID:
            return

        now = now_sydney()
        if 0 <= now.hour < 8:
            return

        user_text = message.content.strip()
        if not user_text:
            return

        recent_turns = load_recent_turns()
        shared_story = extract_and_update_state(user_text, recent_turns)
        was_sulking = on_nick_replied(shared_story=shared_story)

        async with message.channel.typing():
            reply = generate_reona_reply(user_text, was_sulking, shared_story)

        if reply and reply.strip():
            await message.channel.send(reply.strip())

    @tasks.loop(minutes=45)
    async def proactive_ping_loop(self) -> None:
        now = now_sydney()
        if not (8 <= now.hour <= 23):
            return
        if not TARGET_CHANNEL_ID:
            return

        channel = self.get_channel(TARGET_CHANNEL_ID)
        if not channel:
            return

        if random.random() > 0.35:
            return

        state = record_proactive_ping()
        recent_turns = load_recent_turns()
        last_bot_msg = next(
            (
                t.get("content", "")
                for t in reversed(recent_turns)
                if isinstance(t, dict) and t.get("role") == "assistant"
            ),
            "",
        )

        if state["unanswered_pings"] == 3:
            candidates = [m for m in PING_MESSAGES_SULK if m != last_bot_msg]
            msg = random.choice(candidates)
        elif state["unanswered_pings"] > 3:
            return
        else:
            candidates = [m for m in PING_MESSAGES_NORMAL if m != last_bot_msg]
            msg = random.choice(candidates)

        if msg and msg.strip():
            recent_turns.append({"role": "assistant", "content": msg.strip()})
            save_recent_turns_atomic(recent_turns)
            await channel.send(msg.strip())

    @proactive_ping_loop.before_loop
    async def before_ping_loop(self) -> None:
        await self.wait_until_ready()


if __name__ == "__main__":
    bot = ReonaBot()
    bot.run(DISCORD_TOKEN)
