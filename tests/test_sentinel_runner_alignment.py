# -*- coding: utf-8 -*-
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core import openai_auth, sentinel_runner


class SentinelRunnerAlignmentTests(unittest.TestCase):
    def test_password_flow_uses_auth_wrapper_sdk_and_real_screen_shape(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"p":"proof","c":"challenge","id":"device-id","flow":"username_password_create"}\n',
            stderr="",
        )
        profile = {
            "screen_width": 1680,
            "screen_height": 1050,
            "screen_avail_width": 1680,
            "screen_avail_height": 1025,
            "viewport_width": 1680,
            "viewport_height": 938,
            "color_depth": 24,
            "hardware_concurrency": 4,
            "webgl_vendor": "Google Inc. (Apple)",
            "webgl_renderer": "ANGLE (Apple, Apple M2, OpenGL 4.1)",
        }
        with patch.object(sentinel_runner.subprocess, "run", return_value=completed) as run:
            sentinel_runner.generate_sentinel_token(
                challenge={"token": "challenge", "_request_p": "request-proof"},
                flow="username_password_create",
                device_id="device-id",
                browser_profile=profile,
            )

        cmd = run.call_args.args[0]
        values = {cmd[i]: cmd[i + 1] for i in range(0, len(cmd) - 1) if cmd[i].startswith("--")}
        self.assertEqual(
            values["--script-src"],
            "https://sentinel.openai.com/backend-api/sentinel/sdk.js",
        )
        self.assertEqual(values["--avail-width"], "1680")
        self.assertEqual(values["--avail-height"], "1025")
        self.assertEqual(values["--outer-width"], "1680")
        self.assertEqual(values["--outer-height"], "1025")
        self.assertEqual(values["--inner-width"], "1680")
        self.assertEqual(values["--inner-height"], "938")
        self.assertEqual(values["--color-depth"], "24")
        self.assertEqual(values["--cores"], "4")
        self.assertEqual(values["--challenge-proof"], "request-proof")
        self.assertEqual(values["--webgl-vendor"], "Google Inc. (Apple)")
        self.assertEqual(values["--webgl-renderer"], "ANGLE (Apple, Apple M2, OpenGL 4.1)")

    def test_default_auth_flows_use_matching_pages_without_chatgpt_build(self):
        pages = {
            "password_verify": "https://auth.openai.com/log-in/password",
            "username_password_create": "https://auth.openai.com/create-account/password",
            "email_otp_validate": "https://auth.openai.com/email-verification",
            "authorize_continue": "https://auth.openai.com/email-verification",
            "oauth_create_account": "https://auth.openai.com/about-you",
        }
        for flow, page in pages.items():
            with self.subTest(flow=flow):
                completed = subprocess.CompletedProcess(
                    args=[], returncode=0,
                    stdout=json.dumps({"p": "proof", "c": "challenge", "id": "device-id", "flow": flow}),
                    stderr="",
                )
                with patch.object(sentinel_runner.subprocess, "run", return_value=completed) as run:
                    sentinel_runner.generate_sentinel_token(
                        challenge={"token": "challenge"},
                        flow=flow,
                        device_id="device-id",
                        browser_profile={"build_id": "chatgpt-build"},
                    )
                cmd = run.call_args.args[0]
                self.assertEqual(cmd[cmd.index("--page-url") + 1], page)
                self.assertEqual(cmd[cmd.index("--build-id") + 1], "")

    def test_password_verify_header_keeps_login_session_context(self):
        session = SimpleNamespace(
            device_id="login-device",
            browser_profile={"user_agent": "login-user-agent", "build_id": "chatgpt-build"},
            sentinel_sid="login-sid",
            sentinel_iframe_sid="registration-iframe-sid",
            react_listening_key="login-listening",
            react_container_key="login-container",
            react_resources_key="login-resources",
            auth_cookie_header=Mock(return_value="oai-did=login-device; session=offline-cookie"),
        )
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"p": "proof", "c": "challenge", "id": "login-device", "flow": "password_verify"}),
            stderr="",
        )
        with patch.object(sentinel_runner.subprocess, "run", return_value=completed) as run:
            header, so_header = openai_auth.build_sentinel_header(
                session,
                {"token": "challenge", "_request_p": "login-request-proof"},
                "password_verify",
            )

        cmd = run.call_args.args[0]
        expected = {
            "--flow": "password_verify",
            "--device-id": "login-device",
            "--sentinel-sid": "login-sid",
            "--challenge-proof": "login-request-proof",
            "--page-url": "https://auth.openai.com/log-in/password",
            "--build-id": "",
            "--script-src": "https://sentinel.openai.com/backend-api/sentinel/sdk.js",
            "--user-agent": "login-user-agent",
            "--react-listening-key": "login-listening",
            "--react-container-key": "login-container",
            "--react-resources-key": "login-resources",
            "--cookie": "oai-did=login-device; session=offline-cookie",
        }
        for option, value in expected.items():
            with self.subTest(option=option):
                self.assertEqual(cmd[cmd.index(option) + 1], value)
        session.auth_cookie_header.assert_called_once_with()
        self.assertEqual(json.loads(header)["flow"], "password_verify")
        self.assertIsNone(so_header)

    def test_explicit_chatgpt_page_keeps_profile_build(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"p":"proof","c":"challenge","id":"device-id","flow":"password_verify"}',
            stderr="",
        )
        with patch.object(sentinel_runner.subprocess, "run", return_value=completed) as run:
            sentinel_runner.generate_sentinel_token(
                challenge={"token": "challenge"},
                flow="password_verify",
                device_id="device-id",
                page_url="https://chatgpt.com/",
                browser_profile={"build_id": "chatgpt-build"},
            )
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--page-url") + 1], "https://chatgpt.com/")
        self.assertEqual(cmd[cmd.index("--build-id") + 1], "chatgpt-build")

    def test_otp_flow_uses_versioned_sdk(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"p":"proof","c":"challenge","id":"device-id","flow":"email_otp_validate"}\n',
            stderr="",
        )
        with patch.object(sentinel_runner.subprocess, "run", return_value=completed) as run:
            sentinel_runner.generate_sentinel_token(
                challenge={"token": "challenge"},
                flow="email_otp_validate",
                device_id="device-id",
            )
        cmd = run.call_args.args[0]
        index = cmd.index("--script-src")
        self.assertEqual(
            cmd[index + 1],
            "https://sentinel.openai.com/sentinel/20260810913b/sdk.js",
        )


if __name__ == "__main__":
    unittest.main()
