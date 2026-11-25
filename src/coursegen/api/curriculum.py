from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field, StringConstraints
from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert

from coursegen.documents import storage as db
from coursegen.knowledge_graph.model import Edge, Graph, Name, Node, StrictModel
from coursegen.requirements.extraction import Kind

router = APIRouter(prefix="/api/v1", tags=["Требования и граф"])
Status = Literal["needs_review", "approved", "rejected"]
Positive = Annotated[int, Query(ge=1)]


class Requirement(StrictModel):
    requirement_id: UUID
    document_id: UUID
    document_version: int
    page: int
    char_start: int
    char_end: int
    quote: str
    kind: Kind
    text: str
    status: Status
    revision: int


class Review(StrictModel):
    expected_revision: int = Field(ge=1)
    status: Status
    kind: Kind | None = None
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32000,
                                           pattern=r"^[^\x00]+$")] | None = None


@router.get("/requirements", summary="Получить требования и ссылки на исходные страницы")
def requirements(request: Request, document_id: UUID | None = None, document_version: Positive | None = None,
                 status: Status | None = None, kind: Kind | None = None,
                 limit: Annotated[int, Query(ge=1, le=500)] = 100,
                 offset: Annotated[int, Query(ge=0)] = 0) -> list[Requirement]:
    table = db.requirements
    query = select(table)
    for field, value in dict(document_id=document_id, document_version=document_version, status=status, kind=kind).items():
        if value is not None:
            query = query.where(table.c[field] == value)
    with request.app.state.engine.connect() as conn:
        rows = conn.execute(query.order_by(table.c.document_id, table.c.document_version, table.c.page,
                                          table.c.char_start).limit(limit).offset(offset)).mappings()
        return [Requirement.model_validate(row) for row in rows]


@router.patch("/requirements/{requirement_id}", summary="Подтвердить, отклонить или исправить требование")
def review(request: Request, requirement_id: UUID, body: Review) -> Requirement:
    table = db.requirements
    changes = body.model_dump(exclude={"expected_revision"}, exclude_none=True)
    with request.app.state.engine.begin() as conn:
        row = conn.execute(table.update().where(table.c.requirement_id == requirement_id,
                           table.c.revision == body.expected_revision).values(**changes, revision=table.c.revision + 1)
                           .returning(*table.c)).mappings().first()
        if row is None:
            if conn.scalar(select(table.c.revision).where(table.c.requirement_id == requirement_id)) is None:
                raise HTTPException(404, "Требование не найдено.")
            raise HTTPException(409, "Требование уже изменено. Получите актуальную ревизию.")
        return Requirement.model_validate(row)


class SaveGraph(StrictModel):
    base_version: int = Field(ge=0)
    graph: Graph


class Revision(StrictModel):
    graph_id: UUID
    version: int
    graph: Graph


class FromRequirements(StrictModel):
    discipline: Name
    requirement_ids: list[UUID] = Field(min_length=1, max_length=200)


@router.post("/graph/from-requirements", status_code=201, summary="Создать граф из утверждённых требований")
def from_requirements(request: Request, body: FromRequirements) -> Revision:
    kinds = {"discipline": "topic", "section": "topic", "topic": "topic", "outcome": "outcome",
             "competency": "competency"}
    ids = set(body.requirement_ids)
    with request.app.state.engine.begin() as conn:
        rows = conn.execute(select(db.requirements).where(db.requirements.c.requirement_id.in_(ids),
                            db.requirements.c.status == "approved").order_by(db.requirements.c.requirement_id)
                            .with_for_update(read=True)).mappings().all()
        if len(rows) != len(ids) or any(r["kind"] not in kinds for r in rows):
            raise HTTPException(422, "Выберите утверждённые дисциплины, разделы, темы, компетенции или результаты.")
        nodes = {}
        edges = []
        for row in rows:
            key = str(row["requirement_id"])
            doc, version = row["document_id"], row["document_version"]
            source = f"{doc}@{version}"
            nodes[source] = Node(id=source, kind="source", label=source, document_id=doc, document_version=version)
            nodes[key] = Node(id=key, kind=kinds[row["kind"]], label=row["text"][:200],
                              requirement_id=row["requirement_id"], requirement_revision=row["revision"])
            edges.append(Edge(source=key, target=source, relation="supported_by", origin="rule",
                              basis=f"Утверждённое требование {key}, стр. {row['page']}"))
        graph = Graph(discipline=body.discipline, nodes=list(nodes.values()), edges=edges)
        graph_id = uuid4()
        conn.execute(db.graph_versions.insert().values(graph_id=graph_id, version=1,
                     snapshot=graph.model_dump(mode="json")))
        return Revision(graph_id=graph_id, version=1, graph=graph)


def read_graph(conn: Connection, graph_id: UUID, version: int | None) -> Revision:
    t = db.graph_versions
    query = select(t).where(t.c.graph_id == graph_id)
    if version is not None:
        query = query.where(t.c.version == version)
    row = conn.execute(query.order_by(t.c.version.desc()).limit(1)).first()
    if row is None:
        raise HTTPException(404, "Версия графа не найдена.")
    return Revision(graph_id=graph_id, version=row.version, graph=row.snapshot)


def verify_references(conn: Connection, graph: Graph) -> None:
    approved = {n.requirement_id for n in graph.nodes if n.requirement_id}
    if approved:
        t = db.requirements.c
        versions = dict(conn.execute(select(t.requirement_id, t.revision).where(
            t.requirement_id.in_(approved), t.status == "approved").with_for_update(read=True)).all())
        if any(versions.get(n.requirement_id) != n.requirement_revision for n in graph.nodes if n.requirement_id):
            raise HTTPException(422, "Укажите утверждённое требование и его действующую ревизию.")
    documents = {n.document_id for n in graph.nodes if n.document_id}
    if documents:
        v = db.versions.c
        found = set(conn.execute(select(v.document_id, v.document_version).where(v.document_id.in_(documents),
                                 v.status.in_(["ready", "partial"]))).all())
        if any((n.document_id, n.document_version) not in found for n in graph.nodes if n.document_id):
            raise HTTPException(422, "Версия источника недоступна.")
    chunks = {n.chunk_id for n in graph.nodes if n.chunk_id} | {e.evidence_chunk_id for e in graph.edges if e.evidence_chunk_id}
    if chunks:
        c = db.chunks.c
        refs = {key: (doc, ver) for key, doc, ver in conn.execute(select(c.chunk_id, c.document_id, c.document_version)
                                                                 .where(c.chunk_id.in_(chunks)))}
        if chunks != refs.keys() or any(refs[n.chunk_id] != (n.document_id, n.document_version)
                                        for n in graph.nodes if n.chunk_id):
            raise HTTPException(422, "Ссылка на фрагмент не соответствует источнику.")
        nodes = {node.id: node for node in graph.nodes}
        for edge in graph.edges:
            if edge.relation == "supported_by" and edge.evidence_chunk_id:
                target = nodes[edge.target]
                if (refs[edge.evidence_chunk_id] != (target.document_id, target.document_version)
                        or (target.chunk_id and edge.evidence_chunk_id != target.chunk_id)):
                    raise HTTPException(422, "Фрагмент основания не принадлежит указанному источнику связи.")


@router.put("/graph/{graph_id}", status_code=201, summary="Сохранить новую версию графа дисциплины")
def save_graph(request: Request, graph_id: UUID, body: SaveGraph) -> Revision:
    t = db.graph_versions
    with request.app.state.engine.begin() as conn:
        current = conn.scalar(select(t.c.version).where(t.c.graph_id == graph_id).order_by(t.c.version.desc()).limit(1))
        if body.base_version != (current or 0):
            raise HTTPException(409, "Базовая версия графа устарела.")
        verify_references(conn, body.graph)
        version = body.base_version + 1
        saved = conn.scalar(insert(t).values(graph_id=graph_id, version=version, snapshot=body.graph.model_dump(mode="json"))
                            .on_conflict_do_nothing().returning(t.c.version))
        if saved is None:
            raise HTTPException(409, "Версия графа уже опубликована.")
        return Revision(graph_id=graph_id, version=version, graph=body.graph)


@router.get("/graph", summary="Получить снимок графа")
def graph(request: Request, graph_id: UUID, version: Positive | None = None) -> Revision:
    with request.app.state.engine.connect() as conn:
        return read_graph(conn, graph_id, version)


@router.get("/concepts", summary="Получить понятия дисциплины")
def concepts(request: Request, graph_id: UUID, version: Positive | None = None) -> list[Node]:
    return [n for n in graph(request, graph_id, version).graph.nodes if n.kind == "concept"]


@router.get("/graph/prerequisites", summary="Найти пререквизиты и недостающие понятия")
def prerequisites(request: Request, graph_id: UUID, concept_id: str, version: Positive | None = None,
                  known: Annotated[list[str] | None, Query()] = None) -> dict:
    snapshot = graph(request, graph_id, version)
    try:
        required, missing, _ = snapshot.graph.context(concept_id, set(known or []))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return dict(graph_id=graph_id, version=snapshot.version, prerequisites=required, missing=missing)
