"""Conservative standalone emoji-request detection."""

from __future__ import annotations

import pytest

from qq_ai_bot.emoji.request_detector import EmojiRequestDetector


@pytest.mark.parametrize(
    ("text", "standalone"),
    (
        ("@Yuki 发个表情", True),
        ("来个开心的表情包", True),
        ("给我发张梗图", True),
        ("这个表情是什么意思", False),
        ("不要发表情", False),
        ("回答问题并带一个表情", False),
        ("给图片加表情", False),
    ),
)
def test_emoji_request_detector_is_conservative(text: str, standalone: bool) -> None:
    hint = EmojiRequestDetector().detect(text)
    assert hint.standalone_request is standalone
    assert hint.explicit_request is standalone


def test_emoji_request_detector_accepts_configured_bot_alias() -> None:
    hint = EmojiRequestDetector(("Mika", "米卡")).detect("米卡，发个开心的表情")

    assert hint.standalone_request
    assert hint.goal == "开心"
