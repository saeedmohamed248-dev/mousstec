"""
Local vector store backed by a single pickle file on disk.

Why not ChromaDB / FAISS: the knowledge base here is small (FAQs, pricing,
release notes — O(100s) of rows). A 1 MB pickle is faster to load on worker
start than spinning up an embedded vector DB, and avoids a heavy native
dependency on the production image. The interface below mirrors the
"upsert / query" surface of those libraries, so swapping in ChromaDB later
is a one-file change.

Embeddings come from Gemini's `text-embedding-004` model via the REST API,
following the same call style as `erp_core.ai.advisor_agent`.

Implementation note: cosine similarity is computed in pure Python (no numpy).
At hundreds of docs × 768 dims, a query is sub-millisecond — well below the
network latency we already pay for the embedding call itself.
"""
from __future__ import annotations

import logging
import math
import os
import pickle
import threading
from dataclasses import dataclass, field
from typing import Iterable

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_EMBED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "text-embedding-004:embedContent"
)


def _store_path() -> str:
    base = getattr(settings, "MESSENGER_VECTOR_STORE_PATH", None)
    if base:
        return str(base)
    return os.path.join(str(settings.BASE_DIR), "media", "messenger_bot", "kb.pkl")


@dataclass
class _Entry:
    doc_id: str
    text: str
    metadata: dict
    # Pre-normalised so the query reduces to a plain dot product.
    vector: list  # list[float]


@dataclass
class _Store:
    entries: dict = field(default_factory=dict)  # doc_id -> _Entry


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 0:
        return vec
    return [x / norm for x in vec]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class VectorStore:
    """Thread-safe singleton — one instance per worker process."""

    _instance: "VectorStore | None" = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self):
        self._lock = threading.RLock()
        self._path = _store_path()
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._store = self._load()

    # ----- persistence -----
    def _load(self) -> _Store:
        if not os.path.exists(self._path):
            return _Store()
        try:
            with open(self._path, "rb") as f:
                obj = pickle.load(f)
            if isinstance(obj, _Store):
                return obj
        except Exception as exc:  # corrupt file — start fresh, don't crash boot
            logger.warning("messenger_bot: vector store unreadable (%s); resetting", exc)
        return _Store()

    def _flush_locked(self):
        tmp = self._path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(self._store, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, self._path)

    # ----- embeddings -----
    @staticmethod
    def _embed(text: str) -> list[float]:
        api_key = getattr(settings, "GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        resp = requests.post(
            _EMBED_URL,
            params={"key": api_key},
            json={
                "model": "models/text-embedding-004",
                "content": {"parts": [{"text": text[:8000]}]},
            },
            timeout=15,
        )
        resp.raise_for_status()
        values = resp.json()["embedding"]["values"]
        return _normalise([float(x) for x in values])

    # ----- public API -----
    def upsert(self, doc_id: str, text: str, metadata: dict | None = None) -> None:
        if not text or not text.strip():
            self.delete(doc_id)
            return
        vec = self._embed(text)
        with self._lock:
            self._store.entries[doc_id] = _Entry(
                doc_id=doc_id,
                text=text,
                metadata=metadata or {},
                vector=vec,
            )
            self._flush_locked()

    def delete(self, doc_id: str) -> None:
        with self._lock:
            if doc_id in self._store.entries:
                del self._store.entries[doc_id]
                self._flush_locked()

    def query(self, text: str, top_k: int = 4) -> list[tuple[float, _Entry]]:
        with self._lock:
            if not self._store.entries:
                return []
            entries = list(self._store.entries.values())
        q = self._embed(text)
        scored = [(_dot(q, e.vector), e) for e in entries]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[:top_k]

    def context_for(self, text: str, top_k: int = 4, min_score: float = 0.55) -> str:
        hits = self.query(text, top_k=top_k)
        passages: Iterable[str] = (
            f"[{entry.metadata.get('kind', 'doc')}] {entry.text}"
            for score, entry in hits
            if score >= min_score
        )
        return "\n\n---\n\n".join(passages).strip()


def get_store() -> VectorStore:
    return VectorStore()
