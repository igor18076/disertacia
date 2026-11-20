"""Создать схему CourseGen."""

from alembic import op
from sqlalchemy.schema import CreateSchema, DropSchema

revision = "0001_infrastructure"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(CreateSchema("coursegen"))


def downgrade() -> None:
    op.execute(DropSchema("coursegen"))
