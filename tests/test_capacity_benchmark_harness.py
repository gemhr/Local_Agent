from types import SimpleNamespace

from scripts.capacity.run_stage9_runtime_capacity import (
    ACTIVE_RUN_ENV,
    _configure_active_run_slots,
    _prometheus_gauge,
    _run_start_file_name,
)


def test_prometheus_gauge_reads_server_db_pool_state() -> None:
    body = (
        '# HELP localagent_postgresql_pool_connections SQLAlchemy pool snapshot.\n'
        'localagent_postgresql_pool_connections{state="checked_out"} 7.0\n'
    )

    assert (
        _prometheus_gauge(
            body,
            "localagent_postgresql_pool_connections",
            state="checked_out",
        )
        == 7.0
    )


def test_screening_output_name_is_slot_specific() -> None:
    args = SimpleNamespace(
        run_start_only=True,
        active_run_slots=8,
        concurrency=[25],
        samples=50,
        confirmation_run=None,
    )

    assert _run_start_file_name(args, 25) == "active_run_slots_8_screening.json"


def test_confirmation_output_name_includes_concurrency_and_run() -> None:
    args = SimpleNamespace(
        run_start_only=True,
        active_run_slots=12,
        concurrency=[1, 5, 10, 25],
        samples=100,
        confirmation_run=2,
    )

    assert (
        _run_start_file_name(args, 25)
        == "active_run_slots_12_confirmation_c25_run2.json"
    )


def test_active_run_slots_override_uses_canonical_settings_environment() -> None:
    environment = {}

    assert _configure_active_run_slots(environment, 8) == 8
    assert environment == {ACTIVE_RUN_ENV: "8"}


def test_active_run_slots_without_override_reads_settings_default() -> None:
    environment = {}
    settings = SimpleNamespace(max_active_runs=12)

    assert _configure_active_run_slots(environment, None, settings) == 12
    assert ACTIVE_RUN_ENV not in environment
