from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from qdrant_client import QdrantClient, models as qm
from sqlalchemy import Engine, select

from coursegen.core.config import Settings
from coursegen.documents import storage as db
from coursegen.retrieval.embedding import RetrievalError, encoder
from coursegen.retrieval.index import COLLECTION, JOIN_VERSION, bm25, check_collection, field
from coursegen.retrieval.model_spec import MODEL_VERSION


Mode = Literal["bm25", "dense", "hybrid"]
SearchMode = Literal["bm25", "dense", "hybrid", "hybrid_graph"]
QueryText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class RetrievalFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_document_ids: list[UUID] | None = Field(default=None, max_length=1000)
    document_version: int | None = Field(default=None, ge=1)
    source_type: Literal["textbook", "syllabus", "assessment", "manual", "documentation", "other"] | None = None
    currency: Literal["current", "historical", "requires_verification"] | None = None
    topic: str | None = Field(default=None, min_length=1, max_length=200)


class GraphSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    graph_id: UUID
    version: int = Field(ge=1)
    concept_id: str = Field(min_length=1, max_length=200)
    sources_only: bool = False


class GraphTrace(GraphSelection):
    terms: list[str]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: QueryText
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)
    mode: SearchMode = "hybrid"
    graph: GraphSelection | None = None
    top_k: int = Field(default=5, ge=1, le=100)
    bm25_candidates: int = Field(default=30, ge=1, le=200)
    dense_candidates: int = Field(default=30, ge=1, le=200)

    @model_validator(mode="after")
    def graph_selection(self) -> Self:
        if (self.mode == "hybrid_graph") != (self.graph is not None):
            raise ValueError("Для hybrid_graph укажите граф, его версию и понятие.")
        return self


class RetrievedChunk(BaseModel):
    chunk_id: UUID
    document_id: UUID
    document_version: int
    filename: str
    page_start: int
    page_end: int
    section_path: list[str]
    text: str
    content_hash: str
    rank: int
    score: float
    bm25_score: float | None
    dense_score: float | None
    mode: SearchMode
    graph: GraphTrace | None = None
    filters: RetrievalFilters
    embedding_model_version: str | None


class Retriever(Protocol):
    def search(self, query: str, filters: RetrievalFilters, top_k: int) -> list[RetrievedChunk]: ...


@dataclass
class QdrantRetriever:
    engine: Engine
    client: QdrantClient
    settings: Settings
    mode: Mode
    bm25_candidates: int
    dense_candidates: int
    graph_scope: qm.Filter | None = None

    def ranked(self, query: str, scope: qm.Filter, top_k: int
               ) -> tuple[list[qm.ScoredPoint], dict[str, dict[str | int, float]]]:
        common = dict(filter=scope, with_payload=["generation", "content_hash"])
        requests = {}
        if self.mode in ("bm25", "hybrid"):
            requests["bm25"] = qm.QueryRequest(query=qm.NearestQuery(nearest=bm25(query)), using="bm25",
                    limit=self.bm25_candidates if self.mode == "hybrid" else top_k, **common)
        if self.mode in ("dense", "hybrid"):
            vector = encoder(self.settings.embedding_model_dir).encode(query)
            requests["dense"] = qm.QueryRequest(query=qm.NearestQuery(nearest=vector), using="dense",
                    limit=self.dense_candidates if self.mode == "hybrid" else top_k, **common)
        if self.mode == "hybrid":
            prefetch = [qm.Prefetch(query=r.query, using=r.using, limit=r.limit, filter=scope)
                        for r in requests.values()]
            requests["hybrid"] = qm.QueryRequest(prefetch=prefetch, query=qm.RrfQuery(rrf=qm.Rrf(k=60)),
                                                  limit=top_k, **common)
        response = self.client.http.search_api.query_batch_points(COLLECTION,
                    query_request_batch=qm.QueryRequestBatch(searches=list(requests.values())))
        groups = {key: item.points for key, item in zip(requests, response.result, strict=True)}
        components = {name: {p.id: p.score for p in group} for name, group in groups.items()}
        return groups[self.mode], components

    def search(self, query: str, filters: RetrievalFilters, top_k: int) -> list[RetrievedChunk]:
        idx, ver = db.indexes.c, db.versions.c
        active = select(idx.generation, idx.document_id, idx.document_version, ver.filename).join(
            db.versions, JOIN_VERSION).where(idx.status == "indexed", idx.model_version == MODEL_VERSION,
                                             ver.status.in_(["ready", "partial"]))
        if filters.allowed_document_ids is not None:
            active = active.where(idx.document_id.in_(filters.allowed_document_ids))
        if filters.document_version is not None:
            active = active.where(idx.document_version == filters.document_version)
        else:
            newer = db.versions.alias("newer")
            active = active.where(~select(newer.c.document_id).where(newer.c.document_id == idx.document_id,
                                  newer.c.document_version > idx.document_version).exists())
        for name in ("source_type", "currency"):
            value = getattr(filters, name)
            if value:
                active = active.where(ver.source_meta[name].astext == value)
        with self.engine.begin() as conn:
            # Keep the published generations stable through ranking and citation lookup.
            sources = {str(row.generation): row for row in conn.execute(active.with_for_update(read=True, of=db.indexes))}
            if not sources:
                return []
            if not check_collection(self.client):
                raise RetrievalError("collection_missing")
            conditions = [qm.FieldCondition(key="generation", match=qm.MatchAny(any=list(sources)))]
            if filters.topic:
                conditions.append(field("topic", filters.topic))
            if self.graph_scope is not None:
                conditions.append(self.graph_scope)
            points, scores = self.ranked(query, qm.Filter(must=conditions), top_k)
            ids = [UUID(str(point.id)) for point in points]
            chunks = {row.chunk_id: row for row in conn.execute(select(db.chunks).where(db.chunks.c.chunk_id.in_(ids)))}
            results = []
            for rank, (point, key) in enumerate(zip(points, ids, strict=True), 1):
                payload = point.payload or {}
                source = sources.get(payload.get("generation"))
                row = chunks.get(key)
                if (row is None or source is None or row.document_id != source.document_id
                        or row.document_version != source.document_version
                        or row.payload["content_hash"] != payload.get("content_hash")):
                    raise RetrievalError("index_verification_failed")
                results.append(RetrievedChunk(chunk_id=key, document_id=row.document_id,
                       document_version=row.document_version, filename=source.filename,
                       page_start=row.page_start, page_end=row.payload["page_end"], text=row.payload["text"],
                       section_path=row.payload["section_path"], content_hash=row.payload["content_hash"],
                       rank=rank, score=point.score, bm25_score=scores.get("bm25", {}).get(point.id),
                       dense_score=scores.get("dense", {}).get(point.id), mode=self.mode, filters=filters,
                       embedding_model_version=MODEL_VERSION if self.mode != "bm25" else None))
        return results
