"""Stable errors for conversation history persistence."""


class ConversationHistoryError(Exception):
    """Base error for derived conversation history storage."""


class HistoryIdentityError(ConversationHistoryError):
    """Private/group identity is incomplete or mixed."""


class FrontierInvariantError(ConversationHistoryError):
    """Active frontier would overlap, gap, or keep parent and child active."""


class HistoryJobConflictError(ConversationHistoryError):
    """Lease owner mismatch or a job claim lost the race."""


class ConversationSummaryQualityError(ConversationHistoryError):
    """Structured compaction output failed a deterministic quality gate."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
