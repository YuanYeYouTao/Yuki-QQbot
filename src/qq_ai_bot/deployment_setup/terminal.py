"""Small accessible terminal UI used by the guided setup command."""

from __future__ import annotations

import getpass
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO


class BackRequested(Exception):
    """Request navigation to the previous logical wizard page."""


class QuitRequested(Exception):
    """Request a safe wizard exit without persisting the draft."""


@dataclass(frozen=True, slots=True)
class _Style:
    cyan: str = "\033[36m"
    blue: str = "\033[34m"
    green: str = "\033[32m"
    yellow: str = "\033[33m"
    red: str = "\033[31m"
    gray: str = "\033[90m"
    reset: str = "\033[0m"


class TerminalUI:
    """Render prompts with symbols and optional ANSI colour."""

    def __init__(
        self,
        *,
        no_color: bool = False,
        input_fn: Callable[[str], str] = input,
        secret_fn: Callable[[str], str] = getpass.getpass,
        output: TextIO = sys.stdout,
        environ: dict[str, str] | None = None,
        is_tty: bool | None = None,
    ) -> None:
        environment = environ if environ is not None else dict(os.environ)
        tty = output.isatty() if is_tty is None else is_tty
        self.color = bool(
            not no_color
            and tty
            and "NO_COLOR" not in environment
            and environment.get("TERM", "").casefold() != "dumb"
            and not environment.get("CI")
        )
        self._input = input_fn
        self._secret = secret_fn
        self._output = output
        self._style = _Style()

    def navigation_hint(self) -> None:
        self.disabled("输入 :back 返回上一页，输入 :quit 安全退出（必须输入开头的英文冒号“:”）")

    def _paint(self, value: str, color: str) -> str:
        if not self.color:
            return value
        return f"{color}{value}{self._style.reset}"

    def title(self, value: str) -> None:
        self._write(self._paint(value, self._style.cyan))

    def step(self, current: int, total: int, value: str) -> None:
        self._write(self._paint(f"[{current}/{total}] {value}", self._style.cyan))

    def info(self, value: str) -> None:
        self._write(self._paint(f"i {value}", self._style.blue))

    def success(self, value: str) -> None:
        self._write(self._paint(f"✓ {value}", self._style.green))

    def warning(self, value: str) -> None:
        self._write(self._paint(f"! {value}", self._style.yellow))

    def error(self, value: str) -> None:
        self._write(self._paint(f"× {value}", self._style.red))

    def disabled(self, value: str) -> None:
        self._write(self._paint(f"○ {value}", self._style.gray))

    def line(self, value: str = "") -> None:
        self._write(value)

    def ask(self, label: str, *, default: str = "", required: bool = False) -> str:
        suffix = f" [{default}]" if default else ""
        while True:
            value = self._read(self._input, f"{label}{suffix}: ")
            resolved = value or default
            if resolved or not required:
                return resolved
            self.error(f"{label}不能为空")

    def ask_secret(self, label: str, *, configured: bool = False) -> str:
        suffix = "（留空保留现有值）" if configured else ""
        return self._read(self._secret, f"{label}{suffix}: ")

    def confirm(self, label: str, *, default: bool = False) -> bool:
        prompt = "[Y/n]" if default else "[y/N]"
        while True:
            value = self._read(self._input, f"{label} {prompt}: ").casefold()
            if not value:
                return default
            if value in {"y", "yes", "是"}:
                return True
            if value in {"n", "no", "否"}:
                return False
            self.error("请输入 y 或 n")

    def choose(self, label: str, choices: tuple[tuple[str, str], ...], *, default: str) -> str:
        by_number: dict[str, str] = {}
        allowed: set[str] = set()
        self.line(label)
        for index, (value, description) in enumerate(choices, start=1):
            marker = "*" if value == default else " "
            self.line(f"  {index}. [{marker}] {description}")
            by_number[str(index)] = value
            allowed.add(value)
        while True:
            answer = self._read(self._input, f"请选择 [{default}]: ")
            resolved = by_number.get(answer, answer or default)
            if resolved in allowed:
                return resolved
            self.error("选择无效")

    def choose_many(
        self,
        label: str,
        choices: tuple[tuple[str, str], ...],
    ) -> tuple[str, ...]:
        by_number: dict[str, str] = {}
        self.line(label)
        for index, (value, description) in enumerate(choices, start=1):
            self.line(f"  {index}. {description}")
            by_number[str(index)] = value
        self.line("  a. 全部区块")
        while True:
            answer = self._read(self._input, "请选择编号（逗号分隔，留空不修改）: ")
            if not answer:
                return ()
            if answer.casefold() in {"a", "all", "全部"}:
                return tuple(value for value, _description in choices)
            tokens = tuple(item.strip() for item in answer.split(",") if item.strip())
            selected = tuple(dict.fromkeys(by_number.get(item, item) for item in tokens))
            if selected and all(item in {value for value, _ in choices} for item in selected):
                return selected
            self.error("选择无效，请输入页面中显示的编号")

    def _read(self, reader: Callable[[str], str], prompt: str) -> str:
        try:
            value = reader(prompt).strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise QuitRequested from exc
        command = value.casefold()
        if command == ":back":
            raise BackRequested
        if command == ":quit":
            raise QuitRequested
        return value

    def _write(self, value: str) -> None:
        print(value, file=self._output, flush=True)
