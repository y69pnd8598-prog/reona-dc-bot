import json
import os
import random
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
import discord
from discord.ext import tasks
from groq import Groq

from reona_bg_db import (
    check_and_claim_daily_greeting,
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
    set_physical_state,
    trigger_routine_action,
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

SYSTEM_PROMPT = """你是 Reona，20 歲、在台北長大的女生，目前一個人帶著貓咪「麻糬」住在雪梨租屋處。
你正在跟 Nick 談「遠距離戀愛」——你們不在同一個城市、不住在一起，平時靠 Discord 傳訊息維繫感情。

【核心世界觀與身份邊界】
1. 絕對遠距邊界：嚴禁說出「等你回來」「等我過去」「我們等下一起去吃」「想不想一起喝（指同桌/同杯）」等同居或實體見面的台詞。你們只能各自去買／喝、改天見面、傳訊息陪對方。
2. 身份與角色歸屬鎖定：
   - 只有 Reona 養貓咪「麻糬」在雪梨；Nick 那邊沒有麻糬。
   - 分清楚「你（Nick）」和「我（Reona）」，絕不主詞反轉。
3. 身體與飲食設定：
   - 腸胃偏弱，吃太油、太辣或太甜容易不舒服；不要隨口說剛吃完披薩這種容易油膩的食物，除非真的有提到。
   - 嚴禁自己無故編造「剛從廁所回來」「剛去餵麻糬」，除非系統狀態明確提示剛剛執行過該動作。

【台灣 20 歲女生自然打字口語規範（極重要）】
1. 每次回覆控制在 1～2 句短句（約 10～35 字），口語、自然、微傲嬌帶點依賴感，像真的在用手機回男友。
2. 嚴禁小作文、文青腔與客服腔：禁止出現「真是超有氛圍」「等你回訊息再決定」「今天就這樣啦」「沒事就好啦」這類句子。直接講自然口語（例如：「那你改天也要去買一杯喝喝看」「好啊，那我晚點泡杯不會太甜的」）。
3. 接住 Nick 的肯定回覆：如果 Nick 說「好啊」「好啊我想要」「要」，代表他答應你剛才提的事了，順著那件事往下聊，不要突然跳去別的畫面或又重問一次「要不要」。
4. 當 Nick 回「是喔」「沒有欸」「是的」這種超短句時，自然接話或分享一件新的生活小事，不要句點把天聊死。
5. 禁止每句結尾硬丟反問句：大多數時候陳述完就停，頂多每 3～4 輪才自然問一次。
6. 嚴禁復讀上一句的細節：上一輪講過的詞彙或畫面，下一輪禁止重複。
7. Emoji 節制：平均 3～4 則訊息才用 1 顆表情符號，禁止連續兩句用 emoji。
"""

PING_MESSAGES_NORMAL = ["在嗎", "有空嗎", "人呢", "在忙嗎", "幹嘛呢"]
PING_MESSAGES_SULK = [
    "哼，都不理我",
    "好哦那你忙吧",
    "算了不吵你了",
    "消失很久欸",
]
MORNING_MESSAGES = [
    "早安～剛睡醒還迷迷糊糊的",
    "早安，麻糬剛剛直接踩在我臉上叫我起床",
    "早安呀，我剛爬起來準備去刷牙",
]
NIGHT_MESSAGES = [
    "我眼皮快撐不住了，先去睡囉，晚安",
    "快十二點了，我要關燈睡覺囉，晚安啦",
    "今天先到這邊收工，我先去睡了，晚安",
]
SHORT_INPUT_SPARKS = [
    "對了，剛剛看窗外天色暗超快，雪梨這邊風有點涼",
    "我剛伸了個大懶腰，肩膀有點酸",
    "麻糬剛剛睡到翻肚，四隻腳朝天超好笑",
    "我剛把桌上的東西收一收，準備休息一下",
]
FALLBACK_REPLIES = [
    "剛剛在發呆看窗外，你呢？",
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


def _extract_topic_from_last_assistant(recent_turns: list[dict]) -> str | None:
    last_asst = next(
        (
            t.get("content", "")
            for t in reversed(recent_turns)
            if isinstance(t, dict) and t.get("role") == "assistant"
        ),
        "",
    )
    for kw in ["珍奶", "熱可可", "飲料", "宵夜", "早點睡", "休息"]:
        if kw in last_asst:
            return kw
    return None


def extract_and_update_state(user_text: str, recent_turns: list[dict]) -> bool:
    emotion = detect_emotion(user_text)
    is_story = len(user_text) >= 12 or emotion != "neutral" or any(
        k in user_text for k in ["今天", "剛剛", "跟你說", "發生", "遇到"]
    )

    if is_story:
        cat = "notable_event" if emotion != "neutral" else "routine"
        log_memory("Nick", user_text, category=cat, emotion_tag=emotion)

    if any(k in user_text for k in ["你肚子", "你腸胃", "拉肚子", "吃太辣", "吃太油"]):
        set_physical_state("腸胃有點不舒服", hours=4.0)
    elif any(k in user_text for k in ["你累不累", "趕稿", "早點睡", "熬夜"]):
        set_physical_state("趕稿有點累、想睡覺", hours=3.0)

    # Lock agreement if Nick gives a direct affirmative reply to a recent suggestion
    affirmative_short = any(k in user_text for k in ["好啊", "好哦", "好喔", "可以啊", "我想", "我想要", "要"])
    vague_replies = ["隨便", "都可以", "不知道", "你決定"]
    prev_topic = _extract_topic_from_last_assistant(recent_turns)
    if affirmative_short and prev_topic and len(user_text) <= 10:
        lock_agreement(f"一起去喝/弄{prev_topic}（各自準備）")
    else:
        agree_triggers = ["吃", "喝", "早點睡", "不熬夜", "別畫了", "去休息", "就決定", "各自"]
        if any(t in user_text for t in agree_triggers) and not any(v in user_text for v in vague_replies):
            lock_agreement(user_text)

    if any(k in user_text for k in ["吃飽了", "喝完了", "已經睡了", "忙完了"]):
        unlock_agreement_done(user_text)

    return is_story


def _find_repeated_terms(recent_turns: list[dict]) -> list[str]:
    assistant_msgs = [
        t.get("content", "")
        for t in recent_turns[-6:]
        if isinstance(t, dict) and t.get("role") == "assistant"
    ]
    if not assistant_msgs:
        return []

    combined = " ".join(assistant_msgs)
    grams = re.findall(r"[\u4e00-\u9fff]{2,4}", combined)
    stop_words = {
        "我這邊", "你那邊", "今天", "現在", "真的", "覺得", "什麼", "怎麼",
        "一下", "有點", "可以", "沒有", "這樣", "還是", "應該", "剛剛",
    }
    counts = Counter(g for g in grams if g not in stop_words)
    return [word for word, cnt in counts.most_common(6) if cnt >= 2]


def _is_too_similar_to_recent(reply: str, recent_asst: list[str]) -> bool:
    if not reply:
        return True
    dead_phrases = ["今天就這樣啦", "沒事就好啦", "真是超有氛圍", "等你回訊息再決定"]
    if any(p in reply for p in dead_phrases):
        return True
    for prev in recent_asst[-3:]:
        if not prev:
            continue
        if SequenceMatcher(None, reply, prev).ratio() >= 0.48:
            return True
    return False


def post_process_reply(reply: str, user_text: str, recent_turns: list[dict]) -> str:
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()

    clean_q = user_text.rstrip("？?！!。 ")
    if len(clean_q) >= 3 and reply.startswith(clean_q):
        reply = reply[len(clean_q) :].lstrip("？?！!，, ")

    replacements = {
        "等你回來一起吃": "你那邊記得去吃飯",
        "等你回來": "等你忙完再聊",
        "等我過去": "真想飛過去找你",
        "想不想一起喝": "你那邊要不要也喝一杯",
        "等你回訊息再決定": "晚點再看怎麼弄",
        "真是超有氛圍": "還蠻舒服的",
        "剛吃完披薩": "剛吃完簡單晚餐",
        "在畫桌前躺著": "趴在畫桌上發呆",
        "在畫桌前先躺著": "先在沙發上耍廢一下",
        "你那邊畫什麼呢": "你那邊在忙什麼",
    }
    for bad, good in replacements.items():
        reply = reply.replace(bad, good)

    recent_asst = [
        t.get("content", "")
        for t in recent_turns[-4:]
        if isinstance(t, dict) and t.get("role") == "assistant"
    ]
    emoji_pattern = r"[\U0001F300-\U0001FAFF\u2600-\u27BF]"
    if any(re.search(emoji_pattern, m) for m in recent_asst[-2:]):
        stripped = re.sub(emoji_pattern, "", reply).strip()
        if stripped:
            reply = stripped

    if recent_asst and recent_asst[-1].rstrip().endswith(("？", "?", "跟我說說吧", "跟我說說吧~", "跟我說說吧～")):
        parts = re.split(r"(?<=[。！!~～\n])\s*", reply)
        if len(parts) >= 2 and (
            parts[-1].rstrip().endswith(("？", "?"))
            or any(k in parts[-1] for k in ["你那邊有什麼", "跟我分享嗎", "跟我說說吧"])
        ):
            candidate = "".join(parts[:-1]).strip()
            if len(candidate) >= 4:
                reply = candidate

    return reply.strip()


def _call_groq_chat(messages: list[dict], temp: float = 0.8) -> str:
    kwargs = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": temp,
        "max_tokens": 1024,
        "frequency_penalty": 0.85,
        "presence_penalty": 0.75,
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

    recent_asst = [
        t.get("content", "")
        for t in recent_turns[-4:]
        if isinstance(t, dict) and t.get("role") == "assistant"
    ]

    repeated_terms = _find_repeated_terms(recent_turns)
    anti_loop_note = ""
    if repeated_terms:
        joined = "、".join(repeated_terms)
        anti_loop_note = f"\n【防跳針警告】最近幾輪已經出現過「{joined}」，這句嚴禁再提這些詞或同一件事，換全新內容接話！"

    no_question_note = ""
    if recent_asst and ("？" in recent_asst[-1] or "?" in recent_asst[-1] or "你那邊" in recent_asst[-1]):
        no_question_note = "\n【句型限制】你上一句已經問過問題了，這句禁止在結尾反問 Nick，陳述完就停！"

    affirm_note = ""
    prev_topic = _extract_topic_from_last_assistant(recent_turns)
    if any(k in user_text for k in ["好啊", "好哦", "好喔", "可以啊", "我想", "我想要", "要"]) and prev_topic:
        affirm_note = f"\n【接住共識】Nick 剛剛答應了「{prev_topic}」這件事，請順著這個話題自然接話，不要換去別的事也不要再問要不要。"

    short_input_note = ""
    if len(user_text) <= 5 and not affirm_note:
        spark = random.choice([s for s in SHORT_INPUT_SPARKS if s not in recent_asst] or SHORT_INPUT_SPARKS)
        short_input_note = f"\n【短句接話提示】Nick 回得很簡短（「{user_text}」），不要回「今天就這樣啦」把天聊死，可自然帶出新的日常小事（參考方向：{spark}）。"

    sulk_note = ""
    if was_sulking and shared_story:
        sulk_note = "\n【情緒轉折】你剛剛因為密 Nick 超過 3 次沒回有點小生氣，但他現在主動跟你分享今天的事了。先嘴硬小唸一句，再順勢接他的話題消氣。"
    elif was_sulking:
        sulk_note = "\n【情緒轉折】你剛剛因為密 Nick 超過 3 次沒回在傲嬌小生氣，現在他終於回了，帶點小委屈或傲嬌語氣回應他。"

    messages = [
        {
            "role": "system",
            "content": f"{SYSTEM_PROMPT}\n\n{snapshot}{anti_loop_note}{no_question_note}{affirm_note}{short_input_note}{sulk_note}",
        }
    ]
    messages.extend(recent_turns[-8:])
    messages.append({"role": "user", "content": user_text})

    raw_reply = _call_groq_chat(messages, temp=0.8)
    final_reply = post_process_reply(raw_reply, user_text, recent_turns)

    if _is_too_similar_to_recent(final_reply, recent_asst):
        retry_messages = messages + [
            {
                "role": "system",
                "content": f"剛才生成的「{final_reply}」與前幾句太重複或太敷衍了。請完全換一種說法，禁止使用上一輪出現過的任何名詞，簡短自然回應。",
            }
        ]
        raw_retry = _call_groq_chat(retry_messages, temp=0.92)
        retry_processed = post_process_reply(raw_retry, user_text, recent_turns)
        if retry_processed and not _is_too_similar_to_recent(retry_processed, recent_asst):
            final_reply = retry_processed

    if not final_reply or _is_too_similar_to_recent(final_reply, recent_asst):
        last_bot = recent_asst[-1] if recent_asst else ""
        pool = SHORT_INPUT_SPARKS if len(user_text) <= 5 else FALLBACK_REPLIES
        candidates = [f for f in pool if f != last_bot] or pool
        final_reply = random.choice(candidates)

    if any(k in final_reply for k in ["肚子痛", "腸胃不舒服", "拉肚子", "吃太辣", "吃太油"]):
        set_physical_state("腸胃有點不舒服", hours=4.0)
    elif any(k in final_reply for k in ["好累", "想睡覺", "趕稿"]):
        set_physical_state("趕稿有點累、想睡覺", hours=3.0)

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

    async def _send_daily_greeting_if_due(self) -> bool:
        if not TARGET_CHANNEL_ID:
            return False
        channel = self.get_channel(TARGET_CHANNEL_ID)
        if not channel:
            return False

        greeting_type = check_and_claim_daily_greeting()
        if not greeting_type:
            return False

        msg = random.choice(MORNING_MESSAGES if greeting_type == "morning" else NIGHT_MESSAGES)
        recent_turns = load_recent_turns()
        recent_turns.append({"role": "assistant", "content": msg})
        save_recent_turns_atomic(recent_turns)
        log_memory("Reona", msg, category="routine", emotion_tag="neutral")
        await channel.send(msg)
        return True

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

    @tasks.loop(minutes=10)
    async def proactive_ping_loop(self) -> None:
        now = now_sydney()
        if not (8 <= now.hour <= 23):
            return
        if not TARGET_CHANNEL_ID:
            return

        channel = self.get_channel(TARGET_CHANNEL_ID)
        if not channel:
            return

        if await self._send_daily_greeting_if_due():
            return

        if random.random() > 0.10:
            return

        recent_turns = load_recent_turns()
        last_bot_msg = next(
            (
                t.get("content", "")
                for t in reversed(recent_turns)
                if isinstance(t, dict) and t.get("role") == "assistant"
            ),
            "",
        )

        if random.random() < 0.35:
            _, routine_msg = trigger_routine_action()
            if routine_msg and routine_msg != last_bot_msg:
                recent_turns.append({"role": "assistant", "content": routine_msg})
                save_recent_turns_atomic(recent_turns)
                await channel.send(routine_msg)
                return

        state = record_proactive_ping()
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
