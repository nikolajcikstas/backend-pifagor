"""add indexes for lesson search performance

Revision ID: a7d4e1f9c2b6
Revises: f3c1a9d2b7e4
Create Date: 2026-09-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a7d4e1f9c2b6'
down_revision: Union[str, Sequence[str], None] = 'f3c1a9d2b7e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index('ix_lessons_tutor_id', 'lessons', ['tutor_id'], if_not_exists=True)
    op.create_index('ix_lessons_child_id', 'lessons', ['child_id'], if_not_exists=True)
    op.create_index('ix_lessons_subject_id', 'lessons', ['subject_id'], if_not_exists=True)
    op.create_index('ix_lessons_date', 'lessons', ['date'], if_not_exists=True)
    op.create_index('ix_lessons_status', 'lessons', ['status'], if_not_exists=True)
    op.create_index('ix_users_first_name', 'users', ['first_name'], if_not_exists=True)
    op.create_index('ix_users_last_name', 'users', ['last_name'], if_not_exists=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_users_last_name', table_name='users', if_exists=True)
    op.drop_index('ix_users_first_name', table_name='users', if_exists=True)
    op.drop_index('ix_lessons_status', table_name='lessons', if_exists=True)
    op.drop_index('ix_lessons_date', table_name='lessons', if_exists=True)
    op.drop_index('ix_lessons_subject_id', table_name='lessons', if_exists=True)
    op.drop_index('ix_lessons_child_id', table_name='lessons', if_exists=True)
    op.drop_index('ix_lessons_tutor_id', table_name='lessons', if_exists=True)
