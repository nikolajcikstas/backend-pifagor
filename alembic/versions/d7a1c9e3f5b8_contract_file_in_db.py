"""store parent contract file bytes in DB (survives Render redeploys)

Revision ID: d7a1c9e3f5b8
Revises: c2f6a4e8b1d5
Create Date: 2026-09-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd7a1c9e3f5b8'
down_revision: Union[str, Sequence[str], None] = 'c2f6a4e8b1d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('parent_contracts', sa.Column('file_data', sa.LargeBinary(), nullable=True))
    op.add_column('parent_contracts', sa.Column('file_mime', sa.String(150), nullable=True))
    op.add_column('parent_contracts', sa.Column('file_name', sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column('parent_contracts', 'file_name')
    op.drop_column('parent_contracts', 'file_mime')
    op.drop_column('parent_contracts', 'file_data')
