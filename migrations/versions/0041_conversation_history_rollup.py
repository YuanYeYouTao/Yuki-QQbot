"""Add durable conversation history rollup tables.

3.6.1: Conversation Runtime stores derived session summaries beside the event
ledger.  chat_events remain the only raw source.  These tables must not
reference memory_facts.  Downgrade drops rollup rows only.

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "conversation_history_states",
    "conversation_history_summaries",
    "conversation_history_summary_members",
    "conversation_history_rollup_jobs",
)
_EVENT_INDEXES = (
    "ix_chat_events_bot_scope_private_id",
    "ix_chat_events_bot_scope_group_id",
)


def upgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    indexes = {index["name"] for index in inspector.get_indexes("chat_events")}
    if "ix_chat_events_bot_scope_private_id" not in indexes:
        connection.exec_driver_sql(
            """
            CREATE INDEX ix_chat_events_bot_scope_private_id
            ON chat_events (bot_user_id, scope_type, private_peer_user_id, id)
            """
        )
    if "ix_chat_events_bot_scope_group_id" not in indexes:
        connection.exec_driver_sql(
            """
            CREATE INDEX ix_chat_events_bot_scope_group_id
            ON chat_events (bot_user_id, scope_type, group_id, id)
            """
        )
    if "conversation_history_states" not in tables:
        connection.exec_driver_sql(
            """
            CREATE TABLE conversation_history_states (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                bot_user_id VARCHAR(64) NOT NULL,
                scope_type VARCHAR(16) NOT NULL,
                private_peer_user_id VARCHAR(64),
                group_id VARCHAR(64),
                reset_at DATETIME,
                last_seen_event_id INTEGER NOT NULL DEFAULT 0,
                active_frontier_end_event_id INTEGER NOT NULL DEFAULT 0,
                pending_event_count INTEGER NOT NULL DEFAULT 0,
                pending_character_count INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT ck_conversation_history_states_identity CHECK (
                    (
                        scope_type = 'private'
                        AND private_peer_user_id IS NOT NULL
                        AND group_id IS NULL
                    ) OR (
                        scope_type = 'group'
                        AND group_id IS NOT NULL
                        AND private_peer_user_id IS NULL
                    )
                ),
                CONSTRAINT ck_conversation_history_states_counters CHECK (
                    last_seen_event_id >= 0
                    AND active_frontier_end_event_id >= 0
                    AND pending_event_count >= 0
                    AND pending_character_count >= 0
                    AND revision >= 0
                )
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE UNIQUE INDEX uq_conversation_history_states_identity
            ON conversation_history_states (
                bot_user_id,
                scope_type,
                ifnull(private_peer_user_id, ''),
                ifnull(group_id, ''),
                ifnull(reset_at, '')
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE INDEX ix_conversation_history_states_frontier
            ON conversation_history_states (
                bot_user_id, scope_type, active_frontier_end_event_id
            )
            """
        )
    if "conversation_history_summaries" not in tables:
        connection.exec_driver_sql(
            """
            CREATE TABLE conversation_history_summaries (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                state_id INTEGER NOT NULL,
                level INTEGER NOT NULL,
                status VARCHAR(16) NOT NULL,
                start_event_id INTEGER NOT NULL,
                end_event_id INTEGER NOT NULL,
                start_occurred_at DATETIME NOT NULL,
                end_occurred_at DATETIME NOT NULL,
                source_event_count INTEGER NOT NULL,
                source_character_count INTEGER NOT NULL,
                output_character_count INTEGER NOT NULL,
                structured_payload_json TEXT NOT NULL DEFAULT '{}',
                rendered_text TEXT NOT NULL,
                mode VARCHAR(32) NOT NULL,
                trust VARCHAR(32) NOT NULL,
                summarizer_version VARCHAR(64) NOT NULL,
                source_fingerprint VARCHAR(64) NOT NULL,
                replaced_by_summary_id INTEGER,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT ck_conversation_history_summaries_level CHECK (level >= 0),
                CONSTRAINT ck_conversation_history_summaries_range
                    CHECK (start_event_id <= end_event_id),
                CONSTRAINT ck_conversation_history_summaries_status
                    CHECK (status IN ('active', 'rolled_up', 'invalidated')),
                CONSTRAINT ck_conversation_history_summaries_mode
                    CHECK (mode IN ('extractive', 'model_summary')),
                CONSTRAINT ck_conversation_history_summaries_trust
                    CHECK (trust IN ('extractive_compact', 'model_summary')),
                CONSTRAINT ck_conversation_history_summaries_extractive_l0
                    CHECK (mode != 'extractive' OR level = 0),
                CONSTRAINT ck_conversation_history_summaries_replaced_by CHECK (
                    (status = 'active' AND replaced_by_summary_id IS NULL)
                    OR (status = 'rolled_up' AND replaced_by_summary_id IS NOT NULL)
                    OR (status = 'invalidated')
                ),
                CONSTRAINT ck_conversation_history_summaries_counts CHECK (
                    source_event_count >= 0
                    AND source_character_count >= 0
                    AND output_character_count >= 0
                ),
                FOREIGN KEY(state_id) REFERENCES conversation_history_states(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(replaced_by_summary_id)
                    REFERENCES conversation_history_summaries(id) ON DELETE RESTRICT
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE UNIQUE INDEX uq_conversation_history_summaries_active_fingerprint
            ON conversation_history_summaries (state_id, source_fingerprint)
            WHERE status = 'active'
            """
        )
        connection.exec_driver_sql(
            """
            CREATE INDEX ix_conversation_history_summaries_state_range
            ON conversation_history_summaries (
                state_id, status, start_event_id, end_event_id
            )
            """
        )
    if "conversation_history_summary_members" not in tables:
        connection.exec_driver_sql(
            """
            CREATE TABLE conversation_history_summary_members (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                summary_id INTEGER NOT NULL,
                member_type VARCHAR(16) NOT NULL,
                source_event_id INTEGER,
                source_summary_id INTEGER,
                ordinal INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                CONSTRAINT uq_conversation_history_summary_members_ordinal
                    UNIQUE (summary_id, ordinal),
                CONSTRAINT ck_conversation_history_summary_members_type
                    CHECK (member_type IN ('event', 'summary')),
                CONSTRAINT ck_conversation_history_summary_members_source CHECK (
                    (
                        member_type = 'event'
                        AND source_event_id IS NOT NULL
                        AND source_summary_id IS NULL
                    ) OR (
                        member_type = 'summary'
                        AND source_summary_id IS NOT NULL
                        AND source_event_id IS NULL
                    )
                ),
                FOREIGN KEY(summary_id) REFERENCES conversation_history_summaries(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(source_event_id) REFERENCES chat_events(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY(source_summary_id)
                    REFERENCES conversation_history_summaries(id) ON DELETE RESTRICT
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE UNIQUE INDEX uq_conversation_history_summary_members_event
            ON conversation_history_summary_members (summary_id, source_event_id)
            WHERE member_type = 'event'
            """
        )
        connection.exec_driver_sql(
            """
            CREATE UNIQUE INDEX uq_conversation_history_summary_members_summary
            ON conversation_history_summary_members (summary_id, source_summary_id)
            WHERE member_type = 'summary'
            """
        )
        connection.exec_driver_sql(
            """
            CREATE INDEX ix_conversation_history_summary_members_event
            ON conversation_history_summary_members (source_event_id)
            """
        )
        connection.exec_driver_sql(
            """
            CREATE INDEX ix_conversation_history_summary_members_child
            ON conversation_history_summary_members (source_summary_id)
            """
        )
    if "conversation_history_rollup_jobs" not in tables:
        connection.exec_driver_sql(
            """
            CREATE TABLE conversation_history_rollup_jobs (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                state_id INTEGER NOT NULL,
                job_kind VARCHAR(32) NOT NULL,
                source_level INTEGER NOT NULL,
                source_start_id INTEGER NOT NULL,
                source_end_id INTEGER NOT NULL,
                source_fingerprint VARCHAR(64) NOT NULL,
                summarizer_version VARCHAR(64) NOT NULL,
                status VARCHAR(16) NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                lease_owner VARCHAR(128),
                lease_until DATETIME,
                next_attempt_at DATETIME NOT NULL,
                error_category VARCHAR(64),
                outcome VARCHAR(16),
                result_summary_id INTEGER,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                completed_at DATETIME,
                CONSTRAINT uq_conversation_history_rollup_jobs_idempotent UNIQUE (
                    state_id, job_kind, source_fingerprint, summarizer_version
                ),
                CONSTRAINT ck_conversation_history_rollup_jobs_kind
                    CHECK (job_kind IN ('raw_range', 'summary_rollup', 'rebuild')),
                CONSTRAINT ck_conversation_history_rollup_jobs_status
                    CHECK (status IN ('pending', 'processing', 'done', 'failed')),
                CONSTRAINT ck_conversation_history_rollup_jobs_outcome
                    CHECK (outcome IS NULL OR outcome IN ('summary', 'no_change')),
                CONSTRAINT ck_conversation_history_rollup_jobs_range CHECK (
                    source_level >= 0
                    AND source_start_id <= source_end_id
                    AND attempts >= 0
                ),
                CONSTRAINT ck_conversation_history_rollup_jobs_done CHECK (
                    (status != 'done')
                    OR (outcome = 'summary' AND result_summary_id IS NOT NULL)
                    OR (outcome = 'no_change' AND result_summary_id IS NULL)
                ),
                FOREIGN KEY(state_id) REFERENCES conversation_history_states(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(result_summary_id)
                    REFERENCES conversation_history_summaries(id) ON DELETE SET NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE INDEX ix_conversation_history_rollup_jobs_pending
            ON conversation_history_rollup_jobs (status, next_attempt_at)
            """
        )
        connection.exec_driver_sql(
            """
            CREATE INDEX ix_conversation_history_rollup_jobs_lease
            ON conversation_history_rollup_jobs (lease_until)
            """
        )


def downgrade() -> None:
    connection = op.get_bind()
    for table in reversed(_TABLES):
        connection.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
    for index_name in _EVENT_INDEXES:
        connection.exec_driver_sql(f"DROP INDEX IF EXISTS {index_name}")
