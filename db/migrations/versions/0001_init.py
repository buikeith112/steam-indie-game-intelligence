"""Initial raw / ops / core schema.

Revision ID: 0001
Revises:
"""
from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"


def upgrade() -> None:
    op.execute((SQL_DIR / "001_init.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS mart CASCADE; DROP SCHEMA IF EXISTS core CASCADE; "
               "DROP SCHEMA IF EXISTS raw CASCADE; DROP SCHEMA IF EXISTS ops CASCADE;")
