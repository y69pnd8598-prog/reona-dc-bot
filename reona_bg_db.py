import os
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


def _ensure_columns(conn: sqlite3.Connection, table: str, expected_cols: dict[str, str]) -> None:
    existing = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
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
                location TEXT DEFAULT '雪梨住處畫桌前',
                physical_state TEXT DEFAULT '正常',
                emotional_state TEXT DEFAULT '平穩',
                unanswered_pings INTEGER DEFAULT 0,
                is_sulking INTEGER DEFAULT 0,
                pending_decision_until TEXT,
                agreed_plan TEXT,
                agreed_plan_slot TEXT,
                last_meal_info TEXT,
                last_meal_until TEXT
            );

            INSERT OR IGNORE INTO runtime_state (id)
            VALUES (1);

            CREATE TABLE IF NOT EXISTS daily_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                date_str TEXT NOT NULL,
                hour_int INTEGER NOT NULL,
                speaker TEXT NOT NULL,
                category TEXT NOT NULL, -- 'routine', 'notable_event', 'agreement'
                emotion_tag TEXT DEFAULT 'neutral', -- 'happy', 'sad', 'neutral'
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
                "location": "TEXT DEFAULT '雪梨住處畫桌前'",
                "physical_state": "TEXT DEFAULT '正常'",
                "emotional_state": "TEXT DEFAULT '平穩'",
                "unanswered_pings": "INTEGER DEFAULT 0",
                "is_sulking": "INTEGER DEFAULT 0",
                "pending_decision_until": "TEXT",
                "agreed_plan": "TEXT",
                "agreed_plan_slot": "TEXT",
                "last_meal_info": "TEXT",
                "last_meal_until": "TEXT",
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


def morning_wakeup_reset() -> dict:
    """
    Runs on startup/wake-up (08:00 Sydney time).
    - Resets transient physical states & yesterday's routine clutter before 18:00.
    - Retains memories from 18:00–23:59 yesterday tagged as [昨晚], plus notable emotional events.
    """
    init_db()
    now = now_sydney()
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    with get_conn() as conn:
        state = dict(conn.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone())

        if (state.get("last_active_date") or "") != today_str:
            # 1. Delete routine chatter older than yesterday 18:00
            conn.execute(
                """
                DELETE FROM daily_memories
                WHERE date_str < ?
                   OR (date_str = ? AND hour_int < 18 AND category = 'routine')
                """,
                (yesterday_str, yesterday_str),
            )

            # 2. Mark remaining yesterday memories (18:00+ or notable emotional events)
            conn.execute(
                """
                UPDATE daily_memories
                SET is_overnight_kept = 1
                WHERE date_str = ?
                """,
                (yesterday_str,),
            )

            # 3. Clean expired nick notes
            conn.execute(
                "DELETE FROM nick_notes WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now.isoformat(),),
            )

            # 4. Reset daily physical/agreement flags while keeping ongoing sulk if unanswered
            conn.execute(
                """
                UPDATE runtime_state
                SET last_active_date = ?,
                    location = '雪梨住處客廳',
                    physical_state = '剛睡醒、精神還不錯',
                    pending_decision_until = NULL,
                    agreed_plan = NULL,
                    agreed_plan_slot = NULL,
                    last_meal_info = NULL,
                    last_meal_until = NULL
                WHERE id = 1
                """,
                (today_str,),
            )

        return dict(conn.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone())


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
    clean_content = de_pronoun_text(speaker, raw_content)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO daily_memories
            (created_at, date_str, hour_int, speaker, category, emotion_tag, content)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now.isoformat(),
                now.strftime("%Y-%m-%d"),
                now.hour,
                speaker,
                category,
                emotion_tag,
                clean_content,
            ),
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
        if state.get("last_meal_until") and state["last_meal_until"] < now.isoformat():
            conn.execute(
                "UPDATE runtime_state SET last_meal_info = NULL, last_meal_until = NULL WHERE id = 1"
            )
            state["last_meal_info"] = None

        if state.get("pending_decision_until") and state["pending_decision_until"] < now.isoformat():
            conn.execute("UPDATE runtime_state SET pending_decision_until = NULL WHERE id = 1")
            state["pending_decision_until"] = None

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
        f"【Reona 當前位置與狀態】{state.get('location') or '雪梨住處畫桌前'}｜身體：{state.get('physical_state') or '正常'}｜情緒：{state.get('emotional_state') or '平穩'}",
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
