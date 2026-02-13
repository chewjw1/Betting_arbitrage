"""Database session management."""

from collections.abc import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import get_settings
from src.database.models import Base

settings = get_settings()

# SQLite needs special handling for concurrent access
is_sqlite = "sqlite" in settings.database_url

engine_kwargs = {
    "echo": settings.log_level == "DEBUG",
    "pool_pre_ping": True,
}

if is_sqlite:
    # SQLite: use NullPool to avoid locking issues with multiple processes
    from sqlalchemy.pool import StaticPool

    engine_kwargs.update({
        "connect_args": {"check_same_thread": False, "timeout": 30},
        "poolclass": StaticPool,
    })
else:
    engine_kwargs.update({
        "pool_size": 5,
        "max_overflow": 10,
    })

# Create async engine
engine = create_async_engine(
    settings.database_url,
    **engine_kwargs,
)


# Enable WAL mode for SQLite (allows concurrent reads + writes)
if is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


# Create session factory
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting async database sessions."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Initialize database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
