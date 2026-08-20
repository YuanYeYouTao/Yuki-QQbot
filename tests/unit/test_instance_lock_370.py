from __future__ import annotations

from pathlib import Path

import pytest

from qq_ai_bot.persistence.instance_lock import (
    ApplicationAlreadyActiveError,
    SQLiteApplicationLock,
)


def test_sqlite_application_lock_rejects_dual_active_and_recovers(tmp_path: Path) -> None:
    database_path = tmp_path / "bot.db"
    first = SQLiteApplicationLock(database_path)
    second = SQLiteApplicationLock(database_path)

    first.acquire()
    try:
        with pytest.raises(ApplicationAlreadyActiveError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_in_memory_database_needs_no_process_lock() -> None:
    lock = SQLiteApplicationLock(Path(":memory:"))
    lock.acquire()
    lock.release()
    assert lock.path is None
