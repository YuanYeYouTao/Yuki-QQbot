"""Capability search aliases come from TOML, not query-routing branches."""

from __future__ import annotations

from qq_ai_bot.capabilities.search_aliases import (
    load_search_alias_table,
    merge_search_terms,
    reset_search_alias_cache,
    search_terms_for,
)


def test_package_table_declares_automation_create_aliases() -> None:
    reset_search_alias_cache()
    terms = search_terms_for("automation_create")
    assert "提醒我" in terms.aliases
    assert "定时任务能力" in terms.aliases
    assert "创建定时任务" in terms.aliases


def test_overlay_merges_additional_aliases(tmp_path, monkeypatch) -> None:
    overlay = tmp_path / "capability_search.toml"
    overlay.write_text(
        """
[tool.automation_create]
aliases = ["设个闹钟"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CAPABILITY_SEARCH_FILE", str(overlay))
    reset_search_alias_cache()
    try:
        aliases, use_when = merge_search_terms(
            aliases=("已有别名",),
            tool_name="automation_create",
        )
        assert "已有别名" in aliases
        assert "提醒我" in aliases
        assert "设个闹钟" in aliases
        assert use_when
    finally:
        monkeypatch.delenv("CAPABILITY_SEARCH_FILE", raising=False)
        reset_search_alias_cache()
        load_search_alias_table()
