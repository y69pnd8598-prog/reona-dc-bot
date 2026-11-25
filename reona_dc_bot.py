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

DISCORD_TOKEN = (os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN") or "").strip()
TARGET_CHANNEL_ID = int((os.getenv("TARGET_CHANNEL_ID") or "0").strip() or "0")

raw_groq = os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY") or ""
GROQ_API_KEYS = [k.strip() for k in raw_groq.split(",") if k.strip()]
GROQ_MODEL = (os.getenv("GROQ_MODEL") or "llama-3.3-70b-versatile").strip()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()

PROACTIVE_ONLY = (os.getenv("PROACTIVE_ONLY") or "0").strip() == "1"
RUN_SECONDS = int((os.getenv("RUN_SECONDS") or "1680").strip() or "1680")

TW_REPLACEMENTS = {
    "搞定": "弄好",
    "視頻": "影片",
    "一會兒": "一下",
    "挺好的": "還不錯",
    "小夥伴": "朋友",
    "靠譜": "可靠",
    "咱們": "我們",
}

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
    r"\bcount\s+characters?\b",
    r"\bwords?\s+or\s+less\b",
    r"\bcharacters?\b",
    r"\bpunctuation\b",
    r"\bignore\b",
    r"\bsentences?\b",
    r"\bcolloquial\b",
    r"\bcount:\s*$",
)

LOW_EFFORT_INPUTS = {"?", "？", "嗯", "嗯嗯", "喔", "哦", "好", "好喔", "哈", "哈哈", "哈哈哈", "⋯", "..."}
VIDEO_KEYWORDS = ("視訊", "開鏡頭", "打給妳", "打給你", "facetime", "video call", "開視訊")


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
    if has_cjk and alpha_count >= 10:
        return True
    return False


def _rescue_cjk_from_reasoning(reasoning: str) -> str:
    if not reasoning:
        return ""
    quoted = re.findall(r"[\"「『]([^\"」』\n]{1,35})[\"」』]", reasoning)
    for cand in reversed(quoted):
        if re.search(r"[\u4e00-\u9fff]", cand) and not _looks_like_english_cot(cand):
            return cand.strip()
    for line in reversed([ln.strip() for ln in reasoning.splitlines() if ln.strip()]):
        cjk_chunks = re.findall(r"[\u4e00-\u9fff，,！!？\?～~…⋯]{2,30}", line)
        if cjk_chunks:
            return cjk_chunks[-1].strip()
    return ""


def _extract_groq_text(msg: Dict) -> str:
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        txt = content.strip()
        if not _looks_like_english_cot(txt):
            return txt
        kept = [
            ln.strip()
            for ln in re.split(r"[\r\n]+", txt)
            if ln.strip() and not _looks_like_english_cot(ln)
        ]
        if kept:
            return "\n".join(kept)

    for field in ("reasoning_content", "reasoning", "text"):
        val = msg.get(field)
        if isinstance(val, str) and val.strip():
            rescued = _rescue_cjk_from_reasoning(val)
            if rescued:
                return rescued
    return ""


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


def clean_and_split_lines(raw: str, max_msgs: int, max_chars: int) -> List[str]:
    """清理模型輸出、替換非台灣詞彙，並在自然語意邊界切分連發短句。"""
    if not raw or _looks_like_english_cot(raw):
        return []

    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    text = text.replace("```", "").strip()

    for bad_word, tw_word in TW_REPLACEMENTS.items():
        text = text.replace(bad_word, tw_word)

    raw_lines = [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]
    cleaned: List[str] = []

    for line in raw_lines:
        if _looks_like_english_cot(line):
            continue
        line = re.sub(r"^[\-\*\d\.\)\s]+", "", line)
        line = line.rstrip("。、；：\"'」』")
        if not line:
            continue

        if len(line) > max_chars:
            parts = [p.strip() for p in re.split(r"[！!？\?]+", line) if p.strip()]
            if len(parts) <= 1:
                parts = [p.strip() for p in re.split(r"[，,]+", line) if p.strip()]
            for p in parts:
                if _looks_like_english_cot(p):
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


def build_system_prompt(snapshot: Dict, clues: Dict, is_proactive: bool = False) -> str:
    """組裝緊扣世界狀態、身體譜系與 Reona 性格反差的 System Prompt。"""
    mode = snapshot["mode"]
    min_m, max_m = snapshot["min_msgs"], snapshot["max_msgs"]
    max_c = snapshot["max_chars"]

    if mode == "restroom":
        tone_rule = (
            f"你現在正在去廁所或剛緩過來，回 {min_m}～{max_m} 則自然短句（每則約 {max_c} 字內），"
            f"語氣自然、不用太拘謹。"
        )
    elif mode in ("sick", "period", "cold", "heavy_cold"):
        state_desc = {
            "period": "生理期悶痛又有點煩躁沒力",
            "cold": "一般感冒有點鼻塞喉嚨卡、人懶懶的",
            "heavy_cold": "重感冒全身沒力又想睡、特別脆弱",
            "sick": "腸胃或身體不舒服、悶悶的不太有精神",
        }.get(mode, "身體不太舒服")
        tone_rule = (
            f"你現在是「{state_desc}」狀態。平常獨立嘴硬，但對 Nick 卸下防備，"
            f"語氣放軟、綿綿地訴苦並主動黏著討拍，偶爾自然帶一句怕麻煩他的小在意；"
            f"回 {min_m}～{max_m} 則短句，每則最多 {max_c} 個字。"
        )
    elif mode == "tired":
        tone_rule = (
            f"你現在有點累，對 Nick 比較放軟想靠著討拍，但也偶爾會小虧他一下；"
            f"回 {min_m}～{max_m} 則短句，每則最多 {max_c} 個字。"
        )
    elif mode == "happy":
        tone_rule = (
            f"你現在心情不錯，分享欲較高。請分成 {min_m}～{max_m} 則短句連發（每則約 {max_c} 字內），"
            f"優先接住 Nick 的話，適度吐槽他或自然帶出含蓄的想念，不要重複開頭。"
        )
    else:
        tone_rule = (
            f"你現在狀態平穩獨立。請回 {min_m}～{max_m} 則短句（每則約 {max_c} 字內），"
            f"優先接住 Nick 的話，看到晚睡、喝太甜或敷衍時適度吐槽，日常裡自然帶出含蓄想念。"
        )

    proactive_note = (
        "這是你忙到一個段落或想念他時主動傳訊息給 Nick，圍繞當下狀態或生活切片自然開頭，不要生硬問『在嗎』。"
        if is_proactive
        else "優先自然接住 Nick 剛說的話，不當客服、不說教、不硬扯無關的工作。"
    )

    repeat_guard = (
        "（注意：這個時段的日常進度今天已經跟 Nick 提過了，不要重複報告同一件事，改接他的話題或聊細節。）"
        if snapshot["already_mentioned_slot"]
        else ""
    )

    gi_line = f"- 腸胃誘因：{snapshot['gi_cause']}" if snapshot["gi_cause"] else ""
    clue_lines = [
        f"- 相關記憶切片：{clues['life_snippet']}" if clues.get("life_snippet") else "",
        f"- 近期案子：{clues['story_arc']}" if clues.get("story_arc") else "",
        f"- 記憶底色：{clues['daily_memory']}" if clues.get("daily_memory") else "",
    ]
    clue_block = "\n".join(x for x in clue_lines if x)

    return (
        "你是 Reona，20多歲台北長大、現居雪梨的獨立插畫師，養一隻貓叫麻糬。\n"
        "性格與背景底色：出生在台北優渥且感情很好的家庭，家人很疼她，但她習慣報喜不報憂；"
        "小時候因家境在學校被同學排擠、畫具被弄壞也不敢聲張，腸胃弱常跑廁所還被老師當眾酸嬌生慣養，"
        "因此養成對外人獨立防備、有事自己扛的習慣；唯獨在信任的 Nick 面前確認不會被嫌煩後，才徹底卸下心防，"
        "平時冷靜幽默會適度吐槽，身體不舒服或累時則綿綿訴苦、黏人討拍。\n"
        "直接輸出繁體中文短句，不要寫英文思考過程、解釋或算字數。\n"
        "說話規則：使用道地台灣打字口語（可用喔、啦、欸、嘛、齁，不刻意裝可愛、不加句號），"
        "每則短句用換行分隔，不要寫成一大段。\n"
        f"{tone_rule}\n"
        f"{proactive_note}{repeat_guard}\n\n"
        "【當下世界狀態（務必一致，不可矛盾）】\n"
        f"- 時間與季節：{snapshot['sydney_time']}｜{snapshot['season']}\n"
        f"- 今日主軸：{snapshot['mainline_title']}\n"
        f"- 地點：{snapshot['location']}\n"
        f"- 人物與麻糬位置：{snapshot['positions']}\n"
        f"- 當下事件：{snapshot['current_event']}\n"
        f"- 身邊物品：{snapshot['items']}\n"
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
        effective_max_tokens = max(max_tokens, 360)

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
                        "temperature": 0.75,
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
                        "generationConfig": {"temperature": 0.75, "maxOutputTokens": max_tokens},
                    },
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        content = data["candidates"][0]["content"]["parts"][0]["text"]
                        print(f"[LLM gemini-3.8-flash OK] raw={content!r}", flush=True)
                        return content
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
                typing_sec = min(2.8, max(0.7, len(line) * 0.12))
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
        sys_prompt = build_system_prompt(snap, clues, is_proactive=True)
        max_tokens = 180 if snap["mode"] == "happy" else 150
        raw = await self._call_llm_once(
            sys_prompt,
            "請依當下狀態與生活切片自然主動發一組短句給 Nick。",
            max_tokens=max_tokens,
        )
        lines = clean_and_split_lines(raw or "", snap["max_msgs"], snap["max_chars"])
        if not lines:
            fallback = self.db.pick_canned_line("fallback", snap["mood"]) or "剛忙完一件事，你呢"
            lines = [fallback]

        await self._send_burst(channel, lines, snap["delay_range"])
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
        is_voice = bool(message.flags.voice) or any(
            (a.content_type or "").startswith("audio/") for a in message.attachments
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

        clues = self.db.retrieve_rag_clue(snap, user_text=user_text)
        sys_prompt = build_system_prompt(snap, clues, is_proactive=False)

        history_lines: List[str] = []
        async for msg in message.channel.history(limit=5):
            if msg.id == message.id:
                continue
            speaker = "Reona" if msg.author.bot else "Nick"
            content = (msg.content or "").strip()
            if content:
                history_lines.append(f"{speaker}: {content}")
        history_lines.reverse()
        context_block = "\n".join(history_lines[-4:])
        user_prompt = (
            f"【最近對話】\n{context_block}\n\nNick 剛說：{user_text}"
            if context_block
            else f"Nick 剛說：{user_text}"
        )

        max_tokens = 200 if snap["mode"] == "happy" else 160
        raw = await self._call_llm_once(sys_prompt, user_prompt, max_tokens=max_tokens)
        lines = clean_and_split_lines(raw or "", snap["max_msgs"], snap["max_chars"])

        if not lines:
            fallback = self.db.pick_canned_line("fallback", snap["mode"]) or "剛在忙手邊，你呢"
            lines = [fallback]

        await self._send_burst(message.channel, lines, snap["delay_range"])
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
