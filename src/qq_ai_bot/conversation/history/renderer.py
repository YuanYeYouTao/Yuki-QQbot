"""Deterministic SESSION text for conversation history summaries."""

from __future__ import annotations

from qq_ai_bot.conversation.history.summarizer import ConversationSummaryOutput


def render_conversation_summary(output: ConversationSummaryOutput) -> str:
    """Turn structured output into a stable, non-markdown SESSION block."""

    lines = [output.narrative.strip()]
    for decision in output.decisions:
        actors = ",".join(decision.actors) if decision.actors else "unknown"
        lines.append(f"decision {decision.status.value} [{actors}]: {decision.decision}")
    for loop in output.open_loops:
        lines.append(f"open {loop.state.value} [{loop.owner.value}]: {loop.item}")
    for constraint in output.constraints:
        lines.append(
            f"constraint {constraint.scope.value}/{constraint.source_type.value}: "
            f"{constraint.constraint}"
        )
    for entity in output.entities:
        lines.append(f"entity {entity.name}: {entity.role}")
    for change in output.state_changes:
        lines.append(
            f"state {change.certainty.value} {change.subject}: {change.before} -> {change.after}"
        )
    for uncertainty in output.uncertainties:
        lines.append(f"uncertain: {uncertainty.claim} ({uncertainty.reason})")
    for outcome in output.terminal_tool_outcomes:
        lines.append(
            f"tool {outcome.tool} {outcome.outcome} durable={outcome.durable_effect.value}: "
            f"{outcome.public_result}"
        )
    return "\n".join(line for line in lines if line)
