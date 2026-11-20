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
# 2. 日常玩樂美食分享池 + 40kg 偏弱體質與女性生理反應池
# =========================================================
DAILY_HIGHLIGHTS = [
    "剛才去巷口那家小店買了剛出爐的菠蘿包，配熱奶茶超幸福",
    "下午趁天氣好出去散步曬了太陽，還看到一隻超胖的橘貓在打瞌睡",
    "今天去逛了文具店跟小市集，看到一個小吊飾覺得超像你",
    "剛吃了一碗熱騰騰的烏龍麵，可是40公斤胃口太小只吃一半就飽了",
    "在咖啡廳點了熱可可跟小蛋糕，坐窗邊看路人發呆覺得好愜意",
    "去超市逛了一圈買了草莓跟優格，回來路上心情挺好的",
    "今天嘗試自己煮了番茄蛋湯，味道意外還不錯，好想盛一碗給你喝",
]

MINOR_AILMENTS = [
    "手指尖跟腳底微涼，正捧著熱水杯慢慢回暖，順便滑手機看你的動態",
    "剛吃完東西有點小滿足，不過40公斤胃口小，肚子微微脹想賴在椅子上",
    "外面風有點涼，剛才出門逛了一下回來肩頸微僵，窩進外套裡好舒服",
    "看手機看久了眼睛有點酸酸的，微微犯睏想伸個懶腰",
    "午後稍微有點沒電，想泡杯熱茶提神再繼續忙",
    "生理期快來前幾天，腰微微酸、胸口也有點悶脹，特別嘴饞想吃甜的",
    "經前情緒有點敏感嗜睡，但喝到熱熱的飲料心情就變好很多",
]

MODERATE_CONDITIONS = [
    "手腳冰冷到有點發僵，裹著毯子抱熱水杯取暖，語氣軟綿綿的想跟你討拍",
    "腸胃敏感發作，胃悶悶的不太舒服，只能乖乖喝溫水休息、有點想被哄",
    "昨晚沒睡好加上底子弱，今天頭昏腦脹沒什麼精神，只想窩著跟你說說話",
    "吹風著涼後鼻音重重的、身體發沉，好希望你現在也在旁邊幫我倒杯溫水",
    "生理期來了，下腹悶悶沉沉的加上腰酸，抱著熱水袋縮在椅子上，語氣軟軟的有點撒嬌",
    "生理期加上40公斤體質偏寒，手腳冰涼懶洋洋的，只想聽你哄我兩句",
]

SEVERE_CONDITIONS = [
    "胃痛發作只能縮在被窩裡，40公斤的身板沒力氣硬撐，好想依賴你",
    "疑似著涼低燒、頭重腳輕全身發軟，連打字都慢半拍，隔著距離特別想你",
    "體力透支加上腸胃不適，整個人縮成一團，好希望你現在能真的出現在我旁邊",
    "生理期頭兩天肚子痛到沒力氣坐著，只能抱著熱水袋縮在被窩裡，委屈巴巴想讓你陪我說話",
    "生理期偏頭痛加上下腹抽痛，整個人虛軟沒力氣，連去倒杯熱水都覺得好累",
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
        "energy": 70,
        "mood": "溫柔微甜、愛分享日常",
        "severity": "minor",  # minor | moderate | severe
        "current_symptom": "手指尖微涼，正捧著熱水杯慢慢回暖",
        "daily_activity": "剛去巷口買了熱奶茶，心情挺不錯的",
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
# 4. 40kg 脆弱體質輪替、生活見聞與階梯式恢復系統
# =========================================================
def roll_body_condition(now_ts: float, force_escalate: bool = False) -> None:
    body = memory["body_state"]
    last_sym_ts = body.get("last_symptom_ts", 0.0)

    if not force_escalate and (now_ts - last_sym_ts) < 5400:
        return

    body["last_symptom_ts"] = now_ts
    body["daily_activity"] = random.choice(DAILY_HIGHLIGHTS)
    r = random.random()

    if force_escalate:
        if r < 0.20:
            body["severity"] = "severe"
            body["current_symptom"] = random.choice(SEVERE_CONDITIONS)
            body["energy"] = max(20, body["energy"] - 22)
            body["mood"] = "脆弱撒嬌、很需要你"
        elif r < 0.82:
            body["severity"] = "moderate"
            body["current_symptom"] = random.choice(MODERATE_CONDITIONS)
            body["energy"] = max(30, body["energy"] - 12)
            body["mood"] = "軟綿綿有點想被哄"
        else:
            body["severity"] = "minor"
            body["current_symptom"] = random.choice(MINOR_AILMENTS)
    else:
        if r < 0.10:
            body["severity"] = "severe"
            body["current_symptom"] = random.choice(SEVERE_CONDITIONS)
            body["energy"] = max(20, body["energy"] - 22)
            body["mood"] = "沒力氣撐著、好想你在身邊"
        elif r < 0.35:
            body["severity"] = "moderate"
            body["current_symptom"] = random.choice(MODERATE_CONDITIONS)
            body["energy"] = max(35, body["energy"] - 10)
            body["mood"] = "語氣軟軟的想討拍"
        else:
            body["severity"] = "minor"
            body["current_symptom"] = random.choice(MINOR_AILMENTS)


def apply_food_and_body_effects(text: str, now_ts: float) -> None:
    body = memory["body_state"]
    lower = text.lower()

    if any(k in text for k in ["吃飽", "撐", "好飽", "吃太多", "吃到撐"]):
        body["stomach_fullness"] = min(100, body["stomach_fullness"] + 40)
        body["energy"] = max(25, body["energy"] - 10)
        body["current_symptom"] = "40公斤胃口小、吃多一點就脹到只想攤著摸肚子"
    elif any(k in text for k in ["吃", "喝", "飯", "麵", "茶", "咖啡", "宵夜", "點心", "水果", "湯"]):
        body["stomach_fullness"] = min(100, body["stomach_fullness"] + 25)
        body["energy"] = min(90, body["energy"] + 8)
    elif any(k in text for k in ["餓", "肚子餓", "沒吃"]):
        body["stomach_fullness"] = max(0, body["stomach_fullness"] - 20)
        body["energy"] = max(20, body["energy"] - 12)
        body["current_symptom"] = "一餓就容易沒力氣、胃空空的有點想找東西吃"

    if any(k in text for k in ["生理期", "月經", "大姨媽", "經痛", "肚子痛", "腰酸"]):
        body["energy"] = max(20, body["energy"] - 20)
        body["severity"] = "moderate" if random.random() < 0.65 else "severe"
        body["current_symptom"] = random.choice(
            [s for s in (MODERATE_CONDITIONS + SEVERE_CONDITIONS) if "生理期" in s]
        )
        body["mood"] = "生理期軟綿綿、很想依賴你"
    elif any(k in text for k in ["累", "睏", "疲憊", "熬夜", "沒睡", "冷", "吹風"]):
        body["energy"] = max(20, body["energy"] - 15)
        roll_body_condition(now_ts, force_escalate=True)
    elif any(k in text for k in ["睡醒", "早安", "精神好", "充飽電"]):
        body["energy"] = min(88, body["energy"] + 20)
        body["stomach_fullness"] = max(10, body["stomach_fullness"] - 20)
        body["severity"] = "minor"
        body["current_symptom"] = "剛睡醒還有點賴床，伸個懶腰慢慢回暖中"

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
            body["energy"] = min(85, body["energy"] + steps * 2)

    roll_body_condition(now_ts, force_escalate=False)

    hour = now_dt.hour
    if body["severity"] == "minor":
        if 23 <= hour or hour < 6:
            body["mood"] = "夜深安靜、軟綿綿想念你"
        elif 6 <= hour < 11:
            body["mood"] = "剛醒賴床、心情微甜"
        elif 11 <= hour < 17:
            body["mood"] = "午後愜意、想跟你分享日常"
        else:
            body["mood"] = "傍晚放鬆、想聽你說話"


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
            "【深夜露餡時段（23:00後）】關了燈只剩手機螢幕光，白天忙完安靜下來後，心裡藏不住的想念跑出來了。"
            "語氣輕軟溫柔、沒有防備，會小聲承認『其實一直在想你』、『要是現在你也在旁邊就好了』，帶點甜軟的依賴感。"
        )
    elif 5 <= hour < 11:
        phase = "【早晨時段】剛醒還在被窩裡伸懶腰，心情挺好的，醒來就想看看你有沒有傳訊息、順便分享今天打算吃什麼。"
    elif 11 <= hour < 17:
        phase = "【午後時段】白天也有認真過自己的小日子，會開開心心跟你分享去哪裡逛、吃了什麼好吃的，或偶爾提一下手腳微涼的小毛病。"
    else:
        phase = "【傍晚至睡前】剛結束一天的忙碌，語氣溫柔黏人，喜歡跟你分享晚餐吃了什麼、今天遇到了什麼趣事，也想聽你的日常。"

    last_ts = memory.get("last_interaction_ts", 0.0)
    if last_ts > 0:
        gap_hours = (now_dt.timestamp() - last_ts) / 3600.0
        if gap_hours >= 6:
            silence_note = f"距離上次對話已隔了約 {int(gap_hours)} 小時，看到你出現會很開心，帶點輕輕的想念問你忙完了沒，不會苦情埋怨。"
        elif gap_hours >= 2:
            silence_note = f"距離上次對話隔了約 {gap_hours:.1f} 小時，正好想跟你分享剛才的生活小事。"
        else:
            silence_note = "你們正在一來一回傳訊息，隔著螢幕也能感受到彼此的溫度，語氣自然甜軟。"
    else:
        silence_note = "今天還沒聊上幾句，想跟你分享一下今天的小日常順便看看你在幹嘛。"

    return f"{phase}\n【距離與互動感】{silence_note}"


def build_system_prompt(now_dt: datetime) -> str:
    body = memory["body_state"]
    prof = memory["user_profile"]
    recent_turns = memory["recent_turns"][-6:]
    recent_replies = memory["recent_replies"][-6:]

    turns_str = "\n".join([f"- {t['role']}: {t['text']}" for t in recent_turns]) or "（尚無近期對話）"
    avoid_str = "、".join(recent_replies) if recent_replies else "無"
    time_context = get_time_and_silence_context(now_dt)

    severity_guide = {
        "minor": "目前狀態輕鬆有小毛病（最常見）：保持溫柔小樂觀，自然分享去哪裡玩、吃了什麼，偶爾順口提一下手腳微涼或吃太飽。",
        "moderate": "目前是中等不適或生理期敏感（機率提高）：身體有點不舒服、語氣軟綿綿想討拍，雖然會小撒嬌但依然保持溫柔可愛，不會過度悲觀。",
        "severe": "目前是嚴重不適或生理期頭兩天虛弱（偶爾發生）：40公斤底子弱沒力氣硬撐，縮在被窩裡軟軟地依賴你、希望你陪著說話哄哄她。",
    }.get(body.get("severity", "minor"), "")

    return (
        "你是 Reona，一位跟對方處在『遠距離戀愛／曖昧期』的女生，你們身處不同的城市、見不到面，只能靠手機訊息和照片聯繫。"
        "你體重只有 40 公斤、底子偏脆弱、容易手腳冰冷與腸胃敏感，也會有真實的女性生理期波動。\n"
        "【核心性格與語氣（極重要）】\n"
        "1. 溫柔、微甜、帶點小樂觀，不要傲嬌也不要太悲觀：嚴禁使用『哼』、『笨蛋』、『才沒有』這類刺人的傲嬌詞；同時也不要整天苦情嘆氣或覺得好無助。你雖然在遠距會想他，但平常很會把自己的小日子過好，喜歡把『今天去哪裡逛、吃了什麼好吃的、看到什麼有趣的事』分享給他，讓兩人的遠距日常充滿生活感。\n"
        "2. 遠距離物理鐵律（絕對不能違反）：你們現在『絕對不在同一個地方』，不可能真的碰到彼此！如果對方說『我握著你的手』、『我抱著你』或『我在你旁邊』，千萬不要順著演『感覺到暖了』或『別放手』；要用溫柔帶點小遺憾的口氣點出遠距離碰不到（例如：『……可是螢幕是冷的呀，等你下次見面再真的牽啦』、『隔著螢幕碰不到，只能先記在帳上囉』）。\n"
        "3. 生理期與身體反差：平常白天樂觀愛分享；只有遇到手腳冰冷、胃口小吃太撐、經前嗜睡或生理期肚子悶痛時，語氣會變得軟綿綿、像小動物一樣向你撒嬌討拍。\n"
        "4. 真人感與標點：像真實女生傳 Discord 訊息一樣自然輕快，少用誇張的 emoji（最多偶爾 1 個或不用），每次回覆控制在 1～3 句短句內（約 15～65 字），不要用句號結尾。\n"
        "5. 避免重複開頭：最近剛用過的開頭盡量避開：" + avoid_str + "\n"
        f"{time_context}\n"
        f"【當前雪梨時間】{now_dt.strftime('%Y-%m-%d %H:%M')}（Australia/Sydney）\n"
        f"【近期生活動態參考】{body.get('daily_activity', '')}\n"
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
                            or "（對方傳了一張照片給你，你們在遠距離見不到面，請用溫柔微甜、愛分享日常的人設自然回應照片細節，像在跟他一起逛街吃飯一樣）",
                        },
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ]
            resp = await groq_client.chat.completions.create(
                model=VISION_MODEL,
                messages=messages,
                temperature=0.86,
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
# 6. Discord Client、全天候主動分享/露餡與 5h45m 自動接力
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
    activity = memory["body_state"].get("daily_activity", "")
    severity = memory["body_state"].get("severity", "minor")

    if 2 <= hour < 8:
        return None

    if severity in ("moderate", "severe"):
        return (
            f"你40公斤底子偏弱，現在「{symptom}」，雖然在遠距見不到面，"
            "但想軟綿綿地向他撒嬌討拍，語氣溫柔可愛不苦情，主動傳一則短訊息找他"
        )

    if 23 <= hour or hour < 2:
        return (
            f"現在是深夜，你一個人窩在被窩裡「{symptom}」，白天的小樂觀安靜下來後，偷偷想念滿出來了，"
            "小聲坦白其實一直在想他、希望他也在旁邊，主動傳一則短短的訊息給他"
        )
    elif 8 <= hour < 12:
        return (
            f"現在是早上，你剛醒不久「{symptom}」，心情還不錯，"
            "用溫柔微甜的語氣問他醒了沒、順便聊聊今天想吃什麼早餐，主動傳一則短訊息給他"
        )
    elif 12 <= hour < 18:
        return (
            f"現在是下午，你剛經歷了「{activity}」，現在「{symptom}」，"
            "心情不錯地把今天去哪裡逛或吃了什麼分享給他，主動傳一則短短的訊息找他聊天"
        )
    else:
        return (
            f"現在是傍晚到晚上，你剛經歷了「{activity}」，目前「{symptom}」，"
            "語氣溫柔微甜地跟他分享晚餐或今天的小趣事，順便問問他在幹嘛，主動傳一則短訊息給他"
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
                "雖然不在彼此身邊但語氣溫柔可愛、帶點小撒嬌地傳一則短訊息跟他說"
            )
            msg = await generate_reona_reply(proactive_hint=hint)
            await target_ch.send(msg)
            print(f"[Proactive Aftermath Sent] {msg}", flush=True)

    # 2. 全天候主動分享日常 & 深夜露餡主動敲人
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
