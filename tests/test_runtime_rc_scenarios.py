from tests._runtime_rc_manifest import RC_SCENARIOS, assert_real_test_mappings


def test_rc_01_through_08_map_to_real_entry_or_core_chain_tests() -> None:
    assert_real_test_mappings(*(f"RC-{index:02d}" for index in range(1, 9)))


def test_rc_manifest_has_fixed_unique_complete_ids() -> None:
    ids = tuple(item.scenario_id for item in RC_SCENARIOS)
    assert ids == tuple(f"RC-{index:02d}" for index in range(1, 21))
    assert len({item.test_id for item in RC_SCENARIOS}) == 20

