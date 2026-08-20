"""Isolated SQLite migration helpers shared by quality runners."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config


def migrate_sqlite_database(repository_root: Path, path: Path) -> None:
    """Create one temporary database at head without retaining DATABASE_URL changes."""

    config = Config(str(repository_root / "alembic.ini"))
    config.set_main_option("script_location", str(repository_root / "migrations"))
    database_url = f"sqlite+aiosqlite:///{path.as_posix()}"
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(config, "0041")
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
