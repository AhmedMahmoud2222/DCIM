import os
import uuid

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://dcim_app:dcim_dev_password@localhost:5432/dcim_test"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-not-for-production-use-32ch")

from app.core.security import hash_password  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.domain.auth.models import RoleAssignment, User  # noqa: E402
from app.main import app  # noqa: E402

TEST_DATABASE_URL = os.environ["DATABASE_URL"]

_TRUNCATE_TABLES = [
    "idempotency_key",
    "outbox_event",
    "audit_log",
    "managed_asset",
    "room",
    "floor",
    "building",
    "site",
    "city",
    "country",
    "organization",
    "refresh_token",
    "role_assignment",
    "app_user",
]


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(db_engine):
    async with db_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {', '.join(_TRUNCATE_TABLES)} CASCADE"))
    yield


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncSession:
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_session):
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def make_user(db_session):
    async def _make(email: str, password: str, role_name: str) -> User:
        role_stmt = await db_session.execute(text("SELECT id FROM role WHERE name = :n"), {"n": role_name})
        role_id = role_stmt.scalar_one()
        user = User(id=uuid.uuid4(), email=email, full_name="Test User", password_hash=hash_password(password))
        db_session.add(user)
        await db_session.flush()
        db_session.add(RoleAssignment(id=uuid.uuid4(), user_id=user.id, role_id=role_id, scope_type="global"))
        await db_session.commit()
        return user

    return _make


@pytest_asyncio.fixture
async def auth_headers(client, make_user):
    async def _headers(role_name: str = "Administrator") -> dict:
        email = f"user-{uuid.uuid4().hex[:8]}@example.com"
        await make_user(email, "correct horse battery staple", role_name)
        resp = await client.post("/api/v1/auth/login", json={"email": email, "password": "correct horse battery staple"})
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    return _headers
