"""Состояние индекса каждой версии документа."""

from alembic import op

revision = "0003_search"
down_revision = "0002_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE coursegen.search_indexes (
            document_id uuid NOT NULL, document_version integer NOT NULL,
            generation uuid NOT NULL, status text NOT NULL, error_code text,
            indexed_count integer NOT NULL DEFAULT 0, model_version text NOT NULL,
            PRIMARY KEY (document_id, document_version),
            FOREIGN KEY (document_id, document_version) REFERENCES coursegen.document_versions
        )""")


def downgrade() -> None:
    op.drop_table("search_indexes", schema="coursegen")
