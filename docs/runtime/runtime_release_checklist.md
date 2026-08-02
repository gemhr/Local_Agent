# Stage2 Runtime RC1 Release Checklist

Checklist 是有证据引用的 code-level gate，不替代生产容量、外部依赖、容灾、Soak 或渗透测试。

## Pre-release

- [ ] Contract tests：`tests/test_runtime_contract_freeze.py`、`test_runtime_scope_matrix.py`、`test_runtime_owner_matrix.py`、`test_runtime_schema_matrix.py`。
- [ ] 20 Required RC scenarios：`docs/runtime/runtime_rc_scenario_matrix.md` 与 `tests/_runtime_rc_manifest.py` 一致。
- [ ] Full suite：`uv run python -m pytest -q`。
- [ ] Resource invariants：`tests/test_runtime_resource_baseline.py`、`tests/test_runtime_invariants.py`；毫秒值不是硬 Gate。
- [ ] Security scan：`tests/test_runtime_security_boundary.py` 与既有 snapshot/event/trace security tests。
- [ ] Capability/limitations：`runtime_capability_matrix.md`、`runtime_release_gate.md::KNOWN_LIMITATION`。
- [ ] Configuration validation：`tests/test_runtime_configuration_reference.py`，所有配置名来自 `Settings.load()`。

## Startup

- [ ] Application services/lifespan：`tests/test_runtime_lifespan.py`、`tests/test_server_compatibility_handles.py`。
- [ ] Admission=`ACCEPTING`：`tests/test_application_runtime_services.py`。
- [ ] Default Runtime=`COORDINATED`：RC-01、`tests/test_default_runtime_entry.py`。
- [ ] Journal/Observability/Trace health：`tests/test_event_journal.py`、`test_observability_integration.py`、`test_trace_integration.py`。
- [ ] Offline safe smoke：RC-01；不访问生产外部服务。

## Runtime

- [ ] Registry/Budget/Worker owner：`tests/test_runtime_invariants.py`、RC-06/11/12/18。
- [ ] Event sequence/terminal：RC-01/09/10/20、`tests/test_event_journal.py`。
- [ ] Disconnect watcher/producer/channel：RC-13、`tests/test_stream_cancellation.py`。
- [ ] Sensitive output allowlists：`tests/test_runtime_security_boundary.py`、`tests/test_snapshot_security.py`、`tests/test_shutdown_report_truthfulness.py`。

## Shutdown

- [ ] Run/worker drain、flush、close order：RC-17、`tests/test_graceful_shutdown.py`。
- [ ] Model safety/deferred truth：RC-18、`tests/test_shutdown_report_truthfulness.py`。
- [ ] `fully_closed`、failure/deferred/unknown：`test_shutdown_top_level_semantics_distinguish_orchestration_and_closure`。
- [ ] Reentry：`tests/test_shutdown_cancellation_reentry.py`；不要对 UNKNOWN 自动 double close。

## Rollback

- [ ] 请求前将真实 `CHAT_RUNTIME_MODE` 设为 `LEGACY` 并重启：RC-19。
- [ ] 先确认 Legacy capability boundary：`runtime_architecture_v1.md::Legacy / Coordinated Boundary`。
- [ ] 禁止对已失败/已开始 Run 跨 Runtime 重跑：RC-20、`tests/test_runtime_legacy_boundary.py`。
- [ ] 回滚前后均执行新身份的安全 smoke，Application resource identity close once。

## CI Artifact Boundary

可以离线导出 `ReleaseGateAssessment` 的安全摘要，但本轮未连接真实 CI。唯一允许字段：`rc_identifier`、`p0_count`、`p1_count`、`p2_ids`、`known_limitation_ids`、`required_scenarios`、`passed_scenarios`、`contract_tests_passed`、`full_suite_passed`、`resource_invariants_passed`、`security_scan_passed`、`status`。禁止路径、原始异常、业务正文、Rule ID、Provider 配置与测试秘密。Artifact 是 Derived Report，不是 Runtime 控制面，不能启用 Fault。

