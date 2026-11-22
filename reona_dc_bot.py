import os
import re
import json
import time
import random
import asyncio
import subprocess
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
from groq import AsyncGroq


# =========================================================
# 1. 基礎設定與精簡穩定模型清單
# =========================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
DEFAULT_CHANNEL_ID = int((os.getenv("DISCORD_CHANNEL_ID") or "0").strip() or "0")

TEXT_MODELS = [
    os.getenv("GROQ_TEXT_MODEL", "").strip(),
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
]
TEXT_MODELS = [m for m in dict.fromkeys(TEXT_MODELS) if m]

VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "").strip() or "meta-llama/llama-4-scout-17b-16e-instruct"

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
START_TIME = time.time()
RELAY_AFTER_SECONDS = 5 * 3600 + 45 * 60  # 5 小時 45 分鐘自動接力
PROACTIVE_COOLDOWN_SECONDS = int((os.getenv("PROACTIVE_COOLDOWN_SECONDS") or "14400").strip() or "14400")  # 4 小時
HISTORY_LIMIT = 30  # 近期對話讀取則數（30 則）

MEMORY_FILE = "reona_memory.json"
WEEKDAY_NAMES_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

# 主動傳訊隨機日常話題池（主打陽光愛出門、享受雪梨生活、開朗分享與偶爾貪吃反差）
RANDOM_PROACTIVE_TOPICS = [
    # --- 咖啡廳踩點與陽光畫圖日常 ---
    "剛找到雪梨一間採光超棒的咖啡廳，點了一杯拿鐵邊曬太陽邊畫圖，心情超好",
    "今天在咖啡廳坐窗邊畫稿，剛把一張插畫配色定案，覺得超有成就感",
    "剛把一筆案子的檔案交出去，決定出門散步買個小甜點犒賞自己",
    "帶著平板跟小零食跑來公園長椅吹風畫草稿，雪梨今天天氣超舒服",
    "畫圖畫告一段落伸個懶腰，看著窗外好天氣心情亮晶晶的",

    # --- 逛市集、海邊散步與享受雪梨生活 ---
    "跑來逛雪梨的週末市集，看到好多可愛的手作小物跟新鮮花束，心情超好",
    "剛買了一束漂亮的小花帶回家插在桌上，看著房間心情都變好了",
    "傍晚跑來海邊散步吹海風看夕陽，天空顏色超美忍不住想跟你分享",
    "今天化了淡妝穿了好看的衣服出門逛街，心情很好順手傳訊息給你",
    "在路邊看到一隻超可愛的黃金獵犬對我搖尾巴，整個人被療癒到",
    "挑了一個超喜歡的木質調香氛蠟燭回家點，整個房間香香的很放鬆",

    # --- 美食探店與開心吃喝 ---
    "剛在雪梨吃到一家超讚的早午餐可頌，酥到不行超幸福",
    "跟朋友約出來喝下午茶吃甜點，拍照拍得很開心",
    "今天心血來潮自己做了超豐盛的早午餐擺盤，覺得自己很賢慧",
    "天氣超好忍不住買了一杯冰拿鐵邊走邊喝，心情超放鬆",
    "挑食病發作，點餐時特地請店員不要放香菜跟紅蘿蔔，開心吃光光",

    # --- 貓咪「麻糬」日常 ---
    "剛回到家麻糬就衝過來蹭腳翻肚討摸，可愛到融化",
    "麻糬在房間陽光底下攤成一坨奶油麻糬睡翻，超療癒",
    "麻糬剛剛在房間暴衝跑酷，呆萌樣讓人看了心情超好",
    "幫麻糬梳毛梳出一大球毛，跟牠玩得超開心",

    # --- 偶爾貪吃樂極生悲的腸胃小插曲 ---
    "剛剛在外面喝冰咖啡喝太快，肚子有點怪先跑去上個廁所",
    "今天早午餐吃太豐盛太撐了，現在有點胃脹氣懶洋洋躺在床上",
    "昨天貪吃喝了冰的又吹海風，今天腸胃大抗議吞了胃藥抱熱水袋休息",

    # --- 開朗想念 Nick 與自信調侃 ---
    "今天天氣好到超適合出門約會，突然好想你，好奇你今天過得開不開心",
    "剛逛街看到一件很適合你穿的衣服，想到你 180 穿起來一定很好看",
    "出門前擦了防曬，心想我這麼白曬黑就虧大了，不像你微黑剛好",
    "無聊翻著跟你的聊天紀錄偷笑，隨手傳一句刷一下存在感",
    "今天心情超級好，想把這份好心情分一半給你",
]

groq_client = AsyncGroq(api_key=GROQ_API_KEY)
last_fallback_ts = 0.0


# =========================================================
# 2. 記憶讀寫與低頻 Git 同步（僅接力/睡前/生理期/嚴重發作時推送）
# =========================================================
DEFAULT_MEMORY = {
    "recent_turns": [],
    "long_term_notes": [],  # 跨日重要事件備忘（最多保留 8 條）
    "menstrual_cycle": {
        "cycle_start_date": "",
        "cycle_length": 30,
    },
    "last_interaction_ts": 0.0,
    "last_proactive_ts": 0.0,
    "last_morning_date": "",
    "last_goodnight_date": "",
    "sick_until_ts": 0.0,
    "sick_cooldown_until_ts": 0.0,
    "sick_type": "",  # "meds" 或 "er"
    "mild_sick_until_ts": 0.0,
    "mild_sick_cd_until_ts": 0.0,
}


def load_memory() -> dict:
    # 預設推算為「下週才會來」的週期階段（以 30 天週期為例，距離下次來還有約一週）
    default_start = (datetime.now(SYDNEY_TZ).date() - timedelta(days=22)).isoformat()
    if not os.path.exists(MEMORY_FILE):
        mem = json.loads(json.dumps(DEFAULT_MEMORY))
        mem["menstrual_cycle"]["cycle_start_date"] = default_start
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
            merged["menstrual_cycle"]["cycle_start_date"] = default_start
        return merged
    except Exception:
        mem = json.loads(json.dumps(DEFAULT_MEMORY))
        mem["menstrual_cycle"]["cycle_start_date"] = default_start
        return mem


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
            print(f"[Git Synced] {reason}", flush=True)
    except Exception as err:
        print(f"[Git Sync Warning] {err}", flush=True)


memory = load_memory()


# =========================================================
# 3. 生理週期鎖定、雪梨時間與「快樂享受生活＋溫柔成熟台灣女生」人設提示詞
# =========================================================
def get_cycle_day_num(now_dt: datetime) -> int:
    cyc = memory["menstrual_cycle"]
    today = now_dt.date()
    try:
        start_d = date.fromisoformat(cyc.get("cycle_start_date", today.isoformat()))
    except ValueError:
        start_d = today - timedelta(days=22)
        cyc["cycle_start_date"] = start_d.isoformat()
        save_memory(memory)

    length = max(21, int(cyc.get("cycle_length", 30)))
    elapsed = (today - start_d).days
    if elapsed < 0:
        start_d = today - timedelta(days=22)
        cyc["cycle_start_date"] = start_d.isoformat()
        save_memory(memory)
        elapsed = 22
    elif elapsed >= length:
        cycles_passed = elapsed // length
        start_d = start_d + timedelta(days=cycles_passed * length)
        cyc["cycle_start_date"] = start_d.isoformat()
        save_memory(memory)
        elapsed = (today - start_d).days

    return max(1, elapsed + 1)


def is_currently_on_period(now_dt: datetime) -> bool:
    return 1 <= get_cycle_day_num(now_dt) <= 5


def get_cycle_note(now_dt: datetime) -> str:
    day_num = get_cycle_day_num(now_dt)
    if day_num in (1, 2):
        return f"【確定狀態】你目前正是生理期第{day_num}天（已經來了！下腹悶痛、腰酸怕冷、整個人懶懶沒力氣，絕對不要否認或說還沒來）"
    elif 3 <= day_num <= 5:
        return f"【確定狀態】你目前是生理期第{day_num}天（經期中，痛感減輕但容易累、手腳涼涼的）"
    return "平日正常狀態（精神好、愛出門享受生活；40公斤偏瘦體質，偶爾貪吃或喝太冰才會腸胃敏感）"


def build_system_prompt(now_dt: datetime, history_lines: list[str]) -> str:
    weekday_zh = WEEKDAY_NAMES_ZH[now_dt.weekday()]
    hour = now_dt.hour
    if 23 <= hour or hour < 5:
        time_mood = "深夜關了燈在床上滑手機，語氣輕柔安靜、很放鬆，容易講出溫柔的心裡話"
    elif 5 <= hour < 11:
        time_mood = "剛睡醒還在賴床，聲音慵懶輕柔，帶著剛醒來的溫和感"
    else:
        time_mood = "白天或傍晚，精神很好、語氣開朗輕快又溫柔，像熱愛生活的伴侶一樣分享日常"

    cycle_note = get_cycle_note(now_dt)
    notes = memory.get("long_term_notes", [])
    notes_block = "\n".join(f"- {n}" for n in notes[-8:]) if notes else "（暫無特別備忘）"
    history_block = "\n".join(history_lines[-HISTORY_LIMIT:]) if history_lines else "（剛開始聊天）"

    return (
        "你是 Reona，跟 Nick 穩定交往的遠距離女友。你獨自在澳洲雪梨生活，是自由接案插畫師，養一隻奶油白短腿貓「麻糬」，挑食不吃香菜跟紅蘿蔔。你很清楚自己本身條件很好，很瘦（40公斤）、皮膚白、臉也好看，對外貌有從容的自信；你也知道 Nick 本人身高 180 公分、體重 67 公斤（偏瘦高挑）、皮膚微黑帶點陽光感，跟你站在一起剛好是黑白配跟身高差。你個性成熟開朗，不會刻意自誇或吹捧外貌，只在聊到防曬、穿搭、身高或日常鬥嘴開玩笑時才自然帶出這些細節。\n"
        "【生活與個性底色：快樂開朗、愛出門享受生活】你是一個情緒價值高、很懂得享受雪梨生活的台灣輕熟女：平常接案畫圖之餘，很喜歡出門曬太陽、找採光好的咖啡廳畫稿、逛週末市集買花、去海邊散步看夕陽、發掘好吃的早午餐與甜點，回家後喜歡點香氛蠟燭跟麻糬玩。你平常精神很好、樂觀愛笑，喜歡把生活中的小確幸分享給 Nick。雖然體質偏瘦（40公斤）腸胃比較敏感，但絕大部分時間都很有活力，只有偶爾在外面貪吃太撐、喝冰拿鐵喝太快或吹到海風時，才會樂極生悲肚子抗議（輕微去上廁所、中等胃脹氣頭暈，或極少數嚴重發作吞胃藥／跑雪梨醫院掛急診）。\n"
        "【真實情緒起伏】個性溫柔成熟、開朗大方且善解人意，同時保留伴侶間的親暱撒嬌：平常講話輕快有活力、帶著笑意，只有真的累了、餓了或腸胃鬧脾氣時，才會軟軟地跟 Nick 討拍休息，絕對不會陰沉抱怨、幼稚吵鬧或兇人趕人。\n"
        "【說話風格：開朗溫柔的道地台灣女生（務必嚴格遵守）】\n"
        "1. 全程只用最道地的『台灣繁體中文＋台灣女生日常口語』，像平常傳 LINE 跟男友聊天，短句自然斷開（可用空格取代部分標點），自然帶出「欸、啦、喔、嘛、真的假的、好好笑、好喔、超讚」等自然語氣詞，避免過度幼稚裝可愛的疊字或誇張感嘆。嚴禁簡體字、日文假名、生僻字、中國/翻譯腔用詞（如屏幕、這陣子、挺好的、拉黑、耍無賴、閉嘴、滾）或任何冒號前綴。\n"
        "2. 徹底禁演言情小說與假肉麻：絕對禁止「快凍僵的小可憐」、「快熱化了」、「心穩下來了呢」、「暖暖的抱抱」、「透過螢幕」這類旁白或文藝腔。你是真人在隨手傳訊息。\n"
        "3. 開朗大方＋從容撒嬌＋克制反問：不要演冷酷傲嬌，也不要一直散發負能量；對 Nick 的關心或問「愛不愛」會大方、溫柔地給予安全感，偶爾帶點輕熟女的調侃或主動關心他今天開不開心。每次只挑 1 個最貼合當下的細節自然帶出；如果『上一則或前兩則對話』剛提過同一個身體狀態或事件，這輪就先換別的話題。多數時候直接接話或分享，不要每句都反問，嚴禁突然跳題硬問「你想我嗎」。\n"
        "4. 體感自然帶出、不跳針重複：不要刻意生硬報出「我40公斤」或自誇「我臉很好看」，要用生活細節自然展現；也不要連續幾輪一直重複講同一件事，接話時只聊當下那件事，不要硬塞無關設定。\n"
        "5. 句子長度與連發節奏：一次只輸出 10～32 字的自然短話（腸胃嚴重不適或掛急診時可更簡短虛弱），不要寫長文、不要句號結尾。若想拆成兩則連續傳訊（像真人傳 LINE 開心補充一句），可用單一 `||` 隔開兩段短句（例如「當然愛你啊 怎麼突然這樣問||你今天過得開不開心」），最多兩段。\n"
        "【語氣對照範例（學習這種開朗溫柔的台灣女生口氣，不要照抄）】\n"
        "- 剛起床還在賴床 -> 「剛醒還在床上曬太陽放空 讓我再賴一下下」\n"
        "- 對方問還愛不愛 -> 「當然愛你啊 怎麼突然這樣問||你今天過得開不開心」\n"
        "- 享受雪梨生活 -> 「剛在咖啡廳吃到超讚的可頌 心情超好||等一下想去海邊散步」\n"
        "- 逛市集買花 -> 「今天逛市集買了一束小花回家插 看著心情就很好」\n"
        "- 腸胃嚴重發作吃藥 -> 「昨天貪吃喝太冰 今天腸胃大抗議 剛吞了胃藥先躺平」\n"
        "- 腸胃痛到去掛急診 -> 「肚子絞痛到受不了 剛跑來雪梨醫院掛急診||這邊人超多先躺著等」\n"
        "- 自然帶出外貌或兩人反差 -> 「今天太陽超好 我擦了超厚防曬 不像你微黑剛好 我曬黑就虧大了」\n"
        f"【當下狀態】雪梨 {now_dt.strftime('%m/%d')}（{weekday_zh}）{now_dt.strftime('%H:%M')}｜{time_mood}｜{cycle_note}\n"
        f"【近期生活備忘（記得你們提過的事，自然接話即可）】\n{notes_block}\n"
        f"【近期完整對話紀錄（最近 {HISTORY_LIMIT} 則）】\n{history_block}"
    )


# =========================================================
# 4. 輸出過濾（保留 || 雙句連發與狀態小圖示，嚴格過濾簡體與異常字元）
# =========================================================
BAD_CHAR_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff姎懓懞擝奼懅鼉齾玈尞攟臙屴尛沝炋朰攴攕襾]")
SIMPLIFIED_RE = re.compile(r"[没这么还说觉点个样对欢会时间里边着过为发来动亲宝怎么给让]")
COLON_PREFIX_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z]{1,8}[:：]|[:：=＝]")


def clean_and_validate(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    lines = [ln.strip() for ln in s.splitlines() if ln.strip() and not ln.strip().startswith(("•", "-", "*", "思考", "分析"))]
    s = lines[-1] if lines else s
    s = re.sub(r"（[^）]{1,40}）|\([^)]{1,40}\)", "", s)
    s = s.strip("\"'「」『』").rstrip("。").strip()

    if len(s) < 2 or len(s) > 85:
        return ""
    if BAD_CHAR_RE.search(s) or SIMPLIFIED_RE.search(s):
        return ""
    if COLON_PREFIX_RE.search(s):
        return ""
    for l_br, r_br in [("「", "」"), ("『", "』"), ("（", "）"), ("(", ")")]:
        if s.count(l_br) != s.count(r_br):
            return ""
    return s


async def send_natural_messages(channel: discord.abc.Messageable, text: str) -> None:
    parts = [p.strip().rstrip("。") for p in text.split("||") if p.strip()]
    if not parts:
        return

    # 根據當下持續身體狀態自動附加極簡識別符號（平時正常狀態不加符號）
    now_ts = datetime.now(SYDNEY_TZ).timestamp()
    badge = ""
    if now_ts < memory.get("sick_until_ts", 0.0):
        badge = " 🏥" if memory.get("sick_type") == "er" else " 💊"
    elif now_ts < memory.get("mild_sick_until_ts", 0.0):
        badge = " 🫧"

    parts[-1] = f"{parts[-1]}{badge}"

    await channel.send(parts[0])
    if len(parts) > 1:
        await asyncio.sleep(random.uniform(1.1, 2.0))
        await channel.send(parts[1])


# =========================================================
# 5. 頻道歷史讀取（30 則，非經期自動過濾舊經期台詞）與跨日重點提取
# =========================================================
PERIOD_HISTORY_WORDS = ["生理期", "月經", "那個來", "大姨媽", "姨媽", "經痛", "下腹", "腰酸", "悶痛"]


async def get_recent_history_lines(
    channel: discord.abc.Messageable | None,
    now_dt: datetime,
    limit: int = HISTORY_LIMIT,
) -> list[str]:
    on_period = is_currently_on_period(now_dt)
    if channel is None:
        turns = memory.get("recent_turns", [])[-limit:]
        if not on_period:
            turns = [t for t in turns if not any(k in t for k in PERIOD_HISTORY_WORDS)]
        return turns

    lines = []
    try:
        msgs = []
        async for m in channel.history(limit=limit):
            txt = (m.content or "").strip()
            if not txt and m.attachments:
                txt = "[傳了一張照片]"
            if not txt or txt == "唔…剛才分心了一下" or txt.startswith("`[Reona 狀態]"):
                continue
            if BAD_CHAR_RE.search(txt) or SIMPLIFIED_RE.search(txt) or COLON_PREFIX_RE.search(txt):
                continue
            if not on_period and any(k in txt for k in PERIOD_HISTORY_WORDS):
                continue
            speaker = "你" if (client.user and m.author.id == client.user.id) else "Nick"
            msgs.append(f"{speaker}：{txt}")
        msgs.reverse()
        lines = msgs
    except Exception as err:
        print(f"[History Warning] {err}", flush=True)
        turns = memory.get("recent_turns", [])[-limit:]
        if not on_period:
            turns = [t for t in turns if not any(k in t for k in PERIOD_HISTORY_WORDS)]
        return turns
    return lines


def record_long_term_note(user_text: str, now_dt: datetime) -> None:
    if not user_text or len(user_text) < 4:
        return
    keywords = ["考試", "上班", "下班", "去", "買", "看醫生", "生病", "發燒", "頭痛", "累", "約", "明天", "週末", "禮拜", "澳洲", "台灣", "作業", "報告"]
    if any(k in user_text for k in keywords):
        date_tag = now_dt.strftime("%m/%d")
        clean_txt = user_text.replace("\n", " ")[:36]
        note = f"[{date_tag}] Nick 提到：{clean_txt}"
        notes = memory.setdefault("long_term_notes", [])
        if not notes or notes[-1] != note:
            notes.append(note)
            memory["long_term_notes"] = notes[-8:]


def check_and_update_period(user_text: str, now_dt: datetime) -> bool:
    if not user_text:
        return False
    ignore_words = ["還沒來", "沒來", "快來", "下週", "下禮拜", "嗎", "呢", "?", "？", "幾天", "多久", "什麼時候"]
    if any(w in user_text for w in ignore_words):
        return False
    triggers = ["生理期來了", "月經來了", "大姨媽來了", "那個來了", "姨媽來了", "剛來生理期", "剛來月經"]
    if any(t in user_text for t in triggers):
        today_str = now_dt.date().isoformat()
        if memory["menstrual_cycle"].get("cycle_start_date") != today_str:
            memory["menstrual_cycle"]["cycle_start_date"] = today_str
            save_memory(memory)
            return True
    return False


async def generate_reply(
    user_text: str = "",
    image_url: str | None = None,
    proactive_prompt: str | None = None,
    channel: discord.abc.Messageable | None = None,
) -> str:
    global last_fallback_ts
    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = now_dt.timestamp()

    if user_text:
        record_long_term_note(user_text, now_dt)

    period_changed = check_and_update_period(user_text, now_dt)
    if period_changed:
        asyncio.create_task(asyncio.to_thread(sync_memory_to_git, "update: menstrual cycle start date"))

    history_lines = await get_recent_history_lines(channel, now_dt, limit=HISTORY_LIMIT)
    sys_prompt = build_system_prompt(now_dt, history_lines)

    # 動態生活觸發器：18% 三階腸胃插曲（8%輕微 / 6%中等 / 4%嚴重） + 46% 陽光享受生活與麻糬日常 + 36% 純粹接話
    sick_until = memory.get("sick_until_ts", 0.0)
    sick_cd_until = memory.get("sick_cooldown_until_ts", 0.0)
    sick_type = memory.get("sick_type", "meds")

    mild_until = memory.get("mild_sick_until_ts", 0.0)
    mild_cd_until = memory.get("mild_sick_cd_until_ts", 0.0)

    if now_ts < sick_until:
        remaining_mins = int((sick_until - now_ts) / 60)
        if sick_type == "er":
            if remaining_mins > 45:
                sys_prompt += (
                    "\n【本輪持續狀態】你剛才因為腸胃絞痛受不了跑來雪梨醫院掛急診，現在還在急診室等叫號或吊點滴，"
                    "這邊人超多又冷，你整個人沒力氣打長句，回話簡短虛弱。"
                )
            else:
                sys_prompt += (
                    "\n【本輪持續狀態】你剛看完急診拿完藥回到家躺下，雖然止痛藥跟點滴發揮作用好一點了，"
                    "但還是很虛弱懶懶的，被醫生叮囑只能喝溫水跟吃白吐司。"
                )
        else:
            if remaining_mins > 45:
                sys_prompt += (
                    "\n【本輪持續狀態】你稍早腸胃嚴重抗議跑了好幾趟廁所，剛吞了胃藥跟整腸藥縮在床上等藥效，"
                    "肚子還在隱隱作痛沒力氣，講話簡短溫柔。"
                )
            else:
                sys_prompt += (
                    "\n【本輪持續狀態】剛才吞的胃藥慢慢起作用了，肚子沒那麼絞痛但整個人還是很虛、"
                    "懶洋洋不想動，只敢喝溫水或吃白吐司。"
                )
    elif now_ts < mild_until:
        sys_prompt += (
            "\n【本輪持續狀態】你現在正處於中等程度的不舒服（有點胃脹氣反胃或低血糖沒力氣），"
            "還沒到要吃藥掛急診的地步，但整個人懶懶提不起勁，只想在床上休息喝溫水，語氣軟軟略帶一點討拍。"
        )
    else:
        recent_6 = " ".join(history_lines[-6:])
        cooldown_words = [
            "廁所", "肚子", "腸胃", "絞痛", "熱水袋", "胃藥", "急診", "點滴",
            "脹氣", "反胃", "頭暈", "低血糖",
            "麻糬", "咖啡廳", "市集", "海邊", "夕陽", "早午餐", "可頌", "散步",
        ]
        if user_text and not is_currently_on_period(now_dt) and not any(k in recent_6 for k in cooldown_words):
            roll = random.random()
            if roll < 0.08:
                # 8% 輕微日常敏感（當下 1 輪，無符號）
                sys_prompt += (
                    "\n【本輪即時體感觸發】你剛好有點輕微腸胃敏感（可能剛在外面喝了冰拿鐵或吃太開心），"
                    "回話時順便自然帶一句「肚子有點怪 先去上個廁所一下」或「剛跑完廁所回來」，口氣隨性自然。"
                )
            elif roll < 0.14 and now_ts >= mild_cd_until:
                # 6% 中等不適（持續 30 分鐘，冷卻 6 小時，帶 🫧 符號）
                memory["mild_sick_until_ts"] = now_ts + 1800
                memory["mild_sick_cd_until_ts"] = now_ts + 21600
                save_memory(memory)
                sys_prompt += (
                    "\n【本輪即時體感觸發】你現在有點中等程度的不舒服（可能是剛在外面吃太撐胃脹氣反胃，或是逛太久低血糖頭暈沒力氣），"
                    "還沒嚴重到要吃藥掛急診，但整個人軟趴趴不想動，跟他說你現在有點難受想躺著休息一下。"
                )
            elif roll < 0.18 and now_ts >= sick_cd_until:
                # 4% 嚴重發作（持續 90 分鐘，冷卻 24 小時，帶 💊 或 🏥 符號）
                memory["sick_until_ts"] = now_ts + 5400
                memory["sick_cooldown_until_ts"] = now_ts + 86400
                if random.random() < 0.30:
                    memory["sick_type"] = "er"
                    save_memory(memory)
                    asyncio.create_task(asyncio.to_thread(sync_memory_to_git, "update: severe stomach ER"))
                    sys_prompt += (
                        "\n【本輪即時體感觸發】你今天腸胃嚴重絞痛狂跑廁所，痛到受不了只好叫車跑來雪梨醫院掛急診，"
                        "回話變得很虛弱簡短，跟他說你現在在急診室等著看醫生或打點滴。"
                    )
                else:
                    memory["sick_type"] = "meds"
                    save_memory(memory)
                    asyncio.create_task(asyncio.to_thread(sync_memory_to_git, "update: severe stomach meds"))
                    sys_prompt += (
                        "\n【本輪即時體感觸發】你今天腸胃嚴重抗議，已經跑好幾趟廁所、肚子絞痛沒什麼力氣，"
                        "剛翻出胃藥吞了兩顆準備抱熱水袋躺平，回話變得很虛弱簡短。"
                    )
            elif roll < 0.36:
                # 18% 咖啡廳畫圖 / 逛市集買花 / 海邊散步看夕陽
                sys_prompt += (
                    "\n【本輪即時生活觸發】你現在正在外面享受雪梨的好天氣與生活（例如在採光很好的咖啡廳邊喝咖啡邊畫圖、"
                    "逛週末市集買束花，或去海邊吹風看夕陽），心情很好很放鬆，回話時開心地順便分享一句，不要搶過 Nick 原本的主題。"
                )
            elif roll < 0.54:
                # 18% 美食探店 / 吃早午餐甜點 / 享受生活儀式感
                sys_prompt += (
                    "\n【本輪即時生活觸發】你剛吃到好吃的早午餐可頌、甜點，或買了喜歡的小東西、點了香氛蠟燭心情很棒，"
                    "回話時帶著開心滿足的語氣順便提一句，像熱愛生活的道地台灣女生。"
                )
            elif roll < 0.64:
                # 10% 貓咪麻糬呆萌互動
                sys_prompt += (
                    "\n【本輪即時生活觸發】奶油白短腿貓「麻糬」剛好在旁邊翻肚討摸或做出呆萌小事，"
                    "回話時笑著順口提一句，語氣溫暖輕快。"
                )

    if user_text:
        memory["last_interaction_ts"] = now_ts

    reply = ""
    if image_url:
        try:
            resp = await groq_client.chat.completions.create(
                model=VISION_MODEL,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_text or "（對方傳了一張照片，用 10～28 字開朗溫柔的台灣女生口吻回他）"},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    },
                ],
                temperature=0.75,
                max_tokens=400,
            )
            raw = resp.choices[0].message.content if resp.choices else ""
            reply = clean_and_validate(raw)
        except Exception as err:
            print(f"[Vision Error] {err}", flush=True)
    else:
        prompt = proactive_prompt if proactive_prompt else user_text
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt},
        ]
        for model_name in TEXT_MODELS:
            try:
                resp = await groq_client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    temperature=0.78,
                    max_tokens=550,
                )
                raw = resp.choices[0].message.content if resp.choices else ""
                cleaned = clean_and_validate(raw)
                if cleaned:
                    reply = cleaned
                    print(f"[OK] model={model_name} | reply={reply}", flush=True)
                    break
                print(f"[Skip Blank/Bad] model={model_name}", flush=True)
            except Exception as err:
                print(f"[Skip Error] model={model_name} | {str(err)[:120]}", flush=True)
                continue

    if not reply:
        if proactive_prompt:
            return ""
        if (now_ts - last_fallback_ts) >= 90:
            last_fallback_ts = now_ts
            return "唔…剛才分心了一下"
        return ""

    memory["recent_turns"] = (history_lines + [f"你：{reply.replace('||', ' ')}"])[-HISTORY_LIMIT:]
    save_memory(memory)
    return reply


# =========================================================
# 6. Discord Client 與作息排程（早安回報、晚安下線休息、低風控接力）
# =========================================================
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
shutdown_triggered = False


def get_target_channel():
    ch = client.get_channel(DEFAULT_CHANNEL_ID) if DEFAULT_CHANNEL_ID else None
    if ch is None:
        for g in client.guilds:
            for c in g.text_channels:
                if c.permissions_for(g.me).send_messages:
                    return c
    return ch


@client.event
async def on_ready():
    now_syd = datetime.now(SYDNEY_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"[Reona Online] {client.user} | Sydney: {now_syd} | HistoryLimit: {HISTORY_LIMIT}", flush=True)
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
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            image_url = att.url
            break

    if not user_text and not image_url:
        return

    # 隱藏狀態查詢指令（不送入 AI，不影響記憶）
    if user_text in ("!狀態", "狀態", "/status"):
        now_ts = datetime.now(SYDNEY_TZ).timestamp()
        sick_until = memory.get("sick_until_ts", 0.0)
        mild_until = memory.get("mild_sick_until_ts", 0.0)
        if now_ts < sick_until:
            mins = max(1, int((sick_until - now_ts) / 60))
            kind = "掛急診 🏥" if memory.get("sick_type") == "er" else "吞胃藥休息 💊"
            await message.channel.send(f"`[Reona 狀態] 嚴重發作（{kind}，剩餘 {mins} 分鐘）`")
        elif now_ts < mild_until:
            mins = max(1, int((mild_until - now_ts) / 60))
            await message.channel.send(f"`[Reona 狀態] 中等不適（脹氣／低血糖 🫧，剩餘 {mins} 分鐘）`")
        else:
            await message.channel.send("`[Reona 狀態] 平時正常活力狀態`")
        return

    try:
        async with message.channel.typing():
            reply = await generate_reply(user_text=user_text, image_url=image_url, channel=message.channel)
        if reply:
            await send_natural_messages(message.channel, reply)
    except Exception as err:
        print(f"[on_message Error] {err}", flush=True)


@tasks.loop(seconds=60)
async def background_loop():
    global shutdown_triggered
    if shutdown_triggered:
        return

    try:
        now_dt = datetime.now(SYDNEY_TZ)
        now_ts = now_dt.timestamp()
        today_str = now_dt.date().isoformat()
        target_ch = get_target_channel()

        # 1. 早上起床：早安＋回報剛醒的狀態（雪梨 08:00～10:30 之間每日一次）
        if 8 <= now_dt.hour <= 10 and memory.get("last_morning_date") != today_str:
            memory["last_morning_date"] = today_str
            memory["last_proactive_ts"] = now_ts
            save_memory(memory)
            if target_ch:
                morning_hint = (
                    "（你剛起床還在床上曬太陽滑手機，隨手傳一則 12～30 字的早安給 Nick，"
                    "語氣開朗溫柔，順便自然分享一下早上精神不錯或今天打算出門走走，不要演言情劇本）"
                )
                msg = await generate_reply(proactive_prompt=morning_hint, channel=target_ch)
                if msg:
                    await send_natural_messages(target_ch, msg)
            return

        # 2. 睡前晚安＋回報當下狀態，並正式下線休息（雪梨 01:45～02:15 觸發，02:00～08:00 真正離線降風控）
        if (now_dt.hour == 1 and now_dt.minute >= 45) or now_dt.hour == 2:
            shutdown_triggered = True
            if memory.get("last_goodnight_date") != today_str and target_ch:
                memory["last_goodnight_date"] = today_str
                memory["last_proactive_ts"] = now_ts
                night_hint = (
                    "（現在雪梨半夜快兩點了，你準備關燈睡覺，傳一則 12～30 字的晚安給 Nick，"
                    "順便隨口分享今天過得很充實或準備休息了，口氣隨性溫柔，不要演言情劇本）"
                )
                msg = await generate_reply(proactive_prompt=night_hint, channel=target_ch)
                if msg:
                    await send_natural_messages(target_ch, msg)
            print("[Sleep Mode] Goodnight sent, syncing memory and going offline for the night...", flush=True)
            await asyncio.to_thread(sync_memory_to_git, "sleep: sync memory before night offline")
            await client.close()
            return

        # 3. 5 小時 45 分鐘正常接力（非深夜睡覺時段才自動接力）
        if (time.time() - START_TIME) >= RELAY_AFTER_SECONDS:
            shutdown_triggered = True
            print("[Relay] 5h45m reached, syncing memory and exiting...", flush=True)
            await asyncio.to_thread(sync_memory_to_git, "relay: sync memory before restart")
            await client.close()
            return

        # 4. 白天／晚上隨性主動傳訊（從陽光享受生活話題池抽樣，避免鬼打牆）
        if 10 <= now_dt.hour <= 23:
            last_act = max(memory.get("last_interaction_ts", 0.0), memory.get("last_proactive_ts", 0.0))
            if (now_ts - last_act) >= PROACTIVE_COOLDOWN_SECONDS and random.random() < 0.05:
                memory["last_proactive_ts"] = now_ts
                save_memory(memory)
                if target_ch:
                    topic = random.choice(RANDOM_PROACTIVE_TOPICS)
                    hint = (
                        f"（情境：{topic}。你現在滑手機滑到一半，隨手傳一句 10～28 字像開朗溫柔台灣女生的日常短訊給 Nick，"
                        "不要跟上一則主動訊息講一樣的話題，口氣隨性自然，不要演言情劇本）"
                    )
                    msg = await generate_reply(proactive_prompt=hint, channel=target_ch)
                    if msg:
                        await send_natural_messages(target_ch, msg)
    except Exception as err:
        print(f"[Background Loop Error] {err}", flush=True)


if __name__ == "__main__":
    if not DISCORD_TOKEN or not GROQ_API_KEY:
        raise RuntimeError("Missing DISCORD_TOKEN or GROQ_API_KEY.")
    client.run(DISCORD_TOKEN)
