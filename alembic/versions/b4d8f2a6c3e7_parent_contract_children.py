"""add parent_contract_children table

Revision ID: b4d8f2a6c3e7
Revises: a3c7e9f1b5d2
Create Date: 2026-09-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b4d8f2a6c3e7'
down_revision: Union[str, Sequence[str], None] = 'a3c7e9f1b5d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'parent_contract_children',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('contract_id', sa.Integer(), nullable=False),
        sa.Column('child_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['contract_id'], ['parent_contracts.id']),
        sa.ForeignKeyConstraint(['child_id'], ['child_profiles.id']),
    )


def downgrade() -> None:
    op.drop_table('parent_contract_children')
