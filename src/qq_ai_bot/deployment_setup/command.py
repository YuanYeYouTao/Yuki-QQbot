"""Argparse command and interactive flow for guided Docker deployment."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from qq_ai_bot import __version__
from qq_ai_bot.config import Settings
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
    missing_mcp_environment,
    model_profiles_use_flash,
    sanitize_mcp_document,
    validate_configuration,
    verify_health,
)
from qq_ai_bot.deployment_setup.terminal import TerminalUI
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


def add_setup_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    setup = subparsers.add_parser("setup", help="交互配置版本化 Docker 部署")
    setup.add_argument(
        "setup_action",
        nargs="?",
        choices=("configure", "validate", "apply-pending", "verify"),
        default="configure",
    )
    setup.add_argument("--deployment-root", type=Path, default=Path.cwd())
    setup.add_argument("--no-color", action="store_true")
    setup.add_argument("--health-url", default="http://127.0.0.1:8080/healthz")
    setup.add_argument("--timeout", type=float, default=180.0)


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
        if action == "apply-pending":
            with _working_directory(paths.root):
                changed = asyncio.run(apply_pending_plugins(paths, Settings()))
            restart_marker = paths.root / "data/setup/restart-required"
            if changed:
                restart_marker.parent.mkdir(parents=True, exist_ok=True)
                restart_marker.write_text("plugin-selection-changed\n", encoding="utf-8")
                ui.success(f"已应用 {changed} 个插件的批准与启用状态")
            else:
                restart_marker.unlink(missing_ok=True)
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
    except (SetupValidationError, OSError, ValueError) as exc:
        ui.error(str(exc) or "Setup 执行失败")
        return 1


def _configure(paths: SetupPaths, ui: TerminalUI) -> int:
    ui.title(f"Yuki {__version__} Guided Setup")
    ui.step(1, 7, "检查部署目录")
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
    sections = frozenset(_SECTIONS if initial else _select_sections(ui))
    current_protocol = infer_main_protocol(paths.model_profiles, environment)
    flash_enabled = model_profiles_use_flash(paths.model_profiles)
    mcp_document = _read_mcp(paths.mcp)
    pending_plugins: tuple[str, ...] | None = None

    environment["YUKI_VERSION"] = __version__
    environment["MODEL_PROFILES_FILE"] = "config/model_profiles.toml"
    environment["MCP_CONFIG_PATH"] = ".mcp.json"
    environment["ONEBOT_ACCESS_TOKEN"] = _token_or_existing(
        environment.get("ONEBOT_ACCESS_TOKEN", "")
    )
    environment["NAPCAT_WEBUI_TOKEN"] = _token_or_existing(
        environment.get("NAPCAT_WEBUI_TOKEN", "")
    )

    if "basic" in sections:
        ui.step(2, 7, "基础配置与主模型")
        environment["SUPERUSERS"] = _ask_qq(
            ui,
            default=_real_value(environment.get("SUPERUSERS", "")),
        )
        current_protocol = ui.choose(
            "主模型接入类型",
            (
                ("chat_completions", "OpenAI-compatible Chat Completions"),
                ("responses", "DeepSeek Responses（支持模型原生搜索）"),
            ),
            default=current_protocol,
        )
        environment["LLM_PROVIDER"] = (
            "deepseek" if current_protocol == "responses" else "openai_compatible"
        )
        if current_protocol == "responses":
            ui.info("DeepSeek Responses 可使用原生搜索；请求不会发送 tool_choice 字段。")
        else:
            ui.warning("主模型必须支持 Function Calling 才能运行 Planner 和 Agent 工具。")
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

    ui.step(3, 7, "选择扩展能力")
    if "flash" in sections:
        ui.info("Flash 用于 Planner 和后台结构化任务，会增加一个模型连接。")
        flash_enabled = ui.confirm("启用 Flash 模型？", default=flash_enabled)
        if flash_enabled:
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

    if "embedding" in sections:
        ui.info("Embedding 提升长期记忆语义召回，关闭后仍保留 SQLite FTS。")
        enabled = ui.confirm(
            "启用 Embedding？",
            default=_as_bool(environment.get("MEMORY_EMBEDDING_ENABLED", "false")),
        )
        environment["MEMORY_EMBEDDING_ENABLED"] = _bool_text(enabled)
        if enabled:
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

    if "web" in sections:
        ui.info("Web 搜索可关闭、使用主模型原生搜索，或使用 Tavily。")
        web_choices = [("disabled", "关闭"), ("tavily", "Tavily")]
        if current_protocol == "responses":
            web_choices.insert(1, ("native", "模型原生搜索"))
        current_web = "disabled" if initial else environment.get("WEB_MODE", "disabled").casefold()
        if current_web not in {item[0] for item in web_choices}:
            current_web = "disabled"
        web_mode = ui.choose("Web 搜索方式", tuple(web_choices), default=current_web)
        environment["WEB_MODE"] = web_mode
        environment["WEB_ENABLED"] = "false"
        if web_mode == "tavily":
            environment["TAVILY_API_KEY"] = _ask_required_secret(
                ui,
                "Tavily API Key",
                environment.get("TAVILY_API_KEY", ""),
            )

    if "vision" in sections:
        ui.info("Vision 用于图片理解和部分表情分析，会调用独立视觉模型。")
        enabled = ui.confirm(
            "启用 Vision？",
            default=_as_bool(environment.get("VISION_ENABLED", "false")),
        )
        environment["VISION_ENABLED"] = _bool_text(enabled)
        if enabled:
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

    if "mcp" in sections:
        ui.info("MCP 连接外部工具；Docker 引导版仅支持 Streamable HTTP。")
        enabled = ui.confirm(
            "启用 MCP？",
            default=_as_bool(environment.get("MCP_ENABLED", "false")),
        )
        environment["MCP_ENABLED"] = _bool_text(enabled)
        if enabled:
            mcp_document = _configure_mcp(ui, paths, environment, mcp_document)
        elif not paths.mcp.is_file():
            mcp_document = {"mcpServers": {}}

    if "plugin" in sections:
        ui.info("插件是本地可信代码；必须逐个查看权限并批准。")
        enabled = ui.confirm(
            "启用 Plugin 系统？",
            default=_as_bool(environment.get("PLUGIN_SYSTEM_ENABLED", "false")),
        )
        environment["PLUGIN_SYSTEM_ENABLED"] = _bool_text(enabled)
        pending_plugins = _select_plugins(ui, paths) if enabled else (None if initial else ())

    if "automation" in sections:
        ui.info("Automation 支持提醒和周期任务，不会自动创建任务。")
        enabled = ui.confirm(
            "启用 Automation？",
            default=_as_bool(environment.get("AUTOMATION_ENABLED", "false")),
        )
        environment["AUTOMATION_ENABLED"] = _bool_text(enabled)
        if enabled:
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

    if "speech" in sections:
        ui.info("Speech 使用本地 Genie 模型，不会自动下载大型模型。")
        enabled = ui.confirm(
            "启用 Speech？",
            default=_as_bool(environment.get("SPEECH_ENABLED", "false")),
        )
        environment["SPEECH_ENABLED"] = _bool_text(enabled)
        environment["COMPOSE_PROFILES"] = "speech" if enabled else ""
        if enabled:
            speech_root = paths.root / "data/speech"
            genie_data = speech_root / "genie_data"
            if not genie_data.is_dir() or not any(genie_data.iterdir()):
                raise SetupValidationError("Speech 已开启，但 data/speech/genie_data 为空")
            candidates = discover_speech_profiles(speech_root)
            if not candidates:
                raise SetupValidationError("Speech 已开启，但没有合法声线档案")
            default_profile = environment.get("SPEECH_DEFAULT_PROFILE", "")
            if default_profile not in {item.profile_id for item in candidates}:
                default_profile = candidates[0].profile_id
            environment["SPEECH_DEFAULT_PROFILE"] = ui.choose(
                "默认声线",
                tuple(
                    (item.profile_id, f"{item.display_name} ({item.profile_id})")
                    for item in candidates
                ),
                default=default_profile,
            )

    write_model_profiles = initial or bool({"basic", "flash"}.intersection(sections))
    if write_model_profiles:
        profiles = build_model_profiles(
            main_protocol=current_protocol,
            flash_enabled=flash_enabled,
        )
    else:
        try:
            profiles = paths.model_profiles.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SetupValidationError(
                "现有部署缺少模型档案，请重新运行向导并选择 basic 或 flash 区块"
            ) from exc
    configuration = SetupConfiguration(
        environment=environment,
        model_profiles=profiles,
        mcp_document=mcp_document,
        pending_plugins=pending_plugins,
        write_model_profiles=write_model_profiles,
        write_mcp=initial or "mcp" in sections or not paths.mcp.is_file(),
    )

    ui.step(4, 7, "执行本地严格验证")
    validate_configuration(paths, configuration)
    ui.success("Settings、模型路由和本地配置合同有效")
    ui.warning("API Key 尚未在线验证；本次没有发起任何计费请求")

    ui.step(5, 7, "配置摘要")
    _render_summary(ui, environment, current_protocol, flash_enabled)
    if not ui.confirm("确认写入以上配置？", default=False):
        ui.disabled("用户取消，未写入任何配置")
        return 2

    ui.step(6, 7, "原子写入与备份")
    for relative in _PERSISTENT_DIRECTORIES:
        (paths.root / relative).mkdir(parents=True, exist_ok=True)
    backup = commit_configuration(paths, document, configuration)
    if backup is not None:
        ui.info(f"原配置已备份到 {backup.relative_to(paths.root)}")
    ui.success("配置写入完成")
    ui.step(7, 7, "等待安装脚本启动容器")
    ui.info("下一步将执行 docker compose config、pull 和 up -d")
    return 0


def _select_sections(ui: TerminalUI) -> tuple[str, ...]:
    ui.info("检测到现有部署；只会修改你选中的配置区块。")
    ui.line("可选区块：" + ", ".join(_SECTIONS))
    while True:
        value = ui.ask("输入要修改的区块（逗号分隔，all 表示全部）", default="all")
        if value.casefold() == "all":
            return _SECTIONS
        selected = tuple(dict.fromkeys(item.strip().casefold() for item in value.split(",")))
        if selected and set(selected) <= set(_SECTIONS):
            return selected
        ui.error("区块名称无效")


def _configure_mcp(
    ui: TerminalUI,
    paths: SetupPaths,
    environment: dict[str, str],
    current: dict[str, object],
) -> dict[str, object]:
    choices: list[tuple[str, str]] = [("create", "创建 HTTP Server"), ("import", "导入 .mcp.json")]
    if paths.mcp.is_file():
        choices.insert(0, ("keep", "保留并重新验证现有配置"))
    action = ui.choose("MCP 配置方式", tuple(choices), default=choices[0][0])
    if action == "keep":
        document: object = current
    elif action == "import":
        source = Path(ui.ask("导入文件路径", required=True)).expanduser()
        if not source.is_absolute():
            source = paths.root / source
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
            if not ui.confirm("继续添加 MCP Server？", default=False):
                break
        document = {"mcpServers": servers}
    sanitized = sanitize_mcp_document(document, environment)
    for name in missing_mcp_environment(sanitized, environment):
        secret = ui.ask_secret(f"MCP 环境变量 {name}")
        if not secret:
            raise SetupValidationError(f"MCP 环境变量 {name} 不能为空")
        environment[name] = secret
    return sanitized


def _select_plugins(ui: TerminalUI, paths: SetupPaths) -> tuple[str, ...]:
    discovery = PluginDiscovery(paths.root / "plugins", yuki_version=__version__, plugin_api="1.1")
    selected: list[str] = []
    found = discovery.discover()
    valid = tuple(item.manifest for item in found if item.manifest is not None)
    invalid = tuple(item for item in found if item.manifest is None)
    for item in invalid:
        ui.warning(f"跳过无效插件目录：{item.record.directory.name}")
    if not valid:
        ui.disabled("没有发现可批准的插件")
        return ()
    for manifest in valid:
        permissions = ", ".join(item.value for item in manifest.permissions) or "无额外权限"
        ui.line(f"插件：{manifest.name} {manifest.version} ({manifest.id})")
        ui.line(f"  请求权限：{permissions}")
        if ui.confirm("  批准并启用？", default=False):
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
    return (
        SetupConfiguration(
            environment=environment,
            model_profiles=profiles,
            mcp_document=_read_mcp(paths.mcp),
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


def _render_summary(
    ui: TerminalUI,
    environment: dict[str, str],
    protocol: str,
    flash_enabled: bool,
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
    raise SetupValidationError(f"{label}不能为空")


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
