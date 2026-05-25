"""Vector store for filings / earnings transcripts (RAG context).

The store is OPTIONAL — the funnel runs fine without it. When enabled,
it lets the LLM gatekeeper pull historically relevant context for the
asset under analysis (prior 10-K language, recent earnings calls)
before forming a verdict.

Implementations:
  * `InMemoryVectorStore` — dot-product over numpy arrays, no extra
    deps; default for tests and small universes.
  * `QdrantVectorStore`   — lazy import, used when the Qdrant URL is set.
  * `ChromaVectorStore`   — lazy import, used when provider="chroma".

Embeddings: a hashing trick is used as a fallback so the store is
fully functional without any embedding model. Plug a real sentence
transformer in by passing your own `embedder` callable.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

Vector = list[float]
Embedder = Callable[[str], Vector]


def hashing_embedder(dim: int = 384) -> Embedder:
    """Simple deterministic hashing-trick embedder.

    For each lowercase token, blake2b → bucket index, then L2 normalize.
    Not as good as a real model but it gives a stable similarity
    function so the rest of the plumbing can be tested.
    """

    def _embed(text: str) -> Vector:
        v = [0.0] * dim
        if not text:
            return v
        for tok in text.lower().split():
            h = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(h, "big") % dim
            v[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in v))
        if norm > 0:
            v = [x / norm for x in v]
        return v

    return _embed


@dataclass
class VectorRecord:
    id: str
    text: str
    vector: Vector
    metadata: dict[str, str] = field(default_factory=dict)


class VectorStore(Protocol):
    async def upsert(
        self, id: str, text: str, metadata: dict[str, str] | None = None
    ) -> None: ...

    async def query(self, text: str, k: int = 4) -> list[tuple[str, str, float]]: ...

    async def close(self) -> None: ...


# ---- in-memory ----------------------------------------------------------


class InMemoryVectorStore:
    def __init__(self, embedder: Embedder | None = None, dim: int = 384):
        self._embedder = embedder or hashing_embedder(dim)
        self._records: dict[str, VectorRecord] = {}

    async def upsert(
        self, id: str, text: str, metadata: dict[str, str] | None = None
    ) -> None:
        self._records[id] = VectorRecord(
            id=id,
            text=text,
            vector=self._embedder(text),
            metadata=metadata or {},
        )

    async def query(self, text: str, k: int = 4) -> list[tuple[str, str, float]]:
        if not self._records:
            return []
        q = self._embedder(text)
        scored = [
            (rec.id, rec.text, _dot(q, rec.vector))
            for rec in self._records.values()
        ]
        scored.sort(key=lambda x: x[2], reverse=True)
        return scored[:k]

    async def close(self) -> None:
        self._records.clear()


def _dot(a: Vector, b: Vector) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


# ---- Qdrant -------------------------------------------------------------


class QdrantVectorStore:
    """Qdrant-backed store. Lazy import; raises a clear error if
    qdrant-client isn't installed."""

    def __init__(
        self,
        url: str,
        api_key: str | None = None,
        collection: str = "filings",
        embedder: Embedder | None = None,
        dim: int = 384,
    ):
        try:
            from qdrant_client import AsyncQdrantClient  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError(
                "QdrantVectorStore requires the 'vector' extra. "
                "Install with: pip install -e '.[vector]'"
            ) from e
        self._client = AsyncQdrantClient(url=url, api_key=api_key)
        self.collection = collection
        self.dim = dim
        self._embedder = embedder or hashing_embedder(dim)
        self._ready = False

    async def _ensure_collection(self) -> None:
        if self._ready:
            return
        from qdrant_client.http import models  # type: ignore[import-not-found]

        existing = await self._client.get_collections()
        names = {c.name for c in existing.collections}
        if self.collection not in names:
            await self._client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=self.dim, distance=models.Distance.COSINE
                ),
            )
        self._ready = True

    async def upsert(
        self, id: str, text: str, metadata: dict[str, str] | None = None
    ) -> None:
        from qdrant_client.http import models  # type: ignore[import-not-found]

        await self._ensure_collection()
        payload = {"text": text, **(metadata or {})}
        await self._client.upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=id, vector=self._embedder(text), payload=payload
                )
            ],
        )

    async def query(self, text: str, k: int = 4) -> list[tuple[str, str, float]]:
        await self._ensure_collection()
        hits = await self._client.search(
            collection_name=self.collection,
            query_vector=self._embedder(text),
            limit=k,
        )
        out = []
        for h in hits:
            payload = h.payload or {}
            out.append((str(h.id), payload.get("text", ""), float(h.score)))
        return out

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass


# ---- Chroma -------------------------------------------------------------


class ChromaVectorStore:
    """Chroma-backed store. Lazy import."""

    def __init__(
        self,
        url: str | None = None,
        collection: str = "filings",
        embedder: Embedder | None = None,
        dim: int = 384,
    ):
        try:
            import chromadb  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError(
                "ChromaVectorStore requires the 'vector' extra."
            ) from e
        self._client = (
            chromadb.HttpClient(host=url) if url else chromadb.EphemeralClient()
        )
        self._collection = self._client.get_or_create_collection(collection)
        self._embedder = embedder or hashing_embedder(dim)

    async def upsert(
        self, id: str, text: str, metadata: dict[str, str] | None = None
    ) -> None:
        self._collection.upsert(
            ids=[id],
            documents=[text],
            embeddings=[self._embedder(text)],
            metadatas=[metadata or {}],
        )

    async def query(self, text: str, k: int = 4) -> list[tuple[str, str, float]]:
        res = self._collection.query(
            query_embeddings=[self._embedder(text)], n_results=k
        )
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out: list[tuple[str, str, float]] = []
        for i, doc, dist in zip(ids, docs, dists, strict=False):
            # Chroma returns distance; convert to similarity for caller.
            out.append((str(i), str(doc), 1.0 - float(dist)))
        return out

    async def close(self) -> None:  # nothing to do for Chroma
        return None


def build_vector_store(
    provider: str,
    *,
    url: str | None = None,
    api_key: str | None = None,
    collection: str = "filings",
    dim: int = 384,
    embedder: Embedder | None = None,
) -> VectorStore:
    provider = (provider or "memory").lower()
    if provider == "memory":
        return InMemoryVectorStore(embedder=embedder, dim=dim)
    if provider == "qdrant":
        if not url:
            raise ValueError("Qdrant requires a URL")
        return QdrantVectorStore(
            url=url,
            api_key=api_key,
            collection=collection,
            embedder=embedder,
            dim=dim,
        )
    if provider == "chroma":
        return ChromaVectorStore(
            url=url, collection=collection, embedder=embedder, dim=dim
        )
    raise ValueError(f"unknown vector store provider: {provider}")
