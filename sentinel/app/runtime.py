from dataclasses import dataclass
from typing import TYPE_CHECKING

from sentinel.config import Config

if TYPE_CHECKING:
    from telegram.ext import Application

    from sentinel.events import EventMessages
    from sentinel.jobs import JobContext
    from sentinel.app.module_adapter import ModuleAdapter
    from sentinel.services.subscription import TelegramSubscription


_RUNTIME_ATTR = "_module_runtime"


@dataclass(slots=True)
class BotRuntime:
    """Lightweight container for the long-lived bot context."""

    config: Config
    application: "Application"
    subscription: "TelegramSubscription"
    event_messages: "EventMessages"
    job_context: "JobContext"
    module_adapter: "ModuleAdapter"


def attach_runtime(runtime: BotRuntime) -> None:
    """Attach the runtime to the Application instance for easy lookup."""
    setattr(runtime.application, _RUNTIME_ATTR, runtime)


def get_runtime_from_application(application: "Application") -> BotRuntime:
    runtime = getattr(application, _RUNTIME_ATTR, None)
    if runtime is None:
        raise RuntimeError("Bot runtime is not attached to the application")
    return runtime
