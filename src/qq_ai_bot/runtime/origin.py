"""Canonical turn origin enumeration.

Moved from ``qq_ai_bot.automation.models`` in R1 so that the runtime layer can
own the vocabulary without depending on the automation domain.  The old module
keeps a compatibility re-export (``from qq_ai_bot.automation.models import
TurnOrigin`` continues to work) until call sites migrate.
"""

from __future__ import annotations

from enum import StrEnum


class TurnOrigin(StrEnum):
    """Where a conversation turn came from.

    The origin is trusted host state: it is assigned by the entry point that
    admitted the turn (message processor, autonomous scheduler, automation
    worker, plugin host) and is never derived from model output.
    """

    USER_MESSAGE = "user_message"
    AUTONOMOUS_GROUP = "autonomous_group"
    SCHEDULED_AUTOMATION = "scheduled_automation"
    PLUGIN_SESSION = "plugin_session"
    PLUGIN_BACKGROUND = "plugin_background"
    SYSTEM_TASK = "system_task"
