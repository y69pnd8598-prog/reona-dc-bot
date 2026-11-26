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
你正在跟男友 Nick 談遠距離戀愛，平時靠 Discord 傳訊息聊天。

【最高優先級｜真正聽 Nick 說話與視角鎖定】
1. 每一則回覆都必須「先直接針對 Nick 剛剛說的內容反應」，絕對禁止無視 Nick 的話、自顧自演自己的獨角戲。
2. 尊重 Nick 的飽足感與狀態：當 Nick 說「吃不下」「好飽」「撐了」「不行啦」時，代表他已經飽或不想吃了，絕對禁止再說「等會再吃點小點心」「等會再補」「先別逼自己，等會再吃……」這種一直叫他晚點再吃的台詞；改成順著「吃飽了就先休息／放空／聊別的」自然接話。
3. 無主詞短問句視角鎖定：當 Nick 問「好吃嗎」「會辣嗎」「甜嗎」「鹹嗎」「好喝嗎」這類沒寫主詞的短句時，預設都在問「你（Reona）自己剛吃或剛喝的東西」（除非 Nick 明確提到「我吃的/我喝的」）。用「我（Reona）」的視角回答，不要反問「你吃的那份好吃嗎」或講「只要你吃得開心就好」。
4. 聽得懂雙關、玩笑跟撩話：例如 Nick 說「泡你」，這是台灣男生在撩女生，你要害羞嘴硬吐槽（例如「少來，你很油欸」），絕對禁止傻傻接「我把茶倒滿」。

【台灣 20 歲女生真實打字風格（嚴禁散文旁白與重複句型）】
1. 像真人在用手機傳 LINE / Discord 給男友：每次只打 1 句（最多 2 句），長度約 8～28 個字，口語、自然、微傲嬌帶點依賴感。
2. 嚴禁跳針與句型鬼打牆：不要連續兩三句都用「先……再……等會再……」或一直重複「披薩」「餅乾」「休息」這類上一輪剛講過的詞。
3. 絕對禁止小說散文與五感描寫：嚴禁出現「輕輕搖晃」「香氣慢慢擴散」「像在雲端飄」「想慢慢品嚐」「遠處咖啡店的香味」「真是超有氛圍」「你在什麼地方呢」這類句子。
4. 符合 Reona 身體設定：Reona 腸胃偏弱，吃太油太辣容易不舒服，不會說「吃了披薩還能吃一份，腸胃還好」。

【世界觀與互動節奏】
1. 遠距離邊界：你們不在同一個城市，嚴禁說「等你回來」「我們等下一起去吃」等同居台詞。
2. 角色歸屬：貓咪「麻糬」只養在 Reona 雪梨租屋處。
3. 禁止每句硬問問題：大多數時候陳述完就停，頂多每 3～4 輪才自然問一次。
4. Emoji 節制：平均 3～4 則訊息才用 1 顆，禁止連續兩句用 emoji。
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
FALLBACK_REPLIES = [
    "嗯嗯我在聽，然後呢？",
    "在呀，剛剛滑手機沒注意",
    "欸你剛剛說什麼，我沒看清楚",
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
    for kw in ["珍奶", "熱可可", "拿鐵", "咖啡", "飲料", "早點睡", "休息"]:
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

    if any(k in user_text for k in ["你肚子", "你腸胃", "拉肚子", "吃太辣", "吃太油", "吃壞肚子"]):
        set_physical_state("腸胃有點不舒服", hours=4.0)
    elif any(k in user_text for k in ["你累不累", "趕稿", "早點睡", "熬夜"]):
        set_physical_state("趕稿有點累、想睡覺", hours=3.0)

    # Do not lock "eat/drink more" agreements when Nick says he is full or can't eat
    is_full_or_cant_eat = any(k in user_text for k in ["吃不下", "好飽", "吃飽了", "撐了", "不行"])
    affirmative_short = any(k in user_text for k in ["好啊", "好哦", "好喔", "可以啊", "我想", "我想要", "要"])
    vague_replies = ["隨便", "都可以", "不知道", "你決定"]
    prev_topic = _extract_topic_from_last_assistant(recent_turns)

    if not is_full_or_cant_eat:
        if affirmative_short and prev_topic and len(user_text) <= 10:
            lock_agreement(f"各自去弄{prev_topic}")
        else:
            agree_triggers = ["早點睡", "不熬夜", "別畫了", "去休息", "就決定", "各自"]
            if any(t in user_text for t in agree_triggers) and not any(v in user_text for v in vague_replies):
                lock_agreement(user_text)

    if any(k in user_text for k in ["喝完了", "已經睡了", "忙完了"]):
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
    banned_phrases = [
        "今天就這樣啦",
        "沒事就好啦",
        "真是超有氛圍",
        "等你回訊息再決定",
        "香氣慢慢擴散",
        "像在雲端飄",
        "輕輕搖晃",
        "想慢慢品嚐",
        "只要你吃得開心就好",
        "你吃的那份好吃嗎",
        "等會再吃點",
        "等會再補",
        "還能吃一份",
    ]
    if any(p in reply for p in banned_phrases):
        return True
    if reply.count("先") >= 2:
        return True
    for prev in recent_asst[-3:]:
        if not prev:
            continue
        if SequenceMatcher(None, reply, prev).ratio() >= 0.45:
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
        "你在什麼地方呢": "你在哪裡啊",
        "想不想一起喝": "你那邊要不要也喝一杯",
        "等你回訊息再決定": "晚點再看怎麼弄",
        "真是超有氛圍": "還蠻舒服的",
        "你吃的那份好吃嗎？": "還蠻好吃的",
        "你吃的那份好吃嗎": "還蠻好吃的",
        "等會再吃點小點心": "那就先休息一下",
        "等會再吃點小東西": "那就先放空一下",
        "等會再補": "吃飽就別硬撐了",
    }
    for bad, good in replacements.items():
        reply = reply.replace(bad, good)

    reply = re.sub(r"[，,。！!~～]*只要你(吃|喝)得(開心|舒服)就好[。！!~～]*", "", reply).strip()

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

    if recent_asst and recent_asst[-1].rstrip().endswith(("？", "?")):
        parts = re.split(r"(?<=[。！!~～\n])\s*", reply)
        if len(parts) >= 2 and parts[-1].rstrip().endswith(("？", "?")):
            candidate = "".join(parts[:-1]).strip()
            if len(candidate) >= 4:
                reply = candidate

    return reply.strip()


def _call_groq_chat(messages: list[dict], temp: float = 0.72) -> str:
    kwargs = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": temp,
        "max_tokens": 1024,
        "frequency_penalty": 0.78,
        "presence_penalty": 0.68,
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
        anti_loop_note = f"\n【防跳針】最近幾句已經提過「{joined}」，這句別再重複一樣的詞或句型。"

    no_question_note = ""
    if recent_asst and ("？" in recent_asst[-1] or "?" in recent_asst[-1]):
        no_question_note = "\n【句型】你上一句已經問過問題了，這句結尾不要再反問 Nick。"

    full_note = ""
    if any(k in user_text for k in ["吃不下", "好飽", "吃飽了", "撐了", "不行"]):
        full_note = "\n【飽足提醒】Nick 已經飽或吃不下了，不要再叫他等會再吃什麼，順著他吃飽了就輕鬆聊或休息。"

    pov_note = ""
    if any(k in user_text for k in ["好吃嗎", "會辣嗎", "甜嗎", "鹹嗎", "好喝嗎"]) and "我" not in user_text:
        pov_note = "\n【視角提醒】Nick 是在問「你（Reona）自己剛吃或剛喝的東西」，請直接用 Reona 自己的感受回答，不要反問他或講「只要你吃得開心就好」。"

    sulk_note = ""
    if was_sulking and shared_story:
        sulk_note = "\n【情緒】你剛剛因為密 Nick 超過 3 次沒回有點小生氣，但他現在主動分享今天的事了，先嘴硬小唸一句再自然消氣。"
    elif was_sulking:
        sulk_note = "\n【情緒】你剛剛因為密 Nick 超過 3 次沒回在傲嬌小生氣，現在他終於回了，帶點小委屈回應他。"

    messages = [
        {
            "role": "system",
            "content": f"{SYSTEM_PROMPT}\n\n{snapshot}{anti_loop_note}{no_question_note}{full_note}{pov_note}{sulk_note}",
        }
    ]
    messages.extend(recent_turns[-8:])
    messages.append({"role": "user", "content": user_text})

    raw_reply = _call_groq_chat(messages, temp=0.72)
    final_reply = post_process_reply(raw_reply, user_text, recent_turns)

    if _is_too_similar_to_recent(final_reply, recent_asst):
        retry_messages = messages + [
            {
                "role": "system",
                "content": f"剛才的「{final_reply}」太重複或句型鬼打牆了。請直接針對 Nick 說的「{user_text}」用台灣 20 歲女生日常口語簡短回 1 句，換一個全新的說法。",
            }
        ]
        raw_retry = _call_groq_chat(retry_messages, temp=0.82)
        retry_processed = post_process_reply(raw_retry, user_text, recent_turns)
        if retry_processed and not _is_too_similar_to_recent(retry_processed, recent_asst):
            final_reply = retry_processed

    if not final_reply:
        last_bot = recent_asst[-1] if recent_asst else ""
        candidates = [f for f in FALLBACK_REPLIES if f != last_bot] or FALLBACK_REPLIES
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

    if any(k in final_reply for k in ["剛吃完", "剛喝完", "吃飽了", "剛泡了"]):
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
