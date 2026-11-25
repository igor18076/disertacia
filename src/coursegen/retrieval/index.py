"""Публикация версии в Qdrant после записи и сверки всех её фрагментов."""

import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from qdrant_client import QdrantClient, models as qm
from qdrant_client.http.exceptions import UnexpectedResponse
from sqlalchemy import Select, and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import DBAPIError

from coursegen.core.config import Settings
from coursegen.documents import storage as db
from coursegen.retrieval.embedding import RetrievalError, encoder
from coursegen.retrieval.model_spec import DIMENSION, MODEL_VERSION


COLLECTION = "coursegen_chunks_v1"
SCHEMA = {"embedding": MODEL_VERSION, "sparse": "qdrant/bm25:russian", "schema": 1}
JOIN_VERSION = and_(db.versions.c.document_id == db.indexes.c.document_id,
                    db.versions.c.document_version == db.indexes.c.document_version)
logger = logging.getLogger(__name__)


def field(key: str, value: str | int) -> qm.FieldCondition:
    return qm.FieldCondition(key=key, match=qm.MatchValue(value=value))


def source_filter(doc_id: UUID, version: int) -> qm.Filter:
    return qm.Filter(must=[field("document_id", str(doc_id)), field("document_version", version)])


def bm25(text: str) -> qm.Document:
    return qm.Document(text=text, model="qdrant/bm25", options={"language": "russian"})


def check_collection(client: QdrantClient, *, create: bool = False) -> bool:
    if not client.collection_exists(COLLECTION):
        if not create:
            return False
        try:
            client.create_collection(COLLECTION, vectors_config={
                "dense": qm.VectorParams(size=DIMENSION, distance=qm.Distance.COSINE)},
                sparse_vectors_config={"bm25": qm.SparseVectorParams(modifier=qm.Modifier.IDF)},
                metadata=SCHEMA)
        except UnexpectedResponse:
            if not client.collection_exists(COLLECTION):
                raise
    config = client.get_collection(COLLECTION).config
    dense = config.params.vectors
    sparse = config.params.sparse_vectors or {}
    if (config.metadata != SCHEMA or not isinstance(dense, dict) or set(dense) != {"dense"}
            or dense["dense"].size != DIMENSION or dense["dense"].distance != qm.Distance.COSINE
            or set(sparse) != {"bm25"} or sparse["bm25"].modifier != qm.Modifier.IDF):
        raise RetrievalError("collection_schema_mismatch")
    return True


def leaf_query(doc_id: UUID, number: int) -> Select:
    return select(db.chunks).where(db.at_version(db.chunks, doc_id, number),
                                   db.chunks.c.payload["role"].astext == "leaf")


def enqueue(conn: Connection, doc_id: UUID, version: int, remove: bool) -> RowMapping:
    key = db.at_version(db.indexes, doc_id, version)
    status = "removing" if remove else "queued"
    pending = conn.execute(select(db.indexes).where(key, db.indexes.c.status == status)).mappings().first()
    if pending is not None:
        return pending
    values = dict(status=status, generation=uuid4(), error_code=None, indexed_count=0,
                  model_version=MODEL_VERSION)
    statement = insert(db.indexes).values(document_id=doc_id, document_version=version, **values)
    conn.execute(statement.on_conflict_do_update(index_elements=["document_id", "document_version"],
                 set_=values, where=db.indexes.c.status != status))
    return conn.execute(select(db.indexes).where(key)).mappings().one()


def discrepancy(conn: Connection, client: QdrantClient, job: Mapping[str, Any]) -> dict[str, int]:
    doc_id, number = job["document_id"], job["document_version"]
    expected = {}
    if job["status"] != "absent":
        source = leaf_query(doc_id, number).with_only_columns(db.chunks.c.chunk_id,
                                                             db.chunks.c.payload["content_hash"].astext)
        expected = {str(key): digest for key, digest in conn.execute(source)}
    found = {}
    if check_collection(client):
        offset = None
        while True:
            points, offset = client.scroll(COLLECTION, scroll_filter=source_filter(doc_id, number),
                                           limit=256, offset=offset, with_vectors=False,
                                           with_payload=["content_hash", "generation"])
            for point in points:
                payload = point.payload or {}
                found[str(point.id)] = (payload.get("content_hash"), payload.get("generation"))
            if offset is None:
                break
    mismatched = sum(found[key] != (expected[key], str(job["generation"])) for key in expected.keys() & found.keys())
    return dict(expected=len(expected), actual=len(found), missing=len(expected.keys() - found.keys()),
                unexpected=len(found.keys() - expected.keys()), mismatched=mismatched)


def publish(conn: Connection, client: QdrantClient, settings: Settings, job: RowMapping) -> int:
    doc_id, number = job["document_id"], job["document_version"]
    removing = job["status"] == "removing"
    if not check_collection(client, create=not removing):
        return 0
    model = None if removing else encoder(settings.embedding_model_dir)
    client.delete(COLLECTION, points_selector=source_filter(doc_id, number), wait=True)
    if removing:
        return 0
    meta = job["source_meta"]
    rows = conn.execute(leaf_query(doc_id, number).order_by(db.chunks.c.ordinal)
                        .execution_options(yield_per=settings.search_batch_size)).mappings()
    for batch in rows.partitions(settings.search_batch_size):
        points = []
        for row in batch:
            chunk = row["payload"]
            topics = list(chunk["section_path"])
            if meta.get("topic"):
                topics.append(meta["topic"])
            payload = dict(document_id=str(doc_id), document_version=number, generation=str(job["generation"]),
                           content_hash=chunk["content_hash"], source_type=meta["source_type"],
                           currency=meta["currency"], topic=topics)
            points.append(qm.PointStruct(id=str(row["chunk_id"]), payload=payload,
                          vector={"dense": model.encode(chunk["text"]), "bm25": bm25(chunk["text"])}))
        client.http.points_api.upsert_points(COLLECTION, wait=True, point_insert_operations=qm.PointsList(points=points))
    check = discrepancy(conn, client, job)
    if check["missing"] or check["unexpected"] or check["mismatched"]:
        raise RetrievalError("index_verification_failed")
    return check["actual"]


def process_index(engine: Engine, client: QdrantClient, settings: Settings) -> bool:
    with engine.begin() as conn:
        query = (select(db.indexes, db.versions.c.source_meta).join(db.versions, JOIN_VERSION)
                 .where(or_(db.indexes.c.status == "removing", and_(db.indexes.c.status == "queued",
                            db.versions.c.status.in_(["ready", "partial"]))))
                 .order_by(db.indexes.c.document_id, db.indexes.c.document_version)
                 .limit(1).with_for_update(of=db.indexes, skip_locked=True))
        job = conn.execute(query).mappings().first()
        if job is None:
            return False
        try:
            count = publish(conn, client, settings, job)
            state = dict(status="absent" if job["status"] == "removing" else "indexed",
                         indexed_count=count, error_code=None)
        except DBAPIError:
            raise
        except Exception as exc:
            reason = str(exc) if isinstance(exc, RetrievalError) else "index_operation_failed"
            logger.warning("index_failed document_id=%s version=%s reason=%s error_type=%s",
                           job["document_id"], job["document_version"], reason, type(exc).__name__)
            state = dict(status="remove_failed" if job["status"] == "removing" else "failed",
                         indexed_count=0, error_code=reason)
        conn.execute(db.indexes.update().where(db.at_version(db.indexes, job["document_id"],
                                                            job["document_version"])).values(**state))
        return True
