"""Safe health response construction."""

from __future__ import annotations

import time
from typing import TypedDict

from qq_ai_bot import __version__
from qq_ai_bot.container import ApplicationContainer


class HealthPayload(TypedDict):
    """Public, credential-free health response."""

    status: str
    version: str
    database: str
    llm_configured: bool
    web_configured: bool
    vision_configured: bool
    onebot_connected: bool
    automation_enabled: bool
    automation_worker_running: bool
    active_automation_count: int
    plugin_system_enabled: bool
    plugin_running_count: int
    emoji_enabled: bool
    emoji_worker_running: bool
    emoji_asset_count: int
    emoji_pending_jobs: int
    speech_enabled: bool
    speech_worker_connected: bool
    speech_worker_ready: bool
    speech_japanese_frontend_available: bool | None
    speech_default_profile_loaded: bool
    speech_can_send_record: bool
    speech_queue_depth: int
    mcp_enabled: bool
    mcp_configured_servers: int
    mcp_connected_servers: int
    mcp_cached_tools: int
    mcp_automation_tools: int
    mcp_automation_missing_tools: int
    mcp_active_calls: int
    memory_embedding_enabled: bool
    memory_embedding_configured: bool
    memory_embedding_coverage: float
    memory_embedding_pending_jobs: int
    memory_embedding_failed_jobs: int
    memory_maintenance_running: bool
    memory_contested_facts: int
    memory_active_contested_facts: int
    memory_consistency_healthy: bool
    memory_expired_active_facts: int
    memory_classifier_recent_errors: int
    memory_maintenance_last_success_at: str | None
    memory_rebuild: dict[str, object]
    memory_self_reflection: dict[str, object]
    memory_dream: dict[str, object]
    conversation_rollup: dict[str, object]
    conversation_generation_superseded_effects: int
    conversation_prefix_shape_match_total: int
    conversation_prefix_shape_split_total: int
    uptime_seconds: int


async def build_health_payload(container: ApplicationContainer) -> HealthPayload:
    """Check dependencies without probing or billing the LLM provider."""

    database_ok = await container.database.ping()
    plugin_manager = getattr(container, "plugin_manager", None)
    plugin_running_count = int(getattr(plugin_manager, "running_count", 0))
    emoji_counts = await container.emoji_repository.counts()
    speech_health = await container.speech.health()
    speech_metrics = await container.speech.metrics()
    mcp_health = container.mcp_manager.health()
    embedding_health = await container.memory_embeddings.health()
    memory_health = await container.memory_audit.health()
    rebuild_health = await container.persistence.memory_rebuilds.health(
        enabled=container.settings.memory_rebuild_enabled,
        active_in_flight_calls=container.memory_rebuild_service.active_in_flight_calls,
    )
    self_reflection_health = await container.memory_self_reflection_worker.health()
    dream_health = await container.memory_dream_worker.health()
    rollup_health = await container.conversation_rollup_worker.health()
    prompt_shape_metrics = container.models.prompt_shape_metrics()
    return HealthPayload(
        status="ok" if database_ok else "degraded",
        version=__version__,
        database="ok" if database_ok else "unavailable",
        llm_configured=container.settings.llm_configured,
        web_configured=container.settings.web_configured,
        vision_configured=container.settings.vision_configured,
        onebot_connected=container.onebot_connected(),
        automation_enabled=container.settings.automation_enabled,
        automation_worker_running=container.automation_worker.running,
        active_automation_count=await container.automation_repository.active_count(),
        plugin_system_enabled=container.settings.plugin_system_enabled,
        plugin_running_count=plugin_running_count,
        emoji_enabled=container.settings.emoji_enabled,
        emoji_worker_running=(
            container.emoji_worker is not None and container.emoji_worker.running
        ),
        emoji_asset_count=sum(
            value for key, value in emoji_counts.items() if key != "jobs_pending"
        ),
        emoji_pending_jobs=emoji_counts.get("jobs_pending", 0),
        speech_enabled=container.settings.speech_enabled,
        speech_worker_connected=speech_health.connected,
        speech_worker_ready=speech_health.ready,
        speech_japanese_frontend_available=speech_health.japanese_frontend_available,
        speech_default_profile_loaded=(
            bool(container.settings.speech_default_profile)
            and speech_health.loaded_profile_id == container.settings.speech_default_profile
        ),
        speech_can_send_record=container.onebot_connected(),
        speech_queue_depth=speech_metrics.queue_depth,
        mcp_enabled=mcp_health.enabled,
        mcp_configured_servers=mcp_health.configured_servers,
        mcp_connected_servers=mcp_health.connected_servers,
        mcp_cached_tools=mcp_health.cached_tools,
        mcp_automation_tools=container.mcp_automation_bridge.registered_tool_count,
        mcp_automation_missing_tools=container.mcp_automation_bridge.missing_tool_count,
        mcp_active_calls=mcp_health.active_calls,
        memory_embedding_enabled=embedding_health.enabled,
        memory_embedding_configured=embedding_health.provider_configured,
        memory_embedding_coverage=embedding_health.coverage_ratio,
        memory_embedding_pending_jobs=embedding_health.pending_job_count,
        memory_embedding_failed_jobs=embedding_health.failed_job_count,
        memory_maintenance_running=container.memory_maintenance_worker.running,
        memory_contested_facts=memory_health.contested_fact_count,
        memory_active_contested_facts=memory_health.active_contested_count,
        memory_consistency_healthy=memory_health.healthy,
        memory_expired_active_facts=memory_health.expired_active_count,
        memory_classifier_recent_errors=memory_health.classifier_recent_errors,
        memory_maintenance_last_success_at=(
            memory_health.maintenance_last_success_at.isoformat()
            if memory_health.maintenance_last_success_at
            else None
        ),
        memory_rebuild=rebuild_health.model_dump(mode="json"),
        memory_self_reflection=self_reflection_health.model_dump(mode="json"),
        memory_dream=dream_health.model_dump(mode="json"),
        conversation_rollup=rollup_health,
        conversation_generation_superseded_effects=(
            container.conversation_effect_gate.superseded_rejections
        ),
        conversation_prefix_shape_match_total=prompt_shape_metrics[
            "conversation_prefix_shape_match_total"
        ],
        conversation_prefix_shape_split_total=prompt_shape_metrics[
            "conversation_prefix_shape_split_total"
        ],
        uptime_seconds=max(0, int(time.monotonic() - container.started_at)),
    )
