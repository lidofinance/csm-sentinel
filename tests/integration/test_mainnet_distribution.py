import asyncio
from collections.abc import Callable
from contextlib import suppress
import os

import pytest

from sentinel.config import clear_config, get_config
from sentinel.models import Block

from .helpers import build_subscription, replay_transaction_on_anvil

MAINNET_COMMUNITY_MODULE = "0xDa5F930cE326EB5205085D66c72A4E79d60cB8C1"
ZERO_DISTRIBUTION_BLOCK = 25_825_535
ZERO_DISTRIBUTION_TX = "0x221f732daec3f324739593f2ace38573fa16303786d0108005349583f583da16"
REPORT_EVENTS = {"ModuleFeeDistributed", "DistributionLogUpdated"}

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(autouse=True, scope="session")
def mainnet_community_config_env():
    provider_url = os.getenv("WEB3_SOCKET_PROVIDERS") or os.getenv("WEB3_SOCKET_PROVIDER")
    if not provider_url:
        pytest.skip("WEB3_SOCKET_PROVIDERS or WEB3_SOCKET_PROVIDER is required")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("WEB3_SOCKET_PROVIDERS", provider_url)
        monkeypatch.setenv("MODULE_ADDRESS", MAINNET_COMMUNITY_MODULE)
        monkeypatch.setenv("ETHERSCAN_URL", "https://etherscan.io")
        monkeypatch.setenv("BEACONCHAIN_URL", "https://beaconcha.in")
        monkeypatch.setenv("MODULE_UI_URL", "https://csm.lido.fi")
        clear_config()
        yield
    clear_config()


@pytest.fixture(params=[False, True], ids=["block_replay", "live_transaction_replay"])
def via_subscription(request) -> bool:
    return request.param


async def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout: float = 10.0,
    interval: float = 0.1,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("Timed out waiting for distribution report")
        await asyncio.sleep(interval)


async def test_zero_distribution_report_does_not_emit_rewards_notification(
    anvil_launcher,
    via_subscription,
):
    launch_block = ZERO_DISTRIBUTION_BLOCK - 1 if via_subscription else ZERO_DISTRIBUTION_BLOCK
    anvil = await anvil_launcher(launch_block)
    harness = await build_subscription(anvil.ws_url, anvil.http_url)
    subscription_task: asyncio.Task | None = None
    try:
        if via_subscription:
            subscription_task = asyncio.create_task(harness.subscribe())
            await harness.wait_until_subscribed()
            receipt = await replay_transaction_on_anvil(
                fork_provider_url=get_config().web3_socket_providers[0],
                anvil_http_url=anvil.http_url,
                tx_hash=ZERO_DISTRIBUTION_TX,
            )
            assert receipt["status"] == 1
            await _wait_for(
                lambda: any(
                    window.event_names <= {event.event for event in window.events}
                    for window in harness.runtime.storage().aggregation_windows.pending()
                )
            )
            await harness.runtime.handle_block(Block(number=ZERO_DISTRIBUTION_BLOCK))
            await _wait_for(lambda: len(harness.processed_events) == 2)
        else:
            await harness.replay_blocks(ZERO_DISTRIBUTION_BLOCK, ZERO_DISTRIBUTION_BLOCK)

        report = [
            (event, plan)
            for event, plan in harness.processed_events
            if event.event in REPORT_EVENTS
        ]
        assert [event.event for event, _ in report] == [
            "ModuleFeeDistributed",
            "DistributionLogUpdated",
        ]
        assert report[0][0].args["shares"] == 0
        assert report[0][0].tx == report[1][0].tx
        assert all(plan is None for _, plan in report)
    finally:
        if subscription_task is not None:
            await harness.stop()
            subscription_task.cancel()
            with suppress(asyncio.CancelledError):
                await subscription_task
        await harness.disconnect()
