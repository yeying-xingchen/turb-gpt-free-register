# -*- coding: utf-8 -*-
import base64
import unittest

from core.generic_api_mail_client import _decode_data_uri, _fetch_yangyang_otp, _parse_yangyang_code_url


class FakeResponse:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self):
        return self._data


class FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "/api/messages/" in url:
            return FakeResponse(data={
                "items": [
                    {"id": 1, "subject": "旧码", "received_at": "2026-08-01 10:00:00"},
                    {"id": 2, "subject": "Your OpenAI code is 654321", "received_at": "2026-08-01 10:01:00"},
                ],
                "has_more": False,
            })
        if "/message/2/" in url:
            html = "<html><body>Your verification code is <b>654321</b></body></html>"
            body = "data:text/html;charset=utf-8;base64," + base64.b64encode(html.encode()).decode()
            return FakeResponse(data={"subject": "Your OpenAI code is 654321", "body": body, "receivedAt": "2026-08-01 10:01:00"})
        if "/message/1/" in url:
            return FakeResponse(data={"subject": "旧码", "body": "code 111111", "receivedAt": "2026-08-01 10:00:00"})
        return FakeResponse(status_code=404, text="not found")


class GenericApiYangyangTests(unittest.TestCase):
    def test_parse_yangyang_url(self):
        self.assertEqual(
            _parse_yangyang_code_url("http://yangyang.website/messages/tok/a@icloud.com"),
            ("http://yangyang.website", "tok", "a@icloud.com"),
        )

    def test_decode_data_uri_base64(self):
        body = "data:text/html;base64," + base64.b64encode("验证码 123456".encode()).decode()
        self.assertIn("123456", _decode_data_uri(body))

    def test_fetch_yangyang_otp_uses_api_and_detail(self):
        code = _fetch_yangyang_otp(
            FakeSession(),
            "http://yangyang.website/messages/tok/a@icloud.com",
            {"User-Agent": "test"},
        )
        self.assertEqual(code, "654321")


if __name__ == "__main__":
    unittest.main()
