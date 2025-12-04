"""
Database Service for Affinia
Handles PostgreSQL database operations using SQLAlchemy
"""

import os
import logging
from typing import Optional, Dict, List, Any
from datetime import datetime

from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Text, ForeignKey, Table
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session, relationship
from sqlalchemy.pool import QueuePool

logger = logging.getLogger(__name__)

Base = declarative_base()


# Association tables for many-to-many relationships
referral_friends = Table(
    'referral_friends',
    Base.metadata,
    Column('user_id', String(50), ForeignKey('users.user_id'), primary_key=True),
    Column('friend_id', String(50), ForeignKey('users.user_id'), primary_key=True)
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
    balance = Column(Integer, default=0)
    stars = Column(Integer, default=0)
    
    # Profile info
    username = Column(String(100), nullable=True)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    language_code = Column(String(10), nullable=True)
    is_premium = Column(Boolean, default=False)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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


class DatabaseService:
    """Singleton service for database operations"""
    
    _engine = None
    _session_factory = None
    _initialized = False

    @classmethod
    def initialize(cls, database_url: Optional[str] = None) -> bool:
        """Initialize database connection"""
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
            logger.info(f"🔌 Connecting to PostgreSQL...")
            cls._engine = create_engine(
                database_url,
                poolclass=QueuePool,
                pool_size=5,
                max_overflow=10,
                pool_pre_ping=True,
                echo=False
            )
            
            cls._session_factory = scoped_session(sessionmaker(bind=cls._engine))
            
            # Create tables if they don't exist
            Base.metadata.create_all(cls._engine)
            
            cls._initialized = True
            logger.info("✅ PostgreSQL connected successfully!")
            return True
            
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            return False

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
            return session.query(User).filter(User.user_id == user_id).first()
        finally:
            cls.close_session()

    @classmethod
    def create_user(cls, user_id: str, balance: int = 0) -> User:
        """Create a new user"""
        session = cls.get_session()
        try:
            user = User(user_id=user_id, balance=balance)
            session.add(user)
            session.commit()
            logger.info(f"🆕 Created user {user_id} with balance {balance}")
            return user
        except Exception as e:
            session.rollback()
            logger.error(f"Error creating user {user_id}: {e}")
            raise
        finally:
            cls.close_session()

    @classmethod
    def ensure_user(cls, user_id: str) -> tuple[User, bool]:
        """Ensure user exists, create if not. Returns (user, was_created)"""
        session = cls.get_session()
        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if user:
                return user, False
            
            user = User(user_id=user_id, balance=0)
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
    def update_balance(cls, user_id: str, amount: int) -> int:
        """Update user balance by amount. Returns new balance."""
        session = cls.get_session()
        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user:
                user = User(user_id=user_id, balance=amount)
                session.add(user)
            else:
                user.balance += amount
            session.commit()
            return user.balance
        except Exception as e:
            session.rollback()
            logger.error(f"Error updating balance for {user_id}: {e}")
            raise
        finally:
            cls.close_session()

    @classmethod
    def set_balance(cls, user_id: str, balance: int) -> int:
        """Set user balance to exact value. Returns new balance."""
        session = cls.get_session()
        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user:
                user = User(user_id=user_id, balance=balance)
                session.add(user)
            else:
                user.balance = balance
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
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user:
                user = User(user_id=user_id)
                session.add(user)
            
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
        """Add friend relationship (bidirectional)"""
        session = cls.get_session()
        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            friend = session.query(User).filter(User.user_id == friend_id).first()
            
            if not user or not friend:
                return False
            
            if friend not in user.friends:
                user.friends.append(friend)
            if user not in friend.friends:
                friend.friends.append(user)
            
            session.commit()
            return True
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
            user = session.query(User).filter(User.user_id == user_id).first()
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
            user = session.query(User).filter(User.user_id == user_id).first()
            friend = session.query(User).filter(User.user_id == friend_id).first()
            
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
            return False
        finally:
            cls.close_session()

    # ===== Partners/Beloveds Operations =====

    @classmethod
    def add_partner(cls, user_id: str, partner_id: str) -> bool:
        """Add partner relationship (bidirectional)"""
        session = cls.get_session()
        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            partner = session.query(User).filter(User.user_id == partner_id).first()
            
            if not user or not partner:
                return False
            
            if partner not in user.partners:
                user.partners.append(partner)
            if user not in partner.partners:
                partner.partners.append(user)
            
            session.commit()
            return True
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
            user = session.query(User).filter(User.user_id == user_id).first()
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
            user = session.query(User).filter(User.user_id == user_id).first()
            partner = session.query(User).filter(User.user_id == partner_id).first()
            
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
            return False
        finally:
            cls.close_session()

    # ===== Bulk Operations =====

    @classmethod
    def get_all_users(cls) -> Dict[str, int]:
        """Get all users as dict {user_id: balance}"""
        session = cls.get_session()
        try:
            users = session.query(User).all()
            return {u.user_id: u.balance for u in users}
        finally:
            cls.close_session()

    @classmethod
    def get_user_count(cls) -> int:
        """Get total user count"""
        session = cls.get_session()
        try:
            return session.query(User).count()
        finally:
            cls.close_session()

    @classmethod
    def migrate_from_json(cls, data: Dict[str, Any]) -> bool:
        """Migrate data from JSON format to PostgreSQL"""
        session = cls.get_session()
        try:
            user_tokens = data.get('user_tokens', {})
            referrals = data.get('referrals', {})
            beloveds = data.get('beloveds', {})
            user_profiles = data.get('user_profiles', {})
            user_stars = data.get('user_stars', {})
            
            # Create all users first
            for user_id, balance in user_tokens.items():
                user = session.query(User).filter(User.user_id == user_id).first()
                if not user:
                    user = User(user_id=user_id)
                    session.add(user)
                user.balance = balance
                user.stars = user_stars.get(user_id, 0)
                
                # Update profile if exists
                if user_id in user_profiles:
                    profile = user_profiles[user_id]
                    user.username = profile.get('username', '')
                    user.first_name = profile.get('first_name', '')
                    user.last_name = profile.get('last_name', '')
                    user.language_code = profile.get('language_code', '')
                    user.is_premium = profile.get('is_premium', False)
            
            session.commit()
            logger.info(f"✅ Migrated {len(user_tokens)} users")
            
            # Add friend relationships
            for user_id, friends in referrals.items():
                user = session.query(User).filter(User.user_id == user_id).first()
                if user:
                    for friend_id in friends:
                        friend = session.query(User).filter(User.user_id == friend_id).first()
                        if friend and friend not in user.friends:
                            user.friends.append(friend)
            
            session.commit()
            logger.info(f"✅ Migrated referral relationships")
            
            # Add partner relationships
            for user_id, partners in beloveds.items():
                user = session.query(User).filter(User.user_id == user_id).first()
                if user:
                    for partner_id in partners:
                        partner = session.query(User).filter(User.user_id == partner_id).first()
                        if partner and partner not in user.partners:
                            user.partners.append(partner)
            
            session.commit()
            logger.info(f"✅ Migrated beloved relationships")
            
            return True
            
        except Exception as e:
            session.rollback()
            logger.error(f"❌ Migration failed: {e}")
            return False
        finally:
            cls.close_session()

    @classmethod
    def test_connection(cls) -> bool:
        """Test database connection"""
        try:
            session = cls.get_session()
            session.execute("SELECT 1")
            cls.close_session()
            return True
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return False
