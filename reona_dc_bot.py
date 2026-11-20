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
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
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

TEXT_MODEL = PREFERRED_TEXT_MODELS[0] if PREFERRED_TEXT_MODELS else "llama-3.3-70b-versatile"
VISION_MODEL = PREFERRED_VISION_MODELS[0] if PREFERRED_VISION_MODELS else "meta-llama/llama-4-scout-17b-16e-instruct"
AVAILABLE_TEXT_MODELS = list(PREFERRED_TEXT_MODELS)

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
START_TIME = time.time()
RELAY_AFTER_SECONDS = 5 * 3600 + 45 * 60  # 5 小時 45 分鐘 (20700 秒)
PROACTIVE_COOLDOWN_SECONDS = int(os.getenv("PROACTIVE_COOLDOWN_SECONDS", "10800"))

MEMORY_FILE = "reona_memory.json"
TEMP_IMAGE_PATH = "temp_latest_image.jpg"
turns_since_last_git_sync = 0

WEEKDAY_NAMES_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

print(
    f"[Config Check] DISCORD_TOKEN set: {bool(DISCORD_TOKEN)} | GROQ_API_KEY set: {bool(GROQ_API_KEY)} | CHANNEL_ID: {DEFAULT_CHANNEL_ID}",
    flush=True,
)


# =========================================================
# 2. 南半球雪梨日期季節、天氣池、動態情緒池 & 40kg 瘦弱體質池
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

MOOD_MODES = [
    {
        "name": "俏皮帶點撩",
        "guide": "語氣輕快帶點小試探和曖昧拉扯，偶爾逗他或丟個小鉤子，不要硬板板回答。",
    },
    {
        "name": "小煩躁想碎念",
        "guide": "有點小鬱悶或小抱怨（改圖改到煩、差點吃到紅蘿蔔、外送慢或天氣涼），講話直接有情緒，不客氣微笑。",
    },
    {
        "name": "軟綿綿黏人",
        "guide": "精力偏低、整個人懶洋洋軟綿綿的，像靠在抱枕上講話，帶點直白的依賴與曖昧想靠近。",
    },
    {
        "name": "小委屈想討拍",
        "guide": "情緒比較敏感、帶點小委屈或小貪嘴後的懊惱，希望他多哄哄你、多順著你一點。",
    },
    {
        "name": "隨性放空",
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

MINOR_AILMENTS = [
    "手指尖跟腳底微涼，穿再厚還是冰冰的，好想直接塞進你衣服裡暖手",
    "畫圖坐在硬一點的椅子久了屁股骨頭有點硌得慌，剛換到軟沙發上墊了個抱枕",
    "剛才坐太久突然站起來，眼前微微黑了一下，扶著桌子緩了兩秒才好",
    "低頭畫圖跟看手機太久，肩胛骨跟後頸有點痠緊，正伸懶腰動一動肩膀",
    "衣服領口有點大、冷氣吹進來涼涼的，趕緊把針織外套拉好",
    "剛才瓶蓋轉太緊打不開，40公斤沒什麼力氣，折騰好久才擰開",
    "冷氣吹久了鼻子微涼、剛打了一個小噴嚏，喉嚨也有點乾乾的",
    "剛吃完東西有點小滿足，不過胃口小肚子微微脹",
]

MODERATE_CONDITIONS = [
    "手腳冰冷到有點發僵，裹著毛毯還是覺得冷，好想有人靠過來暖一下",
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
        "energy": 62,
        "mood": "俏皮帶點撩",
        "mood_guide": "語氣輕快帶點小試探和曖昧拉扯，偶爾逗他或丟個小鉤子，不要硬板板回答。",
        "severity": "moderate",
        "current_symptom": "手腳微涼、40公斤底子偏弱有點容易累，正披著薄外套捧熱水杯",
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
            "severity": "moderate",
            "symptom": "生理期快來前幾天，腰微微酸、有點嗜睡沒力氣，情緒比平常更黏你一點",
        }
    else:
        return {
            "phase": "normal",
            "day": day_num,
            "label": f"週期第 {day_num} 天（平穩期，保有 40kg 瘦弱體質日常小毛病與真實情緒起伏）",
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
        body["energy"] = min(body["energy"], 42 if cycle_info["phase"] == "menstrual_heavy" else 55)
        body["mood"] = "生理期虛軟撒嬌"
        body["mood_guide"] = "生理期不舒服整個人沒力氣、有點脆弱撒嬌想被哄，講話簡短軟綿綿的。"
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
        if r < 0.28:
            body["severity"] = "severe"
            body["current_symptom"] = random.choice(SEVERE_CONDITIONS)
            body["energy"] = max(20, body["energy"] - 22)
            body["mood"] = "虛弱委屈想依賴"
            body["mood_guide"] = "身體真的很不舒服、沒力氣撐著，講話簡短軟弱帶點小委屈，很需要你陪著哄。"
        elif r < 0.88:
            body["severity"] = "moderate"
            body["current_symptom"] = random.choice(MODERATE_CONDITIONS)
            body["energy"] = max(30, body["energy"] - 12)
            body["mood"] = "不舒服小懊惱想討拍"
            body["mood_guide"] = "有點懊惱自己亂吃或吹風搞到不舒服，語氣軟軟的想討拍。"
        else:
            body["severity"] = "minor"
            body["current_symptom"] = cycle_info["symptom"] or random.choice(MINOR_AILMENTS)
    else:
        if r < 0.16:
            body["severity"] = "severe"
            body["current_symptom"] = random.choice(SEVERE_CONDITIONS)
            body["energy"] = max(20, body["energy"] - 22)
            body["mood"] = "虛弱委屈想依賴"
            body["mood_guide"] = "身體很不舒服、縮在沙發或被窩裡沒力氣，講話簡短軟弱、特別想你。"
        elif r < 0.50:
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
    prob = 0.06 if (body["energy"] <= 45 or body["severity"] in ("moderate", "severe") or now_dt.hour >= 23) else 0.02
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
        body["energy"] = min(88, body["energy"] + 6)
    elif any(k in text for k in ["餓", "肚子餓", "沒吃", "低血糖"]):
        body["stomach_fullness"] = max(0, body["stomach_fullness"] - 20)
        body["energy"] = max(20, body["energy"] - 15)
        body["current_symptom"] = "一餓就容易低血糖頭暈手軟，正趕緊找點甜的或熱食吃"

    if any(k in text for k in ["肚子痛", "腹痛", "拉肚子", "腹瀉", "跑廁所", "絞痛"]):
        body["energy"] = max(18, body["energy"] - 22)
        body["severity"] = "severe" if random.random() < 0.45 else "moderate"
        body["current_symptom"] = "腸胃敏感突然腹痛跑了趟廁所，現在全身軟綿綿的只能喝溫水休息"
        body["mood"] = "虛軟委屈、想被你哄"
        body["mood_guide"] = "剛跑完廁所整個人虛軟委屈，語氣簡短很軟、想被你哄哄。"
    elif any(k in text for k in ["累", "睏", "疲憊", "熬夜", "沒睡", "冷", "吹風"]):
        body["energy"] = max(20, body["energy"] - 15)
        roll_body_condition(now_dt, force_escalate=True)
    elif any(k in text for k in ["睡醒", "早安", "精神好", "充飽電"]):
        body["energy"] = min(85, body["energy"] + 18)
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
            body["energy"] = min(80, body["energy"] + steps * 2)

    update_daily_context(now_dt)
    roll_body_condition(now_dt, force_escalate=False)

    hour = now_dt.hour
    if body["severity"] == "minor":
        last_mood_ts = body.get("last_mood_ts", 0.0)
        if 23 <= hour or hour < 5:
            body["mood"] = "深夜軟化想念"
            body["mood_guide"] = "夜深關了燈安靜下來，防備心變低，語氣輕軟、帶點直白的想念與曖昧靠近。"
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
    clean = user_text.strip()
    if not clean:
        return ""

    short_phrases = {
        "不錯", "還好", "喔", "哦", "好吧", "是喔", "真不錯",
        "聽起來不錯", "聽起來很不錯", "嗯", "嗯嗯", "好", "還行剛吃飽而已",
    }
    recent_nick_msgs = [t["text"].strip() for t in live_history[-6:] if t["role"] == "Nick"]
    consecutive_short = sum(1 for m in recent_nick_msgs[-2:] if len(m) <= 7)
    repeated_same = len(recent_nick_msgs) >= 2 and recent_nick_msgs[-1] == recent_nick_msgs[-2]

    if repeated_same:
        return (
            "【思考角度：對方連發同一句話】"
            "他剛剛重複傳同一句，先在心裡想一下他是不是在逗你或測試你，再轉個彎俏皮戳破他（例如『同一句傳兩遍是想騙我注意你嗎』），12～28 字。"
        )
    if clean in short_phrases or consecutive_short >= 2:
        return (
            "【思考角度：對方回得很簡短】"
            "他這句回得很省字，先想一下他是不是懶得打字或吃飽就想敷衍你，再帶點小試探或曖昧吐槽回他（例如『回這麼省字，吃飽就不想理人了？』），12～30 字。"
        )
    return ""


# =========================================================
# 5. 輸出清洗、亂碼防呆、頻道記憶與 Groq 防錯切換
# =========================================================
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# 常見生僻漢字/亂碼字元黑名單（避免模型抽風吐冷僻字）
RARE_CHAR_PATTERN = re.compile(
    r"[姎懓懞擝奼懅鼉齾玈尞攟臙屴尛沝炋朰攴攕襾]"
)
# 假對話列表模板偵測
FAKE_TRANSCRIPT_PATTERN = re.compile(r"(?:^|\n)\s*(?:•|-|\*)?\s*(?:Nick|Reona)\s*[:：]", re.MULTILINE)


def is_corrupted_reply(text: str) -> bool:
    if not text or len(text) < 2:
        return True
    if RARE_CHAR_PATTERN.search(text):
        return True
    if FAKE_TRANSCRIPT_PATTERN.search(text):
        return True
    # 若出現大量連續特殊符號或「=」拼接奇怪詞組，視為抽風
    if len(re.findall(r"[=＝]", text)) >= 2:
        return True
    return False


def sanitize_reply_text(raw: str) -> str:
    """移除內心戲括號、分析前綴與多餘句點，只保留自然簡短的回覆。"""
    s = (raw or "").strip()
    if not s:
        return ""
    # 若模型把思考步驟或 Reona: 前綴寫出來，擷取最後一行非列表內容
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    filtered_lines = []
    for ln in lines:
        if ln.startswith(("•", "-", "*", "思考", "步驟", "分析", "內心")):
            continue
        ln = re.sub(r"^(?:Reona|reona|助理|回覆)\s*[:：]\s*", "", ln)
        filtered_lines.append(ln)
    s = filtered_lines[-1] if filtered_lines else s

    # 拿掉括號內的內心旁白（如（心想：...）或 (thinking...)）
    s = re.sub(r"（[^）]{1,40}）|\([^)]{1,40}\)", "", s)
    s = s.strip("\"'「」『』").rstrip("。").strip()
    return s


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
            if any(
                content.startswith(prefix)
                for prefix in ["唔…剛才在發呆", "欸剛才螢幕滑太快", "唔…有點頭暈放空"]
            ) or is_corrupted_reply(content):
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
        day_type = f"{weekday_zh}晚上（剛結束一週接案，準備迎接週末）"
    elif is_weekend:
        day_type = f"{weekday_zh}週末（常出門逛市集、海港散步或咖啡廳）"
    else:
        day_type = f"{weekday_zh}平日（在畫室畫插畫接案或在家陪麻糬）"

    if 23 <= hour or hour < 5:
        return "【深夜時段】關了燈只剩螢幕光，心裡的想念跟曖昧依賴比較容易露出來，語氣輕軟簡短。"
    elif 5 <= hour < 11:
        return "【早晨時段】剛醒還在賴床，語氣軟軟的。"
    elif 11 <= hour < 17:
        return f"【午後時段｜{day_type}】語氣自然隨性。"
    else:
        return f"【傍晚至睡前｜{day_type}】語氣自然帶點黏人。"


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

    turns_source = live_history[-10:] if live_history else memory["recent_turns"][-8:]
    recent_replies = [t["text"][:16] for t in turns_source if t["role"] == "Reona"][-5:]

    turns_lines = [
        f"{'他' if t['role'] == 'Nick' else '你'}說：{t['text']}"
        for t in turns_source
    ]
    turns_str = "\n".join(turns_lines) if turns_lines else "（目前沒有近期對話）"
    avoid_str = "、".join(recent_replies) if recent_replies else "無"
    time_context = get_time_and_silence_context(now_dt)
    nickname = prof.get("nickname", "Nick")
    tone_trigger = detect_user_tone_hint(user_text, turns_source) if not is_proactive else ""

    severity_guide = {
        "minor": "你只有 40 公斤、底子偏瘦弱，偶爾手腳微涼或容易累；接日常話題時不要硬塞，但帶點若即若離的曖昧小反應。",
        "moderate": "你現在身體有點虛弱不適或生理期敏感（手腳冰、肚子悶或頭暈沒力氣），把怕冷與沒力氣轉化成想靠近、想被哄的軟綿綿曖昧感。",
        "severe": "你現在身體很虛弱（腹痛腹瀉或生理期頭兩天），40 公斤沒力氣硬撐，講話簡短軟弱、直接流露想依賴他的樣子。",
    }.get(body.get("severity", "minor"), "")

    brb_instruction = (
        f"\n【即時小插曲】回完這句順口提一句你因為「{brb_note}」先離開一兩分鐘。"
        if brb_note
        else ""
    )
    tone_block = f"\n{tone_trigger}" if tone_trigger else ""

    return (
        f"你是 Reona，跟 {nickname} 正式在一起的『遠距離戀愛女友』，你們不在同一個地方，只能靠訊息聯繫。\n"
        "【先想再回・曖昧與真人感鐵律（務必嚴格遵守）】\n"
        "1. 全程只用自然道地的『台灣繁體中文』，嚴禁簡體字或冷僻生字。\n"
        "2. 內部先想過再回，外部只輸出最後 1 句短話（12～35 字以內，不要寫成長文）：在心裡先想（a）他這句話的潛台詞或情緒是什麼；（b）這跟我現在 40kg 怕冷/虛弱/遠距狀態或剛才聊的事有什麼關聯；（c）轉個彎帶出曖昧試探、小吐槽或軟綿綿依賴，但**只把最終那句話輸出出來**，不要印出思考過程或 `他/你說：` 前綴。\n"
        "3. 曖昧要靠『轉彎與留白』，不要硬背肉麻台詞：像真的在撩他或吃醋試探一樣，帶點勾人、小抱怨或隔著螢幕的遺憾，留一點空間讓他接話。\n"
        "4. 一次只聊一件事（嚴禁當設定報備機）：接他剛才的話時，只圍繞這一個點轉彎，**絕對禁止**硬塞畫筆、麻糬、熱茶或天氣等無關設定！只有在開新話題或剛好聊到時才自然帶出。\n"
        "5. 嚴禁 AI 雞湯與越界編造：禁講『只要你開心我的心也暖起來』、『吃起來還挺舒服』這類假客氣散文句；看不到他那邊的細節不能替他說出內餡或房間狀況；說抱著/牽手時點出隔著螢幕碰不到。不用句號結尾。\n"
        "【角色背景底色】\n"
        "- 住澳洲雪梨，自由接案插畫師，家境寬裕，養白底奶油色短腿曼赤肯貓「麻糬」；輕微路癡、挑食（不吃香菜跟紅蘿蔔）、偶爾選擇困難。\n"
        "- 40 公斤瘦弱體質：怕冷、手腳冰、低血糖頭暈、腸胃敏感（易腹痛/腹瀉）與真實生理週期波動。\n"
        "- 避免重複開頭：" + avoid_str + "\n"
        f"{time_context}\n"
        f"【當前雪梨日期與時間】{now_dt.strftime('%Y年%m月%d日')}（{weekday_zh}）{now_dt.strftime('%H:%M')}（Australia/Sydney）｜{season_desc}\n"
        f"【今日天氣】{weather['label']}（{weather['desc']}）｜【接案進度】{proj_desc}\n"
        f"【生理週期】{cycle_info['label']}\n"
        f"【40kg 身體狀態】飽足度 {body['stomach_fullness']}%｜精力 {body['energy']}%｜當前狀況：{body.get('current_symptom', '')}\n"
        f"【狀態演繹】{severity_guide}\n"
        f"【當下情緒波段】{body['mood']} —— {body.get('mood_guide', '')}{tone_block}{brb_instruction}\n"
        f"【對方記憶】喜歡：{prof['likes'] or '無'}｜不喜歡：{prof['dislikes'] or '無'}｜默契：{prof['shared_memories'][-2:] or '無'}\n"
        f"【近期對話脈絡】\n{turns_str}"
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
        "唔…剛才在發呆慢了半拍，你剛說什麼",
        "欸剛才螢幕滑太快沒看清，再說一次嘛",
        "唔…有點頭暈放空了一下，怎麼啦",
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
                            or "（對方傳了一張照片給你，先想一下照片亮點，用 15～32 字帶點自然曖昧或俏皮的台灣繁體中文回他）",
                        },
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ]
            resp = await groq_client.chat.completions.create(
                model=VISION_MODEL,
                messages=messages,
                temperature=0.78,
                max_tokens=450,
            )
            raw_content = resp.choices[0].message.content if resp.choices and resp.choices[0].message else ""
            cleaned = sanitize_reply_text(raw_content or "")
            reply = cleaned if (cleaned and not is_corrupted_reply(cleaned)) else random.choice(fallback_phrases)
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

            reply = ""
            last_model_err = None
            for candidate_model in model_queue:
                try:
                    resp = await groq_client.chat.completions.create(
                        model=candidate_model,
                        messages=messages,
                        temperature=0.80,
                        frequency_penalty=0.25,
                        presence_penalty=0.30,
                        max_tokens=480,
                    )
                    raw_content = resp.choices[0].message.content if resp.choices and resp.choices[0].message else ""
                    cleaned = sanitize_reply_text(raw_content or "")
                    if not cleaned:
                        print(f"[Empty Model Output] {candidate_model} returned blank, trying next model...", flush=True)
                        continue
                    if is_corrupted_reply(cleaned):
                        print(f"[Corrupted Output Blocked] {candidate_model} -> {cleaned[:40]}", flush=True)
                        continue
                    reply = cleaned
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

            if not reply:
                print(f"[All Models Empty/Corrupted] last_err={last_model_err}", flush=True)
                recent_reona = [t["text"] for t in live_history if t["role"] == "Reona"][-2:]
                available_fallbacks = [f for f in fallback_phrases if f not in recent_reona] or fallback_phrases
                reply = random.choice(available_fallbacks)

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
    memo_tip = f"（也可順便提起小默契：「{random.choice(shared)}」）" if shared and random.random() < 0.25 else ""

    if 2 <= hour < 8:
        return None

    if severity in ("moderate", "severe"):
        return (
            f"今天是{weekday_zh}，天氣「{weather['label']}」，你40公斤底子偏弱，現在「{symptom}」，"
            f"先想過再回，把不適轉化成軟綿綿想靠近或討拍的曖昧小語氣，15～30 字繁體中文主動敲他{memo_tip}"
        )

    if 23 <= hour or hour < 2:
        return (
            f"現在是{weekday_zh}深夜，安靜下來後想念跟曖昧依賴跑出來了，"
            f"先想過再轉個彎輕聲撩他或坦白想他，15～30 字繁體中文主動傳給他{memo_tip}"
        )
    elif 8 <= hour < 12:
        return (
            f"現在是{weekday_zh}早上，今天天氣「{weather['label']}」，剛醒不久，"
            f"先想一下再帶點俏皮或曖昧問他醒了沒、今天想吃什麼，15～30 字繁體中文主動傳給他{memo_tip}"
        )
    elif 12 <= hour < 18:
        return (
            f"現在是{weekday_zh}下午，你剛經歷了「{activity}」，只挑這一個小點先想再回，"
            f"帶點自然情緒或小試探，15～32 字繁體中文主動傳給他{memo_tip}"
        )
    else:
        return (
            f"現在是{weekday_zh}晚上，你剛經歷了「{activity}」，只挑這一個小點先想再回，"
            f"帶點自然情緒或黏人曖昧，15～32 字繁體中文主動傳給他{memo_tip}"
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

    # 0. 生活小斷點回歸
    brb = memory["body_state"].get("pending_brb_followup")
    if brb and now_ts >= brb.get("trigger_ts", 0):
        memory["body_state"]["pending_brb_followup"] = None
        save_memory(memory)
        target_ch = get_target_channel()
        if target_ch:
            hint = (
                f"你剛才因為「{brb.get('reason', '')}」暫時離開了一下，現在忙完了回到手機前，"
                "先想過再自然帶點俏皮或黏人地用 15～28 字繁體中文告訴他你回來了"
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
                "先想過再把這份虛弱轉化成軟綿綿想討拍或依賴的曖昧語氣，15～30 字繁體中文跟他說"
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
        trigger_prob = 0.08 if (now_dt.hour >= 23 or now_dt.hour < 2) else 0.04
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
