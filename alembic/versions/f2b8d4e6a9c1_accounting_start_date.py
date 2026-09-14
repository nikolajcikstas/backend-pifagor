"""add accounting_start_date to child_profiles

Revision ID: f2b8d4e6a9c1
Revises: e5a7c3d9f1b4
Create Date: 2026-09-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f2b8d4e6a9c1'
down_revision: Union[str, Sequence[str], None] = 'e5a7c3d9f1b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('child_profiles', sa.Column('accounting_start_date', sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column('child_profiles', 'accounting_start_date')
