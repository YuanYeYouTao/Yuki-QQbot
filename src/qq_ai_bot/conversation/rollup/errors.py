"""Stable failure categories for conversation rollup."""


class ConversationRollupError(RuntimeError):
    """Base class for safe rollup failures."""


class ConversationCoverageError(ConversationRollupError):
    """A continuous, bounded prompt snapshot could not be produced."""


class RollupLeaseLostError(ConversationRollupError):
    """The owner/token/expiry lease fence rejected an operation."""


class RollupSourceChangedError(ConversationRollupError):
    """Candidate inputs changed while summary text was being generated."""


class ScopeGenerationSupersededError(ConversationRollupError):
    """A turn or worker references an obsolete scope generation."""
