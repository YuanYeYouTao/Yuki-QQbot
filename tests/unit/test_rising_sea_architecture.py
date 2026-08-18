"""Focused tests for the reusable 1.9 architecture boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.models import (
    AuthorityContext,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from qq_ai_bot.capabilities.policy import CapabilityPolicyContext, CapabilityPolicyEngine
from qq_ai_bot.cli import _prompt_comparison, _prompt_diagnostic
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.reply import ReplyEffect
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTool,
    ReasoningEffort,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.emoji.models import EmojiPlacement, EmojiReplyMode, PendingReplyEffect
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelRoute,
    ModelTask,
    StructuredOutputMode,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import (
    ModelProfileCatalog,
    ModelRuntimeConfigurationError,
    load_model_profile_catalog,
)
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.model_runtime.structured import StructuredTaskError, StructuredTaskRunner
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.prompting import (
    PromptChannel,
    PromptCompiler,
    PromptContribution,
    PromptProgram,
    PromptStability,
    PromptTrust,
)
from qq_ai_bot.speech.reply_effect import PendingVoiceReplyEffect


def _profile_document(routes: dict[str, str] | None = None) -> str:
    route_values = routes or {task.value: "pro" for task in ModelTask}
    route_text = "\n".join(f'{name} = "{profile}"' for name, profile in route_values.items())
    return (
        "schema_version = 3\n"
        "[profiles.pro]\n"
        'provider = "openai_compatible"\n'
        'base_url_env = "PRO_URL"\n'
        'api_key_env = "PRO_KEY"\n'
        'model_env = "PRO_MODEL"\n'
        "timeout_seconds = 30\n"
        "max_retries = 0\n"
        "default_temperature = 0.1\n"
        "default_max_output_tokens = 512\n"
        'thinking_mode = "disabled"\n'
        'structured_output_mode = "function_tool"\n'
        'capabilities = ["tools", "structured_output", "reasoning", "long_context"]\n'
        "[routes]\n"
        f"{route_text}\n"
    )


def _load_catalog(path: Path, *, environment: dict[str, str] | None = None) -> Any:
    resolved_environment = {
        "PRO_URL": "https://models.invalid/v1",
        "PRO_MODEL": "test-model",
        **(environment or {}),
    }
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
        environment=resolved_environment,
    )


def test_model_profile_resolves_optional_reasoning_effort_from_environment(
    tmp_path: Path,
) -> None:
    profile_path = tmp_path / "reasoning.toml"
    profile_path.write_text(
        _profile_document().replace(
            'thinking_mode = "disabled"',
            'thinking_mode = "configurable"\nreasoning_effort_env = "PRO_REASONING"',
        ),
        encoding="utf-8",
    )

    catalog = _load_catalog(profile_path, environment={"PRO_REASONING": "max"})

    assert catalog.profiles["pro"].reasoning_effort is ReasoningEffort.MAX


def test_model_profiles_require_every_explicit_route(tmp_path: Path) -> None:
    complete = tmp_path / "complete.toml"
    complete.write_text(_profile_document(), encoding="utf-8")
    catalog = _load_catalog(complete)
    assert set(catalog.routes) == set(ModelTask)
    assert catalog.routes[ModelTask.CHAT_AGENT].profile_id == "pro"

    incomplete = tmp_path / "incomplete.toml"
    incomplete.write_text(
        _profile_document({ModelTask.CHAT_AGENT.value: "pro"}),
        encoding="utf-8",
    )
    with pytest.raises(ModelRuntimeConfigurationError, match="invalid model profile"):
        _load_catalog(incomplete)


def test_legacy_profiles_route_attribution_to_utility_structured(tmp_path: Path) -> None:
    profile_path = tmp_path / "legacy.toml"
    routes = {task.value: "pro" for task in ModelTask if task is not ModelTask.MEMORY_ATTRIBUTION}
    profile_path.write_text(_profile_document(routes), encoding="utf-8")

    catalog = _load_catalog(profile_path)

    assert (
        catalog.routes[ModelTask.MEMORY_ATTRIBUTION].profile_id
        == catalog.routes[ModelTask.UTILITY_STRUCTURED].profile_id
    )


def test_prompt_benchmark_meets_declared_reduction_targets() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="fake",
        llm_model="fake",
        model_profiles_file=Path("__no_model_profiles__.toml"),
    )
    comparison = _prompt_comparison(settings)
    scenarios = comparison["scenarios"]
    assert isinstance(scenarios, dict)
    assert scenarios["direct-text"]["character_reduction_percent"] >= 30
    autonomous = _prompt_diagnostic(settings, "autonomous-group")
    mention = _prompt_diagnostic(settings, "group-mention")
    assert autonomous["route_task"] == ModelTask.CHAT_AGENT.value
    assert "core.contract" in autonomous["contribution_ids"]
    assert "core.persona" in autonomous["contribution_ids"]
    assert autonomous["history_characters"] == mention["history_characters"]
    assert autonomous["current_message_characters"] == mention["current_message_characters"]


@pytest.mark.asyncio
async def test_model_client_pool_reuses_matching_endpoint_and_secret_source() -> None:
    common = {
        "provider": "openai_compatible",
        "base_url": "https://models.invalid/v1",
        "api_key_env": "MODEL_KEY",
        "timeout_seconds": 10,
        "max_retries": 0,
        "default_temperature": 0.1,
        "default_max_output_tokens": 100,
        "structured_output_mode": StructuredOutputMode.FUNCTION_TOOL,
        "capabilities": frozenset(ModelCapability),
    }
    first = ModelProfile(id="first", model="pro", **common)
    second = ModelProfile(id="second", model="flash", **common)
    pool = ModelClientPool(secret_overrides={"MODEL_KEY": "test-secret"})
    assert pool.get(first) is not pool.get(second)
    assert pool.connection_pool_count == 1
    await pool.close()


@pytest.mark.asyncio
async def test_model_invocation_stats_group_usage_without_content(database: Database) -> None:
    repository = ModelInvocationRepository(database)
    await repository.record(
        task=ModelTask.CHAT_AGENT,
        profile_id="pro",
        provider="fake",
        model="pro-model",
        success=True,
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        cached_prompt_tokens=40,
        latency_seconds=0.2,
        error_category=None,
    )
    await repository.record(
        task=ModelTask.UTILITY_STRUCTURED,
        profile_id="flash",
        provider="fake",
        model="flash-model",
        success=False,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        cached_prompt_tokens=None,
        latency_seconds=0.1,
        error_category="SyntheticError",
    )
    stats = await repository.stats()
    assert stats.invocations == 2
    assert stats.total_tokens == 120
    assert stats.cached_prompt_tokens == 40
    assert stats.unknown_usage == 1
    assert set(await repository.stats_by_profile()) == {"pro", "flash"}
    assert (await repository.recent_errors(limit=5))[0].error_category == "SyntheticError"


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: int


class _StructuredExecutor:
    def __init__(self, response: ChatResponse, mode: StructuredOutputMode) -> None:
        self.response = response
        self.mode = mode
        self.requests: list[ChatRequest] = []

    async def execute(self, task: ModelTask, request: ChatRequest) -> ChatResponse:
        assert task is ModelTask.UTILITY_STRUCTURED
        self.requests.append(request)
        return self.response

    def model_name(self, task: ModelTask) -> str:
        assert task is ModelTask.UTILITY_STRUCTURED
        return "flash"

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        assert task is ModelTask.UTILITY_STRUCTURED
        return self.mode


class _SequencedStructuredExecutor(_StructuredExecutor):
    def __init__(
        self,
        responses: tuple[ChatResponse, ...],
        mode: StructuredOutputMode,
    ) -> None:
        super().__init__(responses[-1], mode)
        self.responses = list(responses)

    async def execute(self, task: ModelTask, request: ChatRequest) -> ChatResponse:
        assert task is ModelTask.UTILITY_STRUCTURED
        self.requests.append(request)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_structured_runner_uses_schema_channel_and_rejects_extra_text() -> None:
    executor = _StructuredExecutor(
        ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(
                ToolCall(
                    id="1",
                    function=ToolFunction(name="emit_result", arguments='{"value":7}'),
                ),
            ),
        ),
        StructuredOutputMode.FUNCTION_TOOL,
    )
    result = await StructuredTaskRunner(executor).run(
        task=ModelTask.UTILITY_STRUCTURED,
        instruction="Return one result.",
        structured_input={"input": 1},
        output_model=_Output,
        temperature=0,
        max_output_tokens=100,
    )
    assert result.value == 7
    assert executor.requests[0].tools[0].name == "emit_result"
    assert executor.requests[0].structured_output

    invalid = _StructuredExecutor(
        ChatResponse(content='prefix {"value":7}', latency_seconds=0),
        StructuredOutputMode.TEXT_JSON,
    )
    with pytest.raises(StructuredTaskError):
        await StructuredTaskRunner(invalid).run(
            task=ModelTask.UTILITY_STRUCTURED,
            instruction="Return one result.",
            structured_input={},
            output_model=_Output,
            temperature=0,
            max_output_tokens=100,
            allow_text_json=True,
        )


@pytest.mark.asyncio
async def test_structured_runner_repairs_one_invalid_function_result() -> None:
    executor = _SequencedStructuredExecutor(
        (
            ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="invalid",
                        function=ToolFunction(name="emit_result", arguments='{"value":'),
                    ),
                ),
            ),
            ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="repaired",
                        function=ToolFunction(name="emit_result", arguments='{"value":7}'),
                    ),
                ),
            ),
        ),
        StructuredOutputMode.FUNCTION_TOOL,
    )

    result = await StructuredTaskRunner(executor).run(
        task=ModelTask.UTILITY_STRUCTURED,
        instruction="Return one result.",
        structured_input={"input": 1},
        output_model=_Output,
        validation_retries=1,
        validation_repair_hint="Keep the value field as an integer.",
    )

    assert result.value == 7
    assert len(executor.requests) == 2
    assert executor.requests[1].messages[:2] == executor.requests[0].messages
    assert "previous_invalid_result" in (executor.requests[1].messages[2].content or "")
    assert "Keep the value field as an integer." in (executor.requests[1].messages[2].content or "")


@pytest.mark.asyncio
async def test_structured_runner_reports_exhausted_validation_reason() -> None:
    invalid = ChatResponse(
        content="",
        latency_seconds=0,
        tool_calls=(
            ToolCall(
                id="invalid",
                function=ToolFunction(name="emit_result", arguments='{"other":7}'),
            ),
        ),
    )
    executor = _SequencedStructuredExecutor(
        (invalid, invalid),
        StructuredOutputMode.FUNCTION_TOOL,
    )

    with pytest.raises(StructuredTaskError) as captured:
        await StructuredTaskRunner(executor).run(
            task=ModelTask.UTILITY_STRUCTURED,
            instruction="Return one result.",
            structured_input={},
            output_model=_Output,
            validation_retries=1,
        )

    assert captured.value.reason_code == "schema_validation"
    assert captured.value.attempts == 2
    assert "value:missing" in captured.value.detail


class _CapturingModelProvider:
    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return ChatResponse(content='{"value":7}', latency_seconds=0)


class _SingleProviderPool:
    def __init__(self, provider: _CapturingModelProvider) -> None:
        self.provider = provider

    def get(self, profile: ModelProfile) -> _CapturingModelProvider:
        assert profile.id == "flash"
        return self.provider


@pytest.mark.asyncio
async def test_executor_applies_profile_reasoning_effort_when_thinking_is_enabled() -> None:
    profile = ModelProfile(
        id="flash",
        provider="fake",
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_retries=0,
        default_temperature=0,
        default_max_output_tokens=100,
        thinking_enabled=True,
        reasoning_effort=ReasoningEffort.MAX,
        capabilities=frozenset({ModelCapability.REASONING}),
    )
    catalog = ModelProfileCatalog(
        profiles={"flash": profile},
        routes={
            task: ModelRoute(task=task, profile_id="flash", required_capabilities=frozenset())
            for task in ModelTask
        },
    )
    provider = _CapturingModelProvider()
    executor = TaskModelExecutor(
        router=ModelRouter(catalog),
        pool=_SingleProviderPool(provider),  # type: ignore[arg-type]
    )

    await executor.execute(
        ModelTask.CHAT_AGENT,
        ChatRequest(messages=(ChatMessage(role="user", content="hello"),)),
    )

    assert provider.requests[0].thinking_enabled is True
    assert provider.requests[0].reasoning_effort is ReasoningEffort.MAX


@pytest.mark.asyncio
async def test_structured_function_channel_does_not_require_agent_tool_capability() -> None:
    profile = ModelProfile(
        id="flash",
        provider="fake",
        model="flash",
        timeout_seconds=10,
        max_retries=0,
        default_temperature=0,
        default_max_output_tokens=100,
        structured_output_mode=StructuredOutputMode.FUNCTION_TOOL,
        capabilities=frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    )
    catalog = ModelProfileCatalog(
        profiles={"flash": profile},
        routes={
            task: ModelRoute(
                task=task,
                profile_id="flash",
                required_capabilities=(
                    frozenset({ModelCapability.STRUCTURED_OUTPUT})
                    if task is ModelTask.UTILITY_STRUCTURED
                    else frozenset()
                ),
            )
            for task in ModelTask
        },
    )
    provider = _CapturingModelProvider()
    executor = TaskModelExecutor(
        router=ModelRouter(catalog),
        pool=_SingleProviderPool(provider),  # type: ignore[arg-type]
    )
    schema_tool = ChatTool(name="emit_result", description="result", parameters={})

    await executor.execute(
        ModelTask.UTILITY_STRUCTURED,
        ChatRequest(
            messages=(ChatMessage(role="user", content="{}"),),
            tools=(schema_tool,),
            structured_output=True,
        ),
    )

    assert provider.requests[0].structured_output
    with pytest.raises(ValueError, match="does not support: tools"):
        await executor.execute(
            ModelTask.UTILITY_STRUCTURED,
            ChatRequest(
                messages=(ChatMessage(role="user", content="call a business tool"),),
                tools=(schema_tool,),
            ),
        )


def test_prompt_compiler_keeps_stable_prefix_and_required_turn_context() -> None:
    static = PromptContribution(
        id="core",
        channel=PromptChannel.PERSONA,
        trust=PromptTrust.TRUSTED,
        priority=100,
        stability=PromptStability.STATIC,
        content="stable",
        required=True,
    )
    current = PromptContribution(
        id="current",
        channel=PromptChannel.CONTEXT,
        trust=PromptTrust.UNTRUSTED,
        priority=100,
        payload={"message": "current"},
        required=True,
    )
    optional = PromptContribution(
        id="optional",
        channel=PromptChannel.CONTEXT,
        trust=PromptTrust.UNTRUSTED,
        priority=1,
        payload={"large": "x" * 100},
    )
    compiler = PromptCompiler()
    first = compiler.compile(
        PromptProgram(contributions=(static, current, optional)),
        dynamic_character_budget=40,
    )
    second = compiler.compile(
        PromptProgram(contributions=(static, current)),
        dynamic_character_budget=40,
    )
    assert first.metrics.stable_prefix_hash == second.metrics.stable_prefix_hash
    assert [item.id for item in first.selected] == ["core", "current"]


def test_prompt_compiler_attaches_dynamic_context_to_current_message() -> None:
    compiler = PromptCompiler()
    compiled = compiler.compile(
        PromptProgram(
            contributions=(
                PromptContribution(
                    id="core",
                    channel=PromptChannel.PERSONA,
                    trust=PromptTrust.CORE,
                    priority=100,
                    stability=PromptStability.STATIC,
                    content="stable",
                    required=True,
                ),
                PromptContribution(
                    id="runtime.time",
                    channel=PromptChannel.RUNTIME,
                    trust=PromptTrust.TRUSTED,
                    priority=100,
                    payload={"local": "dynamic"},
                    required=True,
                ),
            )
        ),
        history=(
            ChatMessage(role="user", content="past user"),
            ChatMessage(role="assistant", content="past assistant"),
        ),
        current_message=ChatMessage(role="user", content="current user"),
    )

    assert [message.role for message in compiled.messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert compiled.messages[0].content == "stable"
    assert compiled.messages[1].content == "past user"
    assert compiled.messages[2].content == "past assistant"
    current = compiled.messages[3].content or ""
    assert current.endswith("current user")
    assert '"id":"runtime.time"' in current
    assert compiled.metrics.history_characters == len("past userpast assistant")
    assert compiled.metrics.current_message_characters == len("current user")


def _descriptor(name: str, effect: CapabilityEffect) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        canonical_name=f"test.{name}",
        model_name=name,
        group="memory",
        namespace="memory.test",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        effect=effect,
        risk=(
            CapabilityRisk.READ if effect is CapabilityEffect.READ_STATE else CapabilityRisk.MUTATE
        ),
        trust_source=CapabilityTrustSource.CORE,
        allowed_origins=frozenset({TurnOrigin.USER_MESSAGE}),
        required_permissions=frozenset(),
        uses_external_data=False,
        cancellable=True,
        idempotency=CapabilityIdempotency.IDEMPOTENT,
    )


def test_capability_policy_uses_effect_metadata_not_tool_name() -> None:
    renamed_read = _descriptor("a_completely_new_name", CapabilityEffect.READ_STATE)
    renamed_write = _descriptor("another_new_name", CapabilityEffect.WRITE_STATE)
    visible = CapabilityPolicyEngine().visible(
        (renamed_read, renamed_write),
        CapabilityPolicyContext(
            authority=AuthorityContext(actor_user_id="1", is_superuser=False),
            origin=TurnOrigin.USER_MESSAGE,
            read_only=True,
        ),
    )
    assert visible == (renamed_read,)


@pytest.mark.asyncio
async def test_lifecycle_starts_in_order_and_closes_all_in_reverse() -> None:
    events: list[str] = []
    lifecycle = LifecycleRegistry()

    async def mark(value: str) -> None:
        events.append(value)

    lifecycle.register(
        "first", start=lambda: mark("start:first"), close=lambda: mark("close:first")
    )
    lifecycle.register(
        "second", start=lambda: mark("start:second"), close=lambda: mark("close:second")
    )
    await lifecycle.start()
    await lifecycle.close()
    assert events == ["start:first", "start:second", "close:second", "close:first"]


def test_flat_environment_is_composed_into_immutable_domain_settings() -> None:
    settings = Settings(_env_file=None, llm_provider="fake", llm_model="fake")
    assert settings.model_runtime.llm_model == "fake"
    assert settings.conversation.max_context_characters == settings.max_context_characters
    with pytest.raises(ValidationError):
        settings.model_runtime.llm_model = "changed"  # type: ignore[misc]


def test_emoji_and_voice_share_reply_effect_contract() -> None:
    emoji = PendingReplyEffect(
        mode=EmojiReplyMode.EMOJI_ONLY,
        placement=EmojiPlacement.ONLY,
        source="agent",
    )
    voice = PendingVoiceReplyEffect(source="agent")
    assert isinstance(emoji, ReplyEffect)
    assert isinstance(voice, ReplyEffect)
    assert {emoji.kind, voice.kind} == {"emoji", "voice"}
