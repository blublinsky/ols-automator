"""Hybrid RAG for A2A agent skill discovery and matching."""

import json
import logging
import re
import uuid
from collections.abc import Callable

from a2a.types import AgentSkill
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)
from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]
from sentence_transformers import SentenceTransformer

from app.models.models import AgentConfig

logger = logging.getLogger(__name__)


def _make_encode_fn(model_name: str) -> Callable[[str], list[float]]:
    """Create an embedding function using sentence-transformers."""
    for name in ("sentence_transformers", "transformers"):
        logging.getLogger(name).setLevel(logging.ERROR)

    model = SentenceTransformer(model_name)

    def encode(text: str) -> list[float]:
        return model.encode(text, normalize_embeddings=True).tolist()

    return encode


_NON_ALPHA = re.compile(r"[^a-z0-9\s]")

_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "need",
        "dare",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "i",
        "we",
        "you",
        "he",
        "she",
        "they",
        "me",
        "him",
        "her",
        "us",
        "them",
        "my",
        "our",
        "your",
        "his",
        "their",
        "what",
        "which",
        "who",
        "whom",
        "how",
        "when",
        "where",
        "why",
        "not",
        "no",
        "nor",
        "so",
        "if",
        "then",
        "than",
        "too",
        "very",
        "just",
        "about",
        "above",
        "after",
        "before",
        "between",
        "into",
        "out",
        "up",
        "down",
        "over",
        "under",
        "again",
        "further",
        "once",
    }
)


def _tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, remove stop words, and split."""
    tokens = _NON_ALPHA.sub("", text.lower()).split()
    return [t for t in tokens if t not in _STOP_WORDS]


# ---------------------------------------------------------------------------
# In-memory Qdrant vector store
# ---------------------------------------------------------------------------


class _QdrantStore:
    """Wrapper for in-memory vector database operations backed by Qdrant."""

    def __init__(self, collection: str) -> None:
        self._collection = collection
        self.client = QdrantClient(location=":memory:")
        self._collection_ready = False

    def _ensure_collection(self, vector_size: int) -> None:
        """Lazily create the vector collection on first upsert."""
        if self._collection_ready:
            return
        self.client.create_collection(
            self._collection,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
        self._collection_ready = True

    @staticmethod
    def _point_id(string_id: str) -> str:
        """Convert an arbitrary string ID to a deterministic UUID for Qdrant."""
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, string_id))

    def upsert(
        self,
        ids: list[str],
        docs: list[str],
        vectors: list[list[float]],
        metadatas: list[dict[str, str]] | None = None,
    ) -> None:
        """Add or update documents with embeddings in the collection."""
        if not vectors:
            return
        self._ensure_collection(len(vectors[0]))

        points = []
        for i, (str_id, doc, vec) in enumerate(zip(ids, docs, vectors)):
            payload: dict[str, str] = {"_id": str_id, "_document": doc}
            if metadatas and i < len(metadatas):
                payload.update(metadatas[i])
            points.append(
                PointStruct(id=self._point_id(str_id), vector=vec, payload=payload)
            )
        self.client.upsert(self._collection, points=points)

    def search_with_scores(
        self,
        vector: list[float],
        k: int,
    ) -> tuple[list[str], list[float], list[dict[str, str]]]:
        """Search and return IDs, similarity scores, and metadata."""
        if not self._collection_ready:
            return [], [], []

        results = self.client.query_points(
            self._collection,
            query=vector,
            limit=k,
        )

        out_ids: list[str] = []
        scores: list[float] = []
        metas: list[dict[str, str]] = []
        for point in results.points:
            payload = point.payload or {}
            out_ids.append(payload["_id"])
            scores.append(point.score)
            metas.append(
                {key: v for key, v in payload.items() if not key.startswith("_")}
            )
        return out_ids, scores, metas

    def get_all(self) -> dict[str, list]:
        """Get all documents with their metadata."""
        if not self._collection_ready:
            return {"ids": [], "documents": [], "metadatas": []}

        records, _ = self.client.scroll(self._collection, limit=10_000)

        out_ids: list[str] = []
        documents: list[str] = []
        metas: list[dict[str, str]] = []
        for record in records:
            payload = record.payload or {}
            out_ids.append(payload["_id"])
            documents.append(payload.get("_document", ""))
            metas.append(
                {key: v for key, v in payload.items() if not key.startswith("_")}
            )
        return {"ids": out_ids, "documents": documents, "metadatas": metas}


# ---------------------------------------------------------------------------
# Agent skill RAG (hybrid dense + sparse retrieval)
# ---------------------------------------------------------------------------


class AgentSkillRAG:
    """Hybrid RAG for matching operations to A2A agent skills.

    Combines dense retrieval (Qdrant cosine similarity) with sparse
    retrieval (BM25) to find the best-matching agent for an operation.
    """

    _COLLECTION = "agent_skills"

    def __init__(
        self,
        encode_fn: Callable[[str], list[float]],
        alpha: float = 0.8,
        top_k: int = 10,
    ) -> None:
        self.alpha = alpha
        self.top_k = top_k
        self._encode = encode_fn
        self.bm25: BM25Okapi | None = None
        self.store = _QdrantStore(self._COLLECTION)

    def populate(self, agent_name: str, skills: list[AgentSkill]) -> None:
        """Index skills from an agent card.

        Args:
            agent_name: Name of the agent these skills belong to.
            skills: Skills from the agent's AgentCard.
        """
        ids: list[str] = []
        dense_docs: list[str] = []
        vectors: list[list[float]] = []
        metadatas: list[dict[str, str]] = []

        for skill in skills:
            text = f"{skill.name} {skill.description or ''}"
            skill_dict = {
                "name": skill.name,
                "id": skill.id,
                "desc": skill.description or "",
                "server": agent_name,
            }

            ids.append(f"{agent_name}::{skill.id}")
            dense_docs.append(text)
            vectors.append(self._encode(text))
            metadatas.append(
                {
                    "skill_json": json.dumps(skill_dict),
                    "server": agent_name,
                }
            )

        self.store.upsert(ids, dense_docs, vectors, metadatas=metadatas)
        self._rebuild_bm25()

    def match(self, operation: str) -> tuple[str, str] | None:
        """Find the best agent and skill for an operation.

        Returns the single highest-scoring (agent_name, skill_id) pair,
        or None if no skills are indexed.
        """
        q_vec = self._encode(operation)

        dense, dense_ids, dense_metas = self._dense_scores(q_vec)
        metadata_lookup = {
            tid: json.loads(meta["skill_json"])
            for tid, meta in zip(dense_ids, dense_metas)
        }

        sparse, sparse_metadata = self._sparse_with_filter(operation)
        for tid, skill_dict in sparse_metadata.items():
            if tid not in metadata_lookup:
                metadata_lookup[tid] = skill_dict

        fused = self._fuse_scores(dense, sparse)
        if not fused:
            return None

        best_id = next(iter(fused))
        skill = metadata_lookup.get(best_id)
        if not skill:
            return None
        return skill["server"], skill["id"]

    # --- Retrieval internals ---

    def _rebuild_bm25(self) -> None:
        """Rebuild BM25 index from all stored documents."""
        all_data = self.store.get_all()
        if not all_data["documents"]:
            self.bm25 = None
            return
        sparse_docs = [_tokenize(doc) for doc in all_data["documents"]]
        self.bm25 = BM25Okapi(sparse_docs)

    def _dense_scores(
        self, query_vec: list[float]
    ) -> tuple[dict[str, float], list[str], list[dict[str, str]]]:
        """Compute dense retrieval scores using cosine similarity."""
        ids, sim_scores, metas = self.store.search_with_scores(query_vec, self.top_k)
        return dict(zip(ids, sim_scores)), ids, metas

    def _sparse_scores(
        self, query: str
    ) -> tuple[dict[str, float], dict[str, dict[str, str]]]:
        """Compute BM25 scores normalized to 0-1 range."""
        if self.bm25 is None:
            return {}, {}

        all_data = self.store.get_all()
        ids = all_data["ids"]
        raw = self.bm25.get_scores(_tokenize(query))
        clamped = [max(0.0, s) for s in raw]
        mx = max(clamped) or 1.0
        scores = {sid: score / mx for sid, score in zip(ids, clamped)}
        meta_by_id = dict(zip(ids, all_data["metadatas"]))
        return scores, meta_by_id

    def _sparse_with_filter(
        self, query: str
    ) -> tuple[dict[str, float], dict[str, dict[str, str]]]:
        """Retrieve BM25 scores with metadata parsing."""
        base_scores, meta_by_id = self._sparse_scores(query)
        if not base_scores:
            return {}, {}

        scores: dict[str, float] = {}
        metadata: dict[str, dict[str, str]] = {}
        for name, score in base_scores.items():
            raw_meta = meta_by_id.get(name)
            if raw_meta is None:
                continue
            scores[name] = score
            metadata[name] = json.loads(raw_meta["skill_json"])

        return scores, metadata

    def _fuse_scores(
        self,
        dense: dict[str, float],
        sparse: dict[str, float],
    ) -> dict[str, float]:
        """Fuse dense and sparse scores using weighted combination."""
        fused: dict[str, float] = {}
        for t in dense.keys() | sparse.keys():
            d = dense.get(t, 0)
            s = sparse.get(t, 0)
            fused[t] = self.alpha * d + (1 - self.alpha) * s
        return dict(
            sorted(fused.items(), key=lambda x: x[1], reverse=True)[: self.top_k]
        )


async def discover_agents(agents: list[AgentConfig]) -> AgentSkillRAG | None:
    """Fetch agent cards, cache them, and build a skill RAG index.

    Returns None if no agents are configured or all discoveries fail.
    Cards are stored in ``get_config().agent_cards`` for later use.
    """
    if not agents:
        logger.info("No agents configured, skipping discovery")
        return None

    from app.models.config import get_config
    from app.services.a2a_client import fetch_agent_card

    cfg = get_config()
    rag = AgentSkillRAG(encode_fn=_make_encode_fn(cfg.embedding_model))
    total_skills = 0

    for agent in agents:
        try:
            card = await fetch_agent_card(
                agent.url, agent.resolve_headers(), agent.timeout
            )
            cfg.agent_cards[agent.name] = card
            skill_count = len(card.skills) if card.skills else 0
            rag.populate(agent.name, card.skills)
            total_skills += skill_count
        except Exception:
            logger.exception("Failed to discover agent '%s'", agent.name)

    logger.info(
        "Discovery complete: %d/%d agents, %d skills indexed",
        len(cfg.agent_cards),
        len(agents),
        total_skills,
    )
    return rag
