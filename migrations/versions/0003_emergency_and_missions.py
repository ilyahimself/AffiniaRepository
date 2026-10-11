"""Emergency-contact flag on friend links; server-side mission completions.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    cols = {c['name'] for c in insp.get_columns('referral_friends')}
    if 'is_emergency' not in cols:
        with op.batch_alter_table('referral_friends') as batch:
            batch.add_column(sa.Column('is_emergency', sa.Boolean(), nullable=False, server_default=sa.false()))

    if 'mission_completions' not in set(insp.get_table_names()):
        op.create_table(
            'mission_completions',
            sa.Column('user_id', sa.String(50), sa.ForeignKey('users.user_id'), primary_key=True),
            sa.Column('mission_id', sa.String(40), primary_key=True),
            sa.Column('reward', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('completed_at', sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table('mission_completions')
    with op.batch_alter_table('referral_friends') as batch:
        batch.drop_column('is_emergency')
