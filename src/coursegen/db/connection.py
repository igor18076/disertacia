from sqlalchemy import Engine, create_engine

from coursegen.core.config import Settings


def create_database_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url,
        connect_args={"connect_timeout": settings.probe_timeout_seconds},
        pool_timeout=settings.probe_timeout_seconds,
        hide_parameters=True,
    )
