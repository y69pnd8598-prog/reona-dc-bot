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
        "event": "下午專心畫圖，室內冷氣偏涼",
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
            return "
