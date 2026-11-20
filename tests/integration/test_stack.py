import os
import socket

import httpx2
import pytest
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from coursegen.api.main import app
from coursegen.core.config import Settings
from coursegen.db.connection import create_database_engine


def test_running_api():
    settings = Settings()
    with httpx2.Client(
        base_url=os.environ.get("API_BASE_URL", "http://api:8000"),
        timeout=settings.probe_timeout_seconds + 5,
        trust_env=False,
    ) as client:
        health = client.get("/health")
        health.raise_for_status()
        assert health.json()["status"] == "ok"
        ready = client.get("/ready")
        ready.raise_for_status()
        assert ready.json() == {"status": "ready", "checks": {"postgres": "ok", "qdrant": "ok"}}


def test_database_matches_migration_head():
    expected = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
    assert expected
    engine = create_database_engine(Settings())
    try:
        with engine.connect() as connection:
            migration = MigrationContext.configure(connection, opts={"version_table_schema": "public"})
            assert set(migration.get_current_heads()) == set(expected)
            assert inspect(connection).has_schema("coursegen")
    finally:
        engine.dispose()


@pytest.mark.parametrize("dependency", ["postgres", "qdrant"])
def test_unavailable_dependency_preserves_liveness(monkeypatch, dependency):
    # Порт зарезервирован тестом и не принимает соединения.
    with socket.socket() as unused_port:
        unused_port.bind(("127.0.0.1", 0))
        port = unused_port.getsockname()[1]
        if dependency == "postgres":
            monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
            monkeypatch.setenv("POSTGRES_PORT", str(port))
        else:
            monkeypatch.setenv("QDRANT_URL", f"http://127.0.0.1:{port}")
        with TestClient(app) as client:
            ready = client.get("/ready")
            assert ready.status_code == 503
            expected = {"postgres": "ok", "qdrant": "ok"}
            expected[dependency] = "unavailable"
            assert ready.json()["checks"] == expected
            assert client.get("/health").status_code == 200
