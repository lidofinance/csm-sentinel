from dataclasses import fields, replace

import pytest
from types import SimpleNamespace

from sentinel.app.contracts import (
    CONTRACT_ABIS_V3,
    CommunityContractAddresses,
    ContractAddresses,
    CuratedContractAddresses,
)
from sentinel.app.module_adapter import build_module_adapter_from_addresses
from sentinel.chain import SharedChainConnection
from sentinel.module_types import ModuleType
from sentinel.modules.base import BaseModuleAdapter
from sentinel.modules.texts import BotTexts
from sentinel.modules.community.adapter import (
    CommunityModuleAdapter,
    CommunityModuleContracts,
)
from sentinel.modules.community.texts import CommunityTexts
from sentinel.modules.curated.adapter import (
    CuratedModuleAdapter,
    CuratedModuleContracts,
)
from sentinel.modules.curated.texts import CuratedTexts


class _FakeEth:
    def contract(self, **kwargs):
        return kwargs


class _FakeW3:
    eth = _FakeEth()


class _FakeChain:
    def __init__(self):
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self):
        self.enter_count += 1

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.exit_count += 1


class _FakeCall:
    def __init__(self, value):
        self.value = value

    async def call(self, **kwargs):
        _ = kwargs
        return self.value


class _FakeModuleFunctions:
    def __init__(self, node_operator_count: int):
        self.node_operator_count = node_operator_count
        self.node_operator_count_calls = 0

    def getNodeOperatorsCount(self):
        self.node_operator_count_calls += 1
        return _FakeCall(self.node_operator_count)


class _FakeModuleContract:
    def __init__(self, node_operator_count: int):
        self.functions = _FakeModuleFunctions(node_operator_count)


class _FakeMetaRegistryFunctions:
    def __init__(self, names: dict[int, str | None]):
        self.names = names
        self.metadata_ids: list[int] = []

    def getOperatorMetadata(self, node_operator_id: int):
        self.metadata_ids.append(node_operator_id)
        return _FakeCall(
            {
                "name": self.names.get(node_operator_id),
                "description": "",
                "ownerEditsRestricted": False,
            }
        )


class _FakeMetaRegistry:
    def __init__(self, names: dict[int, str | None]):
        self.functions = _FakeMetaRegistryFunctions(names)


def _dummy_addresses(module_type: ModuleType) -> ContractAddresses:
    common_kwargs = {
        "module": "0x0000000000000000000000000000000000000001",
        "accounting": "0x0000000000000000000000000000000000000002",
        "parameters_registry": "0x0000000000000000000000000000000000000003",
        "fee_distributor": "0x0000000000000000000000000000000000000004",
        "exit_penalties": "0x0000000000000000000000000000000000000005",
        "lido_locator": "0x0000000000000000000000000000000000000006",
        "staking_router": "0x0000000000000000000000000000000000000007",
        "vebo": "0x0000000000000000000000000000000000000008",
        "staking_module_id": 1,
        "module_type": module_type,
    }
    if module_type == ModuleType.CURATED:
        return CuratedContractAddresses(
            **common_kwargs,
            meta_registry="0x0000000000000000000000000000000000000009",
        )
    return CommunityContractAddresses(**common_kwargs)


def _dummy_contracts() -> CommunityModuleContracts:
    return CommunityModuleContracts(
        module=object(),
        accounting=object(),
        parameters_registry=object(),
        fee_distributor=object(),
        exit_penalties=object(),
        lido_locator=object(),
        staking_router=object(),
        vebo=object(),
    )


def _dummy_chain() -> SharedChainConnection:
    return SharedChainConnection(_FakeW3())


def test_community_module_adapter_instantiation():
    addresses = _dummy_addresses(ModuleType.COMMUNITY)
    assert isinstance(addresses, CommunityContractAddresses)
    contracts = _dummy_contracts()
    result = CommunityModuleAdapter(
        addresses=addresses,
        contracts=contracts,
        contract_abis=CONTRACT_ABIS_V3,
        module_ui_url=None,
        chain=_dummy_chain(),
    )
    assert result.module_type == ModuleType.COMMUNITY
    assert result.side_effect_events() == {"NodeOperatorAdded"}


def test_curated_module_adapter_instantiation():
    addresses = _dummy_addresses(ModuleType.CURATED)
    assert isinstance(addresses, CuratedContractAddresses)
    contracts = CuratedModuleContracts(
        module=object(),
        accounting=object(),
        parameters_registry=object(),
        fee_distributor=object(),
        exit_penalties=object(),
        meta_registry=object(),
        lido_locator=object(),
        staking_router=object(),
        vebo=object(),
    )
    result = CuratedModuleAdapter(
        addresses=addresses,
        contracts=contracts,
        contract_abis=CuratedModuleAdapter.contract_abis_for(addresses),
        module_ui_url=None,
        chain=_dummy_chain(),
    )
    assert result.module_type == ModuleType.CURATED
    assert not hasattr(result, "csm_version")
    assert "DepositedSigningKeysCountChanged" in result.catalog_events()
    assert "OperatorGroupCreated" in result.catalog_events()
    assert "Resumed" in result.catalog_events()
    assert "Initialized" not in result.catalog_events()
    assert "KeyAllocatedBalanceChanged" not in result.catalog_events()
    assert "KeyAllocatedBalanceChanged" not in result.notifiable_events()
    assert result.catalog_events() == result.notifiable_events()
    assert result.side_effect_events() == {"NodeOperatorAdded", "OperatorMetadataSet"}


def test_build_curated_module_adapter_uses_curated_module_abi():
    addresses = _dummy_addresses(ModuleType.CURATED)
    assert isinstance(addresses, CuratedContractAddresses)
    result = build_module_adapter_from_addresses(
        addresses,
        _FakeW3(),
        module_ui_url=None,
        chain=_dummy_chain(),
    )

    assert result.module_type == ModuleType.CURATED
    assert result.contract_abis.module is CuratedModuleAdapter.contract_abis_for(addresses).module


@pytest.mark.asyncio
async def test_curated_module_adapter_discovers_module_id_after_startup():
    addresses = replace(
        _dummy_addresses(ModuleType.CURATED),
        staking_module_id=None,
    )
    assert isinstance(addresses, CuratedContractAddresses)
    staking_router = SimpleNamespace(
        functions=SimpleNamespace(
            getStakingModules=lambda: _FakeCall([(5, addresses.module)]),
        )
    )
    adapter = CuratedModuleAdapter(
        addresses=addresses,
        contracts=CuratedModuleContracts(
            module=object(),
            accounting=object(),
            parameters_registry=object(),
            fee_distributor=object(),
            exit_penalties=object(),
            meta_registry=object(),
            lido_locator=object(),
            staking_router=staking_router,
            vebo=object(),
        ),
        contract_abis=CuratedModuleAdapter.contract_abis_for(addresses),
        module_ui_url=None,
        chain=_FakeChain(),
    )
    event = SimpleNamespace(args={"stakingModuleId": 5})

    assert not adapter.staking_module_id_matches(event)

    await adapter.refresh_staking_module_id()

    assert adapter.staking_module_id_matches(event)


@pytest.mark.asyncio
async def test_community_module_adapter_discovers_module_id_after_startup():
    addresses = replace(
        _dummy_addresses(ModuleType.COMMUNITY),
        staking_module_id=None,
    )
    assert isinstance(addresses, CommunityContractAddresses)
    staking_router = SimpleNamespace(
        functions=SimpleNamespace(
            getStakingModules=lambda: _FakeCall([(3, addresses.module)]),
        )
    )
    adapter = CommunityModuleAdapter(
        addresses=addresses,
        contracts=replace(_dummy_contracts(), staking_router=staking_router),
        contract_abis=CONTRACT_ABIS_V3,
        module_ui_url=None,
        chain=_FakeChain(),
    )
    event = SimpleNamespace(args={"stakingModuleId": 3})

    assert not adapter.staking_module_id_matches(event)

    await adapter.refresh_staking_module_id()

    assert adapter.staking_module_id_matches(event)


def test_adapter_build_event_list_text_filters_catalog_events():
    class LimitedAdapter(BaseModuleAdapter):
        module_type = ModuleType.COMMUNITY
        texts = CommunityTexts

        def catalog_events(self) -> set[str]:
            return {"BondCurveSet"}

    addresses = _dummy_addresses(ModuleType.COMMUNITY)
    assert isinstance(addresses, CommunityContractAddresses)
    contracts = _dummy_contracts()
    limited = LimitedAdapter(
        addresses=addresses,
        contracts=contracts,
        contract_abis=CONTRACT_ABIS_V3,
        module_ui_url="https://example.invalid",
        chain=_dummy_chain(),
    )

    text = limited.build_event_list_text()
    assert "Node Operator type changed" in text
    assert "Keys were deposited" not in text


def test_community_module_adapter_uses_v3_catalog_and_abis():
    addresses = _dummy_addresses(ModuleType.COMMUNITY)
    assert isinstance(addresses, CommunityContractAddresses)
    contracts = _dummy_contracts()
    adapter = CommunityModuleAdapter(
        addresses=addresses,
        contracts=contracts,
        contract_abis=CommunityModuleAdapter.contract_abis_for(addresses),
        module_ui_url=None,
        chain=_dummy_chain(),
    )

    assert adapter.contract_abis is CONTRACT_ABIS_V3
    assert "GeneralDelayedPenaltyReported" in adapter.catalog_events()
    assert "ValidatorWithdrawn" in adapter.catalog_events()
    assert "Initialized" not in adapter.catalog_events()
    assert "ELRewardsStealingPenaltyReported" not in adapter.catalog_events()
    assert "WithdrawalSubmitted" not in adapter.catalog_events()

    new_v3_events = {
        "ValidatorSlashingReported",
        "BondDebtIncreased",
        "BondDebtCovered",
        "CustomRewardsClaimerSet",
        "FeeSplitsSet",
        "BondLockRemoved",
    }
    assert new_v3_events.issubset(adapter.catalog_events())
    assert "KeyAllocatedBalanceChanged" not in adapter.catalog_events()
    assert "KeyAllocatedBalanceChanged" not in adapter.notifiable_events()


@pytest.mark.asyncio
async def test_community_module_adapter_validates_operator_ids():
    addresses = _dummy_addresses(ModuleType.COMMUNITY)
    assert isinstance(addresses, CommunityContractAddresses)
    chain = _FakeChain()
    contracts = _dummy_contracts()
    contracts = replace(
        contracts,
        module=_FakeModuleContract(node_operator_count=3),
    )
    adapter = CommunityModuleAdapter(
        addresses=addresses,
        contracts=contracts,
        contract_abis=CONTRACT_ABIS_V3,
        module_ui_url=None,
        chain=chain,
    )

    assert await adapter.is_valid_operator_id(0)
    assert await adapter.is_valid_operator_id(2)
    assert not await adapter.is_valid_operator_id(3)
    assert not await adapter.is_valid_operator_id(-1)
    assert chain.enter_count == 1
    assert chain.exit_count == 1


@pytest.mark.asyncio
async def test_curated_module_adapter_validates_operator_ids():
    addresses = _dummy_addresses(ModuleType.CURATED)
    assert isinstance(addresses, CuratedContractAddresses)
    chain = _FakeChain()
    adapter = CuratedModuleAdapter(
        addresses=addresses,
        contracts=CuratedModuleContracts(
            module=_FakeModuleContract(node_operator_count=3),
            accounting=object(),
            parameters_registry=object(),
            fee_distributor=object(),
            exit_penalties=object(),
            meta_registry=object(),
            lido_locator=object(),
            staking_router=object(),
            vebo=object(),
        ),
        contract_abis=CuratedModuleAdapter.contract_abis_for(addresses),
        module_ui_url=None,
        chain=chain,
    )

    assert await adapter.is_valid_operator_id(0)
    assert await adapter.is_valid_operator_id(2)
    assert not await adapter.is_valid_operator_id(3)
    assert not await adapter.is_valid_operator_id(-1)
    assert chain.enter_count == 1
    assert chain.exit_count == 1


@pytest.mark.asyncio
async def test_curated_module_adapter_lists_labeled_node_operator_options_from_cache():
    addresses = _dummy_addresses(ModuleType.CURATED)
    assert isinstance(addresses, CuratedContractAddresses)
    chain = _FakeChain()
    meta_registry = _FakeMetaRegistry({0: "Operator Zero", 1: None, 2: "Operator Two"})
    module_contract = _FakeModuleContract(node_operator_count=3)
    adapter = CuratedModuleAdapter(
        addresses=addresses,
        contracts=CuratedModuleContracts(
            module=module_contract,
            accounting=object(),
            parameters_registry=object(),
            fee_distributor=object(),
            exit_penalties=object(),
            meta_registry=meta_registry,
            lido_locator=object(),
            staking_router=object(),
            vebo=object(),
        ),
        contract_abis=CuratedModuleAdapter.contract_abis_for(addresses),
        module_ui_url=None,
        chain=chain,
    )

    await adapter.warm_up()
    options = await adapter.node_operator_options()
    label = await adapter.node_operator_label(2)
    options_again = await adapter.node_operator_options()

    assert [(option.id, option.label) for option in options] == [
        (0, "#0 - Operator Zero"),
        (1, "#1"),
        (2, "#2 - Operator Two"),
    ]
    assert options_again == options
    assert label == "#2 - Operator Two"
    assert meta_registry.functions.metadata_ids == [0, 1, 2]
    assert module_contract.functions.node_operator_count_calls == 1
    assert chain.enter_count == 2
    assert chain.exit_count == 2


@pytest.mark.asyncio
async def test_curated_module_adapter_uses_label_cache_while_refreshing_operator_count():
    addresses = _dummy_addresses(ModuleType.CURATED)
    assert isinstance(addresses, CuratedContractAddresses)
    module_contract = _FakeModuleContract(node_operator_count=1)
    meta_registry = _FakeMetaRegistry({0: "Old Name", 1: "New Operator"})
    adapter = CuratedModuleAdapter(
        addresses=addresses,
        contracts=CuratedModuleContracts(
            module=module_contract,
            accounting=object(),
            parameters_registry=object(),
            fee_distributor=object(),
            exit_penalties=object(),
            meta_registry=meta_registry,
            lido_locator=object(),
            staking_router=object(),
            vebo=object(),
        ),
        contract_abis=CuratedModuleAdapter.contract_abis_for(addresses),
        module_ui_url=None,
        chain=_FakeChain(),
    )

    await adapter.warm_up()
    adapter.remember_node_operator_label(0, "New Name")
    adapter.remember_node_operator_added(1)

    options = await adapter.node_operator_options()

    assert [(option.id, option.label) for option in options] == [
        (0, "#0 - New Name"),
        (1, "#1 - New Operator"),
    ]
    assert meta_registry.functions.metadata_ids == [0, 1]


def test_bot_text_instances_define_common_texts():
    assert isinstance(CommunityTexts, BotTexts)
    assert isinstance(CuratedTexts, BotTexts)
    assert all(getattr(CommunityTexts, field.name) is not None for field in fields(BotTexts))
    assert all(getattr(CuratedTexts, field.name) is not None for field in fields(BotTexts))
