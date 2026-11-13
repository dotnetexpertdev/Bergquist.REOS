"""Database connection and session management.

SQLite coordination rules used by this application:

* Every transaction starts with ``BEGIN IMMEDIATE`` so writers are serialized by
  SQLite itself. Combined with conditional ``UPDATE ... WHERE revision = ?``
  statements and unique constraints this gives database-enforced coordination
  between two browser tabs, double clicks and concurrent requests.
* Code must commit before any slow remote call (OpenAI, Gmail, QuickBooks) so the
  write lock is never held across the network.
* The server never creates the schema implicitly. ``python backend\\cli.py migrate``
  creates or upgrades the database, so starting from a different directory can
  never silently produce a second empty database.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from .config import settings

SQLITE_BUSY_TIMEOUT_MS = 30000


def _configure_sqlite(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, connection_record):  # pragma: no cover - driver hook
        # Let SQLAlchemy emit BEGIN itself (see _on_begin).
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(conn):  # pragma: no cover - driver hook
        conn.exec_driver_sql("BEGIN IMMEDIATE")


def make_engine(url: Optional[str] = None) -> AsyncEngine:
    engine = create_async_engine(
        url or settings.database_url,
        echo=settings.debug,
        # One SQLite connection per session: real isolation between concurrent
        # requests (the old StaticPool shared a single connection across all of them).
        poolclass=NullPool,
        connect_args={"timeout": SQLITE_BUSY_TIMEOUT_MS / 1000},
    )
    _configure_sqlite(engine)
    return engine


engine = make_engine()

# Session factory
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class SchemaNotReady(RuntimeError):
    """Raised when the database is missing or not migrated."""


async def check_schema() -> str:
    """Verify the database exists and is at the latest migration. Returns revision."""
    from .migrations import head_revision

    if not settings.database_path.exists():
        raise SchemaNotReady(
            f"Database not found at {settings.database_path}. "
            "Run: python backend\\cli.py migrate"
        )
    async with engine.connect() as conn:
        try:
            current = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        except Exception:
            current = None
    head = head_revision()
    if current != head:
        raise SchemaNotReady(
            f"Database at {settings.database_path} is at revision {current or 'none'}, "
            f"expected {head}. Run: python backend\\cli.py migrate"
        )
    return current


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting database sessions."""
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for database sessions outside of request handlers."""
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
