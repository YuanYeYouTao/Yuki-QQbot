"""Cross-platform single-active-Application guard for one SQLite database."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class ApplicationAlreadyActiveError(RuntimeError):
    """Another process already owns the database's active-Application lock."""


class SQLiteApplicationLock:
    """Hold an advisory OS lock for the full Application lifecycle."""

    def __init__(self, database_path: Path | None) -> None:
        if database_path is None or str(database_path) == ":memory:":
            self.path: Path | None = None
        else:
            resolved = database_path.expanduser().resolve(strict=False)
            self.path = Path(f"{resolved}.application.lock")
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self.path is None:
            return
        if self._handle is not None:
            raise RuntimeError("application lock is already held by this instance")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            _lock_handle(handle)
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()}\n".encode("ascii"))
            handle.flush()
        except OSError as exc:
            handle.close()
            raise ApplicationAlreadyActiveError(
                "another active Application is already using this SQLite database"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock_handle(handle)
        finally:
            handle.close()


def _lock_handle(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(  # type: ignore[attr-defined]
        handle.fileno(),
        fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
    )


def _unlock_handle(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
