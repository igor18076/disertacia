"""Обработчик очереди версий PostgreSQL."""

import logging
import time

from qdrant_client import QdrantClient
from sqlalchemy.exc import OperationalError

from coursegen.core.config import Settings
from coursegen.db.connection import create_database_engine
from coursegen.documents.storage import process_next
from coursegen.requirements.worker import extract_next
from coursegen.retrieval.index import process_index


def main() -> None:
    settings = Settings()
    engine = create_database_engine(settings)
    client = QdrantClient(url=str(settings.qdrant_url), timeout=60, check_compatibility=False, trust_env=False)
    try:
        while True:
            try:
                extracted = process_next(engine)
                requirements = extract_next(engine)
                if process_index(engine, client, settings) or extracted or requirements:
                    continue
            except OperationalError:
                logging.warning('{"event":"document_worker_database_unavailable"}')
            time.sleep(settings.worker_poll_seconds)
    finally:
        client.close()
        engine.dispose()


if __name__ == "__main__":
    main()
