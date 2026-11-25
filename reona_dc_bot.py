import asyncio
import datetime
import os
import random
import re
from typing import Dict, List, Optional

import aiohttp
import discord

from reona_bg_db import SYDNEY_TZ, ReonaBGDB

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0"))
GROQ_API_KEYS = [k.strip() for k in os.getenv("GROQ_API_KEYS", "").split(",") if k.strip()]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PROACTIVE_ONLY = os.getenv("PROACTIVE_ONLY", "0") == "1"
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "1680"))

# 攔截非台灣慣用語（一旦出現即判定污染並改走台灣短句保底）
NON_TW_WORDS = ("搞定", "視頻", "一會兒", "挺好的", "小夥伴", "靠譜", "啥玩意", "咋了", "咱們")

LOW_EFFORT_INPUTS = {"?", "？", "嗯", "嗯嗯", "喔", "哦", "好", "好喔", "哈", "哈哈", "哈哈哈", "⋯", "..."}
VIDEO_KEYWORDS = ("視訊", "開鏡頭", "打給妳", "打給你", "facetime", "video call", "開視訊")


def clean_and_split_lines(raw: str, max_msgs: int, max_chars: int) -> List[str]:
    """清理模型輸出、過濾非台灣詞彙，並硬控管連發則數與每則字數上限。"""
    if not raw:
        return []

    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    text = text.replace("```", "").strip()

    if any(w in text for w in NON_TW_WORDS):
        return []

    raw_lines = [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]
    cleaned: List[str] = []

    for line in raw_lines:
        line = re.sub(r"^[\-\*\d\.\)\s]+", "", line)
        line = line.rstrip("。，、；：\"'」』")
        if not line:
            continue

        if len(line) > max_chars:
            parts = [p.strip() for p in re.split(r"[，,！!？\?]+", line) if p.strip()]
            for p in parts:
                if len(p) <= max_chars:
                    cleaned.append(p)
                else:
                    cleaned.append(p[:max_chars])
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
    """組裝輕量、緊扣六維世界狀態與台灣成熟獨立人設的 System Prompt。"""
    mode = snapshot["mode"]
    min_m, max_m = snapshot["min_msgs"], snapshot["max_msgs"]
    max_c = snapshot["max_chars"]

    if mode in ("restroom", "sick", "tired"):
        tone_rule = (
            f"你現在身體疲憊或腸胃不舒服，平常自己扛但對 Nick 卸下防備。"
            f"只能回 {min_m}～{max_m} 則極短句，每則嚴格 {max_c} 個字以內，語氣軟一點、簡短示弱。"
        )
    elif mode == "happy":
        tone_rule = (
            f"你現在心情不錯，分享欲比較高。請分成 {min_m}～{max_m} 則短句連發，"
            f"每則 {max_c} 個字以內，像傳 LINE 連發一樣一句接一句推進細節，不要重複開頭。"
        )
    else:
        tone_rule = (
            f"你現在狀態平穩獨立。請回 {min_m}～{max_m} 則短句，"
            f"每則 4～{max_c} 個字，俐落有生活感。"
        )

    proactive_note = (
        "這是你忙到一個段落或剛緩過來，主動傳訊息給 Nick，圍繞當下正在做的事自然開頭，不要問『在嗎』。"
        if is_proactive
        else "自然接住 Nick 的話，不當客服、不說教、不討好。"
    )

    repeat_guard = (
        "（注意：這個時段的日常進度今天已經跟 Nick 提過了，不要重複報告同一件事，改聊細節或接他的話題。）"
        if snapshot["already_mentioned_slot"]
        else ""
    )

    gi_line = f"- 腸胃誘因：{snapshot['gi_cause']}" if snapshot["gi_cause"] else ""
    clue_lines = [
        f"- 生活切片：{clues['life_snippet']}" if clues.get("life_snippet") else "",
        f"- 近期案子：{clues['story_arc']}" if clues.get("story_arc") else "",
        f"- 記憶底色：{clues['daily_memory']}" if clues.get("daily_memory") else "",
    ]
    clue_block = "\n".join(x for x in clue_lines if x)

    return (
        "你是 Reona，20多歲台北長大、現居雪梨的獨立插畫師，養一隻貓叫麻糬。\n"
        "性格底色：台北中產各自忙碌家庭長大，從小早熟獨立不愛麻煩人、心思細膩易緊張（一忙或吃冰就容易腹痛腹瀉）；"
        "平時有自己的生活步調與邊界，只有在信任的 Nick 面前才會在累或不適時展現成熟依賴。\n"
        "說話規則：使用道地台灣打字口語（可用喔、啦、欸、嘛，不刻意裝可愛、不加句號、禁用搞定/視頻/一會兒/挺好的/咱們），"
        "每則短句用換行分隔，絕不寫長篇段落。\n"
        f"{tone_rule}\n"
        f"{proactive_note}{repeat_guard}\n\n"
        "【當下六維世界狀態（務必完全一致，不可矛盾）】\n"
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

    def _touch(self, nick: bool = False, reona: bool = False, proactive: bool = False) -> None:
        if hasattr(self.db, "touch_timestamps"):
            self.db.touch_timestamps(nick=nick, reona=reona, proactive=proactive)

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    async def close(self) -> None:
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        await super().close()

    async def _call_llm_once(self, system_prompt: str, user_prompt: str, max_tokens: int) -> Optional[str]:
        """省 Token 單次呼叫：優先 Groq 多 Key 輪替，失敗才走 Gemini 備援。"""
        if not self.http_session:
            return None

        keys = list(GROQ_API_KEYS)
        random.shuffle(keys)

        for key in keys:
            try:
                async with self.http_session.post(
                    "[https://api.groq.com/openai/v1/chat/completions](https://api.groq.com/openai/v1/chat/completions)",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": "llama-3.3-70b-versatile",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.72,
                        "max_tokens": max_tokens,
                    },
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"]
            except Exception:
                continue

        if GEMINI_API_KEY:
            try:
                url = (
                    "[https://generativelanguage.googleapis.com/v1beta/models/](https://generativelanguage.googleapis.com/v1beta/models/)"
                    f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
                )
                payload = {
                    "systemInstruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                    "generationConfig": {"temperature": 0.72, "maxOutputTokens": max_tokens},
                }
                async with self.http_session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["candidates"][0]["content"]["parts"][0]["text"]
            except Exception:
                pass

        return None

    async def _send_burst(self, channel: discord.abc.Messageable, lines: List[str], delay_range: tuple) -> None:
        """模擬真人節奏逐則打字發送。"""
        for idx, line in enumerate(lines):
            if idx > 0:
                await asyncio.sleep(random.uniform(*delay_range))
            async with channel.typing():
                typing_sec = min(2.8, max(0.7, len(line) * 0.16))
                await asyncio.sleep(typing_sec)
            await channel.send(line)
        self._touch(reona=True)

    async def maybe_send_proactive(self, channel: discord.abc.Messageable) -> bool:
        """圍繞今日主軸的背景主動發訊（嚴格控管深夜勿擾與冷卻）。"""
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

        if snap["already_mentioned_slot"] and snap["gi_status"] == "normal":
            return False

        clues = self.db.retrieve_rag_clue(snap)
        sys_prompt = build_system_prompt(snap, clues, is_proactive=True)
        max_tokens = 85 if snap["mode"] == "happy" else 55
        raw = await self._call_llm_once(
            sys_prompt,
            "請依當下六維狀態自然主動發一組短句給 Nick。",
            max_tokens=max_tokens,
        )
        lines = clean_and_split_lines(raw or "", snap["max_msgs"], snap["max_chars"])
        if not lines:
            fallback = self.db.pick_canned_line("fallback", snap["mood"]) or "剛忙完一件事"
            lines = [fallback]

        await self._send_burst(channel, lines, snap["delay_range"])
        self._touch(proactive=True)
        self.db.mark_slot_mentioned(snap["time_slot"])

        if any(k in "".join(lines) for k in ("陪我", "有空", "要嗎", "要不要", "在忙嗎", "聊聊")):
            self.db.set_state(pending_request_sec=1500)

        return True

    async def _catch_up_missed_message(self, channel: discord.abc.Messageable) -> None:
        """換班空檔若有 Nick 剛傳的未回訊息（5 分鐘內），上線時自動接住不漏訊。"""
        try:
            async for msg in channel.history(limit=1):
                if msg.author.bot:
                    return
                age = (datetime.datetime.now(datetime.timezone.utc) - msg.created_at).total_seconds()
                if 0 <= age <= 300:
                    print(f"Catching up missed message from Nick ({int(age)}s ago)...", flush=True)
                    await self._handle_incoming_message(msg)
        except Exception as e:
            print(f"Catch-up check skipped: {e}", flush=True)

    async def _github_session_loop(self) -> None:
        """GitHub Actions 28 分鐘班次主迴圈：補接漏訊 → 檢查主動發訊 → 線上監聽 → 到點收工存檔。"""
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
        print(f"Reona online as {self.user}", flush=True)
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

        # === 零 Token 優先分流（廁所 / 語音 / 視訊 / 被拒絕 / 敷衍短訊息）===
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
                line = self.db.pick_canned_line("low_effort", snap["mood"]) or "？"
                await self._send_burst(message.channel, [line], snap["delay_range"])
                return

        # === 一般與深度互動：單次省 Token 呼叫 ＋ 輕量 RAG ===
        clues = self.db.retrieve_rag_clue(snap)
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

        max_tokens = 90 if snap["mode"] == "happy" else 60
        raw = await self._call_llm_once(sys_prompt, user_prompt, max_tokens=max_tokens)
        lines = clean_and_split_lines(raw or "", snap["max_msgs"], snap["max_chars"])

        if not lines:
            fallback = self.db.pick_canned_line("fallback", snap["mode"]) or "剛在忙手邊"
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
