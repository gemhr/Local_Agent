from tests._runtime_rc_manifest import assert_real_test_mappings


def test_checkpoint_and_validation_only_recovery_have_real_tests() -> None:
    assert_real_test_mappings("RC-14", "RC-15", "RC-16")

