from dataclasses import dataclass
from typing import ClassVar

from web3 import AsyncWeb3

from sentinel.app.contracts import (
    CONTRACT_ABIS_V3,
    CommunityContractAddresses,
    CommunityContractABIs,
)
from sentinel.chain import SharedChainConnection
from sentinel.module_types import ModuleType
from sentinel.modules.base import BaseModuleAdapter, EventSource
from sentinel.modules.community.texts import CommunityTexts
from sentinel.modules.distribution import DISTRIBUTION_REPORT_EVENTS


@dataclass(frozen=True, slots=True)
class CommunityModuleContracts:
    module: object
    accounting: object
    parameters_registry: object
    fee_distributor: object
    exit_penalties: object
    lido_locator: object
    staking_router: object
    vebo: object


COMMUNITY_COMMON_EVENTS = frozenset(
    {
        "VettedSigningKeysCountDecreased",
        "DepositedSigningKeysCountChanged",
        "TotalSigningKeysCountChanged",
        "KeyRemovalChargeApplied",
        "BondCurveSet",
        "TargetValidatorsCountChanged",
        "NodeOperatorManagerAddressChangeProposed",
        "NodeOperatorManagerAddressChanged",
        "NodeOperatorRewardAddressChangeProposed",
        "NodeOperatorRewardAddressChanged",
        "ValidatorExitRequest",
        "ValidatorExitDelayProcessed",
        "TriggeredExitFeeRecorded",
        "StrikesPenaltyProcessed",
        "DistributionLogUpdated",
    }
)

COMMUNITY_V3_ONLY_EVENTS = frozenset(
    {
        "GeneralDelayedPenaltyReported",
        "GeneralDelayedPenaltySettled",
        "GeneralDelayedPenaltyCancelled",
        "GeneralDelayedPenaltyCompensated",
        "ValidatorSlashingReported",
        "BondDebtIncreased",
        "BondDebtCovered",
        "CustomRewardsClaimerSet",
        "FeeSplitsSet",
        "BondLockRemoved",
        "KeyAllocatedBalanceChanged",
        "ValidatorWithdrawn",
    }
)

COMMUNITY_EVENTS = COMMUNITY_COMMON_EVENTS | COMMUNITY_V3_ONLY_EVENTS
COMMUNITY_TEMPORARILY_DISABLED_NOTIFIABLE_EVENTS = frozenset(
    {
        # TODO: re-enable after KeyAllocatedBalanceChanged notifications are batched.
        "KeyAllocatedBalanceChanged",
    }
)
COMMUNITY_NOTIFIABLE_EVENTS = COMMUNITY_EVENTS - COMMUNITY_TEMPORARILY_DISABLED_NOTIFIABLE_EVENTS
COMMUNITY_SIDE_EFFECT_EVENTS = frozenset({"NodeOperatorAdded"})


class CommunityModuleAdapter(BaseModuleAdapter):
    module_type: ClassVar[ModuleType] = ModuleType.COMMUNITY
    module_name: ClassVar[str] = "CSM"
    ui_label: ClassVar[str] = "CSM UI"
    texts = CommunityTexts

    def __init__(
        self,
        *,
        addresses: CommunityContractAddresses,
        contracts: CommunityModuleContracts,
        module_ui_url: str | None,
        contract_abis: CommunityContractABIs,
        chain: SharedChainConnection,
    ) -> None:
        if addresses.module_type != ModuleType.COMMUNITY:
            raise RuntimeError(f"Expected community module, got {addresses.module_type!s}")
        super().__init__(
            addresses=addresses,
            contracts=contracts,
            module_ui_url=module_ui_url,
            contract_abis=contract_abis,
            chain=chain,
        )

    @staticmethod
    def contract_abis_for(_addresses: CommunityContractAddresses) -> CommunityContractABIs:
        return CONTRACT_ABIS_V3

    @staticmethod
    def build_contracts(
        w3: AsyncWeb3,
        addresses: CommunityContractAddresses,
        contract_abis: CommunityContractABIs,
    ) -> CommunityModuleContracts:
        return CommunityModuleContracts(
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
        return set(COMMUNITY_EVENTS - COMMUNITY_TEMPORARILY_DISABLED_NOTIFIABLE_EVENTS)

    def notifiable_events(self) -> set[str]:
        return set(COMMUNITY_NOTIFIABLE_EVENTS)

    def side_effect_events(self) -> set[str]:
        return set(COMMUNITY_SIDE_EFFECT_EVENTS)

    def event_sources(self) -> tuple[EventSource, ...]:
        return (
            EventSource("module", self.addresses.module),
            EventSource("accounting", self.addresses.accounting),
            EventSource(
                "vebo",
                self.addresses.vebo,
                frozenset({"ValidatorExitRequest"}),
                self.staking_module_id_matches,
            ),
            EventSource(
                "fee_distributor",
                self.addresses.fee_distributor,
                DISTRIBUTION_REPORT_EVENTS,
            ),
            EventSource("exit_penalties", self.addresses.exit_penalties),
        )

    def topic_abis(self) -> tuple[list[dict], ...]:
        return (
            CONTRACT_ABIS_V3.module,
            CONTRACT_ABIS_V3.accounting,
            CONTRACT_ABIS_V3.fee_distributor,
            CONTRACT_ABIS_V3.vebo,
            CONTRACT_ABIS_V3.exit_penalties,
        )

    def build_event_messages(self):
        from sentinel.modules.community.events import CommunityEventMessages

        return CommunityEventMessages(self)

    def event_aggregators(self):
        from sentinel.modules.aggregation import (
            DistributionReportAggregator,
            node_operator_aggregators_from_event_handlers,
        )
        from sentinel.modules.community.events import COMMUNITY_EVENTS_TO_FOLLOW

        return (
            *node_operator_aggregators_from_event_handlers(COMMUNITY_EVENTS_TO_FOLLOW),
            DistributionReportAggregator(),
        )
