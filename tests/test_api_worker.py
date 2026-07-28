from __future__ import annotations

from types import SimpleNamespace

import requests

import main


class _InterruptedResponse:
    def raise_for_status(self) -> None:
        return None

    def iter_content(self, *, chunk_size: int, decode_unicode: bool):
        assert chunk_size == 128
        assert decode_unicode is True
        raise requests.ConnectionError("response closed during cancellation")
        yield  # pragma: no cover


class _CompletedResponse:
    def raise_for_status(self) -> None:
        return None

    def iter_content(self, *, chunk_size: int, decode_unicode: bool):
        assert chunk_size == 128
        assert decode_unicode is True
        yield "complete"


class _FakeSession:
    def __init__(self, response: _InterruptedResponse | _CompletedResponse) -> None:
        self._response = response

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def post(self, url: str, *, json: dict, stream: bool, timeout: int):
        assert stream is True
        assert timeout == 300
        return self._response

    def close(self) -> None:
        return None


def test_interrupted_worker_emits_settled_without_success_or_error(monkeypatch) -> None:
    worker = main.ApiWorker("http://test/api/chat")
    response = _InterruptedResponse()
    monkeypatch.setattr(main.requests, "Session", lambda: _FakeSession(response))
    monkeypatch.setattr(worker, "isInterruptionRequested", lambda: True)
    emitted: list[str] = []
    worker.finished_signal.connect(lambda: emitted.append("finished"))
    worker.error_signal.connect(lambda _message: emitted.append("error"))
    worker.settled_signal.connect(lambda: emitted.append("settled"))

    worker.set_task(
        "core_router",
        "hello",
        run_id="49796282cdb643c7b8850942f7b66bd1",
    )
    worker.run()

    assert emitted == ["settled"]
    assert worker._response is None
    assert worker._session is None


def test_completed_worker_emits_success_before_settled(monkeypatch) -> None:
    worker = main.ApiWorker("http://test/api/chat")
    monkeypatch.setattr(
        main.requests,
        "Session",
        lambda: _FakeSession(_CompletedResponse()),
    )
    emitted: list[str] = []
    worker.finished_signal.connect(lambda: emitted.append("finished"))
    worker.error_signal.connect(lambda _message: emitted.append("error"))
    worker.settled_signal.connect(lambda: emitted.append("settled"))

    worker.set_task("core_router", "hello")
    worker.run()

    assert emitted == ["finished", "settled"]


def test_failed_worker_emits_error_before_settled(monkeypatch) -> None:
    worker = main.ApiWorker("http://test/api/chat")
    monkeypatch.setattr(
        main.requests,
        "Session",
        lambda: _FakeSession(_InterruptedResponse()),
    )
    emitted: list[str] = []
    worker.finished_signal.connect(lambda: emitted.append("finished"))
    worker.error_signal.connect(lambda _message: emitted.append("error"))
    worker.settled_signal.connect(lambda: emitted.append("settled"))

    worker.set_task("core_router", "hello")
    worker.run()

    assert emitted == ["error", "settled"]


def test_worker_settled_resets_shared_streaming_state_and_run_id() -> None:
    streaming_states: list[bool] = []
    controller = SimpleNamespace(
        chat_panel=SimpleNamespace(set_streaming=streaming_states.append),
        worker=SimpleNamespace(run_id="49796282cdb643c7b8850942f7b66bd1"),
    )

    main.MainController._on_worker_settled(controller)

    assert streaming_states == [False]
    assert controller.worker.run_id == ""
