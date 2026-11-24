import re
import sqlite3
from pathlib import Path

DB_PATH = Path("reona_background.db")

SEED_SNIPPETS = [
    # 1. 核心底色（沒對到特定話題時的輕量備用）
    (
        "persona",
        "個性,怕冷,挑嘴,撒嬌,質感",
        10,
        "台北優渥獨生女，160cm/40kg怕冷挑嘴，慢熱黏人注重質感。",
    ),
    # 2. 國中小冷午餐、冷氣腹痛、就醫吃藥與溫水習慣
    (
        "school_gi",
        "肚子,胃,腸胃,拉肚子,腹瀉,反酸,胃痛,腹痛,冷,冰,溫水,藥,益生菌,午餐,便當,空調",
        30,
        "從小功能性敏感腸胃，曾因冷午餐與冷氣腹痛跑廁所，需常備藥與保溫瓶溫水。",
    ),
    # 3. 求學到畢業來雪梨做自由插畫師
    (
        "sydney_work",
        "雪梨,插畫,畫畫,畫室,稿,作品集,畢業,美術,台北,工作,接案,繪本",
        25,
        "美術班到插畫系畢業後飛雪梨做自由插畫師，時間彈性但忙起來常忘記吃飯喝水。",
    ),
    # 4. 麻糬設定（單一貓名鎖定，不叫別名）
    (
        "pet_mochi",
        "麻糬,貓,逗貓,凍乾,罐頭,毛",
        25,
        "只養一隻貓叫麻糬，平時餵凍乾與逗貓棒互動，早晚各一罐罐頭。",
    ),
    # 5. 日常作息、探店與散步
    (
        "lifestyle",
        "探店,咖啡,甜點,底片,相機,海邊,散步,晚餐,吃飯",
        15,
        "工作空檔喜歡去雪梨探店、拍底片或海邊散步，偏愛熱食與暖胃料理。",
    ),
]


def init_bg_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS background_snippets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            keywords TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 10,
            snippet TEXT NOT NULL UNIQUE
        )
        """
    )
    existing = cur.execute("SELECT COUNT(*) FROM background_snippets").fetchone()[0]
    if existing == 0:
        cur.executemany(
            """
            INSERT OR IGNORE INTO background_snippets (topic, keywords, priority, snippet)
            VALUES (?, ?, ?, ?)
            """,
            SEED_SNIPPETS,
        )
        conn.commit()
    return conn


def pick_relevant_snippet(
    user_text: str,
    gi_level: str = "normal",
    has_active_action: bool = False,
    db_path: Path = DB_PATH,
) -> str:
    """
    由 Python 在本地查 SQLite，按情境挑出最多 1 條最相關的背景短句。
    一般閒聊且有正在做的動作時回傳空字串，避免干擾當下動作與邏輯。
    """
    txt = re.sub(r"\s+", "", user_text or "")
    conn = init_bg_db(db_path)
    try:
        rows = conn.execute(
            "SELECT topic, keywords, priority, snippet FROM background_snippets"
        ).fetchall()
    finally:
        conn.close()

    best_score = 0
    best_snippet = ""

    for topic, kw_csv, priority, snippet in rows:
        kws = [k.strip() for k in kw_csv.split(",") if k.strip()]
        hit_count = sum(1 for k in kws if k in txt)

        # 若目前腸胃處於 mild / moderate / severe，腸胃背景自動加權
        if topic == "school_gi" and gi_level != "normal":
            hit_count += 2

        if hit_count > 0:
            score = hit_count * 100 + priority
            if score > best_score:
                best_score = score
                best_snippet = snippet

    if best_snippet:
        return best_snippet

    # 沒有對到特定關鍵字時：如果正在做某件事，就不額外塞靜態背景，保持對話俐落
    if has_active_action:
        return ""

    # 偶爾（約 25% 機率）帶出核心底色，其餘時間完全不加，省 Token 且邏輯最穩
    import random

    if random.random() < 0.25:
        for topic, _, _, snippet in rows:
            if topic == "persona":
                return snippet
    return ""


if __name__ == "__main__":
    init_bg_db()
    tests = [
        ("今天冷氣好冷，肚子有點悶悶的", "mild", False),
        ("剛畫完一張插畫，打算去買杯熱拿鐵", "normal", True),
        ("麻糬剛剛又跳上桌子了", "normal", False),
        ("早安", "normal", False),
    ]
    for msg, gi, act in tests:
        print(f"輸入：{msg} | 腸胃={gi} | 動作中={act}")
        print(f"  -> 挑出：{pick_relevant_snippet(msg, gi, act) or '（無，省 Token）'}")
