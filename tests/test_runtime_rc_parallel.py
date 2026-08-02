from tests._runtime_rc_manifest import assert_real_test_mappings


def test_parallel_best_effort_and_fail_fast_have_real_core_chain_tests() -> None:
    assert_real_test_mappings("RC-09", "RC-10")

