"""Транзакции PostgreSQL для исходных версий и результатов обработки."""

from hashlib import sha256
import json
import logging
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, LargeBinary, MetaData,
    Table, Text, UniqueConstraint, Uuid, and_, func, select, text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import DBAPIError, DataError, IntegrityError

from coursegen.chunking import build_chunks
from coursegen.documents.extraction import ExtractionError, extract_document, parser_version


metadata = MetaData(schema="coursegen")
docs = Table("documents", metadata, Column("document_id", Uuid, primary_key=True))
versions = Table(
    "document_versions", metadata,
    Column("document_id", Uuid, ForeignKey("coursegen.documents.document_id"), primary_key=True),
    Column("document_version", Integer, primary_key=True), Column("filename", Text, nullable=False),
    Column("format", Text, nullable=False), Column("source_meta", JSONB, nullable=False),
    Column("content_hash", Text, nullable=False), Column("identity_hash", Text, nullable=False),
    Column("original", LargeBinary, nullable=False), Column("config", JSONB, nullable=False),
    Column("status", Text, nullable=False), Column("error_code", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("page_count", Integer, nullable=False, server_default=text("0")),
    Column("chunk_count", Integer, nullable=False, server_default=text("0")),
    Column("requirements_extracted", Boolean, nullable=False, server_default=text("false")),
    UniqueConstraint("document_id", "identity_hash"),
    Index("document_queue", "created_at", postgresql_where=text("status = 'queued'")),
    Index("requirements_queue", "created_at", postgresql_where=text(
        "NOT requirements_extracted AND status IN ('ready', 'partial') "
        "AND source_meta->>'source_type' IN ('syllabus', 'assessment')")),
)
pages = Table(
    "document_pages", metadata, Column("document_id", Uuid, primary_key=True),
    Column("document_version", Integer, primary_key=True), Column("number", Integer, primary_key=True),
    Column("text", Text, nullable=False), Column("status", Text, nullable=False),
    Column("blocks", JSONB, nullable=False), Column("warnings", JSONB, nullable=False),
    ForeignKeyConstraint(["document_id", "document_version"],
                         ["coursegen.document_versions.document_id", "coursegen.document_versions.document_version"]),
)
chunks = Table(
    "document_chunks", metadata, Column("chunk_id", Uuid, primary_key=True),
    Column("document_id", Uuid, nullable=False), Column("document_version", Integer, nullable=False),
    Column("ordinal", Integer, nullable=False), Column("page_start", Integer, nullable=False),
    Column("payload", JSONB, nullable=False), UniqueConstraint("document_id", "document_version", "ordinal"),
    ForeignKeyConstraint(["document_id", "document_version", "page_start"],
                         ["coursegen.document_pages.document_id", "coursegen.document_pages.document_version",
                          "coursegen.document_pages.number"]),
)
indexes = Table(
    "search_indexes", metadata, Column("document_id", Uuid, primary_key=True),
    Column("document_version", Integer, primary_key=True), Column("generation", Uuid, nullable=False),
    Column("status", Text, nullable=False), Column("error_code", Text),
    Column("indexed_count", Integer, nullable=False, server_default=text("0")),
    Column("model_version", Text, nullable=False),
    ForeignKeyConstraint(["document_id", "document_version"],
                         ["coursegen.document_versions.document_id", "coursegen.document_versions.document_version"]),
)
requirements = Table(
    "requirements", metadata, Column("requirement_id", Uuid, primary_key=True),
    Column("document_id", Uuid, nullable=False), Column("document_version", Integer, nullable=False),
    Column("page", Integer, nullable=False), Column("char_start", Integer, nullable=False),
    Column("char_end", Integer, nullable=False), Column("quote", Text, nullable=False),
    Column("kind", Text, nullable=False), Column("text", Text, nullable=False),
    Column("status", Text, nullable=False), Column("revision", Integer, nullable=False),
    ForeignKeyConstraint(["document_id", "document_version"],
                         ["coursegen.document_versions.document_id", "coursegen.document_versions.document_version"]),
    Index("requirements_source", "document_id", "document_version", "page"),
)
graph_versions = Table(
    "graph_versions", metadata, Column("graph_id", Uuid, primary_key=True),
    Column("version", Integer, primary_key=True), Column("snapshot", JSONB, nullable=False),
)
public_fields = [c for c in versions.c if c.name not in {"original", "identity_hash"}]
logger = logging.getLogger(__name__)


def at_version(table: Table, doc_id: UUID, number: int):
    return and_(table.c.document_id == doc_id, table.c.document_version == number)


def lookup(conn: Connection, doc_id: UUID, number: int | None) -> RowMapping | None:
    query = select(*public_fields).where(versions.c.document_id == doc_id)
    if number is not None:
        query = query.where(versions.c.document_version == number)
    return conn.execute(query.order_by(versions.c.document_version.desc()).limit(1)).mappings().first()


def register(conn: Connection, data: bytes, filename: str, fmt: str, meta: dict,
             config: dict, doc_id: UUID | None):
    content_hash = sha256(data).hexdigest()
    identity = json.dumps([content_hash, filename, fmt, meta], ensure_ascii=False, sort_keys=True)
    identity_hash = sha256(identity.encode()).hexdigest()
    if doc_id is None:
        doc_id = uuid5(NAMESPACE_URL, "coursegen:document:" + identity_hash)
        conn.execute(insert(docs).values(document_id=doc_id).on_conflict_do_nothing())
    locked = conn.scalar(select(docs.c.document_id).where(docs.c.document_id == doc_id).with_for_update())
    if locked is None:
        raise KeyError(doc_id)
    existing = conn.scalar(select(versions.c.document_version).where(
        versions.c.document_id == doc_id, versions.c.identity_hash == identity_hash))
    if existing is not None:
        return lookup(conn, doc_id, existing)
    maximum = conn.scalar(select(func.max(versions.c.document_version)).where(versions.c.document_id == doc_id))
    number = (maximum or 0) + 1
    conn.execute(versions.insert().values(document_id=doc_id, document_version=number,
                 original=data, content_hash=content_hash, identity_hash=identity_hash,
                 filename=filename, format=fmt, source_meta=meta, config=config, status="queued"))
    return lookup(conn, doc_id, number)


def _publish_version(conn: Connection, row: RowMapping) -> None:
    doc_id, number, config = row["document_id"], row["document_version"], row["config"]
    parser = parser_version(row["format"])
    if config["pipeline"] != "pages-v1" or config.get("parser", parser) != parser:
        raise ExtractionError("pipeline_version_mismatch")
    src = extract_document(row["original"], row["format"],
                           max_pages=config["max_pages"], max_chars=config["max_chars"])
    pieces = build_chunks(src, doc_id, number, strategy=config["strategy"], size=config["size"],
                          overlap=config["overlap"])
    conn.execute(chunks.delete().where(at_version(chunks, doc_id, number)))
    conn.execute(pages.delete().where(at_version(pages, doc_id, number)))
    conn.execute(pages.insert(), [dict(document_id=doc_id, document_version=number,
                 number=p.number, text=p.text, status=p.status, blocks=p.blocks, warnings=p.warnings)
                 for p in src])
    for start in range(0, len(pieces), 500):
        batch = []
        for piece in pieces[start:start + 500]:
            payload = dict(piece)
            key = payload.pop("chunk_id")
            ordinal = payload.pop("ordinal")
            first_page = payload.pop("page_start")
            if payload["parent_id"]:
                payload["parent_id"] = str(payload["parent_id"])
            batch.append(dict(document_id=doc_id, document_version=number, chunk_id=key,
                              ordinal=ordinal, page_start=first_page, payload=payload))
        conn.execute(chunks.insert(), batch)
    missing = sum(p.status == "needs_ocr" for p in src)
    status = "ready"
    if missing:
        status = "needs_ocr" if missing == len(src) else "partial"
    conn.execute(versions.update().where(at_version(versions, doc_id, number)).values(
        status=status, error_code=None, page_count=len(src), chunk_count=len(pieces), requirements_extracted=False))
    if status == "needs_ocr":
        conn.execute(indexes.update().where(at_version(indexes, doc_id, number), indexes.c.status == "queued")
                     .values(status="failed", error_code="source_needs_ocr"))


def process_next(engine: Engine) -> bool:
    # A worker crash releases the row lock and rolls back the whole unpublished result.
    with engine.begin() as conn:
        row = conn.execute(select(versions).where(versions.c.status == "queued")
                           .order_by(versions.c.created_at, versions.c.document_id, versions.c.document_version)
                           .limit(1).with_for_update(skip_locked=True)).mappings().first()
        if row is None:
            return False
        try:
            with conn.begin_nested():
                _publish_version(conn, row)
        except Exception as exc:
            if isinstance(exc, DBAPIError) and not isinstance(exc, (DataError, IntegrityError)):
                raise
            code = str(exc) if isinstance(exc, ExtractionError) else "processing_error"
            doc_id, number = row["document_id"], row["document_version"]
            logger.warning(json.dumps({"event": "document_processing_failed", "document_id": str(doc_id),
                                       "document_version": number, "reason": code, "error_type": type(exc).__name__}))
            conn.execute(versions.update().where(at_version(versions, doc_id, number)).values(
                status="failed", error_code=code, page_count=0, chunk_count=0))
            conn.execute(indexes.update().where(at_version(indexes, doc_id, number), indexes.c.status == "queued")
                         .values(status="failed", error_code="source_processing_failed"))
        return True
