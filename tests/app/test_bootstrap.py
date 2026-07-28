import asyncio
from types import SimpleNamespace

import pytest

from sentinel.app.bootstrap import _resolve_backfill_start_block, _wait_for_subscription_start


@pytest.mark.parametrize(
    ("configured_block", "persisted_block", "expected"),
    [
        (None, None, 0),
        (None, 0, 0),
        (None, "0x0", 0),
        (None, 42, 43),
        (100, None, 100),
        (0, 42, 0),
    ],
)
def test_resolve_backfill_start_block(
    configured_block: int | None,
    persisted_block: object | None,
    expected: int,
):
    assert _resolve_backfill_start_block(configured_block, persisted_block) == expected


@pytest.mark.asyncio
async def test_subscription_start_propagates_early_supervisor_failure():
    never_subscribed = asyncio.Event()
    supervisor = SimpleNamespace(wait_until_subscribed=never_subscribed.wait)

    async def fail_supervisor():
        await asyncio.sleep(0)
        raise RuntimeError("subscription setup failed")

    supervisor_task = asyncio.create_task(fail_supervisor())

    with pytest.raises(RuntimeError, match="subscription setup failed"):
        await _wait_for_subscription_start(supervisor, supervisor_task)
