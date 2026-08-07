from typing import TYPE_CHECKING

from web3 import AsyncWeb3

from sentinel.app.contracts import (
    CommunityContractAddresses,
    ContractAddresses,
    CuratedContractAddresses,
)
from sentinel.chain import SharedChainConnection
from sentinel.modules.base import BaseModuleAdapter
from sentinel.modules.community.adapter import CommunityModuleAdapter
from sentinel.modules.curated.adapter import CuratedModuleAdapter

if TYPE_CHECKING:
    from sentinel.config import Config

__all__ = [
    "build_module_adapter_from_addresses",
    "build_module_adapter_from_config",
]


def build_module_adapter_from_addresses(
    addresses: ContractAddresses,
    w3: AsyncWeb3,
    module_ui_url: str | None,
    chain: SharedChainConnection,
) -> BaseModuleAdapter:
    if isinstance(addresses, CommunityContractAddresses):
        contract_abis = CommunityModuleAdapter.contract_abis_for(addresses)
        contracts = CommunityModuleAdapter.build_contracts(w3, addresses, contract_abis)
        return CommunityModuleAdapter(
            addresses=addresses,
            contracts=contracts,
            contract_abis=contract_abis,
            module_ui_url=module_ui_url,
            chain=chain,
        )
    if isinstance(addresses, CuratedContractAddresses):
        contract_abis = CuratedModuleAdapter.contract_abis_for(addresses)
        contracts = CuratedModuleAdapter.build_contracts(w3, addresses, contract_abis)
        return CuratedModuleAdapter(
            addresses=addresses,
            contracts=contracts,
            contract_abis=contract_abis,
            module_ui_url=module_ui_url,
            chain=chain,
        )
    raise RuntimeError(f"Unsupported module type: {addresses.module_type!s}")


def build_module_adapter_from_config(
    cfg: "Config",
    w3: AsyncWeb3,
    chain: SharedChainConnection,
) -> BaseModuleAdapter:
    return build_module_adapter_from_addresses(cfg.contract_addresses, w3, cfg.module_ui_url, chain)
