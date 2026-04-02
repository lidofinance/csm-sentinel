import pytest


def test_find_staking_module_id_success():
    from sentinel.app.contracts import _find_staking_module_id

    modules = [
        (1, "0x1234567890123456789012345678901234567890"),
        (2, "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"),
    ]

    assert (
        _find_staking_module_id(modules, "0xABCDefAbcdefABCDefABCDEFabcdefABCDefAbcd")
        == 2
    )


def test_find_staking_module_id_failure():
    from sentinel.app.contracts import _find_staking_module_id

    modules = [
        (1, "0x1234567890123456789012345678901234567890"),
        (2, "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"),
    ]

    with pytest.raises(RuntimeError):
        _find_staking_module_id(modules, "0x0000000000000000000000000000000000000000")
