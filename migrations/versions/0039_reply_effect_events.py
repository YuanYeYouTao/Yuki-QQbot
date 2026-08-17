"""Create reply_effect_events and backfill cadence from planner_runs.

3.6.0-R4: Conversation Runtime owns voice/emoji cadence.  One row per turn,
unique on (source, source_event_hash).  Runtime writes only after confirmed
delivery.  This revision backfills the last 20 eligible Planner reply rows
per conversation so cadence does not reset at cutover.

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect, text

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "reply_effect_events" not in tables:
        connection.exec_driver_sql(
            """
            CREATE TABLE reply_effect_events (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                conversation_key_hash VARCHAR(64) NOT NULL,
                runtime_turn_id VARCHAR(64),
                source_event_hash VARCHAR(64) NOT NULL,
                text_sent BOOLEAN NOT NULL DEFAULT 0,
                voice_sent BOOLEAN NOT NULL DEFAULT 0,
                emoji_sent BOOLEAN NOT NULL DEFAULT 0,
                voice_cadence_eligible BOOLEAN NOT NULL DEFAULT 1,
                voice_request_basis VARCHAR(32) NOT NULL DEFAULT 'none',
                source VARCHAR(32) NOT NULL,
                occurred_at DATETIME NOT NULL,
                recorded_at DATETIME NOT NULL,
                CONSTRAINT uq_reply_effect_events_source
                    UNIQUE (source, source_event_hash),
                CONSTRAINT ck_reply_effect_events_voice_request_basis
                    CHECK (voice_request_basis IN ('user_requested', 'agent_initiated', 'none')),
                CONSTRAINT ck_reply_effect_events_source
                    CHECK (source IN ('runtime', 'migrated_planner'))
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE INDEX ix_reply_effect_events_conversation_occurred
            ON reply_effect_events (conversation_key_hash, occurred_at, id)
            """
        )
    if "planner_runs" not in tables:
        return
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO reply_effect_events (
            conversation_key_hash,
            runtime_turn_id,
            source_event_hash,
            text_sent,
            voice_sent,
            emoji_sent,
            voice_cadence_eligible,
            voice_request_basis,
            source,
            occurred_at,
            recorded_at
        )
        SELECT
            conversation_key_hash,
            runtime_turn_id,
            lower(hex(id)),
            CASE WHEN voice_mode IN ('text', 'text_and_voice') THEN 1 ELSE 0 END,
            CASE WHEN voice_mode IN ('voice', 'text_and_voice', 'optional') THEN 1 ELSE 0 END,
            0,
            1,
            'none',
            'migrated_planner',
            COALESCE(finished_at, created_at),
            CURRENT_TIMESTAMP
        FROM (
            SELECT
                id,
                conversation_key_hash,
                runtime_turn_id,
                voice_mode,
                finished_at,
                created_at,
                ROW_NUMBER() OVER (
                    PARTITION BY conversation_key_hash
                    ORDER BY created_at DESC, id DESC
                ) AS rn
            FROM planner_runs
            WHERE planner_decision = 'reply'
              AND voice_intent = 'neutral'
        ) ranked
        WHERE rn <= 20
        """
    )


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_reply_effect_events_conversation_occurred"))
    op.execute(text("DROP TABLE IF EXISTS reply_effect_events"))
