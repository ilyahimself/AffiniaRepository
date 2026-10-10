"""Baseline: users, referral_friends, beloved_partners.

These tables already exist in production (created by Base.metadata.create_all),
so every step is skipped when the table is already there.

Revision ID: 0001
Revises:
Create Date: 2026-10-10
"""
from alembic import op
import sqlalchemy as sa

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def _existing_tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _existing_tables()

    if 'users' not in tables:
        op.create_table(
            'users',
            sa.Column('user_id', sa.String(50), primary_key=True),
            sa.Column('balance', sa.Integer(), nullable=True),
            sa.Column('stars', sa.Integer(), nullable=True),
            sa.Column('username', sa.String(100), nullable=True),
            sa.Column('first_name', sa.String(100), nullable=True),
            sa.Column('last_name', sa.String(100), nullable=True),
            sa.Column('language_code', sa.String(10), nullable=True),
            sa.Column('is_premium', sa.Boolean(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
        )

    if 'referral_friends' not in tables:
        op.create_table(
            'referral_friends',
            sa.Column('user_id', sa.String(50), sa.ForeignKey('users.user_id'), primary_key=True),
            sa.Column('friend_id', sa.String(50), sa.ForeignKey('users.user_id'), primary_key=True),
        )

    if 'beloved_partners' not in tables:
        op.create_table(
            'beloved_partners',
            sa.Column('user_id', sa.String(50), sa.ForeignKey('users.user_id'), primary_key=True),
            sa.Column('partner_id', sa.String(50), sa.ForeignKey('users.user_id'), primary_key=True),
        )


def downgrade() -> None:
    # Baseline is never rolled back: it describes pre-existing production tables.
    pass
