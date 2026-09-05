"""add payment_mode to parent_contracts

Revision ID: d1f3a8c5e7b2
Revises: c9e2b6f1a3d8
Create Date: 2026-09-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1f3a8c5e7b2'
down_revision: Union[str, Sequence[str], None] = 'c9e2b6f1a3d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'parent_contracts',
        sa.Column('payment_mode', sa.String(20), nullable=False, server_default='unknown'),
    )


def downgrade() -> None:
    op.drop_column('parent_contracts', 'payment_mode')
