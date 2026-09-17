"""add performance indexes for CRM/contracts/payments dashboards

Revision ID: c2f6a4e8b1d5
Revises: b4d8f2a6c3e7
Create Date: 2026-09-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'c2f6a4e8b1d5'
down_revision: Union[str, Sequence[str], None] = 'b4d8f2a6c3e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEXES = [
    ("ix_parent_contracts_child_id", "parent_contracts", "child_id"),
    ("ix_parent_contracts_match_status", "parent_contracts", "match_status"),
    ("ix_parent_contract_children_contract_id", "parent_contract_children", "contract_id"),
    ("ix_parent_contract_children_child_id", "parent_contract_children", "child_id"),
    ("ix_email_receipts_child_id", "email_receipts", "child_id"),
    ("ix_email_receipt_splits_receipt_id", "email_receipt_splits", "receipt_id"),
    ("ix_email_receipt_splits_child_id", "email_receipt_splits", "child_id"),
    ("ix_child_profiles_crm_status", "child_profiles", "crm_status"),
    ("ix_parent_children_parent_id", "parent_children", "parent_id"),
    ("ix_parent_children_child_id", "parent_children", "child_id"),
    ("ix_tutor_rate_history_tutor_id", "tutor_rate_history", "tutor_id"),
    ("ix_tutor_payouts_tutor_id", "tutor_payouts", "tutor_id"),
    ("ix_users_role", "users", "role"),
]


def upgrade() -> None:
    for index_name, table_name, column_name in INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} ({column_name})")


def downgrade() -> None:
    for index_name, table_name, column_name in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")
