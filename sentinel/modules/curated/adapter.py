import logging
from dataclasses import dataclass
from typing import ClassVar

from web3 import AsyncWeb3

from sentinel.app.contracts import (
    CURATED_CONTRACT_ABIS,
    ContractAddresses,
    CuratedContractABIs,
    CuratedContractAddresses,
)
from sentinel.chain import SharedChainConnection
from sentinel.module_types import ModuleType
from sentinel.modules.base import BaseModuleAdapter, EventSource, NodeOperatorOption
from sentinel.modules.curated.texts import CuratedTexts
from sentinel.modules.formatting import read_field

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CuratedModuleContracts:
    module: object
    accounting: object
    parameters_registry: object
    fee_distributor: object
    exit_penalties: object
    meta_registry: object
    lido_locator: object
    staking_router: object
    vebo: object


CURATED_EVENTS = frozenset(
    {
        # CuratedModule
        "DepositedSigningKeysCountChanged",
        "TotalSigningKeysCountChanged",
        "VettedSigningKeysCountDecreased",
        "KeyRemovalChargeApplied",
        "KeyAllocatedBalanceChanged",
        "TargetValidatorsCountChanged",
        "NodeOperatorManagerAddressChangeProposed",
        "NodeOperatorManagerAddressChanged",
        "NodeOperatorRewardAddressChangeProposed",
        "NodeOperatorRewardAddressChanged",
        "GeneralDelayedPenaltyReported",
        "GeneralDelayedPenaltySettled",
        "GeneralDelayedPenaltyCancelled",
        "GeneralDelayedPenaltyCompensated",
        "ValidatorSlashingReported",
        "ValidatorWithdrawn",
        # TODO: Remove the temporary release notification after the CMv2 launch.
        "Resumed",
        # CSAccounting
        "BondCurveSet",
        "CustomRewardsClaimerSet",
        "FeeSplitsSet",
        "BondDebtIncreased",
        "BondDebtCovered",
        "BondDepositedETH",
        "BondDepositedStETH",
        "BondDepositedWstETH",
        "BondClaimedUnstETH",
        "BondClaimedStETH",
        "BondClaimedWstETH",
        "BondBurned",
        "BondCharged",
        "BondLockChanged",
        "BondLockRemoved",
        "BondLockCompensated",
        "BondLockPeriodChanged",
        # CSExitPenalties
        "ValidatorExitDelayProcessed",
        "TriggeredExitFeeRecorded",
        "StrikesPenaltyProcessed",
        # CSFeeDistributor
        "DistributionLogUpdated",
        # ValidatorsExitBusOracle
        "ValidatorExitRequest",
        # MetaRegistry
        "NodeOperatorEffectiveWeightChanged",
        "OperatorGroupCreated",
        "OperatorGroupUpdated",
        "OperatorGroupCleared",
        "BondCurveWeightSet",
        "OperatorMetadataSet",
    }
)

CURATED_FEE_DISTRIBUTOR_EVENTS = frozenset({"DistributionLogUpdated"})
CURATED_VEBO_EVENTS = frozenset({"ValidatorExitRequest"})
CURATED_SIDE_EFFECT_EVENTS = frozenset({"NodeOperatorAdded", "OperatorMetadataSet"})
CURATED_TEMPORARILY_DISABLED_NOTIFIABLE_EVENTS = frozenset(
    {
        # TODO: re-enable after KeyAllocatedBalanceChanged notifications are batched.
        "KeyAllocatedBalanceChanged",
    }
)


class CuratedModuleAdapter(BaseModuleAdapter):
    module_type: ClassVar[ModuleType] = ModuleType.CURATED
    module_name: ClassVar[str] = "Curated Module"
    ui_label: ClassVar[str] = "Curated Module UI"
    texts = CuratedTexts

    def __init__(
        self,
        *,
        addresses: CuratedContractAddresses,
        contracts: CuratedModuleContracts,
        module_ui_url: str | None,
        contract_abis: CuratedContractABIs,
        chain: SharedChainConnection,
    ) -> None:
        if addresses.module_type != ModuleType.CURATED:
            raise RuntimeError(f"Expected curated module, got {addresses.module_type!s}")
        super().__init__(
            addresses=addresses,
            contracts=contracts,
            module_ui_url=module_ui_url,
            contract_abis=contract_abis,
            chain=chain,
        )
        self.curated_addresses = addresses
        self.curated_contract_abis = contract_abis
        self._node_operator_label_cache: dict[int, str] = {}

    @staticmethod
    def contract_abis_for(addresses: ContractAddresses) -> CuratedContractABIs:
        _ = addresses
        return CURATED_CONTRACT_ABIS

    @staticmethod
    def build_contracts(
        w3: AsyncWeb3,
        addresses: CuratedContractAddresses,
        contract_abis: CuratedContractABIs,
    ) -> CuratedModuleContracts:
        return CuratedModuleContracts(
            module=w3.eth.contract(
                address=addresses.module,
                abi=contract_abis.module,
                decode_tuples=True,
            ),
            accounting=w3.eth.contract(
                address=addresses.accounting,
                abi=contract_abis.accounting,
                decode_tuples=True,
            ),
            parameters_registry=w3.eth.contract(
                address=addresses.parameters_registry,
                abi=contract_abis.parameters_registry,
                decode_tuples=True,
            ),
            fee_distributor=w3.eth.contract(
                address=addresses.fee_distributor,
                abi=contract_abis.fee_distributor,
            ),
            exit_penalties=w3.eth.contract(
                address=addresses.exit_penalties,
                abi=contract_abis.exit_penalties,
            ),
            meta_registry=w3.eth.contract(
                address=addresses.meta_registry,
                abi=contract_abis.meta_registry,
                decode_tuples=True,
            ),
            lido_locator=w3.eth.contract(
                address=addresses.lido_locator,
                abi=contract_abis.lido_locator,
            ),
            staking_router=w3.eth.contract(
                address=addresses.staking_router,
                abi=contract_abis.staking_router,
            ),
            vebo=w3.eth.contract(
                address=addresses.vebo,
                abi=contract_abis.vebo,
            ),
        )

    def catalog_events(self) -> set[str]:
        return set(CURATED_EVENTS - CURATED_TEMPORARILY_DISABLED_NOTIFIABLE_EVENTS)

    def notifiable_events(self) -> set[str]:
        return set(CURATED_EVENTS - CURATED_TEMPORARILY_DISABLED_NOTIFIABLE_EVENTS)

    def side_effect_events(self) -> set[str]:
        return set(CURATED_SIDE_EFFECT_EVENTS)

    async def node_operator_label(self, operator_id: int) -> str:
        cached = self._node_operator_label_cache.get(operator_id)
        if cached is not None:
            return cached

        async with self.chain:
            label = await self._fetch_node_operator_label(operator_id)
        return label

    async def node_operator_options(self) -> tuple[NodeOperatorOption, ...]:
        if self._node_operators_count_cache is None:
            async with self.chain:
                self._node_operators_count_cache = await self._fetch_node_operators_count()

        missing_label_ids = [
            node_operator_id
            for node_operator_id in range(self._node_operators_count_cache)
            if node_operator_id not in self._node_operator_label_cache
        ]
        if missing_label_ids:
            async with self.chain:
                for node_operator_id in missing_label_ids:
                    await self._fetch_node_operator_label(node_operator_id)

        return tuple(
            NodeOperatorOption(
                id=node_operator_id,
                label=self._node_operator_label_cache.get(
                    node_operator_id,
                    self._format_node_operator_label(node_operator_id, None),
                ),
            )
            for node_operator_id in range(self._node_operators_count_cache)
        )

    async def warm_up(self) -> None:
        logger.info("Prefetching Curated node operator labels into cache")
        await self.node_operator_options()

    def remember_node_operator_label(self, operator_id: int, name: str | None) -> None:
        label = self._format_node_operator_label(operator_id, name)
        self._node_operator_label_cache[operator_id] = label

    async def _fetch_node_operator_label(self, operator_id: int) -> str:
        cached = self._node_operator_label_cache.get(operator_id)
        if cached is not None:
            return cached

        try:
            metadata = await self.contracts.meta_registry.functions.getOperatorMetadata(
                operator_id
            ).call()
        except Exception:
            logger.warning(
                "Failed to fetch Curated node operator metadata",
                extra={"node_operator_id": operator_id},
                exc_info=True,
            )
            return self._format_node_operator_label(operator_id, None)

        name = read_field(metadata, "name", 0) or None
        label = self._format_node_operator_label(operator_id, name)
        self._node_operator_label_cache[operator_id] = label
        return label

    @staticmethod
    def _format_node_operator_label(operator_id: int, name: str | None) -> str:
        if not name:
            return f"#{operator_id}"
        return f"#{operator_id} - {name}"

    def event_sources(self) -> tuple[EventSource, ...]:
        return (
            EventSource("module", self.addresses.module),
            EventSource("accounting", self.addresses.accounting),
            EventSource("exit_penalties", self.addresses.exit_penalties),
            EventSource(
                "fee_distributor",
                self.addresses.fee_distributor,
                CURATED_FEE_DISTRIBUTOR_EVENTS,
            ),
            EventSource(
                "vebo",
                self.addresses.vebo,
                CURATED_VEBO_EVENTS,
                self.staking_module_id_matches,
            ),
            EventSource(
                "meta_registry",
                self.curated_addresses.meta_registry,
            ),
        )

    def topic_abis(self) -> tuple[list[dict], ...]:
        return (
            self.contract_abis.module,
            self.contract_abis.accounting,
            self.contract_abis.fee_distributor,
            self.contract_abis.exit_penalties,
            self.contract_abis.vebo,
            self.curated_contract_abis.meta_registry,
        )

    def build_event_messages(self):
        from sentinel.modules.curated.events import CuratedEventMessages

        return CuratedEventMessages(self)

    def event_aggregators(self):
        from sentinel.modules.aggregation import (
            OperatorGroupChangeAggregator,
            node_operator_aggregators_from_event_handlers,
        )
        from sentinel.modules.curated.events import CURATED_EVENTS_TO_FOLLOW

        return (
            *node_operator_aggregators_from_event_handlers(CURATED_EVENTS_TO_FOLLOW),
            OperatorGroupChangeAggregator(),
        )
