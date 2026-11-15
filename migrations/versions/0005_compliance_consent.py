"""Record 18+ Terms acceptance and explicit GDPR Art. 9(2)(a) consent timestamps on users.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-15
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    user_cols = {c['name'] for c in insp.get_columns('users')}

    with op.batch_alter_table('users') as batch:
        if 'terms_accepted_at' not in user_cols:
            batch.add_column(sa.Column('terms_accepted_at', sa.DateTime(), nullable=True))
        if 'art9_consent_at' not in user_cols:
            batch.add_column(sa.Column('art9_consent_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.drop_column('art9_consent_at')
        batch.drop_column('terms_accepted_at')
