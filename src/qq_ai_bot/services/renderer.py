"""Safe plain-text cleanup and Unicode-preserving QQ message splitting."""

from __future__ import annotations

import re

from qq_ai_bot.llm.base import LLMEmptyResponseError

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)]\(((?:https?://|mailto:)[^)]+)\)")
_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
_HORIZONTAL_RULE = re.compile(r"(?m)^\s*[-*_]{3,}\s*$")
_INTERNAL_HISTORY_MARKER = re.compile(
    r"\[(?:(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]) )?"
    r"(?:[01]\d|2[0-3]):[0-5]\d(?: QQ [1-9]\d{4,19})?\]\s*"
)
_EVENT_IDENTITY_MARKER = re.compile(
    r"\[发送者:[^\]\r\n]{1,128}\|QQ:[^|\]\r\n]{1,64}"
    r"(?:\|消息:[^|\]\r\n]{1,128})?(?:\|时间:[^|\]\r\n]{1,128})?"
    r"(?:\|回复:[^|\]\r\n]{1,256})?(?:\|提及:[^\]\r\n]{1,512})?\]\s*"
)
_MAIN_AGENT_IDENTITY_MARKER = re.compile(
    r"\[[^\]\r\n]{1,128}\|QQ:[1-9]\d{4,19}\][ \t]*(?:\n[ \t]*)?"
)
_MAIN_AGENT_EVENT_PREFIX = re.compile(r"(?m)^[ \t]*#\d{1,19}(?:\|[^>\r\n]{1,768})?>[ \t]*")
_BLOCKQUOTE_PREFIX = re.compile(r"(?m)^[ \t]*>[ \t]+")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?；;])")


def sanitize_input(text: str) -> str:
    """Normalize line endings and remove unsafe control characters."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _CONTROL_CHARACTERS.sub("", normalized)
    return "\n".join(line.rstrip() for line in normalized.splitlines()).strip()


def strip_internal_history_markers(text: str) -> str:
    """Remove model-only annotations from generated or persisted chat text."""

    cleaned = _INTERNAL_HISTORY_MARKER.sub("", text)
    cleaned = _EVENT_IDENTITY_MARKER.sub("", cleaned)
    cleaned = _MAIN_AGENT_IDENTITY_MARKER.sub("", cleaned)
    cleaned = _MAIN_AGENT_EVENT_PREFIX.sub("", cleaned)
    return _BLOCKQUOTE_PREFIX.sub("", cleaned)


def clean_model_output(text: str, *, max_characters: int) -> str:
    """Validate model text and remove backend-only history annotations."""

    cleaned = sanitize_input(text)
    if not cleaned:
        raise LLMEmptyResponseError("model returned empty content")
    # Identity envelopes belong only to model input. Treat an echoed envelope as
    # an internal annotation wherever it appears, never as ordinary QQ text.
    cleaned = strip_internal_history_markers(cleaned)
    cleaned = _MARKDOWN_LINK.sub(r"\1 (\2)", cleaned)
    cleaned = _HEADING.sub("", cleaned)
    cleaned = _HORIZONTAL_RULE.sub("", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        raise LLMEmptyResponseError("model returned empty content")
    return cleaned[:max_characters]


def _split_hard(text: str, limit: int) -> list[str]:
    return [text[index : index + limit] for index in range(0, len(text), limit)]


def _split_sentence(text: str, limit: int) -> list[str]:
    sentences = [part for part in _SENTENCE_BOUNDARY.split(text) if part]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_hard(sentence, limit))
        elif len(current) + len(sentence) <= limit:
            current += sentence
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def split_qq_message(text: str, *, limit: int) -> tuple[str, ...]:
    """Split by paragraphs, then sentences, then Python Unicode code points."""

    if not text:
        return ()
    paragraphs = re.split(r"(\n\s*\n)", text)
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if not paragraph:
            continue
        if len(paragraph) > limit:
            if current.strip():
                chunks.append(current.strip())
            current = ""
            chunks.extend(
                part.strip() for part in _split_sentence(paragraph, limit) if part.strip()
            )
        elif len(current) + len(paragraph) <= limit:
            current += paragraph
        else:
            if current.strip():
                chunks.append(current.strip())
            current = paragraph
    if current.strip():
        chunks.append(current.strip())
    return tuple(chunks)
