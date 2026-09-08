"""Optional PushPlus notifications with bounded, independent retries.

PushPlus accepts JSON ``POST https://www.pushplus.plus/send`` payloads with a
``token``, ``title`` and ``content``.  The token is read only from the process
environment and is never persisted or written to a build log.  Tests can pass
an in-memory transport rather than making an external request.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol


class Transport(Protocol):
    def post(self, url: str, payload: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any] | None]: ...


class UrlLibTransport:
    def post(self, url: str, payload: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any] | None]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(response.status)
                raw = response.read(64 * 1024)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read(64 * 1024)
        except (urllib.error.URLError, TimeoutError, OSError):
            return 599, None
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            parsed = None
        return status, parsed if isinstance(parsed, dict) else None


@dataclass(frozen=True)
class NotificationSettings:
    token: str | None = None
    endpoint: str = "https://www.pushplus.plus/send"
    title_prefix: str = "OpenWrt 构建"
    retries: int = 3
    timeout_seconds: float = 8.0
    retry_delay_seconds: float = 2.0

    @classmethod
    def from_env(cls) -> "NotificationSettings":
        return cls(
            token=os.getenv("PUSHPLUS_TOKEN") or None,
            endpoint=os.getenv("PUSHPLUS_ENDPOINT", "https://www.pushplus.plus/send"),
            title_prefix=os.getenv("PUSHPLUS_TITLE_PREFIX", "OpenWrt 构建"),
            retries=max(1, min(5, int(os.getenv("PUSHPLUS_RETRIES", "3")))),
            timeout_seconds=max(1.0, min(30.0, float(os.getenv("PUSHPLUS_TIMEOUT_SECONDS", "8")))),
            retry_delay_seconds=max(0.1, min(60.0, float(os.getenv("PUSHPLUS_RETRY_DELAY_SECONDS", "2")))),
        )


class PushPlusNotifier:
    """Fire-and-forget notifier; each job gets its own bounded retry thread."""

    def __init__(self, settings: NotificationSettings | None = None, transport: Transport | None = None, on_result: Callable[[str, bool, int], None] | None = None):
        self.settings = settings or NotificationSettings.from_env()
        self.transport = transport or UrlLibTransport()
        self.on_result = on_result

    @property
    def enabled(self) -> bool:
        return bool(self.settings.token)

    def send_async(self, job: dict[str, Any], status: str, error: str | None = None) -> bool:
        if not self.enabled:
            return False
        thread = threading.Thread(target=self._send, args=(job, status, error), name=f"pushplus-{job['id'][:8]}", daemon=True)
        thread.start()
        return True

    def _send(self, job: dict[str, Any], status: str, error: str | None) -> None:
        # Keep content useful without including command lines, environment or
        # user-supplied secrets.  The destination token never enters content.
        labels = {"succeeded": "成功", "failed": "失败", "canceled": "已取消", "interrupted": "中断"}
        content = f"任务：{job['id']}\n设备：{job['device']}\n状态：{labels.get(status, status)}"
        if error and status in {"failed", "interrupted"}:
            content += f"\n错误：{str(error)[:500]}"
        payload = {
            "token": self.settings.token,
            "title": f"{self.settings.title_prefix}：{labels.get(status, status)}",
            "content": content,
            "template": "txt",
        }
        for attempt in range(1, self.settings.retries + 1):
            http_status, body = self.transport.post(self.settings.endpoint, payload, self.settings.timeout_seconds)
            ok = http_status == 200 and isinstance(body, dict) and body.get("code") in (200, "200")
            if ok:
                if self.on_result:
                    self.on_result(job["id"], True, attempt)
                return
            if attempt < self.settings.retries:
                time.sleep(self.settings.retry_delay_seconds * attempt)
        if self.on_result:
            self.on_result(job["id"], False, self.settings.retries)
