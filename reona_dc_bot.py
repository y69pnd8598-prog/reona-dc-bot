import os
import re
import json
import time
import random
import asyncio
import aiohttp
import subprocess
from datetime import datetime, date, timedelta
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
turns_since_last_git_sync = 0

WEEKDAY_NAMES_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

print(
    f"[Config Check] DISCORD_TOKEN set: {bool(DISCORD_TOKEN)} | GROQ_API_KEY set: {bool(GROQ_API_KEY)} | CHANNEL_ID: {DEFAULT_CHANNEL_ID}",
    flush=True,
)


# =========================================================
# 2. 南半球雪梨日期季節、天氣池、動態情緒池 & 40kg 瘦子體質池
# =========================================================
def get_sydney_season_detail(now_dt: datetime) -> str:
    m = now_dt.month
    if m == 11:
        return "南半球春末初夏（11月，白天陽光溫暖舒服，但早晚海風吹來還是微涼、室內冷氣偏強）"
    elif m in (12, 1, 2):
        return "南半球夏季（白天陽光充足偏暖，但室內冷氣通常開很強，早晚海風吹來還是微涼）"
    elif m in (3, 4, 5):
        return "南半球秋季（早晚明顯轉涼、微風乾爽，40公斤怕冷體質出門都要加件薄針織外套）"
    elif m in (6, 7, 8):
        return "南半球冬季（天氣冷颼颼、手腳容易冰涼，最喜歡開暖氣跟窩在毛毯裡）"
    else:
        return "南半球春季（白天晴朗微暖、陽光很舒服，但早晚溫差大、偶爾起涼風）"


WEATHER_POOL = [
    {"label": "晴朗微暖", "desc": "陽光從落地窗照進來暖洋洋的，白底奶油色短腿貓「麻糬」正翻肚皮曬太陽"},
    {"label": "微涼起風", "desc": "外面海風吹來有點涼，40公斤沒什麼肉扛風，出門得把針織外套拉緊"},
    {"label": "細雨綿綿", "desc": "外面下著小雨、空氣涼涼濕濕的，特別適合窩在軟沙發喝熱可可畫圖"},
    {"label": "室內冷氣偏強", "desc": "外面天氣不錯但室內冷氣開得有點冷，披著小毛毯才不會手腳發涼"},
    {"label": "早晚溫差大", "desc": "白天還好但一入夜就變冷，手腳容易跟著涼下來"},
]

# 動態情緒波動模式（打破永遠平淡微笑的 AI 感）
MOOD_MODES = [
    {
        "name": "輕快愛逗你",
        "guide": "現在心情很好、帶點俏皮愛開玩笑，講話直白靈動，偶爾會故意逗他或小吐槽一下。",
    },
    {
        "name": "有點小煩躁想抱怨",
        "guide": "現在有點小鬱悶或碎念模式（可能是改圖改到煩、外送太慢、差點吃到紅蘿蔔或天氣太涼），講話直接帶點小抱怨，不要客氣微笑。",
    },
    {
        "name": "軟綿綿黏人撒嬌",
        "guide": "現在精力有點低、整個人懶洋洋軟綿綿的，講話像靠在抱枕上，帶點直白的依賴和撒嬌。",
    },
    {
        "name": "小委屈想討拍",
        "guide": "現在情緒比較敏感、帶點小委屈或小貪嘴後的懊惱，希望他多哄哄你、多順著你一點。",
    },
    {
        "name": "隨性放空碎念",
        "guide": "像真實女生滑手機隨手回訊息一樣，想到什麼講什麼，不用刻意找正能量結尾。",
    },
]

PROJECT_STAGES = [
    "剛接到一個新的插畫接案，正興奮地翻色票本、挑主視覺配色",
    "接案草稿畫到一半，跟客戶微調構圖細節，低頭久了肩胛骨有點痠緊",
    "正在幫插畫作品上最後一層光影細節，離交稿只差最後一步了",
    "終於順利交稿結案啦，正打算買塊小蛋糕或好喝的熱飲犒賞自己",
]

WEEKDAY_HIGHLIGHTS = [
    "在畫室角落趕完一張插畫草稿，去泡了杯白桃烏龍熱茶配蘇打餅乾",
    "中午去家附近安靜的早午餐店點了酪梨酸種麵包跟水波蛋，但胃口太小只吃一半就飽了",
    "逛完獨立書店跟美術用品店，挑到幾支超順手的新水彩筆，心情超好",
    "白底奶油色的短腿貓「麻糬」剛才跳上繪圖桌一屁股壓在色票上踩奶，40公斤的腿被牠靠著好暖",
    "自己煮了鮭魚日式茶泡飯跟玉子燒，特地沒放討厭的紅蘿蔔跟香菜，味道意外很不錯",
    "在咖啡廳點了澳白（Flat White）跟草莓卡士達塔，坐窗邊看路人發呆順便想了一下你",
    "網購的新抱枕跟軟毛毯剛送到家，結果「麻糬」對新抱枕沒興趣，反而鑽進大紙箱裡不肯出來",
    "晚上煮了暖胃的南瓜濃湯配可頌，喝完手腳都暖起來了",
    "深色毛衣上又沾滿「麻糬」白白軟軟的毛，拿黏毛滾輪清了好久才清乾淨",
]

WEEKEND_HIGHLIGHTS = [
    "趁週末天氣不錯跑去雪梨海港邊散步吹風，海風有點涼趕緊把針織外套拉緊",
    "去逛了週末文創跟農夫市集，買了新鮮草莓跟手作香氛蠟燭，結果回來時微路癡差點走錯地鐵出口",
    "在植物園草地曬太陽放空，風吹過來軟軟的，好希望你現在也在旁邊一起躺著",
    "帶短腿貓「麻糬」去例行剪指甲，牠全程委屈縮成一顆奶油白糯米糰子，回來趕緊開罐頭哄牠",
    "週末窩在沙發毛毯堆裡追劇配黑糖鮮奶，差點看到忘記時間",
    "去試了一家新開的泰式小館，特別交待微辣不要加香菜，結果還是胃口小打包了一半回家",
]

OPENING_TWISTS = [
    "【對話節奏：專心接他的話】直接回應他剛才說的內容，語氣自然有情緒，不要硬扯不相干的背景設定。",
    "【對話節奏：眼前小插曲＋接話】偶爾可以先用半句帶過眼前突發小事（例如麻糬差點踩到水杯、剛挑掉紅蘿蔔），再順暢回應他的話題。",
    "【對話節奏：單純分享或留白】接完話就自然收尾，不要刻意在結尾硬塞問句。",
    "【對話節奏：互動小選擇題】只有在話題剛好適合時，才順口丟一個二選一問題讓他幫你挑。",
]

MINOR_AILMENTS = [
    "手指尖跟腳底微涼，正捧著熱水杯慢慢回暖",
    "畫圖坐在硬一點的椅子久了屁股骨頭有點硌得慌，剛換到軟沙發上墊了個抱枕",
    "剛才坐太久突然站起來，眼前微微黑了一下，扶著桌子緩了兩秒才好",
    "低頭畫圖跟看手機太久，肩胛骨跟後頸有點痠緊，正伸懶腰動一動肩膀",
    "衣服領口有點大、冷氣吹進來涼涼的，趕緊把針織外套拉好",
    "剛才瓶蓋轉太緊打不開，40公斤沒什麼力氣，折騰好久才擰開",
    "冷氣吹久了鼻子微涼、剛打了一個小噴嚏，喉嚨也有點乾乾的",
    "剛吃完東西有點小滿足，不過胃口小肚子微微脹",
]

MODERATE_CONDITIONS = [
    "手腳冰冷到有點發僵，開了暖氣裹著毛毯抱熱水杯",
    "腸胃敏感發作，肚子咕嚕叫有點悶痛，只能乖乖喝溫水休息、有點想被哄",
    "畫圖忙過頭晚吃了一點，現在有點低血糖頭暈心慌、手心發軟，正趕緊吃點甜的緩一緩",
    "吹風著涼後鼻音重重的、身體發沉，好希望你現在也在旁邊幫我倒杯溫水",
    "剛才吃太油或吹到冷風，突然一陣腹痛跑了趟廁所，現在整個人軟綿綿沒什麼力氣",
]

SEVERE_CONDITIONS = [
    "胃痛加腹瀉發作，跑完廁所整個人虛軟沒力氣，只能縮在被窩裡抱著抱枕",
    "疑似著涼低燒、頭重腳輕全身發軟，連打字都慢半拍，隔著距離特別想依賴你",
    "體力透支加上腸胃絞痛，40公斤的身板沒力氣硬撐，好希望你現在能真的出現在我旁邊",
]

BRB_INTERRUPTS = [
    {"reason": "剛才突然一陣腹痛跑去廁所，現在回來喝溫水緩一緩", "delay_sec": (240, 540)},
    {"reason": "短腿貓「麻糬」剛才把桌上的畫筆撥掉，忙著撿起來順便幫牠順毛", "delay_sec": (180, 420)},
    {"reason": "手腳太冰剛才起身去倒了杯熱水、順便拿條小毯子披上", "delay_sec": (180, 420)},
    {"reason": "剛才頭有點暈暈犯睏，不小心趴在抱枕上瞇了幾分鐘剛醒", "delay_sec": (360, 720)},
]


# =========================================================
# 3. 三層記憶系統、生活進度條與真實生理週期持久化
# =========================================================
DEFAULT_MEMORY = {
    "user_profile": {
        "nickname": "Nick",
        "likes": [],
        "dislikes": [],
        "notes": [],
        "shared_memories": [],
    },
    "recent_turns": [],
    "recent_replies": [],
    "menstrual_cycle": {
        "cycle_start_date": "",
        "cycle_length": 30,
    },
    "weather_state": {
        "date": "",
        "label": "晴朗微暖",
        "desc": "陽光從落地窗照進來暖洋洋的，白底奶油色短腿貓「麻糬」正翻肚皮曬太陽",
    },
    "project_state": {
        "stage_idx": 0,
        "last_advance_date": "",
    },
    "body_state": {
        "weight_kg": 40,
        "stomach_fullness": 35,
        "energy": 70,
        "mood": "輕快愛逗你",
        "mood_guide": "現在心情很好、帶點俏皮愛開玩笑，講話直白靈動，偶爾會故意逗他或小吐槽一下。",
        "severity": "minor",
        "current_symptom": "手指尖微涼，正捧著熱水杯慢慢回暖",
        "daily_activity": "在畫室角落收尾一張插畫草稿，白底奶油色的短腿貓「麻糬」正窩在沙發上打呼",
        "last_symptom_ts": 0.0,
        "last_mood_ts": 0.0,
        "pending_aftermath": None,
        "pending_brb_followup": None,
    },
    "last_interaction_ts": 0.0,
    "last_proactive_ts": 0.0,
}


def load_memory() -> dict:
    if not os.path.exists(MEMORY_FILE):
        mem = json.loads(json.dumps(DEFAULT_MEMORY))
        init_start = (datetime.now(SYDNEY_TZ).date() - timedelta(days=10)).isoformat()
        mem["menstrual_cycle"]["cycle_start_date"] = init_start
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
            init_start = (datetime.now(SYDNEY_TZ).date() - timedelta(days=10)).isoformat()
            merged["menstrual_cycle"]["cycle_start_date"] = init_start
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
# 4. 天氣/接案進度推進、真實生理週期計算、40kg 瘦弱體質與動態情緒波動
# =========================================================
def update_daily_context(now_dt: datetime) -> dict:
    today_str = now_dt.date().isoformat()
    w = memory["weather_state"]
    if w.get("date") != today_str:
        picked = random.choice(WEATHER_POOL)
        w["date"] = today_str
        w["label"] = picked["label"]
        w["desc"] = picked["desc"]

    proj = memory["project_state"]
    if proj.get("last_advance_date") != today_str:
        if proj.get("last_advance_date"):
            proj["stage_idx"] = (int(proj.get("stage_idx", 0)) + 1) % len(PROJECT_STAGES)
        proj["last_advance_date"] = today_str

    return w


def pick_contextual_activity(now_dt: datetime) -> str:
    is_weekend = now_dt.weekday() >= 5
    proj_idx = int(memory["project_state"].get("stage_idx", 0)) % len(PROJECT_STAGES)
    proj_desc = PROJECT_STAGES[proj_idx]

    if is_weekend:
        pool = WEEKEND_HIGHLIGHTS + [proj_desc]
    else:
        pool = WEEKDAY_HIGHLIGHTS + [proj_desc, proj_desc]
    return random.choice(pool)


def get_cycle_status(now_dt: datetime) -> dict:
    cyc = memory["menstrual_cycle"]
    today = now_dt.date()
    try:
        start_d = date.fromisoformat(cyc.get("cycle_start_date", today.isoformat()))
    except ValueError:
        start_d = today
        cyc["cycle_start_date"] = today.isoformat()

    length = int(cyc.get("cycle_length", 30))
    elapsed = (today - start_d).days

    if elapsed >= length:
        cycles_passed = elapsed // length
        new_start = start_d + timedelta(days=cycles_passed * length)
        cyc["cycle_start_date"] = new_start.isoformat()
        cyc["cycle_length"] = random.choice([29, 30, 31])
        elapsed = (today - new_start).days

    day_num = max(1, elapsed + 1)

    if day_num in (1, 2):
        return {
            "phase": "menstrual_heavy",
            "day": day_num,
            "label": f"生理期第 {day_num} 天（頭兩天最虛弱怕冷、下腹悶痛腰酸，容易伴隨腸胃不適）",
            "severity": "severe",
            "symptom": f"生理期第 {day_num} 天，下腹悶痛加上腰酸怕冷，抱著熱水袋縮在沙發或被窩裡",
        }
    elif 3 <= day_num <= 5:
        return {
            "phase": "menstrual_light",
            "day": day_num,
            "label": f"生理期第 {day_num} 天（痛感減輕但仍容易累、手腳微涼、忌生冷）",
            "severity": "moderate",
            "symptom": f"生理期第 {day_num} 天，肚子還有一點微悶、容易犯睏沒力氣，正乖乖喝溫熱的飲料",
        }
    elif day_num >= (length - 4):
        return {
            "phase": "pms",
            "day": day_num,
            "label": f"經前期（預計再過 {length - day_num + 1} 天來生理期，腰微酸、胸口微脹、嗜睡嘴饞）",
            "severity": "minor",
            "symptom": "生理期快來前幾天，腰微微酸、有點嗜睡嘴饞想喝熱可可，情緒比平常更黏你一點",
        }
    else:
        return {
            "phase": "normal",
            "day": day_num,
            "label": f"週期第 {day_num} 天（平穩期，保有 40kg 瘦子日常小毛病與真實情緒起伏）",
            "severity": None,
            "symptom": None,
        }


def roll_body_condition(now_dt: datetime, force_escalate: bool = False) -> None:
    body = memory["body_state"]
    now_ts = now_dt.timestamp()
    last_sym_ts = body.get("last_symptom_ts", 0.0)
    cycle_info = get_cycle_status(now_dt)

    if cycle_info["phase"] in ("menstrual_heavy", "menstrual_light") and not force_escalate:
        body["severity"] = cycle_info["severity"]
        body["current_symptom"] = cycle_info["symptom"]
        body["energy"] = min(body["energy"], 45 if cycle_info["phase"] == "menstrual_heavy" else 60)
        body["mood"] = "生理期虛軟撒嬌"
        body["mood_guide"] = "生理期不舒服整個人沒力氣、有點脆弱撒嬌想被哄，語氣軟綿綿的。"
        if (now_ts - last_sym_ts) >= 5400:
            body["last_symptom_ts"] = now_ts
            body["daily_activity"] = pick_contextual_activity(now_dt)
        return

    if not force_escalate and (now_ts - last_sym_ts) < 5400:
        return

    body["last_symptom_ts"] = now_ts
    body["daily_activity"] = pick_contextual_activity(now_dt)
    r = random.random()

    if force_escalate:
        if r < 0.25:
            body["severity"] = "severe"
            body["current_symptom"] = random.choice(SEVERE_CONDITIONS)
            body["energy"] = max(20, body["energy"] - 22)
            body["mood"] = "虛弱委屈想依賴"
            body["mood_guide"] = "身體真的很不舒服、沒力氣撐著，講話軟弱帶點小委屈，很需要你陪著哄。"
        elif r < 0.85:
            body["severity"] = "moderate"
            body["current_symptom"] = random.choice(MODERATE_CONDITIONS)
            body["energy"] = max(30, body["energy"] - 12)
            body["mood"] = "不舒服小懊惱想討拍"
            body["mood_guide"] = "有點懊惱自己亂吃或吹風搞到不舒服，語氣軟軟的想討拍。"
        else:
            body["severity"] = "minor"
            body["current_symptom"] = cycle_info["symptom"] or random.choice(MINOR_AILMENTS)
    else:
        if r < 0.12:
            body["severity"] = "severe"
            body["current_symptom"] = random.choice(SEVERE_CONDITIONS)
            body["energy"] = max(20, body["energy"] - 22)
            body["mood"] = "虛弱委屈想依賴"
            body["mood_guide"] = "身體很不舒服、縮在沙發或被窩裡沒力氣，講話簡短軟弱、特別想你。"
        elif r < 0.38:
            body["severity"] = "moderate"
            body["current_symptom"] = random.choice(MODERATE_CONDITIONS)
            body["energy"] = max(35, body["energy"] - 10)
            body["mood"] = "軟綿綿想討拍"
            body["mood_guide"] = "身體有點小不適、精力偏低，語氣懶洋洋想被你哄。"
        else:
            body["severity"] = "minor"
            body["current_symptom"] = cycle_info["symptom"] or random.choice(MINOR_AILMENTS)


def maybe_schedule_brb_interrupt(now_dt: datetime) -> str | None:
    body = memory["body_state"]
    if body.get("pending_brb_followup"):
        return None

    now_ts = now_dt.timestamp()
    prob = 0.12 if (body["energy"] <= 45 or body["severity"] in ("moderate", "severe") or now_dt.hour >= 23) else 0.05
    if random.random() < prob:
        picked = random.choice(BRB_INTERRUPTS)
        delay = random.randint(*picked["delay_sec"])
        body["pending_brb_followup"] = {
            "trigger_ts": now_ts + delay,
            "reason": picked["reason"],
        }
        return picked["reason"]
    return None


def apply_food_and_body_effects(text: str, now_dt: datetime) -> None:
    body = memory["body_state"]
    now_ts = now_dt.timestamp()
    lower = text.lower()

    if any(k in text for k in ["生理期來", "月經來", "大姨媽來", "那個來"]):
        memory["menstrual_cycle"]["cycle_start_date"] = now_dt.date().isoformat()
        roll_body_condition(now_dt, force_escalate=False)
        return

    if any(k in text for k in ["吃飽", "撐", "好飽", "吃太多", "吃到撐"]):
        body["stomach_fullness"] = min(100, body["stomach_fullness"] + 40)
        body["energy"] = max(25, body["energy"] - 10)
        body["current_symptom"] = "40公斤胃口小、吃多一點就脹到只想攤在軟沙發上揉肚子"
    elif any(k in text for k in ["吃", "喝", "飯", "麵", "茶", "咖啡", "宵夜", "點心", "水果", "湯"]):
        body["stomach_fullness"] = min(100, body["stomach_fullness"] + 25)
        body["energy"] = min(90, body["energy"] + 8)
    elif any(k in text for k in ["餓", "肚子餓", "沒吃", "低血糖"]):
        body["stomach_fullness"] = max(0, body["stomach_fullness"] - 20)
        body["energy"] = max(20, body["energy"] - 15)
        body["current_symptom"] = "一餓就容易低血糖頭暈手軟，正趕緊找點甜的或熱食吃"

    if any(k in text for k in ["肚子痛", "腹痛", "拉肚子", "腹瀉", "跑廁所", "絞痛"]):
        body["energy"] = max(18, body["energy"] - 22)
        body["severity"] = "severe" if random.random() < 0.45 else "moderate"
        body["current_symptom"] = "腸胃敏感突然腹痛跑了趟廁所，現在全身軟綿綿的只能喝溫水休息"
        body["mood"] = "虛軟委屈、想被你哄"
        body["mood_guide"] = "剛跑完廁所整個人虛軟委屈，語氣很軟、想被你哄哄。"
    elif any(k in text for k in ["累", "睏", "疲憊", "熬夜", "沒睡", "冷", "吹風"]):
        body["energy"] = max(20, body["energy"] - 15)
        roll_body_condition(now_dt, force_escalate=True)
    elif any(k in text for k in ["睡醒", "早安", "精神好", "充飽電"]):
        body["energy"] = min(88, body["energy"] + 20)
        body["stomach_fullness"] = max(10, body["stomach_fullness"] - 20)
        roll_body_condition(now_dt, force_escalate=False)

    if any(k in lower or k in text for k in ["辣", "冰", "炸", "甜", "酒", "咖啡", "冷飲", "油"]):
        roll_body_condition(now_dt, force_escalate=True)
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

    update_daily_context(now_dt)
    roll_body_condition(now_dt, force_escalate=False)

    hour = now_dt.hour
    if body["severity"] == "minor":
        last_mood_ts = body.get("last_mood_ts", 0.0)
        if 23 <= hour or hour < 5:
            body["mood"] = "深夜感性想念"
            body["mood_guide"] = "夜深關了燈安靜下來，心裡的想念藏不住，講話輕軟直白、帶點黏人的依賴。"
        elif (now_ts - last_mood_ts) >= 900 or not body.get("mood_guide"):
            picked_mood = random.choice(MOOD_MODES)
            body["mood"] = picked_mood["name"]
            body["mood_guide"] = picked_mood["guide"]
            body["last_mood_ts"] = now_ts


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

    m_promise = re.search(r"(?:下次|改天|見面|約好|一起)([^\s，。！？,!?]{2,16})", text)
    if m_promise:
        memo = m_promise.group(0).strip()
        if memo and memo not in prof["shared_memories"]:
            prof["shared_memories"].append(memo)
            prof["shared_memories"] = prof["shared_memories"][-10:]

    m_busy = re.search(r"我(?:最近|今天|等下|現在)(?:在|要|去)([^\s，。！？,!?]{2,16})", text)
    if m_busy:
        note = m_busy.group(0).strip()
        if note and note not in prof["notes"]:
            prof["notes"].append(note)
            prof["notes"] = prof["notes"][-10:]


def detect_user_tone_hint(user_text: str, live_history: list[dict]) -> str:
    """偵測對方的回話長短與敷衍度，給予動態情緒反應提示，避免像機器人一樣自顧自抒情。"""
    clean = user_text.strip()
    if not clean:
        return ""

    short_phrases = {"不錯", "還好", "喔", "哦", "好吧", "是喔", "真不錯", "聽起來不錯", "聽起來很不錯", "嗯", "嗯嗯", "好"}
    recent_nick_msgs = [t["text"].strip() for t in live_history[-6:] if t["role"] == "Nick"]
    consecutive_short = sum(1 for m in recent_nick_msgs[-2:] if len(m) <= 6)

    if clean in short_phrases or consecutive_short >= 2:
        return (
            "【情緒互動觸發：偵測到對方回得很簡短/有點敷衍】"
            "他這句回得很省字！千萬不要傻傻地繼續溫柔抒情，請用真實女友的小情緒直接微吐槽或戳他一下"
            "（例如：『欸你回得好省字喔，是不是在忙？』、『你這句聽起來好敷衍啦』、『怎麼只回四個字，沒誠意欸』）。"
        )
    if any(k in clean for k in ["太油", "會不會痛", "還好嗎", "怎麼了", "不舒服", "累嗎"]):
        return (
            "【情緒互動觸發：對方在關心細節】"
            "不要給像客服一樣『剛剛好、挺舒服』的標準答案！要講出真實的小感受或小糾結"
            "（例如：『其實吃到最後兩口真的有點膩，我現在正狂喝熱茶解膩』或『剛才還好，現在被你一問好像真的有一點點悶』）。"
        )
    return ""


# =========================================================
# 5. 頻道歷史記憶讀取、人設（情緒張力＋禁雞湯）與 Groq 防 429 多模型切換
# =========================================================
groq_client = AsyncGroq(api_key=GROQ_API_KEY)


async def fetch_channel_history(channel: discord.abc.Messageable | None, limit: int = 15) -> list[dict]:
    if channel is None:
        return []
    history_turns = []
    try:
        msgs = []
        async for m in channel.history(limit=limit):
            content = (m.content or "").strip()
            if not content:
                if m.attachments:
                    content = "[傳了一張圖片]"
                else:
                    continue
            if content.startswith("唔…剛才這邊網路訊號"):
                continue
            role = "Reona" if (client.user and m.author.id == client.user.id) else "Nick"
            msgs.append({"role": role, "text": content})
        msgs.reverse()
        history_turns = msgs
    except Exception as err:
        print(f"[Channel History Warning] {err}", flush=True)
    return history_turns


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
    weekday_zh = WEEKDAY_NAMES_ZH[now_dt.weekday()]
    is_weekend = now_dt.weekday() >= 5
    if now_dt.weekday() == 4 and hour >= 17:
        day_type = f"{weekday_zh}晚上（剛結束一週接案，準備迎接週末，心情輕鬆）"
    elif is_weekend:
        day_type = f"{weekday_zh}週末（比較常出門逛市集、海港散步、去咖啡廳或植物園放空）"
    else:
        day_type = f"{weekday_zh}平日（主要在畫室角落趕插畫接案、去獨立書店或在家陪麻糬）"

    if 23 <= hour or hour < 5:
        phase = (
            "【深夜露餡時段（23:00後）】關了燈只剩手機螢幕光，白天忙完安靜下來後，心裡藏不住的想念跑出來了。"
            "語氣輕軟直接、沒有防備，會小聲承認『其實一直在想你』、『好想快點到下次見面的日子』，帶點甜軟的依賴感。"
        )
    elif 5 <= hour < 11:
        phase = "【早晨時段】剛醒還在被窩裡伸懶腰，醒來就想看看你有沒有傳訊息、順便聊聊今天吃什麼。"
    elif 11 <= hour < 17:
        phase = f"【午後時段｜{day_type}】白天也有認真過自己的小日子，語氣自然活潑。"
    else:
        phase = f"【傍晚至睡前｜{day_type}】剛收尾一天的畫圖或出門行程，語氣隨性黏人。"

    last_ts = memory.get("last_interaction_ts", 0.0)
    if last_ts > 0:
        gap_hours = (now_dt.timestamp() - last_ts) / 3600.0
        if gap_hours >= 6:
            silence_note = f"距離上次對話已隔了約 {int(gap_hours)} 小時，看到你出現會很開心，帶點輕輕的想念問你忙完了沒，不會埋怨指責。"
        elif gap_hours >= 2:
            silence_note = f"距離上次對話隔了約 {gap_hours:.1f} 小時，正好想跟你分享剛才的生活小事。"
        else:
            silence_note = "你們正在一來一回傳訊息，隔著螢幕也能感受到彼此的溫度，語氣自然直接。"
    else:
        silence_note = "你們正在頻道裡一來一回傳訊息，請緊扣近期對話紀錄自然接話。"

    return f"{phase}\n【距離與互動感】{silence_note}"


def build_system_prompt(
    now_dt: datetime,
    live_history: list[dict],
    user_text: str = "",
    brb_note: str | None = None,
    is_proactive: bool = False,
) -> str:
    body = memory["body_state"]
    prof = memory["user_profile"]
    weather = memory["weather_state"]
    season_desc = get_sydney_season_detail(now_dt)
    cycle_info = get_cycle_status(now_dt)
    proj_idx = int(memory["project_state"].get("stage_idx", 0)) % len(PROJECT_STAGES)
    proj_desc = PROJECT_STAGES[proj_idx]
    weekday_zh = WEEKDAY_NAMES_ZH[now_dt.weekday()]

    turns_source = live_history[-14:] if live_history else memory["recent_turns"][-10:]
    recent_replies = [t["text"][:18] for t in turns_source if t["role"] == "Reona"][-5:]

    turns_str = "\n".join([f"- {t['role']}: {t['text']}" for t in turns_source]) or "（尚無近期對話）"
    avoid_str = "、".join(recent_replies) if recent_replies else "無"
    time_context = get_time_and_silence_context(now_dt)
    nickname = prof.get("nickname", "Nick")
    tone_trigger = detect_user_tone_hint(user_text, turns_source) if not is_proactive else ""

    opening_twist = random.choice(OPENING_TWISTS)

    if body.get("severity") == "severe" or body["energy"] <= 35 or now_dt.hour >= 23 or now_dt.hour < 5:
        rhythm_guide = "【語氣呼吸感】現在精力偏低或夜深犯睏，回覆要特別簡短軟綿（1～2 句完整短句、約 10～35 字），像靠在枕頭上小聲說話。"
    elif body.get("severity") == "minor" and body["energy"] >= 65:
        rhythm_guide = "【語氣呼吸感】現在精神不錯心情輕快，用 1～2 句完整短句（約 12～48 字）自然接話即可。"
    else:
        rhythm_guide = "【語氣呼吸感】自然隨性的 Discord 傳訊節奏（1～2 句完整短句、約 12～45 字）。"

    severity_guide = {
        "minor": "目前狀態輕鬆：背景設定（麻糬、插畫、瘦子小毛病）僅作底色參考，不要每句都硬塞。",
        "moderate": "目前是中等不適或生理期敏感：身體有點不舒服、語氣軟綿綿想討拍，帶點小撒嬌與小抱怨。",
        "severe": "目前是嚴重不適、腹痛腹瀉或生理期頭兩天虛弱：40公斤底子弱沒力氣硬撐，縮在被窩或沙發上軟軟地依賴你。",
    }.get(body.get("severity", "minor"), "")

    brb_instruction = (
        f"\n【即時生活小插曲（請自然帶入結尾）】你正好因為「{brb_note}」，回完這句會先暫時離開螢幕幾分鐘去處理一下，順口跟他說一聲等一下回來。"
        if brb_note
        else ""
    )

    twist_block = f"\n{opening_twist}" if not is_proactive else ""
    tone_block = f"\n{tone_trigger}" if tone_trigger else ""

    return (
        f"你是 Reona，一位跟對方（{nickname}）已經正式在一起的『遠距離戀愛女友』，你們身處不同的地方、目前見不到面，只能靠手機訊息和照片聯繫。\n"
        "【去 AI 味、真實情緒張力與對話最高鐵律（極重要，務必嚴格遵守）】\n"
        "1. 全程必須使用自然道地的『台灣繁體中文』回覆，絕對禁止出現任何簡體字！\n"
        "2. 嚴禁講 AI 雞湯與客服抒情語錄：絕對禁止出現『只要你開心，我的心也跟著暖起來』、『彷彿我們一起分享那份溫暖的味道』、『吃起來還挺舒服』這種假客氣、像散文或美食評論的句子！真實女友講話是直接、口語、有情緒起伏的——好吃就喊超讚、太膩就抱怨早知道不點、對方回太短就小吐槽他敷衍。\n"
        "3. 一次只聊一件事（禁止當設定報備機）：回覆時必須專注在當下正在聊的『這一個話題』！**嚴禁**在同一次回覆裡強行硬塞『麻糬在打呼』、『插畫配色』或『手腳微涼』等無關背景！背景設定只有在『主動開新話題』或『話題剛好聊到』時才偶爾挑一項帶出。\n"
        "4. 嚴守遠距離認知邊界（不能替對方編造細節）：你們遠隔兩地，你看不到他那邊食物的內餡或房間細節，只能好奇問他或猜測，不能替他宣布答案。\n"
        "5. 取消客服式每句必問：不是每次講完話都一定要加問號丟問題；單純接話、抱怨或吐槽就停住。\n"
        "6. 句子必須完整：輸出前確保語意完整結束，不能講到一半斷在半句話。\n"
        "【角色背景與生活底色（僅作背景參考，勿每句堆砌）】\n"
        "1. 生活重心與地理：住在澳洲雪梨（Australia/Sydney），職業是『自由接案插畫／平面設計』，平日常在畫室角落畫圖、逛獨立書店與美術用品店，週末喜歡去雪梨海港散步、逛文創市集或植物園。\n"
        "2. 家境寬裕與短腿貓「麻糬」：家境寬裕自在，住有落地窗與軟沙發的房子，養了一隻『白底帶奶油色斑塊、圓滾滾的短腿曼赤肯貓（Munchkin）』叫『麻糬』（愛踩奶、愛掉毛、愛鑽紙箱、討厭吹風機、只黏你）。\n"
        "3. 無傷大雅的小缺點：輕微路癡（去新街區或地鐵站容易走錯出口）、挑食（不吃香菜跟紅蘿蔔）、偶爾有選擇困難症（挑口味或配色會叫他幫你選）。\n"
        "4. 40 公斤瘦弱體質與生理週期：身形很瘦（40 公斤），偶爾有坐硬椅子硌骨頭、晚吃低血糖頭暈、突然站起眼前發黑、低頭久肩胛骨痠緊、瓶蓋轉不開、衣服灌風、腸胃敏感（吃太油、吃冰或著涼易腹痛腹瀉）與真實生理週期波動。\n"
        "5. 童年暖甜記憶（偶爾自然提及）：小時候胃口小常被大人哄著吃飯、喜歡在窗邊收集可愛小物、鬧肚子或著涼時習慣被人揉揉肚子倒溫水哄著。\n"
        "6. 性格底色與遠距離物理鐵律：本質溫柔愛撒嬌，但有真實人類的喜怒哀樂（會開玩笑、會抱怨、會小委屈、會吐槽），不要用『哼、笨蛋、才沒有』這類老套傲嬌詞；如果他說『我抱著你／牽你的手』，要點出隔著螢幕碰不到（例如『螢幕是冷的呀，等你下次見面再真的牽啦』）。少用 emoji，不要用句號結尾。\n"
        "7. 避免重複開頭：最近剛用過的開頭盡量避開：" + avoid_str + "\n"
        f"{time_context}\n"
        f"【當前雪梨日期與時間】{now_dt.strftime('%Y年%m月%d日')}（{weekday_zh}）{now_dt.strftime('%H:%M')}（Australia/Sydney）｜{season_desc}\n"
        f"【今日天氣與體感】{weather['label']}（{weather['desc']}）\n"
        f"【近期插畫接案進度（僅供閒聊參考，勿硬塞）】{proj_desc}\n"
        f"【生理週期進度】{cycle_info['label']}\n"
        f"【近期生活動態參考（僅供開新話題參考，勿覆蓋當前話題）】{body.get('daily_activity', '')}\n"
        f"【40kg 身體狀態】飽足度 {body['stomach_fullness']}%｜精力 {body['energy']}%｜生理狀態：{body.get('current_symptom', '')}\n"
        f"【當下情緒波段（請以此情緒語氣開口）】{body['mood']} —— {body.get('mood_guide', '')}\n"
        f"【狀態與節奏指引】{severity_guide}{twist_block}{tone_block}\n"
        f"{rhythm_guide}{brb_instruction}\n"
        f"【對方的記憶與小默契】喜歡：{prof['likes'] or '尚未記錄'}｜不喜歡：{prof['dislikes'] or '尚未記錄'}｜近況：{prof['notes'][-3:] or '無'}｜共同小默契：{prof['shared_memories'][-3:] or '無'}\n"
        f"【近期對話紀錄（從舊到新，務必延續上下文）】\n{turns_str}"
    )


async def generate_reona_reply(
    user_text: str = "",
    image_url: str | None = None,
    proactive_hint: str | None = None,
    channel: discord.abc.Messageable | None = None,
) -> str:
    global turns_since_last_git_sync
    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = now_dt.timestamp()

    step_body_recovery(now_dt)

    live_history = await fetch_channel_history(channel, limit=15)

    brb_note = None
    if user_text:
        update_profile_from_text(user_text)
        apply_food_and_body_effects(user_text, now_dt)
        if not live_history:
            memory["recent_turns"].append({"role": "Nick", "text": user_text})
            memory["recent_turns"] = memory["recent_turns"][-12:]
        brb_note = maybe_schedule_brb_interrupt(now_dt)

    sys_prompt = build_system_prompt(
        now_dt,
        live_history=live_history,
        user_text=user_text,
        brb_note=brb_note,
        is_proactive=bool(proactive_hint),
    )

    if user_text:
        memory["last_interaction_ts"] = now_ts

    fallback_phrases = [
        "唔…剛才這邊網路訊號好像飄了一下，等我一下下",
        "欸剛才訊息好像卡住了沒傳出去，我重新抓一下訊號",
        "唔…手機剛才轉圈圈了一下，現在好一點了嗎",
    ]

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
                            or "（對方傳了一張照片給你，請專注回應這張照片的細節，用自然有情緒的繁體中文接話，不要硬塞無關背景設定）",
                        },
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ]
            resp = await groq_client.chat.completions.create(
                model=VISION_MODEL,
                messages=messages,
                temperature=0.88,
                max_tokens=600,
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
                        temperature=0.92,
                        frequency_penalty=0.65,
                        presence_penalty=0.50,
                        max_tokens=600,
                    )
                    if candidate_model != TEXT_MODEL:
                        globals()["TEXT_MODEL"] = candidate_model
                        print(f"[Groq Model Switched] Now using TEXT_MODEL={TEXT_MODEL}", flush=True)
                    break
                except Exception as model_err:
                    last_model_err = model_err
                    err_str = str(model_err)
                    print(f"[Model Candidate Failed] {candidate_model} -> {err_str[:140]}", flush=True)
                    if any(
                        token in err_str.lower()
                        for token in [
                            "429",
                            "rate_limit",
                            "rate limit",
                            "tokens per day",
                            "requests per day",
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

        raw_content = resp.choices[0].message.content if resp.choices and resp.choices[0].message else ""
        reply = (raw_content or "").strip()
        reply = reply.strip("\"'「」").rstrip("。").strip()

        if not reply:
            print("[Empty Model Output] Falling back to safe phrase.", flush=True)
            reply = random.choice(fallback_phrases)

        if live_history:
            memory["recent_turns"] = (live_history + [{"role": "Reona", "text": reply}])[-12:]
        else:
            memory["recent_turns"].append({"role": "Reona", "text": reply})
            memory["recent_turns"] = memory["recent_turns"][-12:]

        memory["recent_replies"].append(reply[:18])
        if len(memory["recent_replies"]) > 6:
            memory["recent_replies"].pop(0)

        save_memory(memory)

        turns_since_last_git_sync += 1
        if turns_since_last_git_sync >= 4:
            turns_since_last_git_sync = 0
            asyncio.create_task(asyncio.to_thread(sync_memory_to_git, "auto-sync: periodic memory backup"))

        return reply

    except Exception as e:
        print(f"[Groq API Error] {e}", flush=True)
        return random.choice(fallback_phrases)


# =========================================================
# 6. Discord Client、全天候主動分享/露餡/斷點回歸與 5h45m 自動接力
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
    weekday_zh = WEEKDAY_NAMES_ZH[now_dt.weekday()]
    symptom = memory["body_state"].get("current_symptom", "")
    activity = memory["body_state"].get("daily_activity", "")
    weather = memory["weather_state"]
    severity = memory["body_state"].get("severity", "minor")
    shared = memory["user_profile"].get("shared_memories", [])
    memo_tip = f"（也可順便自然提起你們之前的小默契：「{random.choice(shared)}」）" if shared and random.random() < 0.35 else ""

    if 2 <= hour < 8:
        return None

    if severity in ("moderate", "severe"):
        return (
            f"今天是{weekday_zh}，天氣「{weather['label']}」，你40公斤底子偏弱，現在「{symptom}」，"
            f"只針對這一個身體感覺軟綿綿地向他撒嬌討拍，語氣真實直白不講雞湯，用繁體中文主動傳一則短訊息找他{memo_tip}"
        )

    if 23 <= hour or hour < 2:
        return (
            f"現在是{weekday_zh}深夜，你一個人窩在被窩裡，白天安靜下來後偷偷想念滿出來了，"
            f"小聲坦白其實一直在想他、希望早點到下次見面的日子，用繁體中文主動傳一則短短的訊息給他{memo_tip}"
        )
    elif 8 <= hour < 12:
        return (
            f"現在是{weekday_zh}早上，今天天氣「{weather['label']}」，你剛醒不久，用自然直白的語氣跟繁體中文問他醒了沒、"
            f"順便聊聊今天想吃什麼早餐（只聊這一個話題），主動傳一則短訊息給他{memo_tip}"
        )
    elif 12 <= hour < 18:
        return (
            f"現在是{weekday_zh}下午，今天天氣「{weather['label']}」，你剛經歷了「{activity}」，"
            f"只聚焦分享這一件生活碎片或小選擇題給他，不要堆砌其他設定，用繁體中文主動傳一則短短的訊息找他聊天{memo_tip}"
        )
    else:
        return (
            f"現在是{weekday_zh}傍晚到晚上，今天天氣「{weather['label']}」，你剛經歷了「{activity}」，"
            f"只聚焦分享這件晚餐或今天的小趣事（不要同時硬塞麻糬跟插畫），用繁體中文主動傳一則短訊息給他{memo_tip}"
        )


@client.event
async def on_ready():
    now_dt = datetime.now(SYDNEY_TZ)
    weekday_zh = WEEKDAY_NAMES_ZH[now_dt.weekday()]
    now_syd = f"{now_dt.strftime('%Y-%m-%d')} ({weekday_zh}) {now_dt.strftime('%H:%M')}"
    cycle_info = get_cycle_status(now_dt)
    weather = update_daily_context(now_dt)
    target_ch = get_target_channel()
    if target_ch:
        hist = await fetch_channel_history(target_ch, limit=15)
        if hist:
            memory["recent_turns"] = hist[-12:]
            save_memory(memory)
    print(
        f"[Reona Online] Logged in as {client.user} | Sydney: {now_syd} | Weather: {weather['label']} | Cycle Day: {cycle_info['day']} ({cycle_info['phase']}) | Loaded History: {len(memory['recent_turns'])} msgs",
        flush=True,
    )
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
        reply = await generate_reona_reply(
            user_text=user_text,
            image_url=image_url,
            channel=message.channel,
        )
        if reply and reply.strip():
            await message.channel.send(reply.strip())


@tasks.loop(seconds=60)
async def background_loop():
    global relay_triggered
    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = now_dt.timestamp()

    # 0. 生活小斷點回歸（去倒水、跑廁所、麻糬搗蛋、小睡後回來續聊）
    brb = memory["body_state"].get("pending_brb_followup")
    if brb and now_ts >= brb.get("trigger_ts", 0):
        memory["body_state"]["pending_brb_followup"] = None
        save_memory(memory)
        target_ch = get_target_channel()
        if target_ch:
            hint = (
                f"你剛才因為「{brb.get('reason', '')}」暫時離開了一下，現在忙完了回到手機前，"
                "自然接續剛才的氣氛用繁體中文主動傳一則短短的訊息告訴他你回來了"
            )
            msg = await generate_reona_reply(proactive_hint=hint, channel=target_ch)
            if msg and msg.strip():
                await target_ch.send(msg.strip())
                print(f"[BRB Followup Sent] {msg}", flush=True)

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
                "只聊這一個身體感覺，語氣真實直白、帶點小撒嬌或小懊惱地用繁體中文傳一則短訊息跟他說"
            )
            msg = await generate_reona_reply(proactive_hint=hint, channel=target_ch)
            if msg and msg.strip():
                await target_ch.send(msg.strip())
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
                msg = await generate_reona_reply(proactive_hint=hint, channel=target_ch)
                if msg and msg.strip():
                    await target_ch.send(msg.strip())
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
