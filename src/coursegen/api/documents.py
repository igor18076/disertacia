from datetime import datetime
from hashlib import sha256
from pathlib import PurePath
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import and_, func, select

from coursegen.chunking import digest
from coursegen.documents import storage as db
from coursegen.documents.extraction import extract_document, parser_version


router = APIRouter(prefix="/api/v1/documents", tags=["Документы"])
Version = Annotated[int | None, Query(ge=1)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
ShortText = Annotated[str, Query(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f]+$")]
MIME = {"pdf": "application/pdf", "txt": "text/plain", "md": "text/markdown"}


class Document(BaseModel):
    requirements_extracted: bool
    document_id: UUID
    document_version: int
    filename: str
    format: str
    source_meta: dict
    content_hash: str
    config: dict
    status: str
    error_code: str | None
    created_at: datetime
    page_count: int
    chunk_count: int


class Chunk(BaseModel):
    document_id: UUID
    document_version: int
    chunk_id: UUID
    ordinal: int
    page_start: int
    page_end: int
    char_start: int
    char_end: int
    section_path: list[str]
    text: str
    content_hash: str
    token_count: int
    language: str
    extraction_status: str
    embedding_model_version: str | None = None
    role: str
    parent_id: UUID | None


def version_or_404(conn, doc_id: UUID, number: int | None):
    row = db.lookup(conn, doc_id, number)
    if row is None:
        raise HTTPException(404, "Документ или версия не найдены.")
    return row


def available(row) -> None:
    if row["status"] in ("queued", "failed"):
        raise HTTPException(409, "Результаты обработки этой версии пока недоступны.")


@router.post("", status_code=202, summary="Загрузить оригинал и поставить версию в очередь",
             openapi_extra={"requestBody": {"required": True, "content": {
                 mime: {"schema": {"type": "string", "format": "binary"}} for mime in MIME.values()
             }}})
async def upload(
    request: Request, filename: ShortText, document_id: UUID | None = None,
    source_type: Literal["textbook", "syllabus", "assessment", "manual", "documentation", "other"] = "other",
    currency: Literal["current", "historical", "requires_verification"] = "requires_verification",
    language: Annotated[str, Query(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]+)*$", max_length=32)] = "und",
    publication_year: Annotated[int | None, Query(ge=1, le=9999)] = None,
    technology_version: ShortText | None = None, license_terms: ShortText | None = None,
    topic: ShortText | None = None,
) -> Document:
    if "/" in filename or "\\" in filename:
        raise HTTPException(422, "Укажите имя файла без пути.")
    fmt = PurePath(filename).suffix[1:].lower()
    if fmt == "markdown":
        fmt = "md"
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if fmt not in MIME or media_type not in {MIME.get(fmt), "application/octet-stream"}:
        raise HTTPException(415, "Поддерживаются PDF, TXT и Markdown с соответствующим типом содержимого.")
    settings = request.app.state.settings
    length = request.headers.get("content-length")
    if length is not None and (not length.isascii() or not length.isdecimal() or len(length) > 20):
        raise HTTPException(400, "Некорректный Content-Length.")
    if length is not None and int(length) > settings.document_max_bytes:
        raise HTTPException(413, "Файл превышает допустимый размер.")
    data = bytearray()
    async for block in request.stream():
        if len(data) + len(block) > settings.document_max_bytes:
            raise HTTPException(413, "Файл превышает допустимый размер.")
        data.extend(block)
    if not data:
        raise HTTPException(422, "Пустой файл.")
    if (fmt == "pdf") != data.startswith(b"%PDF-"):
        raise HTTPException(415, "Сигнатура файла не соответствует формату.")
    meta = dict(source_type=source_type, currency=currency, language=language, publication_year=publication_year,
                technology_version=technology_version, license_terms=license_terms)
    if topic is not None:
        meta["topic"] = topic
    config = dict(pipeline="pages-v1", parser=parser_version(fmt), strategy=settings.chunk_strategy,
                  size=settings.chunk_size, overlap=settings.chunk_overlap,
                  max_pages=settings.document_max_pages, max_chars=settings.document_max_text_chars)

    def save():
        with request.app.state.engine.begin() as conn:
            try:
                return db.register(conn, bytes(data), filename, fmt, meta, config, document_id)
            except KeyError as exc:
                raise HTTPException(404, "Документ для добавления версии не найден.") from exc

    return await run_in_threadpool(save)


@router.get("", summary="Последняя версия каждого документа")
def list_documents(request: Request, limit: Limit = 50, offset: Offset = 0) -> list[Document]:
    v = db.versions.c
    latest = select(v.document_id, func.max(v.document_version).label("n")).group_by(v.document_id).subquery()
    query = select(*db.public_fields).join(latest, and_(latest.c.document_id == v.document_id,
                                                       latest.c.n == v.document_version))
    with request.app.state.engine.connect() as conn:
        return conn.execute(query.order_by(v.document_id).limit(limit).offset(offset)).mappings().all()


@router.get("/{document_id}", summary="Метаданные и статус версии")
def detail(request: Request, document_id: UUID, document_version: Version = None) -> Document:
    with request.app.state.engine.connect() as conn:
        return version_or_404(conn, document_id, document_version)


@router.get("/{document_id}/original", summary="Скачать зарегистрированный оригинал", response_class=Response)
def original(request: Request, document_id: UUID, document_version: Version = None) -> Response:
    with request.app.state.engine.connect() as conn:
        row = version_or_404(conn, document_id, document_version)
        data = conn.scalar(select(db.versions.c.original).where(
            db.at_version(db.versions, document_id, row["document_version"])))
        return Response(data, media_type=MIME[row["format"]], headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["filename"], safe=""),
            "X-Content-Type-Options": "nosniff",
        })


@router.post("/{document_id}/reprocess", status_code=202, summary="Повторить обработку с сохранёнными параметрами")
def reprocess(request: Request, document_id: UUID, document_version: Version = None) -> Document:
    with request.app.state.engine.begin() as conn:
        row = version_or_404(conn, document_id, document_version)
        if row["status"] == "queued":
            return row
        number = row["document_version"]
        conn.execute(db.versions.update().where(db.at_version(db.versions, document_id, number))
                     .values(status="queued", error_code=None, page_count=0, chunk_count=0,
                             requirements_extracted=False))
        conn.execute(db.indexes.update().where(db.at_version(db.indexes, document_id, number),
                     db.indexes.c.status.in_(["indexed", "queued", "failed"]))
                     .values(status="queued", generation=uuid4(), indexed_count=0, error_code=None))
        return db.lookup(conn, document_id, number)


@router.get("/{document_id}/pages/{number}", summary="Извлечённый текст и координаты блоков страницы")
def page(request: Request, document_id: UUID, number: int, document_version: Version = None) -> dict:
    with request.app.state.engine.connect() as conn:
        row = version_or_404(conn, document_id, document_version)
        available(row)
        query = select(db.pages).where(db.at_version(db.pages, document_id, row["document_version"]),
                                       db.pages.c.number == number)
        found = conn.execute(query).mappings().first()
        if found is None:
            raise HTTPException(404, "Страница не найдена.")
        return dict(found)


@router.get("/{document_id}/chunks", summary="Фрагменты версии в порядке следования")
def list_chunks(request: Request, document_id: UUID, document_version: Version = None,
                limit: Limit = 50, offset: Offset = 0) -> list[Chunk]:
    with request.app.state.engine.connect() as conn:
        row = version_or_404(conn, document_id, document_version)
        available(row)
        query = select(db.chunks).where(db.at_version(db.chunks, document_id, row["document_version"]))
        model_version = conn.scalar(select(db.indexes.c.model_version).where(
            db.at_version(db.indexes, document_id, row["document_version"]), db.indexes.c.status == "indexed"))
        items = conn.execute(query.order_by(db.chunks.c.ordinal).limit(limit).offset(offset)).mappings()
        result = []
        for item in items:
            payload = dict(item["payload"])
            if payload["role"] == "leaf":
                payload["embedding_model_version"] = model_version
            result.append(Chunk(**payload, chunk_id=item["chunk_id"], document_id=document_id,
                          document_version=row["document_version"], page_start=item["page_start"],
                          ordinal=item["ordinal"], language=row["source_meta"]["language"]))
        return result


@router.get("/{document_id}/chunks/{chunk_id}/verify", summary="Проверить цитату по оригиналу версии")
def verify(request: Request, document_id: UUID, chunk_id: UUID, document_version: Version = None) -> dict:
    with request.app.state.engine.connect() as conn:
        row = version_or_404(conn, document_id, document_version)
        available(row)
        number = row["document_version"]
        chunk = conn.execute(select(db.chunks).where(db.at_version(db.chunks, document_id, number),
                                                     db.chunks.c.chunk_id == chunk_id)).mappings().first()
        if chunk is None:
            raise HTTPException(404, "Фрагмент не найден в данной версии.")
        data = conn.scalar(select(db.versions.c.original).where(db.at_version(db.versions, document_id, number)))
    first_page = chunk["page_start"]
    payload = chunk["payload"]
    verified = False
    if sha256(data).hexdigest() == row["content_hash"] and first_page == payload["page_end"]:
        extracted = extract_document(data, row["format"], max_pages=row["config"]["max_pages"],
                                     max_chars=row["config"]["max_chars"])
        if 1 <= first_page <= len(extracted):
            page_text = extracted[first_page - 1].text
            start, end = payload["char_start"], payload["char_end"]
            quote_text = page_text[start:end]
            verified = (0 <= start < end <= len(page_text) and quote_text == payload["text"]
                        and digest(quote_text) == payload["content_hash"])
    return {"verified": verified, "document_id": document_id, "document_version": number,
            "page": first_page, "chunk_id": chunk_id}
