"""add parsed contract fields to parent_contracts

Revision ID: c9e2b6f1a3d8
Revises: a7d4e1f9c2b6
Create Date: 2026-09-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9e2b6f1a3d8'
down_revision: Union[str, Sequence[str], None] = 'a7d4e1f9c2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('parent_contracts', 'parent_id', existing_type=sa.Integer(), nullable=True)
    op.alter_column('parent_contracts', 'child_id', existing_type=sa.Integer(), nullable=True)

    op.add_column('parent_contracts', sa.Column('contract_number', sa.String(50), nullable=True))
    op.add_column('parent_contracts', sa.Column('client_name_raw', sa.String(300), nullable=True))
    op.add_column('parent_contracts', sa.Column('start_date', sa.Date(), nullable=True))
    op.add_column('parent_contracts', sa.Column('end_date', sa.Date(), nullable=True))
    op.add_column('parent_contracts', sa.Column('total_amount', sa.Float(), nullable=True))
    op.add_column('parent_contracts', sa.Column('parent_full_name', sa.String(300), nullable=True))
    op.add_column('parent_contracts', sa.Column('parent_phone', sa.String(50), nullable=True))
    op.add_column('parent_contracts', sa.Column('parent_email', sa.String(200), nullable=True))
    op.add_column('parent_contracts', sa.Column('city', sa.String(200), nullable=True))
    op.add_column('parent_contracts', sa.Column('street', sa.String(200), nullable=True))
    op.add_column('parent_contracts', sa.Column('house', sa.String(50), nullable=True))
    op.add_column('parent_contracts', sa.Column('payments_json', sa.Text(), nullable=True))
    op.add_column('parent_contracts', sa.Column(
        'match_status', sa.String(20), nullable=False, server_default='unmatched'
    ))
    op.add_column('parent_contracts', sa.Column(
        'needs_review', sa.Boolean(), nullable=False, server_default=sa.false()
    ))
    op.add_column('parent_contracts', sa.Column('recommendation', sa.Text(), nullable=True))
    op.add_column('parent_contracts', sa.Column('recommendation_as_of', sa.Date(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    for col in (
        'recommendation_as_of', 'recommendation', 'needs_review', 'match_status',
        'payments_json', 'house', 'street', 'city', 'parent_email', 'parent_phone',
        'parent_full_name', 'total_amount', 'end_date', 'start_date',
        'client_name_raw', 'contract_number',
    ):
        op.drop_column('parent_contracts', col)
    op.alter_column('parent_contracts', 'child_id', existing_type=sa.Integer(), nullable=False)
    op.alter_column('parent_contracts', 'parent_id', existing_type=sa.Integer(), nullable=False)
