from uuid import uuid4

import pytest
from pydantic import ValidationError

from coursegen.knowledge_graph.model import Graph
from coursegen.requirements.extraction import candidates


def test_extract_normalize_and_preserve_evidence():
    doc = uuid4()
    text = ("  Дисциплина: Базы   данных\nРаздел 2. Хранение\nТема: Индексы\nКомпетенции:\n- ПК-1\n"
            "Уметь: применять SQL\nВид занятия: лекция\nСодержание: транзакции\nОбъём: 36 часов\n"
            "Практическая работа: проект\nКритерий оценивания: корректность\nСвободная формулировка")
    rows = list(candidates(doc, 1, 2, text))
    assert [r["kind"] for r in rows] == ["discipline", "section", "topic", "competency", "outcome", "activity",
                                         "content", "workload", "practical", "assessment", "other"]
    assert rows[0]["text"] == "Базы данных"
    for row in rows:
        assert text[row["char_start"]:row["char_end"]] == row["quote"]
        assert row["status"] == "needs_review" and row["page"] == 2
    assert rows == list(candidates(doc, 1, 2, text))
    assert rows[0]["requirement_id"] != next(candidates(doc, 2, 2, text))["requirement_id"]


def test_prerequisites_and_cycle():
    nodes = [dict(id=k, kind="concept", label=k) for k in "abcd"]
    def edge(a, b, origin="expert"):
        return dict(source=a, target=b, origin=origin, basis="проверка", relation="prerequisite_of")
    edges = [edge("a", "b"), edge("b", "c"), edge("d", "c", "llm")]
    graph = Graph(discipline="Курс", nodes=nodes, edges=edges)
    required, missing, _ = graph.context("c", {"a"})
    assert [n.id for n in required] == ["a", "b"]
    assert [n.id for n in missing] == ["b"]
    with pytest.raises(ValidationError, match="Цикл"):
        Graph(discipline="Курс", nodes=nodes, edges=[*edges, edge("c", "a")])
    with pytest.raises(ValidationError, match="Цикл"):
        Graph(discipline="Курс", nodes=nodes, edges=[*edges, edge("c", "a", "llm")])
    with pytest.raises(ValidationError):
        Graph(discipline="Курс", nodes=nodes, edges=[edge("unknown", "a")])
