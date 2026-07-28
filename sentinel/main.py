import asyncio
import logging

from sentinel.app.bootstrap import create_runtime, run
from sentinel.handlers import register_handlers
from sentinel.modules.community.events import (
    assert_event_mappings as assert_community_event_mappings,
)
from sentinel.modules.curated.events import assert_event_mappings as assert_curated_event_mappings

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _assert_event_mappings() -> None:
    assert_community_event_mappings()
    assert_curated_event_mappings()


async def main() -> None:
    _assert_event_mappings()
    runtime = await create_runtime()
    register_handlers(runtime)
    logger.info("Starting CSM bot")
    await run(runtime)


if __name__ == "__main__":
    asyncio.run(main())
