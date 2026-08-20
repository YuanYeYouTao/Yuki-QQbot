"""NoneBot2 application entrypoint and lifespan wiring."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager

import nonebot
from nonebot.adapters.onebot.v11 import Adapter
from nonebot.drivers.fastapi import Driver as FastAPIDriver

from qq_ai_bot.config import Settings
from qq_ai_bot.container import ApplicationContainer, get_container, set_container
from qq_ai_bot.health import HealthPayload, build_health_payload
from qq_ai_bot.logging import configure_logging
from qq_ai_bot.persistence.instance_lock import SQLiteApplicationLock


@contextmanager
def _nonebot_superusers_environment(superusers: frozenset[str]) -> Iterator[None]:
    """Expose SUPERUSERS in the JSON form expected by NoneBot during initialization."""

    previous = os.environ.get("SUPERUSERS")
    os.environ["SUPERUSERS"] = json.dumps(sorted(superusers))
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SUPERUSERS", None)
        else:
            os.environ["SUPERUSERS"] = previous


def bootstrap(settings: Settings | None = None) -> None:
    """Configure NoneBot, routes, adapters, plugins, and resource lifecycle."""

    app_settings = settings or Settings()
    # Yuki accepts the long-standing comma-separated SUPERUSERS setting, while
    # NoneBot's environment source parses its own field as JSON before applying
    # the explicit value below. Temporarily normalize the shared variable so a
    # valid Yuki deployment cannot fail before the application starts.
    with _nonebot_superusers_environment(app_settings.superusers):
        nonebot.init(
            driver="~fastapi",
            host=app_settings.app_host,
            port=app_settings.app_port,
            log_level=app_settings.log_level,
            superusers=set(app_settings.superusers),
            onebot_access_token=app_settings.onebot_access_token or None,
        )
    configure_logging(app_settings.log_level)
    driver = nonebot.get_driver()
    driver.register_adapter(Adapter)
    application_lock = SQLiteApplicationLock(app_settings.sqlite_path)

    @driver.on_startup
    async def startup() -> None:
        application_lock.acquire()
        try:
            container = await ApplicationContainer.create(app_settings)
            set_container(container)
            await container.start()
        except BaseException:
            application_lock.release()
            raise

    @driver.on_shutdown
    async def shutdown() -> None:
        try:
            await get_container().close()
        finally:
            application_lock.release()

    async def healthz() -> HealthPayload:
        return await build_health_payload(get_container())

    if not isinstance(driver, FastAPIDriver):
        raise RuntimeError("FastAPI driver is required")
    driver.server_app.add_api_route("/healthz", healthz, methods=["GET"])

    nonebot.load_plugin("qq_ai_bot.plugins.ai_chat")


def run() -> None:
    """Run the ASGI server until SIGINT or SIGTERM."""

    bootstrap()
    nonebot.run()


if __name__ == "__main__":
    run()
