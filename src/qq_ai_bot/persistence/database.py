"""Database engine lifecycle and health checks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from qq_ai_bot.persistence.metadata import Base

_SQLITE_BUSY_TIMEOUT_MS = 5_000


class Database:
    """Own the async SQLAlchemy engine and explicit session factory."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._ensure_sqlite_parent(url)
        # Runtime configuration mutations share one process-wide database owner.
        # Keeping the lock here prevents separately constructed service facades from
        # racing their read/validate/write/audit sequence.
        self.runtime_config_mutation_lock = asyncio.Lock()
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        if url.startswith("sqlite+aiosqlite:///"):
            event.listen(self.engine.sync_engine, "connect", self._configure_sqlite_connection)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    @staticmethod
    def _configure_sqlite_connection(
        dbapi_connection: Any,
        _connection_record: Any,
    ) -> None:
        """Enable integrity and bounded writer waiting for concurrent workers."""

        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    @staticmethod
    def _ensure_sqlite_parent(url: str) -> None:
        prefix = "sqlite+aiosqlite:///"
        if not url.startswith(prefix):
            return
        path = Path(url.removeprefix(prefix))
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)

    async def create_schema(self) -> None:
        """Create all tables for tests; deployments use Alembic migrations."""

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await self._create_fts_schema(connection)

    @staticmethod
    async def _create_fts_schema(connection: Any) -> None:
        """Create external-content FTS indexes used by isolated test databases."""

        statements = (
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chat_events_fts USING fts5(
                content,
                content='chat_events',
                content_rowid='id',
                tokenize='trigram'
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS chat_events_fts_ai
            AFTER INSERT ON chat_events BEGIN
                INSERT INTO chat_events_fts(rowid, content) VALUES (new.id, new.content);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS chat_events_fts_ad
            AFTER DELETE ON chat_events BEGIN
                INSERT INTO chat_events_fts(chat_events_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS chat_events_fts_au
            AFTER UPDATE OF content ON chat_events BEGIN
                INSERT INTO chat_events_fts(chat_events_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
                INSERT INTO chat_events_fts(rowid, content) VALUES (new.id, new.content);
            END
            """,
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_facts_fts USING fts5(
                content,
                memory_key,
                category,
                content='memory_facts',
                content_rowid='id',
                tokenize='trigram'
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS memory_facts_fts_ai
            AFTER INSERT ON memory_facts BEGIN
                INSERT INTO memory_facts_fts(rowid, content, memory_key, category)
                VALUES (new.id, new.content, new.memory_key, new.category);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS memory_facts_fts_ad
            AFTER DELETE ON memory_facts BEGIN
                INSERT INTO memory_facts_fts(
                    memory_facts_fts, rowid, content, memory_key, category
                ) VALUES ('delete', old.id, old.content, old.memory_key, old.category);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS memory_facts_fts_au
            AFTER UPDATE OF content, memory_key, category ON memory_facts BEGIN
                INSERT INTO memory_facts_fts(
                    memory_facts_fts, rowid, content, memory_key, category
                ) VALUES ('delete', old.id, old.content, old.memory_key, old.category);
                INSERT INTO memory_facts_fts(rowid, content, memory_key, category)
                VALUES (new.id, new.content, new.memory_key, new.category);
            END
            """,
        )
        for statement in statements:
            await connection.execute(text(statement))

    async def ping(self) -> bool:
        """Check database connectivity without exposing its path."""

        try:
            async with self.sessions() as session:
                await session.execute(text("SELECT 1"))
            return True
        except (OSError, RuntimeError, SQLAlchemyError):
            return False

    @asynccontextmanager
    async def immediate_session(self) -> AsyncIterator[AsyncSession]:
        """Open one short writer transaction, using BEGIN IMMEDIATE on SQLite."""

        async with self.sessions() as session:
            try:
                if self.url.startswith("sqlite+"):
                    await session.execute(text("BEGIN IMMEDIATE"))
                else:
                    await session.begin()
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def close(self) -> None:
        """Dispose pooled database connections."""

        await self.engine.dispose()
