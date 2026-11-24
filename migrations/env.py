from logging.config import fileConfig

from alembic import context

from coursegen.core.config import Settings
from coursegen.db.connection import create_database_engine
from coursegen.documents.storage import metadata


config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

def include_name(name, object_type, _parents):
    return object_type != "schema" or name == metadata.schema


def run_online() -> None:
    engine = create_database_engine(Settings())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection, target_metadata=metadata, version_table_schema="public",
                include_schemas=True, include_name=include_name,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    context.configure(
        dialect_name="postgresql",
        target_metadata=metadata,
        version_table_schema="public",
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    run_online()
