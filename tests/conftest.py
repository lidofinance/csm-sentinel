"""Shared pytest configuration for the test suite."""

from pathlib import Path

from dotenv import load_dotenv
import pytest

from sentinel.app.contracts import CommunityContractAddresses
from sentinel.module_types import ModuleType

# Load local environment variables before tests run.
env_file = Path(".env")
if env_file.exists():
    load_dotenv(env_file, override=False)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: mark tests that require a locally forked Anvil node",
    )


@pytest.fixture
def fake_contract_addresses():
    def _make(module_address: str = "0x0000000000000000000000000000000000000001"):
        return CommunityContractAddresses(
            module=module_address,
            accounting="0x0000000000000000000000000000000000000002",
            parameters_registry="0x0000000000000000000000000000000000000003",
            fee_distributor="0x0000000000000000000000000000000000000004",
            exit_penalties="0x0000000000000000000000000000000000000005",
            lido_locator="0x0000000000000000000000000000000000000006",
            staking_router="0x0000000000000000000000000000000000000007",
            vebo="0x0000000000000000000000000000000000000008",
            staking_module_id=3,
            module_type=ModuleType.COMMUNITY,
            csm_version=2,
        )

    return _make


@pytest.fixture(autouse=True)
def clear_alru_caches():
    """Reset class-level alru_cache wrappers between tests.

    async_lru >= 2.2 enforces single-loop usage per cache instance. Since
    pytest-asyncio creates a new event loop per test, reset the internal loop
    binding alongside cached entries.
    """
    from sentinel.modules.community.events import CommunityEventMessages
    from sentinel.modules.curated.events import CuratedEventMessages

    def _reset(instance_method):
        instance_method.cache_clear()
        inner = instance_method._LRUCacheWrapperInstanceMethod__wrapper
        inner._LRUCacheWrapper__first_loop = None

    cached_methods = (
        CommunityEventMessages._fetch_distribution_log,
        CuratedEventMessages._fetch_node_operator_metadata,
    )
    for cached_method in cached_methods:
        _reset(cached_method)
    yield
    for cached_method in cached_methods:
        _reset(cached_method)
