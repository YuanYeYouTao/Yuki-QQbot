"""Bind provider-native tools only from backend-authorized runtime state."""

from __future__ import annotations

import logging

from qq_ai_bot.domain.messages import NativeToolDefinition, NativeToolType
from qq_ai_bot.model_runtime.models import ModelCapability, ModelProtocol
from qq_ai_bot.web.models import WebMode

logger = logging.getLogger(__name__)


class NativeToolBinder:
    """Small provider-neutral policy intersection for native tools."""

    def bind(
        self,
        *,
        protocol: ModelProtocol,
        capabilities: frozenset[ModelCapability],
        allowed_capabilities: frozenset[str],
        web_mode: WebMode,
        web_was_used: bool,
    ) -> tuple[NativeToolDefinition, ...]:
        del web_was_used
        web_approved = bool({"web", "web_search"}.intersection(allowed_capabilities))
        if not web_approved or web_mode in {WebMode.DISABLED, WebMode.TAVILY}:
            return ()
        if protocol not in {ModelProtocol.RESPONSES, ModelProtocol.CHAT_COMPLETIONS}:
            logger.warning(
                "native_tool_binding_skipped reason=protocol web_mode=%s protocol=%s",
                web_mode.value,
                protocol.value,
            )
            return ()
        if protocol is ModelProtocol.CHAT_COMPLETIONS and ModelCapability.TOOLS in capabilities:
            # Chat search models cannot honor the Main Agent's fixed function contract.
            return ()
        if ModelCapability.NATIVE_WEB_SEARCH not in capabilities:
            logger.warning(
                "native_tool_binding_skipped reason=capability web_mode=%s protocol=%s",
                web_mode.value,
                protocol.value,
            )
            return ()
        return (NativeToolDefinition(type=NativeToolType.WEB_SEARCH),)
