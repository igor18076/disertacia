import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from qdrant_client.http.exceptions import ResponseHandlingException
from sqlalchemy.exc import OperationalError

from coursegen.api import main


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "unit-test-password")
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:6333")
    with TestClient(main.app) as client:
        yield client


def test_health_does_not_query_dependencies(client, monkeypatch):
    postgres = Mock(side_effect=AssertionError)
    qdrant = Mock(side_effect=AssertionError)
    monkeypatch.setattr(main, "check_postgres", postgres)
    monkeypatch.setattr(main, "check_qdrant", qdrant)

    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    postgres.assert_not_called()
    qdrant.assert_not_called()


@pytest.mark.parametrize(
    ("postgres_ok", "qdrant_ok"), [(True, True), (False, True), (True, False), (False, False)]
)
def test_readiness_reports_each_dependency(client, monkeypatch, caplog, postgres_ok, qdrant_ok):
    sensitive_message = "internal-connection-string"
    postgres = Mock(return_value=True)
    qdrant = Mock(return_value=True)
    if not postgres_ok:
        postgres.side_effect = OperationalError(None, None, Exception(sensitive_message))
    if not qdrant_ok:
        qdrant.side_effect = ResponseHandlingException(ConnectionError(sensitive_message))
    monkeypatch.setattr(main, "check_postgres", postgres)
    monkeypatch.setattr(main, "check_qdrant", qdrant)

    response = client.get("/ready")
    success = postgres_ok and qdrant_ok
    assert response.status_code == (200 if success else 503)
    assert response.json() == {
        "status": "ready" if success else "not_ready",
        "checks": {
            "postgres": "ok" if postgres_ok else "unavailable",
            "qdrant": "ok" if qdrant_ok else "unavailable",
        },
    }
    assert sensitive_message not in response.text
    assert sensitive_message not in caplog.text
    events = [json.loads(record.message) for record in caplog.records if record.name == main.__name__]
    assert all(event["event"] == "dependency_unavailable" for event in events)
    assert len(events) == 2 - int(postgres_ok) - int(qdrant_ok)
