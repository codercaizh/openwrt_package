from __future__ import annotations

import threading

from owrt_builder.notifications import NotificationSettings, PushPlusNotifier


class FakeTransport:
    def __init__(self, responses: list[tuple[int, dict[str, object] | None]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object], float]] = []
        self.done = threading.Event()

    def post(self, url: str, payload: dict[str, object], timeout: float) -> tuple[int, dict[str, object] | None]:
        self.calls.append((url, payload, timeout))
        response = self.responses.pop(0)
        if not self.responses:
            self.done.set()
        return response


def test_pushplus_retries_are_independent_of_build_result() -> None:
    transport = FakeTransport([(503, None), (200, {"code": 200})])
    notifier = PushPlusNotifier(
        NotificationSettings(token="secret-token", retries=2, retry_delay_seconds=0.01),
        transport=transport,
    )
    assert notifier.send_async({"id": "job-1", "device": "netcore_n60-pro"}, "succeeded")
    assert transport.done.wait(2)
    assert len(transport.calls) == 2
    assert all(call[1]["token"] == "secret-token" for call in transport.calls)
    assert all("secret-token" not in str(call[1]["content"]) for call in transport.calls)


def test_pushplus_reports_canceled_tasks() -> None:
    transport = FakeTransport([(200, {"code": 200})])
    notifier = PushPlusNotifier(
        NotificationSettings(token="secret-token", retries=1),
        transport=transport,
    )
    assert notifier.send_async({"id": "job-2", "device": "n60pro"}, "canceled", "用户取消")
    assert transport.done.wait(2)
    assert "已取消" in str(transport.calls[0][1]["title"])


def test_pushplus_reads_persisted_token_when_a_job_finishes() -> None:
    transport = FakeTransport([(200, {"code": 200}), (200, {"code": 200})])
    token = {"value": "first-token"}
    notifier = PushPlusNotifier(
        NotificationSettings(token=None, retries=1),
        transport=transport,
        token_provider=lambda: token["value"],
    )
    notifier._send({"id": "job-3", "device": "n60pro"}, "succeeded", None)
    token["value"] = "second-token"
    notifier._send({"id": "job-4", "device": "n60pro"}, "failed", "compile failed")
    assert transport.calls[0][1]["token"] == "first-token"
    assert transport.calls[1][1]["token"] == "second-token"
