# -*- coding: utf-8 -*-
"""浏览器注册流程的网络流量统计。

统计的是浏览器侧能观察到的 HTTP 请求/响应字节，不会读取或保存请求内容。
Playwright 直接使用 ``Request.sizes()``；Selenium/Roxy 优先读取 Chrome
performance log，无法读取时退回到 Resource Timing。两种方式都不包含
TLS/IP/代理隧道等协议额外开销，因此不等同于代理服务商的计费流量。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from typing import Any

logger = logging.getLogger(__name__)


def _non_negative_int(value: Any) -> int:
    """把 Playwright/CDP 返回的数字安全地归一化为非负整数。"""
    try:
        value = int(float(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, value)


def _optional_size(value: Any) -> int | None:
    """读取可能使用 -1 表示 unknown 的尺寸字段。"""
    if value is None:
        return None
    try:
        value = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value >= 0 else None


def _payload_size(payload: Any) -> int:
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return len(payload)
    return len(str(payload or "").encode("utf-8", errors="replace"))


def _value_size(value: Any) -> int:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    return len(str(value or "").encode("utf-8", errors="replace"))


def _size_field(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class _TrafficAccumulator:
    """只保存计数，不保存 URL、Header 或请求体。"""

    def __init__(self, *, label: str, method: str):
        self.label = label
        self.method = method
        self.started_at = _iso_now()
        self.started_perf = time.perf_counter()
        self._lock = threading.RLock()
        self._stopped = False
        self._final_snapshot: dict[str, Any] | None = None

        self.http_upload_bytes = 0
        self.http_download_bytes = 0
        self.websocket_upload_bytes = 0
        self.websocket_download_bytes = 0
        self.request_count = 0
        self.completed_request_count = 0
        self.failed_request_count = 0
        self.unfinished_request_count = 0
        self.unknown_size_request_count = 0
        self.websocket_count = 0
        self._reported = False

    def _add_http(self, upload: int = 0, download: int = 0) -> None:
        with self._lock:
            self.http_upload_bytes += _non_negative_int(upload)
            self.http_download_bytes += _non_negative_int(download)

    def _build_snapshot(self) -> dict[str, Any]:
        with self._lock:
            total_upload = self.http_upload_bytes + self.websocket_upload_bytes
            total_download = self.http_download_bytes + self.websocket_download_bytes
            total = total_upload + total_download
            note = (
                "浏览器侧 HTTP 请求/响应字节（含可获取的请求/响应头和主体）"
                "；另计 WebSocket 帧 payload；不含 TLS/IP/代理隧道开销"
            )
            if self.method == "selenium.resource_timing_fallback":
                note = (
                    "Chrome Resource Timing 可见的响应传输字节；上传请求头/请求体不可见，"
                    "另计可见的 WebSocket 帧 payload；不含 TLS/IP/代理隧道开销"
                )
            result = {
                "available": True,
                "method": self.method,
                "label": self.label,
                "started_at": self.started_at,
                "finished_at": _iso_now(),
                "duration_seconds": round(max(0.0, time.perf_counter() - self.started_perf), 3),
                "upload_bytes": int(total_upload),
                "download_bytes": int(total_download),
                "total_bytes": int(total),
                "http_upload_bytes": int(self.http_upload_bytes),
                "http_download_bytes": int(self.http_download_bytes),
                "websocket_upload_bytes": int(self.websocket_upload_bytes),
                "websocket_download_bytes": int(self.websocket_download_bytes),
                "request_count": int(self.request_count),
                "completed_request_count": int(self.completed_request_count),
                "failed_request_count": int(self.failed_request_count),
                "unfinished_request_count": int(self.unfinished_request_count),
                "unknown_size_request_count": int(self.unknown_size_request_count),
                "websocket_count": int(self.websocket_count),
                "note": note,
            }
            return result

    def _finish_snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._final_snapshot is None:
                self._final_snapshot = self._build_snapshot()
            return dict(self._final_snapshot)

    def _log_snapshot(self, snapshot: dict[str, Any]) -> None:
        if self._reported:
            return
        self._reported = True
        logger.info(
            "[%s] 注册浏览器网络流量：上传 %.2f KiB，下载 %.2f KiB，合计 %.2f KiB，"
            "HTTP请求 %s（失败 %s，未完成 %s）",
            self.label,
            snapshot.get("upload_bytes", 0) / 1024,
            snapshot.get("download_bytes", 0) / 1024,
            snapshot.get("total_bytes", 0) / 1024,
            snapshot.get("request_count", 0),
            snapshot.get("failed_request_count", 0),
            snapshot.get("unfinished_request_count", 0),
        )


class PlaywrightTrafficTracker(_TrafficAccumulator):
    """统计一个 BrowserContext 内所有 Page 的 HTTP/WebSocket 流量。"""

    def __init__(self, context: Any, *, label: str = "BrowserUse"):
        super().__init__(label=label, method="playwright.request.sizes")
        self.context = context
        self._requests: dict[int, Any] = {}
        self._accounted_requests: set[int] = set()
        self._websockets: dict[int, Any] = {}
        self._listeners: list[tuple[Any, str, Any]] = []
        self._attach()

    def _listen(self, emitter: Any, event: str, callback: Any) -> None:
        try:
            emitter.on(event, callback)
            self._listeners.append((emitter, event, callback))
        except Exception as exc:
            logger.debug("[%s] 注册流量监听失败 event=%s：%s", self.label, event, exc)

    def _attach(self) -> None:
        # 监听 BrowserContext，而不是只监听当前 page，确保 popup/重定向窗口也计入。
        self._listen(self.context, "request", self._on_request)
        self._listen(self.context, "requestfinished", self._on_request_finished)
        self._listen(self.context, "requestfailed", self._on_request_failed)
        # websocket 是 Page 事件，不是 BrowserContext 事件；对已有页面和后续 popup
        # 都挂载监听。
        self._listen(self.context, "page", self._on_page)
        for page in list(getattr(self.context, "pages", []) or []):
            self._on_page(page)

    def _on_page(self, page: Any) -> None:
        self._listen(page, "websocket", self._on_websocket)

    def _on_request(self, request: Any) -> None:
        if self._stopped:
            return
        key = id(request)
        with self._lock:
            if key not in self._requests:
                self._requests[key] = request
                self.request_count += 1

    @staticmethod
    def _request_fallback_upload(request: Any) -> int:
        """失败请求没有 sizes() 时，尽量估算已经发出的头/体。"""
        try:
            method = str(getattr(request, "method", "GET") or "GET")
        except Exception:
            method = "GET"
        try:
            url = str(getattr(request, "url", "") or "")
        except Exception:
            url = ""
        header_bytes = len(f"{method} {url} HTTP/1.1\r\n".encode("utf-8"))
        try:
            headers = request.headers or {}
            header_bytes += sum(
                _value_size(name) + _value_size(value) + 4
                for name, value in headers.items()
            ) + 2
        except Exception:
            pass
        try:
            body = request.post_data
        except Exception:
            body = None
        return header_bytes + _payload_size(body) if body else header_bytes

    def _mark_request(self, request: Any, *, failed: bool) -> None:
        key = id(request)
        with self._lock:
            if key in self._accounted_requests:
                return
            self._accounted_requests.add(key)
            if key not in self._requests:
                self.request_count += 1
            self._requests[key] = request

        try:
            sizes = request.sizes()
        except Exception:
            # requestfailed 没有 response，sizes() 通常不可用。
            upload = self._request_fallback_upload(request)
            with self._lock:
                self.http_upload_bytes += upload
                self.unknown_size_request_count += 1
                if failed:
                    self.failed_request_count += 1
                else:
                    self.completed_request_count += 1
            return

        values = {
            name: _optional_size(_size_field(sizes, name))
            for name in (
                "requestBodySize",
                "requestHeadersSize",
                "responseBodySize",
                "responseHeadersSize",
            )
        }
        unknown = any(value is None for value in values.values())
        upload = (values["requestBodySize"] or 0) + (values["requestHeadersSize"] or 0)
        download = (values["responseBodySize"] or 0) + (values["responseHeadersSize"] or 0)
        self._add_http(upload, download)
        with self._lock:
            if unknown:
                self.unknown_size_request_count += 1
            if failed:
                self.failed_request_count += 1
            else:
                self.completed_request_count += 1

    def _on_request_finished(self, request: Any) -> None:
        self._mark_request(request, failed=False)

    def _on_request_failed(self, request: Any) -> None:
        self._mark_request(request, failed=True)

    def _on_websocket(self, websocket: Any) -> None:
        if self._stopped:
            return
        key = id(websocket)
        with self._lock:
            if key in self._websockets:
                return
            self._websockets[key] = websocket
            self.websocket_count += 1
        self._listen(websocket, "framesent", self._on_websocket_sent)
        self._listen(websocket, "framereceived", self._on_websocket_received)

    def _on_websocket_sent(self, payload: Any) -> None:
        with self._lock:
            self.websocket_upload_bytes += _payload_size(payload)

    def _on_websocket_received(self, payload: Any) -> None:
        with self._lock:
            self.websocket_download_bytes += _payload_size(payload)

    def _remove_listeners(self) -> None:
        for emitter, event, callback in self._listeners:
            try:
                emitter.remove_listener(event, callback)
            except Exception:
                pass
        self._listeners.clear()

    def stop(self) -> dict[str, Any]:
        if self._stopped:
            snapshot = self._finish_snapshot()
            self._log_snapshot(snapshot)
            return snapshot
        # 同步 Playwright 的事件会在这里调用前完成；仍未完成的请求单独标记，
        # 不伪造响应字节，避免把“估计值”误报成精确值。
        with self._lock:
            self._stopped = True
            self.unfinished_request_count = max(
                0, len(self._requests) - len(self._accounted_requests)
            )
        self._remove_listeners()
        snapshot = self._finish_snapshot()
        self._log_snapshot(snapshot)
        return snapshot


class SeleniumTrafficTracker(_TrafficAccumulator):
    """统计 Roxy/Selenium 的 Chrome performance log 流量。

    Selenium 没有统一的网络事件订阅 API，ChromeDriver 会把 CDP Network
    事件放进 performance log；本类在 checkpoint/stop 时批量取出并解析。
    """

    def __init__(self, driver: Any, *, label: str = "Roxy"):
        super().__init__(label=label, method="selenium.chrome.performance_log")
        self.driver = driver
        self._log_supported: bool | None = None
        self._network_event_count = 0
        self._requests: dict[str, dict[str, Any]] = {}
        self._fallback_seen: set[tuple[Any, ...]] = set()
        try:
            # 对已经连接到指纹浏览器的 debuggerAddress 也尽量开启 Network 域；
            # 是否能读到 performance log 仍由对应的 ChromeDriver 决定。
            self.driver.execute_cdp_cmd("Network.enable", {})
        except Exception as exc:
            logger.debug("[%s] Network.enable 失败，将尝试读取现有 performance log：%s", label, exc)

    @staticmethod
    def _header_size(headers: Any, *, start_line: str = "") -> int:
        size = _value_size(start_line) if start_line else 0
        if isinstance(headers, dict):
            size += sum(_value_size(k) + _value_size(v) + 4 for k, v in headers.items())
        return size + 2

    @classmethod
    def _request_upload_size(cls, request: dict[str, Any]) -> int:
        method = str(request.get("method") or "GET")
        url = str(request.get("url") or "")
        try:
            parsed = urlsplit(url)
            target = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        except Exception:
            target = url
        headers = request.get("headers") or {}
        header_size = cls._header_size(headers, start_line=f"{method} {target} HTTP/1.1\r\n")
        body = request.get("postData")
        if body is None and request.get("postDataEntries"):
            body = "".join(str(item.get("bytes") or "") for item in request["postDataEntries"] if isinstance(item, dict))
        return header_size + _payload_size(body) if body else header_size

    @classmethod
    def _response_header_size(cls, response: dict[str, Any] | None) -> int:
        if not response:
            return 0
        status = response.get("status")
        status_text = str(response.get("statusText") or "")
        start_line = f"HTTP/1.1 {status or 0} {status_text}\r\n"
        return cls._header_size(response.get("headers") or {}, start_line=start_line)

    def _finish_cdp_request(self, record: dict[str, Any], *, body_size: Any = 0, failed: bool = False) -> None:
        if record.get("finished"):
            return
        record["finished"] = True
        if not record.get("served_from_cache"):
            if not record.get("upload_added"):
                self._add_http(record.get("upload_size", 0), 0)
                record["upload_added"] = True
            body_size = max(_non_negative_int(body_size), _non_negative_int(record.get("received_body_bytes")))
            download = self._response_header_size(record.get("response")) + body_size
            self._add_http(0, download)
        with self._lock:
            if failed:
                self.failed_request_count += 1
            else:
                self.completed_request_count += 1

    def _handle_cdp_event(self, method: str, params: dict[str, Any]) -> None:
        self._network_event_count += 1
        if method == "Network.requestWillBeSent":
            request_id = str(params.get("requestId") or "")
            if not request_id:
                return
            previous = self._requests.get(request_id)
            redirect = params.get("redirectResponse")
            if previous is not None and not previous.get("finished"):
                # 同一个 requestId 的重定向会重新触发 requestWillBeSent。
                redirect_body = (redirect or {}).get("encodedDataLength", 0) if isinstance(redirect, dict) else 0
                if isinstance(redirect, dict):
                    previous["response"] = redirect
                self._finish_cdp_request(previous, body_size=redirect_body)
            request = params.get("request") or {}
            record = {
                "request": request,
                "response": None,
                "finished": False,
                "served_from_cache": False,
                "upload_size": self._request_upload_size(request),
                "upload_added": False,
                "received_body_bytes": 0,
            }
            self._requests[request_id] = record
            with self._lock:
                self.request_count += 1
            return

        if method == "Network.responseReceived":
            request_id = str(params.get("requestId") or "")
            record = self._requests.get(request_id)
            if record is not None:
                response = params.get("response") or {}
                record["response"] = response
                if response.get("fromDiskCache") or response.get("fromMemoryCache"):
                    record["served_from_cache"] = True
            return

        if method == "Network.webSocketHandshakeResponseReceived":
            request_id = str(params.get("requestId") or "")
            record = self._requests.get(request_id)
            if record is not None:
                record["response"] = params.get("response") or {}
                self._finish_cdp_request(record)
            return

        if method == "Network.requestServedFromCache":
            request_id = str(params.get("requestId") or "")
            record = self._requests.get(request_id)
            if record is not None:
                record["served_from_cache"] = True
            return

        if method == "Network.loadingFinished":
            request_id = str(params.get("requestId") or "")
            record = self._requests.get(request_id)
            if record is not None:
                self._finish_cdp_request(record, body_size=params.get("encodedDataLength", 0))
            return

        if method == "Network.dataReceived":
            request_id = str(params.get("requestId") or "")
            record = self._requests.get(request_id)
            if record is not None and not record.get("served_from_cache"):
                record["received_body_bytes"] = (
                    _non_negative_int(record.get("received_body_bytes"))
                    + _non_negative_int(params.get("encodedDataLength", 0))
                )
            return

        if method == "Network.loadingFailed":
            request_id = str(params.get("requestId") or "")
            record = self._requests.get(request_id)
            if record is not None:
                self._finish_cdp_request(record, failed=True)
            return

        if method == "Network.webSocketCreated":
            with self._lock:
                self.websocket_count += 1
            return

        if method == "Network.webSocketFrameSent":
            frame = params.get("response") or {}
            with self._lock:
                self.websocket_upload_bytes += _payload_size(frame.get("payloadData"))
            return

        if method == "Network.webSocketFrameReceived":
            frame = params.get("response") or {}
            with self._lock:
                self.websocket_download_bytes += _payload_size(frame.get("payloadData"))

    def drain(self) -> int:
        """读取当前 performance log 缓冲区，返回解析的记录数。"""
        try:
            entries = self.driver.get_log("performance")
            self._log_supported = True
        except Exception as exc:
            if self._log_supported is not False:
                logger.debug("[%s] 无法读取 performance log：%s", self.label, exc)
            self._log_supported = False
            return 0

        parsed = 0
        for entry in entries or []:
            try:
                raw = entry.get("message") if isinstance(entry, dict) else entry
                outer = json.loads(raw) if isinstance(raw, str) else raw
                message = outer.get("message") if isinstance(outer, dict) else None
                if not isinstance(message, dict):
                    continue
                method = str(message.get("method") or "")
                if not method.startswith("Network."):
                    continue
                self._handle_cdp_event(method, message.get("params") or {})
                parsed += 1
            except Exception:
                continue
        return parsed

    def _collect_resource_timing_fallback(self) -> None:
        """performance log 不可用时统计当前文档已暴露的响应传输字节。"""
        if self._network_event_count:
            return
        script = """
        return (() => ({
          href: location.href,
          timeOrigin: performance.timeOrigin || 0,
          resources: performance.getEntriesByType('resource').map(e => ({
            name: e.name,
            startTime: e.startTime,
            duration: e.duration,
            transferSize: e.transferSize || 0,
            encodedBodySize: e.encodedBodySize || 0
          })),
          navigation: performance.getEntriesByType('navigation').map(e => ({
            name: e.name,
            startTime: e.startTime,
            duration: e.duration,
            transferSize: e.transferSize || 0,
            encodedBodySize: e.encodedBodySize || 0
          }))
        }))();
        """
        try:
            data = self.driver.execute_script(script)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        document = str(data.get("href") or "")
        time_origin = _non_negative_int(data.get("timeOrigin"))
        self.method = "selenium.resource_timing_fallback"
        entries = list(data.get("resources") or []) + list(data.get("navigation") or [])
        for item in entries:
            if not isinstance(item, dict):
                continue
            key = (
                document,
                time_origin,
                str(item.get("name") or ""),
                _non_negative_int(item.get("startTime")),
                _non_negative_int(item.get("duration")),
                _non_negative_int(item.get("transferSize")),
                _non_negative_int(item.get("encodedBodySize")),
            )
            if key in self._fallback_seen:
                continue
            self._fallback_seen.add(key)
            transfer = _non_negative_int(item.get("transferSize"))
            if not transfer:
                transfer = _non_negative_int(item.get("encodedBodySize"))
            if transfer:
                self._add_http(0, transfer)
            with self._lock:
                self.request_count += 1
                self.completed_request_count += 1

    def checkpoint(self) -> None:
        if not self._stopped:
            self.drain()
            self._collect_resource_timing_fallback()

    def stop(self) -> dict[str, Any]:
        if not self._stopped:
            self.drain()
            self._collect_resource_timing_fallback()
            with self._lock:
                self._stopped = True
                unfinished_records = [record for record in self._requests.values() if not record.get("finished")]
                # 停止前把已经从 CDP dataReceived 看到的部分响应计入；剩余未知部分不虚构。
                for record in unfinished_records:
                    if record.get("served_from_cache") or record.get("partial_accounted"):
                        continue
                    if not record.get("upload_added"):
                        self.http_upload_bytes += _non_negative_int(record.get("upload_size"))
                        record["upload_added"] = True
                    self.http_download_bytes += (
                        self._response_header_size(record.get("response"))
                        + _non_negative_int(record.get("received_body_bytes"))
                    )
                    record["partial_accounted"] = True
                    self.unknown_size_request_count += 1
                unfinished = len(unfinished_records)
                self.unfinished_request_count = unfinished
        snapshot = self._finish_snapshot()
        self._log_snapshot(snapshot)
        return snapshot
