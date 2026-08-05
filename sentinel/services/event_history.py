from web3 import AsyncWeb3

from sentinel.config import Config, get_config
from sentinel.models import Event
from sentinel.modules.base import ModuleAdapter
from sentinel.web3_event_log_reader import Web3EventLogReader
from sentinel.web3_events import build_event_bindings


class Web3EventHistory:
    """Fetch raw chain events by block range for aggregation windows."""

    def __init__(
        self,
        w3: AsyncWeb3,
        *,
        module_adapter: ModuleAdapter,
    ) -> None:
        cfg = get_config()
        event_bindings = build_event_bindings(module_adapter)
        self._event_log_reader = Web3EventLogReader(
            w3,
            event_sources=event_bindings.event_sources,
            abi_by_topics=event_bindings.abi_by_topics,
            request_interval_seconds=self._request_interval_from_config(cfg),
            block_batch_size=cfg.block_batch_size,
        )

    async def fetch_events(self, start_block: int, end_block: int) -> list[Event]:
        block_events = await self._event_log_reader.fetch_events(
            start_block=start_block,
            end_block=end_block,
        )
        if block_events is None:
            return []
        return sorted(
            block_events,
            key=lambda event: (event.block, event.transaction_index, event.log_index),
        )

    @staticmethod
    def _request_interval_from_config(cfg: Config) -> float | None:
        rps_limit = cfg.process_blocks_requests_per_second
        return (1 / rps_limit) if rps_limit else None
