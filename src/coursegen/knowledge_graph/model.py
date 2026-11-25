from typing import Annotated, Literal, Self
from uuid import UUID

import networkx as nx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^\x00]+$")]
NodeKind = Literal["topic", "concept", "outcome", "competency", "source", "chunk"]
Origin = Literal["document", "rule", "llm", "expert"]
Relation = Literal["prerequisite_of", "part_of", "related_to", "covers", "supported_by"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Node(StrictModel):
    id: Name
    kind: NodeKind
    label: Name
    requirement_id: UUID | None = None
    requirement_revision: int | None = Field(None, ge=1)
    document_id: UUID | None = None
    document_version: int | None = Field(None, ge=1)
    chunk_id: UUID | None = None

    @model_validator(mode="after")
    def check_reference(self) -> Self:
        source = self.kind in ("source", "chunk")
        if (self.requirement_id is None) != (self.requirement_revision is None):
            raise ValueError("Для требования нужны идентификатор и ревизия подтверждения.")
        if ((self.document_id is not None) != source or (self.document_version is not None) != source
                or (self.chunk_id is not None) != (self.kind == "chunk")):
            raise ValueError("Для источника требуется документ и версия, для фрагмента также chunk_id.")
        return self


class Edge(StrictModel):
    source: Name
    target: Name
    relation: Relation
    origin: Origin
    basis: Name  # Правило, эксперт, предложение модели или пояснение к ссылке.
    evidence_chunk_id: UUID | None = None


class Graph(StrictModel):
    discipline: Name
    nodes: list[Node] = Field(max_length=2000)
    edges: list[Edge] = Field(max_length=10000)

    @model_validator(mode="after")
    def check_edges(self) -> Self:
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("Идентификаторы узлов должны быть уникальными.")
        seen = set()
        for edge in self.edges:
            key = (edge.source, edge.target, edge.relation)
            if key in seen or edge.source not in nodes or edge.target not in nodes or edge.source == edge.target:
                raise ValueError("Связь повторяется, замкнута на себя или ссылается на неизвестный узел.")
            seen.add(key)
            if edge.origin == "document" and edge.evidence_chunk_id is None:
                raise ValueError("Для связи из документа требуется ссылка на фрагмент.")
            if edge.relation == "supported_by" and nodes[edge.target].kind not in ("source", "chunk"):
                raise ValueError("Подтверждение должно указывать на источник или фрагмент.")
            if edge.relation == "prerequisite_of" and any(nodes[k].kind not in ("concept", "topic")
                                                         for k in (edge.source, edge.target)):
                raise ValueError("Пререквизиты связывают темы и понятия.")
        dag = self.prerequisites_graph(include_proposals=True)
        if not nx.is_directed_acyclic_graph(dag):
            cycle = nx.find_cycle(dag)
            raise ValueError("Цикл пререквизитов: " + " → ".join([a for a, _ in cycle] + [cycle[0][0]]))
        return self

    def prerequisites_graph(self, *, include_proposals: bool = False) -> nx.DiGraph:
        graph = nx.DiGraph()
        graph.add_nodes_from(n.id for n in self.nodes)
        graph.add_edges_from((e.source, e.target) for e in self.edges
                             if e.relation == "prerequisite_of" and (include_proposals or e.origin != "llm"))
        return graph

    def context(self, concept_id: str, known: set[str]) -> tuple[list[Node], list[Node], list[Node]]:
        nodes = {node.id: node for node in self.nodes}
        if concept_id not in nodes or nodes[concept_id].kind not in ("concept", "topic") or known - nodes.keys():
            raise ValueError("Проверьте идентификаторы понятий для выбранной версии графа.")
        dag = self.prerequisites_graph()
        required = nx.ancestors(dag, concept_id)
        ordered = [nodes[n] for n in nx.lexicographical_topological_sort(dag.subgraph(required))]
        relevant = required | {concept_id}
        support = {e.target for e in self.edges if e.source in relevant and e.relation == "supported_by"
                   and e.origin != "llm"}
        return ordered, [n for n in ordered if n.id not in known], [nodes[n] for n in sorted(support)]
