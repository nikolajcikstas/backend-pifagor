"""add tutor_payouts table

Revision ID: f3c1a9d2b7e4
Revises: a29f838506aa
Create Date: 2026-09-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3c1a9d2b7e4'
down_revision: Union[str, Sequence[str], None] = 'a29f838506aa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'tutor_payouts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tutor_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('paid_at', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['tutor_id'], ['tutor_profiles.id']),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('tutor_payouts')
