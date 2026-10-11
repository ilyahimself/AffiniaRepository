"""
Database Service for Affinia
Handles PostgreSQL database operations using SQLAlchemy.

PostgreSQL is the single source of truth. The in-memory dicts in app.py are a
read cache that is rebuilt from `load_snapshot()` on startup; every mutation
must go through a method here (write-through).
"""

import os
import logging
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Boolean, DateTime, Text,
    ForeignKey, Table, select, update, text, func,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker, scoped_session, relationship
from sqlalchemy.pool import QueuePool

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Naive UTC timestamp (columns are timezone-naive)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Placeholder that replaces an erased user's id in records shared with other people
# (their consent history, payment records). Not a real Telegram id, so it can never log in.
DELETED_USER_ID = 'deleted'


class Base(DeclarativeBase):
    pass


# Association tables for many-to-many relationships
referral_friends = Table(
    'referral_friends',
    Base.metadata,
    Column('user_id', String(50), ForeignKey('users.user_id'), primary_key=True),
    Column('friend_id', String(50), ForeignKey('users.user_id'), primary_key=True),
    # user_id has marked friend_id as an emergency contact (one direction only)
    Column('is_emergency', Boolean, nullable=False, default=False, server_default='0'),
)

beloved_partners = Table(
    'beloved_partners',
    Base.metadata,
    Column('user_id', String(50), ForeignKey('users.user_id'), primary_key=True),
    Column('partner_id', String(50), ForeignKey('users.user_id'), primary_key=True)
)


class User(Base):
    """User model storing tokens, profile info, and relationships"""
    __tablename__ = 'users'

    user_id = Column(String(50), primary_key=True)
    balance = Column(Integer, default=0, nullable=False)
    stars = Column(Integer, default=0, nullable=False)

    # Profile info
    username = Column(String(100), nullable=True)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    language_code = Column(String(10), nullable=True)
    is_premium = Column(Boolean, default=False)

    # Unseen counters shown as badges in the Mini App
    unseen_friends = Column(Integer, default=0, nullable=False)
    unseen_partners = Column(Integer, default=0, nullable=False)

    # Timestamps
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    # Relationships
    friends = relationship(
        'User',
        secondary=referral_friends,
        primaryjoin=user_id == referral_friends.c.user_id,
        secondaryjoin=user_id == referral_friends.c.friend_id,
        backref='referred_by'
    )

    partners = relationship(
        'User',
        secondary=beloved_partners,
        primaryjoin=user_id == beloved_partners.c.user_id,
        secondaryjoin=user_id == beloved_partners.c.partner_id,
        backref='partner_of'
    )


class Consent(Base):
    """An accepted consent between two users. One row per pair."""
    __tablename__ = 'consents'

    id = Column(Integer, primary_key=True, autoincrement=True)
    inviter_id = Column(String(50), ForeignKey('users.user_id'), nullable=False, index=True)
    partner_id = Column(String(50), ForeignKey('users.user_id'), nullable=False, index=True)
    kind = Column(String(30), default='intimate', nullable=False)
    status = Column(String(20), default='active', nullable=False)  # active | revoked
    created_at = Column(DateTime, default=utcnow, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    revoked_by = Column(String(50), nullable=True)


class Invite(Base):
    """Invitation link issued by a user (friend or consent)."""
    __tablename__ = 'invites'

    token = Column(String(120), primary_key=True)
    inviter_id = Column(String(50), ForeignKey('users.user_id'), nullable=False, index=True)
    kind = Column(String(20), nullable=False)  # friend | consent
    created_at = Column(DateTime, default=utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    used_by = Column(String(50), nullable=True)
    used_at = Column(DateTime, nullable=True)
    decision = Column(String(20), nullable=True)  # accept | decline


class Notification(Base):
    """In-app notification for a user; delivered once via /get_notifications."""
    __tablename__ = 'notifications'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(50), ForeignKey('users.user_id'), nullable=False, index=True)
    type = Column(String(50), nullable=False)
    from_user = Column(String(50), nullable=True)
    message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    delivered_at = Column(DateTime, nullable=True)


class Payment(Base):
    """Telegram Stars payment, keyed by Telegram's charge id (idempotent)."""
    __tablename__ = 'payments'

    id = Column(Integer, primary_key=True, autoincrement=True)
    charge_id = Column(String(120), unique=True, nullable=False)
    user_id = Column(String(50), ForeignKey('users.user_id'), nullable=False, index=True)
    amount = Column(Integer, nullable=False)
    currency = Column(String(10), nullable=False, default='XTR')
    payload = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class LedgerEntry(Base):
    """Every AFN balance change, for audit and idempotent rewards."""
    __tablename__ = 'ledger'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(50), ForeignKey('users.user_id'), nullable=False, index=True)
    delta = Column(Integer, nullable=False)
    reason = Column(String(50), nullable=False)
    ref = Column(String(120), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class MissionCompletion(Base):
    """A mission completed by a user; the primary key makes the reward idempotent."""
    __tablename__ = 'mission_completions'

    user_id = Column(String(50), ForeignKey('users.user_id'), primary_key=True)
    mission_id = Column(String(40), primary_key=True)
    reward = Column(Integer, nullable=False, default=0)
    completed_at = Column(DateTime, default=utcnow, nullable=False)


def _ts(dt: Optional[datetime]) -> int:
    if dt is None:
        return 0
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


class DatabaseService:
    """Singleton service for database operations"""

    _engine = None
    _session_factory = None
    _initialized = False

    @classmethod
    def initialize(cls, database_url: Optional[str] = None, create_tables: bool = False) -> bool:
        """Initialize database connection.

        Schema is managed by Alembic (release phase on Heroku). `create_tables`
        is only for tests / local sqlite.
        """
        if cls._initialized:
            return True

        if database_url is None:
            database_url = os.getenv('HEROKU_POSTGRESQL_COBALT_URL') or os.getenv('DATABASE_URL')

        if not database_url:
            logger.error("❌ No database URL provided!")
            return False

        # Fix postgres:// to postgresql:// for SQLAlchemy 2.0
        if database_url.startswith('postgres://'):
            database_url = database_url.replace('postgres://', 'postgresql://', 1)

        try:
            logger.info("🔌 Connecting to database...")
            engine_kwargs: Dict[str, Any] = {'pool_pre_ping': True, 'echo': False}
            if not database_url.startswith('sqlite'):
                engine_kwargs.update(poolclass=QueuePool, pool_size=5, max_overflow=10)
            cls._engine = create_engine(database_url, **engine_kwargs)
            # expire_on_commit=False: объекты возвращаются наружу после commit/close, их атрибуты должны читаться
            cls._session_factory = scoped_session(sessionmaker(bind=cls._engine, expire_on_commit=False))

            if create_tables:
                Base.metadata.create_all(cls._engine)

            # Fail fast if the connection is broken
            with cls._engine.connect() as conn:
                conn.execute(text("SELECT 1"))

            cls._initialized = True
            logger.info("✅ Database connected successfully!")
            return True

        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            cls._engine = None
            cls._session_factory = None
            return False

    @classmethod
    def reset(cls):
        """Dispose engine (tests)."""
        if cls._session_factory:
            cls._session_factory.remove()
        if cls._engine:
            cls._engine.dispose()
        cls._engine = None
        cls._session_factory = None
        cls._initialized = False

    @classmethod
    def get_session(cls):
        """Get a database session"""
        if not cls._initialized:
            raise RuntimeError("Database not initialized! Call DatabaseService.initialize() first.")
        return cls._session_factory()

    @classmethod
    def close_session(cls):
        """Close the current session"""
        if cls._session_factory:
            cls._session_factory.remove()

    # ===== User Operations =====

    @classmethod
    def get_user(cls, user_id: str) -> Optional[User]:
        """Get user by ID"""
        session = cls.get_session()
        try:
            return session.get(User, user_id)
        finally:
            cls.close_session()

    @classmethod
    def _get_or_create(cls, session, user_id: str) -> User:
        user = session.get(User, user_id)
        if not user:
            user = User(user_id=user_id, balance=0, stars=0, unseen_friends=0, unseen_partners=0)
            session.add(user)
            session.flush()
        return user

    @classmethod
    def ensure_user(cls, user_id: str) -> tuple[User, bool]:
        """Ensure user exists, create if not. Returns (user, was_created)"""
        session = cls.get_session()
        try:
            user = session.get(User, user_id)
            if user:
                return user, False

            user = User(user_id=user_id, balance=0, stars=0, unseen_friends=0, unseen_partners=0)
            session.add(user)
            session.commit()
            logger.info(f"🆕 NEW USER: {user_id}")
            return user, True
        except Exception as e:
            session.rollback()
            logger.error(f"Error ensuring user {user_id}: {e}")
            raise
        finally:
            cls.close_session()

    @classmethod
    def get_balance(cls, user_id: str) -> int:
        """Get user balance"""
        user = cls.get_user(user_id)
        return user.balance if user else 0

    @classmethod
    def add_balance(cls, user_id: str, delta: int, reason: str, ref: Optional[str] = None) -> int:
        """Atomically change balance and write a ledger entry. Returns new balance."""
        session = cls.get_session()
        try:
            cls._get_or_create(session, user_id)
            session.execute(
                update(User).where(User.user_id == user_id).values(balance=User.balance + delta)
            )
            session.add(LedgerEntry(user_id=user_id, delta=delta, reason=reason, ref=ref))
            session.commit()
            return session.execute(select(User.balance).where(User.user_id == user_id)).scalar_one()
        except Exception as e:
            session.rollback()
            logger.error(f"Error adding balance for {user_id}: {e}")
            raise
        finally:
            cls.close_session()

    @classmethod
    def set_balance(cls, user_id: str, balance: int) -> int:
        """Set user balance to exact value (admin/repair). Returns new balance."""
        session = cls.get_session()
        try:
            user = cls._get_or_create(session, user_id)
            old = user.balance or 0
            user.balance = balance
            session.add(LedgerEntry(user_id=user_id, delta=balance - old, reason='set_balance'))
            session.commit()
            return user.balance
        except Exception as e:
            session.rollback()
            logger.error(f"Error setting balance for {user_id}: {e}")
            raise
        finally:
            cls.close_session()

    # ===== Profile Operations =====

    @classmethod
    def update_profile(cls, user_id: str, username: str = None, first_name: str = None,
                       last_name: str = None, language_code: str = None, is_premium: bool = None) -> User:
        """Update user profile info"""
        session = cls.get_session()
        try:
            user = cls._get_or_create(session, user_id)

            if username is not None:
                user.username = username
            if first_name is not None:
                user.first_name = first_name
            if last_name is not None:
                user.last_name = last_name
            if language_code is not None:
                user.language_code = language_code
            if is_premium is not None:
                user.is_premium = is_premium

            session.commit()
            logger.info(f"👤 Updated profile for {user_id}: @{username or 'unknown'}")
            return user
        except Exception as e:
            session.rollback()
            logger.error(f"Error updating profile for {user_id}: {e}")
            raise
        finally:
            cls.close_session()

    @classmethod
    def get_display_name(cls, user_id: str) -> str:
        """Get display name for user (username or first/last name)"""
        user = cls.get_user(user_id)
        if not user:
            return f"User {user_id}"

        if user.username:
            return f"@{user.username} (ID: {user_id})"
        elif user.first_name or user.last_name:
            full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
            return f"{full_name} (ID: {user_id})"
        return f"User {user_id}"

    # ===== Friends/Referrals Operations =====

    @classmethod
    def add_friend(cls, user_id: str, friend_id: str) -> bool:
        """Add friend relationship (bidirectional). Returns True if a new link was created."""
        session = cls.get_session()
        try:
            user = cls._get_or_create(session, user_id)
            friend = cls._get_or_create(session, friend_id)

            created = False
            if friend not in user.friends:
                user.friends.append(friend)
                created = True
            if user not in friend.friends:
                friend.friends.append(user)
                created = True

            session.commit()
            return created
        except Exception as e:
            session.rollback()
            logger.error(f"Error adding friend: {e}")
            return False
        finally:
            cls.close_session()

    @classmethod
    def get_friends(cls, user_id: str) -> List[str]:
        """Get list of friend user IDs"""
        session = cls.get_session()
        try:
            user = session.get(User, user_id)
            if not user:
                return []
            return [f.user_id for f in user.friends]
        finally:
            cls.close_session()

    @classmethod
    def remove_friend(cls, user_id: str, friend_id: str) -> bool:
        """Remove friend relationship"""
        session = cls.get_session()
        try:
            user = session.get(User, user_id)
            friend = session.get(User, friend_id)

            if not user or not friend:
                return False

            if friend in user.friends:
                user.friends.remove(friend)
            if user in friend.friends:
                friend.friends.remove(user)

            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error removing friend: {e}")
            return False
        finally:
            cls.close_session()

    # ===== Emergency contacts (a flag on the user's own friend link) =====

    @classmethod
    def set_emergency_contact(cls, user_id: str, friend_id: str, enabled: bool) -> bool:
        """Mark/unmark a friend as an emergency contact. Returns False if they are not friends."""
        session = cls.get_session()
        try:
            result = session.execute(
                referral_friends.update()
                .where((referral_friends.c.user_id == user_id) & (referral_friends.c.friend_id == friend_id))
                .values(is_emergency=bool(enabled))
            )
            session.commit()
            return result.rowcount > 0
        except Exception as e:
            session.rollback()
            logger.error(f"Error setting emergency contact: {e}")
            return False
        finally:
            cls.close_session()

    @classmethod
    def get_emergency_contacts(cls, user_id: str) -> List[str]:
        """Friend ids the user has marked as emergency contacts."""
        session = cls.get_session()
        try:
            rows = session.execute(
                select(referral_friends.c.friend_id).where(
                    (referral_friends.c.user_id == user_id) & (referral_friends.c.is_emergency.is_(True)))
            ).all()
            return [fid for (fid,) in rows]
        finally:
            cls.close_session()

    # ===== Missions (server-side, idempotent rewards) =====

    @classmethod
    def get_completed_missions(cls, user_id: str) -> List[str]:
        session = cls.get_session()
        try:
            rows = session.execute(
                select(MissionCompletion.mission_id).where(MissionCompletion.user_id == user_id)).all()
            return [m for (m,) in rows]
        finally:
            cls.close_session()

    @classmethod
    def complete_mission(cls, user_id: str, mission_id: str, reward: int) -> Optional[int]:
        """Record a mission completion and credit the reward once.

        Returns the new balance, or None if the mission was already completed.
        """
        session = cls.get_session()
        try:
            cls._get_or_create(session, user_id)
            exists = session.execute(select(MissionCompletion.mission_id).where(
                (MissionCompletion.user_id == user_id) & (MissionCompletion.mission_id == mission_id))).first()
            if exists:
                return None
            session.add(MissionCompletion(user_id=user_id, mission_id=mission_id, reward=reward))
            if reward:
                session.execute(update(User).where(User.user_id == user_id).values(balance=User.balance + reward))
                session.add(LedgerEntry(user_id=user_id, delta=reward, reason='mission', ref=mission_id))
            session.commit()
            return session.execute(select(User.balance).where(User.user_id == user_id)).scalar_one()
        except Exception as e:
            session.rollback()
            logger.error(f"Error completing mission {mission_id}: {e}")
            raise
        finally:
            cls.close_session()

    @classmethod
    def count_payments(cls, user_id: str) -> int:
        session = cls.get_session()
        try:
            return session.execute(
                select(func.count()).select_from(Payment).where(Payment.user_id == user_id)).scalar_one()
        finally:
            cls.close_session()

    # ===== Partners/Beloveds Operations =====

    @classmethod
    def add_partner(cls, user_id: str, partner_id: str) -> bool:
        """Add partner relationship (bidirectional). Returns True if a new link was created."""
        session = cls.get_session()
        try:
            user = cls._get_or_create(session, user_id)
            partner = cls._get_or_create(session, partner_id)

            created = False
            if partner not in user.partners:
                user.partners.append(partner)
                created = True
            if user not in partner.partners:
                partner.partners.append(user)
                created = True

            session.commit()
            return created
        except Exception as e:
            session.rollback()
            logger.error(f"Error adding partner: {e}")
            return False
        finally:
            cls.close_session()

    @classmethod
    def get_partners(cls, user_id: str) -> List[str]:
        """Get list of partner user IDs"""
        session = cls.get_session()
        try:
            user = session.get(User, user_id)
            if not user:
                return []
            return [p.user_id for p in user.partners]
        finally:
            cls.close_session()

    @classmethod
    def remove_partner(cls, user_id: str, partner_id: str) -> bool:
        """Remove partner relationship"""
        session = cls.get_session()
        try:
            user = session.get(User, user_id)
            partner = session.get(User, partner_id)

            if not user or not partner:
                return False

            if partner in user.partners:
                user.partners.remove(partner)
            if user in partner.partners:
                partner.partners.remove(user)

            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error removing partner: {e}")
            return False
        finally:
            cls.close_session()

    # ===== Consents =====

    @classmethod
    def add_consent(cls, inviter_id: str, partner_id: str, kind: str = 'intimate') -> Dict[str, Any]:
        """Record an accepted consent. Returns dict in the app's consent_history shape."""
        session = cls.get_session()
        try:
            cls._get_or_create(session, inviter_id)
            cls._get_or_create(session, partner_id)
            consent = Consent(inviter_id=inviter_id, partner_id=partner_id, kind=kind, status='active')
            session.add(consent)
            session.commit()
            return {
                'id': consent.id,
                'inviter_id': inviter_id,
                'partner_id': partner_id,
                'timestamp': _ts(consent.created_at),
                'status': consent.status,
                'type': consent.kind,
            }
        except Exception as e:
            session.rollback()
            logger.error(f"Error adding consent: {e}")
            raise
        finally:
            cls.close_session()

    @classmethod
    def revoke_consent(cls, consent_id: int, by_user_id: str) -> bool:
        """Revoke a consent; only one of the two parties may do it."""
        session = cls.get_session()
        try:
            consent = session.get(Consent, consent_id)
            if not consent or by_user_id not in (consent.inviter_id, consent.partner_id):
                return False
            if consent.status == 'active':
                consent.status = 'revoked'
                consent.revoked_at = utcnow()
                consent.revoked_by = by_user_id
                session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error revoking consent: {e}")
            return False
        finally:
            cls.close_session()

    # ===== Invites =====

    @classmethod
    def create_invite(cls, token: str, inviter_id: str, kind: str, expires_at: Optional[datetime] = None) -> bool:
        session = cls.get_session()
        try:
            cls._get_or_create(session, inviter_id)
            session.add(Invite(token=token, inviter_id=inviter_id, kind=kind, expires_at=expires_at))
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error creating invite: {e}")
            return False
        finally:
            cls.close_session()

    @classmethod
    def get_invite(cls, token: str) -> Optional[Invite]:
        session = cls.get_session()
        try:
            return session.get(Invite, token)
        finally:
            cls.close_session()

    @classmethod
    def mark_invite_used(cls, token: str, used_by: str, decision: str) -> bool:
        session = cls.get_session()
        try:
            invite = session.get(Invite, token)
            if not invite:
                return False
            invite.used_by = used_by
            invite.used_at = utcnow()
            invite.decision = decision
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error marking invite used: {e}")
            return False
        finally:
            cls.close_session()

    # ===== Notifications =====

    @classmethod
    def add_notification(cls, user_id: str, type_: str, from_user: Optional[str], message: str) -> Dict[str, Any]:
        session = cls.get_session()
        try:
            cls._get_or_create(session, user_id)
            n = Notification(user_id=user_id, type=type_, from_user=from_user, message=message)
            session.add(n)
            session.commit()
            return {'type': type_, 'from_user': from_user, 'timestamp': _ts(n.created_at), 'message': message}
        except Exception as e:
            session.rollback()
            logger.error(f"Error adding notification: {e}")
            raise
        finally:
            cls.close_session()

    @classmethod
    def mark_notifications_delivered(cls, user_id: str) -> None:
        session = cls.get_session()
        try:
            session.execute(
                update(Notification)
                .where(Notification.user_id == user_id, Notification.delivered_at.is_(None))
                .values(delivered_at=utcnow())
            )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Error marking notifications delivered: {e}")
        finally:
            cls.close_session()

    # ===== Unseen counters =====

    @classmethod
    def bump_unseen(cls, user_id: str, friends: int = 0, partners: int = 0) -> None:
        session = cls.get_session()
        try:
            cls._get_or_create(session, user_id)
            session.execute(
                update(User).where(User.user_id == user_id).values(
                    unseen_friends=User.unseen_friends + friends,
                    unseen_partners=User.unseen_partners + partners,
                )
            )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Error bumping unseen counters: {e}")
        finally:
            cls.close_session()

    @classmethod
    def clear_unseen(cls, user_id: str, friends: bool = False, partners: bool = False) -> None:
        values = {}
        if friends:
            values['unseen_friends'] = 0
        if partners:
            values['unseen_partners'] = 0
        if not values:
            return
        session = cls.get_session()
        try:
            session.execute(update(User).where(User.user_id == user_id).values(**values))
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Error clearing unseen counters: {e}")
        finally:
            cls.close_session()

    # ===== Payments =====

    @classmethod
    def record_payment(cls, charge_id: str, user_id: str, amount: int, currency: str = 'XTR',
                       payload: Optional[str] = None) -> Optional[int]:
        """Record a Stars payment and credit stars. Returns new stars total, or None if duplicate."""
        session = cls.get_session()
        try:
            existing = session.execute(select(Payment.id).where(Payment.charge_id == charge_id)).first()
            if existing:
                return None
            cls._get_or_create(session, user_id)
            session.add(Payment(charge_id=charge_id, user_id=user_id, amount=amount, currency=currency, payload=payload))
            session.execute(update(User).where(User.user_id == user_id).values(stars=User.stars + amount))
            session.commit()
            return session.execute(select(User.stars).where(User.user_id == user_id)).scalar_one()
        except Exception as e:
            session.rollback()
            logger.error(f"Error recording payment {charge_id}: {e}")
            raise
        finally:
            cls.close_session()

    # ===== GDPR: access and erasure =====

    @classmethod
    def export_user(cls, user_id: str) -> Optional[Dict[str, Any]]:
        """Everything stored about one user, as plain JSON-serialisable data (right of access)."""
        session = cls.get_session()
        try:
            u = session.get(User, user_id)
            if u is None:
                return None
            iso = lambda dt: dt.isoformat() + 'Z' if dt else None  # noqa: E731
            return {
                'user': {
                    'user_id': u.user_id, 'balance': u.balance, 'stars': u.stars,
                    'username': u.username, 'first_name': u.first_name, 'last_name': u.last_name,
                    'language_code': u.language_code, 'is_premium': bool(u.is_premium),
                    'created_at': iso(u.created_at), 'updated_at': iso(u.updated_at),
                },
                'friends': [fid for (fid,) in session.execute(
                    select(referral_friends.c.friend_id).where(referral_friends.c.user_id == user_id)).all()],
                'emergency_contacts': [fid for (fid,) in session.execute(
                    select(referral_friends.c.friend_id).where(
                        (referral_friends.c.user_id == user_id) & (referral_friends.c.is_emergency.is_(True)))).all()],
                'missions': [
                    {'mission_id': m.mission_id, 'reward': m.reward, 'completed_at': iso(m.completed_at)}
                    for m in session.execute(select(MissionCompletion).where(
                        MissionCompletion.user_id == user_id)).scalars().all()
                ],
                'partners': [pid for (pid,) in session.execute(
                    select(beloved_partners.c.partner_id).where(beloved_partners.c.user_id == user_id)).all()],
                'consents': [
                    {'id': c.id, 'inviter_id': c.inviter_id, 'partner_id': c.partner_id, 'kind': c.kind,
                     'status': c.status, 'created_at': iso(c.created_at), 'revoked_at': iso(c.revoked_at),
                     'revoked_by': c.revoked_by}
                    for c in session.execute(select(Consent).where(
                        (Consent.inviter_id == user_id) | (Consent.partner_id == user_id))).scalars().all()
                ],
                'invites_issued': [
                    {'kind': i.kind, 'created_at': iso(i.created_at), 'expires_at': iso(i.expires_at),
                     'used_at': iso(i.used_at), 'decision': i.decision}
                    for i in session.execute(select(Invite).where(Invite.inviter_id == user_id)).scalars().all()
                ],
                'payments': [
                    {'charge_id': p.charge_id, 'amount': p.amount, 'currency': p.currency, 'created_at': iso(p.created_at)}
                    for p in session.execute(select(Payment).where(Payment.user_id == user_id)).scalars().all()
                ],
                'ledger': [
                    {'delta': e.delta, 'reason': e.reason, 'ref': e.ref, 'created_at': iso(e.created_at)}
                    for e in session.execute(select(LedgerEntry).where(LedgerEntry.user_id == user_id)).scalars().all()
                ],
            }
        finally:
            cls.close_session()

    @classmethod
    def delete_user(cls, user_id: str) -> bool:
        """Erase a user (right to erasure).

        Everything owned by the user is deleted. Records that also belong to someone else
        (the other party's consent history, payment records) are kept with the user's id
        replaced by the DELETED_USER_ID placeholder; active consents are revoked.
        """
        if user_id == DELETED_USER_ID:
            return False
        session = cls.get_session()
        try:
            if session.get(User, user_id) is None:
                return False
            if session.get(User, DELETED_USER_ID) is None:
                session.add(User(user_id=DELETED_USER_ID, balance=0, stars=0, first_name='Deleted user'))
                session.flush()
            now = utcnow()

            session.execute(referral_friends.delete().where(
                (referral_friends.c.user_id == user_id) | (referral_friends.c.friend_id == user_id)))
            session.execute(beloved_partners.delete().where(
                (beloved_partners.c.user_id == user_id) | (beloved_partners.c.partner_id == user_id)))

            session.execute(update(Consent).where(
                ((Consent.inviter_id == user_id) | (Consent.partner_id == user_id)) & (Consent.status == 'active')
            ).values(status='revoked', revoked_at=now, revoked_by=DELETED_USER_ID))
            session.execute(update(Consent).where(Consent.inviter_id == user_id).values(inviter_id=DELETED_USER_ID))
            session.execute(update(Consent).where(Consent.partner_id == user_id).values(partner_id=DELETED_USER_ID))
            session.execute(update(Consent).where(Consent.revoked_by == user_id).values(revoked_by=DELETED_USER_ID))

            session.execute(Invite.__table__.delete().where(Invite.inviter_id == user_id))
            session.execute(update(Invite).where(Invite.used_by == user_id).values(used_by=DELETED_USER_ID))

            session.execute(Notification.__table__.delete().where(Notification.user_id == user_id))
            session.execute(update(Notification).where(Notification.from_user == user_id).values(from_user=DELETED_USER_ID))

            session.execute(LedgerEntry.__table__.delete().where(LedgerEntry.user_id == user_id))
            session.execute(MissionCompletion.__table__.delete().where(MissionCompletion.user_id == user_id))
            session.execute(update(Payment).where(Payment.user_id == user_id).values(user_id=DELETED_USER_ID))

            session.execute(User.__table__.delete().where(User.user_id == user_id))
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error deleting user: {e}")
            raise
        finally:
            cls.close_session()

    # ===== Bulk Operations =====

    @classmethod
    def get_all_users(cls) -> Dict[str, int]:
        """Get all users as dict {user_id: balance}"""
        session = cls.get_session()
        try:
            rows = session.execute(select(User.user_id, User.balance)).all()
            return {uid: bal or 0 for uid, bal in rows}
        finally:
            cls.close_session()

    @classmethod
    def get_user_count(cls) -> int:
        """Get total user count"""
        session = cls.get_session()
        try:
            return session.execute(select(func.count()).select_from(User)).scalar_one()
        finally:
            cls.close_session()

    @classmethod
    def load_snapshot(cls) -> Dict[str, Any]:
        """Load the whole state in a handful of queries (no N+1) in app.py's cache shape."""
        session = cls.get_session()
        try:
            users = session.execute(select(User)).scalars().all()

            user_tokens: Dict[str, int] = {}
            user_stars: Dict[str, int] = {}
            user_profiles: Dict[str, Dict[str, Any]] = {}
            new_friends_count: Dict[str, int] = {}
            new_partners_count: Dict[str, int] = {}
            for u in users:
                if u.user_id == DELETED_USER_ID:
                    continue
                user_tokens[u.user_id] = u.balance or 0
                if u.stars:
                    user_stars[u.user_id] = u.stars
                if u.username or u.first_name:
                    user_profiles[u.user_id] = {
                        'username': u.username or '',
                        'first_name': u.first_name or '',
                        'last_name': u.last_name or '',
                        'language_code': u.language_code or '',
                        'is_premium': bool(u.is_premium),
                    }
                if u.unseen_friends:
                    new_friends_count[u.user_id] = u.unseen_friends
                if u.unseen_partners:
                    new_partners_count[u.user_id] = u.unseen_partners

            referrals: Dict[str, List[str]] = {}
            for uid, fid in session.execute(select(referral_friends.c.user_id, referral_friends.c.friend_id)).all():
                referrals.setdefault(uid, []).append(fid)

            beloveds: Dict[str, List[str]] = {}
            for uid, pid in session.execute(select(beloved_partners.c.user_id, beloved_partners.c.partner_id)).all():
                beloveds.setdefault(uid, []).append(pid)

            consent_history: Dict[str, List[Dict[str, Any]]] = {}
            for c in session.execute(select(Consent).order_by(Consent.created_at)).scalars().all():
                base = {'id': c.id, 'timestamp': _ts(c.created_at), 'status': c.status, 'type': c.kind}
                consent_history.setdefault(c.inviter_id, []).append({**base, 'partner_id': c.partner_id})
                consent_history.setdefault(c.partner_id, []).append({**base, 'partner_id': c.inviter_id})

            user_notifications: Dict[str, List[Dict[str, Any]]] = {}
            for n in session.execute(
                select(Notification).where(Notification.delivered_at.is_(None)).order_by(Notification.created_at)
            ).scalars().all():
                user_notifications.setdefault(n.user_id, []).append({
                    'type': n.type, 'from_user': n.from_user, 'timestamp': _ts(n.created_at), 'message': n.message,
                })

            pending_friend_requests: Dict[str, List[str]] = {}
            pending_consent_requests: Dict[str, List[str]] = {}
            for inv in session.execute(select(Invite).where(Invite.used_at.is_(None))).scalars().all():
                target = pending_friend_requests if inv.kind == 'friend' else pending_consent_requests
                target.setdefault(inv.inviter_id, []).append(inv.token)

            return {
                'user_tokens': user_tokens,
                'referrals': referrals,
                'beloveds': beloveds,
                'user_stars': user_stars,
                'user_profiles': user_profiles,
                'pending_friend_requests': pending_friend_requests,
                'pending_consent_requests': pending_consent_requests,
                'user_notifications': user_notifications,
                'new_friends_count': new_friends_count,
                'new_partners_count': new_partners_count,
                'consent_history': consent_history,
            }
        finally:
            cls.close_session()

    @classmethod
    def test_connection(cls) -> bool:
        """Test database connection"""
        try:
            session = cls.get_session()
            session.execute(text("SELECT 1"))
            cls.close_session()
            return True
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return False
