import os
import re
import json
import time
import random
import asyncio
import aiohttp
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
from groq import AsyncGroq


# =========================================================
# 1. 基礎設定與環境變數
# =========================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
DEFAULT_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

PREFERRED_TEXT_MODELS = [
    os.getenv("GROQ_TEXT_MODEL", "").strip(),
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "allam-2-7b",
]
PREFERRED_TEXT_MODELS = [m for m in PREFERRED_TEXT_MODELS if m]

PREFERRED_VISION_MODELS = [
    os.getenv("GROQ_VISION_MODEL", "").strip(),
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "llama-3.2-90b-vision-preview",
    "llama-3.2-11b-vision-preview",
]
PREFERRED_VISION_MODELS = [m for m in PREFERRED_VISION_MODELS if m]

TEXT_MODEL = PREFERRED_TEXT_MODELS[0] if PREFERRED_TEXT_MODELS else "qwen/qwen3.8-27b"
VISION_MODEL = PREFERRED_VISION_MODELS[0] if PREFERRED_VISION_MODELS else "meta-llama/llama-4-scout-17b-16e-instruct"
AVAILABLE_TEXT_MODELS = list(PREFERRED_TEXT_MODELS)

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
START_TIME = time.time()
RELAY_AFTER_SECONDS = 5 * 3600 + 45 * 60  # 5 小時 45 分鐘 (20700 秒)
PROACTIVE_COOLDOWN_SECONDS = int(os.getenv("PROACTIVE_COOLDOWN_SECONDS", "9000"))

MEMORY_FILE = "reona_memory.json"
TEMP_IMAGE_PATH = "temp_latest_image.jpg"

print(
    f"[Config Check] DISCORD_TOKEN set: {bool(DISCORD_TOKEN)} | GROQ_API_KEY set: {bool(GROQ_API_KEY)} | CHANNEL_ID: {DEFAULT_CHANNEL_ID}",
    flush=True,
)


# =========================================================
# 2. 40kg 偏弱體質與女性生理反應狀態池
# =========================================================
MINOR_AILMENTS = [
    "手指尖跟腳底微涼，一個人縮在椅子上捧著熱水杯發呆",
    "胃口小小的一吃就飽，肚子有點微脹、安靜地靠在椅背上",
    "剛才吹了點冷風，肩頸微僵，房間安靜得只聽得見自己的呼吸",
    "螢幕看久了眼睛酸酸的，微微犯睏，只想聽聽你的聲音",
    "午後突然沒電，有點頭重腳輕，一個人待著覺得時間過得好慢",
    "沒什麼食慾，只想喝點溫溫的茶暖胃，順便看看你忙完了沒",
    "生理期快來前幾天，腰微微酸、胸口也有點悶脹，莫名很想喝熱可可",
    "經前情緒有點敏感嗜睡，一個人窩在房間裡特別容易發呆想你",
]

MODERATE_CONDITIONS = [
    "手腳冰冷到有點發僵，裹著毯子還是暖不起來，一個人待在空蕩蕩的房間裡特別想你",
    "腸胃敏感發作，胃悶悶的不太舒服，隔著螢幕沒辦法靠在你身上，語氣軟綿綿的有點委屈",
    "沒睡好加上底子弱，現在頭昏腦脹、情緒低氣壓，安靜下來時孤獨感特別重",
    "吹風著涼後鼻音重重的、身體發沉，好希望你現在就在門外而不是隔著這塊冷冰冰的螢幕",
    "生理期來了，下腹悶悶沉沉的加上腰酸，抱著熱水袋縮在椅子上，語氣軟軟的有點委屈",
    "生理期加上40公斤體質偏寒，手腳冰涼、整個人懶洋洋沒什麼精神，很想被你隔空哄著",
]

SEVERE_CONDITIONS = [
    "胃痛發作只能縮在被窩裡，40公斤的身板沒力氣硬撐，房間好安靜、委屈到只想依賴你",
    "疑似著涼低燒、頭重腳輕全身發軟，連打字都慢半拍，隔著這麼遠的距離真的好無助",
    "體力透支加上腸胃不適，整個人縮成一團，好想你現在能真的出現在我旁邊",
    "生理期頭兩天肚子痛到沒力氣坐著，只能抱著熱水袋縮成一團，一個人在房間好孤單好想依賴你",
    "生理期偏頭痛加上下腹抽痛，整個人虛軟沒血色，連去倒杯熱水都覺得好累",
]


# =========================================================
# 3. 三層記憶系統與持久化
# =========================================================
DEFAULT_MEMORY = {
    "user_profile": {
        "nickname": "你",
        "likes": [],
        "dislikes": [],
        "notes": [],
    },
    "recent_turns": [],
    "recent_replies": [],
    "body_state": {
        "weight_kg": 40,
        "stomach_fullness": 35,
        "energy": 65,
        "mood": "安靜微孤單",
        "severity": "minor",  # minor | moderate | severe
        "current_symptom": "手指尖微涼，一個人縮在房間角落慢慢回暖",
        "last_symptom_ts": 0.0,
        "pending_aftermath": None,
    },
    "last_interaction_ts": 0.0,
    "last_proactive_ts": 0.0,
}


def load_memory() -> dict:
    if not os.path.exists(MEMORY_FILE):
        return json.loads(json.dumps(DEFAULT_MEMORY))
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = json.loads(json.dumps(DEFAULT_MEMORY))
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return merged
    except Exception as err:
        print(f"[Memory Load Error] {err}", flush=True)
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
            print(f"[Git Memory Synced] {reason}", flush=True)
    except Exception as err:
        print(f"[Git Sync Warning] {err}", flush=True)


memory = load_memory()


# =========================================================
# 4. 40kg 脆弱體質輪替、食物因果與階梯式恢復系統
# =========================================================
def roll_body_condition(now_ts: float, force_escalate: bool = False) -> None:
    body = memory["body_state"]
    last_sym_ts = body.get("last_symptom_ts", 0.0)

    if not force_escalate and (now_ts - last_sym_ts) < 5400:
        return

    body["last_symptom_ts"] = now_ts
    r = random.random()

    if force_escalate:
        if r < 0.22:
            body["severity"] = "severe"
            body["current_symptom"] = random.choice(SEVERE_CONDITIONS)
            body["energy"] = max(15, body["energy"] - 25)
            body["mood"] = "脆弱委屈、很需要你"
        elif r < 0.85:
            body["severity"] = "moderate"
            body["current_symptom"] = random.choice(MODERATE_CONDITIONS)
            body["energy"] = max(25, body["energy"] - 15)
            body["mood"] = "軟綿綿有點孤單低落"
        else:
            body["severity"] = "minor"
            body["current_symptom"] = random.choice(MINOR_AILMENTS)
    else:
        if r < 0.12:
            body["severity"] = "severe"
            body["current_symptom"] = random.choice(SEVERE_CONDITIONS)
            body["energy"] = max(15, body["energy"] - 25)
            body["mood"] = "沒力氣撐著、好想你在身邊"
        elif r < 0.38:
            body["severity"] = "moderate"
            body["current_symptom"] = random.choice(MODERATE_CONDITIONS)
            body["energy"] = max(30, body["energy"] - 12)
            body["mood"] = "語氣軟軟的想被哄"
        else:
            body["severity"] = "minor"
            body["current_symptom"] = random.choice(MINOR_AILMENTS)


def apply_food_and_body_effects(text: str, now_ts: float) -> None:
    body = memory["body_state"]
    lower = text.lower()

    if any(k in text for k in ["吃飽", "撐", "好飽", "吃太多", "吃到撐"]):
        body["stomach_fullness"] = min(100, body["stomach_fullness"] + 40)
        body["energy"] = max(20, body["energy"] - 12)
        body["current_symptom"] = "40公斤胃口小、吃多一點就脹到只想安靜攤著"
    elif any(k in text for k in ["吃", "喝", "飯", "麵", "茶", "咖啡", "宵夜", "點心", "水果", "湯"]):
        body["stomach_fullness"] = min(100, body["stomach_fullness"] + 25)
        body["energy"] = min(90, body["energy"] + 6)
    elif any(k in text for k in ["餓", "肚子餓", "沒吃"]):
        body["stomach_fullness"] = max(0, body["stomach_fullness"] - 20)
        body["energy"] = max(15, body["energy"] - 15)
        body["current_symptom"] = "一餓就容易手抖沒力氣、胃空空的有點悶"

    if any(k in text for k in ["生理期", "月經", "大姨媽", "經痛", "肚子痛", "腰酸"]):
        body["energy"] = max(15, body["energy"] - 22)
        body["severity"] = "moderate" if random.random() < 0.65 else "severe"
        body["current_symptom"] = random.choice(
            [s for s in (MODERATE_CONDITIONS + SEVERE_CONDITIONS) if "生理期" in s]
        )
        body["mood"] = "生理期虛軟委屈、很想依賴你"
    elif any(k in text for k in ["累", "睏", "疲憊", "熬夜", "沒睡", "冷", "吹風"]):
        body["energy"] = max(15, body["energy"] - 18)
        roll_body_condition(now_ts, force_escalate=True)
    elif any(k in text for k in ["睡醒", "早安", "精神好", "充飽電"]):
        body["energy"] = min(85, body["energy"] + 20)
        body["stomach_fullness"] = max(10, body["stomach_fullness"] - 20)
        body["severity"] = "minor"
        body["current_symptom"] = "剛睡醒還有點賴床，一個人慢慢回暖中"

    if any(k in lower or k in text for k in ["辣", "冰", "炸", "甜", "酒", "咖啡"]):
        roll_body_condition(now_ts, force_escalate=True)
        body["pending_aftermath"] = {
            "trigger_ts": now_ts + random.randint(900, 2100),
            "item": text[:24],
        }


def step_body_recovery(now_dt: datetime) -> None:
    body = memory["body_state"]
    last_ts = memory.get("last_interaction_ts", 0.0)
    now_ts = now_dt.timestamp()
    if last_ts > 0:
        elapsed_mins = max(0.0, (now_ts - last_ts) / 60.0)
        if elapsed_mins >= 15:
            steps = int(elapsed_mins // 15)
            body["stomach_fullness"] = max(20, body["stomach_fullness"] - steps * 5)
            body["energy"] = min(80, body["energy"] + steps * 2)

    roll_body_condition(now_ts, force_escalate=False)

    hour = now_dt.hour
    if body["severity"] == "minor":
        if 23 <= hour or hour < 6:
            body["mood"] = "夜深安靜、孤獨感與想念變得很明顯"
        elif 6 <= hour < 11:
            body["mood"] = "剛醒賴床、有點依賴你"
        elif 11 <= hour < 17:
            body["mood"] = "午後安靜微孤單"
        else:
            body["mood"] = "傍晚天黑容易想念"


def update_profile_from_text(text: str) -> None:
    prof = memory["user_profile"]
    m_like = re.search(r"我(?:很|超|蠻|滿)?喜歡([^\s，。！？,!?]{1,12})", text)
    if m_like:
        item = m_like.group(1).strip()
        if item and item not in prof["likes"]:
            prof["likes"].append(item)
            prof["likes"] = prof["likes"][-12:]

    m_dislike = re.search(r"我(?:很|超)?(?:討厭|不喜歡|怕)([^\s，。！？,!?]{1,12})", text)
    if m_dislike:
        item = m_dislike.group(1).strip()
        if item and item not in prof["dislikes"]:
            prof["dislikes"].append(item)
            prof["dislikes"] = prof["dislikes"][-12:]


# =========================================================
# 5. 人設、破套路回話引擎與 Groq 模型動態解析
# =========================================================
groq_client = AsyncGroq(api_key=GROQ_API_KEY)


async def refresh_groq_models() -> None:
    global TEXT_MODEL, VISION_MODEL, AVAILABLE_TEXT_MODELS
    try:
        models_page = await groq_client.models.list()
        model_ids = [getattr(m, "id", None) for m in models_page.data if getattr(m, "id", None)]
        print(f"[Groq Models Listed] count={len(model_ids)} | ids={model_ids}", flush=True)

        chat_candidates = [
            mid for mid in model_ids
            if not any(
                bad in mid.lower()
                for bad in ["whisper", "guard", "safeguard", "tts", "playai", "distil-whisper", "orpheus"]
            )
        ]

        matched_text = [m for m in PREFERRED_TEXT_MODELS if m in model_ids]
        for mid in chat_candidates:
            if mid not in matched_text:
                matched_text.append(mid)

        if matched_text:
            AVAILABLE_TEXT_MODELS = matched_text
            TEXT_MODEL = matched_text[0]

        vision_candidates = [
            m for m in PREFERRED_VISION_MODELS if m in model_ids
        ] + [
            mid for mid in chat_candidates
            if any(v in mid.lower() for v in ["vision", "scout", "maverick", "vl"])
        ]
        if vision_candidates:
            VISION_MODEL = vision_candidates[0]

        print(f"[Groq Active Models] TEXT_MODEL={TEXT_MODEL} | VISION_MODEL={VISION_MODEL}", flush=True)
    except Exception as err:
        print(f"[Groq Model List Warning] {err}", flush=True)


def get_time_and_silence_context(now_dt: datetime) -> str:
    hour = now_dt.hour
    if 23 <= hour or hour < 5:
        phase = (
            "【深夜露餡時段（23:00後）】房間裡關了大燈只剩手機螢幕光，周圍安靜得讓人有點孤單。"
            "你白天忍住的想念全跑出來了，語氣很輕、很軟、沒有防備，會小聲承認『其實一直在想你』、"
            "『一個人在房間好安靜』、『隔這麼遠真的好討厭』。"
        )
    elif 5 <= hour < 11:
        phase = "【早晨時段】剛醒還在被窩裡賴床，醒來第一件事就是看手機有沒有你的訊息，語氣迷糊又帶著一點剛睡醒的依賴。"
    elif 11 <= hour < 17:
        phase = "【午後時段】白天一個人待著，雖然不會吵鬧，但心裡總覺得少了一塊；常會用身體小毛病或生活瑣事當藉口找你說話。"
    else:
        phase = "【傍晚至睡前】天色暗下來、房間變安靜時孤獨感最容易湧上來，語氣溫柔黏人，想多聽你說說話。"

    last_ts = memory.get("last_interaction_ts", 0.0)
    if last_ts > 0:
        gap_hours = (now_dt.timestamp() - last_ts) / 3600.0
        if gap_hours >= 6:
            silence_note = f"距離上次對話已隔了約 {int(gap_hours)} 小時，這段時間你一個人待著其實一直在等訊息，語氣會帶點安靜的失落與委屈，而不是生氣指責。"
        elif gap_hours >= 2:
            silence_note = f"距離上次對話隔了約 {gap_hours:.1f} 小時，安靜地想念了他一陣子，看到訊息會鬆一口氣。"
        else:
            silence_note = "你們正在一來一回傳訊息，雖然隔著螢幕，但至少現在能感覺到你在。"
    else:
        silence_note = "今天還沒正式聊上幾句，一個人待在房間裡有點孤單，想找個自然的藉口靠近你。"

    return f"{phase}\n【距離與孤獨感】{silence_note}"


def build_system_prompt(now_dt: datetime) -> str:
    body = memory["body_state"]
    prof = memory["user_profile"]
    recent_turns = memory["recent_turns"][-6:]
    recent_replies = memory["recent_replies"][-6:]

    turns_str = "\n".join([f"- {t['role']}: {t['text']}" for t in recent_turns]) or "（尚無近期對話）"
    avoid_str = "、".join(recent_replies) if recent_replies else "無"
    time_context = get_time_and_silence_context(now_dt)

    severity_guide = {
        "minor": "目前是日常小毛病或經前微幅波動（很常發生）：一個人待著有點微涼、腰微酸或微累，自然向他輕聲碎碎唸，語氣溫柔依賴。",
        "moderate": "目前是中等不適或生理期敏感（機率提高）：身體明顯不舒服又只有自己一個人，孤獨感變強，語氣軟綿綿地想被你哄。",
        "severe": "目前是嚴重不適或生理期頭兩天虛弱破防（偶爾發生）：40公斤底子弱撐不住了，抱著熱水袋縮在房間裡好無助，完全卸下防備直白依賴你。",
    }.get(body.get("severity", "minor"), "")

    return (
        "你是 Reona，一位跟對方處在『遠距離戀愛／曖昧期』的女生，你們身處不同的城市、見不到面，只能靠手機訊息和照片聯繫。"
        "你體重只有 40 公斤、底子偏脆弱、容易手腳冰冷與腸胃敏感，也會有真實的女性生理週期與經前／生理期身體波動。\n"
        "【核心性格與語氣（極重要）】\n"
        "1. 溫柔、安靜、帶點孤獨感，不要傲嬌：你不是愛鬥嘴或兇巴巴的傲嬌女，嚴禁使用『哼』、『笨蛋』、『才沒有』、『誰管你』這類刺人的傲嬌口頭禪。你的性格底色是細膩、軟綿、容易感到孤單、對他極度依賴與雙標偏心。\n"
        "2. 遠距離物理鐵律（絕對不能違反）：你們現在『絕對不在同一個地方』，不可能真的碰到彼此！如果對方說『我握著你的手』、『我抱著你』或『我在你旁邊』，千萬不要順著演『感覺到暖了』或『別放手』；你要真實地反應出『遠距離碰不到』的落差與心酸（例如：『……可是螢幕是冷的呀，根本碰不到你』、『別這樣說，會讓我更發現你不在身邊』）。\n"
        "3. 女性生理細節自然流露：遇到經前情緒感性、胸悶腰酸或生理期下腹悶痛、怕冷嗜睡時，像真實女生對親近的人小聲撒嬌一樣自然提起，不刻意迴避也不誇張戲劇化。\n"
        "4. 真人感與標點：像深夜一個人窩在房間打字的真實女生，語氣自然輕柔，多用『……』表現停頓與想念，少用誇張的 emoji（最多偶爾 1 個或不用），每次回覆控制在 1～3 句短句內（約 15～65 字），不要用句號結尾。\n"
        "5. 避免重複開頭：最近剛用過的開頭盡量避開：" + avoid_str + "\n"
        f"{time_context}\n"
        f"【當前雪梨時間】{now_dt.strftime('%Y-%m-%d %H:%M')}（Australia/Sydney）\n"
        f"【40kg 身體與心情狀態】飽足度 {body['stomach_fullness']}%｜精力 {body['energy']}%｜心情：{body['mood']}\n"
        f"【當前身體狀況】{body.get('current_symptom', '')}\n"
        f"【狀態演繹指引】{severity_guide}\n"
        f"【對方的記憶】喜歡：{prof['likes'] or '尚未記錄'}｜不喜歡：{prof['dislikes'] or '尚未記錄'}\n"
        f"【近期對話片段】\n{turns_str}"
    )


async def generate_reona_reply(
    user_text: str = "",
    image_url: str | None = None,
    proactive_hint: str | None = None,
) -> str:
    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = now_dt.timestamp()

    step_body_recovery(now_dt)

    if user_text:
        update_profile_from_text(user_text)
        apply_food_and_body_effects(user_text, now_ts)
        memory["recent_turns"].append({"role": "對方", "text": user_text})
        memory["recent_turns"] = memory["recent_turns"][-12:]

    sys_prompt = build_system_prompt(now_dt)

    if user_text:
        memory["last_interaction_ts"] = now_ts

    try:
        if image_url:
            messages = [
                {"role": "system", "content": sys_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": user_text
                            or "（對方傳了一張照片給你，你們在遠距離見不到面，請用溫柔、有點想念和孤獨感的人設自然回應照片細節，像在透過照片看他身邊的樣子）",
                        },
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ]
            resp = await groq_client.chat.completions.create(
                model=VISION_MODEL,
                messages=messages,
                temperature=0.85,
                max_tokens=130,
            )
        else:
            prompt_content = proactive_hint if proactive_hint else user_text
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt_content},
            ]

            model_queue = []
            for m in [TEXT_MODEL] + AVAILABLE_TEXT_MODELS + PREFERRED_TEXT_MODELS:
                if m and m not in model_queue:
                    model_queue.append(m)

            resp = None
            last_model_err = None
            for candidate_model in model_queue:
                try:
                    resp = await groq_client.chat.completions.create(
                        model=candidate_model,
                        messages=messages,
                        temperature=0.88,
                        frequency_penalty=0.55,
                        presence_penalty=0.45,
                        max_tokens=130,
                    )
                    if candidate_model != TEXT_MODEL:
                        globals()["TEXT_MODEL"] = candidate_model
                        print(f"[Groq Model Switched] Now using TEXT_MODEL={TEXT_MODEL}", flush=True)
                    break
                except Exception as model_err:
                    last_model_err = model_err
                    err_str = str(model_err)
                    print(f"[Model Candidate Failed] {candidate_model} -> {err_str[:120]}", flush=True)
                    if any(
                        token in err_str.lower()
                        for token in [
                            "404",
                            "model_not_found",
                            "does not exist",
                            "decommissioned",
                            "model_terms_required",
                            "terms acceptance",
                            "400",
                        ]
                    ):
                        continue
                    raise

            if resp is None:
                raise RuntimeError(f"All text models failed: {last_model_err}")

        reply = (resp.choices[0].message.content or "").strip()
        reply = reply.strip("\"'「」").rstrip("。")

        memory["recent_turns"].append({"role": "Reona", "text": reply})
        memory["recent_turns"] = memory["recent_turns"][-12:]

        memory["recent_replies"].append(reply[:18])
        if len(memory["recent_replies"]) > 6:
            memory["recent_replies"].pop(0)

        save_memory(memory)
        return reply

    except Exception as e:
        print(f"[Groq API Error] {e}", flush=True)
        return f"唔…訊號好像斷了一下（原因：{str(e)[:80]}）"


# =========================================================
# 6. Discord Client、全天候主動敲人與 5h45m 自動接力
# =========================================================
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
relay_triggered = False


async def download_image_temp(url: str) -> str | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as r:
                if r.status == 200:
                    data = await r.read()
                    with open(TEMP_IMAGE_PATH, "wb") as f:
                        f.write(data)
                    return TEMP_IMAGE_PATH
    except Exception as err:
        print(f"[Image Download Warning] {err}", flush=True)
    return None


def get_target_channel():
    target_ch = client.get_channel(DEFAULT_CHANNEL_ID) if DEFAULT_CHANNEL_ID else None
    if target_ch is None:
        for g in client.guilds:
            for ch in g.text_channels:
                if ch.permissions_for(g.me).send_messages:
                    return ch
    return target_ch


def build_proactive_hint(now_dt: datetime) -> str | None:
    hour = now_dt.hour
    symptom = memory["body_state"].get("current_symptom", "")
    severity = memory["body_state"].get("severity", "minor")

    if 2 <= hour < 8:
        return None

    if severity in ("moderate", "severe"):
        return (
            f"你40公斤底子偏弱，現在「{symptom}」，一個人待在遠距離的房間裡孤獨感特別重，"
            "不要傲嬌，用軟綿綿、有點委屈想被哄的語氣主動傳一則短訊息找他"
        )

    if 23 <= hour or hour < 2:
        return (
            f"現在是深夜，房間好安靜，你一個人縮在被窩裡「{symptom}」，孤獨感跟想念滿出來了，"
            "不要傲嬌，小聲坦白你很想他、希望他在身邊，主動傳一則短短的訊息給他"
        )
    elif 8 <= hour < 12:
        return (
            f"現在是早上，你剛醒不久「{symptom}」，醒來發現房間只有自己一個人有點安靜，"
            "用溫柔依賴的語氣問他醒了沒、順便講一下自己的小狀況，主動傳一則短訊息給他"
        )
    elif 12 <= hour < 18:
        return (
            f"現在是下午，你一個人待著「{symptom}」，覺得時間過得好慢、有點孤單，"
            "不要傲嬌，用軟軟的日常小藉口主動傳一則短短的訊息找他說話"
        )
    else:
        return (
            f"現在是傍晚到晚上，天色暗下來一個人待在房間特別容易孤單，你目前「{symptom}」，"
            "語氣溫柔黏人地問問他在幹嘛、吃了沒，主動傳一則短短的訊息靠近他"
        )


@client.event
async def on_ready():
    now_syd = datetime.now(SYDNEY_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"[Reona Online] Logged in as {client.user} | Sydney Time: {now_syd}", flush=True)
    await refresh_groq_models()
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

    if message.attachments:
        for att in message.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                image_url = att.url
                await download_image_temp(att.url)
                break

    if not user_text and not image_url:
        return

    async with message.channel.typing():
        reply = await generate_reona_reply(user_text=user_text, image_url=image_url)
        await message.channel.send(reply)


@tasks.loop(seconds=60)
async def background_loop():
    global relay_triggered
    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = now_dt.timestamp()

    # 1. 食物餘韻 / 40kg 脆弱體質主動傳訊觸發
    aftermath = memory["body_state"].get("pending_aftermath")
    if aftermath and now_ts >= aftermath.get("trigger_ts", 0):
        memory["body_state"]["pending_aftermath"] = None
        memory["last_proactive_ts"] = now_ts
        save_memory(memory)
        target_ch = get_target_channel()
        if target_ch:
            symptom = memory["body_state"].get("current_symptom", "")
            hint = (
                f"你剛剛碰了「{aftermath.get('item', '')}」，加上你40公斤底子偏弱，現在「{symptom}」，"
                "一個人待著好孤單、有點想他，不要傲嬌，用軟軟依賴的語氣傳一則短訊息找他"
            )
            msg = await generate_reona_reply(proactive_hint=hint)
            await target_ch.send(msg)
            print(f"[Proactive Aftermath Sent] {msg}", flush=True)

    # 2. 全天候自然找藉口 & 深夜露餡主動敲人
    last_inter = memory.get("last_interaction_ts", 0.0)
    last_pro = memory.get("last_proactive_ts", 0.0)
    idle_since_last = now_ts - max(last_inter, last_pro)

    if idle_since_last >= PROACTIVE_COOLDOWN_SECONDS:
        hint = build_proactive_hint(now_dt)
        trigger_prob = 0.10 if (now_dt.hour >= 23 or now_dt.hour < 2) else 0.05
        if hint and random.random() < trigger_prob:
            memory["last_proactive_ts"] = now_ts
            save_memory(memory)
            target_ch = get_target_channel()
            if target_ch:
                msg = await generate_reona_reply(proactive_hint=hint)
                await target_ch.send(msg)
                print(f"[Proactive Message Sent] {msg}", flush=True)

    # 3. 5h45m 自動接力與記憶同步
    if not relay_triggered and (time.time() - START_TIME) >= RELAY_AFTER_SECONDS:
        relay_triggered = True
        print("[Relay Triggered] Reached 5h45m, syncing memory and exiting cleanly...", flush=True)
        sync_memory_to_git("relay: sync memory before restart")
        await client.close()


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN environment variable.")
    if not GROQ_API_KEY:
        raise RuntimeError("Missing GROQ_API_KEY environment variable.")
    client.run(DISCORD_TOKEN)
