"""Resolve a safe Genie target language from voice intent and actual reply text."""

from __future__ import annotations

import re

from qq_ai_bot.speech.models import SpeechLanguageHint, VoiceProfile

_KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")
_PARENTHETICAL = re.compile(r"[（(]([^（）()]*)[）)]")
_CHINESE_G2P_COMPATIBILITY = str.maketrans(
    {
        # Genie-TTS 2.0.2 can leave an empty final for this interjection and
        # crash ToneSandhi while indexing it. The homophone keeps the intended
        # short vocalization without coupling the bot to Genie internals.
        "嗯": "恩",
    }
)


def resolve_target_language(profile: VoiceProfile, text: str, hint: str = "auto") -> str:
    """Prefer strong script evidence, then a validated language hint, then profile default."""

    supported = set(profile.supported_languages) or {profile.language}
    detected = _detected_language(text)
    if detected is not None and detected in supported:
        return detected
    try:
        requested = SpeechLanguageHint(hint).value
    except ValueError:
        requested = SpeechLanguageHint.AUTO.value
    if requested != SpeechLanguageHint.AUTO.value and requested in supported:
        return requested
    return profile.language


def _detected_language(text: str) -> str | None:
    if _KANA.search(text):
        return "jp"
    if _CJK.search(text):
        return "zh"
    if _LATIN.search(text):
        return "en"
    return None


def language_fallback_text(text: str, target_language: str) -> str:
    """Project bilingual model text onto a locally available speech language."""

    if target_language != "zh":
        return ""
    selected: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not _KANA.search(stripped) and _CJK.search(stripped):
            selected.append(stripped)
            continue
        selected.extend(
            value.strip()
            for value in _PARENTHETICAL.findall(stripped)
            if value.strip() and _CJK.search(value) and not _KANA.search(value)
        )
    return "\n".join(dict.fromkeys(selected))


def prepare_text_for_language(text: str, target_language: str) -> str:
    """Apply small, versioned compatibility substitutions at the TTS boundary."""

    if target_language == "zh":
        return text.translate(_CHINESE_G2P_COMPATIBILITY)
    return text
