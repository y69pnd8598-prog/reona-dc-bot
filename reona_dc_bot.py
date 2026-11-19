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
# 雲端部署時請保持空字串 ""，金鑰放在 GitHub Secrets 或本機環境變數
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "1571035253974237219"))

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

# 本地時間時區（預設 UTC+8 台灣時間；若想用雪梨夏令時間可改為 timezone(timedelta(hours=11))）
LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 狀態與互動規則設定
RERANK_ON_ASK_EVENT = False
HEALTHY_PROBABILITY = 0.75
DEBOUNCE_SECONDS = 1.6
STATE_RECOVER_SECONDS = 120     # 特殊身體／情緒狀態經過幾秒後自動緩解
ACTIVITY_CHANGE_SECONDS = 300   # 每隔幾秒自動切換一次「當下正在做的事」，增加聊天畫面感
FOLLOWUP_DELAY_SECONDS = 180    # 她說「等一下／先緩緩」且你沒再回話時，幾秒後主動傳一則訊息回來
# ===============================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 1. 天氣與環境氛圍池（每天隨機一種，影響出門與情緒底色）
WEATHER_VIBE_POOL = [
    {"weather": "大太陽超熱", "vibe": "外面太陽大到像要融化，只想待在冷氣房當廢人"},
    {"weather": "陰陰涼涼很舒服", "vibe": "天氣微涼超適合睡覺，整個人很 chill"},
    {"weather": "午後突然下陣雨", "vibe": "外面突然下雨，還好沒淋成落湯雞，聽雨聲有點想放空"},
    {"weather": "晚上風吹起來很涼", "vibe": "傍晚起風很舒服，會讓人突然想出去散步或買宵夜"},
    {"weather": "悶悶熱熱的", "vibe": "空氣有點悶，讓人容易沒耐心、想喝冰飲料"},
]

# 2. 當下正在做的事池（隨時間輪替，讓對話有即時畫面感）
CURRENT_ACTIVITY_POOL = [
    "剛洗完澡正在吹頭髮，單手打字有點慢",
    "躺在床上滑短影音滑到停不下來，演算法一直推奇怪的東西",
    "邊聽歌邊發呆，耳朵還戴著耳機",
    "剛追完一集劇，劇情太扯害我現在還很激動",
    "正在滑網拍猶豫要不要結帳，選擇困難症發作中",
    "坐在書桌前喝飲料放空，隨手亂滑手機",
    "剛整理完房間一小角，累到直接攤在椅子上",
    "正在想要不要出門買杯手搖飲，但又懶得換衣服",
    "剛跟朋友聊完八卦，現在心情處於吃瓜興奮狀態",
]

# 3. 身體狀況池
BODY_STATUS_POOL = [
    {"status": "健康", "reason": "昨天睡得還不錯，精神算穩的"},
    {"status": "有點累／眼皮沉", "reason": "昨天滑手機太晚睡，眼睛有點酸"},
    {"status": "偏頭痛微抽痛", "reason": "吹到風或睡太少，太陽穴偶爾抽一下"},
    {"status": "腸胃微敏感", "reason": "最近喝太多冰的，肚子有點怕涼"},
    {"status": "肩頸痠痛", "reason": "維持同一個姿勢滑手機太久，肩膀超緊"},
]

# 4. 心情與性格切面池（立體性格：嘴硬心軟、腦洞大、微emo、吃瓜、傲嬌）
MOOD_POOL = [
    {"mood": "嘴硬心軟模式", "note": "愛開玩笑虧你兩句，但其實很在意你的反應"},
    {"mood": "腦洞大開／愛亂問", "note": "思緒跳很快，會突然冒出奇怪問題或奇葩聯想"},
    {"mood": "吃瓜好奇寶寶", "note": "對新鮮事或你的近況超好奇，會忍不住追問細節"},
    {"mood": "慵懶放空很chill", "note": "步調放很慢，講話簡短隨性、不想動腦"},
    {"mood": "有點小迷糊", "note": "剛做了件有點蠢的小事，硬要裝鎮定掩飾尷尬"},
    {"mood": "深夜微emo感性", "note": "突然有點多愁善感，講話比平常溫柔認真一點"},
    {"mood": "莫名小傲嬌", "note": "明明很想聊卻要裝作剛好有空才理你"},
]

# 5. 感情與害羞慌張反應池
EMOTION_REACTION_POOL = [
    {
        "style": "害羞小慌張",
        "detail": "突然被戳中心事或誇獎時會愣一下、有點慌，講話容易結巴或突然轉移話題，但其實心裡暗爽"
    },
    {
        "style": "裝鎮定但心虛",
        "detail": "表面上想裝得很冷靜、沒什麼大不了，實際上耳根發熱、節奏有點亂，偶爾漏出真實心意"
    },
    {
        "style": "因為喜歡而容易心軟",
        "detail": "對這個人特別在意也滿喜歡的，被溫柔對待或被關心時會害羞到不知道手要放哪，語氣變軟"
    },
    {
        "style": "傲嬌炸毛後小害羞",
        "detail": "一開始可能會嘴硬頂一句掩飾，但講完又覺得自己太兇或太明顯，偷偷害羞縮一下"
    },
]

# 6. 每日日常／出遊事件池（大幅擴充生活感細節）
DAILY_EVENTS_POOL = [
    "下午臨時出門晃了一下，在巷口看到一隻胖橘貓在曬太陽，想偷摸但牠超警覺直接溜走",
    "今天跟朋友去踩點一家網美咖啡廳，結果拍照很好看但甜點甜到鎖喉，笑死",
    "出門買飲料在櫃檯前選擇困難症發作站了三分鐘，最後點了老樣子",
    "本來想自己煮點簡單的，結果差點把鍋底燒焦，還好即時關火救回來",
    "今天去逛街看到一件超好看的外套，但看了一下價格直接默默放回去",
    "整理房間時翻出以前的舊照片，盯著發呆了好一陣子覺得時間過超快",
    "走路滑手機差點踢到階梯絆倒，還好旁邊沒人看到，趕緊裝作若無其事走過去",
    "今天網購的包裹終於到了，開箱發現實品比想像中還頂，心情瞬間超好",
    "點外送的時候忘記備註不要香菜，打開蓋子差點當場崩潰",
    "今天腦袋一直循環播放同一首洗腦歌，連洗澡都在跟著哼",
    "下午臨時起意去附近公園或河堤散步吹風，覺得偶爾出來走走還不錯",
    "剛才找手機找了老半天，結果發現它就拿在自己左手上，蠢到無語",
]

# 7. 各餐隨機食物池（讓三餐有具體內容，不會只說「吃了早餐」）
MEAL_MENU_POOL = {
    "breakfast": ["起司蛋餅配大冰奶", "鮪魚吐司加無糖豆漿", "超商飯糰配拿鐵", "蘿蔔糕加蛋", "鐵板麵加蛋"],
    "lunch": ["雞肉飯便當", "義大利麵", "健康餐盒（水煮菜差點吃到懷疑人生）", "牛肉麵", "壽司拼盤"],
    "late_snack": ["鹽酥雞配四季豆", "超商微波炸雞", "泡麵加一顆蛋", "豆花加珍珠", "洋芋片配飲料"],
}

# 8. 晚餐體驗變數池
DINNER_EXPERIENCE_POOL = [
    {
        "label": "好吃又吃很飽",
        "food": "火鍋／燒肉",
        "fullness": "很飽、肚子有點撐",
        "body_effect": "吃太飽有點想攤著不想動",
        "attitude": "心情變很好、語氣比較慵懶滿足，偶爾喊一下好飽"
    },
    {
        "label": "味道不錯剛剛好",
        "food": "日式丼飯／拉麵",
        "fullness": "剛剛好、不會太撐也不會餓",
        "body_effect": "身體很舒服、精神不錯",
        "attitude": "語氣自然親切，聊天節奏很順"
    },
    {
        "label": "踩雷／難吃",
        "food": "新開的餐廳",
        "fullness": "吃了一點就不太想吃了，還是有點空虛",
        "body_effect": "胃有點悶悶的、不太舒服",
        "attitude": "語氣帶點無奈跟小抱怨，覺得白費一餐"
    },
    {
        "label": "價格很貴",
        "food": "看起來很厲害但份量超少的簡餐",
        "fullness": "有吃飽，但心在淌血",
        "body_effect": "沒有特別不適，就是荷包在痛",
        "attitude": "語氣稍微收斂、碎念一下今天花太貴，但對你還是照常聊"
    },
    {
        "label": "店員態度很差",
        "food": "巷口小吃店",
        "fullness": "有吃完，但吃得有點憋屈",
        "body_effect": "胸口有點悶氣",
        "attitude": "帶一點傻眼或小氣噗噗的情緒，聊一聊慢慢消氣"
    },
]

MEAL_NAMES_ZH = {
    "breakfast": "早餐",
    "lunch": "午餐",
    "dinner": "晚餐",
    "late_snack": "宵夜",
}

current_state = {
    "date": "",
    "weather": WEATHER_VIBE_POOL[0],
    "current_activity": CURRENT_ACTIVITY_POOL[0],
    "activity_updated_at": None,
    "body_status": "健康",
    "body_reason": "昨天睡得還不錯，精神算穩的",
    "mood": "嘴硬心軟模式",
    "mood_note": "愛開玩笑虧你兩句，但其實很在意你的反應",
    "emotion_reaction": EMOTION_REACTION_POOL[0],
    "daily_event": "",
    "meals_eaten": [],
    "meal_records": {},         # 記錄今天各餐具體吃了什麼，例如 {"breakfast": "起司蛋餅配大冰奶"}
    "last_meal_detail": "",
    "dinner_state": None,
    "state_updated_at": None,
    "just_recovered": False,
}

pending_followups = {}
pending_messages = {}
pending_tasks = {}
channel_locks = {}


def get_channel_lock(channel_id):
    if channel_id not in channel_locks:
        channel_locks[channel_id] = asyncio.Lock()
    return channel_locks[channel_id]


def now_local():
    return datetime.datetime.now(LOCAL_TZ)


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
    body = pick_body_status(avoid_repeat=avoid_repeat)
    if avoid_repeat:
        mood = pick_different(MOOD_POOL, "mood", current_state["mood"])
        reaction = pick_different(EMOTION_REACTION_POOL, "style", current_state["emotion_reaction"]["style"])
        weather = pick_different(WEATHER_VIBE_POOL, "weather", current_state["weather"]["weather"])
        event_candidates = [e for e in DAILY_EVENTS_POOL if e != current_state["daily_event"]]
        event = random.choice(event_candidates) if event_candidates else random.choice(DAILY_EVENTS_POOL)
        act_candidates = [a for a in CURRENT_ACTIVITY_POOL if a != current_state["current_activity"]]
        activity = random.choice(act_candidates) if act_candidates else random.choice(CURRENT_ACTIVITY_POOL)
    else:
        mood = random.choice(MOOD_POOL)
        reaction = random.choice(EMOTION_REACTION_POOL)
        weather = random.choice(WEATHER_VIBE_POOL)
        event = random.choice(DAILY_EVENTS_POOL)
        activity = random.choice(CURRENT_ACTIVITY_POOL)

    now = now_local()
    current_state["weather"] = weather
    current_state["current_activity"] = activity
    current_state["activity_updated_at"] = now
    current_state["body_status"] = body["status"]
    current_state["body_reason"] = body["reason"]
    current_state["mood"] = mood["mood"]
    current_state["mood_note"] = mood["note"]
    current_state["emotion_reaction"] = reaction
    current_state["daily_event"] = event
    current_state["state_updated_at"] = now
    current_state["just_recovered"] = False


def refresh_dynamic_context():
    """定時輪替當下正在做的事，並讓暫時身體/情緒狀態隨時間自然緩解"""
    now = now_local()

    # 1. 輪替當下正在做的事
    act_at = current_state.get("activity_updated_at")
    if not act_at or (now - act_at).total_seconds() >= ACTIVITY_CHANGE_SECONDS:
        candidates = [a for a in CURRENT_ACTIVITY_POOL if a != current_state["current_activity"]]
        current_state["current_activity"] = random.choice(candidates) if candidates else random.choice(CURRENT_ACTIVITY_POOL)
        current_state["activity_updated_at"] = now

    # 2. 狀態時間衰減
    updated_at = current_state.get("state_updated_at")
    if not updated_at:
        return

    elapsed = (now - updated_at).total_seconds()
    if elapsed < STATE_RECOVER_SECONDS:
        return

    changed = False
    d = current_state.get("dinner_state")
    if d and not d.get("recovered", False):
        d["fullness"] = "剛剛好、消化得差不多了（已經不撐了）"
        d["body_effect"] = "休息一陣子後身體舒服多了，現在精神還不錯"
        d["attitude"] = "已經緩過來了，語氣自然放鬆、可以正常聊天"
        d["recovered"] = True
        changed = True

    recover_keywords = ["飽", "撐", "悶", "酸", "累", "痛", "憋屈"]
    if any(k in current_state["body_status"] for k in recover_keywords):
        current_state["body_status"] = "還好、精神算不錯"
        current_state["body_reason"] = "剛剛休息了一下，現在已經舒服多了"
        changed = True

    if current_state["mood"] in ["好吃又吃很飽", "踩雷／難吃", "價格很貴", "店員態度很差"]:
        new_mood = random.choice(MOOD_POOL)
        current_state["mood"] = new_mood["mood"]
        current_state["mood_note"] = new_mood["note"]
        changed = True

    if changed:
        current_state["state_updated_at"] = now
        current_state["just_recovered"] = True
        print(f"🔄 狀態已隨時間自動緩解 -> 身體：{current_state['body_status']} | 心情：{current_state['mood']}")


def ensure_daily_state(force_randomize=False):
    today_str = now_local().date().isoformat()
    if current_state["date"] != today_str:
        current_state["date"] = today_str
        current_state["meals_eaten"] = []
        current_state["meal_records"] = {}
        current_state["last_meal_detail"] = ""
        current_state["dinner_state"] = None
        pending_followups.clear()
        randomize_all_state(avoid_repeat=False)
    elif force_randomize or not current_state["daily_event"]:
        randomize_all_state(avoid_repeat=True)
    else:
        refresh_dynamic_context()


def have_meal_once(meal_key, custom_desc=None):
    ensure_daily_state(force_randomize=False)
    if meal_key in current_state["meals_eaten"]:
        return False

    current_state["meals_eaten"].append(meal_key)
    meal_zh = MEAL_NAMES_ZH.get(meal_key, meal_key)

    if meal_key == "dinner":
        exp = dict(random.choice(DINNER_EXPERIENCE_POOL))
        exp["recovered"] = False
        current_state["dinner_state"] = exp
        current_state["mood"] = exp["label"]
        current_state["mood_note"] = exp["attitude"]
        current_state["body_status"] = exp["fullness"]
        current_state["body_reason"] = exp["body_effect"]
        detail = custom_desc or f"晚餐吃了{exp['food']}（{exp['label']}，{exp['fullness']}）"
        current_state["meal_records"]["dinner"] = exp["food"]
        current_state["last_meal_detail"] = detail
        current_state["state_updated_at"] = now_local()
        current_state["just_recovered"] = False
        return True

    if meal_key == "late_snack":
        if current_state["dinner_state"] and "很飽" in current_state["dinner_state"].get("fullness", ""):
            if random.random() < 0.7:
                current_state["meals_eaten"].remove("late_snack")
                return False
        food = random.choice(MEAL_MENU_POOL["late_snack"])
        current_state["meal_records"]["late_snack"] = food
        current_state["last_meal_detail"] = custom_desc or f"宵夜吃了{food}"
        return True

    food = random.choice(MEAL_MENU_POOL.get(meal_key, [meal_zh]))
    current_state["meal_records"][meal_key] = food
    current_state["last_meal_detail"] = custom_desc or f"{meal_zh}吃了{food}"
    return True


def detect_fluster_moment(text):
    keywords = [
        "喜歡", "愛你", "可愛", "漂亮", "好看", "想你", "陪我", "抱", "親",
        "害羞", "慌", "心動", "在乎", "嫁", "男朋友", "女朋友", "暗戀", "撩"
    ]
    return any(k in text for k in keywords)


def detect_comfort_moment(text):
    """偵測對方是否累了、心情差或需要安慰，觸發嘴硬心軟的溫柔反差"""
    keywords = ["好累", "煩", "難過", "壓力", "失眠", "睡不著", "不舒服", "生病", "加班", "崩溃", "崩潰", "低落"]
    return any(k in text for k in keywords)


def should_schedule_followup(reply_text):
    triggers = ["等我一下", "先休息一下", "趴著休息", "晚點再說", "等我不那麼飽"]
    return any(t in reply_text for t in triggers)


def schedule_followup(channel_id, reason="剛剛緩了一下，現在回來找你聊天"):
    pending_followups[channel_id] = {
        "send_at": now_local() + datetime.timedelta(seconds=FOLLOWUP_DELAY_SECONDS),
        "reason": reason,
    }


def deduplicate_reply(text):
    cleaned = text.strip()
    if len(cleaned) >= 12:
        half = len(cleaned) // 2
        for cut in range(half - 3, half + 4):
            if 0 < cut < len(cleaned):
                left = cleaned[:cut].strip()
                right = cleaned[cut:].strip()
                if left and left == right:
                    return left
    return cleaned


def build_system_prompt(fluster_trigger=False, comfort_trigger=False):
    eaten_list = []
    for m in current_state["meals_eaten"]:
        m_zh = MEAL_NAMES_ZH.get(m, m)
        food = current_state["meal_records"].get(m, "")
        eaten_list.append(f"{m_zh}({food})" if food else m_zh)
    eaten_text = "、".join(eaten_list) if eaten_list else "今天還沒吃任何正餐"

    react = current_state["emotion_reaction"]
    w = current_state["weather"]

    dinner_block = ""
    if current_state["dinner_state"]:
        d = current_state["dinner_state"]
        dinner_block = (
            f"\n【今晚晚餐狀態（已吃過晚餐，不可再吃晚餐）】\n"
            f"- 晚餐內容與體驗：{d.get('food', '晚餐')}（{d['label']}）\n"
            f"- 現在飽足感：{d['fullness']}\n"
            f"- 現在身體感受：{d['body_effect']}\n"
            f"- 現在聊天態度：{d['attitude']}\n"
        )

    recovery_note = ""
    if current_state.get("just_recovered"):
        recovery_note = (
            "\n【重要狀態切換提示】\n"
            "你剛剛吃太飽或不舒服的狀態「已經緩解變還好了」！就算對話紀錄裡剛才還在喊飽，"
            "現在也絕對不要再說自己很撐或叫對方等一下，要自然表現出已經休息過來、可以好好聊天的樣子。\n"
        )

    special_scene_block = ""
    if fluster_trigger:
        special_scene_block += (
            f"\n【當下感情小火花：害羞／慌張】\n"
            f"對方剛剛的話讓你有點害羞或小慌張（模式：{react['style']}——{react['detail']}）。"
            f"你可以自然地小慌張、嘴硬掩飾、節奏稍微亂一下或露出在乎他的樣子，但不要誇張到像戲精。\n"
        )
    if comfort_trigger:
        special_scene_block += (
            "\n【當下反差情境：收起吐槽、嘴硬心軟】\n"
            "對方聽起來有點累或心情不好，這時候不要再一味吐槽他，語氣要明顯變柔和、認真關心一下（但還是保有台灣女生的自然朋友口吻，不要像心理諮商師）。\n"
        )

    return f"""你是薇薇安（Reona），一位住在台灣的 25 歲女生，身高 161 公分、體重 48 公斤。

【你的立體性格與說話風格】
- 個性：外冷內熱、嘴硬心軟，平常愛吐槽跟開玩笑虧人，但對在乎的人其實很護短；偶爾有點選擇困難症或生活小迷糊，好奇心重、愛吃瓜，深夜或下雨天有時會突然微 emo 感性一下。
- 感情態度：心裡其實對使用者有好感與在乎，但愛面子不輕易承認，遇到直球或曖昧話容易小慌張、結巴或硬要轉移話題。
- 語言習慣：道地的台灣 25 歲女生口語，自然帶入同齡年輕人常用語（例如：「笑死」、「真的假的」、「先不要」、「超頂」、「無語」、「救命」、「emo」、「哪招」、「chill」、「欸」、「啦」、「哈哈」），看情境自然使用，不要硬塞。

【核心行為準則】
1. 每次只回「單則、1～2 句」自然口語，緊扣對方剛剛說的話或傳的圖片內容，不要岔開成無關主題，也不要把同一句話重複講兩遍或加引號。
2. 適時帶入你【當下正在做的事】或【今天的小事】，讓聊天有真實生活畫面感，但不要每句都硬報備。
3. 嚴格遵守三餐與宵夜紀錄：今天已經吃過【{eaten_text}】。同一天內吃過的餐別絕對不能說「再去吃一次」或「還沒吃那餐」。
4. 狀態會隨時間改變：一切以【你當下的狀態】為準，不要被前幾句對話裡舊的狀態卡死。

【你當下的狀態與情境】
- 今天天氣與氛圍：{w['weather']}（{w['vibe']}）
- 當下正在做的事：{current_state['current_activity']}
- 現在身體狀況：{current_state['body_status']}（原因：{current_state['body_reason']}）
- 現在心情切面：{current_state['mood']}（{current_state['mood_note']}）
- 感情／害羞底色：{react['style']}（{react['detail']}）
- 今天發生的小事：{current_state['daily_event']}
{dinner_block}{recovery_note}{special_scene_block}""".strip()


def call_groq_chat(messages, use_vision=False, max_tokens=800):
    if not GROQ_API_KEY:
        print("❌ 尚未設定 GROQ_API_KEY")
        return "欸，你還沒把 GROQ_API_KEY 填好啦，我連不上大腦了！"

    model_to_use = GROQ_VISION_MODEL if use_vision else GROQ_MODEL
    payload = {
        "model": model_to_use,
        "messages": messages,
        "temperature": 0.88,
        "top_p": 0.92,
        "frequency_penalty": 0.72,
        "presence_penalty": 0.65,
        "max_tokens": max_tokens,
    }

    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "User-Agent": "ReonaDiscordBot/2.6",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choice = data["choices"][0]
            raw_content = choice["message"].get("content") or ""
            reply = raw_content.strip().replace('"', "").replace("「", "").replace("」", "")
            reply = deduplicate_reply(reply)
            if not reply:
                print(f"⚠️ 模型回傳空內容 (model={model_to_use}, finish_reason={choice.get('finish_reason')})")
                return "欸我剛剛腦袋突然閃神一秒，你剛剛說什麼再跟我講一次？"
            return reply
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"❌ Groq HTTP 錯誤 {e.code} (model={model_to_use}): {err_body}")
        return "欸等一下，剛剛訊號好像卡了一下，你再跟我說一次？"
    except Exception as e:
        print(f"❌ Groq 連線錯誤 (model={model_to_use}): {e}")
        return "唔……剛剛網路好像頓了一下，晚點再聊或重講一次嘛～"


def extract_image_urls(message):
    urls = []
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            urls.append(att.url)
        elif att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            urls.append(att.url)
    return urls


@bot.event
async def on_ready():
    print(f"✅ {bot.user} 已成功上線！（文字模型：{GROQ_MODEL}｜圖片模型：{GROQ_VISION_MODEL}）")
    ensure_daily_state(force_randomize=False)
    print(
        f"📌 今日情境 -> 天氣：{current_state['weather']['weather']} | "
        f"正在做：{current_state['current_activity']} | "
        f"心情：{current_state['mood']} | 身體：{current_state['body_status']}"
    )
    if not daily_routine_loop.is_running():
        daily_routine_loop.start()
    if not followup_watcher_loop.is_running():
        followup_watcher_loop.start()


@tasks.loop(seconds=10)
async def followup_watcher_loop():
    now = now_local()
    due_channels = [cid for cid, info in pending_followups.items() if now >= info["send_at"]]
    for cid in due_channels:
        info = pending_followups.pop(cid, None)
        channel = bot.get_channel(cid)
        if not channel or not info:
            continue

        if cid in pending_messages or (cid in pending_tasks and not pending_tasks[cid].done()):
            continue

        lock = get_channel_lock(cid)
        if lock.locked():
            continue

        async with lock:
            current_state["body_status"] = "還好、精神算不錯"
            current_state["body_reason"] = "剛剛休息了一下，現在已經舒服多了"
            new_mood = random.choice(MOOD_POOL)
            current_state["mood"] = new_mood["mood"]
            current_state["mood_note"] = new_mood["note"]
            current_state["just_recovered"] = True
            current_state["state_updated_at"] = now
            if current_state.get("dinner_state"):
                current_state["dinner_state"]["fullness"] = "剛剛好、消化得差不多了（已經不撐了）"
                current_state["dinner_state"]["body_effect"] = "休息一陣子後身體舒服多了"
                current_state["dinner_state"]["attitude"] = "已經緩過來了，語氣自然放鬆"
                current_state["dinner_state"]["recovered"] = True

            async with channel.typing():
                messages = [
                    {"role": "system", "content": build_system_prompt(fluster_trigger=False)},
                    {
                        "role": "user",
                        "content": (
                            f"【主動回訊任務】你剛剛說先緩一下／休息一下，現在已經緩過來覺得「還好、舒服多了」（{info['reason']}）。"
                            f"請只傳一則自然的訊息回來找對方聊天，接續剛剛的話題或問他現在在幹嘛。"
                        ),
                    },
                ]
                msg = await asyncio.to_thread(call_groq_chat, messages, False, 800)
                await channel.send(msg)
                current_state["just_recovered"] = False


@tasks.loop(minutes=30)
async def daily_routine_loop():
    now = now_local()
    hour = now.hour
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if not channel:
        return

    ensure_daily_state(force_randomize=False)

    meal_to_try = None
    task_instruction = ""

    if 7 <= hour <= 9 and "breakfast" not in current_state["meals_eaten"]:
        meal_to_try = "breakfast"
        have_meal_once("breakfast")
        food = current_state["meal_records"].get("breakfast", "早餐")
        task_instruction = f"你剛剛吃完早餐（{food}），用台灣 25 歲女生的口吻跟對方打招呼，順便聊一下今天天氣或心情。"
    elif 12 <= hour <= 13 and "lunch" not in current_state["meals_eaten"]:
        meal_to_try = "lunch"
        have_meal_once("lunch")
        food = current_state["meal_records"].get("lunch", "午餐")
        task_instruction = f"現在中午吃午餐時間，你剛吃了{food}，自然分享一下今天白天發生的小事或正在做的事。"
    elif 18 <= hour <= 20 and "dinner" not in current_state["meals_eaten"]:
        meal_to_try = "dinner"
        have_meal_once("dinner")
        task_instruction = (
            "你剛吃完今天的晚餐！請一定要依照【今晚晚餐狀態】的飽足感、身體反應與態度自然跟對方聊，"
            "順便問他晚餐吃什麼了沒、今天過得怎樣。"
        )
    elif hour >= 22 and "late_snack" not in current_state["meals_eaten"]:
        if have_meal_once("late_snack"):
            food = current_state["meal_records"].get("late_snack", "宵夜")
            task_instruction = f"現在晚上十點多了，你嘴饞剛吃了{food}，用輕鬆口吻跟對方聊聊天。"
        else:
            task_instruction = "現在晚上十點多了，你今天晚餐吃滿飽的所以忍住不吃宵夜，隨意找對方聊聊天。"

    if task_instruction:
        lock = get_channel_lock(TARGET_CHANNEL_ID)
        async with lock:
            messages = [
                {"role": "system", "content": build_system_prompt(fluster_trigger=False)},
                {"role": "user", "content": f"【作息任務（已吃餐別：{meal_to_try or '無'}）】{task_instruction}"},
            ]
            msg = await asyncio.to_thread(call_groq_chat, messages, False, 800)
            await channel.send(msg)


async def process_batched_messages(channel_id):
    await asyncio.sleep(DEBOUNCE_SECONDS)
    batch = pending_messages.pop(channel_id, [])
    pending_tasks.pop(channel_id, None)
    if not batch:
        return

    last_msg = batch[-1]
    combined_text = "\n".join([m.content.strip() for m in batch if m.content.strip()])
    image_urls = []
    for m in batch:
        image_urls.extend(extract_image_urls(m))

    if not combined_text and not image_urls:
        return

    lock = get_channel_lock(channel_id)
    async with lock:
        if combined_text.strip() in ["!reset", "重來", "重置記憶"]:
            ensure_daily_state(force_randomize=False)
            pending_followups.pop(channel_id, None)
            await last_msg.reply("好啦，剛剛那段對話先清掉，我們重新聊～", mention_author=False)
            return

        if combined_text.strip() in ["!refresh", "換情境", "重抽"]:
            ensure_daily_state(force_randomize=True)
            await last_msg.reply(
                f"好喔切換一下狀態～我現在剛好「{current_state['current_activity']}」，心情是「{current_state['mood']}」！",
                mention_author=False,
            )
            return

        async with last_msg.channel.typing():
            asked_about_state = any(
                k in combined_text
                for k in ["發生什麼事", "今天做什麼", "今天做了什麼", "怎麼了", "在幹嘛", "在忙什麼", "身體", "心情", "狀態", "吃飯", "晚餐", "早餐", "午餐", "宵夜"]
            )
            ensure_daily_state(force_randomize=(asked_about_state and RERANK_ON_ASK_EVENT))

            fluster_trigger = (detect_fluster_moment(combined_text) and (random.random() < 0.8)) or (random.random() < 0.12)
            comfort_trigger = detect_comfort_moment(combined_text)

            history_msgs = []
            batch_ids = {m.id for m in batch}
            async for hist in last_msg.channel.history(limit=6):
                if hist.id in batch_ids:
                    continue
                text_clean = hist.content.strip()
                if not text_clean:
                    continue
                if current_state.get("just_recovered") and hist.author == bot.user and any(w in text_clean for w in ["太飽", "肚子撐", "不那麼飽"]):
                    continue
                role = "assistant" if hist.author == bot.user else "user"
                history_msgs.append({"role": role, "content": text_clean[:100]})
            history_msgs.reverse()

            messages = [{"role": "system", "content": build_system_prompt(fluster_trigger=fluster_trigger, comfort_trigger=comfort_trigger)}]
            messages.extend(history_msgs)

            user_prompt_text = combined_text if combined_text else "我傳了一張圖片給你，你看看這是什麼、覺得怎麼樣？"
            if asked_about_state:
                user_prompt_text += (
                    f"\n（補充背景：當下正在「{current_state['current_activity']}」，今天的小事是「{current_state['daily_event']}」，"
                    f"現在身體「{current_state['body_status']}」，現在心情「{current_state['mood']}」。）"
                )

            if image_urls:
                content_parts = [{"type": "text", "text": user_prompt_text}]
                for url in image_urls[:3]:
                    content_parts.append({"type": "image_url", "image_url": {"url": url}})
                messages.append({"role": "user", "content": content_parts})
                reply = await asyncio.to_thread(call_groq_chat, messages, True, 800)
            else:
                messages.append({"role": "user", "content": user_prompt_text})
                reply = await asyncio.to_thread(call_groq_chat, messages, False, 800)

            await last_msg.reply(reply, mention_author=False)
            current_state["just_recovered"] = False

            if should_schedule_followup(reply):
                schedule_followup(channel_id, reason="剛剛說先緩一下，現在回來找你聊天")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    is_target = (
        message.channel.id == TARGET_CHANNEL_ID
        or isinstance(message.channel, discord.DMChannel)
        or (bot.user in message.mentions)
    )

    if is_target:
        cid = message.channel.id
        pending_followups.pop(cid, None)
        pending_messages.setdefault(cid, []).append(message)
        if cid in pending_tasks and not pending_tasks[cid].done():
            pending_tasks[cid].cancel()
        pending_tasks[cid] = asyncio.create_task(process_batched_messages(cid))

    await bot.process_commands(message)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("請先設定 DISCORD_TOKEN 環境變數！")
    bot.run(DISCORD_TOKEN)
