# -*- coding: utf-8 -*-
import socket
import threading
import unittest
from unittest.mock import MagicMock, patch

from core.proxy_chain import ProxyChainRelay
from core.proxy_utils import (
    SUPPORTED_PROXY_SCHEMES,
    diagnose_proxy_endpoint,
    parse_proxy_url,
    validate_proxy_lines,
)


class ProxyUtilsCoreTests(unittest.TestCase):
    def test_all_advertised_schemes_and_bare_default(self):
        self.assertEqual(
            SUPPORTED_PROXY_SCHEMES,
            {"http", "https", "socks4", "socks4a", "socks5", "socks5h"},
        )
        for scheme in SUPPORTED_PROXY_SCHEMES:
            with self.subTest(scheme=scheme):
                spec = parse_proxy_url(f"{scheme}://user:pa%40ss@example.test:1234")
                self.assertEqual((spec.scheme, spec.host, spec.port), (scheme, "example.test", 1234))
                self.assertEqual((spec.username, spec.password), ("user", "pa@ss"))

        bare = parse_proxy_url("example.test:8080:user:pass", allow_bare=True)
        self.assertEqual(bare.raw, "http://user:pass@example.test:8080")
        legacy = parse_proxy_url("socks5://example.test:1080:user:pass")
        self.assertEqual((legacy.scheme, legacy.username, legacy.password), ("socks5", "user", "pass"))

    def test_authority_only_and_blank_validation(self):
        for value in ("http://proxy.example:8080/path", "http://proxy.example:8080?q=1", "http://proxy.example:8080#frag"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_proxy_url(value)
        validate_proxy_lines(["", "  ", "proxy.example:8080"])

    def test_http_diagnosis_does_not_send_socks_greeting(self):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        with patch("core.proxy_utils.socket.create_connection", return_value=sock) as create:
            result = diagnose_proxy_endpoint("http://proxy.example:8080")
        create.assert_called_once_with(("proxy.example", 8080), timeout=2.0)
        sock.sendall.assert_not_called()
        self.assertIn("HTTP", result)


class ProxyChainCoreTests(unittest.TestCase):
    def test_upstream_scheme_mapping_preserves_dns_semantics(self):
        class FakeRemote:
            def __init__(self):
                self.proxy_args = None
                self.timeout = None
                self.destination = None

            def set_proxy(self, *args, **kwargs):
                self.proxy_args = (args, kwargs)

            def settimeout(self, value):
                self.timeout = value

            def connect(self, destination):
                self.destination = destination

        for scheme, expected_type, expected_rdns in (
            ("http", "HTTP", True),
            ("socks4", "SOCKS4", False),
            ("socks4a", "SOCKS4", True),
            ("socks5", "SOCKS5", False),
            ("socks5h", "SOCKS5", True),
        ):
            with self.subTest(scheme=scheme), patch("core.proxy_chain.socks.socksocket", return_value=FakeRemote()) as factory:
                relay = ProxyChainRelay("http://target.example:8080", f"{scheme}://upstream.example:1080")
                remote = relay._connect_target_through_upstream("target.example", 8080)
                proxy_args, proxy_kwargs = remote.proxy_args
                self.assertEqual(proxy_args[0], getattr(__import__("socks"), expected_type))
                self.assertEqual(proxy_kwargs["rdns"], expected_rdns)
                self.assertEqual(remote.destination, ("target.example", 8080))
                factory.assert_called_once()

    def test_target_socks5_dns_mode_is_preserved(self):
        for scheme, expected_atyp in (("socks5", b"\x01"), ("socks5h", b"\x03")):
            with self.subTest(scheme=scheme):
                relay = ProxyChainRelay(f"{scheme}://target.example:1080", "http://upstream.example:8081")
                relay._resolve_local = lambda host, port: "127.0.0.1"
                local, server = socket.socketpair()
                received = []

                def socks_server():
                    received.append(server.recv(64))
                    server.sendall(b"\x05\x00")
                    received.append(server.recv(64))
                    server.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")

                thread = threading.Thread(target=socks_server, daemon=True)
                thread.start()
                try:
                    relay._target_socks_connect(local, "dns.example", 443)
                    thread.join(timeout=2)
                    request = received[1]
                    self.assertEqual(request[:4], b"\x05\x01\x00" + expected_atyp)
                    if scheme == "socks5":
                        self.assertEqual(request[4:8], socket.inet_aton("127.0.0.1"))
                    else:
                        self.assertIn(b"\x0bdns.example", request)
                finally:
                    local.close()
                    server.close()

    def test_plain_http_absolute_form_and_body_are_forwarded(self):
        relay = ProxyChainRelay("http://target.example:8080", "http://upstream.example:8081")
        client, local = socket.socketpair()
        remote, target = socket.socketpair()
        relay._connect_target_through_upstream = lambda host, port: remote
        thread = threading.Thread(target=relay._handle_client, args=(local,), daemon=True)
        thread.start()
        try:
            client.sendall(
                b"POST http://origin.example/form HTTP/1.1\r\n"
                b"Host: origin.example\r\nContent-Length: 4\r\n\r\nbody"
            )
            target.settimeout(2)
            forwarded = target.recv(4096)
            self.assertIn(b"POST http://origin.example/form HTTP/1.1", forwarded)
            self.assertTrue(forwarded.endswith(b"body"))
            target.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            client.settimeout(2)
            self.assertIn(b"200 OK", client.recv(4096))
        finally:
            client.close()
            target.close()
            relay._stop.set()
            thread.join(timeout=2)
            remote.close()


if __name__ == "__main__":
    unittest.main()
