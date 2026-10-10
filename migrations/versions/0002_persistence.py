"""Persist consents, invites, notifications, payments, ledger; unseen counters on users.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    user_cols = {c['name'] for c in insp.get_columns('users')}

    with op.batch_alter_table('users') as batch:
        if 'unseen_friends' not in user_cols:
            batch.add_column(sa.Column('unseen_friends', sa.Integer(), nullable=False, server_default='0'))
        if 'unseen_partners' not in user_cols:
            batch.add_column(sa.Column('unseen_partners', sa.Integer(), nullable=False, server_default='0'))

    # Existing rows may have NULL balance/stars from the old schema
    op.execute("UPDATE users SET balance = 0 WHERE balance IS NULL")
    op.execute("UPDATE users SET stars = 0 WHERE stars IS NULL")

    tables = set(insp.get_table_names())

    if 'consents' not in tables:
        op.create_table(
            'consents',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('inviter_id', sa.String(50), sa.ForeignKey('users.user_id'), nullable=False),
            sa.Column('partner_id', sa.String(50), sa.ForeignKey('users.user_id'), nullable=False),
            sa.Column('kind', sa.String(30), nullable=False, server_default='intimate'),
            sa.Column('status', sa.String(20), nullable=False, server_default='active'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.Column('revoked_by', sa.String(50), nullable=True),
        )
        op.create_index('ix_consents_inviter_id', 'consents', ['inviter_id'])
        op.create_index('ix_consents_partner_id', 'consents', ['partner_id'])

    if 'invites' not in tables:
        op.create_table(
            'invites',
            sa.Column('token', sa.String(120), primary_key=True),
            sa.Column('inviter_id', sa.String(50), sa.ForeignKey('users.user_id'), nullable=False),
            sa.Column('kind', sa.String(20), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=True),
            sa.Column('used_by', sa.String(50), nullable=True),
            sa.Column('used_at', sa.DateTime(), nullable=True),
            sa.Column('decision', sa.String(20), nullable=True),
        )
        op.create_index('ix_invites_inviter_id', 'invites', ['inviter_id'])

    if 'notifications' not in tables:
        op.create_table(
            'notifications',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('user_id', sa.String(50), sa.ForeignKey('users.user_id'), nullable=False),
            sa.Column('type', sa.String(50), nullable=False),
            sa.Column('from_user', sa.String(50), nullable=True),
            sa.Column('message', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('delivered_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_notifications_user_id', 'notifications', ['user_id'])

    if 'payments' not in tables:
        op.create_table(
            'payments',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('charge_id', sa.String(120), nullable=False, unique=True),
            sa.Column('user_id', sa.String(50), sa.ForeignKey('users.user_id'), nullable=False),
            sa.Column('amount', sa.Integer(), nullable=False),
            sa.Column('currency', sa.String(10), nullable=False, server_default='XTR'),
            sa.Column('payload', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )
        op.create_index('ix_payments_user_id', 'payments', ['user_id'])

    if 'ledger' not in tables:
        op.create_table(
            'ledger',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('user_id', sa.String(50), sa.ForeignKey('users.user_id'), nullable=False),
            sa.Column('delta', sa.Integer(), nullable=False),
            sa.Column('reason', sa.String(50), nullable=False),
            sa.Column('ref', sa.String(120), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )
        op.create_index('ix_ledger_user_id', 'ledger', ['user_id'])


def downgrade() -> None:
    for name in ('ledger', 'payments', 'notifications', 'invites', 'consents'):
        op.drop_table(name)
    with op.batch_alter_table('users') as batch:
        batch.drop_column('unseen_partners')
        batch.drop_column('unseen_friends')
