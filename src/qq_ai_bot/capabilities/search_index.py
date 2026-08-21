"""Revision-cached in-memory FTS5 capability search (R3 §4.2).

The index covers the full descriptor registry.  Discovery never grants
authority: callers must intersect hits with the turn's requestable ids.
Rebuilds are keyed by the full descriptor content hash, not
``provider:model_name:schema_version``.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field

from qq_ai_bot.capabilities.search_document import (
    SEARCH_QUERY_MAX,
    TOKENIZER_VERSION,
    CapabilitySearchDocument,
)

_ASCII_TERM = re.compile(r"[a-z0-9_.-]{2,}")
_CJK_RUN = re.compile(r"[\u3400-\u9fff]{2,}")
_FTS_SPECIAL = re.compile(r'["\'*:^]')
_NAME_SEPARATORS = re.compile(r"[-_\s]+")
QUERY_TERM_LIMIT = 64
FTS_CANDIDATE_LIMIT = 48
EXACT_NAME_SCORE = 10_000.0
EXACT_ALIAS_SCORE = 8_000.0
ALIAS_LENGTH_BONUS = 20.0
NAMESPACE_BONUS = 120.0
PARAMETER_BONUS = 40.0
AFFINITY_BONUS = 25.0
SCHEMA_COST_PENALTY_PER_TOKEN = 0.02
SCHEMA_COST_PENALTY_CAP = 8.0


@dataclass(frozen=True, slots=True)
class CapabilitySearchHit:
    """One ranked hit; carries ids and score only, never schemas."""

    capability_id: str
    namespace_id: str
    score: float
    synthetic: bool = False


@dataclass(slots=True)
class FtsCapabilitySearchIndex:
    """Exact + alias maps plus in-memory SQLite FTS5 BM25."""

    tokenizer_version: str = TOKENIZER_VERSION
    _revision: str | None = None
    _connection: sqlite3.Connection | None = None
    _documents: dict[str, CapabilitySearchDocument] = field(default_factory=dict)
    _exact_names: dict[str, str] = field(default_factory=dict)
    _aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def revision(self) -> str | None:
        return self._revision

    def rebuild(self, *, revision: str, documents: Sequence[CapabilitySearchDocument]) -> None:
        """Atomically replace the index for a new registry content hash."""

        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA query_only = 0")
        connection.execute(
            """
            CREATE VIRTUAL TABLE capability_fts USING fts5(
                capability_id UNINDEXED,
                namespace_id,
                canonical_name,
                model_name,
                aliases,
                body,
                tokenize = 'unicode61 remove_diacritics 2'
            )
            """
        )
        exact_names: dict[str, str] = {}
        aliases: dict[str, tuple[str, ...]] = {}
        stored: dict[str, CapabilitySearchDocument] = {}
        for document in documents:
            stored[document.capability_id] = document
            _register_name_keys(
                exact_names,
                document.model_name,
                document.capability_id,
                primary=True,
            )
            _register_name_keys(
                exact_names,
                document.canonical_name,
                document.capability_id,
                primary=True,
            )
            for alias in document.aliases:
                for key in _name_lookup_keys(alias):
                    existing = aliases.get(key, ())
                    if document.capability_id not in existing:
                        aliases[key] = (*existing, document.capability_id)
            connection.execute(
                """
                INSERT INTO capability_fts(
                    capability_id, namespace_id, canonical_name, model_name, aliases, body
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    document.capability_id,
                    _fts_document_text(document.namespace_id, document.namespace_description),
                    _fts_document_text(document.canonical_name),
                    _fts_document_text(*_model_name_search_parts(document.model_name)),
                    _fts_document_text(*document.aliases, *document.tags),
                    _fts_document_text(
                        document.description,
                        *document.use_when,
                        *document.parameter_names,
                        *document.parameter_descriptions,
                    ),
                ),
            )
        if self._connection is not None:
            self._connection.close()
        self._connection = connection
        self._revision = revision
        self._documents = stored
        self._exact_names = exact_names
        self._aliases = aliases

    def search(
        self,
        query: str,
        *,
        limit: int,
        affinity_namespace_ids: tuple[str, ...] = (),
    ) -> tuple[CapabilitySearchHit, ...]:
        cleaned = query.strip()[:SEARCH_QUERY_MAX]
        if len(cleaned) < 2 or limit <= 0:
            return ()
        folded = cleaned.casefold()
        ranked: dict[str, float] = {}
        for key in _query_name_keys(cleaned):
            exact_id = self._exact_names.get(key)
            if exact_id is not None:
                ranked[exact_id] = max(ranked.get(exact_id, 0.0), EXACT_NAME_SCORE)
            for alias_id in self._aliases.get(key, ()):
                ranked[alias_id] = max(ranked.get(alias_id, 0.0), _alias_score(key))
        query_terms = set(_query_terms(cleaned))
        for alias, capability_ids in self._aliases.items():
            if alias and alias in query_terms:
                for capability_id in capability_ids:
                    ranked[capability_id] = max(
                        ranked.get(capability_id, 0.0), _alias_score(alias)
                    )
        if self._connection is not None:
            match = _fts_match_expression(cleaned)
            if match:
                rows = self._connection.execute(
                    """
                    SELECT capability_id, bm25(capability_fts, 0, 4.0, 6.0, 8.0, 3.0, 1.0) AS rank
                    FROM capability_fts
                    WHERE capability_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (match, FTS_CANDIDATE_LIMIT),
                ).fetchall()
                for capability_id, rank in rows:
                    # sqlite FTS5 bm25() is lower-is-better; invert for our sort.
                    score = 1_000.0 / (1.0 + max(float(rank), 0.0))
                    ranked[str(capability_id)] = ranked.get(str(capability_id), 0.0) + score
        affinity = set(affinity_namespace_ids)
        hits: list[CapabilitySearchHit] = []
        for capability_id, score in ranked.items():
            document = self._documents.get(capability_id)
            if document is None:
                continue
            adjusted = score
            if document.namespace_id in affinity:
                adjusted += AFFINITY_BONUS
            if any(term in folded for term in document.parameter_names if len(term) >= 2):
                adjusted += PARAMETER_BONUS
            if any(term in folded for term in document.namespace_path):
                adjusted += NAMESPACE_BONUS
            adjusted -= min(
                document.estimated_schema_tokens * SCHEMA_COST_PENALTY_PER_TOKEN,
                SCHEMA_COST_PENALTY_CAP,
            )
            hits.append(
                CapabilitySearchHit(
                    capability_id=document.capability_id,
                    namespace_id=document.namespace_id,
                    score=adjusted,
                    synthetic=document.synthetic,
                )
            )
        hits.sort(
            key=lambda item: (
                -item.score,
                self._documents[item.capability_id].canonical_name,
                item.capability_id,
            )
        )
        return tuple(hits[:limit])

    def document(self, capability_id: str) -> CapabilitySearchDocument | None:
        return self._documents.get(capability_id)


def _alias_score(alias: str) -> float:
    return EXACT_ALIAS_SCORE + min(len(alias.strip()), 24) * ALIAS_LENGTH_BONUS


def _compact(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _query_name_keys(query: str) -> tuple[str, ...]:
    """Exact-name keys for the whole query and any consecutive ASCII tool-name span."""

    keys = list(_name_lookup_keys(query))
    ascii_terms = _ASCII_TERM.findall(query.casefold())
    for start in range(len(ascii_terms)):
        for end in range(start + 1, len(ascii_terms) + 1):
            for key in _name_lookup_keys("_".join(ascii_terms[start:end])):
                if key not in keys:
                    keys.append(key)
    return tuple(keys)


def _name_lookup_keys(value: str) -> tuple[str, ...]:
    """Treat spaces, hyphens and underscores as the same tool-name spelling."""

    folded = value.casefold().strip()
    if not folded:
        return ()
    keys = [folded]
    underscored = _NAME_SEPARATORS.sub("_", folded).strip("_")
    spaced = _NAME_SEPARATORS.sub(" ", folded).strip()
    compact = _compact(folded)
    for item in (underscored, spaced, compact):
        if item and item not in keys:
            keys.append(item)
    return tuple(keys)


def _register_name_keys(
    exact_names: dict[str, str],
    raw_name: str,
    capability_id: str,
    *,
    primary: bool,
) -> None:
    for index, key in enumerate(_name_lookup_keys(raw_name)):
        if primary and index == 0:
            exact_names[key] = capability_id
            continue
        exact_names.setdefault(key, capability_id)


def _model_name_search_parts(name: str) -> tuple[str, ...]:
    parts = [name]
    for token in _NAME_SEPARATORS.split(name):
        if len(token) >= 2 and token not in parts:
            parts.append(token)
    return tuple(parts)


def _query_terms(query: str) -> tuple[str, ...]:
    folded = query.casefold()
    runs = _CJK_RUN.findall(query)
    ngrams: dict[int, list[str]] = {2: [], 3: [], 4: []}
    for run in runs:
        for size in (2, 3, 4):
            if len(run) < size:
                continue
            ngrams[size].extend(run[index : index + size] for index in range(len(run) - size + 1))
    compact = _compact(query)
    groups = (
        _ASCII_TERM.findall(folded),
        runs,
        ngrams[2],
        ngrams[3],
        ngrams[4],
        (compact,) if len(compact) >= 2 else (),
    )
    unique: list[str] = []
    for group in groups:
        for term in group:
            if term and term not in unique:
                unique.append(term)
            if len(unique) >= QUERY_TERM_LIMIT:
                return tuple(unique)
    return tuple(unique)


def _fts_document_text(*parts: str) -> str:
    chunks: list[str] = []
    for part in parts:
        cleaned = part.strip()
        if not cleaned:
            continue
        chunks.append(cleaned)
        compact = _compact(cleaned)
        if compact and compact != cleaned.casefold():
            chunks.append(compact)
        for run in _CJK_RUN.findall(cleaned):
            for size in (2, 3, 4):
                if len(run) < size:
                    continue
                chunks.extend(run[index : index + size] for index in range(len(run) - size + 1))
    return " ".join(chunks)[:2_000]


def _fts_match_expression(query: str) -> str:
    terms = _query_terms(query)
    if not terms:
        return ""
    specific = [term for term in terms if len(term) >= 3 or _ASCII_TERM.fullmatch(term) is not None]
    selected = specific or ()
    if not selected:
        # Two-character CJK-only queries stay on the exact/alias maps.
        return ""
    escaped = []
    for term in selected:
        token = _FTS_SPECIAL.sub(" ", term).strip()
        if len(token) < 2:
            continue
        escaped.append(f'"{token}"')
    return " OR ".join(escaped)


__all__ = [
    "AFFINITY_BONUS",
    "SCHEMA_COST_PENALTY_CAP",
    "TOKENIZER_VERSION",
    "CapabilitySearchHit",
    "FtsCapabilitySearchIndex",
]
