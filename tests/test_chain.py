import asyncio
from types import SimpleNamespace

import pytest

from sentinel.chain import SharedChainConnection


class _Provider:
    def __init__(self) -> None:
        self.connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.connect_error: Exception | None = None

    async def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            error = self.connect_error
            self.connect_error = None
            raise error
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False


def _chain() -> tuple[SharedChainConnection, _Provider]:
    provider = _Provider()
    return SharedChainConnection(SimpleNamespace(provider=provider)), provider


@pytest.mark.asyncio
async def test_chain_access_is_reentrant_for_the_owning_task():
    chain, provider = _chain()

    async with chain:
        assert provider.connected
        async with chain:
            assert provider.connected
        assert provider.disconnect_calls == 0

    assert provider.connect_calls == 1
    assert provider.disconnect_calls == 0

    await chain.close()
    assert provider.disconnect_calls == 1


@pytest.mark.asyncio
async def test_chain_access_serializes_different_tasks():
    chain, provider = _chain()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_reader() -> None:
        async with chain:
            first_entered.set()
            await release_first.wait()

    async def second_reader() -> None:
        await first_entered.wait()
        async with chain:
            second_entered.set()

    first_task = asyncio.create_task(first_reader())
    second_task = asyncio.create_task(second_reader())
    await first_entered.wait()
    await asyncio.sleep(0)

    assert not second_entered.is_set()

    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert second_entered.is_set()
    assert provider.connect_calls == 1
    assert provider.disconnect_calls == 0

    await chain.close()
    assert provider.disconnect_calls == 1


@pytest.mark.asyncio
async def test_chain_access_is_released_when_connect_fails():
    chain, provider = _chain()
    provider.connect_error = RuntimeError("connect failed")

    with pytest.raises(RuntimeError, match="connect failed"):
        async with chain:
            raise AssertionError("unreachable")

    async with asyncio.timeout(1):
        async with chain:
            assert provider.connected

    assert provider.connect_calls == 2
    assert provider.disconnect_calls == 0

    await chain.close()
    assert provider.disconnect_calls == 1


@pytest.mark.asyncio
async def test_chain_access_is_released_when_reader_fails():
    chain, provider = _chain()

    with pytest.raises(RuntimeError, match="read failed"):
        async with chain:
            raise RuntimeError("read failed")

    async with asyncio.timeout(1):
        async with chain:
            assert provider.connected

    assert provider.connect_calls == 1
    assert provider.disconnect_calls == 0

    await chain.close()
    assert provider.disconnect_calls == 1


@pytest.mark.asyncio
async def test_chain_close_waits_for_active_reader_and_rejects_new_access():
    chain, provider = _chain()
    reader_entered = asyncio.Event()
    release_reader = asyncio.Event()

    async def reader() -> None:
        async with chain:
            reader_entered.set()
            await release_reader.wait()

    reader_task = asyncio.create_task(reader())
    await reader_entered.wait()
    close_task = asyncio.create_task(chain.close())
    await asyncio.sleep(0)

    assert not close_task.done()
    assert provider.disconnect_calls == 0

    release_reader.set()
    await reader_task
    await close_task

    assert provider.disconnect_calls == 1
    with pytest.raises(RuntimeError, match="closed"):
        async with chain:
            raise AssertionError("unreachable")
