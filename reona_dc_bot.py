import datetime
import json
import os
import random
import sqlite3
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
DEFAULT_DB_PATH = os.getenv("REONA_DB_PATH", "reona_bg.db")


class ReonaBGDB:
    """Reona 的六維世界狀態引擎 ＋ 輕量 RAG 資料庫（單檔 SQLite，零 Token 消耗）。"""

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
                    restroom_until REAL NOT NULL DEFAULT 0,
                    pending_request_until REAL NOT NULL DEFAULT 0,
                    mentioned_slots TEXT NOT NULL DEFAULT '',
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
                    phases_json TEXT NOT NULL,
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
                                    "location": "雪梨租屋處畫桌前",
                                    "positions": "Reona 剛坐上工作椅；麻糬在窗台曬春末太陽",
                                    "event": "泡好熱烏龍茶，正在整理客戶 brief 開畫草稿",
                                    "items": "保溫杯、數位筆、草稿本",
                                },
                                "afternoon": {
                                    "location": "雪梨租屋處畫桌前（冷氣房）",
                                    "positions": "Reona 盯著螢幕修構圖；麻糬趴在畫桌角落睡覺",
                                    "event": "下午趕著把草稿比例順完，時間過超快",
                                    "items": "數位板、色票本、喝一半的冰美式",
                                },
                                "evening": {
                                    "location": "家裡客廳沙發",
                                    "positions": "Reona 剛離開畫桌伸懶腰；麻糬跑來蹭腳邊討罐頭",
                                    "event": "傍晚終於把草稿線條順完，肩膀有點緊",
                                    "items": "熱水袋、馬克杯、抱枕",
                                },
                                "night": {
                                    "location": "房間床邊",
                                    "positions": "Reona 洗完澡靠在床頭；麻糬縮在床尾",
                                    "event": "晚上收工放空，不想再看色票了",
                                    "items": "溫開水、手機、薄毯",
                                },
                            },
                            ensure_ascii=False,
                        ),
                        "tired",
                        0.45,
                        "下午趕稿吹冷氣又空腹喝冰美式",
                        12,
                    ),
                    (
                        "work_final_delivery",
                        "交最終完稿日",
                        0,
                        json.dumps(
                            {
                                "morning": {
                                    "location": "雪梨租屋處畫桌前",
                                    "positions": "Reona 專心檢查圖層；麻糬蹲在書架上看著她",
                                    "event": "在做完稿交件前的最後細節檢查",
                                    "items": "高解析度輸出檔、保溫杯、眼鏡",
                                },
                                "afternoon": {
                                    "location": "雪梨租屋處畫桌前",
                                    "positions": "Reona 剛寄出信件靠在椅背上；麻糬在抓貓抓板",
                                    "event": "把完稿檔案寄給客戶了，正在等對方確認",
                                    "items": "筆電、手機、熱菊花茶",
                                },
                                "evening": {
                                    "location": "家裡廚房與餐桌",
                                    "positions": "Reona 在弄晚餐；麻糬在飯碗旁邊轉圈",
                                    "event": "客戶居然一次就過稿沒退件，整個人鬆一口氣",
                                    "items": "熱湯、餐具、手機",
                                },
                                "night": {
                                    "location": "家裡客廳沙發",
                                    "positions": "Reona 窩在沙發上；麻糬直接趴在她腿上",
                                    "event": "交完大案子心情超好，邊摸貓邊放空",
                                    "items": "無咖啡因花草茶、抱枕",
                                },
                            },
                            ensure_ascii=False,
                        ),
                        "happy",
                        0.30,
                        "前幾天趕稿作息亂掉，一鬆懈腸胃就鬧脾氣",
                        12,
                    ),
                    (
                        "wknd_grocery_walk",
                        "出門採買補貨散步",
                        1,
                        json.dumps(
                            {
                                "morning": {
                                    "location": "家裡餐桌旁",
                                    "positions": "Reona 睡到自然醒慢慢吃早餐；麻糬在地板翻肚",
                                    "event": "週末早上難得不用趕稿，邊看陽光邊吃吐司",
                                    "items": "手沖熱茶、烤吐司、太陽眼鏡",
                                },
                                "afternoon": {
                                    "location": "雪梨街頭與超市",
                                    "positions": "Reona 在外面採買；麻糬獨自在家看門睡午覺",
                                    "event": "推著推車補買下週食材跟貓砂，順便散步",
                                    "items": "帆布購物袋、超市清單、冰拿鐵",
                                },
                                "evening": {
                                    "location": "雪梨租屋處廚房",
                                    "positions": "Reona 剛提著袋子進門；麻糬立刻衝到玄關迎接",
                                    "event": "把買回來的東西塞滿冰箱，吹到傍晚海風很舒服",
                                    "items": "剛買的水果、貓零食、溫水",
                                },
                                "night": {
                                    "location": "家裡客廳沙發",
                                    "positions": "Reona 洗完澡在沙發滑手機；麻糬窩在旁邊",
                                    "event": "採買完覺得下週很有安全感，準備早點休息",
                                    "items": "薄外套、熱茶、香氛蠟燭",
                                },
                            },
                            ensure_ascii=False,
                        ),
                        "happy",
                        0.35,
                        "下午外面太陽大貪喝冰拿鐵，傍晚又吹到海風",
                        12,
                    ),
                    (
                        "wknd_home_mochi",
                        "在家大掃除陪麻糬",
                        1,
                        json.dumps(
                            {
                                "morning": {
                                    "location": "雪梨租屋處工作室",
                                    "positions": "Reona 在整理畫桌；麻糬好奇地鑽進紙箱",
                                    "event": "週末早上洗床單順便清理畫具",
                                    "items": "吸塵器、抹布、髮圈",
                                },
                                "afternoon": {
                                    "location": "家裡客廳地毯上",
                                    "positions": "Reona 坐在地毯上；麻糬被抓著梳毛一臉無奈",
                                    "event": "下午幫麻糬梳毛剪指甲，跟牠鬥智鬥勇",
                                    "items": "針梳、指甲剪、肉泥條",
                                },
                                "evening": {
                                    "location": "家裡客廳沙發",
                                    "positions": "Reona 泡好茶坐下；麻糬舔完毛在旁邊打盹",
                                    "event": "家裡變得很乾淨，開著冷氣放空發呆",
                                    "items": "熱烏龍茶、薄毯、抱枕",
                                },
                                "night": {
                                    "location": "房間床邊",
                                    "positions": "Reona 靠在枕頭上；麻糬縮在腳邊",
                                    "event": "晚上在房間聽音樂看點閒書",
                                    "items": "溫開水、藍牙耳機、手機",
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
                    # 廁所極簡（4~6 字）
                    ("restroom", "normal", "去一下廁所", 12),
                    ("restroom", "normal", "還在廁所喔", 10),
                    ("restroom", "normal", "等我一下啦", 10),
                    ("restroom", "sick", "還在廁所裡", 12),
                    ("restroom", "sick", "肚子又絞了", 12),
                    # 腸胃不適 / 疲憊示弱（4~6 字）
                    ("sick", "any", "肚子有點悶", 12),
                    ("sick", "any", "先喝點溫水", 12),
                    ("sick", "any", "沒什麼力氣", 10),
                    ("sick", "any", "腸胃又鬧了", 10),
                    ("tired", "any", "今天好累喔", 12),
                    ("tired", "any", "想靠著你了", 10),
                    ("tired", "any", "讓我喘口氣", 10),
                    # 敷衍訊息反擊
                    ("low_effort", "normal", "？", 12),
                    ("low_effort", "normal", "在忙嗎", 10),
                    ("low_effort", "normal", "嗯？", 10),
                    ("low_effort", "normal", "敷衍我喔", 8),
                    ("low_effort", "tired", "⋯", 10),
                    # 收到語音訊息（零 Token 直出）
                    ("voice_msg", "normal", "懶得點開聽欸", 12),
                    ("voice_msg", "normal", "用打字的啦", 12),
                    ("voice_msg", "normal", "現在不方便聽", 10),
                    ("voice_msg", "tired", "沒力氣聽語音", 10),
                    ("voice_msg", "sick", "先打字好嗎", 10),
                    # 視訊要求（守住獨立成熟邊界）
                    ("video_req", "normal", "現在超邋遢不要", 12),
                    ("video_req", "normal", "在忙沒空開啦", 10),
                    ("video_req", "happy", "懶得整理頭髮", 10),
                    ("video_req", "tired", "今天狀態超差", 12),
                    ("video_req", "sick", "沒力氣開視訊", 12),
                    # 自己提出的請求被 Nick 拒絕（體面收尾、不情緒勒索）
                    ("request_declined", "normal", "好喔那沒關係", 12),
                    ("request_declined", "normal", "你先忙你的啦", 12),
                    ("request_declined", "happy", "行吧我自己來", 10),
                    ("request_declined", "tired", "好啦我先休息", 12),
                    ("request_declined", "sick", "沒事我喝溫水", 12),
                    # 防卡保底句
                    ("fallback", "normal", "剛在忙手邊", 10),
                    ("fallback", "normal", "晚點再聊喔", 10),
                    ("fallback", "happy", "剛想到一件", 10),
                    ("fallback", "tired", "先休息一下", 10),
                    ("fallback", "sick", "先躺一下喔", 10),
                ]
                conn.executemany(
                    "INSERT INTO canned_lines (category, sub_state, text, weight) VALUES (?, ?, ?, ?)",
                    canned_rows,
                )

            if conn.execute("SELECT COUNT(*) FROM life_snippets").fetchone()[0] == 0:
                snippet_rows = [
                    ("drawing", "neutral", "normal", "any", 0, "畫桌旁邊永遠放著保溫杯，冷掉就不再喝了。", 12),
                    ("sydney", "neutral", "normal", "afternoon", 1, "雪梨快夏天了白天太陽超亮，但進室內冷氣又偏冷。", 12),
                    ("mochi", "good", "happy", "any", -1, "麻糬剛剛直接踩過我的數位板，還一臉理所當然。", 12),
                    ("roots", "neutral", "normal", "night", -1, "從小家裡大家各忙各的，習慣有事自己先消化完再說。", 10),
                    ("roots", "neutral", "tired", "night", -1, "小時候一緊張就會肚子痛，長大後忙起來這個毛病還是沒變。", 11),
                    ("good_memory", "good", "happy", "any", -1, "記得以前在台北下雨天收工，去巷口喝到一碗熱湯整個人都活過來。", 10),
                    ("good_memory", "good", "happy", "evening", -1, "第一次收到陌生人私訊說被我的插畫療癒到，那張截圖我還留著。", 10),
                    ("bad_memory", "bad", "tired", "night", -1, "以前剛接案時不敢拒絕別人，熬夜硬撐到半夜自己跑去掛急診。", 8),
                    ("bad_memory", "bad", "sick", "any", -1, "小時候有次被誤會卻懶得解釋，悶在心裡結果連拉兩天肚子。", 7),
                    ("reliance", "good", "tired", "night", -1, "平常習慣自己扛，但累的時候有你在旁邊聽著真的安心很多。", 12),
                    ("boundary", "neutral", "normal", "night", -1, "太晚了別跟著熬夜啦，我自己的進度我自己會顧好。", 11),
                ]
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
                    VALUES (?, 'life', '台北長大的獨立插畫師，現在帶著貓咪麻糬在雪梨生活。', 8, ?)
                    """,
                    (now_dt.strftime("%Y-%m-%d"), now_dt.timestamp()),
                )

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

    def _ensure_today_mainline(self, conn: sqlite3.Connection, dt: datetime.datetime) -> Dict[str, Any]:
        today_str = dt.strftime("%Y-%m-%d")
        row = dict(conn.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone())

        if row["mainline_date"] != today_str or not row["mainline_key"]:
            is_wknd = 1 if dt.weekday() >= 5 else 0
            candidates = conn.execute(
                "SELECT * FROM daily_mainlines WHERE is_weekend = ?",
                (is_wknd,),
            ).fetchall()
            chosen = self._weighted_choice(candidates)
            if chosen:
                conn.execute(
                    """
                    UPDATE runtime_state
                    SET mainline_date = ?, mainline_key = ?, mainline_title = ?,
                        gi_cause = ?, gi_triggered_today = 0,
                        mood = 'normal', mood_until = 0, recovery_until = 0,
                        gi_status = 'normal', gi_until = 0, restroom_until = 0,
                        pending_request_until = 0, mentioned_slots = '',
                        updated_at = ?
                    WHERE id = 1
                    """,
                    (
                        today_str,
                        chosen["mainline_key"],
                        chosen["title"],
                        chosen["gi_cause_hint"],
                        dt.timestamp(),
                    ),
                )
                row = dict(conn.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone())

        ml = conn.execute(
            "SELECT * FROM daily_mainlines WHERE mainline_key = ?",
            (row["mainline_key"],),
        ).fetchone()
        return {"state": row, "mainline": dict(ml) if ml else None}

    def set_state(
        self,
        mood: Optional[str] = None,
        mood_duration_sec: int = 2400,
        recovery_sec: int = 0,
        gi_status: Optional[str] = None,
        gi_duration_sec: int = 2400,
        restroom_sec: int = 0,
        gi_cause: Optional[str] = None,
        pending_request_sec: int = 0,
    ) -> None:
        now = datetime.datetime.now(SYDNEY_TZ).timestamp()
        with self._connect() as conn:
            row = dict(conn.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone())
            new_mood = mood if mood is not None else row["mood"]
            new_mood_until = (now + mood_duration_sec) if mood is not None else row["mood_until"]
            new_recovery = (now + recovery_sec) if recovery_sec > 0 else row["recovery_until"]

            new_gi = gi_status if gi_status is not None else row["gi_status"]
            new_gi_until = (now + gi_duration_sec) if gi_status is not None else row["gi_until"]
            new_gi_triggered = 1 if gi_status == "upset" else row["gi_triggered_today"]
            new_cause = gi_cause if gi_cause is not None else row["gi_cause"]

            new_restroom_until = (now + restroom_sec) if restroom_sec > 0 else row["restroom_until"]
            new_pending = (now + pending_request_sec) if pending_request_sec > 0 else row["pending_request_until"]

            conn.execute(
                """
                UPDATE runtime_state
                SET mood = ?, mood_until = ?, recovery_until = ?,
                    gi_status = ?, gi_until = ?, gi_triggered_today = ?, gi_cause = ?,
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
            slots = [s for s in (row["mentioned_slots"] or "").split(",") if s]
            if slot not in slots:
                slots.append(slot)
                conn.execute(
                    "UPDATE runtime_state SET mentioned_slots = ? WHERE id = 1",
                    (",".join(slots),),
                )

    def touch_timestamps(self, nick: bool = False, reona: bool = False, proactive: bool = False) -> None:
        now = datetime.datetime.now(SYDNEY_TZ).timestamp()
        with self._connect() as conn:
            if nick:
                conn.execute("UPDATE runtime_state SET last_nick_ts = ? WHERE id = 1", (now,))
            if reona:
                conn.execute("UPDATE runtime_state SET last_reona_ts = ? WHERE id = 1", (now,))
            if proactive:
                conn.execute("UPDATE runtime_state SET last_proactive_ts = ? WHERE id = 1", (now,))

    def evaluate_causal_events(self, user_text: str = "", now_dt: Optional[datetime.datetime] = None) -> Optional[str]:
        """
        圍繞每日主軸的因果推進（回傳特殊事件標籤，例如 'request_declined'）：
        1. 若 Reona 剛提出請求且 Nick 明確拒絕，體面收尾並啟動情緒冷卻鎖。
        2. 不在冷卻鎖期間時，誇獎/美食/貓咪可帶出好心情。
        3. 下午到晚上依今日主軸風險自然誘發腸胃體質（每日最多主發作一次）。
        """
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

            # 1. 檢查是否為「拒絕 Reona 剛提出的請求」
            if txt and now < state["pending_request_until"]:
                decline_kw = ("不要", "沒空", "不行", "改天", "沒辦法", "先忙", "算了", "晚點再說", "沒時間")
                if any(k in txt for k in decline_kw):
                    self.clear_pending_request()
                    # 體面收尾並進入 1.5 小時情緒緩衝期，不會下一秒秒變嗨
                    self.set_state(mood="normal", mood_duration_sec=3600, recovery_sec=5400)
                    return "request_declined"

            # 2. 互動語意因果（不在冷卻期且腸胃正常時才轉開心）
            in_recovery = now < state["recovery_until"]
            if txt and not in_recovery:
                if any(k in txt for k in ("厲害", "好棒", "喜歡", "好看", "麻糬", "好吃", "辛苦了", "抱抱")):
                    if state["gi_status"] == "normal" and now >= state["mood_until"]:
                        self.set_state(mood="happy", mood_duration_sec=2700)
                elif any(k in txt for k in ("喝冰", "冰咖啡", "吃辣", "熬夜", "別睡")):
                    if state["gi_triggered_today"] == 0 and random.random() < 0.45:
                        self.set_state(
                            gi_status="upset",
                            gi_duration_sec=3000,
                            recovery_sec=5400,
                            restroom_sec=random.randint(180, 320),
                        )

            # 3. 下午/傍晚依今日主軸風險自然誘發腸胃體質（每日最多主發作一次）
            if slot in ("afternoon", "evening") and state["gi_triggered_today"] == 0 and state["gi_status"] == "normal":
                step_prob = float(ml["gi_risk"]) * 0.18
                if random.random() < step_prob:
                    self.set_state(
                        gi_status="upset",
                        gi_duration_sec=random.randint(2400, 4200),
                        recovery_sec=5400,
                        restroom_sec=random.randint(180, 360),
                        gi_cause=ml["gi_cause_hint"],
                    )

            # 4. 腸胃不適期間，過一段時間可能再跑一趟廁所
            if state["gi_status"] == "upset" and now > state["restroom_until"] + 900:
                if random.random() < 0.25:
                    self.set_state(restroom_sec=random.randint(150, 300))

        return None

    def compute_snapshot(self, now_dt: Optional[datetime.datetime] = None) -> Dict[str, Any]:
        """計算六維世界狀態（時間、地點、人物位置、情緒起伏、事件、物品）與短句節奏規則。"""
        dt = now_dt or datetime.datetime.now(SYDNEY_TZ)
        now = dt.timestamp()
        slot = self._time_slot(dt)
        is_weekend = dt.weekday() >= 5

        with self._connect() as conn:
            bundle = self._ensure_today_mainline(conn, dt)
            row = bundle["state"]
            ml = bundle["mainline"]

            if ml:
                phases = json.loads(ml["phases_json"])
                p = phases.get(slot, phases["afternoon"])
                default_mood = ml["evening_mood"] if slot in ("evening", "night") else "normal"
            else:
                p = {
                    "location": "雪梨租屋處",
                    "positions": "Reona 在家；麻糬在旁邊",
                    "event": "日常自由步調",
                    "items": "保溫杯、手機",
                }
                default_mood = "normal"

            in_recovery = now < row["recovery_until"]
            mood = row["mood"] if now < row["mood_until"] else default_mood
            if in_recovery and mood == "happy":
                mood = "normal"

            gi = row["gi_status"] if now < row["gi_until"] else "normal"
            in_restroom = now < row["restroom_until"]

            location = "家裡洗手間" if in_restroom else (
                "家裡客廳沙發" if gi != "normal" else p["location"]
            )
            positions = (
                "Reona 在廁所；麻糬蹲在門外等"
                if in_restroom
                else ("Reona 抱著熱水袋蜷在沙發上；麻糬趴在腳邊守著" if gi != "normal" else p["positions"])
            )
            items = (
                "手機、衛生紙"
                if in_restroom
                else ("熱水袋、溫開水、腸胃藥" if gi != "normal" else p["items"])
            )

            mentioned_slots = [s for s in (row["mentioned_slots"] or "").split(",") if s]
            already_mentioned = slot in mentioned_slots

            if mood != row["mood"] or gi != row["gi_status"]:
                conn.execute(
                    "UPDATE runtime_state SET mood = ?, gi_status = ?, updated_at = ? WHERE id = 1",
                    (mood, gi, now),
                )

        if in_restroom:
            mode = "restroom"
            min_msgs, max_msgs = 1, 1
            max_chars = 6
            delay_range = (1.5, 4.0)
        elif gi != "normal" or mood == "sick":
            mode = "sick"
            min_msgs, max_msgs = 1, 2
            max_chars = 6
            delay_range = (2.0, 5.0)
        elif mood == "tired":
            mode = "tired"
            min_msgs, max_msgs = 1, 2
            max_chars = 6
            delay_range = (2.0, 5.0)
        elif mood == "happy":
            mode = "happy"
            min_msgs, max_msgs = 3, 4
            max_chars = 15
            delay_range = (1.5, 3.2)
        else:
            mode = "normal"
            min_msgs, max_msgs = 1, 3
            max_chars = 10
            delay_range = (1.5, 4.0)

        return {
            "sydney_time": dt.strftime("%Y-%m-%d %H:%M"),
            "season": self._season_hint(dt),
            "time_slot": slot,
            "is_weekend": is_weekend,
            "mainline_title": row["mainline_title"],
            "location": location,
            "positions": positions,
            "current_event": p["event"],
            "items": items,
            "already_mentioned_slot": already_mentioned,
            "mood": mood,
            "in_recovery": in_recovery,
            "gi_status": gi,
            "gi_cause": row["gi_cause"] if gi != "normal" else "",
            "in_restroom": in_restroom,
            "mode": mode,
            "min_msgs": min_msgs,
            "max_msgs": max_msgs,
            "max_chars": max_chars,
            "delay_range": delay_range,
            "last_nick_ts": row["last_nick_ts"],
            "last_reona_ts": row["last_reona_ts"],
            "last_proactive_ts": row["last_proactive_ts"],
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

    def retrieve_rag_clue(self, snapshot: Dict[str, Any]) -> Dict[str, Optional[str]]:
        mood = snapshot["mood"]
        slot = snapshot["time_slot"]
        weekend_flag = 1 if snapshot["is_weekend"] else 0

        allowed_polarities = ("neutral", "good")
        if mood in ("tired", "sick") or snapshot["gi_status"] != "normal":
            if random.random() < 0.35:
                allowed_polarities = ("neutral", "bad")

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
            snippet = self._weighted_choice(snippet_rows)

            mem_row = conn.execute(
                "SELECT * FROM daily_memories ORDER BY importance DESC, id DESC LIMIT 1"
            ).fetchone()

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
            "life_snippet": snippet["content"] if snippet else None,
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
