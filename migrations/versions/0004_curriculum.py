"""Требования к дисциплине и версии графа понятий."""

from alembic import op

revision = "0004_curriculum"
down_revision = "0003_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE coursegen.document_versions
        ADD COLUMN requirements_extracted boolean NOT NULL DEFAULT false
    """)
    op.execute("""CREATE INDEX requirements_queue ON coursegen.document_versions (created_at)
                  WHERE NOT requirements_extracted AND status IN ('ready', 'partial')
                  AND source_meta->>'source_type' IN ('syllabus', 'assessment')""")
    op.execute("""
        CREATE TABLE coursegen.requirements (
            requirement_id uuid PRIMARY KEY, document_id uuid NOT NULL, document_version integer NOT NULL,
            page integer NOT NULL, char_start integer NOT NULL, char_end integer NOT NULL,
            quote text NOT NULL, kind text NOT NULL, text text NOT NULL,
            status text NOT NULL, revision integer NOT NULL,
            FOREIGN KEY (document_id, document_version) REFERENCES coursegen.document_versions
        )
    """)
    op.execute("CREATE INDEX requirements_source ON coursegen.requirements (document_id, document_version, page)")
    op.execute("""
        CREATE TABLE coursegen.graph_versions (
            graph_id uuid NOT NULL, version integer NOT NULL, snapshot jsonb NOT NULL,
            PRIMARY KEY (graph_id, version)
        )
    """)


def downgrade() -> None:
    op.drop_table("graph_versions", schema="coursegen")
    op.drop_table("requirements", schema="coursegen")
    op.drop_column("document_versions", "requirements_extracted", schema="coursegen")
