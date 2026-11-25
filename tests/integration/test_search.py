import os
import runpy
import time
from contextlib import closing
from uuid import uuid4

import httpx2
import numpy as np
import pytest
from qdrant_client import QdrantClient

from coursegen.core.config import Settings
from coursegen.retrieval.embedding import encoder
from coursegen.retrieval.index import COLLECTION


@pytest.fixture
def api():
    base = os.environ.get("API_BASE_URL", "http://api:8000").rstrip("/")
    with httpx2.Client(base_url=base + "/api/v1/", timeout=30, trust_env=False) as client:
        yield client


def wait(api, path, status, version=1):
    deadline = time.monotonic() + max(60, 3 * float(os.environ.get("WORKER_POLL_SECONDS", 1)))
    while time.monotonic() < deadline:
        response = api.get(path, params={"document_version": version})
        response.raise_for_status()
        state = response.json()
        if state["status"] == status:
            return state
        assert state["status"] not in ("failed", "remove_failed"), state
        time.sleep(0.12)
    pytest.fail(f"Истекло время ожидания {status}")


def register(api, filename, content, **params):
    response = api.post("documents", params=dict(filename=filename, language="ru", topic="Базы данных",
                        currency="current", source_type="manual", **params),
                        content=content.encode(), headers={"Content-Type": "text/plain"})
    assert response.status_code == 202, response.text
    row = response.json()
    path = "documents/" + row["document_id"]
    wait(api, path, "ready", row["document_version"])
    return row, path


def search(api, mode, **filters):
    r = api.post("search", json=dict(query="ROLLBACK", mode=mode, filters=filters, top_k=2,
                                     bm25_candidates=3, dense_candidates=5))
    assert r.status_code == 200, r.text
    return r.json()


def test_real_cpu_embedding():
    model = encoder(Settings().embedding_model_dir)
    vector = model.encode("Операция ROLLBACK отменяет транзакцию.")
    assert len(vector) == 312 and np.isclose(np.linalg.norm(vector), 1)
    assert model.encode("Операция ROLLBACK отменяет транзакцию.") == vector
    assert not np.allclose(model.encode("Резервное копирование файлов"), vector)


def test_ranked_corpus(api):
    compare = runpy.run_path("scripts/benchmark_search.py")["compare"]
    report = compare(api)
    assert set(report) == {"bm25", "dense", "hybrid"}
    for metrics in report.values():
        assert len(metrics["queries"]) == 8 and 0 < metrics["recall_at_3"] <= 1
    lexical = {row["query"]: row["rank"] for row in report["bm25"]["queries"]}
    assert lexical["ROLLBACK"] == 1


def test_search_publication_filters_and_citations(api):
    name = f"retrieval-{uuid4()}.txt"
    row, doc = register(api, name, "Команда ROLLBACK отменяет изменения транзакции.")
    doc_id, index = row["document_id"], doc + "/index"
    scope = dict(allowed_document_ids=[doc_id])
    initial = api.get(index, params={"verify": "true"}).json()
    assert initial["status"] == "absent" and initial["verification"]["actual"] == 0
    assert not search(api, "dense", **scope)
    assert api.post(index).status_code == 202
    assert wait(api, index, "indexed")["indexed_count"] == 1
    for mode in ("bm25", "dense", "hybrid"):
        filters = dict(**scope, topic="Базы данных", currency="current", source_type="manual", document_version=1)
        hit, = search(api, mode, **filters)
        assert hit["rank"] == 1 and hit["mode"] == mode and hit["filters"] == filters
        assert hit["document_id"] == doc_id and hit["filename"] == name and hit["document_version"] == 1
        assert hit["page_start"] == hit["page_end"] == 1 and "ROLLBACK" in hit["text"]
        assert (hit["bm25_score"] is not None) == (mode != "dense")
        assert (hit["dense_score"] is not None) == (mode != "bm25")
        assert api.get(f"{doc}/chunks/{hit['chunk_id']}/verify", params={"document_version": 1}).json()["verified"]
    for restriction in [dict(topic="иная тема"), dict(currency="historical"), dict(source_type="textbook"),
                        dict(document_version=2)]:
        assert search(api, "hybrid", **scope, **restriction) == []
    assert search(api, "hybrid", allowed_document_ids=[]) == []
    register(api, name, "Новая редакция. ROLLBACK отменяет транзакцию.", document_id=doc_id)
    assert search(api, "dense", **scope) == []
    assert search(api, "dense", **scope, document_version=1)[0]["document_version"] == 1
    assert api.post(index).status_code == 202
    wait(api, index, "indexed", 2)
    assert search(api, "dense", **scope)[0]["document_version"] == 2


def test_partial_index_repair_and_removal(api):
    row, doc = register(api, f"index-repair-{uuid4()}.txt", "Служебный пример ROLLBACK для сверки.")
    index = doc + "/index"
    scope = dict(allowed_document_ids=[row["document_id"]])
    assert api.post(index).status_code == 202
    state = wait(api, index, "indexed")
    hit, = search(api, "bm25", **scope)
    # Удаляется только точка только что созданного синтетического документа.
    with closing(QdrantClient(url=str(Settings().qdrant_url), check_compatibility=False, trust_env=False)) as client:
        client.delete(COLLECTION, points_selector=[hit["chunk_id"]], wait=True)
    check = api.get(index, params={"verify": "true"}).json()["verification"]
    assert check == dict(expected=1, actual=0, missing=1, unexpected=0, mismatched=0)
    assert api.post(index).status_code == 202
    assert wait(api, index, "indexed")["generation"] != state["generation"]
    assert api.get(index, params={"verify": "true"}).json()["verification"] == {
        "expected": 1, "actual": 1, "missing": 0, "unexpected": 0, "mismatched": 0}
    assert search(api, "hybrid", **scope)[0]["chunk_id"] == hit["chunk_id"]
    assert api.post(doc + "/reprocess").status_code == 202
    wait(api, doc, "ready")
    wait(api, index, "indexed")
    assert search(api, "dense", **scope)[0]["chunk_id"] == hit["chunk_id"]
    assert api.delete(index).status_code == 202
    wait(api, index, "absent")
    assert api.get(index, params={"verify": "true"}).json()["verification"]["actual"] == 0
    assert search(api, "hybrid", **scope) == []
