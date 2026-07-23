import threading

from core.cancellation_client import request_run_cancellation


def test_short_cancel_request_runs_while_stream_worker_is_blocked():
    """取消 POST 使用独立执行路径，不等待模拟的流读取解除阻塞。"""
    stream_blocked = threading.Event()
    stream_release = threading.Event()
    cancel_called = threading.Event()

    def blocked_stream_worker():
        stream_blocked.set()
        stream_release.wait()

    def fake_post(url, timeout):
        assert url.endswith("/cancel")
        assert timeout == 2
        cancel_called.set()

    stream_thread = threading.Thread(target=blocked_stream_worker)
    stream_thread.start()
    assert stream_blocked.wait(1)
    cancel_thread = threading.Thread(target=request_run_cancellation, args=(fake_post, "http://test/cancel"))
    cancel_thread.start()
    assert cancel_called.wait(1)
    stream_release.set()
    cancel_thread.join(); stream_thread.join()
