import os
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
DB_PATH = Path(os.getenv("REONA_DB_PATH", "reona_bg.db"))

ROUTINE_ACTIONS = {
    "water": {
        "cooldown_min": 60,
        "daily_cap": 5,
        "messages": [
            "我先去倒杯水喝，等我一下",
            "嘴巴好乾，我去裝個水馬上回來",
            "剛坐太久了，起來倒杯溫水順便伸個懶腰",
        ],
    },
    "toilet": {
        "cooldown_min": 120,
        "daily_cap": 4,
        "messages": [
            "我先去上個廁所，等我一下",
            "等我一下，我先去一趟洗手間",
        ],
    },
    "toilet_upset": {
        "cooldown_min": 45,
        "daily_cap": 6,
        "messages": [
            "肚子突然一陣滾，我先去跑個廁所",
            "等我一下，今天腸胃有點不給力，先去廁所",
            "肚子又悶悶的了，我先去一趟洗手間",
        ],
    },
    "tired_rest": {
        "cooldown_min": 90,
        "daily_cap": 3,
        "messages": [
            "盯螢幕太久眼睛好酸，先閉眼放空五分鐘",
            "肩膀硬到像石頭，起來拉個筋喝口水",
            "昨晚有點淺眠現在腦袋鈍鈍的，先去洗把臉清醒一下",
            "剛趕完一段草圖才發現手有點抖，先去弄點東西墊胃",
        ],
    },
    "feed_mochi": {
        "cooldown_min": 240,
        "daily_cap": 2,
        "messages": [
            "麻糬在腳邊一直喵喵叫討飯了，我先去幫牠倒飼料",
            "麻糬瘋狂蹭我腿催放飯，我去餵牠一下",
        ],
    },
}


def now_sydney() -> datetime:
    return datetime.now(SYDNEY_TZ)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                speaker TEXT NOT NULL,
                content TEXT NOT NULL,
                category TEXT DEFAULT 'routine',
                emotion_tag TEXT DEFAULT 'neutral',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT
            );
            """
        )
        now_iso = now_sydney().isoformat()
        defaults = {
            "unanswered_pings": "0",
            "last_ping_at": "",
            "last_nick_reply_at": now_iso,
            "is_sulking": "0",
            "last_wakeup_date": "",
            "last_morning_greet_date": "",
            "last_night_greet_date": "",
        }
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO bot_state (key, value, updated_at, expires_at) VALUES (?, ?, ?, NULL)",
                (k, v, now_iso),
            )


def set_state(key: str, value: str, ttl_hours: float | None = None) -> None:
    now = now_sydney()
    exp = (now + timedelta(hours=ttl_hours)).isoformat() if ttl_hours else None
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO bot_state (key, value, updated_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at
            """,
            (key, value, now.isoformat(), exp),
        )


def get_state(key: str, default: str = "") -> str:
    now = now_sydney()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value, expires_at FROM bot_state WHERE key = ?",
            (key,),
        ).fetchone()
        if not row:
            return default
        if row["expires_at"]:
            try:
                if datetime.fromisoformat(row["expires_at"]) <= now:
                    conn.execute("DELETE FROM bot_state WHERE key = ?", (key,))
                    return default
            except ValueError:
                pass
        return row["value"]


def log_memory(speaker: str, content: str, category: str = "routine", emotion_tag: str = "neutral") -> None:
    now = now_sydney()
    ttl_days = 7 if category == "notable_event" or emotion_tag in ("sad", "happy") else 2
    expires = (now + timedelta(days=ttl_days)).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO memories (speaker, content, category, emotion_tag, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (speaker, content.strip(), category, emotion_tag, now.isoformat(), expires),
        )


def cleanup_expired_memories() -> int:
    now_iso = now_sydney().isoformat()
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM memories WHERE expires_at <= ?", (now_iso,))
        conn.execute(
            "DELETE FROM bot_state WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now_iso,),
        )
        return cur.rowcount


def set_physical_state(state_desc: str, hours: float = 4.0) -> None:
    set_state("physical_state", state_desc, ttl_hours=hours)


def set_meal_mutex(meal_desc: str, hours: float = 2.0) -> None:
    set_state("recent_meal_mutex", meal_desc, ttl_hours=hours)


def lock_agreement(topic: str, hours: float = 3.0) -> None:
    set_state("active_agreement", topic, ttl_hours=hours)
    set_state("decision_hook_open", "0", ttl_hours=None)


def unlock_agreement_done(done_note: str) -> None:
    set_state("active_agreement", "", ttl_hours=None)
    log_memory("System", f"約定已完成：{done_note}", category="routine", emotion_tag="neutral")


def open_decision_hook(minutes: int = 20) -> None:
    set_state("decision_hook_open", "1", ttl_hours=minutes / 60.0)


def check_and_claim_daily_greeting() -> str | None:
    now = now_sydney()
    today_str = now.strftime("%Y-%m-%d")

    if 8 <= now.hour < 11:
        if get_state("last_morning_greet_date") != today_str:
            set_state("last_morning_greet_date", today_str)
            return "morning"
    elif now.hour == 23 and now.minute >= 35:
        if get_state("last_night_greet_date") != today_str:
            set_state("last_night_greet_date", today_str)
            return "night"
    return None


def trigger_routine_action(action_key: str | None = None) -> tuple[bool, str]:
    now = now_sydney()
    today_str = now.strftime("%Y-%m-%d")
    phys = get_state("physical_state", "")

    if not action_key:
        candidates = ["water"]
        if any(k in phys for k in ["腸胃", "肚子", "拉肚子"]):
            candidates.append("toilet_upset")
        else:
            candidates.append("toilet")

        if any(k in phys for k in ["累", "睡", "淺眠", "低血糖", "頭暈", "著涼", "那個"]):
            candidates.append("tired_rest")

        if (7 <= now.hour <= 9) or (17 <= now.hour <= 19):
            candidates.append("feed_mochi")

        random.shuffle(candidates)
        for cand in candidates:
            ok, msg = trigger_routine_action(cand)
            if ok:
                return True, msg
        return False, ""

    cfg = ROUTINE_ACTIONS.get(action_key)
    if not cfg:
        return False, ""

    count_key = f"routine_count_{action_key}_{today_str}"
    last_key = f"routine_last_{action_key}"

    used_today = int(get_state(count_key, "0") or "0")
    if used_today >= cfg["daily_cap"]:
        return False, ""

    last_at_str = get_state(last_key, "")
    if last_at_str:
        try:
            elapsed_min = (now - datetime.fromisoformat(last_at_str)).total_seconds() / 60.0
            if elapsed_min < cfg["cooldown_min"]:
                return False, ""
        except ValueError:
            pass

    set_state(count_key, str(used_today + 1), ttl_hours=26)
    set_state(last_key, now.isoformat(), ttl_hours=26)

    if action_key in ("toilet", "toilet_upset"):
        set_state("just_went_toilet", "1", ttl_hours=5 / 60.0)

    msg = random.choice(cfg["messages"])
    log_memory("Reona", msg, category="routine", emotion_tag="neutral")
    return True, msg


def record_proactive_ping() -> dict:
    now = now_sydney()
    pings = int(get_state("unanswered_pings", "0") or "0") + 1
    set_state("unanswered_pings", str(pings))
    set_state("last_ping_at", now.isoformat())
    if pings >= 3:
        set_state("is_sulking", "1")
    return {"unanswered_pings": pings, "is_sulking": pings >= 3}


def on_nick_replied(shared_story: bool = False) -> bool:
    now = now_sydney()
    was_sulking = get_state("is_sulking", "0") == "1"
    set_state("unanswered_pings", "0")
    set_state("last_nick_reply_at", now.isoformat())
    if was_sulking:
        set_state("is_sulking", "0")
    return was_sulking


def morning_wakeup_reset() -> bool:
    now = now_sydney()
    if not (8 <= now.hour < 12):
        return False
    today_str = now.strftime("%Y-%m-%d")
    if get_state("last_wakeup_date") == today_str:
        return False
    set_state("last_wakeup_date", today_str)
    set_state("unanswered_pings", "0")
    set_state("is_sulking", "0")
    cleanup_expired_memories()
    return True


def compute_snapshot() -> str:
    now = now_sydney()
    time_str = now.strftime("%Y-%m-%d %H:%M (%A)")
    phys = get_state("physical_state", "")
    meal = get_state("recent_meal_mutex", "")
    agree = get_state("active_agreement", "")
    sulking = get_state("is_sulking", "0") == "1"
    just_toilet = get_state("just_went_toilet", "0") == "1"

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT speaker, content, category, emotion_tag, created_at
            FROM memories
            WHERE expires_at > ?
            ORDER BY id DESC
            LIMIT 8
            """,
            (now.isoformat(),),
        ).fetchall()

    lines = [f"【雪梨時間】{time_str}（時區 Australia/Sydney；00:00~08:00 睡覺勿擾）"]
    if phys:
        lines.append(f"【Reona 身體狀態（鎖定中）】{phys}（請符合此狀態反應，別硬撐）")
    if just_toilet:
        lines.append("【剛發生的動作】Reona 剛從廁所出來，若對話接上可自然帶過")
    if meal:
        lines.append(f"【進食冷卻（2小時內勿重複吃喝）】{meal}")
    if agree:
        lines.append(f"【已達成共識（勿再反覆問要不要）】{agree}")
    if sulking:
        lines.append("【當前情緒】前面密了 Nick 3 次都沒回，目前有點傲嬌小生氣")

    if rows:
        lines.append("【近期記憶（僅供背景參考，勿照抄覆誦）】")
        for r in reversed(rows):
            emo = f"[{r['emotion_tag']}]" if r["emotion_tag"] != "neutral" else ""
            lines.append(f"- {r['speaker']}{emo}: {r['content']}")

    return "\n".join(lines)
