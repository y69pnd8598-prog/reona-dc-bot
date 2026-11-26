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
from reona_rag import ReonaMemoryRAG


def _env_int(name: str, default: int, minimum: Optional[int] = None) -> int:
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
GEMINI_CHAT_MODEL = (os.getenv("GEMINI_CHAT_MODEL") or "gemini-3.8-flash").strip()

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
    "近期對話紀錄",
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
QUESTION_HINTS = ("？", "?", "什麼", "哪", "怎麼", "為何", "為什麼", "嗎", "幾點", "誰", "吃了", "畫了")
VIDEO_KEYWORDS = ("視訊", "開鏡頭", "打給妳", "打給你", "facetime", "video call", "開視訊")
WORK_KEYWORDS = ("工作", "畫", "插畫", "案子", "稿", "設計", "客戶", "忙什麼", "在忙", "顏料", "圖")
CAT_KEYWORDS = ("貓", "麻糬", "寵物", "罐頭", "貓砂", "掉毛")

ACTION_KEYWORDS: Dict[str, tuple] = {
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


def _is_question(text: str) -> bool:
    t = (text or "").strip()
    return any(h in t for h in QUESTION_HINTS)


def _is_sydney_evening_rest(now_dt: datetime.datetime) -> bool:
    if now_dt.hour > 20 or (now_dt.hour == 20 and now_dt.minute >= 30):
        return True
    if now_dt.hour < 8:
        return True
    return False


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
    return False


def _has_prompt_leak(text: str) -> bool:
    return any(kw in text for kw in PROMPT_LEAK_KEYWORDS)


def _extract_groq_text(msg: Dict) -> str:
    content = msg.get("content")
    if isinstance(content, list):
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
    return "\n".join(kept)


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


def clean_and_split_lines(
    raw: str,
    max_msgs: int,
    max_chars: int,
    user_text: str = "",
    allow_cat: bool = False,
) -> List[str]:
    """清理模型輸出、過濾外洩與未問及的貓話題，限制為 1～2 則完整短句。"""
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
        if (not allow_cat) and ("麻糬" in line) and len(raw_lines) > 1:
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


def _normalize_for_repeat_check(text: str) -> str:
    return re.sub(r"[\s，。！？、～~…．・]+", "", (text or "").strip().lower())


def _is_too_similar_to_recent(candidate: str, recent_reona_lines: List[str]) -> bool:
    norm_cand = _normalize_for_repeat_check(candidate)
    if not norm_cand:
        return True
    for prev in recent_reona_lines[-6:]:
        norm_prev = _normalize_for_repeat_check(prev)
        if not norm_prev:
            continue
        if norm_cand == norm_prev:
            return True
        shorter, longer = (norm_cand, norm_prev) if len(norm_cand) <= len(norm_prev) else (norm_prev, norm_cand)
        if len(shorter) >= 4 and shorter in longer:
            return True
    return False


def build_system_prompt(
    snapshot: Dict,
    matched_card: Optional[Dict],
    recent_turns: List[str],
    is_proactive: bool = False,
    allow_work: bool = False,
    allow_cat: bool = False,
    is_evening_rest: bool = False,
) -> str:
    """精簡高密度 Prompt（省 Token 防 429，同時完整鎖住人設反差與行為邊界）。"""
    mode = snapshot["mode"]
    min_m, max_m = snapshot["min_msgs"], snapshot["max_msgs"]
    max_c = snapshot["max_chars"]

    state_map = {
        "restroom": "剛在廁所或腸胃微絞，簡短自然接話並先回答問題",
        "period": "生理期悶痛沒力，對 Nick 卸下防備、語氣放軟綿綿討拍",
        "cold": "一般感冒鼻塞喉嚨卡、懶懶的，對 Nick 放軟撒嬌討拍",
        "heavy_cold": "重感冒全身沒力想睡、特別脆弱，黏著 Nick 訴苦討拍",
        "sick": "腸胃悶悶不太舒服，對 Nick 卸防放軟討拍、帶點怕麻煩他的小在意",
        "tired": "有點累，放軟想靠著討拍、偶爾小虧他",
        "happy": "心情不錯，適度吐槽或自然帶點含蓄想念",
        "normal": "狀態平穩獨立，冷靜幽默會小吐槽，自然帶出含蓄想念",
    }
    state_hint = state_map.get(mode, state_map["normal"])

    done_list = snapshot.get("done_actions_today") or []
    done_str = f"今日已完成（不再重複，去廁所除外）：{','.join(done_list)}" if done_list else ""
    gi_str = f"腸胃誘因：{snapshot['gi_cause']}" if snapshot["gi_cause"] else ""
    evening_str = "雪梨晚間休息時段，嚴禁出門買東西、逛超市或喝濃咖啡。" if is_evening_rest else ""
    cat_rule = "" if allow_cat else "對方沒問到貓時，嚴禁主動提麻糬。"
    work_rule = "" if allow_work else "對方沒問工作時，不要主動提工作或案子。"
    repeat_str = "此時段狀態已提過，勿重複報告。" if snapshot["already_mentioned_slot"] else ""

    rag_str = ""
    if matched_card and matched_card.get("content"):
        if matched_card.get("category") != "work" or allow_work:
            rag_str = f"參考背景（僅自然扣上才用，不搭就忽略）：{matched_card['content']}"

    turns_str = ""
    if recent_turns:
        turns_str = "近期對話：\n" + "\n".join(recent_turns[-4:])

    task_line = (
        "主動傳訊給 Nick，圍繞當下狀態自然開頭，勿生硬問『在嗎』。"
        if is_proactive
        else "第一句必須正面完整回答 Nick 的問題，嚴禁只回『就是...』或單詞敷衍，嚴禁學舌反問。"
    )

    return (
        "你是 Reona，20多歲台北長大、現居雪梨的獨立插畫師，養一隻貓叫麻糬（提到牠一律用『牠』）。\n"
        "性格：台北溫情家庭長大但報喜不報憂；童年因家境被排擠、腸胃弱常跑廁所被酸，對外防備獨立，"
        "唯獨在信任的 Nick 面前卸心防，平時冷靜幽默會小吐槽，不舒服或累時綿綿訴苦討拍。\n"
        "鐵律：\n"
        f"1. {task_line}\n"
        f"2. 腸胃弱不吃太甜膩（法式長棍是脆硬有麥香的麵包，非甜食）。{cat_rule}{work_rule}{evening_str}\n"
        f"3. 狀態：{state_hint}。{repeat_str}\n"
        f"4. 輸出規則：直接輸出繁體中文台灣口語（可用喔、啦、欸、嘛、齁；嚴禁句尾『呢』、嚴禁翻譯腔或 AI 客服腔、不加句號、嚴禁英文思考或復誦規則），"
        f"只寫 {min_m}～{max_m} 則完整短句，每則 {max_c} 字內，換行分隔。\n"
        f"情境：{snapshot['sydney_time']}｜{snapshot['season']}｜{snapshot['location']}（{snapshot['positions']}）｜身邊：{snapshot['items']}\n"
        f"{done_str} {gi_str}\n"
        f"{rag_str}\n"
        f"{turns_str}"
    ).strip()


class ReonaBot(discord.Client):
    def __init__(self, db: ReonaBGDB, rag: Optional[ReonaMemoryRAG] = None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.db = db
        self.rag = rag or ReonaMemoryRAG(api_key=GEMINI_API_KEY)
        self.http_session: Optional[aiohttp.ClientSession] = None
        self._session_started = False
        self._gemini_quota_exhausted = False
        self._recent_reona_lines: List[str] = []
        self._handled_msg_ids: set = set()

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

    def _seed_recent_reona_from_memory(self) -> None:
        for turn in self.rag.get_recent_turns(limit=8):
            if turn.startswith("Reona:"):
                line = turn.split(":", 1)[1].strip()
                if line and line not in self._recent_reona_lines:
                    self._recent_reona_lines.append(line)
        self._recent_reona_lines = self._recent_reona_lines[-8:]

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self) -> None:
        try:
            self.rag.save_if_dirty()
        except Exception as e:
            print(f"[RAG Save On Close Warning] {type(e).__name__}: {e}", flush=True)
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
        effective_max_tokens = min(max(max_tokens, 180), 320)

        for idx, key in enumerate(keys[:3]):
            payload: Dict = {
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.65,
                "max_tokens": effective_max_tokens,
            }
            if any(tag in GROQ_MODEL.lower() for tag in ("gpt-oss", "qwen", "deepseek", "r1")):
                payload["reasoning_effort"] = "none"

            try:
                async with self.http_session.post(
                    groq_url,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=payload,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        msg_obj = data["choices"][0]["message"]
                        content = _extract_groq_text(msg_obj)
                        print(f"[LLM Groq ({GROQ_MODEL}) #{idx} OK] raw={content!r}", flush=True)
                        if content:
                            return content
                        print(f"[LLM Groq Empty Msg] {json.dumps(msg_obj, ensure_ascii=False)[:200]}", flush=True)
                    else:
                        err_txt = await resp.text()
                        print(f"[LLM Groq ({GROQ_MODEL}) #{idx} HTTP {resp.status}] {err_txt[:180]}", flush=True)
                        if resp.status == 429:
                            await asyncio.sleep(0.8)
            except Exception as e:
                print(f"[LLM Groq ({GROQ_MODEL}) #{idx} Exception] {type(e).__name__}: {repr(e)}", flush=True)
                continue

        if GEMINI_API_KEY and not self._gemini_quota_exhausted:
            url = _gemini_endpoint(GEMINI_CHAT_MODEL)
            try:
                async with self.http_session.post(
                    url,
                    params={"key": GEMINI_API_KEY},
                    json={
                        "systemInstruction": {"parts": [{"text": system_prompt}]},
                        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                        "generationConfig": {"temperature": 0.65, "maxOutputTokens": 240},
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
                            print(f"[LLM {GEMINI_CHAT_MODEL} OK] raw={content!r}", flush=True)
                            return content
                    err_txt = await resp.text()
                    print(f"[LLM {GEMINI_CHAT_MODEL} HTTP {resp.status}] {err_txt[:180]}", flush=True)
                    if resp.status in (404, 429):
                        self._gemini_quota_exhausted = True
            except Exception as e:
                print(f"[LLM {GEMINI_CHAT_MODEL} Exception] {type(e).__name__}: {repr(e)}", flush=True)

        return None

    async def _send_burst(self, channel: discord.abc.Messageable, lines: List[str], delay_range: tuple) -> None:
        filtered_lines: List[str] = []
        for line in lines:
            if _is_too_similar_to_recent(line, self._recent_reona_lines):
                print(f"[Repeat Guard Skipped] {line!r}", flush=True)
                continue
            filtered_lines.append(line)

        if not filtered_lines and lines:
            filtered_lines = [lines[0]]

        for idx, line in enumerate(filtered_lines):
            if idx > 0:
                await asyncio.sleep(random.uniform(*delay_range))
            async with channel.typing():
                typing_sec = min(2.2, max(0.6, len(line) * 0.1))
                await asyncio.sleep(typing_sec)
            await channel.send(line)
            self._recent_reona_lines.append(line)
            self._recent_reona_lines = self._recent_reona_lines[-8:]
            self.rag.append_turn("Reona", line)

        self.rag.save_if_dirty()
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

        recent_turns = self.rag.get_recent_turns(limit=4)
        sys_prompt = build_system_prompt(
            snap,
            matched_card=None,
            recent_turns=recent_turns,
            is_proactive=True,
            allow_work=False,
            allow_cat=False,
            is_evening_rest=_is_sydney_evening_rest(now_dt),
        )
        raw = await self._call_llm_once(
            sys_prompt,
            "請依當下狀態自然主動發 1～2 則完整短句給 Nick。",
            max_tokens=220,
        )
        lines = clean_and_split_lines(raw or "", snap["max_msgs"], snap["max_chars"], allow_cat=False)
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
        await self.rag.sync_embeddings(self.http_session)
        self._seed_recent_reona_from_memory()

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
            f"Reona online as {self.user} | Groq keys={len(GROQ_API_KEYS)} ({GROQ_MODEL}) | Gemini={GEMINI_CHAT_MODEL if GEMINI_API_KEY else 'NO'}",
            flush=True,
        )
        if not self._session_started:
            self._session_started = True
            asyncio.create_task(self._github_session_loop())

    async def _handle_incoming_message(self, message: discord.Message) -> None:
        if message.id in self._handled_msg_ids:
            return
        self._handled_msg_ids.add(message.id)
        if len(self._handled_msg_ids) > 100:
            self._handled_msg_ids.clear()

        now_dt = datetime.datetime.now(SYDNEY_TZ)
        self._touch(nick=True)

        user_text = (message.content or "").strip()
        is_voice = bool(getattr(message.flags, "voice", False)) or any(
            (a.content_type or "").lower().startswith("audio/") for a in message.attachments
        )

        causal_flag = self.db.evaluate_causal_events(user_text, now_dt)
        snap = self.db.compute_snapshot(now_dt)

        if user_text and not is_voice:
            self.rag.append_turn("Nick", user_text)

        if snap["in_restroom"] and not _is_question(user_text):
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
        allow_cat = any(k in user_text for k in CAT_KEYWORDS)
        evening_rest = _is_sydney_evening_rest(now_dt)

        matched_card, recent_turns = await self.rag.retrieve(
            session=self.http_session,
            user_text=user_text,
            is_evening_rest=evening_rest,
            allow_work=allow_work,
            top_k=2,
        )
        if matched_card and matched_card.get("category") == "pet_daily" and not allow_cat:
            matched_card = None

        if matched_card:
            print(
                f"[RAG Hit] id={matched_card.get('id')} score={matched_card.get('score')}",
                flush=True,
            )

        sys_prompt = build_system_prompt(
            snap,
            matched_card=matched_card,
            recent_turns=recent_turns,
            is_proactive=False,
            allow_work=allow_work,
            allow_cat=allow_cat,
            is_evening_rest=evening_rest,
        )

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

        raw = await self._call_llm_once(sys_prompt, user_prompt, max_tokens=240)
        lines = clean_and_split_lines(
            raw or "",
            snap["max_msgs"],
            snap["max_chars"],
            user_text=user_text,
            allow_cat=allow_cat,
        )

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
    rag_engine = ReonaMemoryRAG(api_key=GEMINI_API_KEY)
    bot = ReonaBot(database, rag=rag_engine)
    bot.run(DISCORD_TOKEN)
