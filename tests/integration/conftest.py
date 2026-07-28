import asyncio
from collections.abc import Awaitable, Callable

import pytest_asyncio

from sentinel.config import clear_config, get_config, load_config_from_env, set_config

from .helpers import (
    AnvilInstance,
    discover_contract_addresses_from_url,
    start_anvil,
    start_anvil_node,
    stop_anvil,
)


@pytest_asyncio.fixture
async def resolved_config():
    env_cfg = load_config_from_env()
    addresses = await discover_contract_addresses_from_url(
        env_cfg.web3_socket_providers[0], env_cfg.module_address
    )
    cfg = env_cfg.resolve(addresses)
    set_config(cfg)
    yield cfg
    clear_config()


@pytest_asyncio.fixture
async def anvil_launcher(
    unused_tcp_port_factory, resolved_config
) -> Callable[[int], Awaitable[AnvilInstance]]:
    instances: list[AnvilInstance] = []
    cfg = get_config()
    fork_url = cfg.web3_socket_providers[0]

    async def _launch(fork_block: int) -> AnvilInstance:
        port = unused_tcp_port_factory()
        instance = await start_anvil(fork_block, port, fork_url)
        instances.append(instance)
        return instance

    yield _launch

    await asyncio.gather(*(stop_anvil(instance) for instance in instances), return_exceptions=True)


@pytest_asyncio.fixture
async def local_anvil_fork_launcher(
    unused_tcp_port_factory,
) -> Callable[[int], Awaitable[AnvilInstance]]:
    source = await start_anvil_node(unused_tcp_port_factory())
    instances: list[AnvilInstance] = [source]

    async def _launch(fork_block: int) -> AnvilInstance:
        port = unused_tcp_port_factory()
        instance = await start_anvil(fork_block, port, source.http_url)
        instances.append(instance)
        return instance

    yield source, _launch

    await asyncio.gather(*(stop_anvil(instance) for instance in instances), return_exceptions=True)
