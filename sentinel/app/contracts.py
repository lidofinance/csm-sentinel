import logging
import json
from dataclasses import dataclass
from pathlib import Path

from eth_typing import ChecksumAddress
from web3 import AsyncWeb3, WebSocketProvider

from sentinel.module_types import ModuleType, decode_module_type

logger = logging.getLogger(__name__)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ABI_DIR = Path("abi")
ABI_V3_DIR = ABI_DIR / "v3"


def _load_abi(name: str, *, version: int | None = None) -> list[dict]:
    base_dir = ABI_V3_DIR if version == 3 else ABI_DIR
    with (base_dir / name).open() as fh:
        return json.load(fh)


MODULE_ABI_V3 = _load_abi("CSModule.json", version=3)
CURATED_MODULE_ABI = _load_abi("CuratedModule.json", version=3)
META_REGISTRY_ABI = _load_abi("MetaRegistry.json", version=3)

ACCOUNTING_ABI_V3 = _load_abi("CSAccounting.json", version=3)

FEE_DISTRIBUTOR_ABI_V3 = _load_abi("CSFeeDistributor.json", version=3)

EXIT_PENALTIES_ABI_V3 = _load_abi("CSExitPenalties.json", version=3)

PARAMETERS_REGISTRY_ABI_V3 = _load_abi("CSParametersRegistry.json", version=3)

VEBO_ABI = _load_abi("VEBO.json")

LIDO_LOCATOR_ABI_V3 = _load_abi("LidoLocator.json", version=3)

STAKING_ROUTER_ABI_V3 = _load_abi("StakingRouter.json", version=3)


@dataclass(frozen=True, slots=True)
class BaseContractABIs:
    module: list[dict]
    accounting: list[dict]
    parameters_registry: list[dict]
    fee_distributor: list[dict]
    exit_penalties: list[dict]
    lido_locator: list[dict]
    staking_router: list[dict]
    vebo: list[dict]


@dataclass(frozen=True, slots=True)
class CommunityContractABIs(BaseContractABIs):
    pass


@dataclass(frozen=True, slots=True)
class CuratedContractABIs(BaseContractABIs):
    meta_registry: list[dict]


ContractABIs = CommunityContractABIs | CuratedContractABIs


CONTRACT_ABIS_V3 = CommunityContractABIs(
    module=MODULE_ABI_V3,
    accounting=ACCOUNTING_ABI_V3,
    parameters_registry=PARAMETERS_REGISTRY_ABI_V3,
    fee_distributor=FEE_DISTRIBUTOR_ABI_V3,
    exit_penalties=EXIT_PENALTIES_ABI_V3,
    lido_locator=LIDO_LOCATOR_ABI_V3,
    staking_router=STAKING_ROUTER_ABI_V3,
    vebo=VEBO_ABI,
)

CURATED_CONTRACT_ABIS = CuratedContractABIs(
    module=CURATED_MODULE_ABI,
    accounting=ACCOUNTING_ABI_V3,
    parameters_registry=PARAMETERS_REGISTRY_ABI_V3,
    fee_distributor=FEE_DISTRIBUTOR_ABI_V3,
    exit_penalties=EXIT_PENALTIES_ABI_V3,
    meta_registry=META_REGISTRY_ABI,
    lido_locator=LIDO_LOCATOR_ABI_V3,
    staking_router=STAKING_ROUTER_ABI_V3,
    vebo=VEBO_ABI,
)


@dataclass(frozen=True, slots=True)
class BaseContractAddresses:
    module: ChecksumAddress
    accounting: ChecksumAddress
    parameters_registry: ChecksumAddress
    fee_distributor: ChecksumAddress
    exit_penalties: ChecksumAddress
    lido_locator: ChecksumAddress
    staking_router: ChecksumAddress
    vebo: ChecksumAddress
    staking_module_id: int | None
    module_type: ModuleType

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "module": self.module,
            "accounting": self.accounting,
            "parameters_registry": self.parameters_registry,
            "fee_distributor": self.fee_distributor,
            "exit_penalties": self.exit_penalties,
            "lido_locator": self.lido_locator,
            "staking_router": self.staking_router,
            "vebo": self.vebo,
            "staking_module_id": self.staking_module_id,
            "module_type": self.module_type.value,
        }


@dataclass(frozen=True, slots=True)
class CommunityContractAddresses(BaseContractAddresses):
    pass


@dataclass(frozen=True, slots=True)
class CuratedContractAddresses(BaseContractAddresses):
    meta_registry: ChecksumAddress

    def as_dict(self) -> dict[str, str | int | None]:
        return BaseContractAddresses.as_dict(self) | {"meta_registry": self.meta_registry}


ContractAddresses = CommunityContractAddresses | CuratedContractAddresses


async def discover_contract_addresses(w3: AsyncWeb3, module_address: str) -> ContractAddresses:
    """Asynchronously discover dependent contract addresses using the provided provider."""

    if isinstance(w3.provider, WebSocketProvider) and not await w3.is_connected():
        await w3.provider.connect()

    checksum = w3.to_checksum_address
    module_contract = w3.eth.contract(
        address=checksum(module_address),
        abi=CONTRACT_ABIS_V3.module,
        decode_tuples=True,
    )

    module_type_raw = await module_contract.functions.getType().call()
    module_type = decode_module_type(module_type_raw)

    accounting = await module_contract.functions.ACCOUNTING().call()
    parameters_registry = await module_contract.functions.PARAMETERS_REGISTRY().call()
    fee_distributor = await module_contract.functions.FEE_DISTRIBUTOR().call()
    exit_penalties = await module_contract.functions.EXIT_PENALTIES().call()
    lido_locator = await module_contract.functions.LIDO_LOCATOR().call()

    locator = w3.eth.contract(
        address=checksum(_ensure_address(lido_locator, "LIDO_LOCATOR")),
        abi=CONTRACT_ABIS_V3.lido_locator,
    )
    vebo = await locator.functions.validatorsExitBusOracle().call()
    staking_router = await locator.functions.stakingRouter().call()

    staking_router_contract = w3.eth.contract(
        address=checksum(_ensure_address(staking_router, "stakingRouter")),
        abi=CONTRACT_ABIS_V3.staking_router,
    )
    modules = await staking_router_contract.functions.getStakingModules().call()

    module_id = _find_staking_module_id(modules, checksum(module_address))

    module_checksum = checksum(_ensure_address(module_address, "MODULE_ADDRESS"))
    accounting_checksum = checksum(_ensure_address(accounting, "ACCOUNTING()"))
    parameters_registry_checksum = checksum(
        _ensure_address(parameters_registry, "PARAMETERS_REGISTRY()")
    )
    fee_distributor_checksum = checksum(_ensure_address(fee_distributor, "FEE_DISTRIBUTOR()"))
    exit_penalties_checksum = checksum(_ensure_address(exit_penalties, "EXIT_PENALTIES()"))
    lido_locator_checksum = checksum(_ensure_address(lido_locator, "LIDO_LOCATOR()"))
    staking_router_checksum = checksum(_ensure_address(staking_router, "stakingRouter()"))
    vebo_checksum = checksum(_ensure_address(vebo, "validatorsExitBusOracle()"))

    if module_id is None:
        logger.warning(
            "%s module is not registered in Staking Router yet; "
            "VEBO events will be ignored until its module ID is discovered",
            module_type.value,
        )

    if module_type == ModuleType.CURATED:
        meta_registry = await _discover_meta_registry(w3, module_address)
        addresses = CuratedContractAddresses(
            module=module_checksum,
            accounting=accounting_checksum,
            parameters_registry=parameters_registry_checksum,
            fee_distributor=fee_distributor_checksum,
            exit_penalties=exit_penalties_checksum,
            lido_locator=lido_locator_checksum,
            staking_router=staking_router_checksum,
            vebo=vebo_checksum,
            staking_module_id=module_id,
            module_type=module_type,
            meta_registry=checksum(_ensure_address(meta_registry, "META_REGISTRY()")),
        )
    else:
        addresses = CommunityContractAddresses(
            module=module_checksum,
            accounting=accounting_checksum,
            parameters_registry=parameters_registry_checksum,
            fee_distributor=fee_distributor_checksum,
            exit_penalties=exit_penalties_checksum,
            lido_locator=lido_locator_checksum,
            staking_router=staking_router_checksum,
            vebo=vebo_checksum,
            staking_module_id=module_id,
            module_type=module_type,
        )

    return addresses


def _ensure_address(raw_address: str, source: str) -> str:
    if not raw_address or raw_address == ZERO_ADDRESS:
        raise RuntimeError(f"Failed to discover address from {source}")
    return raw_address


async def _discover_meta_registry(w3: AsyncWeb3, module_address: str) -> str:
    curated_module = w3.eth.contract(
        address=w3.to_checksum_address(module_address),
        abi=CURATED_CONTRACT_ABIS.module,
        decode_tuples=True,
    )
    return await curated_module.functions.META_REGISTRY().call()


def _find_staking_module_id(modules: list[tuple], module_address: str) -> int | None:
    for module in modules:
        # getStakingModules returns tuple entries with well known layout
        module_id, staking_module_address = module[0], module[1]
        if staking_module_address.lower() == module_address.lower():
            return int(module_id)
    return None


def log_discovered_addresses(addresses: ContractAddresses) -> None:
    printable = json.dumps(addresses.as_dict(), indent=2, sort_keys=True)
    logger.info("Discovered contract addresses:\n%s", printable)
