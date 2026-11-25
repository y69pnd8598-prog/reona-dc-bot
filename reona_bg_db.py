import datetime
import json
import math
import os
import random
import re
import sqlite3
from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
DEFAULT_DB_PATH = os.getenv("REONA_DB_PATH", "reona_bg.db")

DEFAULT_PHASES: Dict[str, Dict[str, str]] = {
    "morning": {
        "location": "雪梨住處畫桌前",
        "positions": "Reona 剛坐上工作椅；麻糬在窗台曬太陽",
        "event": "泡好熱烏龍茶，正在整理今天的插畫進度",
        "items": "保溫杯、數位筆、草稿本",
    },
    "afternoon": {
        "location": "雪梨住處畫桌前（冷氣房）",
        "positions": "Reona 盯著螢幕修構圖；麻糬趴在畫桌角落睡覺",
        "event": "下午修草稿比例，室內冷氣偏涼",
        "items": "數位板、色票本、溫開水",
    },
    "evening": {
        "location": "客廳沙發",
        "positions": "Reona 剛離開畫桌伸懶腰；麻糬跑來蹭腳邊",
        "event": "傍晚收工準備吃飯與放空",
        "items": "馬克杯、抱枕、薄毯",
    },
    "night": {
        "location": "房間床邊",
        "positions": "Reona 靠在床頭；麻糬縮在床尾",
        "event": "晚上在家休息放空，準備睡覺",
        "items": "溫開水、手機、薄毯",
    },
}


class ReonaBGDB:
    """Reona 的世界狀態引擎 ＋ 輕量相似度檢索資料庫（單檔 SQLite）。"""

    SIMILARITY_THRESHOLD = 0.26

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._init_db()
        self._seed_defaults_if_empty()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    mainline_date TEXT NOT NULL DEFAULT '',
                    mainline_key TEXT NOT NULL DEFAULT '',
                    mainline_title TEXT NOT NULL DEFAULT '',
                    gi_cause TEXT NOT NULL DEFAULT '',
                    gi_triggered_today INTEGER NOT NULL DEFAULT 0,
                    mood TEXT NOT NULL DEFAULT 'normal',
                    mood_until REAL NOT NULL DEFAULT 0,
                    recovery_until REAL NOT NULL DEFAULT 0,
                    gi_status TEXT NOT NULL DEFAULT 'normal',
                    gi_until REAL NOT NULL DEFAULT 0,
                    illness_status TEXT NOT NULL DEFAULT 'normal',
                    illness_until REAL NOT NULL DEFAULT 0,
                    restroom_until REAL NOT NULL DEFAULT 0,
                    pending_request_until REAL NOT NULL DEFAULT 0,
                    mentioned_slots TEXT NOT NULL DEFAULT '',
                    done_actions_today TEXT NOT NULL DEFAULT '',
                    last_nick_ts REAL NOT NULL DEFAULT 0,
                    last_reona_ts REAL NOT NULL DEFAULT 0,
                    last_proactive_ts REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS daily_mainlines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mainline_key TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    is_weekend INTEGER NOT NULL DEFAULT 0,
                    phases_json TEXT NOT NULL DEFAULT '{}',
                    evening_mood TEXT NOT NULL DEFAULT 'normal',
                    gi_risk REAL NOT NULL DEFAULT 0.25,
                    gi_cause_hint TEXT NOT NULL DEFAULT '',
                    weight INTEGER NOT NULL DEFAULT 10
                );

                CREATE TABLE IF NOT EXISTS canned_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    sub_state TEXT NOT NULL DEFAULT 'any',
                    text TEXT NOT NULL,
                    weight INTEGER NOT NULL DEFAULT 10
                );

                CREATE TABLE IF NOT EXISTS life_snippets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    polarity TEXT NOT NULL DEFAULT 'neutral',
                    mood_fit TEXT NOT NULL DEFAULT 'any',
                    time_slot TEXT NOT NULL DEFAULT 'any',
                    is_weekend INTEGER NOT NULL DEFAULT -1,
                    content TEXT NOT NULL,
                    weight INTEGER NOT NULL DEFAULT 10
                );

                CREATE TABLE IF NOT EXISTS story_arcs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    arc_key TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    current_step INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS daily_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mem_date TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'life',
                    content TEXT NOT NULL,
                    importance INTEGER NOT NULL DEFAULT 5,
                    created_at REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS nick_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    content TEXT NOT NULL,
                    updated_at REAL NOT NULL DEFAULT 0
                );
                """
            )

            runtime_cols = {r[1] for r in conn.execute("PRAGMA table_info(runtime_state)").fetchall()}
            runtime_migrations = {
                "recovery_until": "REAL NOT NULL DEFAULT 0",
                "gi_status": "TEXT NOT NULL DEFAULT 'normal'",
                "gi_until": "REAL NOT NULL DEFAULT 0",
                "illness_status": "TEXT NOT NULL DEFAULT 'normal'",
                "illness_until": "REAL NOT NULL DEFAULT 0",
                "restroom_until": "REAL NOT NULL DEFAULT 0",
                "pending_request_until": "REAL NOT NULL DEFAULT 0",
                "mentioned_slots": "TEXT NOT NULL DEFAULT ''",
                "done_actions_today": "TEXT NOT NULL DEFAULT ''",
                "last_nick_ts": "REAL NOT NULL DEFAULT 0",
                "last_reona_ts": "REAL NOT NULL DEFAULT 0",
                "last_proactive_ts": "REAL NOT NULL DEFAULT 0",
                "updated_at": "REAL NOT NULL DEFAULT 0",
            }
            for col_name, col_def in runtime_migrations.items():
                if col_name not in runtime_cols:
                    conn.execute(f"ALTER TABLE runtime_state ADD COLUMN {col_name} {col_def}")

            mainline_cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_mainlines)").fetchall()}
            mainline_migrations = {
                "phases_json": "TEXT NOT NULL DEFAULT '{}'",
                "evening_mood": "TEXT NOT NULL DEFAULT 'normal'",
                "gi_risk": "REAL NOT NULL DEFAULT 0.25",
                "gi_cause_hint": "TEXT NOT NULL DEFAULT ''",
                "weight": "INTEGER NOT NULL DEFAULT 10",
            }
            for col_name, col_def in mainline_migrations.items():
                if col_name not in mainline_cols:
                    conn.execute(f"ALTER TABLE daily_mainlines ADD COLUMN {col_name} {col_def}")

            conn.execute(
                "INSERT OR IGNORE INTO runtime_state (id, updated_at) VALUES (1, ?)",
                (datetime.datetime.now(SYDNEY_TZ).timestamp(),),
            )

    def _seed_defaults_if_empty(self) -> None:
        with self._connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM daily_mainlines").fetchone()[0] == 0:
                mainlines = [
                    (
                        "work_sketch_rush",
                        "趕商業插畫草稿",
                        0,
                        json.dumps(
                            {
                                "morning": {
                                    "location": "住處畫桌前",
                                    "positions": "Reona 剛坐上工作椅；麻糬在窗台曬太陽",
                                    "event": "泡好熱烏龍茶，正在整理今天的插畫進度",
                                    "items": "保溫杯、數位筆、草稿本",
                                },
                                "afternoon": {
                                    "location": "住處畫桌前（冷氣房）",
                                    "positions": "Reona 盯著螢幕修構圖；麻糬趴在畫桌角落睡覺",
                                    "event": "下午修草稿比例，室內冷氣偏涼",
                                    "items": "數位板、色票本、溫開水",
                                },
                                "evening": {
                                    "location": "客廳沙發",
                                    "positions": "Reona 離開畫桌伸懶腰；麻糬跑來蹭腳邊",
                                    "event": "傍晚收工準備吃飯與放空",
                                    "items": "馬克杯、抱枕、薄毯",
                                },
                                "night": {
                                    "location": "房間床邊",
                                    "positions": "Reona 靠在床頭；麻糬縮在床尾",
                                    "event": "晚上在家休息放空，準備睡覺",
                                    "items": "溫開水、手機、薄毯",
                                },
                            },
                            ensure_ascii=False,
                        ),
                        "tired",
                        0.45,
                        "下午吹冷氣又空腹喝冰的",
                        12,
                    ),
                    (
                        "work_final_delivery",
                        "交最終完稿日",
                        0,
                        json.dumps(
                            {
                                "morning": {
                                    "location": "住處畫桌前",
                                    "positions": "Reona 專心檢查圖層；麻糬蹲在書架上看著她",
                                    "event": "做完稿交件前的最後細節檢查",
                                    "items": "數位筆、保溫杯、眼鏡",
                                },
                                "afternoon": {
                                    "location": "住處畫桌前",
                                    "positions": "Reona 剛寄出檔案靠在椅背上；麻糬在抓貓抓板",
                                    "event": "把完稿寄出去了，正在等回覆",
                                    "items": "筆電、手機、熱菊花茶",
                                },
                                "evening": {
                                    "location": "廚房與餐桌",
                                    "positions": "Reona 在弄晚餐；麻糬在旁邊轉圈",
                                    "event": "傍晚準備吃飯，整個人鬆一口氣",
                                    "items": "餐具、手機、溫開水",
                                },
                                "night": {
                                    "location": "客廳沙發",
                                    "positions": "Reona 窩在沙發上；麻糬趴在腿邊",
                                    "event": "晚上在家放空摸貓，不想碰工作了",
                                    "items": "抱枕、薄毯、手機",
                                },
                            },
                            ensure_ascii=False,
                        ),
                        "happy",
                        0.30,
                        "前幾天作息亂掉，一鬆懈腸胃就鬧脾氣",
                        12,
                    ),
                    (
                        "wknd_grocery_walk",
                        "白天出門採買補貨",
                        1,
                        json.dumps(
                            {
                                "morning": {
                                    "location": "餐桌旁",
                                    "positions": "Reona 慢慢吃早餐；麻糬在地板翻肚",
                                    "event": "週末早上邊吃早餐邊看陽光",
                                    "items": "熱茶、烤吐司、手機",
                                },
                                "afternoon": {
                                    "location": "附近超市",
                                    "positions": "Reona 在超市挑食材；麻糬在家睡午覺",
                                    "event": "下午補買下週食材跟貓砂",
                                    "items": "購物袋、清單、溫水",
                                },
                                "evening": {
                                    "location": "廚房與客廳",
                                    "positions": "Reona 提東西回家；麻糬衝到門口迎接",
                                    "event": "傍晚把東西收好，準備吃飯休息",
                                    "items": "溫水、馬克杯、薄外套",
                                },
                                "night": {
                                    "location": "客廳沙發",
                                    "positions": "Reona 在沙發滑手機；麻糬窩在旁邊",
                                    "event": "晚上待在家裡放空，準備早點休息",
                                    "items": "薄毯、抱枕、手機",
                                },
                            },
                            ensure_ascii=False,
                        ),
                        "happy",
                        0.35,
                        "下午外面太陽大貪喝冰的，傍晚又吹到風",
                        12,
                    ),
                    (
                        "wknd_home_mochi",
                        "白天整理家裡陪麻糬",
                        1,
                        json.dumps(
                            {
                                "morning": {
                                    "location": "住處工作室",
                                    "positions": "Reona 在整理桌邊；麻糬好奇鑽進紙箱",
                                    "event": "早上洗床單順便收拾桌邊",
                                    "items": "抹布、髮圈、溫開水",
                                },
                                "afternoon": {
                                    "location": "客廳地毯上",
                                    "positions": "Reona 坐在地毯上；麻糬被抓著梳毛",
                                    "event": "下午幫麻糬梳毛剪指甲",
                                    "items": "針梳、指甲剪、貓零食",
                                },
                                "evening": {
                                    "location": "客廳沙發",
                                    "positions": "Reona 泡好茶坐下；麻糬在旁邊打盹",
                                    "event": "傍晚家裡變整齊了，準備吃飯休息",
                                    "items": "熱烏龍茶、薄毯、抱枕",
                                },
                                "night": {
                                    "location": "房間床邊",
                                    "positions": "Reona 靠在枕頭上；麻糬縮在腳邊",
                                    "event": "晚上在房間聽音樂放空，準備睡覺",
                                    "items": "溫開水、耳機、手機",
                                },
                            },
                            ensure_ascii=False,
                        ),
                        "normal",
                        0.15,
                        "傍晚冷氣開太涼沒蓋肚子",
                        12,
                    ),
                ]
                conn.executemany(
                    """
                    INSERT INTO daily_mainlines (
                        mainline_key, title, is_weekend, phases_json,
                        evening_mood, gi_risk, gi_cause_hint, weight
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    mainlines,
                )

            if conn.execute("SELECT COUNT(*) FROM canned_lines").fetchone()[0] == 0:
                canned_rows = [
                    ("restroom", "normal", "去一下廁所，等我一下", 12),
                    ("restroom", "normal", "還在廁所喔，很快出來", 10),
                    ("restroom", "sick", "還在廁所裡，肚子又絞了", 12),
                    ("sick", "any", "肚子有點悶，先喝點溫水", 12),
                    ("sick", "any", "今天身體不太舒服，沒什麼力氣", 12),
                    ("sick", "any", "腸胃又鬧脾氣了，你別嫌我煩", 10),
                    ("tired", "any", "今天好累喔，想靠著你一下", 12),
                    ("tired", "any", "讓我喘口氣，有你在旁邊聽著好多了", 10),
                    ("low_effort", "normal", "嗯？怎麼突然這樣", 12),
                    ("low_effort", "normal", "在忙嗎，還是又在敷衍我", 10),
                    ("low_effort", "tired", "⋯好累喔，想跟你說說話", 10),
                    ("voice_msg", "normal", "現在不太方便聽語音，用打字的啦", 12),
                    ("voice_msg", "sick", "沒什麼力氣聽語音，先打字好嗎", 12),
                    ("video_req", "normal", "現在超邋遢不要啦", 12),
                    ("video_req", "sick", "現在沒力氣開視訊，想先乖乖躺著", 12),
                    ("request_declined", "normal", "好喔那沒關係，你先忙你的", 12),
                    ("request_declined", "sick", "好啦沒事，我先喝點溫水休息", 12),
                    ("fallback", "normal", "剛在忙手邊的事，你呢", 10),
                    ("fallback", "happy", "剛想到一件好笑的事想跟你说", 10),
                    ("fallback", "tired", "先休息一下，你陪我聊聊天好不好", 10),
                    ("fallback", "sick", "先躺一下喔，你會不會覺得我很麻煩", 10),
                ]
                conn.executemany(
                    "INSERT INTO canned_lines (category, sub_state, text, weight) VALUES (?, ?, ?, ?)",
                    canned_rows,
                )

            if conn.execute("SELECT COUNT(*) FROM life_snippets").fetchone()[0] == 0:
                snippet_rows = self._default_life_snippets()
                conn.executemany(
                    """
                    INSERT INTO life_snippets (topic, polarity, mood_fit, time_slot, is_weekend, content, weight)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    snippet_rows,
                )

            if conn.execute("SELECT COUNT(*) FROM story_arcs").fetchone()[0] == 0:
                sample_steps = [
                    "接到一份雪梨在地咖啡烘焙品牌的包裝插畫邀約，正在看需求。",
                    "草稿畫到一半卡在色調搭配，反覆試了幾組配色。",
                    "初稿剛寄出去給對方，這兩天等客戶回覆意見。",
                    "客戶對初稿很滿意只微調一點字體，準備進入完稿。",
                    "順利定稿收工，這個案子告一段落超有成就感。",
                ]
                conn.execute(
                    """
                    INSERT INTO story_arcs (arc_key, title, steps_json, current_step, is_active, updated_at)
                    VALUES (?, ?, ?, 0, 1, ?)
                    """,
                    (
                        "sydney_coffee_packaging",
                        "雪梨咖啡品牌包裝插畫案",
                        json.dumps(sample_steps, ensure_ascii=False),
                        datetime.datetime.now(SYDNEY_TZ).timestamp(),
                    ),
                )

            if conn.execute("SELECT COUNT(*) FROM daily_memories").fetchone()[0] == 0:
                now_dt = datetime.datetime.now(SYDNEY_TZ)
                conn.execute(
                    """
                    INSERT INTO daily_memories (mem_date, category, content, importance, created_at)
                    VALUES (?, 'life', '台北優渥溫情家庭長大的插畫師，跟家人感情很好，但小時候在學校受過委屈，現在帶著麻糬在雪梨靠自己生活。', 8, ?)
                    """,
                    (now_dt.strftime("%Y-%m-%d"), now_dt.timestamp()),
                )

    @staticmethod
    def _default_life_snippets() -> List[Tuple[str, str, str, str, int, str, int]]:
        return [
            (
                "childhood_bullying",
                "bad",
                "any",
                "night",
                -1,
                "小時候家裡環境其實不錯，但在學校反而常被同學排擠，連畫具被弄壞也不敢聲張，怕爸媽擔心或去學校鬧大，就自己默默吞了。",
                9,
            ),
            (
                "childhood_gi_teacher",
                "bad",
                "any",
                "any",
                -1,
                "小時候腸胃就很敏感、常跑廁所，結果被老師當眾酸嬌生慣養、毛病多，從那之後我只要不舒服就習慣硬撐，很怕別人覺得我很麻煩。",
                9,
            ),
            (
                "sydney_independence",
                "neutral",
                "any",
                "any",
                -1,
                "一個人來雪梨接案生活，其實也是想低調靠自己的本事站穩，不想一直靠家裡，也不想讓遠在台北的家人替我操心。",
                10,
            ),
            (
                "family_bond",
                "good",
                "any",
                "any",
                -1,
                "我跟台北的家人感情其實很好，爸媽很疼我，偶爾還會寄台灣的腸胃藥或小零食過來，只是我習慣報喜不報憂，不舒服很少跟家裡說。",
                11,
            ),
            (
                "family_care",
                "good",
                "happy",
                "evening",
                -1,
                "跟家裡聊麻糬跟生活瑣事的時候很放鬆，他們都很支持我在外面過自己的步調。",
                10,
            ),
            (
                "reliance_comfort",
                "good",
                "tired",
                "night",
                -1,
                "平常在外面習慣自己扛、嘴硬逞強，但跟你在一起時只要確定你不會嫌我煩，累的時候就會忍不住想黏著你討拍。",
                12,
            ),
            (
                "illness_clingy",
                "good",
                "any",
                "any",
                -1,
                "生理期或感冒、腸胃不舒服的時候，我其實特別沒力氣也特別想撒嬌，只敢在你面前把委屈說出來，你不會覺得我很麻煩吧？",
                12,
            ),
            (
                "gi_diet_habit",
                "neutral",
                "any",
                "any",
                -1,
                "我自己腸胃偏弱，太冰、太甜或太刺激的東西都不太敢亂碰，看到別人拿鐵又加奶又加糖，心裡都會默默替他的胃捏一把冷汗。",
                11,
            ),
            (
                "drawing_habit",
                "neutral",
                "normal",
                "any",
                0,
                "畫桌旁邊永遠放著保溫杯，冷掉就不再喝了；畫到卡關時會起來伸懶腰順便看麻糬發呆。",
                10,
            ),
            (
                "mochi_daily",
                "good",
                "happy",
                "any",
                -1,
                "麻糬剛剛又踩過我的畫桌，一臉理所當然的樣子，看牠這樣心情就很難壞起來。",
                11,
            ),
            (
                "sydney_weather",
                "neutral",
                "normal",
                "afternoon",
                1,
                "雪梨白天陽光很亮，但進室內冷氣又偏涼，一不小心就容易著涼或肚子悶。",
                10,
            ),
        ]

    @staticmethod
    def _time_slot(dt: datetime.datetime) -> str:
        h = dt.hour
        if 5 <= h < 12:
            return "morning"
        if 12 <= h < 18:
            return "afternoon"
        if 18 <= h < 23:
            return "evening"
        return "night"

    @staticmethod
    def _season_hint(dt: datetime.datetime) -> str:
        m = dt.month
        if m in (11, 12, 1, 2):
            return "雪梨春末夏季（白天日照長、陽光強，室內冷氣偏涼，傍晚有海風）"
        if m in (3, 4, 5):
            return "雪梨秋季（早晚微涼乾爽）"
        if m in (6, 7, 8):
            return "雪梨冬季（早晚偏冷乾燥）"
        return "雪梨春季（氣候溫和偶爾有風）"

    @staticmethod
    def _weighted_choice(rows: List[sqlite3.Row]) -> Optional[sqlite3.Row]:
        if not rows:
            return None
        weights = [max(1, int(r["weight"])) for r in rows]
        return random.choices(rows, weights=weights, k=1)[0]

    @staticmethod
    def _tokenize_for_similarity(text: str) -> Counter:
        cleaned = re.sub(r"\s+", "", (text or "").lower())
        if not cleaned:
            return Counter()
        tokens: List[str] = []
        tokens.extend(re.findall(r"[a-z0-9_]+", cleaned))
        for ch in cleaned:
            if "\u4e00" <= ch <= "\u9fff":
                tokens.append(ch)
        for i in range(len(cleaned) - 1):
            pair = cleaned[i : i + 2]
            if not re.fullmatch(r"[\W_]+", pair):
                tokens.append(pair)
        return Counter(tokens)

    @classmethod
    def _cosine_similarity(cls, query_text: str, doc_text: str) -> float:
        q_vec = cls._tokenize_for_similarity(query_text)
        d_vec = cls._tokenize_for_similarity(doc_text)
        if not q_vec or not d_vec:
            return 0.0
        common = set(q_vec.keys()) & set(d_vec.keys())
        dot = sum(q_vec[k] * d_vec[k] for k in common)
        if dot <= 0:
            return 0.0
        q_norm = math.sqrt(sum(v * v for v in q_vec.values()))
        d_norm = math.sqrt(sum(v * v for v in d_vec.values()))
        if q_norm == 0 or d_norm == 0:
            return 0.0
        return dot / (q_norm * d_norm)

    def _ensure_today_mainline(self, conn: sqlite3.Connection, dt: datetime.datetime) -> Dict[str, Any]:
        today_str = dt.strftime("%Y-%m-%d")
        row = dict(conn.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone())

        if row.get("mainline_date") != today_str or not row.get("mainline_key"):
            is_wknd = 1 if dt.weekday() >= 5 else 0
            candidates = conn.execute(
                "SELECT * FROM daily_mainlines WHERE is_weekend = ?",
                (is_wknd,),
            ).fetchall()
            chosen = self._weighted_choice(candidates)
            if chosen:
                chosen_dict = dict(chosen)
                conn.execute(
                    """
                    UPDATE runtime_state
                    SET mainline_date = ?, mainline_key = ?, mainline_title = ?,
                        gi_cause = ?, gi_triggered_today = 0,
                        mood = 'normal', mood_until = 0, recovery_until = 0,
                        gi_status = 'normal', gi_until = 0,
                        illness_status = 'normal', illness_until = 0,
                        restroom_until = 0, pending_request_until = 0,
                        mentioned_slots = '', done_actions_today = '',
                        updated_at = ?
                    WHERE id = 1
                    """,
                    (
                        today_str,
                        chosen_dict.get("mainline_key", ""),
                        chosen_dict.get("title", "日常步調"),
                        chosen_dict.get("gi_cause_hint", ""),
                        dt.timestamp(),
                    ),
                )
                row = dict(conn.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone())

        ml = conn.execute(
            "SELECT * FROM daily_mainlines WHERE mainline_key = ?",
            (row.get("mainline_key", ""),),
        ).fetchone()
        return {"state": row, "mainline": dict(ml) if ml else None}

    def set_state(
        self,
        mood: Optional[str] = None,
        mood_duration_sec: int = 2400,
        recovery_sec: int = 0,
        gi_status: Optional[str] = None,
        gi_duration_sec: int = 2400,
        illness_status: Optional[str] = None,
        illness_duration_sec: int = 7200,
        restroom_sec: int = 0,
        gi_cause: Optional[str] = None,
        pending_request_sec: int = 0,
    ) -> None:
        now = datetime.datetime.now(SYDNEY_TZ).timestamp()
        with self._connect() as conn:
            row = dict(conn.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone())
            new_mood = mood if mood is not None else row.get("mood", "normal")
            new_mood_until = (now + mood_duration_sec) if mood is not None else float(row.get("mood_until") or 0)
            new_recovery = (now + recovery_sec) if recovery_sec > 0 else float(row.get("recovery_until") or 0)

            new_gi = gi_status if gi_status is not None else row.get("gi_status", "normal")
            new_gi_until = (now + gi_duration_sec) if gi_status is not None else float(row.get("gi_until") or 0)
            new_gi_triggered = 1 if gi_status == "upset" else int(row.get("gi_triggered_today") or 0)
            new_cause = gi_cause if gi_cause is not None else row.get("gi_cause", "")

            new_illness = illness_status if illness_status is not None else row.get("illness_status", "normal")
            new_illness_until = (
                (now + illness_duration_sec)
                if illness_status is not None
                else float(row.get("illness_until") or 0)
            )

            new_restroom_until = (now + restroom_sec) if restroom_sec > 0 else float(row.get("restroom_until") or 0)
            new_pending = (
                (now + pending_request_sec)
                if pending_request_sec > 0
                else float(row.get("pending_request_until") or 0)
            )

            conn.execute(
                """
                UPDATE runtime_state
                SET mood = ?, mood_until = ?, recovery_until = ?,
                    gi_status = ?, gi_until = ?, gi_triggered_today = ?, gi_cause = ?,
                    illness_status = ?, illness_until = ?,
                    restroom_until = ?, pending_request_until = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    new_mood,
                    new_mood_until,
                    new_recovery,
                    new_gi,
                    new_gi_until,
                    new_gi_triggered,
                    new_cause,
                    new_illness,
                    new_illness_until,
                    new_restroom_until,
                    new_pending,
                    now,
                ),
            )

    def clear_pending_request(self) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE runtime_state SET pending_request_until = 0 WHERE id = 1")

    def mark_slot_mentioned(self, slot: str) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT mentioned_slots FROM runtime_state WHERE id = 1").fetchone()
            raw_slots = row["mentioned_slots"] if row and row["mentioned_slots"] else ""
            slots = [s for s in raw_slots.split(",") if s]
            if slot not in slots:
                slots.append(slot)
                conn.execute(
                    "UPDATE runtime_state SET mentioned_slots = ? WHERE id = 1",
                    (",".join(slots),),
                )

    def mark_action_done(self, action_key: str) -> None:
        """記錄今天已經做過的一般行為（去廁所與喝水不列入，允許重複）。"""
        key = (action_key or "").strip()
        if not key or key in ("restroom", "drink_water"):
            return
        with self._connect() as conn:
            row = conn.execute("SELECT done_actions_today FROM runtime_state WHERE id = 1").fetchone()
            raw_actions = row["done_actions_today"] if row and row["done_actions_today"] else ""
            actions = [a for a in raw_actions.split(",") if a]
            if key not in actions:
                actions.append(key)
                conn.execute(
                    "UPDATE runtime_state SET done_actions_today = ? WHERE id = 1",
                    (",".join(actions),),
                )

    def get_done_actions(self, now_dt: Optional[datetime.datetime] = None) -> Set[str]:
        dt = now_dt or datetime.datetime.now(SYDNEY_TZ)
        with self._connect() as conn:
            self._ensure_today_mainline(conn, dt)
            row = conn.execute("SELECT done_actions_today FROM runtime_state WHERE id = 1").fetchone()
            raw_actions = row["done_actions_today"] if row and row["done_actions_today"] else ""
            return {a for a in raw_actions.split(",") if a}

    def touch_timestamps(self, nick: bool = False, reona: bool = False, proactive: bool = False) -> None:
        now = datetime.datetime.now(SYDNEY_TZ).timestamp()
        with self._connect() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(runtime_state)").fetchall()}
            if nick and "last_nick_ts" in cols:
                conn.execute("UPDATE runtime_state SET last_nick_ts = ? WHERE id = 1", (now,))
            if reona and "last_reona_ts" in cols:
                conn.execute("UPDATE runtime_state SET last_reona_ts = ? WHERE id = 1", (now,))
            if proactive and "last_proactive_ts" in cols:
                conn.execute("UPDATE runtime_state SET last_proactive_ts = ? WHERE id = 1", (now,))

    def evaluate_causal_events(self, user_text: str = "", now_dt: Optional[datetime.datetime] = None) -> Optional[str]:
        dt = now_dt or datetime.datetime.now(SYDNEY_TZ)
        now = dt.timestamp()
        slot = self._time_slot(dt)

        with self._connect() as conn:
            bundle = self._ensure_today_mainline(conn, dt)
            state = bundle["state"]
            ml = bundle["mainline"]
            if not ml:
                return None

            txt = (user_text or "").strip()
            pending_until = float(state.get("pending_request_until") or 0)

            if txt and now < pending_until:
                decline_kw = ("不要", "沒空", "不行", "改天", "沒辦法", "先忙", "算了", "晚點再說", "沒時間")
                if any(k in txt for k in decline_kw):
                    self.clear_pending_request()
                    self.set_state(mood="normal", mood_duration_sec=3600, recovery_sec=5400)
                    return "request_declined"

            in_recovery = now < float(state.get("recovery_until") or 0)
            gi_status = state.get("gi_status") or "normal"
            mood_until = float(state.get("mood_until") or 0)
            gi_triggered_today = int(state.get("gi_triggered_today") or 0)
            restroom_until = float(state.get("restroom_until") or 0)

            if txt and not in_recovery:
                if any(k in txt for k in ("厲害", "好棒", "喜歡", "好看", "麻糬", "好吃", "辛苦了", "抱抱")):
                    if gi_status == "normal" and now >= mood_until:
                        self.set_state(mood="happy", mood_duration_sec=2700)
                elif any(k in txt for k in ("喝冰", "冰咖啡", "吃辣", "熬夜", "別睡")):
                    if gi_triggered_today == 0 and random.random() < 0.45:
                        self.set_state(
                            gi_status="upset",
                            gi_duration_sec=3000,
                            recovery_sec=5400,
                            restroom_sec=random.randint(180, 320),
                        )

            gi_risk = float(ml.get("gi_risk") or 0.25)
            if slot in ("afternoon", "evening") and gi_triggered_today == 0 and gi_status == "normal":
                step_prob = gi_risk * 0.18
                if random.random() < step_prob:
                    self.set_state(
                        gi_status="upset",
                        gi_duration_sec=random.randint(2400, 4200),
                        recovery_sec=5400,
                        restroom_sec=random.randint(180, 360),
                        gi_cause=ml.get("gi_cause_hint") or "",
                    )

            if gi_status == "upset" and now > restroom_until + 900:
                if random.random() < 0.25:
                    self.set_state(restroom_sec=random.randint(150, 300))

        return None

    def compute_snapshot(self, now_dt: Optional[datetime.datetime] = None) -> Dict[str, Any]:
        dt = now_dt or datetime.datetime.now(SYDNEY_TZ)
        now = dt.timestamp()
        slot = self._time_slot(dt)
        is_weekend = dt.weekday() >= 5
        is_late_or_night = (dt.hour == 20 and dt.minute >= 30) or dt.hour >= 21 or dt.hour < 8

        with self._connect() as conn:
            bundle = self._ensure_today_mainline(conn, dt)
            row = bundle["state"]
            ml = bundle["mainline"]

            phases = dict(DEFAULT_PHASES)
            default_mood = "normal"

            if ml:
                raw_phases = ml.get("phases_json") or "{}"
                try:
                    parsed = json.loads(raw_phases)
                    if isinstance(parsed, dict):
                        for k, v in parsed.items():
                            if isinstance(v, dict):
                                phases[k] = {**DEFAULT_PHASES.get(k, DEFAULT_PHASES["afternoon"]), **v}
                except Exception:
                    pass
                if ml.get("evening_mood") and slot in ("evening", "night"):
                    default_mood = ml["evening_mood"]

            p = phases.get(slot, phases["afternoon"])

            in_recovery = now < float(row.get("recovery_until") or 0)
            stored_mood = row.get("mood") or "normal"
            mood_until = float(row.get("mood_until") or 0)
            mood = stored_mood if now < mood_until else default_mood
            if in_recovery and mood == "happy":
                mood = "normal"

            stored_gi = row.get("gi_status") or "normal"
            gi_until = float(row.get("gi_until") or 0)
            gi = stored_gi if now < gi_until else "normal"

            stored_illness = row.get("illness_status") or "normal"
            illness_until = float(row.get("illness_until") or 0)
            illness = stored_illness if now < illness_until else "normal"

            in_restroom = now < float(row.get("restroom_until") or 0)
            is_unwell = (gi != "normal") or (illness != "normal") or (mood == "sick")

            location = p["location"]
            positions = p["positions"]
            current_event = p["event"]
            items = p["items"]

            if is_late_or_night:
                location = "房間床邊" if slot == "night" else "客廳沙發"
                positions = (
                    "Reona 靠在床頭；麻糬縮在床尾"
                    if slot == "night"
                    else "Reona 坐在沙發上；麻糬趴在旁邊"
                )
                current_event = "晚上待在家裡休息放空"
                items = "溫開水、手機、薄毯"

            if in_restroom:
                location = "洗手間"
                positions = "Reona 在廁所；麻糬在門外"
                items = "手機、衛生紙"
            elif is_unwell:
                location = "客廳沙發"
                positions = "Reona 蜷在沙發上；麻糬趴在旁邊"
                items = "熱水袋、溫開水"

            raw_slots = row.get("mentioned_slots") or ""
            mentioned_slots = [s for s in raw_slots.split(",") if s]
            already_mentioned = slot in mentioned_slots

            raw_done = row.get("done_actions_today") or ""
            done_actions = [a for a in raw_done.split(",") if a]

            if mood != stored_mood or gi != stored_gi or illness != stored_illness:
                conn.execute(
                    "UPDATE runtime_state SET mood = ?, gi_status = ?, illness_status = ?, updated_at = ? WHERE id = 1",
                    (mood, gi, illness, now),
                )

        if in_restroom:
            mode = "restroom"
            min_msgs, max_msgs = 1, 1
            max_chars = 18
            delay_range = (1.5, 3.5)
        elif illness in ("heavy_cold", "flu"):
            mode = "heavy_cold"
            min_msgs, max_msgs = 1, 2
            max_chars = 28
            delay_range = (2.0, 4.0)
        elif illness == "cold":
            mode = "cold"
            min_msgs, max_msgs = 1, 2
            max_chars = 26
            delay_range = (1.8, 3.8)
        elif illness == "period":
            mode = "period"
            min_msgs, max_msgs = 1, 2
            max_chars = 28
            delay_range = (1.8, 3.8)
        elif gi != "normal" or mood == "sick":
            mode = "sick"
            min_msgs, max_msgs = 1, 2
            max_chars = 26
            delay_range = (1.8, 3.8)
        elif mood == "tired":
            mode = "tired"
            min_msgs, max_msgs = 1, 2
            max_chars = 25
            delay_range = (1.8, 3.5)
        elif mood == "happy":
            mode = "happy"
            min_msgs, max_msgs = 1, 3
            max_chars = 28
            delay_range = (1.5, 3.0)
        else:
            mode = "normal"
            min_msgs, max_msgs = 1, 2
            max_chars = 25
            delay_range = (1.5, 3.5)

        return {
            "sydney_time": dt.strftime("%Y-%m-%d %H:%M"),
            "season": self._season_hint(dt),
            "time_slot": slot,
            "is_weekend": is_weekend,
            "mainline_title": row.get("mainline_title") or "日常步調",
            "location": location,
            "positions": positions,
            "current_event": current_event,
            "items": items,
            "already_mentioned_slot": already_mentioned,
            "done_actions_today": done_actions,
            "mood": mood,
            "in_recovery": in_recovery,
            "gi_status": gi,
            "illness_status": illness,
            "gi_cause": (row.get("gi_cause") or "") if gi != "normal" else "",
            "in_restroom": in_restroom,
            "mode": mode,
            "min_msgs": min_msgs,
            "max_msgs": max_msgs,
            "max_chars": max_chars,
            "delay_range": delay_range,
            "last_nick_ts": float(row.get("last_nick_ts") or 0),
            "last_reona_ts": float(row.get("last_reona_ts") or 0),
            "last_proactive_ts": float(row.get("last_proactive_ts") or 0),
        }

    def pick_canned_line(self, category: str, sub_state: str = "any") -> Optional[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM canned_lines
                WHERE category = ? AND (sub_state = ? OR sub_state = 'any')
                """,
                (category, sub_state),
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT * FROM canned_lines WHERE category = ?",
                    (category,),
                ).fetchall()
        chosen = self._weighted_choice(rows)
        return chosen["text"] if chosen else None

    def retrieve_rag_clue(
        self,
        snapshot: Dict[str, Any],
        user_text: str = "",
        threshold: Optional[float] = None,
    ) -> Dict[str, Optional[str]]:
        mood = snapshot["mood"]
        slot = snapshot["time_slot"]
        weekend_flag = 1 if snapshot["is_weekend"] else 0
        is_unwell = (
            snapshot.get("gi_status", "normal") != "normal"
            or snapshot.get("illness_status", "normal") != "normal"
            or mood in ("sick", "period", "cold", "heavy_cold")
        )
        score_cut = self.SIMILARITY_THRESHOLD if threshold is None else threshold

        allowed_polarities = ("neutral", "good")
        if mood in ("tired", "sick", "period", "cold", "heavy_cold") or snapshot.get("gi_status", "normal") != "normal":
            allowed_polarities = ("neutral", "good", "bad")

        placeholders = ",".join("?" for _ in allowed_polarities)

        with self._connect() as conn:
            snippet_rows = conn.execute(
                f"""
                SELECT * FROM life_snippets
                WHERE polarity IN ({placeholders})
                  AND (mood_fit = ? OR mood_fit = 'any')
                  AND (time_slot = ? OR time_slot = 'any')
                  AND (is_weekend = ? OR is_weekend = -1)
                """,
                (*allowed_polarities, mood, slot, weekend_flag),
            ).fetchall()

            candidates = [dict(r) for r in snippet_rows]
            if is_unwell:
                filtered = [c for c in candidates if c.get("topic") != "drawing_habit"]
                if filtered:
                    candidates = filtered

            chosen_snippet: Optional[str] = None
            q = (user_text or "").strip()
            if q and candidates:
                scored: List[Tuple[float, Dict[str, Any]]] = []
                for c in candidates:
                    doc = f"{c.get('topic', '')} {c.get('content', '')}"
                    sim = self._cosine_similarity(q, doc)
                    if sim >= score_cut:
                        scored.append((sim, c))
                if scored:
                    scored.sort(key=lambda item: (item[0], int(item[1].get("weight") or 10)), reverse=True)
                    top_pool = [item[1] for item in scored[:2]]
                    picked = random.choice(top_pool)
                    chosen_snippet = picked["content"]

            mem_row = conn.execute(
                "SELECT * FROM daily_memories ORDER BY importance DESC, id DESC LIMIT 1"
            ).fetchone()

            arc_row = None
            if not is_unwell:
                arc_row = conn.execute(
                    "SELECT * FROM story_arcs WHERE is_active = 1 ORDER BY id ASC LIMIT 1"
                ).fetchone()

        arc_clue = None
        if arc_row:
            steps = json.loads(arc_row["steps_json"])
            idx = min(max(0, int(arc_row["current_step"])), len(steps) - 1)
            if steps:
                arc_clue = f"{arc_row['title']}：{steps[idx]}"

        return {
            "life_snippet": chosen_snippet,
            "daily_memory": mem_row["content"] if mem_row else None,
            "story_arc": arc_clue,
        }

    def add_daily_memory(self, content: str, category: str = "life", importance: int = 6, keep_max: int = 12) -> None:
        now_dt = datetime.datetime.now(SYDNEY_TZ)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_memories (mem_date, category, content, importance, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (now_dt.strftime("%Y-%m-%d"), category, content.strip(), importance, now_dt.timestamp()),
            )
            conn.execute(
                """
                DELETE FROM daily_memories
                WHERE id NOT IN (
                    SELECT id FROM daily_memories
                    ORDER BY importance DESC, id DESC
                    LIMIT ?
                )
                """,
                (keep_max,),
            )

    def upsert_nick_note(self, topic: str, content: str) -> None:
        now = datetime.datetime.now(SYDNEY_TZ).timestamp()
        with self._connect() as conn:
            existing = conn.execute("SELECT id FROM nick_notes WHERE topic = ?", (topic,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE nick_notes SET content = ?, updated_at = ? WHERE id = ?",
                    (content.strip(), now, existing["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO nick_notes (topic, content, updated_at) VALUES (?, ?, ?)",
                    (topic.strip(), content.strip(), now),
                )
