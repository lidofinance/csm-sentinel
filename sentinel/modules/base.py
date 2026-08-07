from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from eth_typing import ChecksumAddress

from sentinel.app.contracts import ContractABIs, ContractAddresses, _find_staking_module_id
from sentinel.chain import SharedChainConnection
from sentinel.module_types import ModuleType
from sentinel.modules.texts import BotTexts

if TYPE_CHECKING:
    from sentinel.models import Event
    from sentinel.modules.aggregation import EventAggregator
    from sentinel.modules.event_engine import EventMessageEngineBase

EventPredicate = Callable[["Event"], bool]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EventSource:
    name: str
    address: ChecksumAddress
    event_names: frozenset[str] | None = None
    predicate: EventPredicate | None = None


@dataclass(frozen=True, slots=True)
class NodeOperatorOption:
    id: int
    label: str


class BaseModuleAdapter:
    module_type: ClassVar[ModuleType]
    module_name: ClassVar[str] = "Module"
    ui_label: ClassVar[str] = "Module UI"
    texts: ClassVar[BotTexts]

    def __init__(
        self,
        *,
        addresses: ContractAddresses,
        contracts: Any,
        module_ui_url: str | None,
        contract_abis: ContractABIs,
        chain: SharedChainConnection,
    ) -> None:
        self.addresses = addresses
        self.contracts = contracts
        self.contract_abis = contract_abis
        self.chain = chain
        self.module_ui_url = module_ui_url
        self._staking_module_id = addresses.staking_module_id
        self._node_operators_count_cache: int | None = None

    def catalog_events(self) -> set[str]:
        raise NotImplementedError

    def notifiable_events(self) -> set[str]:
        return self.catalog_events()

    def side_effect_events(self) -> set[str]:
        return set()

    def event_sources(self) -> tuple[EventSource, ...]:
        raise NotImplementedError

    def topic_abis(self) -> tuple[list[dict], ...]:
        raise NotImplementedError

    def build_event_list_text(self) -> str:
        return self.texts.build_event_list_text(self.catalog_events(), self.module_ui_url)

    def event_aggregators(self) -> tuple["EventAggregator", ...]:
        return ()

    def build_event_messages(self) -> "EventMessageEngineBase":
        raise NotImplementedError

    async def is_valid_operator_id(self, operator_id: int) -> bool:
        return 0 <= operator_id < await self.node_operators_count()

    async def node_operators_count(self) -> int:
        if self._node_operators_count_cache is not None:
            return self._node_operators_count_cache
        async with self.chain:
            self._node_operators_count_cache = await self._fetch_node_operators_count()
        return self._node_operators_count_cache

    async def _fetch_node_operators_count(self) -> int:
        return await self.contracts.module.functions.getNodeOperatorsCount().call()

    def staking_module_id_matches(self, event: "Event") -> bool:
        return event.args["stakingModuleId"] == self._staking_module_id

    async def refresh_staking_module_id(self) -> None:
        if self._staking_module_id is not None:
            return

        try:
            async with self.chain:
                modules = await self.contracts.staking_router.functions.getStakingModules().call()
        except Exception:
            logger.warning(
                "Failed to refresh %s staking module ID", self.module_name, exc_info=True
            )
            return

        module_id = _find_staking_module_id(modules, self.addresses.module)
        if module_id is None:
            return

        self._staking_module_id = module_id
        logger.info("Discovered %s staking module ID: %s", self.module_name, module_id)

    async def node_operator_label(self, operator_id: int) -> str:
        return f"#{operator_id}"

    async def node_operator_options(self) -> tuple[NodeOperatorOption, ...]:
        return ()

    def remember_node_operator_added(self, operator_id: int) -> None:
        self._node_operators_count_cache = max(
            self._node_operators_count_cache or 0,
            operator_id + 1,
        )

    async def warm_up(self) -> None:
        return None
