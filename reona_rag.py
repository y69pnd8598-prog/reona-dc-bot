import hashlib
import json
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

DEFAULT_MEMORY_PATH = os.getenv("REONA_MEMORY_PATH", "reona_memory.json")
DEFAULT_CACHE_PATH = os.getenv("REONA_MEMORY_CACHE_PATH", "reona_memory_cache.json")
EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001").strip()
EMBEDDING_DIM = int(os.getenv("GEMINI_EMBEDDING_DIM", "768"))
SIMILARITY_THRESHOLD = float(os.getenv("RAG_SIMILARITY_THRESHOLD", "0.58"))

SHORT_OR_FOLLOWUP_PATTERNS = {
    "?", "？", "嗯", "嗯嗯", "喔", "哦", "好", "好喔", "哈", "哈哈", "哈哈哈",
    "什麼", "什麼事", "什麼事情", "為什麼", "怎麼了", "哪個", "哪間", "是的", "對啊", "無聊",
}

DEFAULT_KNOWLEDGE_CARDS: List[Dict[str, Any]] = [
    {
        "id": "pet_mochi",
        "category": "pet_daily",
        "evening_safe": True,
        "requires_work_intent": False,
        "trigger_hint": "貓 麻糬 寵物 掉毛 梳毛 罐頭 窗邊",
        "content": "養了一隻叫麻糬的貓，提到牠一律用『牠』，不能用『他』。麻糬常趴在窗邊發呆，日常會幫牠梳毛、清貓砂。"
    },
    {
        "id": "diet_gi",
        "category": "diet_health",
        "evening_safe": True,
        "requires_work_intent": False,
        "trigger_hint": "吃 喝 胃痛 肚子 甜食 咖啡 麵包 食物 宵夜 反酸",
        "content": "腸胃偏弱，吃太甜或配冰飲容易反酸胃悶，平時偏好清淡、有麥香或溫熱的食物。法式長棍是偏硬有嚼勁的麵包，不是甜食。"
    },
    {
        "id": "background_family",
        "category": "background",
        "evening_safe": True,
        "requires_work_intent": False,
        "trigger_hint": "台北 家人 爸媽 小時候 以前 學校 畫具 排擠",
        "content": "在台北溫情家庭長大，與家人感情好但習慣報喜不報憂；小時候曾因家境被排擠、畫具被弄壞也不敢聲張，腸胃弱常跑廁所還被老師酸過，因此對外人防備、唯獨在信任的 Nick 面前卸下心防。"
    },
    {
        "id": "work_illustration",
        "category": "work",
        "evening_safe": True,
        "requires_work_intent": True,
        "trigger_hint": "工作 畫畫 插畫 案子 交稿 客戶 靈感 顏料",
        "content": "在雪梨當獨立接案插畫師，平時在家畫圖接案。只有對方主動問到工作或畫畫時才聊起，不主動推銷工作進度。"
    },
    {
        "id": "sydney_routine",
        "category": "lifestyle_sydney",
        "evening_safe": True,
        "requires_work_intent": False,
        "trigger_hint": "雪梨 天氣 出門 睡覺 熬夜 買東西 超市 晚安",
        "content": "住在雪梨，作息按雪梨時間走。晚上 20:30 後鎖定待在室內休息，絕不會半夜出門買顏料、逛超市或喝濃咖啡。"
    }
]


def _gemini_embed_endpoint(model: str) -> str:
    host = "generativelanguage" + ".googleapis.com"
    return f"https://{host}/v1beta/models/{model}:embedContent"


def _card_hash(card: Dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "id": card.get("id", ""),
            "category": card.get("category", ""),
            "trigger_hint": card.get("trigger_hint", ""),
            "content": card.get("content", ""),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for a, b in zip(vec_a, vec_b):
        dot += a * b
        norm_a += a * a
        norm_b += b * b
    if norm_a <= 1e-12 or norm_b <= 1e-12:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


class ReonaMemoryRAG:
    """直接引入現有 reona_memory.json：整合短期狀態/對話 + 長期記憶卡片增量 Embedding 檢索。"""

    def __init__(
        self,
        memory_path: str = DEFAULT_MEMORY_PATH,
        cache_path: str = DEFAULT_CACHE_PATH,
        api_key: Optional[str] = None,
    ) -> None:
        self.memory_path = memory_path
        self.cache_path = cache_path
        self.api_key = (api_key or os.getenv("GEMINI_API_KEY") or "").strip()
        self.memory_doc: Dict[str, Any] = {}
        self.cards: List[Dict[str, Any]] = []
        self.cache_map: Dict[str, Dict[str, Any]] = {}
        self.cache_dirty: bool = False
        self.memory_dirty: bool = False

    def load_memory_and_cache(self) -> None:
        if os.path.exists(self.memory_path):
            try:
                with open(self.memory_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                    if isinstance(raw, dict):
                        self.memory_doc = raw
            except Exception as e:
                print(f"[Memory Load Warning] {type(e).__name__}: {e}", flush=True)
                self.memory_doc = {}

        cards = self.memory_doc.get("knowledge_cards")
        if not isinstance(cards, list) or not cards:
            self.memory_doc["knowledge_cards"] = list(DEFAULT_KNOWLEDGE_CARDS)
            self.cards = self.memory_doc["knowledge_cards"]
            self.memory_dirty = True
        else:
            self.cards = cards

        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    raw_cache = json.load(f)
                    if isinstance(raw_cache, dict):
                        self.cache_map = raw_cache
            except Exception as e:
                print(f"[RAG Cache Load Warning] {type(e).__name__}: {e}", flush=True)
                self.cache_map = {}

    def save_if_dirty(self) -> None:
        if self.memory_dirty:
            tmp_mem = f"{self.memory_path}.tmp"
            with open(tmp_mem, "w", encoding="utf-8") as f:
                json.dump(self.memory_doc, f, ensure_ascii=False, indent=2)
            os.replace(tmp_mem, self.memory_path)
            self.memory_dirty = False

        if self.cache_dirty:
            tmp_cache = f"{self.cache_path}.tmp"
            with open(tmp_cache, "w", encoding="utf-8") as f:
                json.dump(self.cache_map, f, ensure_ascii=False, indent=2)
            os.replace(tmp_cache, self.cache_path)
            self.cache_dirty = False

    def get_recent_turns(self, limit: int = 6) -> List[str]:
        turns = self.memory_doc.get("recent_turns")
        if not isinstance(turns, list):
            return []
        return [str(t).strip() for t in turns[-limit:] if str(t).strip()]

    def get_live_state_summary(self) -> Dict[str, Any]:
        return {
            "gi_level": self.memory_doc.get("gi_level", "normal"),
            "gi_until": float(self.memory_doc.get("gi_until") or 0),
            "gi_cause": str(self.memory_doc.get("gi_cause") or "").strip(),
            "restroom_daily": int(self.memory_doc.get("restroom_daily") or 0),
            "voice_chars": int(self.memory_doc.get("voice_chars") or 0),
        }

    def append_turn(self, speaker: str, text: str, max_keep: int = 12) -> None:
        clean_txt = re.sub(r"\s+", " ", (text or "").strip())
        if not clean_txt:
            return
        turns = self.memory_doc.setdefault("recent_turns", [])
        if not isinstance(turns, list):
            turns = []
            self.memory_doc["recent_turns"] = turns
        turns.append(f"{speaker}:{clean_txt}")
        if len(turns) > max_keep:
            self.memory_doc["recent_turns"] = turns[-max_keep:]
        self.memory_doc["turn_counter"] = int(self.memory_doc.get("turn_counter") or 0) + 1
        self.memory_dirty = True

    async def _embed_text(self, session: aiohttp.ClientSession, text: str, task_type: str) -> Optional[List[float]]:
        if not self.api_key or not text.strip():
            return None
        url = _gemini_embed_endpoint(EMBEDDING_MODEL)
        body = {
            "model": f"models/{EMBEDDING_MODEL}",
            "content": {"parts": [{"text": text.strip()}]},
            "taskType": task_type,
            "outputDimensionality": EMBEDDING_DIM,
        }
        try:
            async with session.post(url, params={"key": self.api_key}, json=body) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    values = (data.get("embedding") or {}).get("values")
                    if isinstance(values, list) and values:
                        return [float(x) for x in values]
                else:
                    err_txt = await resp.text()
                    print(f"[RAG Embed HTTP {resp.status}] {err_txt[:180]}", flush=True)
        except Exception as e:
            print(f"[RAG Embed Exception] {type(e).__name__}: {repr(e)}", flush=True)
        return None

    async def sync_embeddings(self, session: aiohttp.ClientSession) -> None:
        """啟動時讀取 reona_memory.json 並執行增量更新。"""
        self.load_memory_and_cache()
        active_ids = set()

        for card in self.cards:
            cid = str(card.get("id") or "").strip()
            if not cid:
                continue
            active_ids.add(cid)
            digest = _card_hash(card)
            cached = self.cache_map.get(cid)
            if cached and cached.get("hash") == digest and isinstance(cached.get("embedding"), list):
                continue

            doc_text = (
                f"類別：{card.get('category', '')}\n"
                f"關鍵詞：{card.get('trigger_hint', '')}\n"
                f"內容：{card.get('content', '')}"
            )
            vec = await self._embed_text(session, doc_text, task_type="RETRIEVAL_DOCUMENT")
            if vec:
                self.cache_map[cid] = {
                    "id": cid,
                    "hash": digest,
                    "embedding": vec,
                }
                self.cache_dirty = True
                print(f"[RAG Incremental Updated] id={cid}", flush=True)

        for rid in [k for k in self.cache_map.keys() if k not in active_ids]:
            del self.cache_map[rid]
            self.cache_dirty = True

        self.save_if_dirty()

    def _filter_candidates(self, is_evening_rest: bool, allow_work: bool) -> List[Dict[str, Any]]:
        filtered: List[Dict[str, Any]] = []
        for card in self.cards:
            cid = str(card.get("id") or "").strip()
            cached = self.cache_map.get(cid)
            if not cached or not isinstance(cached.get("embedding"), list):
                continue
            if card.get("requires_work_intent") and not allow_work:
                continue
            if is_evening_rest and card.get("evening_safe") is False:
                continue
            filtered.append({**card, "_embedding": cached["embedding"]})
        return filtered

    async def retrieve(
        self,
        session: Optional[aiohttp.ClientSession],
        user_text: str,
        is_evening_rest: bool = False,
        allow_work: bool = False,
        top_k: int = 2,
        threshold: float = SIMILARITY_THRESHOLD,
    ) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        """回傳 (過門檻的最相關記憶卡片或 None, 最近對話 turns)。"""
        recent = self.get_recent_turns(limit=6)
        cleaned_q = re.sub(r"[？\?！!。，,\s]+", "", (user_text or "").strip())
        if not cleaned_q or len(cleaned_q) <= 2 or cleaned_q in SHORT_OR_FOLLOWUP_PATTERNS:
            return None, recent

        if not session:
            return None, recent

        candidates = self._filter_candidates(is_evening_rest=is_evening_rest, allow_work=allow_work)
        if not candidates:
            return None, recent

        context_tail = "\n".join(recent[-3:])
        query_input = f"{context_tail}\n目前提問：{user_text}".strip() if context_tail else user_text
        query_vec = await self._embed_text(session, query_input, task_type="RETRIEVAL_QUERY")
        if not query_vec:
            return None, recent

        scored: List[Dict[str, Any]] = []
        for cand in candidates:
            score = _cosine_similarity(query_vec, cand["_embedding"])
            scored.append({
                "id": cand.get("id"),
                "category": cand.get("category"),
                "content": cand.get("content"),
                "score": round(score, 4),
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        top_matches = scored[:top_k]
        best = top_matches[0] if top_matches else None
        if not best or best["score"] < threshold:
            return None, recent
        return best, recent
