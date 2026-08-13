"""Destructively rebuild storage around people and a permanent event ledger.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-25
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

from qq_ai_bot.persistence.metadata import Base

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Discard pre-1.0 business data and create the person-centric schema."""

    bind = op.get_bind()
    op.execute("DROP TRIGGER IF EXISTS chat_events_fts_ai")
    op.execute("DROP TRIGGER IF EXISTS chat_events_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS chat_events_fts_au")
    op.execute("DROP TABLE IF EXISTS chat_events_fts")

    existing = set(inspect(bind).get_table_names())
    for table_name in (
        "user_group_profiles",
        "private_user_settings",
        "messages",
        "conversations",
        "group_memories",
        "group_settings",
        "processed_events",
        "user_profiles",
    ):
        if table_name in existing:
            op.drop_table(table_name)

    # Later revisions add tables to the shared ORM metadata. Keep this historical
    # migration deterministic so a fresh install does not create future tables early.
    v1_tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.name
        not in {
            "web_search_runs",
            "web_search_sources",
            "person_relationships",
            "relationship_events",
            "relationship_jobs",
            "runtime_config_overrides",
            "admin_operation_events",
            "media_analyses",
            "emoji_descriptions",
            "person_time_settings",
            "automations",
            "automation_versions",
            "automation_runs",
            "automation_step_runs",
            "planner_runs",
            "plugin_installations",
            "plugin_config_values",
            "plugin_state",
            "plugin_audit_events",
            "plugin_agent_sessions",
            "plugin_agent_messages",
            "emoji_assets",
            "emoji_scope_states",
            "emoji_jobs",
            "emoji_usage_events",
            "speech_voice_profiles",
            "speech_voice_references",
            "speech_generations",
            "person_speech_preferences",
            "model_invocations",
            "memory_facts",
            "memory_evidence",
            "memory_jobs",
            "mcp_server_states",
            "mcp_tool_cache",
            "tool_artifacts",
            "tool_invocations",
            "memory_embedding_profiles",
            "memory_embeddings",
            "memory_embedding_jobs",
            "memory_fact_relations",
            "memory_fact_state_events",
            "memory_rebuild_runs",
            "memory_rebuild_items",
            "memory_rebuild_proposals",
            "memory_mutation_receipts",
            "memory_reflection_jobs",
            "memory_claim_candidates",
            "memory_claim_candidate_evidence",
            "memory_tool_receipts",
            "memory_self_reflection_runtime",
            "memory_self_reflection_states",
            "memory_self_reflection_runs",
            "memory_self_reflection_results",
            "memory_dream_runtime",
            "memory_dream_runs",
            "memory_dream_clusters",
            "memory_dream_operations",
            "memory_dream_operation_sources",
            "memory_dream_operation_results",
            "memory_dream_fact_checkpoints",
            "memory_dream_cluster_previews",
            "memory_evidence_compaction_runs",
            "memory_evidence_compaction_items",
            "plugin_background_target_grants",
            "plugin_media_artifacts",
            "plugin_notification_outbox",
            "plugin_background_turn_jobs",
        }
    ]
    Base.metadata.create_all(bind=bind, tables=v1_tables, checkfirst=True)
    op.execute(
        """
        CREATE VIRTUAL TABLE chat_events_fts USING fts5(
            content,
            content='chat_events',
            content_rowid='id',
            tokenize='trigram'
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER chat_events_fts_ai AFTER INSERT ON chat_events BEGIN
            INSERT INTO chat_events_fts(rowid, content) VALUES (new.id, new.content);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER chat_events_fts_ad AFTER DELETE ON chat_events BEGIN
            INSERT INTO chat_events_fts(chat_events_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER chat_events_fts_au AFTER UPDATE OF content ON chat_events BEGIN
            INSERT INTO chat_events_fts(chat_events_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
            INSERT INTO chat_events_fts(rowid, content) VALUES (new.id, new.content);
        END
        """
    )


def downgrade() -> None:
    """Refuse a lossy downgrade after the intentional 1.0 reset."""

    raise RuntimeError("revision 0005 is an irreversible destructive data reset")
