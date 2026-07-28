import asyncio
from types import TracebackType

from web3 import AsyncWeb3


class SharedChainConnection:
    """Give one task at a time reentrant access to the shared reads connection."""

    def __init__(self, w3: AsyncWeb3) -> None:
        self.w3 = w3
        self._lease = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0
        self._closed = False

    async def __aenter__(self) -> AsyncWeb3:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Chain access requires an asyncio task")

        if self._owner is task:
            self._depth += 1
            return self.w3

        if self._closed:
            raise RuntimeError("Chain access is closed")
        await self._lease.acquire()
        if self._closed:
            self._lease.release()
            raise RuntimeError("Chain access is closed")
        self._owner = task
        self._depth = 1
        try:
            if not await self.w3.provider.is_connected():
                await self.w3.provider.connect()
        except BaseException:
            self._owner = None
            self._depth = 0
            self._lease.release()
            raise
        return self.w3

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        task = asyncio.current_task()
        if task is None or self._owner is not task or self._depth == 0:
            raise RuntimeError("Chain access released by a task that does not own it")

        self._depth -= 1
        if self._depth > 0:
            return

        self._owner = None
        self._lease.release()

    async def close(self) -> None:
        """Wait for the active reader and close the shared reads connection."""

        task = asyncio.current_task()
        if task is not None and self._owner is task:
            raise RuntimeError("Chain access cannot be closed by its active reader")

        self._closed = True
        await self._lease.acquire()
        try:
            if await self.w3.provider.is_connected():
                await self.w3.provider.disconnect()
        finally:
            self._lease.release()
