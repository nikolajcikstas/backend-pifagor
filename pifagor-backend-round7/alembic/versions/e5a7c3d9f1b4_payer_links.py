"""add payer_child_links and email_receipt_splits

Revision ID: e5a7c3d9f1b4
Revises: d1f3a8c5e7b2
Create Date: 2026-09-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5a7c3d9f1b4'
down_revision: Union[str, Sequence[str], None] = 'd1f3a8c5e7b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'payer_child_links',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('payer_name_normalized', sa.String(300), nullable=False),
        sa.Column('child_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['child_id'], ['child_profiles.id']),
    )
    op.create_index('ix_payer_child_links_payer_name_normalized', 'payer_child_links', ['payer_name_normalized'])

    op.create_table(
        'email_receipt_splits',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('receipt_id', sa.Integer(), nullable=False),
        sa.Column('child_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['receipt_id'], ['email_receipts.id']),
        sa.ForeignKeyConstraint(['child_id'], ['child_profiles.id']),
    )


def downgrade() -> None:
    op.drop_table('email_receipt_splits')
    op.drop_index('ix_payer_child_links_payer_name_normalized', table_name='payer_child_links')
    op.drop_table('payer_child_links')
