import os
import time
from uuid import uuid4

import httpx2
import pytest


@pytest.fixture
def api():
    base = os.environ.get("API_BASE_URL", "http://api:8000")
    with httpx2.Client(base_url=base.rstrip("/") + "/api/v1/", timeout=30, trust_env=False) as client:
        yield client


def wait(api, path, condition):
    deadline = time.monotonic() + max(45, 4 * float(os.environ.get("WORKER_POLL_SECONDS", 1)))
    while time.monotonic() < deadline:
        response = api.get(path)
        response.raise_for_status()
        data = response.json()
        assert data.get("status") != "failed", data
        if condition(data):
            return data
        time.sleep(0.12)
    pytest.fail("Истекло время ожидания обработки")


def source(api, text, source_type):
    r = api.post("documents", params={"filename": f"test-{uuid4()}.txt", "source_type": source_type},
                 content=text.encode(), headers={"Content-Type": "text/plain"})
    r.raise_for_status()
    path = "documents/" + r.json()["document_id"]
    wait(api, path, lambda d: d["requirements_extracted"])
    api.post(path + "/index").raise_for_status()
    wait(api, path + "/index", lambda d: d["status"] == "indexed")
    return r.json()["document_id"]


def test_review_version_graph_and_search(api):
    doc = source(api, "Дисциплина: Базы данных\fТема: ROLLBACK\nОперация отменяет изменения.", "syllabus")
    other = source(api, "Критерий оценивания: объяснить ROLLBACK", "assessment")
    query = {"document_id": doc, "document_version": 1, "kind": "topic"}
    item, = api.get("requirements", params=query).json()
    key = item["requirement_id"]
    assert item["page"] == 2 and item["status"] == "needs_review"
    page = api.get(f"documents/{doc}/pages/2", params={"document_version": 1}).json()
    assert page["text"][item["char_start"]:item["char_end"]] == item["quote"]
    seed = {"discipline": "Базы данных", "requirement_ids": [key]}
    assert api.post("graph/from-requirements", json=seed).status_code == 422
    review = dict(status="approved", expected_revision=1, text="Откат транзакции")
    r = api.patch(f"requirements/{key}", json=review)
    assert r.json()["revision"] == 2 and r.json()["quote"] == item["quote"]
    assert api.patch(f"requirements/{key}", json=review).status_code == 409
    reprocess = api.post(f"documents/{doc}/reprocess")
    reprocess.raise_for_status()
    assert not reprocess.json()["requirements_extracted"]
    wait(api, f"documents/{doc}", lambda d: d["status"] == "ready" and d["requirements_extracted"])
    wait(api, f"documents/{doc}/index", lambda d: d["status"] == "indexed")
    assert api.get("requirements", params=query).json()[0]["status"] == "approved"
    version = api.post("graph/from-requirements", json=seed)
    assert version.status_code == 201, version.text
    graph = version.json()["graph"]
    gid = version.json()["graph_id"]
    foreign = api.get(f"documents/{other}/chunks").json()[0]["chunk_id"]
    wrong = dict(graph, edges=[dict(graph["edges"][0], origin="document", evidence_chunk_id=foreign)])
    assert api.put("graph/" + gid, json=dict(base_version=1, graph=wrong)).status_code == 422
    graph["nodes"].append(dict(id="tx", kind="concept", label="ROLLBACK"))
    graph["edges"].append(dict(source="tx", target=key, relation="prerequisite_of", origin="expert", basis="тест"))
    save = dict(base_version=1, graph=graph)
    assert api.put("graph/" + gid, json=save).status_code == 201
    assert api.put("graph/" + gid, json=save).status_code == 409
    assert api.get("concepts", params=dict(graph_id=gid, version=1)).json() == []
    assert api.get("concepts", params=dict(graph_id=gid, version=2)).json()[0]["id"] == "tx"
    check = api.get("graph/prerequisites", params=dict(graph_id=gid, version=2, concept_id=key)).json()
    assert [n["id"] for n in check["missing"]] == ["tx"]
    params = dict(query="ROLLBACK", mode="hybrid", filters={"allowed_document_ids": [doc, other]})
    def search():
        result = api.post("search", json=params)
        assert result.status_code == 200, result.text
        return result.json()
    assert {h["document_id"] for h in search()} == {doc, other}
    params.update(mode="hybrid_graph", graph=dict(graph_id=gid, version=2, concept_id=key, sources_only=True))
    hits = search()
    assert {h["document_id"] for h in hits} == {doc}
    assert hits[0]["graph"]["terms"] == ["Откат транзакции", "ROLLBACK"]
    params["filters"] = {"allowed_document_ids": [other]}
    assert search() == []
    params = dict(query="абракадабракси", mode="hybrid", filters={"allowed_document_ids": [doc]})
    assert all(hit["bm25_score"] is None for hit in search())
    params.update(mode="hybrid_graph", graph=dict(graph_id=gid, version=2, concept_id=key))
    assert any(hit["bm25_score"] is not None for hit in search())
