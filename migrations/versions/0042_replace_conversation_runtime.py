"""Replace hierarchical history with bot-aware scope and one rollup checkpoint.

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-20
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TABLES = (
    "conversation_history_summary_members",
    "conversation_history_rollup_jobs",
    "conversation_history_summaries",
    "conversation_history_states",
    "context_resets",
)
_REQUIRED_0041_TABLES = _OLD_TABLES[:-1]


def _failpoint(name: str) -> None:
    """Test-only rollback injection; deployments leave this environment key unset."""

    if os.environ.get("YUKI_MIGRATION_0042_FAILPOINT") == name:
        raise RuntimeError(f"0042 injected failure after {name}")


def _signature(connection: object, table: str) -> tuple[int, str]:
    """Hash every persisted column without depending on SQLite extensions."""

    digest = hashlib.sha256()
    rows = connection.exec_driver_sql(  # type: ignore[attr-defined]
        f'SELECT * FROM "{table}" ORDER BY id'
    )
    count = 0
    for row in rows:
        count += 1
        for value in row:
            encoded = ("<NULL>" if value is None else str(value)).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return count, digest.hexdigest()


def upgrade() -> None:
    connection = op.get_bind()
    current_revision = connection.exec_driver_sql(
        "SELECT version_num FROM alembic_version"
    ).scalar_one_or_none()
    if current_revision != "0041":
        raise RuntimeError(f"0042 requires current Alembic revision 0041, got {current_revision}")
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    missing = [
        table
        for table in (*_REQUIRED_0041_TABLES, "chat_events", "people", "groups")
        if table not in tables
    ]
    if missing:
        raise RuntimeError(f"0042 preflight missing required tables: {', '.join(missing)}")
    if int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()) != 1:
        raise RuntimeError("0042 requires PRAGMA foreign_keys=ON")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("0042 preflight foreign_key_check failed")
    invalid = int(
        connection.exec_driver_sql(
            """
            SELECT COUNT(*) FROM chat_events AS ce
            WHERE trim(ce.bot_user_id) = ''
               OR ce.scope_type NOT IN ('private', 'group')
               OR (
                    ce.scope_type = 'private'
                    AND (
                        ce.private_peer_user_id IS NULL
                        OR trim(ce.private_peer_user_id) = ''
                        OR ce.group_id IS NOT NULL
                    )
               )
               OR (
                    ce.scope_type = 'group'
                    AND (
                        ce.group_id IS NULL
                        OR trim(ce.group_id) = ''
                        OR ce.private_peer_user_id IS NOT NULL
                    )
               )
               OR NOT EXISTS (SELECT 1 FROM people p WHERE p.user_id = ce.bot_user_id)
               OR NOT EXISTS (SELECT 1 FROM people p WHERE p.user_id = ce.sender_user_id)
               OR (
                    ce.scope_type = 'private'
                    AND NOT EXISTS (
                        SELECT 1 FROM people p WHERE p.user_id = ce.private_peer_user_id
                    )
               )
               OR (
                    ce.scope_type = 'group'
                    AND NOT EXISTS (SELECT 1 FROM groups g WHERE g.group_id = ce.group_id)
               )
            """
        ).scalar_one()
    )
    if invalid:
        raise RuntimeError(f"0042 preflight found {invalid} invalid chat_events rows")
    preserved = {"chat_events": _signature(connection, "chat_events")}
    if "memory_facts" in tables:
        preserved["memory_facts"] = _signature(connection, "memory_facts")

    connection.exec_driver_sql(
        """
        CREATE TABLE conversation_scopes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_key VARCHAR(255) NOT NULL UNIQUE,
            bot_user_id VARCHAR(64) NOT NULL,
            scope_type VARCHAR(16) NOT NULL,
            private_peer_user_id VARCHAR(64),
            group_id VARCHAR(64),
            generation INTEGER NOT NULL DEFAULT 1,
            starts_after_event_id INTEGER NOT NULL DEFAULT 0,
            last_event_id INTEGER NOT NULL DEFAULT 0,
            last_generation_change_event_id INTEGER NOT NULL DEFAULT 0,
            uncovered_event_count INTEGER NOT NULL DEFAULT 0,
            uncovered_character_count INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT ck_conversation_scopes_identity CHECK (
                (scope_type = 'private' AND private_peer_user_id IS NOT NULL AND group_id IS NULL)
                OR
                (scope_type = 'group' AND group_id IS NOT NULL AND private_peer_user_id IS NULL)
            ),
            CONSTRAINT ck_conversation_scopes_state CHECK (
                generation >= 1 AND starts_after_event_id >= 0 AND last_event_id >= 0
                AND last_generation_change_event_id >= 0
                AND starts_after_event_id <= last_event_id
                AND last_generation_change_event_id <= last_event_id
                AND uncovered_event_count >= 0 AND uncovered_character_count >= 0
            ),
            FOREIGN KEY(bot_user_id) REFERENCES people(user_id) ON DELETE CASCADE,
            FOREIGN KEY(private_peer_user_id) REFERENCES people(user_id) ON DELETE CASCADE,
            FOREIGN KEY(group_id) REFERENCES groups(group_id) ON DELETE CASCADE
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE UNIQUE INDEX uq_conversation_scopes_private
        ON conversation_scopes (bot_user_id, private_peer_user_id)
        WHERE scope_type = 'private'
        """
    )
    connection.exec_driver_sql(
        """
        CREATE UNIQUE INDEX uq_conversation_scopes_group
        ON conversation_scopes (bot_user_id, group_id)
        WHERE scope_type = 'group'
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE conversation_rollups (
            scope_id INTEGER PRIMARY KEY,
            generation INTEGER NOT NULL,
            covered_through_event_id INTEGER NOT NULL,
            summary_text TEXT NOT NULL,
            summary_kind VARCHAR(16) NOT NULL,
            source_fingerprint VARCHAR(64) NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT ck_conversation_rollups_kind
                CHECK (summary_kind IN ('model', 'extractive')),
            CONSTRAINT ck_conversation_rollups_state CHECK (
                generation >= 1 AND covered_through_event_id >= 0
                AND revision >= 1 AND length(summary_text) > 0
            ),
            FOREIGN KEY(scope_id) REFERENCES conversation_scopes(id) ON DELETE CASCADE
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE conversation_rollup_jobs (
            scope_id INTEGER PRIMARY KEY,
            generation INTEGER NOT NULL,
            signal_revision INTEGER NOT NULL DEFAULT 1,
            status VARCHAR(16) NOT NULL,
            failure_count INTEGER NOT NULL DEFAULT 0,
            lease_owner VARCHAR(128),
            lease_token VARCHAR(64),
            lease_until DATETIME,
            next_attempt_at DATETIME NOT NULL,
            last_error_category VARCHAR(64),
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT ck_conversation_rollup_jobs_status
                CHECK (status IN ('pending', 'processing')),
            CONSTRAINT ck_conversation_rollup_jobs_state CHECK (
                generation >= 1 AND signal_revision >= 1 AND failure_count >= 0
            ),
            CONSTRAINT ck_conversation_rollup_jobs_lease CHECK (
                (status = 'pending' AND lease_owner IS NULL
                 AND lease_token IS NULL AND lease_until IS NULL)
                OR
                (status = 'processing' AND lease_owner IS NOT NULL
                 AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
            ),
            FOREIGN KEY(scope_id) REFERENCES conversation_scopes(id) ON DELETE CASCADE
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX ix_conversation_rollup_jobs_claim
        ON conversation_rollup_jobs (status, next_attempt_at, lease_until)
        """
    )
    _failpoint("new_tables")

    connection.exec_driver_sql(
        """
        INSERT INTO conversation_scopes (
            scope_key, bot_user_id, scope_type, private_peer_user_id, group_id,
            generation, starts_after_event_id, last_event_id,
            last_generation_change_event_id, uncovered_event_count,
            uncovered_character_count, created_at, updated_at
        )
        SELECT
            'bot:' || bot_user_id || ':private:' || private_peer_user_id,
            bot_user_id, 'private', private_peer_user_id, NULL,
            1, MAX(id), MAX(id), 0, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM chat_events
        WHERE scope_type = 'private'
        GROUP BY bot_user_id, private_peer_user_id
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO conversation_scopes (
            scope_key, bot_user_id, scope_type, private_peer_user_id, group_id,
            generation, starts_after_event_id, last_event_id,
            last_generation_change_event_id, uncovered_event_count,
            uncovered_character_count, created_at, updated_at
        )
        SELECT
            'bot:' || bot_user_id || ':group:' || group_id,
            bot_user_id, 'group', NULL, group_id,
            1, MAX(id), MAX(id), 0, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM chat_events
        WHERE scope_type = 'group'
        GROUP BY bot_user_id, group_id
        """
    )
    expected_scopes = int(
        connection.exec_driver_sql(
            """
            SELECT COUNT(*) FROM (
                SELECT bot_user_id, private_peer_user_id FROM chat_events
                WHERE scope_type = 'private' GROUP BY bot_user_id, private_peer_user_id
                UNION ALL
                SELECT bot_user_id, group_id FROM chat_events
                WHERE scope_type = 'group' GROUP BY bot_user_id, group_id
            )
            """
        ).scalar_one()
    )
    actual_scopes = int(
        connection.exec_driver_sql("SELECT COUNT(*) FROM conversation_scopes").scalar_one()
    )
    invalid_boundaries = int(
        connection.exec_driver_sql(
            """
            SELECT COUNT(*) FROM conversation_scopes
            WHERE generation != 1 OR starts_after_event_id != last_event_id
               OR last_generation_change_event_id != 0
               OR uncovered_event_count != 0 OR uncovered_character_count != 0
            """
        ).scalar_one()
    )
    if actual_scopes != expected_scopes or invalid_boundaries:
        raise RuntimeError("0042 scope backfill validation failed")
    _failpoint("scope_backfill")

    for index, table in enumerate(_OLD_TABLES):
        if table in tables:
            connection.exec_driver_sql(f"DROP TABLE {table}")
        if index == 0:
            _failpoint("first_old_table_drop")

    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("0042 postflight foreign_key_check failed")
    for table, signature in preserved.items():
        if _signature(connection, table) != signature:
            raise RuntimeError(f"0042 changed preserved table {table}")


def downgrade() -> None:
    raise RuntimeError(
        "0042 is irreversible; restore the pre-3.7.0 database snapshot and deploy 3.6.1"
    )
