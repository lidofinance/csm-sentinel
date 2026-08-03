import asyncio
import logging

from sentinel.app.bootstrap import create_runtime, run
from sentinel.app.logging import configure_logging
from sentinel.handlers import register_handlers
from sentinel.modules.community.events import (
    assert_event_mappings as assert_community_event_mappings,
)
from sentinel.modules.curated.events import assert_event_mappings as assert_curated_event_mappings

logger = logging.getLogger(__name__)


def _assert_event_mappings() -> None:
    assert_community_event_mappings()
    assert_curated_event_mappings()


async def main() -> None:
    configure_logging()
    _assert_event_mappings()
    runtime = await create_runtime()
    register_handlers(runtime)
    logger.info("Starting CSM bot")
    await run(runtime)


if __name__ == "__main__":
    asyncio.run(main())
