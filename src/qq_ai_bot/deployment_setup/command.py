"""Argparse command and interactive flow for guided Docker deployment."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from qq_ai_bot import __version__
from qq_ai_bot.config import Settings
from qq_ai_bot.deployment_setup.migrate_3_6 import migrate_deployment_3_6
from qq_ai_bot.deployment_setup.service import (
    EnvironmentDocument,
    SetupConfiguration,
    SetupPaths,
    SetupValidationError,
    apply_pending_plugins,
    build_model_profiles,
    commit_configuration,
    discover_speech_profiles,
    infer_main_protocol,
    load_plugin_setup_states,
    missing_mcp_environment,
    model_profiles_use_flash,
    sanitize_mcp_document,
    validate_configuration,
    verify_health,
)
from qq_ai_bot.deployment_setup.terminal import BackRequested, QuitRequested, TerminalUI
from qq_ai_bot.plugin_host.discovery import PluginDiscovery

_SECTIONS = (
    "basic",
    "flash",
    "embedding",
    "web",
    "vision",
    "mcp",
    "plugin",
    "automation",
    "speech",
)
_PERSISTENT_DIRECTORIES = (
    "data",
    "data/setup",
    "data/speech/cache",
    "data/speech/genie_data",
    "data/speech/voices",
    "data/speech/japanese_frontend/models",
    "config",
    "plugins",
    "napcat-data",
    "napcat-config",
    "napcat-plugins",
)


@dataclass(slots=True)
class _SetupDraft:
    environment: dict[str, str]
    protocol: str
    flash_enabled: bool
    mcp_document: dict[str, object]
    pending_plugins: tuple[str, ...] | None = None
    write_mcp: bool = False
    rescue_changed: bool = False


@dataclass(frozen=True, slots=True)
class _WizardPage:
    section: str
    title: str


def add_setup_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    setup = subparsers.add_parser("setup", help="交互配置版本化 Docker 部署")
    setup.add_argument(
        "setup_action",
        nargs="?",
        choices=("configure", "validate", "apply-pending", "verify", "migrate-3-6"),
        default="configure",
    )
    setup.add_argument("--deployment-root", type=Path, default=Path.cwd())
    setup.add_argument("--no-color", action="store_true")
    setup.add_argument("--health-url", default="http://127.0.0.1:8080/healthz")
    setup.add_argument("--timeout", type=float, default=180.0)
    setup.add_argument(
        "--baseline-output",
        type=Path,
        default=None,
        help="Git-external path for the 3.6.0 runtime baseline JSON",
    )
    setup.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Working tree used to reject in-repo baseline output",
    )


def run_setup_command(args: argparse.Namespace) -> int:
    paths = SetupPaths(Path(args.deployment_root).resolve())
    ui = TerminalUI(no_color=bool(args.no_color))
    try:
        action = str(args.setup_action)
        if action == "configure":
            with _working_directory(paths.root):
                return _configure(paths, ui)
        if action == "validate":
            with _working_directory(paths.root):
                configuration, _document = _load_current_configuration(paths)
                validate_configuration(paths, configuration)
            ui.success("配置通过本地严格验证；未发起任何计费 API 请求")
            return 0
        if action == "migrate-3-6":
            result = migrate_deployment_3_6(
                paths,
                baseline_output=Path(args.baseline_output) if args.baseline_output else None,
                repo_root=Path(args.repo_root) if args.repo_root else None,
            )
            profiles = result.profiles
            if profiles.backup is not None:
                ui.info(f"配置已备份到 {profiles.backup.relative_to(paths.root)}")
            if profiles.materialized_attribution_from is not None:
                ui.info(
                    "已将 memory_attribution 显式写为 "
                    f"{profiles.materialized_attribution_from} 档案"
                )
            if profiles.removed_routes:
                ui.info("已删除路由：" + ", ".join(profiles.removed_routes))
            if profiles.changed:
                ui.success("model_profiles.toml 已迁移到 schema v3")
            else:
                ui.disabled("model_profiles.toml 已是 schema v3，无需改写")
            if result.env.renamed:
                ui.info(
                    "已重命名环境变量："
                    + ", ".join(f"{old}->{new}" for old, new in result.env.renamed)
                )
            if result.env.deleted:
                ui.info("已删除环境变量：" + ", ".join(result.env.deleted))
            if result.env.changed:
                ui.success(".env 已按 Conversation Runtime 映射改写")
            else:
                ui.disabled(".env 无需改写")
            if result.baseline.output is not None:
                ui.success(f"runtime baseline 已写入 {result.baseline.output}")
            elif result.baseline.skipped:
                ui.disabled(f"跳过 runtime baseline：{result.baseline.skipped}")
            return 0
        if action == "apply-pending":
            with _working_directory(paths.root):
                changed = asyncio.run(apply_pending_plugins(paths, Settings()))
            if changed:
                paths.restart_required.parent.mkdir(parents=True, exist_ok=True)
                paths.restart_required.write_text("plugin-selection-changed\n", encoding="utf-8")
                ui.success(f"已应用 {changed} 个插件的批准与启用状态")
            else:
                ui.disabled("没有待应用的插件配置")
            return 0
        health = verify_health(str(args.health_url), timeout_seconds=float(args.timeout))
        if health.get("version") != __version__ or health.get("database") != "ok":
            raise SetupValidationError("Bot 版本或数据库健康状态与当前部署不一致")
        _render_health(
            ui,
            health,
            flash_enabled=model_profiles_use_flash(paths.model_profiles),
        )
        return 0
    except (QuitRequested, KeyboardInterrupt, EOFError):
        ui.disabled("用户退出，未写入任何配置")
        return 2
    except BackRequested:
        ui.warning("当前页面已经是向导起点")
        return 2
    except (SetupValidationError, OSError, ValueError) as exc:
        ui.error(str(exc) or "Setup 执行失败")
        return 1


def _configure(paths: SetupPaths, ui: TerminalUI) -> int:
    ui.title(f"Yuki {__version__} Guided Setup")
    ui.step(1, 1, "检查部署目录")
    if not paths.root.is_dir():
        raise SetupValidationError("部署目录不存在")
    if not paths.env_example.is_file():
        raise SetupValidationError("部署目录不是完整的 Yuki Release 部署包")
    if not os.access(paths.root, os.W_OK):
        raise SetupValidationError("部署目录不可写")
    ui.success("部署目录可写，配置模板存在")

    document = EnvironmentDocument.load(paths)
    environment = document.values()
    initial = not paths.env.is_file()
    mcp_document, mcp_error = _read_mcp_for_setup(paths.mcp)
    draft = _SetupDraft(
        environment=environment,
        protocol=infer_main_protocol(paths.model_profiles, environment),
        flash_enabled=model_profiles_use_flash(paths.model_profiles),
        mcp_document=mcp_document,
    )
    draft.environment["YUKI_VERSION"] = __version__
    draft.environment["MODEL_PROFILES_FILE"] = "config/model_profiles.toml"
    draft.environment["MCP_CONFIG_PATH"] = ".mcp.json"
    draft.environment["ONEBOT_ACCESS_TOKEN"] = _token_or_existing(
        draft.environment.get("ONEBOT_ACCESS_TOKEN", "")
    )
    draft.environment["NAPCAT_WEBUI_TOKEN"] = _token_or_existing(
        draft.environment.get("NAPCAT_WEBUI_TOKEN", "")
    )

    forced_sections: set[str] = set()
    if mcp_error is not None:
        _rescue_broken_mcp(ui, draft, mcp_error)
        if draft.write_mcp and _as_bool(draft.environment.get("MCP_ENABLED", "false")):
            forced_sections.add("mcp")

    if not initial:
        try:
            _require_base_configuration(draft.environment)
        except SetupValidationError:
            ui.warning("现有基础配置不完整，已自动加入“基础配置”修复页面")
            forced_sections.add("basic")

    while True:
        sections: tuple[str, ...]
        if initial:
            sections = _SECTIONS
        else:
            sections = _select_sections(ui, draft)
            sections = tuple(
                section for section in _SECTIONS if section in set(sections).union(forced_sections)
            )
            if not sections and not draft.rescue_changed:
                ui.disabled("未选择任何配置区块，未写入配置")
                return 2
        result, draft = _run_page_state_machine(
            paths=paths,
            ui=ui,
            document=document,
            draft=draft,
            sections=sections,
            initial=initial,
        )
        if result is None:
            continue
        return result


def _run_page_state_machine(
    *,
    paths: SetupPaths,
    ui: TerminalUI,
    document: EnvironmentDocument,
    draft: _SetupDraft,
    sections: tuple[str, ...],
    initial: bool,
) -> tuple[int | None, _SetupDraft]:
    titles = {
        "basic": "基础配置与主模型",
        "flash": "Flash 模型",
        "embedding": "Embedding",
        "web": "Web 搜索",
        "vision": "Vision",
        "mcp": "MCP",
        "plugin": "Plugin",
        "automation": "Automation",
        "speech": "Speech",
    }
    handlers = {
        "basic": _page_basic,
        "flash": _page_flash,
        "embedding": _page_embedding,
        "web": _page_web,
        "vision": _page_vision,
        "mcp": _page_mcp,
        "plugin": _page_plugin,
        "automation": _page_automation,
        "speech": _page_speech,
    }
    pages = tuple(_WizardPage(section, titles[section]) for section in sections)
    session_entry = copy.deepcopy(draft)
    page_index = 0
    total = len(pages) + 1
    entry_snapshots: dict[int, _SetupDraft] = {}
    while True:
        entry_snapshots.setdefault(page_index, copy.deepcopy(draft))
        try:
            if page_index < len(pages):
                page = pages[page_index]
                ui.step(page_index + 1, total, page.title)
                ui.navigation_hint()
                handlers[page.section](paths, ui, draft)
                entry_snapshots.pop(page_index, None)
                page_index += 1
                continue
            ui.step(total, total, "验证、摘要与确认")
            ui.navigation_hint()
            result = _review_and_commit(
                paths=paths,
                ui=ui,
                document=document,
                draft=draft,
                sections=sections,
                initial=initial,
            )
            return result, draft
        except BackRequested:
            draft = entry_snapshots.pop(page_index)
            if page_index == 0:
                if initial:
                    ui.warning("当前已经是第一个配置页面")
                    continue
                return None, session_entry
            page_index -= 1
            entry_snapshots.pop(page_index, None)
        except SetupValidationError as exc:
            ui.error(str(exc) or "当前页面配置无效")
            if page_index >= len(pages):
                if pages:
                    page_index = len(pages) - 1
                    entry_snapshots.pop(page_index, None)
                else:
                    raise
            ui.warning("本页没有通过验证，尚未保存；接下来会重新显示本页。")
            ui.warning(
                "可以修正后重试，或输入 :back 返回、输入 :quit 退出；"
                "两个命令都必须带开头的英文冒号“:”。"
            )


def _page_basic(paths: SetupPaths, ui: TerminalUI, draft: _SetupDraft) -> None:
    del paths
    environment = draft.environment
    environment["SUPERUSERS"] = _ask_qq(
        ui,
        default=_real_value(environment.get("SUPERUSERS", "")),
    )
    draft.protocol = ui.choose(
        "主模型接入类型",
        (
            ("chat_completions", "OpenAI-compatible Chat Completions"),
            ("responses", "DeepSeek Responses（支持模型原生搜索）"),
        ),
        default=draft.protocol,
    )
    environment["LLM_PROVIDER"] = (
        "deepseek" if draft.protocol == "responses" else "openai_compatible"
    )
    if draft.protocol == "responses":
        ui.info("DeepSeek Responses 可使用原生搜索；请求不会发送 tool_choice 字段。")
    else:
        ui.warning("主模型必须支持 Function Calling 才能运行 Agent 工具。")
    environment["LLM_BASE_URL"] = ui.ask(
        "主模型 Base URL",
        default=_real_value(environment.get("LLM_BASE_URL", "")),
        required=True,
    )
    environment["LLM_API_KEY"] = _ask_required_secret(
        ui,
        "主模型 API Key",
        environment.get("LLM_API_KEY", ""),
    )
    environment["LLM_MODEL"] = ui.ask(
        "主模型名称",
        default=_real_value(environment.get("LLM_MODEL", "")),
        required=True,
    )
    _require_base_configuration(environment)


def _page_flash(paths: SetupPaths, ui: TerminalUI, draft: _SetupDraft) -> None:
    del paths
    environment = draft.environment
    ui.info("Flash 用于后台结构化任务，会增加一个模型连接。")
    draft.flash_enabled = ui.confirm("启用 Flash 模型？", default=draft.flash_enabled)
    if not draft.flash_enabled:
        return
    reuse = ui.confirm("复用主模型 Base URL 和 API Key？", default=True)
    environment["LLM_FLASH_BASE_URL"] = (
        environment["LLM_BASE_URL"]
        if reuse
        else ui.ask(
            "Flash Base URL",
            default=_real_value(environment.get("LLM_FLASH_BASE_URL", "")),
            required=True,
        )
    )
    environment["LLM_FLASH_API_KEY"] = (
        environment["LLM_API_KEY"]
        if reuse
        else _ask_required_secret(
            ui,
            "Flash API Key",
            environment.get("LLM_FLASH_API_KEY", ""),
        )
    )
    environment["LLM_FLASH_MODEL"] = ui.ask(
        "Flash 模型名",
        default=_real_value(environment.get("LLM_FLASH_MODEL", "")),
        required=True,
    )
    _require_http_url("Flash Base URL", environment["LLM_FLASH_BASE_URL"])


def _page_embedding(paths: SetupPaths, ui: TerminalUI, draft: _SetupDraft) -> None:
    del paths
    environment = draft.environment
    ui.info("Embedding 提升长期记忆语义召回，关闭后仍保留 SQLite FTS。")
    ui.info("启用后必须填写 API Key；向导只检查是否填写，不会联网验证凭据。")
    enabled = ui.confirm(
        "启用 Embedding？",
        default=_as_bool(environment.get("MEMORY_EMBEDDING_ENABLED", "false")),
    )
    environment["MEMORY_EMBEDDING_ENABLED"] = _bool_text(enabled)
    if not enabled:
        return
    environment["MEMORY_EMBEDDING_BASE_URL"] = ui.ask(
        "Embedding Base URL",
        default=_real_value(environment.get("MEMORY_EMBEDDING_BASE_URL", ""))
        or "https://dashscope.aliyuncs.com/api/v1",
        required=True,
    )
    environment["MEMORY_EMBEDDING_MODEL"] = ui.ask(
        "Embedding 模型",
        default=_real_value(environment.get("MEMORY_EMBEDDING_MODEL", ""))
        or "qwen3.7-text-embedding",
        required=True,
    )
    environment["MEMORY_EMBEDDING_API_KEY"] = _ask_required_secret(
        ui,
        "Embedding API Key",
        environment.get("MEMORY_EMBEDDING_API_KEY", ""),
    )
    _require_http_url("Embedding Base URL", environment["MEMORY_EMBEDDING_BASE_URL"])


def _page_web(paths: SetupPaths, ui: TerminalUI, draft: _SetupDraft) -> None:
    del paths
    environment = draft.environment
    ui.info("Web 搜索可关闭、使用主模型原生搜索，或使用 Tavily。")
    web_choices = [("disabled", "关闭"), ("tavily", "Tavily")]
    if draft.protocol == "responses":
        web_choices.insert(1, ("native", "模型原生搜索"))
    current = environment.get("WEB_MODE", "disabled").casefold()
    if current not in {item[0] for item in web_choices}:
        current = "disabled"
    web_mode = ui.choose("Web 搜索方式", tuple(web_choices), default=current)
    environment["WEB_MODE"] = web_mode
    environment["WEB_ENABLED"] = "false"
    if web_mode == "tavily":
        environment["TAVILY_API_KEY"] = _ask_required_secret(
            ui,
            "Tavily API Key",
            environment.get("TAVILY_API_KEY", ""),
        )


def _page_vision(paths: SetupPaths, ui: TerminalUI, draft: _SetupDraft) -> None:
    del paths
    environment = draft.environment
    ui.info("Vision 用于图片理解和部分表情分析，会调用独立视觉模型。")
    enabled = ui.confirm(
        "启用 Vision？",
        default=_as_bool(environment.get("VISION_ENABLED", "false")),
    )
    environment["VISION_ENABLED"] = _bool_text(enabled)
    if not enabled:
        return
    environment["VISION_BASE_URL"] = ui.ask(
        "Vision Base URL",
        default=_real_value(environment.get("VISION_BASE_URL", "")),
        required=True,
    )
    environment["VISION_API_KEY"] = _ask_required_secret(
        ui,
        "Vision API Key",
        environment.get("VISION_API_KEY", ""),
    )
    environment["VISION_MODEL"] = ui.ask(
        "Vision 模型名",
        default=_real_value(environment.get("VISION_MODEL", "")),
        required=True,
    )
    _require_http_url("Vision Base URL", environment["VISION_BASE_URL"])


def _page_mcp(paths: SetupPaths, ui: TerminalUI, draft: _SetupDraft) -> None:
    environment = draft.environment
    ui.info("MCP 连接外部工具；Docker 引导版仅支持 Streamable HTTP。")
    ui.info("启用后至少配置一个未禁用 Server；配置过程中可输入 :back 或 :quit。")
    enabled = ui.confirm(
        "启用 MCP？",
        default=_as_bool(environment.get("MCP_ENABLED", "false")),
    )
    environment["MCP_ENABLED"] = _bool_text(enabled)
    draft.write_mcp = True
    if enabled:
        draft.mcp_document = _configure_mcp(
            ui,
            paths,
            environment,
            draft.mcp_document,
            allow_keep=not draft.rescue_changed,
        )
    elif not paths.mcp.is_file() or draft.rescue_changed:
        draft.mcp_document = {"mcpServers": {}}


def _page_plugin(paths: SetupPaths, ui: TerminalUI, draft: _SetupDraft) -> None:
    environment = draft.environment
    ui.info("插件是本地可信代码；必须逐个查看权限并批准。")
    enabled = ui.confirm(
        "启用 Plugin 系统？",
        default=_as_bool(environment.get("PLUGIN_SYSTEM_ENABLED", "false")),
    )
    environment["PLUGIN_SYSTEM_ENABLED"] = _bool_text(enabled)
    if enabled:
        draft.pending_plugins = _select_plugins(
            ui, paths, environment, initial=not paths.env.is_file()
        )
    else:
        draft.pending_plugins = None if not paths.env.is_file() else ()


def _page_automation(paths: SetupPaths, ui: TerminalUI, draft: _SetupDraft) -> None:
    del paths
    environment = draft.environment
    ui.info("Automation 支持提醒和周期任务，不会自动创建任务。")
    enabled = ui.confirm(
        "启用 Automation？",
        default=_as_bool(environment.get("AUTOMATION_ENABLED", "false")),
    )
    environment["AUTOMATION_ENABLED"] = _bool_text(enabled)
    if not enabled:
        return
    timezone = ui.ask(
        "默认 IANA 时区",
        default=environment.get("DEFAULT_TIMEZONE", "Asia/Shanghai"),
        required=True,
    )
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SetupValidationError("默认时区不是有效的 IANA 时区") from exc
    environment["DEFAULT_TIMEZONE"] = timezone


def _page_speech(paths: SetupPaths, ui: TerminalUI, draft: _SetupDraft) -> None:
    environment = draft.environment
    ui.info("Speech 使用本地 Genie 模型，不会自动下载大型模型。")
    enabled = ui.confirm(
        "启用 Speech？",
        default=_as_bool(environment.get("SPEECH_ENABLED", "false")),
    )
    environment["SPEECH_ENABLED"] = _bool_text(enabled)
    environment["COMPOSE_PROFILES"] = "speech" if enabled else ""
    if not enabled:
        return
    speech_root = paths.root / "data/speech"
    genie_data = speech_root / "genie_data"
    if not genie_data.is_dir() or not any(genie_data.iterdir()):
        raise SetupValidationError(
            "Speech 模型目录为空：请先把 Genie 模型放入 data/speech/genie_data，"
            "或在当前页面选择关闭"
        )
    candidates = discover_speech_profiles(speech_root)
    if not candidates:
        raise SetupValidationError(
            "没有合法声线档案：请检查 data/speech/voices/<profile>/profile.toml"
        )
    default_profile = environment.get("SPEECH_DEFAULT_PROFILE", "")
    if default_profile not in {item.profile_id for item in candidates}:
        default_profile = candidates[0].profile_id
    environment["SPEECH_DEFAULT_PROFILE"] = ui.choose(
        "默认声线",
        tuple((item.profile_id, f"{item.display_name} ({item.profile_id})") for item in candidates),
        default=default_profile,
    )


def _review_and_commit(
    *,
    paths: SetupPaths,
    ui: TerminalUI,
    document: EnvironmentDocument,
    draft: _SetupDraft,
    sections: tuple[str, ...],
    initial: bool,
) -> int:
    write_model_profiles = initial or bool({"basic", "flash"}.intersection(sections))
    if write_model_profiles:
        profiles = build_model_profiles(
            main_protocol=draft.protocol,
            flash_enabled=draft.flash_enabled,
        )
    else:
        try:
            profiles = paths.model_profiles.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SetupValidationError(
                "现有部署缺少模型档案，请返回区块选择并选择 basic 或 flash"
            ) from exc
    configuration = SetupConfiguration(
        environment=draft.environment,
        model_profiles=profiles,
        mcp_document=draft.mcp_document,
        pending_plugins=draft.pending_plugins,
        write_model_profiles=write_model_profiles,
        write_mcp=initial or draft.write_mcp or not paths.mcp.is_file(),
    )
    validate_configuration(paths, configuration)
    ui.success("Settings、模型路由和本地配置合同有效")
    ui.warning("API Key 尚未在线验证；本次没有发起任何计费请求")
    _render_summary(
        ui,
        draft.environment,
        draft.protocol,
        draft.flash_enabled,
        mcp_document=draft.mcp_document,
        pending_plugins=draft.pending_plugins,
    )
    if not ui.confirm("确认写入以上配置？", default=False):
        ui.disabled("用户取消，未写入任何配置")
        return 2
    for relative in _PERSISTENT_DIRECTORIES:
        (paths.root / relative).mkdir(parents=True, exist_ok=True)
    backup = commit_configuration(paths, document, configuration)
    if backup is not None:
        ui.info(f"原配置已备份到 {backup.relative_to(paths.root)}")
    ui.success("配置写入完成")
    ui.info("下一步由安装脚本应用容器动作并执行健康检查")
    return 0


def _select_sections(ui: TerminalUI, draft: _SetupDraft) -> tuple[str, ...]:
    ui.title("选择要修改的配置区块")
    ui.info("检测到现有部署；默认不修改任何区块。")
    ui.navigation_hint()
    labels = {
        "basic": "基础配置与主模型（已配置）",
        "flash": f"Flash（{'开启' if draft.flash_enabled else '关闭'}）",
        "embedding": _feature_label(
            "Embedding", _as_bool(draft.environment.get("MEMORY_EMBEDDING_ENABLED", "false"))
        ),
        "web": f"Web（{draft.environment.get('WEB_MODE', 'disabled')}）",
        "vision": _feature_label(
            "Vision", _as_bool(draft.environment.get("VISION_ENABLED", "false"))
        ),
        "mcp": _feature_label("MCP", _as_bool(draft.environment.get("MCP_ENABLED", "false"))),
        "plugin": _feature_label(
            "Plugin", _as_bool(draft.environment.get("PLUGIN_SYSTEM_ENABLED", "false"))
        ),
        "automation": _feature_label(
            "Automation", _as_bool(draft.environment.get("AUTOMATION_ENABLED", "false"))
        ),
        "speech": _feature_label(
            "Speech", _as_bool(draft.environment.get("SPEECH_ENABLED", "false"))
        ),
    }
    return ui.choose_many(
        "配置区块：",
        tuple((section, labels[section]) for section in _SECTIONS),
    )


def _rescue_broken_mcp(ui: TerminalUI, draft: _SetupDraft, error: str) -> None:
    while True:
        ui.title("检测到损坏的 MCP 配置")
        ui.warning(error)
        ui.navigation_hint()
        try:
            action = ui.choose(
                "请选择救援方式",
                (
                    ("repair", "进入 MCP 页面重新创建或导入"),
                    ("reset", "重置为空配置并关闭 MCP"),
                    ("disable", "关闭 MCP，但保留原文件供人工修复"),
                ),
                default="disable",
            )
        except BackRequested:
            ui.warning("当前已经是配置救援起点")
            continue
        if action == "repair":
            draft.mcp_document = {"mcpServers": {}}
            draft.environment["MCP_ENABLED"] = "true"
            draft.write_mcp = True
        elif action == "reset":
            draft.mcp_document = {"mcpServers": {}}
            draft.environment["MCP_ENABLED"] = "false"
            draft.write_mcp = True
        else:
            draft.mcp_document = {"mcpServers": {}}
            draft.environment["MCP_ENABLED"] = "false"
            draft.write_mcp = False
        draft.rescue_changed = True
        return


def _feature_label(name: str, enabled: bool) -> str:
    return f"{name}（{'开启' if enabled else '关闭'}）"


def _require_http_url(label: str, value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SetupValidationError(f"{label} 必须是绝对 HTTP(S) 地址")


def _configure_mcp(
    ui: TerminalUI,
    paths: SetupPaths,
    environment: dict[str, str],
    current: dict[str, object],
    *,
    allow_keep: bool = True,
) -> dict[str, object]:
    choices: list[tuple[str, str]] = [("create", "创建 HTTP Server"), ("import", "导入 .mcp.json")]
    if paths.mcp.is_file() and allow_keep and _mcp_has_enabled_server(current):
        choices.insert(0, ("keep", "保留并重新验证现有配置"))
    action = ui.choose("MCP 配置方式", tuple(choices), default=choices[0][0])
    if action == "keep":
        document: object = current
    elif action == "import":
        ui.info("导入文件必须位于当前部署目录或其已挂载子目录中。")
        source = Path(ui.ask("导入文件路径", required=True)).expanduser()
        if not source.is_absolute():
            source = paths.root / source
        try:
            source.resolve().relative_to(paths.root.resolve())
        except ValueError as exc:
            raise SetupValidationError("MCP 导入文件必须位于当前部署目录内") from exc
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SetupValidationError("无法读取 MCP 配置") from exc
    else:
        servers: dict[str, object] = {}
        while True:
            server_id = ui.ask("Server ID", required=True)
            if server_id in servers:
                raise SetupValidationError("MCP Server ID 重复")
            url = ui.ask("Streamable HTTP URL", required=True)
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise SetupValidationError("MCP URL 必须是绝对 HTTP(S) 地址")
            server: dict[str, object] = {"url": url, "lifecycle": "lazy"}
            if ui.confirm("该 Server 需要认证 Header？", default=False):
                header = ui.ask("Header 名称", default="Authorization", required=True)
                secret = ui.ask_secret(f"{header} 值")
                if not secret:
                    raise SetupValidationError("认证 Header 值不能为空")
                server["headers"] = {header: secret}
            servers[server_id] = server
            ui.success(f"已暂存 MCP Server：{server_id}")
            next_action = ui.choose(
                "下一步",
                (
                    ("finish", "完成 MCP 配置并继续"),
                    ("add", "添加另一个 MCP Server"),
                ),
                default="finish",
            )
            if next_action == "finish":
                break
            ui.info("开始添加另一个 MCP Server；也可输入 :back 或 :quit。")
        document = {"mcpServers": servers}
    sanitized = sanitize_mcp_document(document, environment)
    for name in missing_mcp_environment(sanitized, environment):
        secret = ui.ask_secret(f"MCP 环境变量 {name}")
        if not secret:
            raise SetupValidationError(f"MCP 环境变量 {name} 不能为空")
        environment[name] = secret
    return sanitized


def _mcp_has_enabled_server(document: dict[str, object]) -> bool:
    servers = document.get("mcpServers")
    return bool(
        isinstance(servers, dict)
        and any(
            isinstance(server, dict) and not bool(server.get("disabled", False))
            for server in servers.values()
        )
    )


def _select_plugins(
    ui: TerminalUI,
    paths: SetupPaths,
    environment: dict[str, str],
    *,
    initial: bool,
) -> tuple[str, ...]:
    discovery = PluginDiscovery(paths.root / "plugins", yuki_version=__version__, plugin_api="2.0")
    selected: list[str] = []
    found = discovery.discover()
    valid = tuple(item.manifest for item in found if item.manifest is not None)
    invalid = tuple(item for item in found if item.manifest is None)
    for item in invalid:
        ui.warning(f"跳过无效插件目录：{item.record.directory.name}")
    if not valid:
        ui.disabled("没有发现可批准的插件")
        return ()
    states = (
        {}
        if initial
        else asyncio.run(
            load_plugin_setup_states(
                paths,
                database_url=environment.get(
                    "DATABASE_URL", "sqlite+aiosqlite:///./data/qq_ai_bot.db"
                ),
            )
        )
    )
    for manifest in valid:
        requested = tuple(sorted(item.value for item in manifest.permissions))
        permissions = ", ".join(requested) or "无额外权限"
        state = states.get(manifest.id)
        approved_matches = bool(
            state is not None
            and state.enabled
            and (
                state.approved_permissions is None
                or tuple(sorted(state.approved_permissions)) == requested
            )
        )
        ui.line(f"插件：{manifest.name} {manifest.version} ({manifest.id})")
        ui.line(f"  请求权限：{permissions}")
        ui.line(f"  当前状态：{'已批准并启用' if approved_matches else '关闭'}")
        if state is not None and state.enabled and not approved_matches:
            ui.warning("插件权限或 Manifest 已变化；旧批准不会沿用，需重新批准。")
        action = ui.choose(
            "  本次处理",
            (
                ("keep", "保持当前状态"),
                ("enable", "批准当前权限并启用"),
                ("disable", "关闭"),
            ),
            default="keep",
        )
        if action == "enable" or (action == "keep" and approved_matches):
            selected.append(manifest.id)
    return tuple(selected)


def _load_current_configuration(
    paths: SetupPaths,
) -> tuple[SetupConfiguration, EnvironmentDocument]:
    document = EnvironmentDocument.load(paths)
    environment = document.values()
    if not paths.env.is_file():
        raise SetupValidationError("尚未生成 .env，请先运行 qq-ai-bot-cli setup")
    try:
        profiles = paths.model_profiles.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SetupValidationError("无法读取 config/model_profiles.toml") from exc
    mcp_document, mcp_error = _read_mcp_for_setup(paths.mcp)
    if mcp_error is not None and _as_bool(environment.get("MCP_ENABLED", "false")):
        raise SetupValidationError(mcp_error)
    return (
        SetupConfiguration(
            environment=environment,
            model_profiles=profiles,
            mcp_document=mcp_document,
            pending_plugins=None,
        ),
        document,
    )


def _read_mcp(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"mcpServers": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SetupValidationError("现有 .mcp.json 无效") from exc
    if not isinstance(payload, dict):
        raise SetupValidationError("现有 .mcp.json 根节点必须是对象")
    return {str(key): value for key, value in payload.items()}


def _read_mcp_for_setup(path: Path) -> tuple[dict[str, object], str | None]:
    try:
        return _read_mcp(path), None
    except SetupValidationError as exc:
        return {"mcpServers": {}}, str(exc)


def _render_summary(
    ui: TerminalUI,
    environment: dict[str, str],
    protocol: str,
    flash_enabled: bool,
    *,
    mcp_document: dict[str, object],
    pending_plugins: tuple[str, ...] | None,
) -> None:
    masked_qq = environment["SUPERUSERS"][-4:].rjust(len(environment["SUPERUSERS"]), "*")
    ui.line(f"管理员 QQ：{masked_qq}")
    ui.line(f"主模型协议：{protocol}")
    ui.line(f"主模型：{environment['LLM_MODEL']}")
    ui.line("主模型 API Key：已配置")
    if flash_enabled:
        ui.line(f"Flash 模型：{environment.get('LLM_FLASH_MODEL', '未配置')}")
        ui.line("Flash API Key：已配置")
    if _as_bool(environment.get("MEMORY_EMBEDDING_ENABLED", "false")):
        ui.line(f"Embedding 模型：{environment.get('MEMORY_EMBEDDING_MODEL', '未配置')}")
        ui.line("Embedding API Key：已配置")
    if environment.get("WEB_MODE", "disabled") == "tavily":
        ui.line("Tavily API Key：已配置")
    if _as_bool(environment.get("VISION_ENABLED", "false")):
        ui.line(f"Vision 模型：{environment.get('VISION_MODEL', '未配置')}")
        ui.line("Vision API Key：已配置")
    states = {
        "Flash": flash_enabled,
        "Embedding": _as_bool(environment.get("MEMORY_EMBEDDING_ENABLED", "false")),
        "Web": environment.get("WEB_MODE", "disabled") != "disabled",
        "Vision": _as_bool(environment.get("VISION_ENABLED", "false")),
        "MCP": _as_bool(environment.get("MCP_ENABLED", "false")),
        "Plugin": _as_bool(environment.get("PLUGIN_SYSTEM_ENABLED", "false")),
        "Automation": _as_bool(environment.get("AUTOMATION_ENABLED", "false")),
        "Speech": _as_bool(environment.get("SPEECH_ENABLED", "false")),
    }
    for name, enabled in states.items():
        (ui.success if enabled else ui.disabled)(f"{name}：{'开启' if enabled else '关闭'}")
    ui.line(f"Web 模式：{environment.get('WEB_MODE', 'disabled')}")
    if states["MCP"]:
        raw_servers = mcp_document.get("mcpServers", {})
        server_ids = (
            tuple(str(item) for item in raw_servers) if isinstance(raw_servers, dict) else ()
        )
        ui.line(f"MCP Server：{', '.join(server_ids) if server_ids else '未配置'}")
    if pending_plugins is not None:
        ui.line("Plugin 待应用：" + (", ".join(pending_plugins) if pending_plugins else "全部关闭"))
    if states["Automation"]:
        ui.line(f"默认时区：{environment.get('DEFAULT_TIMEZONE', '未配置')}")
    if states["Speech"]:
        ui.line(f"默认声线：{environment.get('SPEECH_DEFAULT_PROFILE', '未配置')}")


def _render_health(
    ui: TerminalUI,
    health: dict[str, Any],
    *,
    flash_enabled: bool,
) -> None:
    ui.title("Yuki 部署状态")
    ui.success(f"Yuki 版本：{health.get('version', 'unknown')}")
    ui.success(f"Bot 状态：{health.get('status', 'unknown')}")
    ui.success(f"数据库：{health.get('database', 'unknown')}")
    enabled: list[str] = ["Flash"] if flash_enabled else []
    for key, label in (
        ("memory_embedding_enabled", "Embedding"),
        ("web_configured", "Web"),
        ("vision_configured", "Vision"),
        ("mcp_enabled", "MCP"),
        ("plugin_system_enabled", "Plugin"),
        ("automation_enabled", "Automation"),
        ("speech_enabled", "Speech"),
    ):
        if bool(health.get(key)):
            enabled.append(label)
    ui.info("已启用功能：" + (", ".join(enabled) if enabled else "仅基础功能"))
    ui.info("NapCat WebUI：http://127.0.0.1:6099")
    ui.info("登录 Token 保存在部署目录 .env 的 NAPCAT_WEBUI_TOKEN 中")


def _ask_required_secret(ui: TerminalUI, label: str, existing: str) -> str:
    configured = bool(_real_value(existing))
    value = ui.ask_secret(label, configured=configured)
    if value:
        return value
    if configured:
        return existing
    raise SetupValidationError(
        f"{label} 未填写。本地验证失败：启用此功能必须提供凭据；"
        "向导不会联网判断 API Key 是否真实有效"
    )


def _ask_qq(ui: TerminalUI, *, default: str) -> str:
    value = ui.ask("管理员 QQ", default=default, required=True)
    if not value.isdigit() or not 5 <= len(value) <= 20:
        raise SetupValidationError("管理员 QQ 必须是 5 到 20 位数字")
    return value


def _require_base_configuration(environment: dict[str, str]) -> None:
    required = ("SUPERUSERS", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
    missing = [key for key in required if not _real_value(environment.get(key, ""))]
    if missing:
        raise SetupValidationError("基础配置不完整：" + ", ".join(missing))
    parsed = urlsplit(environment["LLM_BASE_URL"])
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SetupValidationError("主模型 Base URL 必须是绝对 HTTP(S) 地址")


def _token_or_existing(value: str) -> str:
    return value if _real_value(value) else secrets.token_urlsafe(32)


def _real_value(value: str) -> str:
    normalized = value.strip()
    return "" if normalized.casefold().startswith("replace-with-") else normalized


def _as_bool(value: str) -> bool:
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


@contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)
