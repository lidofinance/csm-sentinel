import logging
from collections.abc import MutableMapping
from typing import Any

from sentinel.app.storage import AggregationWindowStore, DigestStore
from sentinel.services.digest import DigestGroups

logger = logging.getLogger(__name__)

LEGACY_DEPOSIT_AGGREGATION_GROUP = "deposited_signing_key_counts"
LEGACY_DIGEST_EVENTS_KEY = "digest_events"


def migrate_legacy_storage(bot_data: MutableMapping[str, Any]) -> None:
    events = [
        *DigestStore.parse_events(bot_data.pop(LEGACY_DIGEST_EVENTS_KEY, None)),
        *AggregationWindowStore.pop_group_events(
            bot_data,
            LEGACY_DEPOSIT_AGGREGATION_GROUP,
        ),
    ]
    if not events:
        return

    DigestStore(bot_data).extend(DigestGroups.DEPOSITED_SIGNING_KEYS, events)
    logger.info(
        "Migrated legacy deposit aggregation to digest storage",
        extra={"source_event_count": len(events)},
    )
