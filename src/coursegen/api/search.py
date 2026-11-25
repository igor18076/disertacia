from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from qdrant_client.http.exceptions import ApiException
from sqlalchemy import select

from coursegen.api.documents import Version, version_or_404
from coursegen.documents import storage as db
from coursegen.retrieval.embedding import RetrievalError
from coursegen.retrieval.index import discrepancy, enqueue
from coursegen.retrieval.search import QdrantRetriever, RetrievedChunk, SearchRequest

router = APIRouter(prefix="/api/v1", tags=["Поиск"])


class Verification(BaseModel):
    expected: int
    actual: int
    missing: int
    unexpected: int
    mismatched: int


class IndexState(BaseModel):
    document_id: UUID
    document_version: int
    status: Literal["absent", "queued", "indexed", "removing", "failed", "remove_failed"]
    indexed_count: int
    generation: UUID | None = None
    model_version: str | None = None
    error_code: str | None = None
    verification: Verification | None = None


def schedule(request: Request, doc_id: UUID, version: int | None, *, remove: bool) -> dict:
    with request.app.state.engine.begin() as conn:
        source = version_or_404(conn, doc_id, version)
        number = source["document_version"]
        status = conn.scalar(select(db.versions.c.status).where(db.at_version(db.versions, doc_id, number))
                             .with_for_update())
        if not remove and status not in ("ready", "partial"):
            raise HTTPException(409, "Версия ещё не содержит доступных для поиска фрагментов.")
        return dict(enqueue(conn, doc_id, number, remove))


@router.post("/documents/{document_id}/index", status_code=202, response_model=IndexState,
             response_model_exclude_unset=True, summary="Поставить версию в очередь индексирования")
def index(request: Request, document_id: UUID, document_version: Version = None) -> dict:
    return schedule(request, document_id, document_version, remove=False)


@router.delete("/documents/{document_id}/index", status_code=202, response_model=IndexState,
               response_model_exclude_unset=True, summary="Удалить версию из поиска")
def remove_index(request: Request, document_id: UUID, document_version: Version = None) -> dict:
    return schedule(request, document_id, document_version, remove=True)


@router.get("/documents/{document_id}/index", response_model=IndexState, response_model_exclude_unset=True,
            summary="Получить состояние индекса и сверить фрагменты")
def index_status(request: Request, document_id: UUID, document_version: Version = None, verify: bool = False) -> dict:
    with request.app.state.engine.begin() as conn:
        source = version_or_404(conn, document_id, document_version)
        number = source["document_version"]
        query = select(db.indexes).where(db.at_version(db.indexes, document_id, number))
        row = conn.execute(query.with_for_update(read=True) if verify else query).mappings().first()
        result = (dict(row) if row is not None else
                  dict(document_id=document_id, document_version=number, status="absent", indexed_count=0))
        if verify:
            try:
                result["verification"] = discrepancy(conn, request.app.state.qdrant, result)
            except (ApiException, RetrievalError) as exc:
                raise unavailable(exc) from exc
        return result


def unavailable(exc: Exception) -> HTTPException:
    code = str(exc) if isinstance(exc, RetrievalError) else "qdrant_unavailable"
    return HTTPException(503, detail={"code": code, "message": "Поисковый индекс временно недоступен."})


@router.post("/search", summary="Найти фрагменты с указанием источников")
def search(request: Request, body: SearchRequest) -> list[RetrievedChunk]:
    state = request.app.state
    retriever = QdrantRetriever(state.engine, state.qdrant, state.settings, body.mode,
                                body.bm25_candidates, body.dense_candidates)
    try:
        return retriever.search(body.query, body.filters, body.top_k)
    except (ApiException, RetrievalError) as exc:
        raise unavailable(exc) from exc
