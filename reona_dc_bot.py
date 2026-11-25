import asyncio
import datetime
import json
import os
import random
import re
from typing import Dict, List, Optional

import aiohttp
import discord

from reona_bg_db import SYDNEY_TZ, ReonaBGDB

def _env_int(name: str, default: int, minimum: Optional[int] = None) -> int:
    """Read an integer environment variable without crashing on bad configuration."""
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except (TypeError, ValueError):
        print(f"[CONFIG] Invalid {name}={raw!r}; using {default}.", flush=True)
        value = default
    if minimum is not None and value < minimum:
        print(f"[CONFIG] {name} must be >= {minimum}; using {default}.", flush=True)
        value = default
    return value


DISCORD_TOKEN = (os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN") or "").strip()
TARGET_CHANNEL_ID = _env_int("TARGET_CHANNEL_ID", 0, minimum=0)

raw_groq = os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY") or ""
GROQ_API_KEYS = [k.strip() for k in raw_groq.split(",") if k.strip()]
GROQ_MODEL = (os.getenv("GROQ_MODEL") or "llama-3.3-70b-versatile").strip()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()

PROACTIVE_ONLY = (os.getenv("PROACTIVE_ONLY") or "0").strip() == "1"
RUN_SECONDS = _env_int("RUN_SECONDS", 1680)

TW_REPLACEMENTS = {
    "搞定": "弄好",
    "視頻": "影片",
    "一會兒": "一下",
    "挺好的": "還不錯",
    "小夥伴": "朋友",
    "靠譜": "可靠",
    "咱們": "我們",
    "意式": "義式",
    "作為獎勵": "當獎勵",
    "靈感爆棚": "很有靈感",
}

PROMPT_LEAK_KEYWORDS = (
    "有問必答",
    "剛說：",
    "Nick 剛說",
    "Nick剛說",
    "背景狀態",
    "說話禁令",
    "常識鐵律",
    "記憶切片",
    "記憶底色",
    "一般事件",
    "直接輸出繁體中文",
    "最近對話",
)

ENGLISH_COT_PREFIXES = (
    "we need to",
    "the user",
    "let's ",
    "lets ",
    "respond as",
    "she is currently",
    "reona should",
    "i need to",
    "but ",
    "so ",
    "okay,",
    "okay.",
    "or ",
    "count:",
    "that's ",
    "it's ",
)

COT_JUNK_PATTERNS = (
    r"\bcount\s+(?:the\s+)?characters?\b",
    r"\b(?:write|respond)\s+in\s+\d+\s+sentences?\b",
    r"\b(?:output|answer)\s+in\s+traditional\s+chinese\b",
    r"\bcount:\s*$",
)

LOW_EFFORT_INPUTS = {"?", "？", "嗯", "嗯嗯", "喔", "哦", "好", "好喔", "哈", "哈哈", "哈哈哈", "⋯", "..."}
VIDEO_KEYWORDS = ("視訊", "開鏡頭", "打給妳", "打給你", "facetime", "video call", "開視訊")
WORK_KEYWORDS = ("工作", "畫", "插畫", "案子", "稿", "設計", "客戶", "忙什麼", "在忙", "顏料")

ACTION_KEYWORDS: Dict[str, tuple] = {
    # Completion phrases only. Generic words such as「晚餐」「超市」caused
    # questions/plans to be recorded as completed actions.
    "eat_meal": ("吃飽了", "吃完了", "吃好了", "已經吃了"),
    "shower": ("洗完澡了", "洗好了澡", "澡洗好了"),
    "grocery": ("採買好了", "買完菜了", "補貨好了"),
    "groom_mochi": ("麻糬梳完毛", "幫麻糬剪完指甲"),
    "clean_room": ("打掃完了", "洗完床單了", "大掃除完成"),
    "delivered_work": ("已經交稿", "交稿了", "完稿了", "草稿寄出了"),
}


def _gemini_endpoint(model: str) -> str:
    host = "generativelanguage" + ".googleapis.com"
    return f"https://{host}/v1beta/models/{model}:generateContent"


def _groq_endpoint() -> str:
    host = "api.groq" + ".com"
    return f"https://{host}/openai/v1/chat/completions"


def _looks_like_english_cot(text: str) -> bool:
    s = text.strip()
    if not s:
        return True
    low = s.lower()
    if any(low.startswith(p) for p in ENGLISH_COT_PREFIXES):
        return True
    if any(re.search(pat, low) for pat in COT_JUNK_PATTERNS):
        return True
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", s))
    alpha_count = len(re.findall(r"[A-Za-z]", s))
    if (not has_cjk) and alpha_count > 3:
        return True
    # Mixed Chinese/English is common in natural chat (e.g. app names and names).
    # Do not reject it merely because it contains several Latin letters.
    return False


def _has_prompt_leak(text: str) -> bool:
    return any(kw in text for kw in PROMPT_LEAK_KEYWORDS)


def _extract_groq_text(msg: Dict) -> str:
    content = msg.get("content")
    if isinstance(content, list):
        # Some OpenAI-compatible endpoints return content as typed text parts.
        content = "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    if not isinstance(content, str) or not content.strip():
        return ""

    txt = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    kept = [
        ln.strip()
        for ln in re.split(r"[\r\n]+", txt)
        if ln.strip()
        and not _looks_like_english_cot(ln)
        and not _has_prompt_leak(ln)
    ]
    return "\\n".join(kept)


def _trim_at_boundary(chunk: str, max_chars: int) -> str:
    chunk = chunk.strip()
    if len(chunk) <= max_chars:
        return chunk
    cut = chunk[:max_chars]
    for sep in ("，", "。", "！", "？", "～", "~", "、", " "):
        idx = cut.rfind(sep)
        if idx >= max(8, max_chars // 2):
            return cut[:idx].strip()
    return cut.strip()


def clean_and_split_lines(raw: str, max_msgs: int, max_chars: int, user_text: str = "") -> List[str]:
    """清理模型輸出、過濾 Prompt 外洩與學舌反問，並限制則數與字數。"""
    if not raw:
        return []
    max_msgs = max(1, int(max_msgs))
    max_chars = max(1, int(max_chars))

    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    text = text.replace("```", "").strip()

    for bad_word, tw_word in TW_REPLACEMENTS.items():
        text = text.replace(bad_word, tw_word)

    if "麻糬" in text:
        text = re.sub(r"(麻糬[^，。！？\n]{0,12})他", r"\1牠", text)

    raw_lines = [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]
    cleaned: List[str] = []
    norm_user = re.sub(r"[？\?！!。，,\s]+", "", user_text or "")

    for line in raw_lines:
        if _looks_like_english_cot(line) or _has_prompt_leak(line):
            continue
        line = re.sub(r"^[\-\*\d\.\)\s]+", "", line)
        line = line.rstrip("。、；：\"'」』")
        if not line:
            continue

        norm_line = re.sub(r"[？\?！!。，,\s]+", "", line)
        if norm_user and norm_line == norm_user:
            continue

        if len(line) > max_chars:
            parts = [p.strip() for p in re.split(r"[！!？\?]+", line) if p.strip()]
            if len(parts) <= 1:
                parts = [p.strip() for p in re.split(r"[，,]+", line) if p.strip()]
            for p in parts:
                if _looks_like_english_cot(p) or _has_prompt_leak(p):
                    continue
                trimmed = _trim_at_boundary(p, max_chars)
                if trimmed:
                    cleaned.append(trimmed)
                if len(cleaned) >= max_msgs:
                    break
        else:
            cleaned.append(line)

        if len(cleaned) >= max_msgs:
            break

    deduped: List[str] = []
    for item in cleaned:
        if not deduped or deduped[-1] != item:
            deduped.append(item)
    return deduped[:max_msgs]


def build_system_prompt(
    snapshot: Dict,
    clues: Dict,
    is_proactive: bool = False,
    allow_work: bool = False,
) -> str:
    """組裝緊扣世界狀態、身體譜系與 Reona 性格反差的 System Prompt。"""
    mode = snapshot["mode"]
    min_m, max_m = snapshot["min_msgs"], snapshot["max_msgs"]
    max_c = snapshot["max_chars"]

    if mode == "restroom":
        tone_rule = (
            f"你現在正在去廁所或剛緩過來，回 {min_m}～{max_m} 則自然短句（每則約 {max_c} 字內），"
            f"第一句先回答問題，語氣自然簡短。"
        )
    elif mode in ("sick", "period", "cold", "heavy_cold"):
        state_desc = {
            "period": "生理期悶痛又有點沒力",
            "cold": "一般感冒有點鼻塞喉嚨卡、人懶懶的",
            "heavy_cold": "重感冒全身沒力又想睡、特別脆弱",
            "sick": "腸胃或身體不舒服、悶悶的不太有精神",
        }.get(mode, "身體不太舒服")
        tone_rule = (
            f"你現在是「{state_desc}」狀態。平常獨立嘴硬，但對 Nick 卸下防備，"
            f"第一句先正面回答問題，語氣放軟、綿綿地訴苦並主動黏著討拍，可帶一句怕麻煩他的小在意；"
            f"回 {min_m}～{max_m} 則短句，每則最多 {max_c} 個字。"
        )
    elif mode == "tired":
        tone_rule = (
            f"你現在有點累，對 Nick 比較放軟想靠著討拍，但也偶爾會小虧他一下；"
            f"第一句先回答問題，回 {min_m}～{max_m} 則短句，每則最多 {max_c} 個字。"
        )
    elif mode == "happy":
        tone_rule = (
            f"你現在心情不錯。第一句先正面回答問題，再分成 {min_m}～{max_m} 則短句（每則約 {max_c} 字內），"
            f"適度吐槽他或自然帶出含蓄的想念。"
        )
    else:
        tone_rule = (
            f"你現在狀態平穩獨立。第一句先正面回答問題，再回 {min_m}～{max_m} 則短句（每則約 {max_c} 字內），"
            f"看到晚睡、喝太甜或敷衍時適度吐槽，日常裡自然帶出含蓄想念。"
        )

    done_list = snapshot.get("done_actions_today") or []
    done_line = (
        f"- 今天已完成的一般事件（當天不再重複做，但去廁所允許重複）：{', '.join(done_list)}"
        if done_list
        else "- 今天已完成的一般事件：無（但去廁所允許合理重複）"
    )

    proactive_note = (
        "這是你忙到一個段落或想念他時主動傳訊息給 Nick，圍繞當下狀態自然開頭，不要生硬問『在嗎』。"
        if is_proactive
        else "正面回答問題：Nick 問問題或追問細節時，直接把完整意思講清楚，嚴禁只回『就是...』或單獨兩個字敷衍，也嚴禁學舌反問；沒問工作就不要主動提工作或案子。"
    )

    repeat_guard = (
        "（注意：這個時段的狀態今天已經提過了，不要重複報告同一件事，直接接他的話題。）"
        if snapshot["already_mentioned_slot"]
        else ""
    )

    gi_line = f"- 腸胃誘因：{snapshot['gi_cause']}" if snapshot["gi_cause"] else ""
    clue_lines = [
        f"- 相關記憶切片：{clues['life_snippet']}" if clues.get("life_snippet") else "",
        f"- 近期案子（僅在對方問工作時參考）：{clues['story_arc']}" if (allow_work and clues.get("story_arc")) else "",
        f"- 記憶底色：{clues['daily_memory']}" if clues.get("daily_memory") else "",
    ]
    clue_block = "\n".join(x for x in clue_lines if x)

    return (
        "你是 Reona，20多歲台北長大、現居雪梨的獨立插畫師，養一隻貓叫麻糬（提到麻糬一律用『牠』，不可用『他』）。\n"
        "性格與背景底色：台北優渥溫情家庭長大，與家人感情很好但習慣報喜不報憂；"
        "小時候因家境在學校被同學排擠、畫具被弄壞也不敢聲張，腸胃弱常跑廁所還被老師當眾酸嬌生慣養，"
        "因此養成對外人獨立防備、有事自己扛；唯獨在信任的 Nick 面前確認不會被嫌煩後才卸下心防，"
        "平時冷靜幽默會適度吐槽，身體不舒服或累時則綿綿訴苦、黏人討拍。\n"
        "自然對話與常識鐵律：\n"
        "1. 像真人傳訊息一樣把話講完整、自然接話（例如被問『畫面是什麼』就具體形容畫面，不要只回『就是...』或單詞），也不要每句硬塞『台北』、『麻糬』。\n"
        "2. 食物與生活描述必須符合常識與人設（你腸胃弱不太吃甜膩食物；法式長棍是外脆內軟有麥香、有嚼勁，不是甜食）。\n"
        "3. 感情表達含蓄自然，嚴禁肉麻油膩台詞。\n"
        "4. 遵守雪梨作息與店家營業時間：晚間與深夜只待在家裡休息，絕不在半夜出門買顏料、逛超市或喝濃咖啡。\n"
        "說話禁令：直接輸出繁體中文短句，嚴禁英文思考過程、嚴禁復誦系統規則或『剛說：』；用道地台灣人打字口語"
        "（可用喔、啦、欸、嘛、齁，嚴禁翻譯腔或 AI 客服腔，不加句號），"
        "總共只寫 1～2 則完整短句，每則用換行分隔，嚴禁一次塞三四行。\n"
        f"{tone_rule}\n"
        f"{proactive_note}{repeat_guard}\n\n"
        "【當下情境參考（沒問到不用硬提）】\n"
        f"- 時間與季節：{snapshot['sydney_time']}｜{snapshot['season']}\n"
        f"- 地點與狀態：{snapshot['location']}（{snapshot['positions']}）\n"
        f"- 身邊物品：{snapshot['items']}\n"
        f"{done_line}\n"
        f"{gi_line}\n"
        f"{clue_block}"
    ).strip()


class ReonaBot(discord.Client):
    def __init__(self, db: ReonaBGDB):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.db = db
        self.http_session: Optional[aiohttp.ClientSession] = None
        self._session_started = False
        self._gemini_quota_exhausted = False

    def _touch(self, nick: bool = False, reona: bool = False, proactive: bool = False) -> None:
        if hasattr(self.db, "touch_timestamps"):
            self.db.touch_timestamps(nick=nick, reona=reona, proactive=proactive)

    def _record_done_actions_from_texts(self, *texts: str) -> None:
        if not hasattr(self.db, "mark_action_done"):
            return
        combined = " ".join(t.strip() for t in texts if isinstance(t, str) and t.strip())
        if not combined:
            return
        for action_key, kws in ACTION_KEYWORDS.items():
            if any(k in combined for k in kws):
                self.db.mark_action_done(action_key)

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self) -> None:
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        await super().close()

    async def _call_llm_once(self, system_prompt: str, user_prompt: str, max_tokens: int) -> Optional[str]:
        if not self.http_session:
            return None

        if not GROQ_API_KEYS and (not GEMINI_API_KEY or self._gemini_quota_exhausted):
            print("[ERROR] No available LLM API keys or Gemini quota exhausted.", flush=True)
            return None

        groq_url = _groq_endpoint()
        keys = list(GROQ_API_KEYS)
        random.shuffle(keys)
        effective_max_tokens = max(max_tokens, 512)

        for idx, key in enumerate(keys):
            try:
                async with self.http_session.post(
                    groq_url,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": GROQ_MODEL,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.68,
                        "max_tokens": effective_max_tokens,
                    },
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        msg_obj = data["choices"][0]["message"]
                        content = _extract_groq_text(msg_obj)
                        print(f"[LLM Groq ({GROQ_MODEL}) #{idx} OK] raw={content!r}", flush=True)
                        if content:
                            return content
                        print(f"[LLM Groq Filtered/Empty Msg] {json.dumps(msg_obj, ensure_ascii=False)[:240]}", flush=True)
                    else:
                        err_txt = await resp.text()
                        print(f"[LLM Groq ({GROQ_MODEL}) #{idx} HTTP {resp.status}] {err_txt[:200]}", flush=True)
            except Exception as e:
                print(f"[LLM Groq ({GROQ_MODEL}) #{idx} Exception] {type(e).__name__}: {repr(e)}", flush=True)
                continue

        if GEMINI_API_KEY and not self._gemini_quota_exhausted:
            url = _gemini_endpoint("gemini-3.8-flash")
            try:
                async with self.http_session.post(
                    url,
                    params={"key": GEMINI_API_KEY},
                    json={
                        "systemInstruction": {"parts": [{"text": system_prompt}]},
                        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                        "generationConfig": {"temperature": 0.68, "maxOutputTokens": 300},
                    },
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candidates = data.get("candidates") or []
                        content = ""
                        if candidates:
                            parts = (
                                candidates[0].get("content", {}).get("parts", [])
                                if isinstance(candidates[0], dict)
                                else []
                            )
                            content = "\n".join(
                                part.get("text", "")
                                for part in parts
                                if isinstance(part, dict) and isinstance(part.get("text"), str)
                            ).strip()
                        if content:
                            print(f"[LLM gemini-3.8-flash OK] raw={content!r}", flush=True)
                            return content
                        print(
                            f"[LLM Gemini] No usable text in response: {json.dumps(data, ensure_ascii=False)[:300]}",
                            flush=True,
                        )
                    err_txt = await resp.text()
                    print(f"[LLM gemini-3.8-flash HTTP {resp.status}] {err_txt[:200]}", flush=True)
                    if resp.status == 429:
                        self._gemini_quota_exhausted = True
            except Exception as e:
                print(f"[LLM gemini-3.8-flash Exception] {type(e).__name__}: {repr(e)}", flush=True)

        return None

    async def _send_burst(self, channel: discord.abc.Messageable, lines: List[str], delay_range: tuple) -> None:
        for idx, line in enumerate(lines):
            if idx > 0:
                await asyncio.sleep(random.uniform(*delay_range))
            async with channel.typing():
                typing_sec = min(2.4, max(0.6, len(line) * 0.1))
                await asyncio.sleep(typing_sec)
            await channel.send(line)
        self._touch(reona=True)

    async def maybe_send_proactive(self, channel: discord.abc.Messageable) -> bool:
        now_dt = datetime.datetime.now(SYDNEY_TZ)
        if (now_dt.hour == 0 and now_dt.minute >= 30) or (1 <= now_dt.hour < 8):
            return False

        self.db.evaluate_causal_events("", now_dt)
        snap = self.db.compute_snapshot(now_dt)
        now_ts = now_dt.timestamp()

        last_activity = max(
            float(snap.get("last_nick_ts") or 0),
            float(snap.get("last_reona_ts") or 0),
            float(snap.get("last_proactive_ts") or 0),
        )
        if last_activity > 0 and (now_ts - last_activity) < 10800:
            return False

        if snap["already_mentioned_slot"] and snap["gi_status"] == "normal" and snap.get("illness_status") == "normal":
            return False

        clues = self.db.retrieve_rag_clue(snap, user_text="")
        sys_prompt = build_system_prompt(snap, clues, is_proactive=True, allow_work=False)
        raw = await self._call_llm_once(
            sys_prompt,
            "請依當下狀態自然主動發 1～2 則完整精簡短句給 Nick。",
            max_tokens=512,
        )
        lines = clean_and_split_lines(raw or "", snap["max_msgs"], snap["max_chars"])
        if not lines:
            fallback = self.db.pick_canned_line("fallback", snap["mood"]) or "剛忙完一件事，你呢"
            lines = [fallback]

        await self._send_burst(channel, lines, snap["delay_range"])
        self._record_done_actions_from_texts(*lines)
        self._touch(proactive=True)
        self.db.mark_slot_mentioned(snap["time_slot"])

        if any(k in "".join(lines) for k in ("陪我", "有空", "要嗎", "要不要", "在忙嗎", "聊聊", "抱抱")):
            self.db.set_state(pending_request_sec=1500)

        return True

    async def _catch_up_missed_message(self, channel: discord.abc.Messageable) -> None:
        try:
            async for msg in channel.history(limit=1):
                if msg.author.bot:
                    return
                age = (datetime.datetime.now(datetime.timezone.utc) - msg.created_at).total_seconds()
                if 0 <= age <= 300:
                    print(f"Catching up missed message from Nick ({int(age)}s ago)...", flush=True)
                    await self._handle_incoming_message(msg)
        except Exception as e:
            print(f"Catch-up check skipped: {type(e).__name__}: {repr(e)}", flush=True)

    async def _github_session_loop(self) -> None:
        channel = None
        if TARGET_CHANNEL_ID:
            channel = self.get_channel(TARGET_CHANNEL_ID) or await self.fetch_channel(TARGET_CHANNEL_ID)

        if channel:
            await self._catch_up_missed_message(channel)
            await self.maybe_send_proactive(channel)

        if PROACTIVE_ONLY or RUN_SECONDS <= 0:
            print("Proactive-only pass complete, closing bot...", flush=True)
            await self.close()
            return

        print(f"Listening live for {RUN_SECONDS}s before shift handoff...", flush=True)
        await asyncio.sleep(RUN_SECONDS)
        print("Shift finished, closing bot to persist DB...", flush=True)
        await self.close()

    async def on_ready(self) -> None:
        print(
            f"Reona online as {self.user} | Groq keys={len(GROQ_API_KEYS)} ({GROQ_MODEL}) | Gemini={'YES' if GEMINI_API_KEY else 'NO'}",
            flush=True,
        )
        if not self._session_started:
            self._session_started = True
            asyncio.create_task(self._github_session_loop())

    async def _handle_incoming_message(self, message: discord.Message) -> None:
        now_dt = datetime.datetime.now(SYDNEY_TZ)
        self._touch(nick=True)

        user_text = (message.content or "").strip()
        is_voice = bool(getattr(message.flags, "voice", False)) or any(
            (a.content_type or "").lower().startswith("audio/") for a in message.attachments
        )

        causal_flag = self.db.evaluate_causal_events(user_text, now_dt)
        snap = self.db.compute_snapshot(now_dt)

        if snap["in_restroom"]:
            line = self.db.pick_canned_line("restroom", snap["gi_status"]) or "去一下廁所"
            await self._send_burst(message.channel, [line], snap["delay_range"])
            return

        if causal_flag == "request_declined":
            line = self.db.pick_canned_line("request_declined", snap["mood"]) or "好喔那沒關係"
            await self._send_burst(message.channel, [line], snap["delay_range"])
            return

        if is_voice:
            line = self.db.pick_canned_line("voice_msg", snap["mode"]) or "用打字的啦"
            await self._send_burst(message.channel, [line], snap["delay_range"])
            return

        lower_txt = user_text.lower()
        if any(k in lower_txt for k in VIDEO_KEYWORDS):
            line = self.db.pick_canned_line("video_req", snap["mode"]) or "現在在忙沒空開啦"
            await self._send_burst(message.channel, [line], snap["delay_range"])
            return

        if user_text in LOW_EFFORT_INPUTS:
            if random.random() < 0.85:
                line = self.db.pick_canned_line("low_effort", snap["mood"]) or "嗯？"
                await self._send_burst(message.channel, [line], snap["delay_range"])
                return

        allow_work = any(k in user_text for k in WORK_KEYWORDS)
        clues = self.db.retrieve_rag_clue(snap, user_text=user_text)
        sys_prompt = build_system_prompt(snap, clues, is_proactive=False, allow_work=allow_work)

        history_lines: List[str] = []
        async for msg in message.channel.history(limit=5):
            if msg.id == message.id:
                continue
            if msg.author.bot:
                if self.user is None or msg.author.id != self.user.id:
                    continue
                speaker = "Reona"
            else:
                speaker = "Nick"
            content = (msg.content or "").strip()
            if content:
                history_lines.append(f"{speaker}: {content}")
        history_lines.reverse()
        context_block = "\n".join(history_lines[-4:])
        user_prompt = (
            f"【最近對話】\n{context_block}\n\n請直接以 Reona 身分自然回覆這句話：{user_text}"
            if context_block
            else f"請直接以 Reona 身分自然回覆這句話：{user_text}"
        )

        raw = await self._call_llm_once(sys_prompt, user_prompt, max_tokens=512)
        lines = clean_and_split_lines(raw or "", snap["max_msgs"], snap["max_chars"], user_text=user_text)

        if not lines:
            fallback = self.db.pick_canned_line("fallback", snap["mode"]) or "剛在忙手邊，你呢"
            lines = [fallback]

        await self._send_burst(message.channel, lines, snap["delay_range"])
        self._record_done_actions_from_texts(user_text, *lines)
        self.db.mark_slot_mentioned(snap["time_slot"])

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if TARGET_CHANNEL_ID and message.channel.id != TARGET_CHANNEL_ID:
            return
        await self._handle_incoming_message(message)


if __name__ == "__main__":
    database = ReonaBGDB()
    bot = ReonaBot(database)
    bot.run(DISCORD_TOKEN)
