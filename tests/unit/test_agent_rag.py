"""Unit tests for agent_rag — tokenizer, vector store, and hybrid RAG."""

import pytest
from a2a.types import AgentSkill

from app.services.agent_rag import AgentSkillRAG, _QdrantStore, _tokenize

# Use a trivial encode function for tests (avoids loading the real model).
_DIM = 4
_COUNTER = 0


def _fake_encode(text: str) -> list[float]:
    """Deterministic pseudo-embedding based on text hash."""
    h = hash(text) % 10000
    return [float((h >> i) & 1) for i in range(_DIM)]


def _make_skill(skill_id: str, name: str, desc: str = "") -> AgentSkill:
    return AgentSkill(id=skill_id, name=name, description=desc, tags=["test"])


# ---------------------------------------------------------------------------
# _tokenize
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_lowercases_and_splits(self):
        assert _tokenize("Hello World") == ["hello", "world"]

    def test_removes_stop_words(self):
        tokens = _tokenize("the quick brown fox is very fast")
        assert "the" not in tokens
        assert "is" not in tokens
        assert "very" not in tokens
        assert "quick" in tokens

    def test_strips_punctuation(self):
        tokens = _tokenize("hello, world! test-case.")
        assert all("," not in t and "!" not in t for t in tokens)

    def test_empty_string(self):
        assert _tokenize("") == []


# ---------------------------------------------------------------------------
# _QdrantStore
# ---------------------------------------------------------------------------


class TestQdrantStore:
    def test_empty_search(self):
        store = _QdrantStore("test")
        ids, scores, metas = store.search_with_scores([0.0] * _DIM, 5)
        assert ids == []
        assert scores == []
        assert metas == []

    def test_empty_get_all(self):
        store = _QdrantStore("test")
        data = store.get_all()
        assert data["ids"] == []
        assert data["documents"] == []

    def test_upsert_and_search(self):
        store = _QdrantStore("test")
        vecs = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
        store.upsert(
            ["a", "b"],
            ["doc a", "doc b"],
            vecs,
            metadatas=[{"key": "va"}, {"key": "vb"}],
        )

        ids, scores, metas = store.search_with_scores([1.0, 0.0, 0.0, 0.0], k=1)
        assert len(ids) == 1
        assert ids[0] == "a"
        assert metas[0]["key"] == "va"

    def test_upsert_and_get_all(self):
        store = _QdrantStore("test")
        vecs = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
        store.upsert(["x", "y"], ["doc x", "doc y"], vecs)

        data = store.get_all()
        assert set(data["ids"]) == {"x", "y"}
        assert len(data["documents"]) == 2

    def test_upsert_empty_is_noop(self):
        store = _QdrantStore("test")
        store.upsert([], [], [])
        assert store.get_all()["ids"] == []


# ---------------------------------------------------------------------------
# AgentSkillRAG
# ---------------------------------------------------------------------------


class TestAgentSkillRAG:
    def test_match_returns_none_when_empty(self):
        rag = AgentSkillRAG(encode_fn=_fake_encode)
        assert rag.match("anything") is None

    def test_populate_and_match(self):
        rag = AgentSkillRAG(encode_fn=_fake_encode)
        skills = [
            _make_skill("alert-analysis", "Alert Analysis", "Analyze alerts"),
            _make_skill("remediation", "Remediation", "Execute fixes"),
        ]
        rag.populate("cluster-ops", skills)

        result = rag.match("Analyze this alert")
        assert result is not None
        agent, skill_id = result
        assert agent == "cluster-ops"
        assert skill_id in ("alert-analysis", "remediation")

    def test_multiple_agents(self):
        rag = AgentSkillRAG(encode_fn=_fake_encode)
        rag.populate(
            "agent-a",
            [_make_skill("s1", "Skill A", "Does A things")],
        )
        rag.populate(
            "agent-b",
            [_make_skill("s2", "Skill B", "Does B things")],
        )

        result = rag.match("Do something")
        assert result is not None
        agent, skill_id = result
        assert agent in ("agent-a", "agent-b")
        assert skill_id in ("s1", "s2")

    def test_match_always_returns_best(self):
        rag = AgentSkillRAG(encode_fn=_fake_encode)
        rag.populate(
            "ops",
            [
                _make_skill("deploy", "Deploy", "Deploy applications"),
                _make_skill("scale", "Scale", "Scale workloads"),
            ],
        )

        result = rag.match("deploy the new version")
        assert result is not None

    def test_fuse_scores_weighted(self):
        rag = AgentSkillRAG(encode_fn=_fake_encode, alpha=0.5)
        dense = {"a": 0.8, "b": 0.2}
        sparse = {"a": 0.2, "b": 0.8}
        fused = rag._fuse_scores(dense, sparse)
        assert fused["a"] == pytest.approx(0.5)
        assert fused["b"] == pytest.approx(0.5)

    def test_fuse_scores_dense_only(self):
        rag = AgentSkillRAG(encode_fn=_fake_encode, alpha=1.0)
        dense = {"a": 0.9}
        sparse = {"a": 0.1}
        fused = rag._fuse_scores(dense, sparse)
        assert fused["a"] == pytest.approx(0.9)

    def test_fuse_scores_truncates_to_top_k(self):
        rag = AgentSkillRAG(encode_fn=_fake_encode, top_k=2)
        dense = {"a": 0.9, "b": 0.5, "c": 0.1}
        fused = rag._fuse_scores(dense, {})
        assert len(fused) == 2
        assert "a" in fused
        assert "b" in fused
