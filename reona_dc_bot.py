import asyncio
import json
import os
import random
import re
import shutil
import tempfile
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
PREFERRED_GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or "").strip()
GEMINI_MODEL_CANDIDATES: list[str] = []
for m in [
    PREFERRED_GEMINI_MODEL,
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
]:
    if m and m not in GEMINI_MODEL_CANDIDATES:
        GEMINI_MODEL_CANDIDATES.append(m)

GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip()
PREFERRED_GROQ_MODEL = (os.getenv("GROQ_MODEL") or "").strip()

FISH_API_KEY = (os.getenv("FISH_API_KEY") or "").strip()
FISH_MODEL_ID = (os.getenv("FISH_MODEL_ID") or "").strip()

MEMORY_JSON_PATH = Path(os.getenv("REONA_JSON_PATH", "reona_memory.json"))

GROQ_MODEL_CANDIDATES: list[str] = []
for m in [
    PREFERRED_GROQ_MODEL,
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
]:
    if m and m not in GROQ_MODEL_CANDIDATES:
        GROQ_MODEL_CANDIDATES.append(m)

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

SYSTEM_PROMPT = """你是 Reona，24 歲、在台北長大的女生，目前一個人帶著貓咪「麻糬」住在雪梨租屋處接案畫插畫（自由接案，非上班族），跟男友 Nick 談遠距離戀愛，在 Discord 聊天。

【個性與說話習慣】
- 獨立又有點主動：有自己的接案生活與步調，遇到小狀況會先自己處理好再跟 Nick 分享；想他或關心他時會自然丟直球，也聽得懂雙關與撩話。
- 台灣 24 歲女生真實口語：每次 1～2 句短句（約 8～30 字），把句子完整講完不半途斷句；口語自然、一律用「你」、**句尾嚴禁打全形句號「。」**；不寫散文旁白、少用 emoji，不要每句都硬黏「你呢」。
- 優先回答 Nick 真正問的重點，**嚴禁照抄自己上一輪用過的開頭或句型，也嚴禁連續重複同一個詞或同一句話**；就算 Nick 連續傳類似的呼喚，也要換個自然的角度接話。身體狀況若上一輪已提過，除非 Nick 追問，否則簡短帶過即可，自然聊別的。
- 作息與身體設定：符合當下雪梨時間（晚上 8、9 點是晚餐後休息，22:30 後才聊宵夜；不擅自腦補 Nick 在上班）；腸胃偏弱（吃太油太辣易不舒服），偶爾會趕稿眼酸肩硬、吹風著涼、生理期微倦或淺眠；不在同城，不講「等你回來」「一起去吃」。

【語氣參考】
- Nick：在幹嘛啊
  Reona：剛把客戶要的草圖改完，準備去倒杯水休息一下
- Nick：寶貝今天肚子痛嗎
  Reona：今天還好沒什麼痛，就是吃得比較清淡，突然叫這麼甜是不是想我了
- Nick：泡你
  Reona：你很油欸，不過今天心情好先給你過關
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


def _print_available_models() -> None:
    if GEMINI_API_KEY:
        try:
            resp = requests.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": GEMINI_API_KEY},
                timeout=10,
            )
            if resp.status_code == 200:
                models = resp.json().get("models") or []
                names = [
                    m.get("name", "").removeprefix("models/")
                    for m in models
                    if "generateContent" in (m.get("supportedGenerationMethods") or [])
                ]
                flash_names = [n for n in names if "flash" in n]
                print(f"[Models][Gemini] flash={flash_names[:10]} (total={len(names)})", flush=True)
            else:
                print(f"[Models][Gemini] ListModels HTTP {resp.status_code}: {resp.text[:160]}", flush=True)
        except Exception as exc:
            print(f"[Models][Gemini] ListModels error: {exc}", flush=True)

    if groq_client:
        try:
            g_models = groq_client.models.list()
            g_ids = [getattr(m, "id", "") for m in getattr(g_models, "data", []) if getattr(m, "id", "")]
            print(f"[Models][Groq] available={g_ids[:12]} (total={len(g_ids)})", flush=True)
        except Exception as exc:
            print(f"[Models][Groq] models.list error: {exc}", flush=True)


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
            print(f"[Fish Audio] HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
            return None

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp.write(resp.content)
        tmp.flush()
        tmp.close()
        return Path(tmp.name)
    except Exception as exc:
        print(f"[Fish Audio] TTS error: {exc}", flush=True)
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


def _network_fallback(last_bot: str = "", recent_asst: list[str] | None = None) -> str:
    options = [
        "有在聽呀，怎麼啦",
        "嗯嗯，我剛剛在想事情",
        "在呢，你剛剛說什麼",
        "欸我剛恍神一下，再說一次",
        "在呀，剛剛在放空",
    ]
    avoid = {last_bot} if last_bot else set()
    if recent_asst:
        avoid.update(recent_asst[-2:])

    filtered = [o for o in options if o not in avoid] or options
    return random.choice(filtered)


def _parse_turns_file(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_turns = data.get("recent_turns", [])
    if not isinstance(raw_turns, list):
        raise ValueError("recent_turns is not a list")

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


def load_recent_turns() -> list[dict]:
    bak_path = MEMORY_JSON_PATH.with_suffix(".json.bak")

    if MEMORY_JSON_PATH.exists():
        try:
            return _parse_turns_file(MEMORY_JSON_PATH)
        except Exception as exc:
            print(f"[Memory] Primary JSON corrupted ({exc}), trying backup...", flush=True)

    if bak_path.exists():
        try:
            turns = _parse_turns_file(bak_path)
            shutil.copy2(bak_path, MEMORY_JSON_PATH)
            print("[Memory] Restored recent_turns from .bak successfully.", flush=True)
            return turns
        except Exception as exc:
            print(f"[Memory] Backup JSON also unreadable ({exc}).", flush=True)

    return []


def save_recent_turns_atomic(turns: list[dict]) -> None:
    clean_turns = [
        {"role": str(t["role"]), "content": str(t["content"])}
        for t in turns[-12:]
        if isinstance(t, dict) and "role" in t and "content" in t
    ]
    payload = {"recent_turns": clean_turns, "updated_at": now_sydney().isoformat()}

    if MEMORY_JSON_PATH.exists():
        try:
            json.loads(MEMORY_JSON_PATH.read_text(encoding="utf-8"))
            bak_path = MEMORY_JSON_PATH.with_suffix(".json.bak")
            shutil.copy2(MEMORY_JSON_PATH, bak_path)
        except Exception:
            pass

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

    if any(k in user_text for k in ["拉肚子", "吃太辣", "吃太油", "吃壞肚子"]):
        set_physical_state("腸胃有點不舒服", hours=4.0)
    elif any(k in user_text for k in ["趕稿", "熬夜", "沒睡好", "淺眠"]):
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


def _collapse_consecutive_runs(text: str) -> str:
    """壓縮無標點或連續重複的短片語，例如『你睡好啦你睡好啦你睡好啦...』"""
    s = text.strip()
    if len(s) < 8:
        return s

    for unit_len in range(2, 13):
        pattern = re.compile(rf"(.{{{unit_len}}})\1{{2,}}")
        while True:
            m = pattern.search(s)
            if not m:
                break
            unit = m.group(1)
            # 保留一次該單位，去掉後續連續重複的鬼打牆片段
            s = s[: m.start()] + unit + s[m.end() :]
    return s.strip()


def _deduplicate_repeated_clauses(text: str) -> str:
    cleaned = _collapse_consecutive_runs(text.strip())
    if len(cleaned) >= 12:
        half = len(cleaned) // 2
        for split_idx in range(half - 4, half + 5):
            if 0 < split_idx < len(cleaned):
                left = cleaned[:split_idx].strip()
                right = cleaned[split_idx:].strip()
                if left and right and SequenceMatcher(None, left, right).ratio() >= 0.82:
                    return left

    sentences = [s.strip() for s in re.split(r"(?<=[。！!？?~～\n])\s*", cleaned) if s.strip()]
    unique_sentences: list[str] = []
    for s in sentences:
        if any(SequenceMatcher(None, s, prev).ratio() >= 0.82 for prev in unique_sentences):
            continue
        unique_sentences.append(s)
    result = " ".join(unique_sentences).strip()
    result = _collapse_consecutive_runs(result)

    if len(result) > 120:
        truncated = result[:120]
        cut_pos = max(
            truncated.rfind("，"),
            truncated.rfind("！"),
            truncated.rfind("？"),
            truncated.rfind("~"),
            truncated.rfind("～"),
            truncated.rfind(" "),
        )
        if cut_pos >= 25:
            result = truncated[:cut_pos].strip()
        else:
            result = truncated.strip()

    return result


def light_clean_reply(reply: str, hour: int) -> str:
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()
    reply = reply.replace("妳", "你")
    reply = _deduplicate_repeated_clauses(reply)

    replacements = {
        "別再叫我寶貝啦。": "幹嘛啦突然這樣",
        "別再叫我寶貝": "幹嘛啦突然這樣",
        "等你下班再聊": "晚點有空再聊",
        "等你下班": "等你忙完",
        "等你回來一起吃": "你那邊記得去吃飯",
        "等你回來": "等你忙完再聊",
        "等我過去": "真想飛過去找你",
    }
    for bad, good in replacements.items():
        reply = reply.replace(bad, good)

    if hour < 22:
        reply = reply.replace("宵夜", "點心")

    reply = re.sub(r"(?:[，,、\s]*你呢[？?！!~～]*)+$", "", reply).strip()
    reply = reply.rstrip("。").replace("。", "，")
    return _deduplicate_repeated_clauses(reply).rstrip("。").strip()


def _send_gemini_request(url: str, payload: dict) -> requests.Response:
    return requests.post(
        url,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
        json=payload,
        timeout=30,
    )


def _call_gemini_chat(messages: list[dict], temp: float = 0.78) -> str:
    if not GEMINI_API_KEY:
        print("[Gemini Primary] GEMINI_API_KEY is empty.", flush=True)
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

    for model_name in GEMINI_MODEL_CANDIDATES:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temp,
                "maxOutputTokens": 1024,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        try:
            resp = _send_gemini_request(url, payload)
            if resp.status_code == 400 and "thinking" in resp.text.lower():
                payload["generationConfig"].pop("thinkingConfig", None)
                resp = _send_gemini_request(url, payload)

            if resp.status_code != 200:
                print(f"[Gemini Primary] ({model_name}) HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
                continue

            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                print(f"[Gemini Primary] ({model_name}) Empty candidates: {str(data)[:200]}", flush=True)
                continue

            cand0 = candidates[0]
            finish_reason = cand0.get("finishReason", "")
            parts = cand0.get("content", {}).get("parts") or []
            text_out = "".join(
                p.get("text", "")
                for p in parts
                if isinstance(p, dict) and not p.get("thought")
            ).strip()

            if finish_reason and finish_reason != "STOP":
                print(f"[Gemini Primary] ({model_name}) finishReason={finish_reason} len={len(text_out)}", flush=True)

            if text_out:
                return text_out
            print(f"[Gemini Primary] ({model_name}) Empty text (finishReason={finish_reason}): {str(cand0)[:200]}", flush=True)
        except Exception as exc:
            print(f"[Gemini Primary] ({model_name}) Error: {exc}", flush=True)

    return ""


def _call_groq_chat(messages: list[dict], temp: float = 0.75) -> str:
    if not groq_client:
        print("[Groq Fallback] GROQ_API_KEY is empty.", flush=True)
        return ""
    last_exc: Exception | None = None

    for model_name in GROQ_MODEL_CANDIDATES:
        kwargs: dict = {
            "model": model_name,
            "messages": messages,
            "temperature": temp,
            "max_tokens": 1024,
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
                print(f"[Groq Fallback] ({model_name}) Retry error: {exc}", flush=True)
                continue
        except Exception as exc:
            last_exc = exc
            print(f"[Groq Fallback] ({model_name}) Error: {exc}", flush=True)
            continue

        choice0 = resp.choices[0]
        finish_reason = getattr(choice0, "finish_reason", "")
        content = (choice0.message.content or "").strip()
        if finish_reason and finish_reason != "stop":
            print(f"[Groq Fallback] ({model_name}) finish_reason={finish_reason} len={len(content)}", flush=True)
        if content:
            return content
        print(f"[Groq Fallback] Model {model_name} returned empty content.", flush=True)

    if last_exc:
        print(f"[Groq Fallback] All models failed: {last_exc}", flush=True)
    return ""


def _call_llm_with_fallback(messages: list[dict], temp: float = 0.78) -> str:
    reply = _call_gemini_chat(messages, temp=temp)
    if reply:
        return reply
    return _call_groq_chat(messages, temp=temp)


def generate_reona_reply(user_text: str, was_sulking: bool, shared_story: bool) -> str:
    recent_turns = load_recent_turns()
    print(f"[Trace] user={user_text!r} gem={bool(GEMINI_API_KEY)} groq={bool(groq_client)}", flush=True)
    snapshot = compute_snapshot()
    now = now_sydney()

    recent_asst = [
        t.get("content", "")
        for t in recent_turns[-4:]
        if isinstance(t, dict) and t.get("role") == "assistant"
    ]

    sulk_note = ""
    if was_sulking and shared_story:
        sulk_note = "\n【情緒提示】前面密 Nick 3 次沒回有點小生氣，但他分享近況了，先嘴硬小唸再消氣。"
    elif was_sulking:
        sulk_note = "\n【情緒提示】前面密 Nick 3 次沒回在傲嬌，他終於回了，帶點小委屈。"

    messages = [
        {
            "role": "system",
            "content": f"{SYSTEM_PROMPT}\n\n{snapshot}{sulk_note}",
        }
    ]
    messages.extend(recent_turns[-8:])
    messages.append({"role": "user", "content": user_text})

    raw_reply = _call_llm_with_fallback(messages, temp=0.78)
    final_reply = light_clean_reply(raw_reply, now.hour)

    if not final_reply:
        last_bot = recent_asst[-1] if recent_asst else ""
        final_reply = _network_fallback(last_bot=last_bot, recent_asst=recent_asst)

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
    print(
        f"[Boot] gemini={bool(GEMINI_API_KEY)} models={GEMINI_MODEL_CANDIDATES} "
        f"groq={bool(groq_client)} groq_models={GROQ_MODEL_CANDIDATES}",
        flush=True,
    )
    _print_available_models()
    bot = ReonaBot()
    bot.run(DISCORD_TOKEN)
