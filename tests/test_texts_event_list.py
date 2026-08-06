from sentinel.modules.community.texts import build_event_list_text
from sentinel.modules.curated.texts import build_event_list_text as build_curated_event_list_text


def test_build_event_list_text_for_common_group():
    catalog_events = {"DistributionLogUpdated"}
    text = build_event_list_text(catalog_events)
    expected = (
        "Here is the list of events you will receive notifications for:\n"
        "A 🚨 means urgent action is required from you\n"
        "\n"
        "*Common CSM Events for all the Node Operators:*\n"
        "\\- 📈 New rewards distributed\n"
        "\n"
        "\n"
    )
    assert text == expected


def test_build_event_list_text_for_key_management_group():
    catalog_events = {"DepositedSigningKeysCountChanged"}
    text = build_event_list_text(catalog_events)
    expected = (
        "Here is the list of events you will receive notifications for:\n"
        "A 🚨 means urgent action is required from you\n"
        "\n"
        "*Key Management Events:*\n"
        "Changes related to keys and their status\\.\n"
        "\\- 🤩 Node Operator's keys received deposits\n"
        "\n"
        "\n"
    )
    assert text == expected


def test_build_event_list_text_uses_changed_event_titles():
    catalog_events = {
        "VettedSigningKeysCountDecreased",
        "KeyAllocatedBalanceChanged",
        "FeeSplitsSet",
    }

    community_text = build_event_list_text(catalog_events)
    curated_text = build_curated_event_list_text(catalog_events)

    for text in (community_text, curated_text):
        assert "\\- 🚨 Invalid or duplicated keys has been uploaded" in text
        assert "\\- 👀 Key balance increased" in text
        assert "\\- ℹ️ Fee splits changed" in text
