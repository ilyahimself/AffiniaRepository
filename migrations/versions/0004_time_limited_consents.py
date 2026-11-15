"""Time-limited consents (expires_at, duration_hours) and optional per-record notes.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-15
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    consent_cols = {c['name'] for c in insp.get_columns('consents')}
    with op.batch_alter_table('consents') as batch:
        if 'expires_at' not in consent_cols:
            batch.add_column(sa.Column('expires_at', sa.DateTime(), nullable=True))
        if 'duration_hours' not in consent_cols:
            batch.add_column(sa.Column('duration_hours', sa.Integer(), nullable=True))
        if 'note' not in consent_cols:
            batch.add_column(sa.Column('note', sa.String(200), nullable=True))

    invite_cols = {c['name'] for c in insp.get_columns('invites')}
    with op.batch_alter_table('invites') as batch:
        if 'duration_hours' not in invite_cols:
            batch.add_column(sa.Column('duration_hours', sa.Integer(), nullable=True))
        if 'note' not in invite_cols:
            batch.add_column(sa.Column('note', sa.String(200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('invites') as batch:
        batch.drop_column('note')
        batch.drop_column('duration_hours')
    with op.batch_alter_table('consents') as batch:
        batch.drop_column('note')
        batch.drop_column('duration_hours')
        batch.drop_column('expires_at')
