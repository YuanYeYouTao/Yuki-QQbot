"""Load request_tools aliases from TOML instead of routing word lists."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path

_DEFAULT_OVERLAY = Path("config/capability_search.toml")
_PACKAGE_FILE = "search_aliases.toml"


@dataclass(frozen=True, slots=True)
class SearchTerms:
    aliases: tuple[str, ...] = ()
    use_when: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchAliasTable:
    tools: dict[str, SearchTerms]


def search_terms_for(tool_name: str) -> SearchTerms:
    return load_search_alias_table().tools.get(tool_name.strip(), SearchTerms())


def merge_search_terms(
    *,
    aliases: tuple[str, ...] = (),
    use_when: tuple[str, ...] = (),
    tool_name: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    extra = search_terms_for(tool_name)
    return (
        tuple(dict.fromkeys((*aliases, *extra.aliases))),
        tuple(dict.fromkeys((*use_when, *extra.use_when))),
    )


@lru_cache(maxsize=1)
def load_search_alias_table() -> SearchAliasTable:
    table = _parse_table(_package_payload())
    for path in _overlay_paths():
        if not path.is_file():
            continue
        table = _merge_tables(table, _parse_table(path.read_bytes()))
    return table


def reset_search_alias_cache() -> None:
    load_search_alias_table.cache_clear()


def _package_payload() -> bytes:
    return resources.files(__package__).joinpath(_PACKAGE_FILE).read_bytes()


def _overlay_paths() -> tuple[Path, ...]:
    configured = os.environ.get("CAPABILITY_SEARCH_FILE", "").strip()
    paths: list[Path] = []
    if configured:
        paths.append(Path(configured))
    paths.append(_DEFAULT_OVERLAY)
    return tuple(paths)


def _parse_table(payload: bytes) -> SearchAliasTable:
    raw = tomllib.loads(payload.decode("utf-8"))
    tools_raw = raw.get("tool")
    if tools_raw is None:
        return SearchAliasTable(tools={})
    if not isinstance(tools_raw, dict):
        raise ValueError("capability search aliases [tool] must be a table")
    tools: dict[str, SearchTerms] = {}
    for name, spec in tools_raw.items():
        tool_name = str(name).strip()
        if not tool_name or not isinstance(spec, dict):
            raise ValueError(f"invalid capability search alias entry: {name!r}")
        tools[tool_name] = SearchTerms(
            aliases=_string_tuple(spec.get("aliases"), field=f"tool.{tool_name}.aliases"),
            use_when=_string_tuple(spec.get("use_when"), field=f"tool.{tool_name}.use_when"),
        )
    return SearchAliasTable(tools=tools)


def _merge_tables(base: SearchAliasTable, overlay: SearchAliasTable) -> SearchAliasTable:
    merged = dict(base.tools)
    for name, extra in overlay.tools.items():
        current = merged.get(name, SearchTerms())
        merged[name] = SearchTerms(
            aliases=tuple(dict.fromkeys((*current.aliases, *extra.aliases))),
            use_when=tuple(dict.fromkeys((*current.use_when, *extra.use_when))),
        )
    return SearchAliasTable(tools=merged)


def _string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be an array of strings")
    items = [item.strip() for item in value if item.strip()]
    if len(items) != len(set(items)):
        raise ValueError(f"{field} must not contain duplicates")
    return tuple(items)
