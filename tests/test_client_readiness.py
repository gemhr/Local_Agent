"""WP1-C Client Readiness Worker / bounded probe 测试。

覆盖：
- immediate ready / READY_DEGRADED / becomes ready / never ready / 404 /
  timeout / malformed JSON / invalid body；
- deadline 有界（monkeypatch monotonic + fake session + fake sleep）；
- request timeout <= remaining；sleep <= remaining；
- interruption 结束 worker；Session close；client_trust_env 接线；
- UI 非阻塞（MainController 启动 QThread 而非同步 requests.get）；
- history gating（success 才触发 initial history fetch；failure 不触发）。
"""

from __future__ import annotations

import inspect

import pytest

import core.client_readiness as readiness_module
from core.client_readiness import (
    PER_REQUEST_TIMEOUT_SECONDS,
    TOTAL_READINESS_DEADLINE_SECONDS,
    ReadinessWorker,
    run_bounded_readiness_probe,
)


class _FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeSession:
    """记录每次 GET 的 timeout，并按脚本返回响应。"""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, float]] = []
        self.closed = False
        self.trust_env: bool | None = None

    def get(self, url: str, timeout: float):
        self.calls.append((url, timeout))
        if not self.responses:
            raise readiness_module.requests.ConnectionError("offline")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


def _ready_body(status: str = "READY") -> dict:
    """构造合法 ready combination：READY→degraded=False；READY_DEGRADED→degraded=True。"""
    return {
        "status": status,
        "lifecycle": "READY",
        "admission": "ACCEPTING",
        "degraded": status == "READY_DEGRADED",
    }


def _run_with_fake_clock(
    session,
    *,
    interruption_check=None,
) -> bool:
    """用 fake monotonic + fake sleep 执行 bounded probe。

    §44：不得让测试真实等待 30s deadline；断言语义与真实时钟完全一致，
    只是时间被 fake 时钟快速推进。
    """
    now = [1000.0]

    def fake_monotonic() -> float:
        return now[0]

    def fake_sleep(seconds: float) -> None:
        now[0] += seconds

    return run_bounded_readiness_probe(
        session,
        "http://x/readyz",
        interruption_check=interruption_check,
        monotonic=fake_monotonic,
        sleep=fake_sleep,
    )


# ---------------------------------------------------------------------------
# Probe outcome matrix
# ---------------------------------------------------------------------------


def test_immediate_ready_succeeds_once() -> None:
    session = _FakeSession([_FakeResponse(200, _ready_body())])
    assert run_bounded_readiness_probe(session, "http://x/readyz") is True
    assert len(session.calls) == 1


def test_ready_degraded_is_success() -> None:
    session = _FakeSession([_FakeResponse(200, _ready_body("READY_DEGRADED"))])
    assert run_bounded_readiness_probe(session, "http://x/readyz") is True


def test_becomes_ready_after_retries() -> None:
    session = _FakeSession(
        [
            _FakeResponse(503, {}),
            _FakeResponse(503, {}),
            _FakeResponse(200, _ready_body()),
        ]
    )
    assert _run_with_fake_clock(session) is True
    assert len(session.calls) == 3


def test_404_is_retryable_unavailable() -> None:
    session = _FakeSession([_FakeResponse(404, {})])
    assert _run_with_fake_clock(session) is False


def test_timeout_is_retryable() -> None:
    session = _FakeSession([readiness_module.requests.Timeout("slow")])
    assert _run_with_fake_clock(session) is False


def test_malformed_json_is_retryable() -> None:
    session = _FakeSession([_FakeResponse(200, ValueError("bad json"))])
    assert _run_with_fake_clock(session) is False


def test_invalid_body_is_retryable() -> None:
    session = _FakeSession([_FakeResponse(200, {"status": "READY"})])
    assert _run_with_fake_clock(session) is False


def test_200_non_ready_status_is_retryable() -> None:
    session = _FakeSession([_FakeResponse(200, _ready_body("DRAINING"))])
    assert _run_with_fake_clock(session) is False


# ---------------------------------------------------------------------------
# Typed ready body validator（WP1-C typed readiness contract）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {
            "status": "READY",
            "lifecycle": "READY",
            "admission": "ACCEPTING",
            "degraded": False,
        },
        {
            "status": "READY_DEGRADED",
            "lifecycle": "READY",
            "admission": "ACCEPTING",
            "degraded": True,
        },
    ],
)
def test_validator_accepts_ready_combinations(body: dict) -> None:
    assert readiness_module._is_valid_ready_body(body) is True


@pytest.mark.parametrize(
    "body",
    [
        # missing field
        {"lifecycle": "READY", "admission": "ACCEPTING", "degraded": False},
        {"status": "READY", "admission": "ACCEPTING", "degraded": False},
        {"status": "READY", "lifecycle": "READY", "degraded": False},
        {"status": "READY", "lifecycle": "READY", "admission": "ACCEPTING"},
        # extra field
        {
            "status": "READY",
            "lifecycle": "READY",
            "admission": "ACCEPTING",
            "degraded": False,
            "unexpected_field": 1,
        },
        # wrong type（bool 是 int 子类；"false"/0/None 都必须拒绝）
        {"status": None, "lifecycle": "READY", "admission": "ACCEPTING", "degraded": False},
        {"status": "READY", "lifecycle": 1, "admission": "ACCEPTING", "degraded": False},
        {"status": "READY", "lifecycle": "READY", "admission": [], "degraded": False},
        {"status": "READY", "lifecycle": "READY", "admission": "ACCEPTING", "degraded": "false"},
        {"status": "READY", "lifecycle": "READY", "admission": "ACCEPTING", "degraded": 0},
        {"status": "READY", "lifecycle": "READY", "admission": "ACCEPTING", "degraded": None},
        # unknown enum/string
        {"status": "BOGUS", "lifecycle": "READY", "admission": "ACCEPTING", "degraded": False},
        {"status": "READY", "lifecycle": "BOGUS", "admission": "ACCEPTING", "degraded": False},
        {"status": "READY", "lifecycle": "READY", "admission": "BOGUS", "degraded": False},
        {"status": "READY", "lifecycle": "READY", "admission": "ACCEPTING", "degraded": "UNKNOWN"},
        {"status": "", "lifecycle": "READY", "admission": "ACCEPTING", "degraded": False},
        # inconsistent combinations（含 Codex Final Gate 复现的两个 P1 输入）
        {"status": "READY", "lifecycle": "BOGUS", "admission": "BOGUS", "degraded": False},
        {"status": "READY", "lifecycle": "CLOSED", "admission": "ACCEPTING", "degraded": False},
        {"status": "READY", "lifecycle": "READY", "admission": "DRAINING", "degraded": False},
        {"status": "READY", "lifecycle": "READY", "admission": "ACCEPTING", "degraded": True},
        {"status": "READY_DEGRADED", "lifecycle": "CLOSED", "admission": "CLOSED", "degraded": False},
        {"status": "READY_DEGRADED", "lifecycle": "CLOSED", "admission": "ACCEPTING", "degraded": True},
        {"status": "READY_DEGRADED", "lifecycle": "READY", "admission": "CLOSED", "degraded": True},
        {"status": "READY_DEGRADED", "lifecycle": "SHUTTING_DOWN", "admission": "ACCEPTING", "degraded": True},
        {"status": "READY_DEGRADED", "lifecycle": "READY", "admission": "ACCEPTING", "degraded": False},
        # legal but non-ready status（即使四字段合法也必须拒绝）
        {"status": "STARTING", "lifecycle": "STARTING", "admission": "UNAVAILABLE", "degraded": False},
        {"status": "DRAINING", "lifecycle": "READY", "admission": "DRAINING", "degraded": False},
        {"status": "DRAINING", "lifecycle": "SHUTTING_DOWN", "admission": "DRAINING", "degraded": False},
        {"status": "CLOSED", "lifecycle": "CLOSED", "admission": "CLOSED", "degraded": False},
        {"status": "UNAVAILABLE", "lifecycle": "UNAVAILABLE", "admission": "UNAVAILABLE", "degraded": False},
    ],
)
def test_validator_rejects_invalid_bodies(body: dict) -> None:
    assert readiness_module._is_valid_ready_body(body) is False


def test_validator_rejects_non_dict() -> None:
    assert readiness_module._is_valid_ready_body("not-a-dict") is False
    assert readiness_module._is_valid_ready_body(None) is False
    assert readiness_module._is_valid_ready_body([1, 2, 3]) is False


# ---------------------------------------------------------------------------
# Probe integration：invalid typed body → bounded retry（不只测 validator 函数）
# ---------------------------------------------------------------------------


def test_malformed_then_valid_ready_probe_succeeds_exactly_once() -> None:
    """200 malformed/inconsistent → 200 valid READY → terminal success 恰一次。"""
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                {
                    "status": "READY",
                    "lifecycle": "BOGUS",
                    "admission": "BOGUS",
                    "degraded": False,
                },
            ),
            _FakeResponse(200, _ready_body()),
        ]
    )
    assert _run_with_fake_clock(session) is True
    assert len(session.calls) == 2


def test_repeated_malformed_probe_terminates_unavailable() -> None:
    """200 malformed/inconsistent 反复出现 → deadline → terminal unavailable。"""
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                {
                    "status": "READY_DEGRADED",
                    "lifecycle": "CLOSED",
                    "admission": "CLOSED",
                    "degraded": False,
                },
            )
        ]
    )
    assert _run_with_fake_clock(session) is False


# ---------------------------------------------------------------------------
# Deadline boundedness
# ---------------------------------------------------------------------------


def test_deadline_is_bounded_and_request_timeout_never_exceeds_remaining() -> None:
    """注入 monotonic 使 deadline 立即耗尽；证明 request timeout 有界。"""
    now = [1000.0]

    def fake_monotonic() -> float:
        return now[0]

    def advance(seconds: float) -> None:
        now[0] += seconds

    session = _FakeSession([_FakeResponse(503, {})])
    result = run_bounded_readiness_probe(
        session,
        "http://x/readyz",
        monotonic=fake_monotonic,
        sleep=advance,
    )
    assert result is False
    # 第一次 request timeout 应为 min(1.0, 30.0) = 1.0
    assert session.calls[0][1] == min(PER_REQUEST_TIMEOUT_SECONDS, TOTAL_READINESS_DEADLINE_SECONDS)


def test_sleep_never_exceeds_remaining_deadline() -> None:
    """deadline 剩余不足时，request timeout 与 sleep 都必须截断到 remaining。"""
    now = [1000.0]
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        return now[0]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    session = _FakeSession(
        [_FakeResponse(503, {}), _FakeResponse(503, {})]
    )

    # 第一次 request 后把时钟推进到只剩 0.7s（> RETRY_INTERVAL 0.5）：
    # - 第一次 sleep = min(0.5, 0.7) = 0.5 → 剩余 0.2s；
    # - 第二次 request timeout = min(1.0, 0.2) = 0.2（截断）；
    # - 第二次 sleep = min(0.5, 0.2) = 0.2（截断，不跨过 deadline）。
    original_get = session.get
    call_count = [0]

    def wrapped_get(url, timeout):
        call_count[0] += 1
        if call_count[0] == 1:
            now[0] += TOTAL_READINESS_DEADLINE_SECONDS - 0.7
        return original_get(url, timeout)

    session.get = wrapped_get

    result = run_bounded_readiness_probe(
        session,
        "http://x/readyz",
        monotonic=fake_monotonic,
        sleep=fake_sleep,
    )
    assert result is False
    # 第一次 request timeout = min(1.0, 30.0) = 1.0
    assert session.calls[0][1] == PER_REQUEST_TIMEOUT_SECONDS
    # 第二次 request 时剩余 0.2s → request timeout 被截断到 0.2
    # （浮点累积，使用 approx 比较）
    assert session.calls[1][1] == pytest.approx(0.2)
    # sleep 不允许跨过 deadline：0.5（未截断）后紧跟 0.2（截断）
    assert sleeps == [0.5, pytest.approx(0.2)]


def test_interruption_ends_probe() -> None:
    session = _FakeSession([_FakeResponse(503, {})])
    interrupted = [False]

    def check() -> bool:
        return interrupted[0]

    # 第一次 request 后设置 interruption。
    original_get = session.get
    call_count = [0]

    def wrapped_get(url, timeout):
        call_count[0] += 1
        if call_count[0] == 1:
            interrupted[0] = True
        return original_get(url, timeout)

    session.get = wrapped_get

    result = run_bounded_readiness_probe(
        session,
        "http://x/readyz",
        interruption_check=check,
        sleep=lambda _: None,
    )
    assert result is False
    assert len(session.calls) == 1


# ---------------------------------------------------------------------------
# ReadinessWorker QThread wrapper
# ---------------------------------------------------------------------------


def test_worker_creates_session_with_trust_env_and_closes(monkeypatch) -> None:
    captured: list[_FakeSession] = []

    class FakeSession:
        def __init__(self) -> None:
            self.trust_env: bool | None = None
            self.closed = False
            captured.append(self)

        def get(self, url, timeout):
            return _FakeResponse(200, _ready_body())

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(readiness_module.requests, "Session", FakeSession)

    worker = ReadinessWorker("http://127.0.0.1:8000", client_trust_env=False)
    # 直接调用 run() 同步执行（不启动真实线程，避免 flaky）。
    worker.run()

    assert captured, "worker 必须创建 Session"
    assert captured[0].trust_env is False
    assert captured[0].closed is True


def test_worker_trust_env_true(monkeypatch) -> None:
    captured: list[_FakeSession] = []

    class FakeSession:
        def __init__(self) -> None:
            self.trust_env: bool | None = None
            self.closed = False
            captured.append(self)

        def get(self, url, timeout):
            return _FakeResponse(200, _ready_body())

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(readiness_module.requests, "Session", FakeSession)

    worker = ReadinessWorker("http://127.0.0.1:8000", client_trust_env=True)
    worker.run()

    assert captured[0].trust_env is True
    assert captured[0].closed is True


# ---------------------------------------------------------------------------
# UI non-blocking contract
# ---------------------------------------------------------------------------


def test_main_controller_starts_qthread_not_sync_requests(monkeypatch) -> None:
    """结构/行为证据：MainController 通过 _start_readiness_probe 启动 QThread，
    不在 __init__ 中同步 requests.get。"""
    import main as main_module

    # __init__ 不得包含同步 requests.get 调用。
    init_source = inspect.getsource(main_module.MainController.__init__)
    assert "requests.get" not in init_source
    assert "requests.Session().get" not in init_source
    # 必须调用 _start_readiness_probe。
    assert "_start_readiness_probe" in init_source
    # _start_readiness_probe 必须启动 QThread。
    probe_source = inspect.getsource(
        main_module.MainController._start_readiness_probe
    )
    assert "ReadinessWorker(" in probe_source
    assert ".start()" in probe_source


# ---------------------------------------------------------------------------
# History gating
# ---------------------------------------------------------------------------


def test_readiness_success_triggers_history_once() -> None:
    """readiness 成功：_on_readiness_ready 每次 signal 恰好触发一次 history fetch。"""
    import types

    import main as main_module

    # MainController 是 PyQt6 QObject 子类，无法绕过 __init__ 实例化；
    # 用 types.MethodType 把真实方法体绑定到轻量 double 上执行（行为证据）。
    # “整个 startup 只 fetch 一次”的 once 语义由 worker 单次 emit 保证，
    # 见 test_worker_emits_ready_signal_exactly_once。
    controller = types.SimpleNamespace()
    controller.chat_panel = type("P", (), {"current_agent_id": "core_router"})()
    calls: list[str] = []
    controller._fetch_and_load_history = lambda agent_id: calls.append(agent_id)

    bound_ready = types.MethodType(
        main_module.MainController._on_readiness_ready, controller
    )
    bound_ready()
    assert calls == ["core_router"]


def test_worker_emits_ready_signal_exactly_once(monkeypatch) -> None:
    """Worker terminal 时 ready_signal 恰好发出一次、unavailable 不发出。

    与 _on_readiness_ready 的 handler 一起构成 §27/§48 的
    “readiness 成功后 initial history fetch 只启动一次”。
    """
    import core.client_readiness as readiness_module

    class FakeSession:
        def __init__(self) -> None:
            self.trust_env: bool | None = None

        def get(self, url, timeout):
            return _FakeResponse(200, _ready_body())

        def close(self) -> None:
            pass

    monkeypatch.setattr(readiness_module.requests, "Session", FakeSession)

    worker = ReadinessWorker("http://127.0.0.1:8000", client_trust_env=False)
    ready_emits: list[str] = []
    unavailable_emits: list[str] = []
    worker.ready_signal.connect(lambda: ready_emits.append("ready"))
    worker.unavailable_signal.connect(lambda: unavailable_emits.append("unavail"))
    worker.run()

    assert ready_emits == ["ready"]
    assert unavailable_emits == []


def test_worker_unavailable_signal_exactly_once(monkeypatch) -> None:
    """probe 返回 False（如反复 malformed body）→ worker terminal 只发一次
    unavailable_signal、不误发 ready，Session 关闭。"""
    import core.client_readiness as readiness_module

    class FakeSession:
        def __init__(self) -> None:
            self.trust_env: bool | None = None
            self.closed = False

        def get(self, url, timeout):
            raise AssertionError("probe 已替换，不应真实发起请求")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(readiness_module.requests, "Session", FakeSession)
    monkeypatch.setattr(
        readiness_module,
        "run_bounded_readiness_probe",
        lambda *args, **kwargs: False,
    )

    worker = ReadinessWorker("http://127.0.0.1:8000", client_trust_env=False)
    ready_emits: list[str] = []
    unavailable_emits: list[str] = []
    worker.ready_signal.connect(lambda: ready_emits.append("ready"))
    worker.unavailable_signal.connect(lambda: unavailable_emits.append("unavail"))
    worker.run()

    assert ready_emits == []
    assert unavailable_emits == ["unavail"]


def test_readiness_failure_does_not_trigger_history() -> None:
    """readiness 失败：_on_readiness_unavailable 不触发 initial history fetch，
    只追加一次固定 safe 系统消息。"""
    import types

    import main as main_module

    controller = types.SimpleNamespace()
    messages: list[str] = []

    class _Panel:
        current_agent_id = "core_router"

        def append_system_msg(self, msg: str, target_agent_id=None) -> None:
            messages.append(msg)

    controller.chat_panel = _Panel()
    calls: list[str] = []
    controller._fetch_and_load_history = lambda agent_id: calls.append(agent_id)

    bound_unavailable = types.MethodType(
        main_module.MainController._on_readiness_unavailable, controller
    )
    bound_unavailable()
    assert calls == []
    assert messages == ["Server unavailable; retry later."]