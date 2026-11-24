"""Сохранение версий оригиналов, страниц и фрагментов."""

from alembic import op

revision = "0002_documents"
down_revision = "0001_infrastructure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TABLE coursegen.documents (document_id uuid PRIMARY KEY)")
    op.execute("""
        CREATE TABLE coursegen.document_versions (
            document_id uuid REFERENCES coursegen.documents, document_version integer NOT NULL,
            filename text NOT NULL, format text NOT NULL, source_meta jsonb NOT NULL,
            content_hash text NOT NULL, identity_hash text NOT NULL, original bytea NOT NULL,
            config jsonb NOT NULL, status text NOT NULL, error_code text,
            created_at timestamptz NOT NULL DEFAULT now(),
            page_count integer NOT NULL DEFAULT 0, chunk_count integer NOT NULL DEFAULT 0,
            PRIMARY KEY (document_id, document_version), UNIQUE (document_id, identity_hash)
        )""")
    op.execute("""CREATE INDEX document_queue ON coursegen.document_versions (created_at)
                  WHERE status = 'queued'""")
    op.execute("""
        CREATE TABLE coursegen.document_pages (
            document_id uuid NOT NULL, document_version integer NOT NULL, number integer NOT NULL,
            text text NOT NULL, status text NOT NULL, blocks jsonb NOT NULL, warnings jsonb NOT NULL,
            PRIMARY KEY (document_id, document_version, number),
            FOREIGN KEY (document_id, document_version) REFERENCES coursegen.document_versions
        )""")
    op.execute("""
        CREATE TABLE coursegen.document_chunks (
            chunk_id uuid PRIMARY KEY, document_id uuid NOT NULL, document_version integer NOT NULL,
            ordinal integer NOT NULL, page_start integer NOT NULL, payload jsonb NOT NULL,
            UNIQUE (document_id, document_version, ordinal),
            FOREIGN KEY (document_id, document_version, page_start) REFERENCES coursegen.document_pages
        )""")


def downgrade() -> None:
    for table in ("document_chunks", "document_pages", "document_versions", "documents"):
        op.drop_table(table, schema="coursegen")
