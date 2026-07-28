from asyncio import Task
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sentinel.config import Config

if TYPE_CHECKING:
    from sentinel.app.application import SentinelApplication
    from sentinel.app.health import HealthServer, HealthState
    from sentinel.app.telegram_adapters import TelegramNotificationHandler
    from sentinel.chain import SharedChainConnection
    from sentinel.jobs import JobContext
    from sentinel.modules.base import ModuleAdapter
    from sentinel.services.subscription import ModuleRuntimeSupervisor


@dataclass(slots=True)
class BotRuntime:
    """Lightweight container for the long-lived bot context."""

    application: "SentinelApplication"
    module_supervisor: "ModuleRuntimeSupervisor"
    notification_handler: "TelegramNotificationHandler"
    job_context: "JobContext"
    chain: "SharedChainConnection"
    health: "HealthState"
    health_server: "HealthServer"
    heartbeat_task: Task[None]

    @property
    def config(self) -> Config:
        return self.module_supervisor.cfg

    @property
    def module_adapter(self) -> "ModuleAdapter":
        return self.module_supervisor.module_runtime.module_adapter
