# -*- coding: utf-8 -*-
import json
import unittest

from core.browser_traffic import PlaywrightTrafficTracker, SeleniumTrafficTracker


class _Emitter:
    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.listeners.get(event, []).remove(callback)

    def emit(self, event, *args):
        for callback in list(self.listeners.get(event, [])):
            callback(*args)


class _Request:
    method = "POST"
    url = "https://example.test/register"
    headers = {"content-type": "application/json"}
    post_data = "{}"

    def sizes(self):
        return {
            "requestBodySize": 2,
            "requestHeadersSize": 10,
            "responseBodySize": 20,
            "responseHeadersSize": 8,
        }


class _WebSocket(_Emitter):
    pass


def _performance_event(method, params):
    return {
        "message": json.dumps({"message": {"method": method, "params": params}}),
    }


class _SeleniumDriver:
    def __init__(self, entries):
        self.entries = list(entries)
        self.cdp_commands = []

    def execute_cdp_cmd(self, command, params):
        self.cdp_commands.append((command, params))
        return {}

    def get_log(self, name):
        entries, self.entries = self.entries, []
        return entries

    def execute_script(self, script):
        return {}


class BrowserTrafficTests(unittest.TestCase):
    def test_playwright_counts_http_and_websocket_without_payload_capture(self):
        context = _Emitter()
        page = _Emitter()
        context.pages = [page]
        tracker = PlaywrightTrafficTracker(context)
        request = _Request()
        context.emit("request", request)
        context.emit("requestfinished", request)

        websocket = _WebSocket()
        page.emit("websocket", websocket)
        websocket.emit("framesent", "abc")
        websocket.emit("framereceived", b"1234")

        result = tracker.stop()
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["completed_request_count"], 1)
        self.assertEqual(result["http_upload_bytes"], 12)
        self.assertEqual(result["http_download_bytes"], 28)
        self.assertEqual(result["websocket_upload_bytes"], 3)
        self.assertEqual(result["websocket_download_bytes"], 4)
        self.assertEqual(result["total_bytes"], 47)

    def test_selenium_parses_network_events_and_websocket_frames(self):
        entries = [
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "1",
                    "request": {
                        "method": "POST",
                        "url": "https://example.test/register",
                        "headers": {"content-type": "application/json"},
                        "postData": "{}",
                    },
                },
            ),
            _performance_event(
                "Network.responseReceived",
                {
                    "requestId": "1",
                    "response": {"status": 200, "statusText": "OK", "headers": {"content-type": "application/json"}},
                },
            ),
            _performance_event(
                "Network.loadingFinished",
                {"requestId": "1", "encodedDataLength": 20},
            ),
            _performance_event(
                "Network.webSocketCreated",
                {"requestId": "ws-1"},
            ),
            _performance_event(
                "Network.webSocketFrameSent",
                {"requestId": "ws-1", "response": {"payloadData": "xy"}},
            ),
            _performance_event(
                "Network.webSocketFrameReceived",
                {"requestId": "ws-1", "response": {"payloadData": "z"}},
            ),
        ]
        driver = _SeleniumDriver(entries)
        tracker = SeleniumTrafficTracker(driver)
        result = tracker.stop()

        self.assertEqual(driver.cdp_commands[0][0], "Network.enable")
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["completed_request_count"], 1)
        self.assertEqual(result["websocket_count"], 1)
        self.assertEqual(result["websocket_upload_bytes"], 2)
        self.assertEqual(result["websocket_download_bytes"], 1)
        self.assertGreater(result["http_upload_bytes"], 0)
        self.assertGreaterEqual(result["http_download_bytes"], 20)


if __name__ == "__main__":
    unittest.main()
