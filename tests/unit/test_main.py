"""Application bootstrap compatibility tests."""

from __future__ import annotations

import json
import os

from qq_ai_bot.main import _nonebot_superusers_environment


def test_nonebot_superusers_environment_normalizes_and_restores_csv(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUPERUSERS", "9001,9000")

    with _nonebot_superusers_environment(frozenset({"9000", "9001"})):
        assert json.loads(os.environ["SUPERUSERS"]) == ["9000", "9001"]

    assert os.environ["SUPERUSERS"] == "9001,9000"


def test_nonebot_superusers_environment_removes_temporary_value(monkeypatch) -> None:
    monkeypatch.delenv("SUPERUSERS", raising=False)

    with _nonebot_superusers_environment(frozenset()):
        assert os.environ["SUPERUSERS"] == "[]"

    assert "SUPERUSERS" not in os.environ
