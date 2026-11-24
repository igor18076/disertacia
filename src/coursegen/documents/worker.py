"""Обработчик очереди версий PostgreSQL."""

import logging
import time

from sqlalchemy.exc import OperationalError

from coursegen.core.config import Settings
from coursegen.db.connection import create_database_engine
from coursegen.documents.storage import process_next


def main() -> None:
    settings = Settings()
    engine = create_database_engine(settings)
    try:
        while True:
            try:
                if process_next(engine):
                    continue
            except OperationalError:
                logging.warning('{"event":"document_worker_database_unavailable"}')
            time.sleep(settings.worker_poll_seconds)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
