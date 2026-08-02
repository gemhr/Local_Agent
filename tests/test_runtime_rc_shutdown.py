from tests._runtime_rc_manifest import assert_real_test_mappings


def test_clean_degraded_and_legacy_shutdown_boundaries_have_real_tests() -> None:
    assert_real_test_mappings("RC-17", "RC-18", "RC-19")

