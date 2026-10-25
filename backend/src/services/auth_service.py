import hashlib
import hmac
import secrets
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from src.models.entities import User, Session, CompanyMembership, Company
from src.config import settings

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return f"{salt}:{key.hex()}"

def verify_password(stored_hash: str, password: str) -> bool:
    try:
        salt, key_hex = stored_hash.split(":")
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
        return hmac.compare_digest(key.hex(), key_hex)
    except Exception:
        return False

class AuthService:
    @staticmethod
    async def get_or_create_demo_user(db: AsyncSession) -> User:
        demo_email = "demo@centrinvest.ru"
        res = await db.execute(select(User).where(User.email == demo_email))
        user = res.scalar_one_or_none()
        if not user:
            user = User(
                email=demo_email,
                password_hash=hash_password("password123"),
                display_name="Демо Пользователь (Центр-инвест)"
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
        return user

    @staticmethod
    async def authenticate(db: AsyncSession, email: str, password: str) -> User | None:
        res = await db.execute(select(User).where(User.email == email))
        user = res.scalar_one_or_none()
        if not user:
            return None
        if not verify_password(user.password_hash, password):
            return None
        return user

    @staticmethod
    async def create_session(db: AsyncSession, user_id: str) -> Session:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.SESSION_MAX_AGE_SECONDS)
        session = Session(
            id=token,
            user_id=user_id,
            expires_at=expires_at
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session

    @staticmethod
    async def validate_session(db: AsyncSession, token: str) -> tuple[User, Session] | None:
        if not token:
            return None
        res = await db.execute(select(Session).where(Session.id == token))
        session = res.scalar_one_or_none()
        if not session:
            return None
        expires_at = session.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            await db.execute(delete(Session).where(Session.id == token))
            await db.commit()
            return None
        
        user_res = await db.execute(select(User).where(User.id == session.user_id))
        user = user_res.scalar_one_or_none()
        if not user:
            return None
        return user, session

    @staticmethod
    async def delete_session(db: AsyncSession, token: str):
        await db.execute(delete(Session).where(Session.id == token))
        await db.commit()

    @staticmethod
    async def get_user_company_ids(db: AsyncSession, user_id: str) -> list[str]:
        res = await db.execute(select(CompanyMembership.company_id).where(CompanyMembership.user_id == user_id))
        return [r[0] for r in res.all()]

    @staticmethod
    async def ensure_user_has_company(db: AsyncSession, user_id: str, company_id: str) -> bool:
        res = await db.execute(
            select(CompanyMembership).where(
                CompanyMembership.user_id == user_id,
                CompanyMembership.company_id == company_id
            )
        )
        membership = res.scalar_one_or_none()
        return bool(membership)
