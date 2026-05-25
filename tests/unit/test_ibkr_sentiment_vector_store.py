"""Tests for the in-memory vector store + hashing embedder."""

from __future__ import annotations

import pytest

from src.ibkr_sentiment.sentiment.vector_store import (
    InMemoryVectorStore,
    hashing_embedder,
)


def test_hashing_embedder_is_deterministic_and_normalized():
    embed = hashing_embedder(dim=128)
    a = embed("apple earnings beat")
    b = embed("apple earnings beat")
    assert a == b
    norm = sum(x * x for x in a) ** 0.5
    assert abs(norm - 1.0) < 1e-9


def test_hashing_embedder_handles_empty_text():
    embed = hashing_embedder(dim=64)
    assert embed("") == [0.0] * 64


@pytest.mark.asyncio
async def test_in_memory_vector_store_returns_most_similar():
    store = InMemoryVectorStore(dim=128)
    await store.upsert("aapl", "apple iphone supply chain china")
    await store.upsert("msft", "microsoft cloud azure earnings")
    await store.upsert("xom",  "oil gas opec production cut")
    results = await store.query("apple iphone earnings", k=2)
    ids = [r[0] for r in results]
    # AAPL doc should be the top match.
    assert ids[0] == "aapl"


@pytest.mark.asyncio
async def test_in_memory_vector_store_empty_query_returns_nothing():
    store = InMemoryVectorStore()
    assert await store.query("anything") == []
