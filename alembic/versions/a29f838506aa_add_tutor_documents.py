"""add tutor_documents table

Revision ID: a29f838506aa
Revises: 6241aa1584ed
Create Date: 2026-07-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a29f838506aa'
down_revision: Union[str, Sequence[str], None] = '6241aa1584ed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'tutor_documents',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tutor_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('file_url', sa.String(length=500), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['tutor_id'], ['tutor_profiles.id']),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('tutor_documents')
