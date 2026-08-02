from tests._runtime_rc_manifest import assert_real_test_mappings


def test_budget_deadline_disconnect_and_no_runtime_fallback_have_real_tests() -> None:
    assert_real_test_mappings("RC-11", "RC-12", "RC-13", "RC-20")

