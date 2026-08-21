"""Deterministic provider routing for backend-authorized web access."""

from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import urlsplit

from qq_ai_bot.domain.messages import NativeToolEvent, NativeToolStatus, ResponseCitation
from qq_ai_bot.web.base import WebSearchError, normalize_public_url
from qq_ai_bot.web.models import (
    WebMode,
    WebProvider,
    WebRouteDecision,
    WebRouteReason,
)

_URL = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?)]}，。；：！？）】》"
_TAVILY_OVERRIDE = re.compile(r"tavil(?:y|ity)|塔维利", re.IGNORECASE)


class WebProviderRouter:
    """Select native or Tavily without granting the web capability itself."""

    def __init__(
        self,
        *,
        tavily_domains: frozenset[str] = frozenset(),
        allow_provider_override: bool = True,
        fallback_on_access_denied: bool = True,
        fallback_on_target_miss: bool = True,
    ) -> None:
        self._tavily_domains = frozenset(
            normalized
            for item in tavily_domains
            if (normalized := self._normalize_domain_rule(item))
        )
        self._allow_provider_override = allow_provider_override
        self._fallback_on_access_denied = fallback_on_access_denied
        self._fallback_on_target_miss = fallback_on_target_miss

    def deployment_route(self, mode: WebMode | str) -> WebRouteDecision | None:
        """First-hop web shape from WEB_MODE only, ignoring the current sentence."""

        return self.select("", mode)

    def select(self, message: str, mode: WebMode | str) -> WebRouteDecision | None:
        """Choose the initial provider from deployment mode and current user input."""

        try:
            resolved_mode = WebMode(mode)
        except ValueError:
            return None
        target_urls = self.extract_public_urls(message)
        if resolved_mode is WebMode.DISABLED:
            return None
        if resolved_mode is WebMode.TAVILY:
            return WebRouteDecision(
                provider=WebProvider.TAVILY,
                reason=WebRouteReason.MODE,
                fallback_allowed=False,
                target_urls=target_urls,
            )
        if resolved_mode is WebMode.NATIVE:
            return WebRouteDecision(
                provider=WebProvider.NATIVE,
                reason=WebRouteReason.MODE,
                fallback_allowed=False,
                target_urls=target_urls,
            )

        override = self._provider_override(message)
        if override is WebProvider.TAVILY:
            return WebRouteDecision(
                provider=override,
                reason=WebRouteReason.USER_OVERRIDE,
                fallback_allowed=False,
                target_urls=target_urls,
            )
        if override is WebProvider.NATIVE:
            return WebRouteDecision(
                provider=override,
                reason=WebRouteReason.USER_OVERRIDE,
                fallback_allowed=False,
                target_urls=target_urls,
            )
        for url in target_urls:
            host = self._host(url)
            matched = self._matching_domain(host)
            if matched is not None:
                return WebRouteDecision(
                    provider=WebProvider.TAVILY,
                    reason=WebRouteReason.DOMAIN_RULE,
                    fallback_allowed=False,
                    matched_domain=matched,
                    target_urls=target_urls,
                )
        return WebRouteDecision(
            provider=WebProvider.NATIVE,
            reason=WebRouteReason.DEFAULT_NATIVE,
            fallback_allowed=True,
            target_urls=target_urls,
        )

    def native_terminal_failure(
        self,
        decision: WebRouteDecision | None,
        *,
        events: tuple[NativeToolEvent, ...],
        citations: tuple[ResponseCitation, ...],
    ) -> WebRouteReason | None:
        """Return a structured fallback reason for a completed native response."""

        if not self.can_fallback(decision) or not events:
            return None
        opened = {
            self._comparable_url(event.url)
            for event in events
            if event.status is NativeToolStatus.COMPLETED and event.url
        }
        opened.update(self._comparable_url(citation.url) for citation in citations)
        targets = (
            {self._comparable_url(url) for url in decision.target_urls}
            if decision is not None
            else set()
        )
        if targets.intersection(opened):
            return None
        if (
            self._fallback_on_access_denied
            and not opened
            and any(event.status is NativeToolStatus.FAILED for event in events)
        ):
            return WebRouteReason.NATIVE_ACCESS_DENIED
        if not self._fallback_on_target_miss or not targets:
            return None
        if targets and not targets.intersection(opened):
            return WebRouteReason.TARGET_NOT_OPENED
        return None

    @staticmethod
    def missing_source_failure(
        decision: WebRouteDecision | None,
        *,
        source_display_requested: bool,
        source_count: int,
    ) -> WebRouteReason | None:
        """Route an otherwise successful native answer when provenance is required."""

        if (
            source_display_requested
            and source_count == 0
            and WebProviderRouter.can_fallback(decision)
        ):
            return WebRouteReason.SOURCE_NOT_RECOVERED
        return None

    @staticmethod
    def can_fallback(decision: WebRouteDecision | None) -> bool:
        return bool(
            decision is not None
            and decision.provider is WebProvider.NATIVE
            and decision.fallback_allowed
            and decision.attempt == 1
        )

    @staticmethod
    def fallback(
        decision: WebRouteDecision,
        reason: WebRouteReason,
    ) -> WebRouteDecision:
        """Create the only permitted native-to-Tavily transition."""

        return replace(
            decision,
            provider=WebProvider.TAVILY,
            reason=reason,
            fallback_allowed=False,
            attempt=2,
        )

    @staticmethod
    def extract_public_urls(message: str) -> tuple[str, ...]:
        urls: list[str] = []
        for match in _URL.finditer(message):
            candidate = match.group(0).rstrip(_TRAILING_PUNCTUATION)
            try:
                normalized = normalize_public_url(candidate)
            except WebSearchError:
                continue
            if normalized not in urls:
                urls.append(normalized)
        return tuple(urls)

    def _provider_override(self, message: str) -> WebProvider | None:
        if not self._allow_provider_override:
            return None
        return WebProvider.TAVILY if _TAVILY_OVERRIDE.search(message) else None

    def _matching_domain(self, host: str) -> str | None:
        return next(
            (
                rule
                for rule in sorted(self._tavily_domains, key=len, reverse=True)
                if host == rule or host.endswith(f".{rule}")
            ),
            None,
        )

    @staticmethod
    def _normalize_domain_rule(rule: str) -> str:
        candidate = rule.strip().casefold().lstrip("*.").rstrip(".")
        if not candidate or "/" in candidate or ":" in candidate:
            return ""
        try:
            return candidate.encode("idna").decode("ascii")
        except UnicodeError:
            return ""

    @staticmethod
    def _host(url: str) -> str:
        return (urlsplit(url).hostname or "").casefold().rstrip(".")

    @classmethod
    def _comparable_url(cls, url: str) -> tuple[str, str]:
        try:
            parsed = urlsplit(normalize_public_url(url))
        except WebSearchError:
            return "", ""
        path = parsed.path.rstrip("/") or "/"
        return (parsed.hostname or "").casefold(), path
