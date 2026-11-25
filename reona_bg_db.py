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
    """Reona 的每日主軸狀態引擎 ＋ 輕量 RAG 資料庫（單檔 SQLite，零 Token 消耗）。"""

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
                    mainline_phase TEXT NOT NULL DEFAULT 'steady',
                    gi_cause TEXT NOT NULL DEFAULT '',
                    gi_triggered_today INTEGER NOT NULL DEFAULT 0,
                    mood TEXT NOT NULL DEFAULT 'normal',
                    mood_until REAL NOT NULL DEFAULT 0,
                    gi_status TEXT NOT NULL DEFAULT 'normal',
                    gi_until REAL NOT NULL DEFAULT 0,
                    restroom_until REAL NOT NULL DEFAULT 0,
                    last_nick_ts REAL NOT NULL DEFAULT 0,
                    last_reona_ts REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS daily_mainlines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mainline_key TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    is_weekend INTEGER NOT NULL DEFAULT 0,
                    morning_desc TEXT NOT NULL,
                    afternoon_desc TEXT NOT NULL,
                    evening_desc TEXT NOT NULL,
                    night_desc TEXT NOT NULL,
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
                """
                INSERT OR IGNORE INTO runtime_state (id, updated_at)
                VALUES (1, ?)
                """,
                (datetime.datetime.now(SYDNEY_TZ).timestamp(),),
            )

    def _seed_defaults_if_empty(self) -> None:
        """初始化每日主軸、台灣打字短句池、生活與童年/家庭/好壞記憶切片。"""
        with self._connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM daily_mainlines").fetchone()[0] == 0:
                mainlines = [
                    # 平日主軸（插畫師接案日常）
                    (
                        "work_sketch_rush",
                        "趕商業插畫草稿",
                        0,
                        "早上剛泡好熱茶，準備整理客戶 brief 開畫草稿",
                        "下午盯著螢幕修構圖比例，時間過超快",
                        "傍晚終於把草稿線條順完，肩膀有點緊",
                        "晚上收工放空，不想再看色票了",
                        "tired",
                        0.45,
                        "下午趕稿空腹喝了一杯冰美式提神",
                        12,
                    ),
                    (
                        "work_final_delivery",
                        "交最終完稿日",
                        0,
                        "早上在做完稿前的最後細節檢查",
                        "下午把檔案丟給客戶了，正在等對方確認",
                        "客戶居然一次就過稿沒退件，整個人鬆一口氣",
                        "晚上心情很好，打算弄點好吃的犒賞自己",
                        "happy",
                        0.30,
                        "前幾天趕稿作息有點亂，一放鬆腸胃就開始抗議",
                        12,
                    ),
                    (
                        "work_client_revision",
                        "跟客戶來回改稿",
                        0,
                        "一早起來就看到信箱有修改意見，先喝口水冷靜",
                        "下午一直在微調配色跟光影，有點考驗耐心",
                        "總算把修改版交出去，今天腦袋快燒乾",
                        "晚上只想窩在沙發上撸麻糬充電",
                        "tired",
                        0.40,
                        "改稿改到太晚吃飯，隨便吃點冷的就胃悶了",
                        10,
                    ),
                    (
                        "work_admin_prep",
                        "整理作品集與請款日",
                        0,
                        "早上在對帳跟整理這週的案子進度",
                        "下午泡在書桌前整理新作品集排版",
                        "雜事處理得差不多，步調算滿穩的",
                        "晚上早早洗完澡在房間聽音樂",
                        "normal",
                        0.15,
                        "下午嘴饞喝了點冷飲",
                        10,
                    ),
                    # 週末主軸（雪梨生活節奏）
                    (
                        "wknd_grocery_walk",
                        "出門採買補貨散步",
                        1,
                        "週末睡到自然醒，慢慢吃早餐看陽光",
                        "下午出門去超市補貨，順便在雪梨街頭散步",
                        "提著戰利品回家，順手買了杯咖啡",
                        "晚上把冰箱塞滿覺得很療癒",
                        "happy",
                        0.35,
                        "下午在外面嘴饞喝了冰飲又吹到風",
                        12,
                    ),
                    (
                        "wknd_home_mochi",
                        "在家大掃除陪麻糬",
                        1,
                        "早上在整理畫桌跟洗床單，麻糬一直跟前跟後",
                        "下午幫麻糬梳毛剪指甲，跟牠鬥智鬥勇",
                        "家裡變得很乾淨，泡了壺熱烏龍茶發呆",
                        "晚上窩在毯子裡看劇放空",
                        "normal",
                        0.15,
                        "晚上吃太飽稍微有點脹氣",
                        12,
                    ),
                    (
                        "wknd_cafe_recharge",
                        "去喜歡的咖啡廳放空",
                        1,
                        "早上挑了本畫冊出門，打算去常去的角落坐坐",
                        "下午在咖啡廳邊看人來人往邊隨手塗鴉",
                        "充電充得很夠，吹著傍晚的風慢慢散步回家",
                        "晚上靈感不錯，心情輕飄飄的",
                        "happy",
                        0.30,
                        "在咖啡廳點了冰拿鐵結果腸胃又鬧脾氣",
                        10,
                    ),
                ]
                conn.executemany(
                    """
                    INSERT INTO daily_mainlines (
                        mainline_key, title, is_weekend,
                        morning_desc, afternoon_desc, evening_desc, night_desc,
                        evening_mood, gi_risk, gi_cause_hint, weight
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    mainlines,
                )

            if conn.execute("SELECT COUNT(*) FROM canned_lines").fetchone()[0] == 0:
                canned_rows = [
                    # 廁所極簡（4~6 字，台灣口語）
                    ("restroom", "normal", "去一下廁所", 12),
                    ("restroom", "normal", "還在廁所喔", 10),
                    ("restroom", "normal", "等我一下啦", 10),
                    ("restroom", "sick", "還在廁所裡", 12),
                    ("restroom", "sick", "肚子又絞了", 12),
                    # 腸胃不適 / 疲憊示弱（4~6 字，成熟依賴）
                    ("sick", "any", "肚子有點悶", 12),
                    ("sick", "any", "先喝點溫水", 12),
                    ("sick", "any", "沒什麼力氣", 10),
                    ("sick", "any", "腸胃又鬧了", 10),
                    ("tired", "any", "今天好累喔", 12),
                    ("tired", "any", "想靠著你了", 10),
                    ("tired", "any", "讓我喘口氣", 10),
                    # 敷衍訊息反擊（？、嗯、喔、哈）
                    ("low_effort", "normal", "？", 12),
                    ("low_effort", "normal", "在忙嗎", 10),
                    ("low_effort", "normal", "嗯？", 10),
                    ("low_effort", "normal", "敷衍我喔", 8),
                    ("low_effort", "tired", "⋯", 10),
                    # 防卡保底短句
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
                    # 日常與雪梨生活
                    ("drawing", "neutral", "normal", "any", 0, "畫桌旁邊永遠放著保溫杯，冷掉就不再喝了。", 12),
                    ("sydney", "neutral", "normal", "afternoon", 1, "雪梨下午的陽光很舒服，走去超商買東西心情都會變好。", 12),
                    ("mochi", "good", "happy", "any", -1, "麻糬剛剛直接踩過我的數位板，還一臉理所當然。", 12),
                    ("mochi", "neutral", "normal", "evening", -1, "麻糬蹲在窗邊看鳥看得超入迷，叫牠都不理。", 10),
                    # 原生家庭與童年性格底色（中產各自忙碌、早熟不愛麻煩人、敏感易緊張）
                    ("roots", "neutral", "normal", "night", -1, "從小家裡大家各忙各的，習慣有事自己先消化完再說。", 10),
                    ("roots", "neutral", "tired", "night", -1, "小時候一緊張就會肚子痛，長大後忙起來這個毛病還是沒變。", 11),
                    # 好的記憶（溫暖有光）
                    ("good_memory", "good", "happy", "any", -1, "記得以前在台北下雨天收工，去巷口喝到一碗熱湯整個人都活過來。", 10),
                    ("good_memory", "good", "happy", "evening", -1, "第一次收到陌生人私訊說被我的插畫療癒到，那張截圖我還留著。", 10),
                    ("good_memory", "good", "normal", "afternoon", 1, "剛來雪梨沒多久在海邊發呆吹風，突然覺得一個人也能過得很好。", 9),
                    # 不好的記憶（警戒、委屈、硬撐的代價，僅在疲憊或不適時偶爾浮現）
                    ("bad_memory", "bad", "tired", "night", -1, "以前剛接案時不敢拒絕別人，熬夜硬撐到半夜自己跑去掛急診。", 8),
                    ("bad_memory", "bad", "sick", "any", -1, "小時候有次被誤會卻懶得解釋，悶在心裡結果連拉兩天肚子。", 7),
                    # 成熟依賴與立場邊界
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
    def _weighted_choice(rows: List[sqlite3.Row]) -> Optional[sqlite3.Row]:
        if not rows:
            return None
        weights = [max(1, int(r["weight"])) for r in rows]
        return random.choices(rows, weights=weights, k=1)[0]

    def _ensure_today_mainline(self, conn: sqlite3.Connection, dt: datetime.datetime) -> Dict[str, Any]:
        """確保今天已鎖定一條每日主軸；換日時自動抽一條符合平日/週末的新主軸。"""
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
                        mood = 'normal', mood_until = 0,
                        gi_status = 'normal', gi_until = 0, restroom_until = 0,
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
        gi_status: Optional[str] = None,
        gi_duration_sec: int = 2400,
        restroom_sec: int = 0,
        gi_cause: Optional[str] = None,
    ) -> None:
        now = datetime.datetime.now(SYDNEY_TZ).timestamp()
        with self._connect() as conn:
            row = dict(conn.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone())
            new_mood = mood if mood is not None else row["mood"]
            new_mood_until = (now + mood_duration_sec) if mood is not None else row["mood_until"]

            new_gi = gi_status if gi_status is not None else row["gi_status"]
            new_gi_until = (now + gi_duration_sec) if gi_status is not None else row["gi_until"]
            new_gi_triggered = 1 if gi_status == "upset" else row["gi_triggered_today"]
            new_cause = gi_cause if gi_cause is not None else row["gi_cause"]

            new_restroom_until = (now + restroom_sec) if restroom_sec > 0 else row["restroom_until"]

            conn.execute(
                """
                UPDATE runtime_state
                SET mood = ?, mood_until = ?, gi_status = ?, gi_until = ?,
                    gi_triggered_today = ?, gi_cause = ?, restroom_until = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    new_mood,
                    new_mood_until,
                    new_gi,
                    new_gi_until,
                    new_gi_triggered,
                    new_cause,
                    new_restroom_until,
                    now,
                ),
            )

    def evaluate_causal_events(self, user_text: str = "", now_dt: Optional[datetime.datetime] = None) -> None:
        """
        圍繞每日主軸的因果推進（非隨機亂跳）：
        1. 對話關鍵字若觸及誇獎/貓咪/美食，順勢帶出好心情。
        2. 下午到晚上時段，依今日主軸的 gi_risk 決定是否誘發腸胃不適（每天最多一次主發作）。
        3. 若正在腸胃不適期間，有合理機率短暫去廁所（180~360 秒）。
        """
        dt = now_dt or datetime.datetime.now(SYDNEY_TZ)
        now = dt.timestamp()
        slot = self._time_slot(dt)

        with self._connect() as conn:
            bundle = self._ensure_today_mainline(conn, dt)
            state = bundle["state"]
            ml = bundle["mainline"]
            if not ml:
                return

            # 1. 互動語意微調情緒（圍繞互動因果）
            txt = (user_text or "").strip()
            if txt:
                if any(k in txt for k in ("厲害", "好棒", "喜歡", "好看", "麻糬", "好吃", "辛苦了", "抱抱")):
                    if state["gi_status"] == "normal" and now >= state["mood_until"]:
                        self.set_state(mood="happy", mood_duration_sec=2700)
                elif any(k in txt for k in ("喝冰", "冰咖啡", "吃辣", "熬夜", "別睡")):
                    if state["gi_triggered_today"] == 0 and random.random() < 0.45:
                        self.set_state(
                            gi_status="upset",
                            gi_duration_sec=3000,
                            restroom_sec=random.randint(180, 320),
                        )

            # 2. 下午/傍晚依今日主軸風險自然誘發腸胃體質（每日最多主發作一次）
            if slot in ("afternoon", "evening") and state["gi_triggered_today"] == 0 and state["gi_status"] == "normal":
                # 每次檢查給予平滑小機率，累積在下午到傍晚期間接近 gi_risk
                step_prob = float(ml["gi_risk"]) * 0.18
                if random.random() < step_prob:
                    self.set_state(
                        gi_status="upset",
                        gi_duration_sec=random.randint(2400, 4200),
                        restroom_sec=random.randint(180, 360),
                        gi_cause=ml["gi_cause_hint"],
                    )

            # 3. 腸胃不適期間，若剛結束上一次廁所一段時間，可能再跑一趟
            if state["gi_status"] == "upset" and now > state["restroom_until"] + 900:
                if random.random() < 0.25:
                    self.set_state(restroom_sec=random.randint(150, 300))

    def compute_snapshot(self, now_dt: Optional[datetime.datetime] = None) -> Dict[str, Any]:
        """計算當下完整狀態（圍繞每日主軸 ＋ 體質狀態 ＋ 字數與連發規則）。"""
        dt = now_dt or datetime.datetime.now(SYDNEY_TZ)
        now = dt.timestamp()
        slot = self._time_slot(dt)
        is_weekend = dt.weekday() >= 5

        with self._connect() as conn:
            bundle = self._ensure_today_mainline(conn, dt)
            row = bundle["state"]
            ml = bundle["mainline"]

            # 依時段取得主軸當下階段描述與預設情緒
            if ml:
                phase_map = {
                    "morning": (ml["morning_desc"], "normal"),
                    "afternoon": (ml["afternoon_desc"], "normal"),
                    "evening": (ml["evening_desc"], ml["evening_mood"]),
                    "night": (ml["night_desc"], ml["evening_mood"]),
                }
                phase_desc, default_mood = phase_map[slot]
            else:
                phase_desc, default_mood = ("日常自由步調", "normal")

            mood = row["mood"] if now < row["mood_until"] else default_mood
            gi = row["gi_status"] if now < row["gi_until"] else "normal"
            in_restroom = now < row["restroom_until"]

            if mood != row["mood"] or gi != row["gi_status"]:
                conn.execute(
                    """
                    UPDATE runtime_state
                    SET mood = ?, gi_status = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    (mood, gi, now),
                )

        # 動態決定連發則數與每則字數上限
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
            "time_slot": slot,
            "is_weekend": is_weekend,
            "mainline_title": row["mainline_title"],
            "mainline_phase": phase_desc,
            "mood": mood,
            "gi_status": gi,
            "gi_cause": row["gi_cause"] if gi != "normal" else "",
            "in_restroom": in_restroom,
            "mode": mode,
            "min_msgs": min_msgs,
            "max_msgs": max_msgs,
            "max_chars": max_chars,
            "delay_range": delay_range,
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
        """輕量 RAG：圍繞今日主軸 ＋ 當下狀態，精準撈取最多 1 條背景線索與動態記憶。"""
        mood = snapshot["mood"]
        slot = snapshot["time_slot"]
        weekend_flag = 1 if snapshot["is_weekend"] else 0

        # 不好的記憶只在疲憊或腸胃不適時低機率浮現，平常以中性與好記憶為主
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
                """
                SELECT * FROM daily_memories
                ORDER BY importance DESC, id DESC
                LIMIT 1
                """
            ).fetchone()

            arc_row = conn.execute(
                """
                SELECT * FROM story_arcs
                WHERE is_active = 1
                ORDER BY id ASC
                LIMIT 1
                """
            ).fetchone()

        arc_clue = None
        if arc_row:
            steps = json.loads(arc_row["steps_json"])
            idx = min(max(0, int(arc_row["current_step"])), len(steps) - 1)
            if steps:
                arc_clue = f"{arc_row['title']}：{steps[idx]}"

        return {
            "today_mainline": f"{snapshot['mainline_title']}（{snapshot['mainline_phase']}）",
            "gi_context": snapshot["gi_cause"] if snapshot["gi_status"] != "normal" else None,
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


if __name__ == "__main__":
    db = ReonaBGDB(":memory:")
    db.evaluate_causal_events("今天工作辛苦了，麻糬乖嗎")
    snap = db.compute_snapshot()
    clues = db.retrieve_rag_clue(snap)
    print("Snapshot:", json.dumps(snap, ensure_ascii=False, indent=2))
    print("RAG Clues:", json.dumps(clues, ensure_ascii=False, indent=2))
