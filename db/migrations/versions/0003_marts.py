"""mart layer for Power BI.

Revision ID: 0003
Revises: 0002
"""
from pathlib import Path

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"


def upgrade() -> None:
    op.execute((SQL_DIR / "003_marts.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("DROP SCHEMA mart CASCADE; CREATE SCHEMA mart;")
