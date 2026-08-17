"""Local autonomous participation scoring (R4).

This is the production ``AutonomousParticipationPolicy``.  Direct private / @ /
reply-to-bot turns never call it.  The 3.5.3 scorer forced those turns through
the threshold while still recording ``necessity_score``; R4 does not score them
at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from qq_ai_bot.conversation.admission import AutonomousAdmissionScore, AutonomousCandidate
from qq_ai_bot.domain.conversations import ScopeType

_LOW_VALUE_REACTIONS = frozenset(
    {
        "6",
        "66",
        "666",
        "哈哈",
        "哈哈哈",
        "嗯",
        "嗯嗯",
        "哦",
        "噢",
        "啊",
        "行",
        "好",
        "好的",
        "草",
        "笑死",
    }
)
_QUESTION_TOKENS = ("?", "？", "吗", "么", "呢", "谁", "什么", "怎么", "为何", "为什么")
_REQUEST_TOKENS = (
    "请",
    "帮我",
    "帮忙",
    "能不能",
    "可以帮",
    "麻烦",
    "告诉我",
    "提醒我",
    "查一下",
    "看一下",
    "改成",
    "设置成",
)
_OPINION_TOKENS = ("你觉得", "怎么看", "有什么建议", "你认为", "意见", "建议")
_EMOTION_TOKENS = ("难过", "伤心", "焦虑", "害怕", "生气", "委屈", "孤独", "烦", "崩溃")
_CORRECTION_TOKENS = ("不对", "错了", "不是这样", "你误会", "纠正", "应该是")
_VISUAL_TOKENS = ("看图", "看图片", "这张图", "表情包", "引用", "我回复的")
_UNFINISHED_SUFFIXES = ("但是", "不过", "然后", "所以", "还有", "而且")
_PURE_PUNCTUATION = re.compile(r"^[\W_]+$", re.UNICODE)


class AdmissionSignalHint(Protocol):
    """Duck-typed plugin admission hint consumed by the local scorer."""

    source_plugin_id: str
    score_delta: float
    confidence: float
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class AdmissionFeatures:
    """Trusted counters and current untrusted text consumed by the pure scorer."""

    scope_type: ScopeType
    text: str
    reply_target_is_bot: bool = False
    mentions_bot: bool = False
    continuation: bool = False
    pending_message_count: int = 1
    recent_bot_messages: int = 0
    recent_total_messages: int = 1
    average_human_interval_seconds: float = 60.0
    idle_seconds: float = 0.0
    seconds_since_last_bot_message: float | None = None
    relationship_adjustment: float = 0.0
    plugin_signals: tuple[AdmissionSignalHint, ...] = ()
    new_message_count: int = 1
    media_only: bool = False
    addresses_other_bot: bool = False
    now: datetime | None = None


@dataclass(frozen=True, slots=True)
class AdmissionScoreSnapshot:
    """Detailed local score used by tests and autonomous admission metrics."""

    score: int
    threshold: int
    should_participate: bool
    relevance_score: int
    content_score: int
    pressure_score: int
    presence_penalty: int
    activity_penalty: int
    relationship_adjustment: int
    plugin_adjustment: int
    reasons: tuple[str, ...] = ()
    pending_message_count: int = 0
    recent_bot_messages: int = 0
    recent_total_messages: int = 0
    average_human_interval_seconds: float = 0.0
    idle_seconds: float = 0.0

    @property
    def should_enter_planner(self) -> bool:
        """Historical alias kept for leftover Planner-era unit tests."""

        return self.should_participate

    def as_admission_score(self) -> AutonomousAdmissionScore:
        return AutonomousAdmissionScore(
            score=float(self.score),
            threshold=float(self.threshold),
            reasons=self.reasons,
        )


class LocalAutonomousParticipationPolicy:
    """Compute a stable 0..100 gate score without invoking a model."""

    def __init__(
        self,
        *,
        threshold: int = 80,
        bot_aliases: tuple[str, ...] = ("yuki", "由纪"),
    ) -> None:
        if not 0 <= threshold <= 100:
            raise ValueError("threshold must be between 0 and 100")
        self._threshold = threshold
        self._bot_aliases = tuple(alias.casefold() for alias in bot_aliases if alias.strip())

    @property
    def threshold(self) -> int:
        return self._threshold

    def score(self, features: AdmissionFeatures) -> AdmissionScoreSnapshot:
        """Compatibility alias for the 3.5.3 ``ReplyNecessityScorer.score`` API."""

        return self.evaluate(features)

    async def score_candidate(self, candidate: AutonomousCandidate) -> AutonomousAdmissionScore:
        """Protocol-shaped scorer for one autonomous candidate."""

        features = AdmissionFeatures(
            scope_type=candidate.scene.scope_type,
            text=candidate.latest_content.text,
            reply_target_is_bot=candidate.scene.replies_to_bot,
            mentions_bot=candidate.scene.mentions_bot,
            pending_message_count=candidate.pending_message_count,
            recent_bot_messages=1 if candidate.bot_recently_active else 0,
            media_only=candidate.latest_content.source == "media_only",
        )
        return self.evaluate(features).as_admission_score()

    def evaluate(self, features: AdmissionFeatures) -> AdmissionScoreSnapshot:
        """Return the deterministic snapshot for one real-message batch."""

        pending = max(0, min(100, int(features.pending_message_count)))
        recent_bot = max(0, int(features.recent_bot_messages))
        recent_total = max(recent_bot, int(features.recent_total_messages), 0)
        average_interval = max(0.0, float(features.average_human_interval_seconds))
        idle_seconds = max(0.0, float(features.idle_seconds))
        if features.new_message_count <= 0:
            return AdmissionScoreSnapshot(
                score=0,
                threshold=self._threshold,
                should_participate=False,
                relevance_score=0,
                content_score=0,
                pressure_score=0,
                presence_penalty=0,
                activity_penalty=0,
                relationship_adjustment=0,
                plugin_adjustment=0,
                reasons=("no_new_messages",),
                pending_message_count=pending,
                recent_bot_messages=recent_bot,
                recent_total_messages=recent_total,
                average_human_interval_seconds=average_interval,
                idle_seconds=idle_seconds,
            )

        text = features.text.strip()
        folded = text.casefold()
        reasons: list[str] = []
        relevance = 0
        # Direct private / @ / reply-to-bot never enter this scorer. Do not keep
        # the 3.5.3 forced-adjacent bonuses; they would inflate leaked flags.
        if any(alias in folded for alias in self._bot_aliases):
            relevance += 32
            reasons.append("names_bot")
        if features.continuation:
            relevance += 18
            reasons.append("continuation")
        relevance = min(100, relevance)

        content = 0
        low_value = self._is_low_value(text)
        if low_value:
            content -= 30
            reasons.append("low_value_reaction")
        if self._contains_any(text, _QUESTION_TOKENS):
            content += 18
            reasons.append("question")
        if self._contains_any(text, _REQUEST_TOKENS):
            content += 22
            reasons.append("request")
        if self._contains_any(text, _OPINION_TOKENS):
            content += 18
            reasons.append("asks_opinion")
        if self._contains_any(text, _EMOTION_TOKENS):
            content += 12
            reasons.append("emotional_expression")
        if self._contains_any(text, _CORRECTION_TOKENS):
            content += 20
            reasons.append("corrects_bot")
        if self._contains_any(text, _VISUAL_TOKENS):
            content += 12
            reasons.append("visual_or_reply_reference")
        if text.endswith(_UNFINISHED_SUFFIXES):
            content += 8
            reasons.append("unfinished_topic")
        if features.media_only and not (features.mentions_bot or features.reply_target_is_bot):
            content -= 15
            reasons.append("unaddressed_media")
        if features.addresses_other_bot:
            content -= 25
            reasons.append("addresses_other_bot")
        content = max(-100, min(100, content))

        pressure = min(max(pending - 1, 0) * 2, 12)
        if pending >= 3:
            pressure += 4
            reasons.append("context_accumulated")
        if idle_seconds >= 30:
            pressure += min(15, int(idle_seconds // 30))
            reasons.append("idle_window")
        pressure = min(100, pressure)

        presence_penalty = min(15, recent_bot * 3)
        bot_ratio = recent_bot / recent_total if recent_total else 0.0
        if bot_ratio >= 0.4:
            presence_penalty += 15
        elif bot_ratio >= 0.25:
            presence_penalty += 8
        elif bot_ratio >= 0.15:
            presence_penalty += 4
        since_bot = features.seconds_since_last_bot_message
        if since_bot is not None:
            if since_bot < 30:
                presence_penalty += 12
            elif since_bot < 120:
                presence_penalty += 6
        presence_penalty = min(100, presence_penalty)
        if presence_penalty:
            reasons.append("recent_bot_presence")

        activity_penalty = 0
        if average_interval <= 2:
            activity_penalty = 25
        elif average_interval <= 5:
            activity_penalty = 18
        elif average_interval <= 10:
            activity_penalty = 10
        elif average_interval <= 20:
            activity_penalty = 4
        if pending >= 15:
            activity_penalty += 8
        activity_penalty = min(100, activity_penalty)
        if activity_penalty:
            reasons.append("conversation_fast")

        relationship = max(-5, min(5, round(features.relationship_adjustment)))
        if relationship:
            reasons.append("relationship_adjustment")
        plugin_adjustment = self._plugin_adjustment(features.plugin_signals, features.now)
        if plugin_adjustment:
            reasons.append("plugin_signal_adjustment")

        base = 10 if features.scope_type is ScopeType.GROUP else 0
        raw_score = (
            base
            + relevance
            + content
            + pressure
            + relationship
            + plugin_adjustment
            - presence_penalty
            - activity_penalty
        )
        score = max(0, min(100, round(raw_score)))
        should_enter = bool(text or features.media_only) and score >= self._threshold
        return AdmissionScoreSnapshot(
            score=score,
            threshold=self._threshold,
            should_participate=should_enter,
            relevance_score=relevance,
            content_score=content,
            pressure_score=pressure,
            presence_penalty=presence_penalty,
            activity_penalty=activity_penalty,
            relationship_adjustment=relationship,
            plugin_adjustment=plugin_adjustment,
            reasons=tuple(reasons),
            pending_message_count=pending,
            recent_bot_messages=recent_bot,
            recent_total_messages=recent_total,
            average_human_interval_seconds=average_interval,
            idle_seconds=idle_seconds,
        )

    @staticmethod
    def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
        return any(token in text for token in tokens)

    @staticmethod
    def _is_low_value(text: str) -> bool:
        if not text:
            return True
        return text.casefold() in _LOW_VALUE_REACTIONS or bool(_PURE_PUNCTUATION.fullmatch(text))

    @staticmethod
    def _plugin_adjustment(
        signals: tuple[AdmissionSignalHint, ...],
        now: datetime | None,
    ) -> int:
        effective_now = now or datetime.now(UTC)
        by_plugin: dict[str, float] = {}
        for signal in signals:
            expires_at = signal.expires_at
            if expires_at is not None:
                comparable_now = effective_now
                if expires_at.tzinfo is None and comparable_now.tzinfo is not None:
                    comparable_now = comparable_now.replace(tzinfo=None)
                if expires_at <= comparable_now:
                    continue
            weighted = signal.score_delta * signal.confidence
            by_plugin[signal.source_plugin_id] = max(
                -10,
                min(10, by_plugin.get(signal.source_plugin_id, 0.0) + weighted),
            )
        return max(-15, min(15, round(sum(by_plugin.values()))))


# R5 deletes planner/necessity.py.  Keep the historical names as aliases so
# leftover unit tests can import either path during the cutover.
ReplyNecessityFeatures = AdmissionFeatures
ReplyNecessityScorer = LocalAutonomousParticipationPolicy
ReplyNecessitySnapshot = AdmissionScoreSnapshot

__all__ = [
    "AdmissionFeatures",
    "AdmissionScoreSnapshot",
    "AdmissionSignalHint",
    "LocalAutonomousParticipationPolicy",
    "ReplyNecessityFeatures",
    "ReplyNecessityScorer",
    "ReplyNecessitySnapshot",
]
