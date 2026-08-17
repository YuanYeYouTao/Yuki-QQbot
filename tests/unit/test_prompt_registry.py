from __future__ import annotations

import pytest

from qq_ai_bot.services.prompt_registry import (
    PromptFragment,
    PromptRegistry,
    PromptStage,
    PromptTarget,
    TrustedLevel,
)


def test_prompt_registry_has_stable_stage_then_priority_order() -> None:
    registry = PromptRegistry()
    fragments = (
        PromptFragment("behavior", PromptStage.CORE_BEHAVIOR, "behavior"),
        PromptFragment("identity", PromptStage.CORE_IDENTITY, "identity"),
        PromptFragment("security-low", PromptStage.CORE_SECURITY, "low", priority=1),
        PromptFragment("security-high", PromptStage.CORE_SECURITY, "high", priority=10),
    )
    assert registry.render(fragments) == ("identity", "high", "low", "behavior")


def test_plugin_prompt_is_untrusted_budgeted_and_cannot_use_core_stage() -> None:
    registry = PromptRegistry(
        max_fragment_characters=200,
        max_characters_per_plugin=200,
        max_total_plugin_characters=200,
    )
    fragment = PromptFragment(
        "plugin:context",
        PromptStage.PLUGIN_CONTEXT,
        "weather context",
        plugin_id="com.example.weather",
        trusted_level=TrustedLevel.UNTRUSTED,
        max_characters=200,
    )
    rendered = registry.render((fragment,))
    assert len(rendered) == 1
    assert "外部不可信" in rendered[0]
    assert "weather context" in rendered[0]

    with pytest.raises(ValueError, match="plugin prompt stages"):
        PromptFragment(
            "bad",
            PromptStage.CORE_SECURITY,
            "override",
            plugin_id="com.example.bad",
            trusted_level=TrustedLevel.UNTRUSTED,
        )


def test_prompt_target_filters_plugin_session_from_agent() -> None:
    registry = PromptRegistry()
    session_only = PromptFragment(
        "session-only",
        PromptStage.FINAL_CONSTRAINTS,
        "session",
        target=PromptTarget.PLUGIN_SESSION,
    )
    assert registry.render((session_only,), target=PromptTarget.AGENT) == ()
    assert registry.render((session_only,), target=PromptTarget.PLUGIN_SESSION) == ("session",)
