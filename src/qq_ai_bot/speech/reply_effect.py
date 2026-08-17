"""Resolve Planner voice intent into the existing outbound media pipeline."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.messages import AttachmentKind, InboundMessage, OutboundMedia, OutboundMessage
from qq_ai_bot.services.plugin_events import LifecycleEventPublisher, publish_notification
from qq_ai_bot.services.turn_coordinator import TurnSupersededError, TurnToken
from qq_ai_bot.speech.genie_client import GenieWorkerFailure, GenieWorkerUnavailable
from qq_ai_bot.speech.models import VoiceMode
from qq_ai_bot.speech.provider import SpeechSynthesisRequest
from qq_ai_bot.speech.service import (
    SpeechQueueFullError,
    SpeechService,
    SpeechUnavailableError,
)
from yuki_plugin_sdk.events import EventName

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VoiceReplyEffect:
    generation_id: int
    profile_id: str
    reference_key: str
    relative_path: str
    duration_milliseconds: int
    mode: VoiceMode


@dataclass(frozen=True, slots=True)
class PendingVoiceReplyEffect:
    """A path-free voice request queued by an Agent tool or plugin."""

    kind: Literal["voice"] = "voice"
    profile_id: str = ""
    style_hint: str = ""
    language_hint: str = "auto"
    mode: VoiceMode = VoiceMode.OPTIONAL
    request_basis: str = "none"
    source: str = "plugin"


@dataclass(frozen=True, slots=True)
class PreparedVoiceReply:
    effect: VoiceReplyEffect
    message: OutboundMessage
    suppress_text: bool


class VoiceReplyEffectService:
    def __init__(
        self,
        speech: SpeechService,
        *,
        event_publisher: LifecycleEventPublisher | None = None,
        bot_display_name: str = "Yuki",
        bot_voice_name: str = "ゆき",
    ) -> None:
        self._speech = speech
        self._event_publisher = event_publisher
        self._bot_voice_name = bot_voice_name
        self._spoken_bot_name = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(bot_display_name)}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )

    def set_event_publisher(self, publisher: LifecycleEventPublisher) -> None:
        self._event_publisher = publisher

    def spoken_text(self, response_text: str) -> str:
        """Map the display name to its pronunciation without changing text replies."""

        return self._spoken_bot_name.sub(self._bot_voice_name, response_text)

    async def prepare(
        self,
        *,
        inbound: InboundMessage,
        response_text: str,
        runtime: RuntimeConfigSnapshot,
        token: TurnToken,
        mode: VoiceMode,
        style_hint: str,
        language_hint: str = "auto",
        profile_id: str = "",
    ) -> PreparedVoiceReply | None:
        if mode is VoiceMode.TEXT:
            return None
        scope_enabled = (
            runtime.speech.private_enabled
            if inbound.group_id is None
            else runtime.speech.group_enabled
        )
        if not scope_enabled:
            return None
        speech_text = self.spoken_text(response_text)
        cancellation = asyncio.Event()
        try:
            generated = await self._speech.synthesize(
                SpeechSynthesisRequest(
                    request_id=str(uuid4()),
                    profile_id=profile_id or runtime.speech.default_profile,
                    style_hint=style_hint,
                    text=speech_text,
                    split_sentence=runtime.speech.split_sentence,
                    conversation_key=token.conversation_key,
                    trigger_event_id=None,
                    turn_token=token,
                    language_hint=language_hint,
                ),
                runtime=runtime.speech,
                cancellation=cancellation,
            )
            path = self._speech.audio_path(generated)
        except (
            ValueError,
            LookupError,
            SpeechUnavailableError,
            SpeechQueueFullError,
            GenieWorkerUnavailable,
            GenieWorkerFailure,
            TurnSupersededError,
            OSError,
        ) as exc:
            error_code = exc.code.value if isinstance(exc, GenieWorkerFailure) else ""
            logger.warning(
                "voice_reply_prepare_failed error_category=%s error_code=%s",
                type(exc).__name__,
                error_code,
            )
            return None
        effect = VoiceReplyEffect(
            generation_id=generated.generation_id,
            profile_id=generated.profile_id,
            reference_key=generated.reference_key,
            relative_path=generated.relative_path,
            duration_milliseconds=generated.duration_milliseconds,
            mode=mode,
        )
        voice_only = mode in {VoiceMode.VOICE, VoiceMode.OPTIONAL}
        return PreparedVoiceReply(
            effect=effect,
            message=OutboundMessage(
                media=(
                    OutboundMedia(
                        kind=AttachmentKind.AUDIO,
                        mime_type="audio/wav",
                        summary="语音消息",
                        local_path=str(path),
                        spoken_text=speech_text if voice_only else "",
                        generation_id=generated.generation_id,
                        voice_profile_id=generated.profile_id,
                        voice_reference_key=generated.reference_key,
                        voice_language=generated.target_language,
                        duration_milliseconds=generated.duration_milliseconds,
                    ),
                )
            ),
            suppress_text=voice_only,
        )

    async def record_success(self, message: OutboundMessage) -> None:
        for media in message.media:
            if media.kind is AttachmentKind.AUDIO and media.generation_id is not None:
                await self._speech.mark_sent(media.generation_id)

    async def record_failure(self, message: OutboundMessage) -> None:
        for media in message.media:
            if media.kind is AttachmentKind.AUDIO and media.generation_id is not None:
                await publish_notification(
                    self._event_publisher,
                    EventName.SPEECH_SEND_FAILED,
                    {"generation_id": media.generation_id},
                )
