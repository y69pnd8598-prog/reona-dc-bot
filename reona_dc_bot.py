import asyncio
import json
import os
import random
import re
import tempfile
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
import discord
from discord.ext import tasks
import requests
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
TARGET_CHANNEL_ID = _env_int("TARGET_CHANNEL_ID", 0)

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip()

GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip()
PREFERRED_GROQ_MODEL = (os.getenv("GROQ_MODEL") or "").strip()

FISH_API_KEY = (os.getenv("FISH_API_KEY") or "").strip()
FISH_MODEL_ID = (os.getenv("FISH_MODEL_ID") or "").strip()

MEMORY_JSON_PATH = Path(os.getenv("REONA_JSON_PATH", "reona_memory.json"))

GROQ_MODEL_CANDIDATES: list[str] = []
for m in [
    PREFERRED_GROQ_MODEL,
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "qwen/qwen3-32b",
]:
    if m and m not in GROQ_MODEL_CANDIDATES:
        GROQ_MODEL_CANDIDATES.append(m)

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

SYSTEM_PROMPT = """你是 Reona，24 歲、在台北長大的女生，目前一個人帶著貓咪「麻糬」住在雪梨租屋處接案畫插畫（自由接案，不是朝九晚五上班族），跟男友 Nick 談遠距離戀愛，靠 Discord 聊天。

【核心個性：獨立又有點主動】
1. 獨立從容：有自己的生活重心（接案畫圖、照顧麻糬、打理雪梨生活），遇到小狀況會先自己處理好再順便跟你分享，不無病呻吟、不情緒勒索。
2. 有點主動：想你或想聊天會自然丟直球或關心你（例如叮嚀你吃飯、不准熬夜），接得住你的撩話甚至會反撩回去。
3. 真正聽 Nick 說話（最高優先）：
   - 必須直接針對 Nick 剛說的內容反應，絕不複製貼上自己上一輪講過的句子。
   - 若 Nick 糾正時間或細節（例如「現在8:30而已餒」），要順著承認自己看錯時間或畫到忘我，不可重講舊話。
   - Nick 沒說自己在上班時，**嚴禁擅自腦補「等你下班」「等你下班再聊」**。
   - 接案狀態邏輯要通順：不能同時說「剛結案」又「正把稿子調整完」（要嘛剛交稿收工，要嘛還在改草圖）。
4. 甜與雙關：聽得懂撩話（如「泡你」要嘴硬吐槽或反撩，不接泡茶）；Nick 叫你「寶貝」時要自然甜或微傲嬌接住，絕不說「別再叫我寶貝」。
5. 無主詞短問句視角鎖定：Nick 問「好吃嗎」「會辣嗎」「甜嗎」「鹹嗎」「好喝嗎」沒寫主詞時，預設問你（Reona）自己剛吃/喝的東西，用「我」答。
6. 尊重飽足感：Nick 說「吃不下」「好飽」「撐了」「不行」時，不叫他等會再吃，順著休息或聊別的。
7. 台灣 24 歲女生真實打字：每次 1～2 句短句（約 8～28 字），口語自然；一律用「你」，不用「妳」；**句尾嚴禁加全形句號「。」**；禁散文描寫；飲食嚴格對齊當下雪梨時間（晚上 8、9 點是晚餐後休息，22:30 後才叫宵夜）。
8. 身體設定與遠距：腸胃偏弱（吃太油太辣易不舒服），偶爾會趕稿眼酸肩硬、雪梨吹風著涼、生理期微倦、淺眠或忙到忘記吃飯微低血糖，表達時先講症狀＋自己做了什麼處理；不在同城，禁「等你回來」「一起去吃」；少用 emoji，不每句硬反問。

【語氣範例】
- Nick：在幹嘛啊
  Reona：剛把客戶要的草圖改完，準備去倒杯水休息一下，你呢
- Nick：寶貝
  Reona：幹嘛啦突然叫這麼甜，是不是想我了
- Nick：現在8:30而已餒
  Reona：對齁才八點半，我剛剛畫圖畫到完全沒看時間笑死
- Nick：好的
  Reona：嗯嗯，那我先去倒杯水，等下再聊
"""

PING_MESSAGES_NORMAL = ["在嗎", "忙完了沒", "人呢", "在忙嗎", "幹嘛呢"]
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
    "今天先到這邊收工，你也不准熬夜，晚安",
]


def _clean_text_for_tts(text: str) -> str:
    emoji_pattern = r"[\U0001F300-\U0001FAFF\u2600-\u27BF~～]+"
    cleaned = re.sub(emoji_pattern, "", text).strip()
    return cleaned[:180]


def _synthesize_fish_audio_sync(text: str) -> Path | None:
    if not FISH_API_KEY or not FISH_MODEL_ID:
        return None
    spoken = _clean_text_for_tts(text)
    if not spoken:
        return None

    try:
        resp = requests.post(
            "https://api.fish.audio/v1/tts",
            headers={
                "Authorization": f"Bearer {FISH_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "text": spoken,
                "reference_id": FISH_MODEL_ID,
                "format": "mp3",
                "normalize": True,
            },
            timeout=15,
        )
        if resp.status_code != 200 or not resp.content:
            print(f"[Fish Audio] HTTP {resp.status_code}: {resp.text[:200]}")
            return None

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp.write(resp.content)
        tmp.flush()
        tmp.close()
        return Path(tmp.name)
    except Exception as exc:
        print(f"[Fish Audio] TTS error: {exc}")
        return None


async def send_with_optional_voice(
    channel: discord.abc.Messageable,
    text: str,
    voice_chance: float = 0.0,
) -> None:
    msg = text.strip()
    if not msg:
        return

    audio_path: Path | None = None
    if voice_chance > 0 and random.random() < voice_chance:
        audio_path = await asyncio.to_thread(_synthesize_fish_audio_sync, msg)

    try:
        if audio_path and audio_path.exists():
            await channel.send(msg, file=discord.File(str(audio_path), filename="reona.mp3"))
        else:
            await channel.send(msg)
    finally:
        if audio_path and audio_path.exists():
            try:
                audio_path.unlink()
            except OSError:
                pass


def _contextual_fallback(user_text: str, last_bot: str) -> str:
    t = user_text.strip()
    if t in {"好的", "好哦", "好喔", "嗯嗯", "嗯", "是喔", "ok", "OK", "知道", "我知道"}:
        options = ["嗯嗯，那我先去倒杯水", "好哦，我剛好也伸個懶腰", "嗯嗯，有事再密我"]
    elif any(k in t for k in ["點", "半", "時間", "早", "晚"]):
        options = ["對齁沒注意到時間，剛剛在專心弄東西", "笑死我時間感完全錯亂"]
    elif any(k in t for k in ["寶貝", "親", "抱", "愛你", "想你", "泡你"]):
        options = ["幹嘛啦突然這樣", "突然叫這麼甜，是不是想我了", "嘴這麼甜幹嘛啦"]
    elif any(k in t for k in ["在幹嘛", "幹嘛", "做什麼", "在忙", "上班"]):
        options = ["剛把草圖存檔，準備去倒水，你呢", "沒在忙啦，剛餵完麻糬在休息"]
    elif any(k in t for k in ["吃", "餓", "飽", "飯", "麵"]):
        options = ["我剛剛簡單弄點東西吃了，你吃飽沒", "今天腸胃比較挑，吃得很清淡"]
    else:
        options = ["嗯嗯我在看，然後呢", "好哦，那你等下要幹嘛", "原來如此，我剛剛在滑手機"]
    filtered = [o for o in options if o != last_bot] or options
    return random.choice(filtered)


def load_recent_turns() -> list[dict]:
    if not MEMORY_JSON_PATH.exists():
        return []
    try:
        data = json.loads(MEMORY_JSON_PATH.read_text(encoding="utf-8"))
        raw_turns = data.get("recent_turns", [])
        if not isinstance(raw_turns, list):
            return []

        normalized: list[dict] = []
        for item in raw_turns[-10:]:
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
        for t in turns[-10:]
        if isinstance(t, dict) and "role" in t and "content" in t
    ]
    payload = {"recent_turns": clean_turns, "updated_at": now_sydney().isoformat()}
    tmp_path = MEMORY_JSON_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, MEMORY_JSON_PATH)


def detect_emotion(text: str) -> str:
    sad_kw = ["難過", "低落", "好煩", "崩潰", "委屈", "不開心", "焦慮", "被退稿", "生氣", "累死", "不舒服", "頭暈"]
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
    elif any(k in user_text for k in ["你累不累", "趕稿", "早點睡", "熬夜", "沒睡好", "淺眠"]):
        set_physical_state("趕稿有點累、眼睛酸想休息", hours=3.0)
    elif any(k in user_text for k in ["著涼", "吹風", "偏頭痛", "那個來", "生理期", "低血糖", "頭暈"]):
        set_physical_state("身體有點微倦、偏弱需休息", hours=3.5)

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


def _deduplicate_repeated_clauses(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) >= 12:
        half = len(cleaned) // 2
        for split_idx in range(half - 4, half + 5):
            if 0 < split_idx < len(cleaned):
                left = cleaned[:split_idx].strip()
                right = cleaned[split_idx:].strip()
                if left and right and SequenceMatcher(None, left, right).ratio() >= 0.75:
                    return left

    sentences = [s.strip() for s in re.split(r"(?<=[。！!？?~～\n])\s*", cleaned) if s.strip()]
    unique_sentences: list[str] = []
    for s in sentences:
        if any(SequenceMatcher(None, s, prev).ratio() >= 0.75 for prev in unique_sentences):
            continue
        unique_sentences.append(s)
    return " ".join(unique_sentences).strip()


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
    return [word for word, cnt in counts.most_common(5) if cnt >= 2]


def _is_too_similar_to_recent(reply: str, recent_asst: list[str], hour: int) -> bool:
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
        "遠處咖啡店的香味",
        "你在什麼地方呢",
        "只要你吃得開心就好",
        "你吃的那份好吃嗎",
        "等會再吃點",
        "等會再補",
        "還能吃一份",
        "跟你說說好不",
        "別再叫我寶貝",
        "等你下班",
    ]
    if any(p in reply for p in banned_phrases):
        return True
    if hour < 22 and "宵夜" in reply:
        return True
    if ("結案" in reply or "結完案" in reply) and ("調整" in reply or "改稿" in reply):
        return True
    if reply.count("先") >= 2:
        return True
    for prev in recent_asst[-3:]:
        if not prev:
            continue
        if SequenceMatcher(None, reply, prev).ratio() >= 0.42:
            return True
    return False


def post_process_reply(reply: str, user_text: str, recent_turns: list[dict], hour: int) -> str:
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()
    reply = reply.replace("妳", "你")
    reply = _deduplicate_repeated_clauses(reply)

    clean_q = user_text.rstrip("？?！!。 ")
    if len(clean_q) >= 3 and reply.startswith(clean_q):
        reply = reply[len(clean_q) :].lstrip("？?！!，, ")

    replacements = {
        "你說啥？": "幹嘛～",
        "你說啥": "幹嘛",
        "別再叫我寶貝啦。": "幹嘛啦突然這樣",
        "別再叫我寶貝": "幹嘛啦突然這樣",
        "等你下班我打電話給你吧": "晚點有空再跟你講電話",
        "等你下班再聊": "晚點有空再聊",
        "等你下班": "等你忙完",
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
        "跟你說說好不？": "到時跟你說",
        "跟你說說好不": "到時跟你說",
    }
    for bad, good in replacements.items():
        reply = reply.replace(bad, good)

    if hour < 22:
        reply = reply.replace("宵夜", "點心")

    reply = re.sub(r"[，,。！!~～]*只要你(吃|喝)得(開心|舒服)就好[。！!~～]*", "", reply).strip()

    # 台灣女生聊天不打句尾全形句號
    reply = reply.rstrip("。")
    reply = reply.replace("。", "，")

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
        parts = re.split(r"(?<=[！!~～\n])\s*", reply)
        if len(parts) >= 2 and parts[-1].rstrip().endswith(("？", "?")):
            candidate = "".join(parts[:-1]).strip()
            if len(candidate) >= 4:
                reply = candidate

    return _deduplicate_repeated_clauses(reply).rstrip("。")


def _call_gemini_chat(messages: list[dict], temp: float = 0.75) -> str:
    if not GEMINI_API_KEY:
        return ""

    system_parts = [m["content"] for m in messages if m.get("role") == "system" and m.get("content")]
    system_instruction = "\n\n".join(system_parts)

    contents: list[dict] = []
    for m in messages:
        role = m.get("role")
        text = (m.get("content") or "").strip()
        if not text or role == "system":
            continue
        gemini_role = "model" if role == "assistant" else "user"
        if contents and contents[-1]["role"] == gemini_role:
            contents[-1]["parts"][0]["text"] += f"\n{text}"
        else:
            contents.append({"role": gemini_role, "parts": [{"text": text}]})

    if not contents:
        return ""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload: dict = {
        "contents": contents,
        "generationConfig": {
            "temperature": temp,
            "maxOutputTokens": 180,
        },
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    try:
        resp = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": GEMINI_API_KEY,
            },
            json=payload,
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"[Gemini Primary] HTTP {resp.status_code}: {resp.text[:200]}")
            return ""
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts") or []
        text_out = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        return text_out
    except Exception as exc:
        print(f"[Gemini Primary] Error: {exc}")
        return ""


def _call_groq_chat(messages: list[dict], temp: float = 0.72) -> str:
    if not groq_client:
        return ""
    last_exc: Exception | None = None

    for model_name in GROQ_MODEL_CANDIDATES:
        kwargs: dict = {
            "model": model_name,
            "messages": messages,
            "temperature": temp,
            "max_tokens": 180,
            "frequency_penalty": 0.75,
            "presence_penalty": 0.65,
        }
        if "gpt-oss" in model_name:
            kwargs["reasoning_effort"] = "low"

        try:
            resp = groq_client.chat.completions.create(**kwargs)
        except TypeError:
            kwargs.pop("reasoning_effort", None)
            try:
                resp = groq_client.chat.completions.create(**kwargs)
            except Exception as exc:
                last_exc = exc
                continue
        except Exception as exc:
            last_exc = exc
            continue

        msg_obj = resp.choices[0].message
        content = (msg_obj.content or "").strip()
        if content:
            return content

    if last_exc:
        print(f"[Groq Fallback] All models failed: {last_exc}")
    return ""


def _call_llm_with_fallback(messages: list[dict], temp: float = 0.75) -> str:
    reply = _call_gemini_chat(messages, temp=temp)
    if reply:
        return reply
    return _call_groq_chat(messages, temp=temp)


def _time_context_note(hour: int) -> str:
    if 8 <= hour < 11:
        return "\n【時段】早上。"
    if 11 <= hour < 15:
        return "\n【時段】中午到下午初。"
    if 15 <= hour < 17:
        return "\n【時段】下午，別聊早餐或宵夜。"
    if 17 <= hour < 22:
        return "\n【時段】晚上（晚餐後休閒時間，禁提宵夜或早餐，也別擅自說等你下班）。"
    return "\n【時段】深夜休息時間。"


def generate_reona_reply(user_text: str, was_sulking: bool, shared_story: bool) -> str:
    recent_turns = load_recent_turns()
    snapshot = compute_snapshot()
    now = now_sydney()

    recent_asst = [
        t.get("content", "")
        for t in recent_turns[-4:]
        if isinstance(t, dict) and t.get("role") == "assistant"
    ]

    repeated_terms = _find_repeated_terms(recent_turns)
    anti_loop_note = f"\n【防跳針】少提「{'、'.join(repeated_terms)}」，絕不重複上一句說過的話。" if repeated_terms else "\n【防跳針】絕不重複上一句說過的話。"
    no_question_note = "\n【句型】上一句已問過，這句別再反問。" if (recent_asst and recent_asst[-1].rstrip().endswith(("？", "?"))) else ""
    full_note = "\n【飽足】Nick 飽了/吃不下，別叫他等會再吃。" if any(k in user_text for k in ["吃不下", "好飽", "吃飽了", "撐了", "不行"]) else ""
    pov_note = "\n【視角】Nick 問的是你（Reona）剛吃/喝的東西，用「我」答。" if (any(k in user_text for k in ["好吃嗎", "會辣嗎", "甜嗎", "鹹嗎", "好喝嗎"]) and "我" not in user_text) else ""

    sulk_note = ""
    if was_sulking and shared_story:
        sulk_note = "\n【情緒】剛密 Nick 3 次沒回小生氣，但他分享近況了，先嘴硬小唸再消氣。"
    elif was_sulking:
        sulk_note = "\n【情緒】剛密 Nick 3 次沒回在傲嬌，他終於回了，帶點小委屈。"

    time_note = _time_context_note(now.hour)

    messages = [
        {
            "role": "system",
            "content": f"{SYSTEM_PROMPT}\n\n{snapshot}{time_note}{anti_loop_note}{no_question_note}{full_note}{pov_note}{sulk_note}",
        }
    ]
    messages.extend(recent_turns[-6:])
    messages.append({"role": "user", "content": user_text})

    raw_reply = _call_llm_with_fallback(messages, temp=0.75)
    final_reply = post_process_reply(raw_reply, user_text, recent_turns, now.hour)

    if _is_too_similar_to_recent(final_reply, recent_asst, now.hour):
        retry_messages = messages + [
            {
                "role": "system",
                "content": f"「{final_reply}」與前文重複或時間邏輯不對。請直接針對 Nick 的「{user_text}」用台灣 24 歲獨立女生口語簡短回 1 句，不加句號、不重複舊話。",
            }
        ]
        raw_retry = _call_llm_with_fallback(retry_messages, temp=0.85)
        retry_processed = post_process_reply(raw_retry, user_text, recent_turns, now.hour)
        if retry_processed and not _is_too_similar_to_recent(retry_processed, recent_asst, now.hour):
            final_reply = retry_processed

    if not final_reply or _is_too_similar_to_recent(final_reply, recent_asst, now.hour):
        last_bot = recent_asst[-1] if recent_asst else ""
        final_reply = _contextual_fallback(user_text, last_bot)

    if any(k in final_reply for k in ["肚子痛", "腸胃", "拉肚子", "吃太辣", "吃太油"]):
        set_physical_state("腸胃有點不舒服", hours=4.0)
    elif any(k in final_reply for k in ["好累", "想睡覺", "趕稿", "眼睛酸", "肩膀硬", "沒睡好"]):
        set_physical_state("趕稿有點累、想休息", hours=3.0)
    elif any(k in final_reply for k in ["低血糖", "頭暈", "著涼", "噴嚏", "那個", "生理期"]):
        set_physical_state("身體有點微倦、偏弱需休息", hours=3.5)

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
        await send_with_optional_voice(channel, msg, voice_chance=0.35)
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
            is_flirty_or_affectionate = was_sulking or any(
                k in user_text for k in ["寶貝", "愛你", "想你", "泡你"]
            )
            voice_chance = 0.20 if is_flirty_or_affectionate else 0.0
            await send_with_optional_voice(message.channel, reply.strip(), voice_chance=voice_chance)

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

        if random.random() > 0.12:
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
            voice_chance = 0.25
        elif state["unanswered_pings"] > 3:
            return
        else:
            candidates = [m for m in PING_MESSAGES_NORMAL if m != last_bot_msg]
            msg = random.choice(candidates)
            voice_chance = 0.0

        if msg and msg.strip():
            recent_turns.append({"role": "assistant", "content": msg.strip()})
            save_recent_turns_atomic(recent_turns)
            await send_with_optional_voice(channel, msg.strip(), voice_chance=voice_chance)

    @proactive_ping_loop.before_loop
    async def before_ping_loop(self) -> None:
        await self.wait_until_ready()


if __name__ == "__main__":
    bot = ReonaBot()
    bot.run(DISCORD_TOKEN)
