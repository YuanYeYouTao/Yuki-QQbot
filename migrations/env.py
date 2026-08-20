"""Async Alembic environment."""

from __future__ import annotations

import asyncio
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from qq_ai_bot.config import Settings
from qq_ai_bot.persistence.metadata import Base

config = context.config
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name)

settings = Settings()
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
target_metadata = Base.metadata


def _current_revision(connection: Connection) -> str | None:
    """Read the on-disk revision before Alembic opens its DDL transaction."""

    if connection.dialect.name != "sqlite":
        return None
    exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    ).scalar_one_or_none()
    if exists is None:
        return None
    return connection.exec_driver_sql(
        "SELECT version_num FROM alembic_version"
    ).scalar_one_or_none()


def run_migrations_offline() -> None:
    """Run migrations without a live connection."""

    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on a synchronous connection proxy."""

    if connection.dialect.name == "sqlite":
        # Historical SQLite batch migrations were authored with FK enforcement
        # disabled. 0042 is a separate 0041 -> 0042 cutover and deliberately
        # starts a fresh, FK-enforced transaction.
        foreign_keys = "ON" if _current_revision(connection) in {"0041", "0042"} else "OFF"
        connection.exec_driver_sql(f"PRAGMA foreign_keys={foreign_keys}")
        connection.exec_driver_sql("PRAGMA busy_timeout=5000")
        connection.commit()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        transactional_ddl=True,
    )
    if connection.dialect.name == "sqlite":
        # Python's sqlite3 driver does not reliably BEGIN for DDL. An explicit
        # write transaction is required so 0042 failpoints restore dropped and
        # newly created tables, not merely the Alembic version row.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            context.run_migrations()
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        return
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run its synchronous migration callback."""

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
