"""Searchable projection of one capability (R3 §4.1).

Documents never carry full JSON Schemas.  Callers must still intersect hits
with the turn's authority-filtered requestable ids before exposure.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qq_ai_bot.capabilities.models import CapabilityEffect, CapabilityRisk, CapabilityTrustSource
from qq_ai_bot.capabilities.namespace import is_valid_namespace_id

SEARCH_DOCUMENT_TEXT_MAX = 4_000
SEARCH_QUERY_MAX = 200
TOKENIZER_VERSION = "cjk-ngram-v1"


class CapabilitySearchDocument(BaseModel):
    """Content-bounded lexical document for one capability."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability_id: str = Field(min_length=1, max_length=128)
    model_name: str = Field(min_length=1, max_length=64)
    canonical_name: str = Field(min_length=1, max_length=256)
    namespace_id: str
    namespace_path: tuple[str, ...] = ()
    namespace_description: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=SEARCH_DOCUMENT_TEXT_MAX)
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    use_when: tuple[str, ...] = ()
    parameter_names: tuple[str, ...] = ()
    parameter_descriptions: tuple[str, ...] = ()
    provider_id: str = Field(default="", max_length=128)
    trust_source: CapabilityTrustSource
    effect: CapabilityEffect
    risk: CapabilityRisk
    estimated_schema_tokens: int = Field(default=1, ge=1)
    synthetic: bool = False

    @model_validator(mode="after")
    def _fill_path(self) -> CapabilitySearchDocument:
        if not is_valid_namespace_id(self.namespace_id):
            raise ValueError(f"invalid namespace id: {self.namespace_id!r}")
        if not self.namespace_path:
            object.__setattr__(self, "namespace_path", tuple(self.namespace_id.split(".")))
        return self
