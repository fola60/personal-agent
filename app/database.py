import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "")

# SQLAlchemy needs postgresql+asyncpg:// scheme for async support
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,          # Set True to log all SQL queries
    pool_pre_ping=True,  # Reconnect automatically on stale connections
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ---------------------------------------------------------------------------
# Base class for models
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Create all tables if they don't exist yet, and seed default categories."""
    async with engine.begin() as conn:
        from app import models  # noqa: F401 – ensures models are registered
        await conn.run_sync(Base.metadata.create_all)

    # Seed default categories if the table is empty
    from app.models import Category
    from sqlalchemy import select, func as sa_func

    async with AsyncSessionLocal() as session:
        count = (await session.execute(select(sa_func.count(Category.id)))).scalar() or 0
        if count == 0:
            defaults = [
                "groceries", "dining", "transport", "rent", "utilities",
                "entertainment", "health", "shopping", "subscriptions",
                "income", "transfer", "savings", "education", "other",
            ]
            for name in defaults:
                session.add(Category(name=name, is_default=True))
            await session.commit()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a DB session per request."""
    async with AsyncSessionLocal() as session:
        yield session
