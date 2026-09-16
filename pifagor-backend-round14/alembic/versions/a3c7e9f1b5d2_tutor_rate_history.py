"""add tutor_rate_history table

Revision ID: a3c7e9f1b5d2
Revises: f2b8d4e6a9c1
Create Date: 2026-09-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3c7e9f1b5d2'
down_revision: Union[str, Sequence[str], None] = 'f2b8d4e6a9c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tutor_rate_history',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tutor_id', sa.Integer(), nullable=False),
        sa.Column('rate_per_hour', sa.Float(), nullable=False),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['tutor_id'], ['tutor_profiles.id']),
    )


def downgrade() -> None:
    op.drop_table('tutor_rate_history')
