"""Apply Planner-authorized persistent voice preferences."""

from __future__ import annotations

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.speech.models import (
    VoicePreferenceDuration,
    VoicePreferenceMode,
    VoiceReplyPlan,
)
from qq_ai_bot.speech.preference_repository import (
    PersonSpeechPreference,
    VoicePreferenceRepository,
)


class VoicePreferenceService:
    """Persist only explicit, person-authored, future-facing mode changes."""

    def __init__(self, repository: VoicePreferenceRepository) -> None:
        self._repository = repository

    async def current_mode(self, user_id: str) -> VoicePreferenceMode | None:
        record = await self._repository.get(user_id)
        return None if record is None else record.mode

    async def apply(
        self,
        voice: VoiceReplyPlan,
        *,
        user_id: str,
        source_message_id: str,
        origin: TurnOrigin,
    ) -> PersonSpeechPreference | None:
        change = voice.preference_change
        if (
            change is None
            or change.duration is not VoicePreferenceDuration.PERSISTENT
            or origin is not TurnOrigin.USER_MESSAGE
        ):
            return None
        return await self._repository.set(
            user_id,
            change.mode,
            source_message_id=source_message_id,
        )

    async def set_persistent(
        self,
        *,
        user_id: str,
        mode: VoicePreferenceMode,
        source_message_id: str,
        origin: TurnOrigin,
    ) -> PersonSpeechPreference | None:
        """Write a long-lived preference only from a real user-message tool call."""

        if origin is not TurnOrigin.USER_MESSAGE:
            return None
        return await self._repository.set(
            user_id,
            mode,
            source_message_id=source_message_id,
        )


__all__ = ["VoicePreferenceService"]
