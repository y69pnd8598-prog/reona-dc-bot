import json
import os
import random
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
DB_PATH = os.getenv("REONA_DB_PATH", "reona_bg.db")


def now_sydney() -> datetime:
    return datetime.now(SYDNEY_TZ)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_columns(conn: sqlite3.Connection, table: str, expected_cols: dict[str, str]) -> None:
    existing = _table_columns(conn, table)
    for col_name, col_def in expected_cols.items():
        if col_name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def};")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_active_date TEXT,
                morning_greeted_date TEXT,
                night_greeted_date TEXT,
                location TEXT DEFAULT '雪梨住處畫桌前',
                physical_state TEXT DEFAULT '正常',
                physical_until TEXT,
                emotional_state TEXT DEFAULT '平穩',
                unanswered_pings INTEGER DEFAULT 0,
                is_sulking INTEGER DEFAULT 0,
                pending_decision_until TEXT,
                agreed_plan TEXT,
                agreed_plan_slot TEXT,
                last_meal_info TEXT,
                last_meal_until TEXT,
                routine_tracker TEXT DEFAULT '{}',
                away_reason TEXT,
                away_until TEXT
            );

            INSERT OR IGNORE INTO runtime_state (id)
            VALUES (1);

            CREATE TABLE IF NOT EXISTS daily_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                date_str TEXT NOT NULL,
                hour_int INTEGER NOT NULL,
                speaker TEXT NOT NULL,
                category TEXT NOT NULL,
                emotion_tag TEXT DEFAULT 'neutral',
                content TEXT NOT NULL,
                is_overnight_kept INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS nick_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                content TEXT NOT NULL,
                expires_at TEXT
            );
            """
        )

        _ensure_columns(
            conn,
            "runtime_state",
            {
                "last_active_date": "TEXT",
                "morning_greeted_date": "TEXT",
                "night_greeted_date": "TEXT",
                "location": "TEXT DEFAULT '雪梨住處畫桌前'",
                "physical_state": "TEXT DEFAULT '正常'",
                "physical_until": "TEXT",
                "emotional_state": "TEXT DEFAULT '平穩'",
                "unanswered_pings": "INTEGER DEFAULT 0",
                "is_sulking": "INTEGER DEFAULT 0",
                "pending_decision_until": "TEXT",
                "agreed_plan": "TEXT",
                "agreed_plan_slot": "TEXT",
                "last_meal_info": "TEXT",
                "last_meal_until": "TEXT",
                "routine_tracker": "TEXT DEFAULT '{}'",
                "away_reason": "TEXT",
                "away_until": "TEXT",
            },
        )

        _ensure_columns(
            conn,
            "daily_memories",
            {
                "date_str": "TEXT DEFAULT ''",
                "hour_int": "INTEGER DEFAULT 0",
                "speaker": "TEXT DEFAULT 'System'",
                "category": "TEXT DEFAULT 'routine'",
                "emotion_tag": "TEXT DEFAULT 'neutral'",
                "content": "TEXT DEFAULT ''",
                "is_overnight_kept": "INTEGER DEFAULT 0",
            },
        )

        _ensure_columns(
            conn,
            "nick_notes",
            {
                "expires_at": "TEXT",
            },
        )


def morning_wakeup_reset() -> dict:
    """
    Runs on startup/wake-up (08:00+ Sydney time).
    - Resets transient physical states, daily routine counters & yesterday's clutter before 18:00.
    - Retains memories from 18:00–23:59 yesterday tagged as [昨晚], plus notable emotional events.
    """
    init_db()
    now = now_sydney()
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    with get_conn() as conn:
        state = dict(conn.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone())

        if (state.get("last_active_date") or "") != today_str:
            mem_cols = _table_columns(conn, "daily_memories")
            date_expr = "COALESCE(NULLIF(date_str, ''), mem_date)" if "mem_date" in mem_cols else "date_str"

            conn.execute(
                f"""
                DELETE FROM daily_memories
                WHERE {date_expr} < ?
                   OR ({date_expr} = ? AND hour_int < 18 AND category = 'routine')
                """,
                (yesterday_str, yesterday_str),
            )

            conn.execute(
                f"""
                UPDATE daily_memories
                SET is_overnight_kept = 1
                WHERE {date_expr} = ?
                """,
                (yesterday_str,),
            )

            conn.execute(
                "DELETE FROM nick_notes WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now.isoformat(),),
            )

            conn.execute(
                """
                UPDATE runtime_state
                SET last_active_date = ?,
                    location = '雪梨住處客廳',
                    physical_state = '剛睡醒、精神還不錯',
                    physical_until = NULL,
                    pending_decision_until = NULL,
                    agreed_plan = NULL,
                    agreed_plan_slot = NULL,
                    last_meal_info = NULL,
                    last_meal_until = NULL,
                    routine_tracker = '{}',
                    away_reason = NULL,
                    away_until = NULL
                WHERE id = 1
                """,
                (today_str,),
            )

        return dict(conn.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone())


def check_and_claim_daily_greeting() -> str | None:
    """
    Returns 'morning' once per day between 08:00 and 10:30 Sydney time,
    or 'night' once per day between 23:25 and 23:59 Sydney time.
    """
    state = morning_wakeup_reset()
    now = now_sydney()
    today_str = now.strftime("%Y-%m-%d")
    minute_of_day = now.hour * 60 + now.minute

    # Morning window: 08:00 - 10:30
    if (8 * 60) <= minute_of_day <= (10 * 60 + 30):
        if (state.get("morning_greeted_date") or "") != today_str:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE runtime_state SET morning_greeted_date = ?, location = '雪梨住處客廳' WHERE id = 1",
                    (today_str,),
                )
            return "morning"

    # Goodnight window: 23:25 - 23:59
    if (23 * 60 + 25) <= minute_of_day <= (23 * 60 + 59):
        if (state.get("night_greeted_date") or "") != today_str:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE runtime_state SET night_greeted_date = ?, location = '雪梨住處房間床上' WHERE id = 1",
                    (today_str,),
                )
            return "night"

    return None


def set_physical_state(state_desc: str, hours: float = 4.0) -> None:
    init_db()
    until = (now_sydney() + timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE runtime_state
            SET physical_state = ?,
                physical_until = ?
            WHERE id = 1
            """,
            (state_desc, until),
        )


def de_pronoun_text(speaker: str, raw_text: str) -> str:
    """Normalizes first/second person pronouns into third-person facts to prevent role reversal."""
    text = raw_text.strip()
    if speaker == "Reona":
        text = text.replace("我", "Reona").replace("你", "Nick")
    else:
        text = text.replace("我", "Nick").replace("你", "Reona")
    return text


def log_memory(
    speaker: str,
    raw_content: str,
    category: str = "routine",
    emotion_tag: str = "neutral",
) -> None:
    init_db()
    now = now_sydney()
    today_str = now.strftime("%Y-%m-%d")
    clean_content = de_pronoun_text(speaker, raw_content)

    with get_conn() as conn:
        mem_cols = _table_columns(conn, "daily_memories")
        row_data = {
            "created_at": now.isoformat(),
            "date_str": today_str,
            "hour_int": now.hour,
            "speaker": speaker,
            "category": category,
            "emotion_tag": emotion_tag,
            "content": clean_content,
        }
        if "mem_date" in mem_cols:
            row_data["mem_date"] = today_str

        cols = list(row_data.keys())
        placeholders = ", ".join(["?"] * len(cols))
        col_sql = ", ".join(cols)
        conn.execute(
            f"INSERT INTO daily_memories ({col_sql}) VALUES ({placeholders})",
            [row_data[c] for c in cols],
        )


def open_decision_hook(minutes: int = 20) -> None:
    init_db()
    until = (now_sydney() + timedelta(minutes=minutes)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE runtime_state SET pending_decision_until = ? WHERE id = 1",
            (until,),
        )


def lock_agreement(plan_text: str, slot: str = "next_action") -> None:
    init_db()
    clean_plan = de_pronoun_text("Nick", plan_text)
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE runtime_state
            SET agreed_plan = ?,
                agreed_plan_slot = ?,
                pending_decision_until = NULL
            WHERE id = 1
            """,
            (clean_plan, slot),
        )
    log_memory("System", f"雙方達成共識：{clean_plan}", category="agreement")


def unlock_agreement_done(completion_note: str) -> None:
    init_db()
    with get_conn() as conn:
        row = conn.execute("SELECT agreed_plan FROM runtime_state WHERE id = 1").fetchone()
        if row and row["agreed_plan"]:
            log_memory("Reona", f"已完成約定：{row['agreed_plan']}（{completion_note}）", category="routine")
        conn.execute(
            """
            UPDATE runtime_state
            SET agreed_plan = NULL,
                agreed_plan_slot = NULL
            WHERE id = 1
            """,
        )


def set_meal_mutex(meal_desc: str, hours: int = 2) -> None:
    init_db()
    until = (now_sydney() + timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE runtime_state
            SET last_meal_info = ?,
                last_meal_until = ?
            WHERE id = 1
            """,
            (meal_desc, until),
        )


def _get_routine_limits(physical_state: str) -> dict:
    """Returns cooldown (minutes) and daily max counts adjusted by Reona's physical condition."""
    is_stomach_upset = any(k in physical_state for k in ["腸胃", "肚子", "拉肚子", "不舒服"])
    is_tired = any(k in physical_state for k in ["累", "想睡", "趕稿", "熬夜"])

    if is_stomach_upset:
        return {
            "toilet": {"cooldown_min": 45, "max_daily": 7},
            "drink_water": {"cooldown_min": 60, "max_daily": 8},
            "feed_cat": {"cooldown_min": 360, "max_daily": 2},
        }
    if is_tired:
        return {
            "toilet": {"cooldown_min": 150, "max_daily": 4},
            "drink_water": {"cooldown_min": 60, "max_daily": 7},
            "feed_cat": {"cooldown_min": 360, "max_daily": 2},
        }
    return {
        "toilet": {"cooldown_min": 150, "max_daily": 4},
        "drink_water": {"cooldown_min": 90, "max_daily": 6},
        "feed_cat": {"cooldown_min": 360, "max_daily": 2},
    }


def _is_cat_feeding_window(now: datetime) -> bool:
    minute_of_day = now.hour * 60 + now.minute
    morning_ok = (8 * 60) <= minute_of_day <= (10 * 60 + 30)
    evening_ok = (17 * 60 + 30) <= minute_of_day <= (20 * 60 + 30)
    return morning_ok or evening_ok


def get_eligible_routines() -> list[str]:
    """Checks cooldowns, daily caps, meal mutex, and feeding windows."""
    state = morning_wakeup_reset()
    now = now_sydney()
    phys = state.get("physical_state") or "正常"
    limits = _get_routine_limits(phys)

    try:
        tracker = json.loads(state.get("routine_tracker") or "{}")
    except Exception:
        tracker = {}

    eligible: list[str] = []
    for action, rule in limits.items():
        if action == "feed_cat" and not _is_cat_feeding_window(now):
            continue
        if action == "drink_water" and state.get("last_meal_info") and "喝" in state["last_meal_info"]:
            continue

        info = tracker.get(action, {"count": 0, "last_at": None})
        if info.get("count", 0) >= rule["max_daily"]:
            continue

        last_at_str = info.get("last_at")
        if last_at_str:
            try:
                last_at = datetime.fromisoformat(last_at_str)
                if (now - last_at).total_seconds() < rule["cooldown_min"] * 60:
                    continue
            except Exception:
                pass

        eligible.append(action)

    return eligible


def trigger_routine_action(action: str | None = None) -> tuple[str | None, str | None]:
    """
    Executes an eligible routine action ('toilet', 'drink_water', 'feed_cat'),
    updates daily counts/cooldowns, and returns (action_key, natural_message).
    """
    state = morning_wakeup_reset()
    now = now_sydney()
    phys = state.get("physical_state") or "正常"
    is_stomach_upset = any(k in phys for k in ["腸胃", "肚子", "拉肚子", "不舒服"])
    is_tired = any(k in phys for k in ["累", "想睡", "趕稿", "熬夜"])

    eligible = get_eligible_routines()
    if not eligible:
        return None, None

    chosen = action if (action and action in eligible) else random.choice(eligible)

    try:
        tracker = json.loads(state.get("routine_tracker") or "{}")
    except Exception:
        tracker = {}

    prev = tracker.get(chosen, {"count": 0, "last_at": None})
    tracker[chosen] = {
        "count": prev.get("count", 0) + 1,
        "last_at": now.isoformat(),
    }

    away_reason = None
    away_until = None

    if chosen == "toilet":
        away_reason = "去上廁所"
        away_until = (now + timedelta(minutes=5)).isoformat()
        if is_stomach_upset:
            msgs = [
                "肚子又有點咕嚕叫，我先去跑個廁所喔",
                "腸胃不太爭氣，我去一下廁所馬上回來",
            ]
        else:
            msgs = [
                "我先去上個廁所，等我一下",
                "我去一下廁所喔，馬上回來",
            ]
        chosen_msg = random.choice(msgs)
        loc = "雪梨住處浴室"

    elif chosen == "drink_water":
        away_reason = "去廚房倒水"
        away_until = (now + timedelta(minutes=2)).isoformat()
        if is_stomach_upset:
            msgs = [
                "我先去廚房倒杯溫水喝，順便讓肚子暖一下",
                "嘴巴有點乾，去倒杯溫水，不敢喝冰的",
            ]
        elif is_tired:
            msgs = [
                "畫到眼睛有點酸，先去倒杯水走動一下",
                "我去廚房倒杯水提個神，順便伸個懶腰",
            ]
        else:
            msgs = [
                "我先去廚房倒杯水喝喔",
                "嘴巴有點乾，去倒個水順便看看麻糬在幹嘛",
            ]
        chosen_msg = random.choice(msgs)
        loc = "雪梨住處廚房"

    else:  # feed_cat
        away_reason = "在餵麻糬吃飯"
        away_until = (now + timedelta(minutes=4)).isoformat()
        msgs = [
            "麻糬一直在我腳邊喵喵叫討飯了，我先去餵牠",
            "時間差不多了，我去幫麻糬倒飼料跟換水",
        ]
        chosen_msg = random.choice(msgs)
        loc = "雪梨住處客廳"

    with get_conn() as conn:
        conn.execute(
            """
            UPDATE runtime_state
            SET routine_tracker = ?,
                location = ?,
                away_reason = ?,
                away_until = ?
            WHERE id = 1
            """,
            (json.dumps(tracker, ensure_ascii=False), loc, away_reason, away_until),
        )

    log_memory("Reona", chosen_msg, category="routine", emotion_tag="neutral")
    return chosen, chosen_msg


def record_proactive_ping() -> dict:
    init_db()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT unanswered_pings FROM runtime_state WHERE id = 1"
        ).fetchone()
        new_count = ((row["unanswered_pings"] if row else 0) or 0) + 1
        sulking = 1 if new_count >= 3 else 0
        emo = "有點委屈傲嬌小生氣（連續多次密 Nick 沒回）" if sulking else "平穩"
        conn.execute(
            """
            UPDATE runtime_state
            SET unanswered_pings = ?,
                is_sulking = ?,
                emotional_state = ?
            WHERE id = 1
            """,
            (new_count, sulking, emo),
        )
        return dict(conn.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone())


def on_nick_replied(shared_story: bool = False) -> bool:
    """Resets unanswered ping counter when Nick replies. Returns True if she was sulking."""
    init_db()
    with get_conn() as conn:
        row = conn.execute("SELECT is_sulking FROM runtime_state WHERE id = 1").fetchone()
        was_sulking = bool(row and row["is_sulking"])
        new_emo = "剛被 Nick 的分享轉移注意力、嘴硬但消氣中" if (was_sulking and shared_story) else "平穩"
        conn.execute(
            """
            UPDATE runtime_state
            SET unanswered_pings = 0,
                is_sulking = 0,
                emotional_state = ?
            WHERE id = 1
            """,
            (new_emo,),
        )
        return was_sulking


def compute_snapshot() -> str:
    state = morning_wakeup_reset()
    now = now_sydney()

    with get_conn() as conn:
        if state.get("physical_until") and state["physical_until"] < now.isoformat():
            conn.execute(
                "UPDATE runtime_state SET physical_state = '正常', physical_until = NULL WHERE id = 1"
            )
            state["physical_state"] = "正常"

        if state.get("last_meal_until") and state["last_meal_until"] < now.isoformat():
            conn.execute(
                "UPDATE runtime_state SET last_meal_info = NULL, last_meal_until = NULL WHERE id = 1"
            )
            state["last_meal_info"] = None

        if state.get("pending_decision_until") and state["pending_decision_until"] < now.isoformat():
            conn.execute("UPDATE runtime_state SET pending_decision_until = NULL WHERE id = 1")
            state["pending_decision_until"] = None

        just_returned_note = ""
        if state.get("away_reason"):
            if state.get("away_until") and state["away_until"] >= now.isoformat():
                just_returned_note = f"（剛剛才說「{state['away_reason']}」，現在剛好回來看到訊息，可自然順口提一句剛回來）"
            conn.execute(
                "UPDATE runtime_state SET away_reason = NULL, away_until = NULL, location = '雪梨住處畫桌前' WHERE id = 1"
            )

        memories = conn.execute(
            """
            SELECT date_str, category, emotion_tag, content, is_overnight_kept
            FROM daily_memories
            ORDER BY id DESC
            LIMIT 15
            """
        ).fetchall()

    mem_lines = []
    for m in reversed(memories):
        prefix = "[昨晚] " if m["is_overnight_kept"] else ("[今日重要] " if m["category"] != "routine" else "")
        emo = f"({m['emotion_tag']}) " if m["emotion_tag"] != "neutral" else ""
        mem_lines.append(f"- {prefix}{emo}{m['content']}")

    lines = [
        f"【雪梨當地時間】{now.strftime('%Y-%m-%d %H:%M')}（清醒時段 08:00–24:00）",
        f"【Reona 當前位置與狀態】{state.get('location') or '雪梨住處畫桌前'}｜身體：{state.get('physical_state') or '正常'}｜情緒：{state.get('emotional_state') or '平穩'}{just_returned_note}",
    ]

    if state.get("agreed_plan"):
        lines.append(f"【最高優先級｜今日共識鎖定】{state['agreed_plan']}（務必記住並依此推進，未完成前不可推翻）")

    if state.get("last_meal_info"):
        lines.append(f"【已進食鎖定】Reona 剛吃過：{state['last_meal_info']}（目前不餓，禁止說還沒吃）")

    if state.get("pending_decision_until"):
        lines.append("【等待 Nick 拍板中】Reona 剛把決定權交給 Nick，若 Nick 給出具體選擇請順從並記住。")

    if mem_lines:
        lines.append("【近期事實與情緒記憶（已去代詞化）】\n" + "\n".join(mem_lines))

    return "\n".join(lines)
