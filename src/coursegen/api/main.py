import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request, Response, status
from pydantic import BaseModel
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ApiException
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from coursegen.core.config import Settings
from coursegen.db.connection import create_database_engine


logger = logging.getLogger(__name__)
CheckStatus = Literal["ok", "unavailable"]


class Health(BaseModel):
    status: Literal["ok"] = "ok"


class DependencyChecks(BaseModel):
    postgres: CheckStatus
    qdrant: CheckStatus


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: DependencyChecks


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    engine = create_database_engine(settings)
    client = QdrantClient(
        url=str(settings.qdrant_url),
        timeout=settings.probe_timeout_seconds,
        check_compatibility=False,
        trust_env=False,
    )
    app.state.engine = engine
    app.state.qdrant = client
    app.state.timeout = settings.probe_timeout_seconds
    try:
        yield
    finally:
        client.close()
        engine.dispose()


app = FastAPI(
    title="CourseGen Research",
    description="Диагностика API, PostgreSQL и Qdrant.",
    version="0.1.0",
    lifespan=lifespan,
)


def check_postgres(engine: Engine, timeout: int) -> bool:
    with engine.connect() as connection:
        connection.execute(
            text("SELECT set_config('statement_timeout', :value, true)"),
            {"value": f"{timeout}s"},
        )
        return bool(connection.scalar(text("SELECT has_schema_privilege('coursegen', 'USAGE')")))


def check_qdrant(client: QdrantClient) -> bool:
    client.get_collections()
    return True


async def probe(name: str, check: Callable[[], bool], timeout: int) -> CheckStatus:
    try:
        async with asyncio.timeout(timeout):
            available = await asyncio.to_thread(check)
        if available:
            return "ok"
        reason = "missing_schema_privilege"
    except (SQLAlchemyError, ApiException, TimeoutError) as exc:
        reason = type(exc).__name__
    logger.warning(
        json.dumps({"event": "dependency_unavailable", "dependency": name, "reason": reason})
    )
    return "unavailable"


@app.get("/health", summary="Проверить работу API")
async def health() -> Health:
    return Health()


@app.get(
    "/ready",
    summary="Проверить доступность PostgreSQL и Qdrant",
    responses={503: {"model": Readiness, "description": "Зависимость недоступна"}},
)
async def ready(request: Request, response: Response) -> Readiness:
    state = request.app.state
    postgres, qdrant = await asyncio.gather(
        probe("postgres", lambda: check_postgres(state.engine, state.timeout), state.timeout),
        probe("qdrant", lambda: check_qdrant(state.qdrant), state.timeout),
    )
    checks = DependencyChecks(postgres=postgres, qdrant=qdrant)
    if postgres == qdrant == "ok":
        return Readiness(status="ready", checks=checks)
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Readiness(status="not_ready", checks=checks)
