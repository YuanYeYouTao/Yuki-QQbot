"""Native web binding, configuration, and source recovery tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from qq_ai_bot.config import Settings
from qq_ai_bot.domain.messages import (
    CitationOrigin,
    NativeToolEvent,
    NativeToolStatus,
    NativeToolType,
    ResponseCitation,
)
from qq_ai_bot.model_runtime.models import ModelCapability, ModelProtocol
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.native_tool_binder import NativeToolBinder
from qq_ai_bot.web.models import WebMode
from qq_ai_bot.web.native_sources import recover_native_web_response


def test_web_mode_preserves_legacy_behavior_and_native_needs_no_tavily_key() -> None:
    assert Settings(_env_file=None, web_enabled=False).web.mode is WebMode.DISABLED
    with pytest.raises(ValidationError, match="TAVILY_API_KEY"):
        _legacy_tavily = Settings(_env_file=None, web_enabled=True, tavily_api_key="").web

    native = Settings(
        _env_file=None,
        web_enabled=False,
        web_mode=WebMode.NATIVE,
        tavily_api_key="",
    )
    assert native.web.mode is WebMode.NATIVE
    assert native.web_configured


def test_prefix_web_capabilities_follow_web_mode_only() -> None:
    disabled = SimpleNamespace(web=SimpleNamespace(mode=WebMode.DISABLED))
    native = SimpleNamespace(web=SimpleNamespace(mode=WebMode.NATIVE))
    hybrid = SimpleNamespace(web=SimpleNamespace(mode=WebMode.NATIVE_WITH_TAVILY_FALLBACK))
    tavily = SimpleNamespace(web=SimpleNamespace(mode=WebMode.TAVILY))

    assert ChatService._prefix_web_capabilities(disabled) == frozenset()  # type: ignore[arg-type]
    assert ChatService._prefix_web_capabilities(native) == frozenset({"web", "web_search"})  # type: ignore[arg-type]
    assert ChatService._prefix_web_capabilities(hybrid) == frozenset({"web", "web_search"})  # type: ignore[arg-type]
    assert ChatService._prefix_web_capabilities(tavily) == frozenset({"web", "web_search"})  # type: ignore[arg-type]


def test_native_binder_intersects_scope_mode_protocol_and_capability() -> None:
    binder = NativeToolBinder()
    supported = frozenset({ModelCapability.TOOLS, ModelCapability.NATIVE_WEB_SEARCH})
    bound = binder.bind(
        protocol=ModelProtocol.RESPONSES,
        capabilities=supported,
        allowed_capabilities=frozenset({"web"}),
        web_mode=WebMode.NATIVE,
        web_was_used=False,
    )
    assert [tool.type for tool in bound] == [NativeToolType.WEB_SEARCH]
    assert not binder.bind(
        protocol=ModelProtocol.RESPONSES,
        capabilities=supported,
        allowed_capabilities=frozenset({"memory"}),
        web_mode=WebMode.NATIVE,
        web_was_used=False,
    )
    assert not binder.bind(
        protocol=ModelProtocol.CHAT_COMPLETIONS,
        capabilities=supported,
        allowed_capabilities=frozenset({"web"}),
        web_mode=WebMode.NATIVE,
        web_was_used=False,
    )
    assert not binder.bind(
        protocol=ModelProtocol.RESPONSES,
        capabilities=frozenset({ModelCapability.TOOLS}),
        allowed_capabilities=frozenset({"web"}),
        web_mode=WebMode.NATIVE,
        web_was_used=False,
    )


def test_native_sources_prefer_annotations_then_completed_actions_then_text() -> None:
    response = recover_native_web_response(
        events=(
            NativeToolEvent(
                tool_type=NativeToolType.WEB_SEARCH,
                call_id="search-1",
                status=NativeToolStatus.COMPLETED,
                action_type="search",
                query="public docs",
            ),
            NativeToolEvent(
                tool_type=NativeToolType.WEB_SEARCH,
                call_id="open-failed",
                status=NativeToolStatus.FAILED,
                action_type="open_page",
                url="https://failed.example/page",
            ),
            NativeToolEvent(
                tool_type=NativeToolType.WEB_SEARCH,
                call_id="open-ok",
                status=NativeToolStatus.COMPLETED,
                action_type="open_page",
                url="https://example.com/docs#ws_call_id=provider",
            ),
        ),
        citations=(
            ResponseCitation(
                url="https://example.com/docs#citation",
                title="Official docs",
                origin=CitationOrigin.ANNOTATION,
            ),
        ),
        answer_text=(
            "See https://example.com/docs and https://github.com/example/project/blob/main/README.md."
        ),
    )

    assert response.query == "public docs"
    assert response.partial_failure
    assert [source.url for source in response.sources] == [
        "https://example.com/docs",
        "https://github.com/example/project/blob/main/README.md",
    ]
    assert response.sources[0].title == "Official docs"
    assert all("failed.example" not in source.url for source in response.sources)
