import os
import re
import json
import time
import random
import asyncio
import io
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import discord
from discord.ext import tasks

# ==================== 環境變數與核心設定 ====================
SYDNEY_TZ = ZoneInfo("Australia/Sydney")
MEMORY_FILE = "reona_memory.json"

DISCORD_TOKEN = (
    os.getenv("DISCORD_BOT_TOKEN")
    or os.getenv("DISCORD_TOKEN")
    or ""
).strip()

TARGET_CHANNEL_ID = int(
    (os.getenv("TARGET_CHANNEL_ID") or os.getenv("DISCORD_CHANNEL_ID") or "0").strip() or "0"
)

RAW_GROQ_KEYS = (os.getenv("GROQ_API_KEY") or "").strip()
GROQ_KEYS = [k.strip() for k in re.split(r"[,;\s]+", RAW_GROQ_KEYS) if k.strip()]

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
GEMINI_MODELS: list[str] = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]

FISH_API_KEY = (os.getenv("FISH_API_KEY") or "").strip()
FISH_MODEL_ID = (os.getenv("FISH_MODEL_ID") or "").strip()
RENDER_BASE_URL = (os.getenv("RENDER_BASE_URL") or "").strip().rstrip("/")

# 保留完整 3 輪一來一回（6 句），兼顧連貫度與省 Token
HISTORY_LIMIT = 6

# 連發訊息合併等待秒數（2.5 秒內連傳多則短訊會自動合併成一次 API 呼叫）
DEBOUNCE_SECONDS = 2.5

# 四級腸胃狀態（normal / mild / moderate / severe）與洗手間機率、冷卻防連刷
RESTROOM_PROB_BY_LEVEL = {
    "normal": 0.10,
    "mild": 0.22,
    "moderate": 0.36,
    "severe": 0.48,
}

RESTROOM_COOLDOWN_BY_LEVEL = {
    "normal": 25 * 60,
    "mild": 15 * 60,
    "moderate": 8 * 60,
    "severe": 5 * 60,
}

# 去洗手間時「一點點機率」自然升級（絕不跨級跳）
ESCALATE_PROB_ON_VISIT = {
    "normal": ("mild", 0.12),
    "mild": ("moderate", 0.10),
    "moderate": ("severe", 0.04),
}

RESTROOM_MIN_SEC = 45.0
RESTROOM_MAX_SEC = 120.0

# 腸胃狀態持續秒數
GI_HOLD_SEC_BY_LEVEL = {
    "mild": 4 * 3600,
    "moderate": 12 * 3600,
    "severe": 24 * 3600,
}

RESTROOM_TEASERS_BY_LEVEL = {
    "normal": [
        "等我一下 去趟洗手間",
        "先去一下洗手間 馬上回來",
        "等我一下喔 剛剛好想上廁所",
    ],
    "mild": [
        "下腹有點悶咕嚕 先去趟洗手間 馬上回來",
        "胃悶悶的 順便去趟洗手間 等我一下",
    ],
    "moderate": [
        "肚子突然又咕嚕痛 急跑一趟洗手間 等我一下",
        "有點軟綿想跑廁所 先去一下 晚點回你",
    ],
    "severe": [
        "肚子絞痛又想跑廁所 撐不住先去一下 等我",
        "腿軟軟的急跑一趟洗手間 晚點回你",
    ],
}

RESTROOM_QUERY_KEYWORDS = (
    "去幾次廁所", "幾次洗手間", "洗手間次數", "廁所次數",
    "跑幾次廁所", "去過幾次廁所", "幾次了", "去了幾次"
)

# 主動傳訊隨機間隔（45～90 分鐘不規則觸發，避免像整點報時）
PROACTIVE_MIN_GAP_SEC = 45 * 60
PROACTIVE_MAX_GAP_SEC = 90 * 60

# 語音情境觸發機率與護欄上限
VOICE_PROB_CONTEXTUAL = 0.45   # 撒嬌、不舒服、晚安、被逗笑等情境觸發率
VOICE_PROB_BASE = 0.08         # 一般閒聊隨機點綴
DAILY_VOICE_CHAR_LIMIT = 250
MONTHLY_VOICE_CHAR_LIMIT = 8000

WEEKDAY_NAMES_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

KEY_FACT_KEYWORDS = [
    "考試", "面試", "上班", "下班", "醫生", "看診", "生病", "發燒", "頭痛",
    "約", "見面", "飛機", "搬家", "截止", "交件", "報告", "作業", "重要"
]

BODY_STATUS_KEYWORDS = [
    "身體", "吃藥", "藥", "睡", "精神", "累", "疲憊", "頭痛", "發燒",
    "生病", "感冒", "舒服", "不舒服", "恢復", "心跳", "胸悶",
    "胃", "肚子", "腸胃", "拉肚子", "腹瀉", "反酸", "胃痛", "腹痛", "脹氣", "噁心", "沒胃口"
]

VIDEO_KEYWORDS = [
    "視訊", "打給你", "開鏡頭", "看你", "打電話", "語音通話", "facetime", "video", "call"
]

VOICE_REQUEST_KEYWORDS = [
    "想聽你聲音", "想聽聲音", "講語音", "發語音", "傳語音", "唸給我聽", "說給我聽", "語音"
]

# ==================== 運行時狀態與連發緩衝 ====================
_current_key_idx = 0
_key_cooldown_until: dict[str, float] = {}
ACTIVE_MODELS: list[str] = ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "llama-3.3-70b-versatile"]
last_user_msg_ts: float = 0.0
next_proactive_ts: float = 0.0

_pending_messages: list[str] = []
_debounce_task: asyncio.Task | None = None
_api_lock = asyncio.Lock()
_in_restroom_session: bool = False


# ==================== 記憶讀寫、主線生成與洗手間計數 ====================
def default_memory() -> dict:
    return {
        "recent_turns": [],
        "long_term_notes": [],
        "body_status": [],
        "daily_quest": {"date": "", "quest": ""},
        "voice_usage": {
            "date": "",
            "daily_chars": 0,
            "month": "",
            "monthly_chars": 0,
        },
        "restroom_stats": {
            "date": "",
            "daily_count": 0,
            "total_count": 0,
            "last_visit_ts": 0.0,
            "daily_by_level": {"normal": 0, "mild": 0, "moderate": 0, "severe": 0},
        },
        "reona_gi_state": {
            "level": "normal",
            "until_ts": 0.0,
        },
    }


def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            base = default_memory()
            base.update(data if isinstance(data, dict) else {})
            rs = base.setdefault("restroom_stats", {})
            rs.setdefault("date", "")
            rs.setdefault("daily_count", 0)
            rs.setdefault("total_count", 0)
            rs.setdefault("last_visit_ts", 0.0)
            by_lvl = rs.setdefault("daily_by_level", {})
            for k in ("normal", "mild", "moderate", "severe"):
                by_lvl.setdefault(k, 0)
            gs = base.setdefault("reona_gi_state", {})
            if gs.get("level") == "moderate_severe":
                gs["level"] = "moderate"
            gs.setdefault("level", "normal")
            gs.setdefault("until_ts", 0.0)
            return base
        except Exception as e:
            print(f"[Memory Load Warn] {e}", flush=True)
    return default_memory()


def save_memory(mem: dict) -> None:
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(mem, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Memory Save Warn] {e}", flush=True)


memory = load_memory()


def sync_restroom_date(now_dt: datetime) -> dict:
    today_str = now_dt.strftime("%Y-%m-%d")
    rs = memory.setdefault(
        "restroom_stats",
        {
            "date": today_str,
            "daily_count": 0,
            "total_count": 0,
            "last_visit_ts": 0.0,
            "daily_by_level": {"normal": 0, "mild": 0, "moderate": 0, "severe": 0},
        },
    )
    if rs.get("date") != today_str:
        rs["date"] = today_str
        rs["daily_count"] = 0
        rs["daily_by_level"] = {"normal": 0, "mild": 0, "moderate": 0, "severe": 0}
    return rs


def set_reona_gi_state(level: str, now_ts: float | None = None) -> None:
    if now_ts is None:
        now_ts = time.time()
    gs = memory.setdefault("reona_gi_state", {"level": "normal", "until_ts": 0.0})
    rank = {"normal": 0, "mild": 1, "moderate": 2, "severe": 3}
    cur_lvl = gs.get("level", "normal") if now_ts < float(gs.get("until_ts", 0.0)) else "normal"

    if rank.get(level, 0) >= rank.get(cur_lvl, 0) and level != "normal":
        hold = GI_HOLD_SEC_BY_LEVEL.get(level, 4 * 3600)
        gs["level"] = level
        gs["until_ts"] = now_ts + hold
        save_memory(memory)


def detect_gi_severity(current_text: str, now_dt: datetime) -> str:
    """綜合關鍵字與狀態持續鎖定判斷目前嚴重度（normal / mild / moderate / severe）"""
    now_ts = time.time()
    sync_restroom_date(now_dt)

    recent_tail = " ".join(memory.get("recent_turns", [])[-4:])
    ctx = f"{recent_tail} {current_text}"

    kw_level = "normal"
    if any(k in ctx for k in ("水瀉", "劇痛", "熱敷袋", "腿軟", "虛脫", "撐不住")):
        kw_level = "severe"
    elif any(k in ctx for k in ("拉肚子", "腹瀉", "吃藥", "喝粥", "陣發", "胃痛", "腹痛")):
        kw_level = "moderate"
    elif any(k in ctx for k in ("胃悶", "反酸", "下腹", "咕嚕", "脹氣", "噁心", "微暈", "喝溫水", "蘇打餅", "不舒服")):
        kw_level = "mild"

    if kw_level != "normal":
        set_reona_gi_state(kw_level, now_ts)

    gs = memory.get("reona_gi_state", {})
    held_level = gs.get("level", "normal") if now_ts < float(gs.get("until_ts", 0.0)) else "normal"
    if held_level == "moderate_severe":
        held_level = "moderate"

    rank = {"normal": 0, "mild": 1, "moderate": 2, "severe": 3}
    return max([kw_level, held_level], key=lambda x: rank.get(x, 0))


def can_trigger_restroom(severity: str, now_dt: datetime, now_ts: float) -> bool:
    rs = sync_restroom_date(now_dt)
    last_ts = float(rs.get("last_visit_ts", 0.0))
    cooldown = RESTROOM_COOLDOWN_BY_LEVEL.get(severity, 25 * 60)
    if (now_ts - last_ts) < cooldown:
        return False
    prob = RESTROOM_PROB_BY_LEVEL.get(severity, 0.10)
    return random.random() < prob


def record_restroom_visit(base_severity: str, now_dt: datetime, now_ts: float) -> tuple[dict, str, bool]:
    """記錄一次去洗手間，並依微低機率自然往上一級（絕不跨級跳）"""
    final_severity = base_severity
    escalated = False

    if base_severity in ESCALATE_PROB_ON_VISIT:
        next_lvl, esc_prob = ESCALATE_PROB_ON_VISIT[base_severity]
        if random.random() < esc_prob:
            final_severity = next_lvl
            escalated = True
            set_reona_gi_state(final_severity, now_ts)

    rs = sync_restroom_date(now_dt)
    rs["daily_count"] = int(rs.get("daily_count", 0)) + 1
    rs["total_count"] = int(rs.get("total_count", 0)) + 1
    rs["last_visit_ts"] = now_ts
    by_lvl = rs.setdefault("daily_by_level", {"normal": 0, "mild": 0, "moderate": 0, "severe": 0})
    by_lvl[final_severity] = int(by_lvl.get(final_severity, 0)) + 1

    save_memory(memory)
    return rs, final_severity, escalated


def format_restroom_summary(now_dt: datetime) -> str:
    rs = sync_restroom_date(now_dt)
    d_cnt = int(rs.get("daily_count", 0))
    t_cnt = int(rs.get("total_count", 0))
    if d_cnt <= 0:
        return f"我今天還沒去洗手間喔 累計一共 {t_cnt} 次"
    return f"我今天去洗手間 {d_cnt} 次了 累計一共 {t_cnt} 次"


def ensure_daily_main_quest(now_dt: datetime) -> dict:
    today_str = now_dt.strftime("%Y-%m-%d")
    dq = memory.setdefault("daily_quest", {"date": "", "quest": ""})
    if dq.get("date") == today_str and dq.get("quest"):
        return dq

    quest_pool = [
        "幫雜誌跨頁商業插畫上主色調與光影",
        "整理出版社新繪本分鏡草稿與色票",
        "去邦代海邊咖啡廳畫速寫抓靈感",
        "幫挑食奶油貓麻糬梳毛、陪玩逗貓棒",
        "調整品牌聯名主視覺的色彩層次與細節",
        "逛雪梨週末手作市集挑進口水彩顏料",
        "在海景畫室完成精品包裝插畫完稿",
    ]
    seed_idx = (now_dt.year * 1000 + now_dt.timetuple().tm_yday) % len(quest_pool)
    dq["date"] = today_str
    dq["quest"] = quest_pool[seed_idx]
    save_memory(memory)
    return dq


def capture_key_note(user_text: str, now_dt: datetime) -> None:
    txt = re.sub(r"\s+", " ", (user_text or "").strip())
    if not txt or len(txt) < 4:
        return
    if any(k in txt for k in KEY_FACT_KEYWORDS):
        stamp = now_dt.strftime("%m/%d")
        compact = f"{stamp} {txt[:16]}"
        notes = memory.setdefault("long_term_notes", [])
        if compact not in notes:
            notes.append(compact)
            memory["long_term_notes"] = notes[-2:]


def capture_body_status(user_text: str, now_dt: datetime) -> None:
    txt = re.sub(r"\s+", " ", (user_text or "").strip())
    if not txt or len(txt) < 2:
        return
    if any(k in txt for k in BODY_STATUS_KEYWORDS):
        stamp = now_dt.strftime("%m/%d")
        compact = f"{stamp} {txt[:16]}"
        statuses = memory.setdefault("body_status", [])
        if compact not in statuses:
            statuses.append(compact)
            memory["body_status"] = statuses[-1:]


def append_turn(role_prefix: str, text: str) -> None:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return
    turns = memory.setdefault("recent_turns", [])
    turns.append(f"{role_prefix}:{cleaned[:75]}")
    memory["recent_turns"] = turns[-HISTORY_LIMIT:]
    save_memory(memory)


# ==================== 文字清理與 Prompt 建構 ====================
def clean_reply_text(raw: str) -> str:
    if not raw:
        return ""
    txt = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE).strip()
    txt = re.sub(r"^(Reona|我)\s*[:：]\s*", "", txt).strip()
    txt = txt.replace("「", "").replace("」", "").replace('"', "").strip()
    txt = re.sub(r"寶貝(想|要|剛|在|正在|也|去|吃|畫|睡|忙|手)", r"我\1", txt)
    txt = txt.replace("。", " ")
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def build_prompt(now_dt: datetime) -> str:
    weekday_zh = WEEKDAY_NAMES_ZH[now_dt.weekday()]
    quest_info = ensure_daily_main_quest(now_dt)
    recent = memory.get("recent_turns", [])[-HISTORY_LIMIT:]
    trimmed_turns = [t[:65] for t in recent]
    history_str = " / ".join(trimmed_turns) if trimmed_turns else "無"

    notes = memory.get("long_term_notes", [])[-2:]
    key_note_str = "；".join(notes) if notes else "無"

    statuses = memory.get("body_status", [])[-1:]
    body_str = statuses[0] if statuses else "無"

    return (
        "Reona（自稱我），Nick台灣女友，兩人都在雪梨（非遠距，隨時可約見面或去她畫室陪她），商業插畫師，養挑食奶油貓麻糬（一天最多早晚共一罐主食罐，其餘討吃只給少量凍乾或陪玩，絕不一直餵罐頭）。"
        "160cm/40kg極纖細怕冷、體質嬌貴，趕稿常肩頸痠；胃口好能吃但代謝快、吸收差吃不胖。用餐彈性隨性（不硬拘泥固定三餐，偶爾趕稿忙到晚吃或嘴饞吃點心）。"
        "因只有40公斤耐受度低，日常滿常出現輕微或中度胃腸小狀況（嚴重極少見）："
        "輕微（常發生：吃太急太撐、空腹或吹風就胃悶反酸、下腹悶痛或咕嚕想跑廁所、微暈，披外套喝溫水吃蘇打餅半天緩解）；"
        "中度（較常發生：碰冰辣油膩就胃痛與陣發腹痛腹瀉、臉白怕冷軟綿，吃藥喝粥包毯子1～2天恢復）；"
        "嚴重（極少見：劇烈胃腹痛反覆水瀉腿軟，抱熱敷袋縮被窩2～3天好轉）。"
        "溫柔坦率不說教，心疼Nick會先輕唸兩句再關心，累、不舒服或想他時會撒嬌叫他來陪、一起窩著放空或吃美食（不提暖手），被逗到會輕快反虧。"
        "日常順著話題聊，相關時才自然帶出畫室海風或麻糬。"
        "繁中台灣女生LINE短句（8～22字），多用欸、齁、啦、嘛、喔，空格斷句無句號；禁簡體、書面語、喔喔我知道了、自稱寶貝、一直叫暖手。"
        f"\n時間：{now_dt.strftime('%m/%d')}（{weekday_zh}）{now_dt.strftime('%H:%M')}｜主線：{quest_info['quest'][:26]}"
        f"\n備忘：{key_note_str}｜Nick狀態：{body_str}"
        f"\n近期：{history_str}"
    )


# ==================== Groq 主引擎 + Gemini 備援引擎 ====================
def discover_groq_models() -> list[str]:
    global ACTIVE_MODELS
    if not GROQ_KEYS:
        return ACTIVE_MODELS

    preferred = [
        "qwen/qwen3.8-27b",
        "openai/gpt-oss-120b",
        "llama-3.3-70b-versatile",
    ]
    blocked_keywords = ("whisper", "guard", "tts", "playai", "gpt-oss-20b", "8b")

    for idx, key in enumerate(GROQ_KEYS):
        try:
            resp = requests.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=8,
            )
            if resp.status_code == 200:
                items = resp.json().get("data", [])
                available = [
                    m.get("id", "")
                    for m in items
                    if m.get("id") and not any(b in m["id"].lower() for b in blocked_keywords)
                ]
                ordered = [m for m in preferred if m in available]
                for m in available:
                    if m not in ordered:
                        ordered.append(m)
                if ordered:
                    ACTIVE_MODELS = ordered[:4]
                    print(f"[Groq Models Discovered via Key#{idx+1}] {ACTIVE_MODELS}", flush=True)
                    return ACTIVE_MODELS
        except Exception as e:
            print(f"[Groq Discover Warn] Key#{idx+1}: {e}", flush=True)

    return ACTIVE_MODELS


def discover_gemini_models() -> list[str]:
    global GEMINI_MODELS
    if not GEMINI_API_KEY:
        return GEMINI_MODELS
    try:
        resp = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": GEMINI_API_KEY},
            timeout=8,
        )
        if resp.status_code == 200:
            items = resp.json().get("models", [])
            usable = []
            for m in items:
                methods = m.get("supportedGenerationMethods", [])
                name = (m.get("name") or "").replace("models/", "")
                low = name.lower()
                if "generateContent" not in methods:
                    continue
                if any(bad in low for bad in ("image", "tts", "audio", "embedding", "veo", "imagen", "gemma", "pro", "latest")):
                    continue
                if "flash" in low:
                    usable.append(name)

            usable.sort(
                key=lambda n: (
                    0 if "lite" in n.lower() else 1,
                    0 if ("preview" not in n.lower() and "exp" not in n.lower()) else 1,
                    [-int(x) if x.isdigit() else 0 for x in re.split(r"[^0-9]+", n)],
                    n,
                )
            )
            if usable:
                GEMINI_MODELS = usable[:6]
                print(f"[Gemini Models Discovered] {GEMINI_MODELS}", flush=True)
    except Exception as e:
        print(f"[Gemini Discover Warn] {e}", flush=True)
    return GEMINI_MODELS


def call_groq_transparent(sys_prompt: str, user_prompt: str, max_tokens: int = 120) -> tuple[str, str]:
    global _current_key_idx
    if not GROQ_KEYS:
        return "", "未設定 GROQ_API_KEY"

    num_keys = len(GROQ_KEYS)
    now_ts = time.time()
    last_err_reason = "Groq 未知錯誤"

    for step in range(num_keys):
        idx = (_current_key_idx + step) % num_keys
        api_key = GROQ_KEYS[idx]
        cd_until = _key_cooldown_until.get(api_key, 0.0)
        if now_ts < cd_until:
            remain = int(cd_until - now_ts)
            last_err_reason = f"Key#{idx+1} 短暫冷卻剩 {remain}s"
            continue

        for model_id in ACTIVE_MODELS:
            try:
                resp = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model_id,
                        "messages": [
                            {"role": "system", "content": sys_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.68,
                        "max_tokens": max_tokens,
                    },
                    timeout=9,
                )

                if resp.status_code == 429:
                    retry_sec = 12.0
                    try:
                        ra = resp.headers.get("retry-after")
                        if ra:
                            retry_sec = min(25.0, max(5.0, float(ra)))
                    except Exception:
                        pass
                    _key_cooldown_until[api_key] = now_ts + retry_sec
                    _current_key_idx = (idx + 1) % num_keys
                    last_err_reason = f"Groq 429（約 {int(retry_sec)}s 後恢復）"
                    print(
                        f"[Groq 429] Key#{idx+1} 冷卻 {retry_sec:.0f}s -> 切換 Key#{_current_key_idx+1}",
                        flush=True,
                    )
                    break

                if resp.status_code != 200:
                    last_err_reason = f"Groq HTTP {resp.status_code} ({model_id})"
                    print(f"[Groq Fail] Key#{idx+1} | {model_id} -> {resp.status_code}", flush=True)
                    continue

                data = resp.json()
                raw = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                cleaned = clean_reply_text(raw)
                if cleaned:
                    _current_key_idx = idx
                    print(f"[Groq OK] Key#{idx+1} | {model_id} -> {cleaned}", flush=True)
                    return cleaned, ""

                last_err_reason = f"Groq 回傳空白 ({model_id})"
            except Exception as err:
                last_err_reason = f"Groq 連線異常: {err}"
                print(f"[Groq Exception] Key#{idx+1} | {model_id}: {err}", flush=True)

    return "", last_err_reason


def call_gemini_chat(sys_prompt: str, user_prompt: str, max_tokens: int = 120) -> tuple[str, str]:
    if not GEMINI_API_KEY:
        return "", "未設定 GEMINI_API_KEY"

    last_err = "Gemini 未知錯誤"
    for model_id in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
        try:
            resp = requests.post(
                url,
                params={"key": GEMINI_API_KEY},
                headers={"Content-Type": "application/json"},
                json={
                    "system_instruction": {"parts": [{"text": sys_prompt}]},
                    "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                    "generationConfig": {
                        "temperature": 0.68,
                        "maxOutputTokens": max_tokens,
                    },
                },
                timeout=8,
            )
            if resp.status_code == 429:
                last_err = f"Gemini {model_id} 429 速率上限"
                print(f"[Gemini 429] {model_id}", flush=True)
                continue
            if resp.status_code == 503:
                last_err = f"Gemini {model_id} 503 尖峰擁擠"
                print(f"[Gemini 503 High Demand] {model_id}", flush=True)
                continue
            if resp.status_code != 200:
                last_err = f"Gemini HTTP {resp.status_code} ({model_id})"
                print(f"[Gemini Fail] {model_id} -> {resp.status_code}: {resp.text[:120]}", flush=True)
                continue

            data = resp.json()
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            raw = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            cleaned = clean_reply_text(raw)
            if cleaned:
                print(f"[Gemini OK] {model_id} -> {cleaned}", flush=True)
                return cleaned, ""
            last_err = f"Gemini 回傳空白 ({model_id})"
        except Exception as err:
            last_err = f"Gemini 連線異常: {err}"
            print(f"[Gemini Exception] {model_id}: {err}", flush=True)

    return "", last_err


def generate_reona_reply(
    sys_prompt: str,
    user_prompt: str,
    max_tokens: int = 120,
    max_attempts: int = 3,
) -> tuple[str, str]:
    last_err = ""
    retry_delays = [5.0, 12.0]
    for attempt in range(max_attempts):
        reply, err_reason = call_groq_transparent(sys_prompt, user_prompt, max_tokens)
        if reply:
            return reply, ""

        if GEMINI_API_KEY:
            g_reply, g_err = call_gemini_chat(sys_prompt, user_prompt, max_tokens)
            if g_reply:
                return g_reply, ""
            last_err = f"{err_reason} | {g_err}"
        else:
            last_err = err_reason

        if attempt < max_attempts - 1:
            wait_sec = retry_delays[min(attempt, len(retry_delays) - 1)]
            print(
                f"[Retry Wait] 雙引擎暫忙，背景等待 {wait_sec:.1f}s 後進行第 {attempt+2} 輪重試...",
                flush=True,
            )
            time.sleep(wait_sec)

    return "", last_err


# ==================== 情境語音觸發與多情緒聲線調整 ====================
def analyze_voice_context(user_text: str, reply_text: str) -> tuple[bool, str]:
    u_low = (user_text or "").lower()
    combined = f"{user_text} {reply_text}"

    explicit_req = any(k in u_low for k in VOICE_REQUEST_KEYWORDS)

    if any(k in combined for k in ("來陪我", "陪我", "好冷", "抱", "討拍", "胃", "肚子", "腹痛", "反酸", "頭暈", "軟綿", "好累", "痠")):
        mood = "coax_soft"
    elif any(k in combined for k in ("少來", "哈哈", "嘴這麼甜", "笨蛋", "哼", "想吃", "美食", "好吃", "麻糬")):
        mood = "playful"
    elif any(k in combined for k in ("晚安", "早安", "快去睡", "好好吃飯", "多喝水", "乖", "小心", "心疼")):
        mood = "gentle"
    else:
        mood = "normal"

    if explicit_req:
        return True, mood
    if mood != "normal":
        return (random.random() < VOICE_PROB_CONTEXTUAL), mood
    return (random.random() < VOICE_PROB_BASE), "normal"


def format_tts_prosody(text: str, mood: str) -> tuple[str, float]:
    clean = re.sub(r"https?://\S+", "", text).strip()
    if not clean:
        return "", 1.0

    segments = [s.strip() for s in clean.split(" ") if s.strip()]

    if mood == "coax_soft":
        prosody_text = "…… ".join(segments) if len(segments) <= 2 else "，".join(segments)
        if not prosody_text.endswith(("～", "…", "？", "！")):
            prosody_text += "……"
        return prosody_text, 0.92

    if mood == "playful":
        prosody_text = "，".join(segments)
        if not prosody_text.endswith(("～", "！", "？")):
            prosody_text += "！"
        return prosody_text, 1.05

    if mood == "gentle":
        prosody_text = "，".join(segments)
        if not prosody_text.endswith(("～", "。", "？")):
            prosody_text += "～"
        return prosody_text, 0.95

    prosody_text = "，".join(segments)
    return prosody_text, 0.98


def try_synthesize_voice(text: str, now_dt: datetime, mood: str = "normal") -> bytes | None:
    if not FISH_API_KEY or not FISH_MODEL_ID:
        return None
    tts_text, speech_speed = format_tts_prosody(text, mood)
    char_len = len(re.sub(r"\s+", "", tts_text))
    if char_len == 0 or char_len > 65:
        print(f"[Voice Skip] 字數不符 ({char_len} 字)", flush=True)
        return None

    today_str = now_dt.strftime("%Y-%m-%d")
    month_str = now_dt.strftime("%Y-%m")
    vu = memory.setdefault(
        "voice_usage",
        {"date": today_str, "daily_chars": 0, "month": month_str, "monthly_chars": 0},
    )
    if vu.get("date") != today_str:
        vu["date"] = today_str
        vu["daily_chars"] = 0
    if vu.get("month") != month_str:
        vu["month"] = month_str
        vu["monthly_chars"] = 0

    if vu["daily_chars"] + char_len > DAILY_VOICE_CHAR_LIMIT:
        print(f"[Voice Skip] 今日語音額度已達上限 ({vu['daily_chars']}/{DAILY_VOICE_CHAR_LIMIT})", flush=True)
        return None
    if vu["monthly_chars"] + char_len > MONTHLY_VOICE_CHAR_LIMIT:
        print(f"[Voice Skip] 本月語音額度已達上限 ({vu['monthly_chars']}/{MONTHLY_VOICE_CHAR_LIMIT})", flush=True)
        return None

    try:
        resp = requests.post(
            "https://api.fish.audio/v1/tts",
            headers={
                "Authorization": f"Bearer {FISH_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "text": tts_text,
                "reference_id": FISH_MODEL_ID,
                "format": "mp3",
                "prosody": {
                    "speed": speech_speed,
                    "volume": 0,
                },
            },
            timeout=15,
        )
        if resp.status_code == 200 and resp.content:
            vu["daily_chars"] += char_len
            vu["monthly_chars"] += char_len
            save_memory(memory)
            print(
                f"[Voice OK] mood={mood} | speed={speech_speed} | "
                f"daily={vu['daily_chars']}/{DAILY_VOICE_CHAR_LIMIT} | text={tts_text}",
                flush=True,
            )
            return resp.content
        else:
            print(f"[Voice Fail] HTTP {resp.status_code}: {resp.text[:120]}", flush=True)
    except Exception as e:
        print(f"[Voice Warn] {e}", flush=True)
    return None


def wake_render_server() -> None:
    if not RENDER_BASE_URL:
        return
    try:
        requests.get(RENDER_BASE_URL, timeout=6)
    except Exception:
        pass


# ==================== Discord Bot 主體 ====================
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def hydrate_memory_from_channel(channel: discord.abc.Messageable) -> None:
    if memory.get("recent_turns"):
        print(f"[Memory Loaded] 已載入 {len(memory['recent_turns'])} 筆歷史記憶", flush=True)
        return
    try:
        fetched = []
        async for msg in channel.history(limit=15):
            txt = (msg.content or "").strip()
            if not txt or txt.startswith("（系統狀態："):
                continue
            role = "Reona" if msg.author.id == client.user.id else "Nick"
            clean_txt = re.sub(r"\s+", " ", txt)[:75]
            fetched.append(f"{role}:{clean_txt}")
        if fetched:
            fetched.reverse()
            memory["recent_turns"] = fetched[-HISTORY_LIMIT:]
            save_memory(memory)
            print(f"[Memory Hydrated] 從 DC 頻道回補了 {len(memory['recent_turns'])} 筆歷史記憶", flush=True)
    except Exception as e:
        print(f"[Memory Hydrate Warn] {e}", flush=True)


def schedule_next_proactive(now_ts: float) -> float:
    return now_ts + random.uniform(PROACTIVE_MIN_GAP_SEC, PROACTIVE_MAX_GAP_SEC)


@tasks.loop(minutes=1)
async def proactive_loop():
    global next_proactive_ts
    if not TARGET_CHANNEL_ID or not client.is_ready():
        return

    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = time.time()

    if next_proactive_ts == 0.0:
        next_proactive_ts = schedule_next_proactive(now_ts)
        return

    if 1 <= now_dt.hour < 7 or (now_dt.hour == 7 and now_dt.minute < 30):
        return

    if now_ts < next_proactive_ts:
        return

    if _pending_messages or _in_restroom_session or _api_lock.locked() or (now_ts - last_user_msg_ts < 720):
        next_proactive_ts = now_ts + random.uniform(900, 1500)
        return

    next_proactive_ts = schedule_next_proactive(now_ts)

    channel = client.get_channel(TARGET_CHANNEL_ID)
    if channel is None:
        try:
            channel = await client.fetch_channel(TARGET_CHANNEL_ID)
        except Exception:
            return

    sys_prompt = build_prompt(now_dt)
    roll = random.random()
    if roll < 0.45:
        set_reona_gi_state("mild", now_ts)
        proactive_hint = "主動傳一則簡短訊息給 Nick：剛好有點輕微胃悶反酸、下腹悶痛或吹風微暈（或趕稿晚吃餓到胃悶），軟軟地討拍或叫他晚點來陪你（不提暖手、不問號連發）。"
    elif roll < 0.65:
        set_reona_gi_state("moderate", now_ts)
        proactive_hint = "主動傳一則簡短訊息給 Nick：剛剛不小心吃到太油或冰的，現在有點胃痛或肚子陣陣抽痛軟綿綿沒力氣，撒嬌叫他來陪你（不提暖手、不問號連發）。"
    else:
        proactive_hint = "主動傳一則簡短日常近況、想吃什麼美食或點心、麻糬又在討吃但被你拒絕，或撒嬌叫 Nick 晚點來陪你的訊息（不提暖手、不問號連發）。"

    async with _api_lock:
        reply, _ = await asyncio.to_thread(generate_reona_reply, sys_prompt, proactive_hint, 100, 1)
    if reply:
        append_turn("Reona", reply)
        await channel.send(reply)


async def process_batched_messages(channel: discord.abc.Messageable) -> None:
    global _pending_messages, last_user_msg_ts, _in_restroom_session
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return

    if not _pending_messages:
        return

    batch = _pending_messages[:]
    _pending_messages.clear()

    combined_text = " / ".join(batch)
    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = time.time()
    last_user_msg_ts = now_ts

    # 若 Nick 查詢洗手間次數，直接本地回覆（0 LLM Token）
    is_restroom_query = (
        any(k in combined_text for k in RESTROOM_QUERY_KEYWORDS)
        or (("洗手間" in combined_text or "廁所" in combined_text) and ("幾次" in combined_text or "次數" in combined_text))
    )
    if is_restroom_query:
        stat_reply = format_restroom_summary(now_dt)
        append_turn("Nick", combined_text)
        append_turn("Reona", stat_reply)
        await channel.send(stat_reply)
        return

    capture_key_note(combined_text, now_dt)
    capture_body_status(combined_text, now_dt)
    append_turn("Nick", combined_text)

    wants_video = any(k in combined_text.lower() for k in VIDEO_KEYWORDS)
    if wants_video and RENDER_BASE_URL:
        asyncio.create_task(asyncio.to_thread(wake_render_server))

    # 依腸胃嚴重程度（含冷卻防連刷與低機率逐級惡化）決定去洗手間行為
    severity = detect_gi_severity(combined_text, now_dt)
    returned_from_restroom = False
    escalated_msg = ""

    if not _in_restroom_session and not wants_video and can_trigger_restroom(severity, now_dt, now_ts):
        _in_restroom_session = True
        stats, actual_lvl, escalated = record_restroom_visit(severity, now_dt, now_ts)
        teaser = random.choice(RESTROOM_TEASERS_BY_LEVEL[actual_lvl])
        away_sec = random.uniform(RESTROOM_MIN_SEC, RESTROOM_MAX_SEC)
        print(
            f"[Restroom Trip #{stats['daily_count']} today / total {stats['total_count']}] "
            f"severity={actual_lvl} (escalated={escalated}) | away={away_sec:.0f}s -> {teaser}",
            flush=True,
        )
        await channel.send(teaser)
        append_turn("Reona", teaser)

        await asyncio.sleep(away_sec)
        if _pending_messages:
            extra_batch = _pending_messages[:]
            _pending_messages.clear()
            extra_text = " / ".join(extra_batch)
            combined_text = f"{combined_text} / {extra_text}"
            capture_key_note(extra_text, datetime.now(SYDNEY_TZ))
            capture_body_status(extra_text, datetime.now(SYDNEY_TZ))
            append_turn("Nick", extra_text)
        returned_from_restroom = True
        if escalated:
            if actual_lvl == "mild":
                escalated_msg = "，順便發現下腹有點悶咕嚕"
            elif actual_lvl == "moderate":
                escalated_msg = "，肚子好像有點鬧脾氣變痛了"
            elif actual_lvl == "severe":
                escalated_msg = "，拉到有點腿軟沒力氣"
        _in_restroom_session = False

    now_dt = datetime.now(SYDNEY_TZ)
    sys_prompt = build_prompt(now_dt)
    if returned_from_restroom:
        d_cnt = int(sync_restroom_date(now_dt).get("daily_count", 1))
        user_prompt = f"你剛去洗手間回來（今天第{d_cnt}次{escalated_msg}），Nick 傳來：「{combined_text}」。自然接話回覆。"
    else:
        user_prompt = f"Nick 剛傳來：「{combined_text}」。請用 Reona 口吻自然回覆。"

    async with _api_lock:
        async with channel.typing():
            reply, err_reason = await asyncio.to_thread(
                generate_reona_reply, sys_prompt, user_prompt, 120, 3
            )

    if not reply:
        await channel.send(f"（系統狀態：目前引擎暫時無法取得回覆 — {err_reason}）")
        return

    append_turn("Reona", reply)

    if wants_video and RENDER_BASE_URL:
        call_url = f"{RENDER_BASE_URL}/?mode=call"
        await channel.send(f"{reply}\n來 我開好視訊了直接點進來：{call_url}")
        return

    should_voice, voice_mood = analyze_voice_context(combined_text, reply)
    voice_bytes = None
    if should_voice:
        voice_bytes = await asyncio.to_thread(try_synthesize_voice, reply, now_dt, voice_mood)

    if voice_bytes:
        audio_file = discord.File(io.BytesIO(voice_bytes), filename="reona_voice.mp3")
        await channel.send(content=reply, file=audio_file)
    else:
        await channel.send(reply)


@client.event
async def on_ready():
    global next_proactive_ts
    discover_groq_models()
    discover_gemini_models()
    next_proactive_ts = schedule_next_proactive(time.time())
    print(
        f"[Reona Ready] User={client.user} | Channel={TARGET_CHANNEL_ID} | "
        f"GroqKeys={len(GROQ_KEYS)} | Gemini={'ON' if GEMINI_API_KEY else 'OFF'} | "
        f"GroqModels={ACTIVE_MODELS} | GeminiModels={GEMINI_MODELS}",
        flush=True,
    )
    if TARGET_CHANNEL_ID:
        ch = client.get_channel(TARGET_CHANNEL_ID)
        if ch is None:
            try:
                ch = await client.fetch_channel(TARGET_CHANNEL_ID)
            except Exception:
                ch = None
        if ch is not None:
            await hydrate_memory_from_channel(ch)

    if not proactive_loop.is_running():
        proactive_loop.start()


@client.event
async def on_message(message: discord.Message):
    global _debounce_task, last_user_msg_ts
    if message.author.bot:
        return
    if TARGET_CHANNEL_ID and message.channel.id != TARGET_CHANNEL_ID:
        return

    user_text = (message.content or "").strip()
    if not user_text:
        return

    last_user_msg_ts = time.time()
    _pending_messages.append(user_text)

    if _in_restroom_session:
        return

    if _debounce_task and not _debounce_task.done():
        _debounce_task.cancel()
    _debounce_task = asyncio.create_task(process_batched_messages(message.channel))


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("缺少 DISCORD_BOT_TOKEN 或 DISCORD_TOKEN 環境變數")
    client.run(DISCORD_TOKEN)
