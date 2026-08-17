"""Schema v3 model-profile migration and retired task projection."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from qq_ai_bot.deployment_setup.migrate_3_6 import migrate_deployment_model_profiles
from qq_ai_bot.deployment_setup.service import SetupPaths
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.profiles import (
    MIGRATE_3_6_COMMAND,
    ModelRuntimeConfigurationError,
    load_model_profile_catalog,
)
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.persistence.database import Database


def _v2_document(*, include_attribution: bool = False, include_utility: bool = True) -> str:
    routes = [
        'chat_agent = "pro"',
        'planner = "flash"',
        'tool_selection = "flash"',
        'memory_extraction = "flash"',
        'memory_self_reflection = "flash"',
        'memory_consolidation = "flash"',
        'memory_dream = "flash"',
        'relationship_evaluation = "flash"',
        'emoji_replacement = "flash"',
        'automation_text_generation = "flash"',
        'automation_agent = "pro"',
        'plugin_agent_session = "pro"',
    ]
    if include_utility:
        routes.append('utility_structured = "flash"')
    if include_attribution:
        routes.append('memory_attribution = "pro"')
    route_text = "\n".join(routes)
    return f"""schema_version = 2

[profiles.pro]
provider = "openai_compatible"
base_url = "https://models.invalid/v1"
api_key_env = "PRO_KEY"
model = "pro-model"
timeout_seconds = 30
max_retries = 0
default_temperature = 0.1
default_max_output_tokens = 512
thinking_mode = "disabled"
structured_output_mode = "function_tool"
capabilities = ["tools", "structured_output"]

[profiles.flash]
provider = "openai_compatible"
base_url = "https://models.invalid/v1"
api_key_env = "FLASH_KEY"
model = "flash-model"
timeout_seconds = 10
max_retries = 0
default_temperature = 0.1
default_max_output_tokens = 256
thinking_mode = "disabled"
structured_output_mode = "function_tool"
capabilities = ["structured_output"]

[routes]
{route_text}
"""


def _load(path):
    return load_model_profile_catalog(
        path,
        legacy_provider="fake",
        legacy_base_url="",
        legacy_model="fake",
        legacy_timeout_seconds=10,
        legacy_max_retries=0,
        legacy_temperature=0.1,
        legacy_max_output_tokens=100,
        legacy_thinking_enabled=False,
        environment={},
    )


def test_unmigrated_schema_fail_fast_names_migrate_command(tmp_path) -> None:
    path = tmp_path / "legacy.toml"
    path.write_text(_v2_document(), encoding="utf-8")
    with pytest.raises(ModelRuntimeConfigurationError, match=MIGRATE_3_6_COMMAND):
        _load(path)


def test_v3_file_with_retired_routes_fail_fast(tmp_path) -> None:
    path = tmp_path / "stale.toml"
    path.write_text(
        _v2_document(include_attribution=True).replace("schema_version = 2", "schema_version = 3"),
        encoding="utf-8",
    )
    with pytest.raises(ModelRuntimeConfigurationError, match="retired model routes"):
        _load(path)


def test_migrate_3_6_materializes_attribution_from_utility_then_strips_planner(
    tmp_path,
) -> None:
    root = tmp_path / "deploy"
    (root / "config").mkdir(parents=True)
    (root / ".env").write_text("LLM_MODEL=pro-model\n", encoding="utf-8")
    (root / ".mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
    profiles = root / "config/model_profiles.toml"
    profiles.write_text(_v2_document(include_attribution=False), encoding="utf-8")
    paths = SetupPaths(root)

    result = migrate_deployment_model_profiles(paths)

    assert result.changed is True
    assert result.materialized_attribution_from == "utility_structured"
    assert result.removed_routes == ("planner", "tool_selection")
    assert result.backup is not None
    assert (result.backup / "config/model_profiles.toml").is_file()
    catalog = _load(profiles)
    assert catalog.routes[ModelTask.MEMORY_ATTRIBUTION].profile_id == "flash"
    assert ModelTask.CHAT_AGENT in catalog.routes
    assert "planner" not in profiles.read_text(encoding="utf-8")


def test_migrate_3_6_uses_planner_when_utility_structured_is_absent(tmp_path) -> None:
    root = tmp_path / "deploy"
    (root / "config").mkdir(parents=True)
    profiles = root / "config/model_profiles.toml"
    profiles.write_text(_v2_document(include_utility=False), encoding="utf-8")

    result = migrate_deployment_model_profiles(SetupPaths(root))

    assert result.materialized_attribution_from == "planner"
    catalog = _load(profiles)
    assert catalog.routes[ModelTask.MEMORY_ATTRIBUTION].profile_id == "flash"


@pytest.mark.asyncio
async def test_repository_projects_retired_planner_task(database: Database) -> None:
    async with database.sessions() as session:
        await session.execute(
            text(
                """
                INSERT INTO model_invocations (
                    task, profile_id, provider, model, success,
                    prompt_tokens, completion_tokens, total_tokens,
                    cached_prompt_tokens, latency_seconds, error_category, created_at
                ) VALUES (
                    'planner', 'flash', 'fake', 'old-planner', 0,
                    NULL, NULL, NULL, NULL, 0.2, 'RetiredTask', :created_at
                )
                """
            ),
            {"created_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat()},
        )
        await session.commit()

    repository = ModelInvocationRepository(database)
    by_task = await repository.stats_by_task()
    errors = await repository.recent_errors(limit=5)
    assert by_task["planner"].invocations == 1
    assert errors[0].task == "planner"
    assert errors[0].error_category == "RetiredTask"
