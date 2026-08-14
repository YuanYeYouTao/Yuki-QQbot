"""Low-cardinality in-process Tool Kernel metrics."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass(slots=True)
class ToolKernelMetrics:
    invocations: Counter[tuple[str, str, bool]] = field(default_factory=Counter)
    refreshes: Counter[tuple[str, bool]] = field(default_factory=Counter)
    selected_for_turn: Counter[tuple[str, str]] = field(default_factory=Counter)
    schema_tokens: Counter[tuple[str, str]] = field(default_factory=Counter)
    planner_scope_turns: Counter[bool] = field(default_factory=Counter)
    first_round_tool_hits: Counter[bool] = field(default_factory=Counter)
    tool_enabled_turns: int = 0
    request_tools_calls: int = 0
    request_tools_zero_results: int = 0
    automatic_memory_request_tools_calls: int = 0
    automatic_memory_read_tools_loaded: int = 0
    automatic_memory_read_tool_calls: int = 0
    mutation_locator_read_fallbacks: int = 0

    def record_invocation(self, provider_id: str, tool_name: str, ok: bool) -> None:
        self.invocations[(provider_id, tool_name, ok)] += 1

    def record_refresh(self, provider_id: str, ok: bool) -> None:
        self.refreshes[(provider_id, ok)] += 1

    def record_selection(self, provider_id: str, tool_name: str, schema_tokens: int) -> None:
        self.selected_for_turn[(provider_id, tool_name)] += 1
        self.schema_tokens[(provider_id, tool_name)] += max(0, schema_tokens)

    def record_tool_enabled_turn(self, *, planner_scope_explicit: bool) -> None:
        """Count one tool-capable turn without retaining conversation identity."""

        self.tool_enabled_turns += 1
        self.planner_scope_turns[planner_scope_explicit] += 1

    def record_request_tools(self) -> None:
        """Track one fallback discovery call."""

        self.request_tools_calls += 1

    def record_request_tools_zero_result(self) -> None:
        """Track a valid discovery attempt that returned no capability."""

        self.request_tools_zero_results += 1

    def record_automatic_memory_request_tools(self) -> None:
        self.automatic_memory_request_tools_calls += 1

    def record_automatic_memory_read_tools_loaded(self) -> None:
        self.automatic_memory_read_tools_loaded += 1

    def record_automatic_memory_read_tool_call(self, *, locator_fallback: bool) -> None:
        self.automatic_memory_read_tool_calls += 1
        if locator_fallback:
            self.mutation_locator_read_fallbacks += 1

    def record_first_round_tool_hit(self, *, hit: bool) -> None:
        """Record whether the first real tool ran without discovery fallback."""

        self.first_round_tool_hits[hit] += 1
