"""Small local HTTP relay for chaining an upstream proxy to a target proxy."""
from __future__ import annotations

import base64
import ipaddress
import logging
import select
import socket
import ssl
import struct
import threading
import time
from urllib.parse import unquote, urlsplit

from core.proxy_utils import mask_proxy_url, parse_proxy_url

try:
    import socks
except ImportError:  # pragma: no cover - dependency is declared in requirements.txt
    socks = None


logger = logging.getLogger(__name__)


class ProxyChainError(ConnectionError):
    """A user-facing error from the outer proxy hop."""


class _BufferedSocket:
    """Socket adapter retaining bytes read past an HTTP header terminator.

    ``recv`` may return a request/response body together with its headers.  The
    relay must not lose those bytes while parsing the control response.  This
    small adapter keeps the already-read suffix while exposing the socket API
    used by ``select`` and the relay.
    """

    def __init__(self, sock: socket.socket, pending: bytes = b""):
        self._sock = sock
        self._pending = bytearray(pending)

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def recv(self, size: int, flags: int = 0) -> bytes:
        if self._pending:
            data = bytes(self._pending[:size])
            del self._pending[:size]
            return data
        return self._sock.recv(size, flags)

    def recv_into(self, buffer, nbytes: int = 0, flags: int = 0) -> int:
        if self._pending:
            size = nbytes or len(buffer)
            data = self.recv(size, flags)
            buffer[:len(data)] = data
            return len(data)
        return self._sock.recv_into(buffer, nbytes or len(buffer), flags)

    def send(self, data: bytes, *args) -> int:
        return self._sock.send(data, *args)

    def sendall(self, data: bytes, *args) -> None:
        self._sock.sendall(data, *args)

    def settimeout(self, value) -> None:
        self._sock.settimeout(value)

    def setblocking(self, flag: bool) -> None:
        self._sock.setblocking(flag)

    def fileno(self) -> int:
        return self._sock.fileno()

    def close(self) -> None:
        self._sock.close()

    def shutdown(self, how: int) -> None:
        self._sock.shutdown(how)

    def __getattr__(self, name):
        return getattr(self._sock, name)


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("SOCKS 客户端提前断开")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_socks_address(sock: socket.socket, atyp: int) -> tuple[str, int]:
    if atyp == 1:
        host = socket.inet_ntoa(_read_exact(sock, 4))
    elif atyp == 3:
        length = _read_exact(sock, 1)[0]
        host = _read_exact(sock, length).decode("idna")
    elif atyp == 4:
        host = socket.inet_ntop(socket.AF_INET6, _read_exact(sock, 16))
    else:
        raise ValueError("SOCKS5 地址类型无效")
    port = int.from_bytes(_read_exact(sock, 2), "big")
    return host, port


def _proxy_authorization(username: str, password: str) -> str:
    credentials = f"{username or ''}:{password or ''}".encode("utf-8")
    return "Basic " + base64.b64encode(credentials).decode("ascii")


class ProxyChainRelay:
    """Expose a local HTTP proxy endpoint through a target and upstream proxy.

    The target and upstream accept every scheme advertised by ``config.proxy``:
    HTTP(S), SOCKS4/4A and SOCKS5/5H.  The local endpoint intentionally remains
    an HTTP proxy because that is the common denominator for curl and browsers.
    """

    def __init__(self, target: str, upstream: str, *, timeout: float = 15.0):
        # Bare values are common for the editable upstream field.  Treat them
        # as HTTP rather than allowing urlsplit/PySocks to interpret them as a
        # malformed scheme.
        self.target = parse_proxy_url(target, allow_bare=True)
        self.upstream = parse_proxy_url(upstream, allow_bare=True)
        if self.target.scheme == "https" and self.upstream.scheme == "https":
            # Wrapping an existing SSLSocket with a second stdlib TLS layer
            # would detach the first layer's file descriptor and silently
            # bypass the upstream TLS session.  Reject this rare combination
            # explicitly rather than failing only after traffic starts.
            raise ValueError("代理链暂不支持 https:// 目标代理经 https:// 上游代理连接")
        if socks is None:
            raise RuntimeError("代理链需要 PySocks，请先安装 requirements.txt 中的依赖")
        # PySocks uses the URL username for SOCKS4/SOCKS4A userid; its
        # password argument is ignored by the protocol, but passing it through
        # keeps URL handling uniform and avoids silently rejecting advertised
        # schemes before the transport is attempted.
        # This bounds setup/handshake operations only.  Tunnel sockets become
        # blocking after CONNECT, so a short test timeout cannot recreate the
        # old fixed 0.5s relay teardown behavior.
        self.timeout = max(0.1, float(timeout or 15.0))
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        self._last_error = ""
        self._traffic_lock = threading.Lock()
        self._upload_bytes = 0
        self._download_bytes = 0

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def proxy_url(self) -> str:
        if self._listener is None:
            raise RuntimeError("代理链尚未启动")
        return f"http://127.0.0.1:{self._listener.getsockname()[1]}"

    def traffic_snapshot(self) -> dict[str, int | bool]:
        """返回整个代理链隧道的实际传输字节数。"""
        with self._traffic_lock:
            upload = int(self._upload_bytes)
            download = int(self._download_bytes)
        return {
            "available": True,
            "upload_bytes": upload,
            "download_bytes": download,
            "total_bytes": upload + download,
        }

    def start(self) -> "ProxyChainRelay":
        if self._listener is not None:
            return self
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(16)
            listener.settimeout(0.25)
            self._listener = listener
            self._accept_thread = threading.Thread(target=self._accept_loop, name="proxy-chain-accept", daemon=True)
            self._accept_thread.start()
            return self
        except Exception:
            self._listener = None
            try:
                listener.close()
            except OSError:
                pass
            raise

    def close(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for conn in connections:
            try:
                conn.close()
            except OSError:
                pass
        thread, self._accept_thread = self._accept_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def __enter__(self) -> "ProxyChainRelay":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _track(self, conn: socket.socket, add: bool) -> None:
        if conn is None:
            return
        with self._connections_lock:
            if add:
                self._connections.add(conn)
            else:
                self._connections.discard(conn)

    def _adopt_remote(self, previous, current):
        """Track the currently active remote socket/wrapper exactly once."""
        if previous is not None and previous is not current:
            self._track(previous, False)
        if current is not None:
            self._track(current, True)
        return current

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._track(client, True)
            threading.Thread(target=self._handle_client, args=(client,), name="proxy-chain-client", daemon=True).start()

    @staticmethod
    def _validate_host(host: str) -> str:
        text = str(host or "")
        if not text or any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in text):
            raise ValueError("HTTP 代理目标 host 含非法字符")
        if any(ch in text for ch in "\r\n\x00@/?#"):
            raise ValueError("HTTP 代理目标 host 无效")
        try:
            if ":" in text:
                ipaddress.ip_address(text.split("%", 1)[0])
        except ValueError as exc:
            raise ValueError("HTTP 代理目标 IPv6 host 无效") from exc
        return text

    @staticmethod
    def _destination(host: str, port: int) -> str:
        host = ProxyChainRelay._validate_host(host)
        return f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"

    @staticmethod
    def _read_http_headers(
        sock: socket.socket,
        *,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        # Read one byte at a time so a CONNECT response cannot consume the
        # first TLS/application bytes that belong to the next protocol layer.
        # A few socket fakes ignore recv(size), so retain any over-read suffix
        # as well; real sockets normally leave this buffer empty.  The aggregate
        # deadline prevents a slow-drip peer from pinning a relay worker forever.
        response = bytearray()
        pending = bytearray()
        deadline = time.monotonic() + float(timeout) if timeout is not None else None
        while not response.endswith(b"\r\n\r\n"):
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("HTTP 代理响应头读取超时")
                try:
                    readable, _, _ = select.select([sock], [], [], remaining)
                except (AttributeError, OSError, ValueError):
                    readable = [sock]
                if not readable:
                    raise TimeoutError("HTTP 代理响应头读取超时")
            if pending:
                chunk = bytes(pending)
                pending.clear()
            else:
                chunk = sock.recv(1)
            if not chunk:
                raise ConnectionError("HTTP 代理提前断开")
            response.append(chunk[0])
            if len(chunk) > 1:
                pending.extend(chunk[1:])
            if len(response) > 64 * 1024:
                raise ValueError("HTTP 代理响应头过大")
        return bytes(response), bytes(pending)

    @classmethod
    def _http_proxy_connect(
        cls,
        remote: socket.socket,
        host: str,
        port: int,
        proxy,
        *,
        timeout: float | None = None,
    ) -> socket.socket | _BufferedSocket:
        """Establish a CONNECT tunnel through an HTTP(S) proxy socket."""
        cls._validate_host(host)
        if not 1 <= int(port) <= 65535:
            raise ValueError("HTTP 代理目标端口无效")
        destination = cls._destination(host, port)
        headers = [
            f"CONNECT {destination} HTTP/1.1",
            f"Host: {destination}",
            "Proxy-Connection: Keep-Alive",
        ]
        if proxy.username or proxy.password:
            headers.append("Proxy-Authorization: " + _proxy_authorization(proxy.username, proxy.password))
        try:
            request = ("\r\n".join(headers) + "\r\n\r\n").encode("latin1")
            cls._send_with_deadline(remote, request, timeout or 15.0)
            response, pending = cls._read_http_headers(remote, timeout=timeout)
            first_line = response.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
            parts = first_line.split(" ", 2)
            if len(parts) < 2 or not parts[0].startswith("HTTP/"):
                raise ProxyChainError("上游 HTTP 代理返回了无效响应")
            try:
                status = int(parts[1])
            except ValueError as exc:
                raise ProxyChainError("上游 HTTP 代理返回了无效状态码") from exc
            if status != 200:
                raise ProxyChainError(f"上游 HTTP CONNECT 失败（HTTP {status}）")
            return remote if not pending else _BufferedSocket(remote, pending)
        except BaseException:
            try:
                remote.close()
            except BaseException:
                pass
            raise

    def _connect_target_through_upstream(self, host: str, port: int) -> socket.socket:
        self._validate_host(host)
        scheme = self.upstream.scheme
        if scheme in {"http", "socks4", "socks4a", "socks5", "socks5h"}:
            if scheme == "http":
                proxy_type = socks.HTTP
                rdns = True
            elif scheme in {"socks4", "socks4a"}:
                proxy_type = socks.SOCKS4
                rdns = scheme == "socks4a"
            else:
                proxy_type = socks.SOCKS5
                rdns = scheme == "socks5h"
            remote = socks.socksocket()
            self._track(remote, True)
            try:
                remote.set_proxy(
                    proxy_type,
                    addr=self.upstream.host,
                    port=self.upstream.port,
                    username=self.upstream.username or None,
                    password=self.upstream.password or None,
                    rdns=rdns,
                )
                remote.settimeout(self.timeout)
                remote.connect((host, port))
                return remote
            except Exception:
                try:
                    remote.close()
                except OSError:
                    pass
                raise

        # PySocks does not implement an HTTPS proxy transport.  An HTTPS proxy
        # is an HTTP CONNECT server reached over TLS, so establish the TCP/TLS
        # hop explicitly and then issue the same CONNECT request.
        if scheme == "https":
            raw = socket.create_connection(
                (self.upstream.host, self.upstream.port),
                timeout=self.timeout,
            )
            self._track(raw, True)
            raw.settimeout(self.timeout)
            remote = None
            try:
                context = ssl.create_default_context()
                remote = context.wrap_socket(raw, server_hostname=self.upstream.host)
                self._adopt_remote(raw, remote)
                connected = self._http_proxy_connect(remote, host, port, self.upstream, timeout=self.timeout)
                if connected is not remote:
                    connected = self._adopt_remote(remote, connected)
                return connected
            except Exception:
                # wrap_socket detaches the descriptor from ``raw`` on success;
                # close both references defensively on TLS/CONNECT failures.
                for conn in (remote, raw):
                    if conn is not None:
                        try:
                            conn.close()
                        except OSError:
                            pass
                raise

        # parse_proxy_url rejects this branch; keep it defensive if a future
        # scheme is added without implementing its transport here.
        raise ValueError(f"代理链不支持上游协议: {scheme}")

    @staticmethod
    def _resolve_local(host: str, port: int, *, ipv4_only: bool = False) -> str:
        try:
            address = ipaddress.ip_address(host)
            if ipv4_only and address.version != 4:
                raise ValueError("SOCKS4 只支持 IPv4 目标")
            return str(address)
        except ValueError as original:
            families = socket.AF_INET if ipv4_only else socket.AF_UNSPEC
            try:
                addresses = socket.getaddrinfo(host, port, families, socket.SOCK_STREAM)
            except OSError as exc:
                raise ConnectionError(f"无法本地解析目标域名: {host}") from exc
            if not addresses:
                raise ConnectionError(f"无法本地解析目标域名: {host}") from original
            return str(addresses[0][4][0])

    @staticmethod
    def _pack_socks_address(host: str, port: int) -> bytes:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            encoded = host.encode("idna")
            if len(encoded) > 255:
                raise ValueError("SOCKS5 目标域名过长")
            return b"\x03" + bytes([len(encoded)]) + encoded + int(port).to_bytes(2, "big")
        if address.version == 4:
            return b"\x01" + address.packed + int(port).to_bytes(2, "big")
        return b"\x04" + address.packed + int(port).to_bytes(2, "big")

    @staticmethod
    def _read_socks_reply(sock: socket.socket) -> int:
        header = _read_exact(sock, 4)
        if header[0] != 5:
            raise ValueError("动态代理返回了非 SOCKS5 响应")
        _read_socks_address(sock, header[3])
        return header[1]

    def _target_socks5_connect(self, remote: socket.socket, host: str, port: int) -> None:
        methods = b"\x00"
        if self.target.username or self.target.password:
            methods += b"\x02"
        remote.sendall(b"\x05" + bytes([len(methods)]) + methods)
        selected = _read_exact(remote, 2)
        if selected[0] != 5:
            response = selected
            try:
                response += remote.recv(512)
            except OSError:
                pass
            if response.startswith(b"HTTP/"):
                first_line = response.splitlines()[0].decode("latin1", errors="replace")
                raise ProxyChainError(f"动态代理经本地上游返回 {first_line}，不是 SOCKS5 响应")
            raise ProxyChainError("动态代理经本地上游返回了非 SOCKS5 响应")
        if selected[1] == 0xFF:
            raise ProxyChainError("动态代理不接受当前认证方式")
        if selected[1] == 2:
            username = self.target.username.encode("utf-8")
            password = self.target.password.encode("utf-8")
            if len(username) > 255 or len(password) > 255:
                raise ValueError("SOCKS5 账号或密码过长")
            remote.sendall(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
            auth = _read_exact(remote, 2)
            if auth[0] != 1 or auth[1] != 0:
                raise PermissionError("动态代理账号密码认证失败")
        elif selected[1] != 0:
            raise ConnectionError("动态代理选择了不支持的认证方式")

        # socks5:// deliberately resolves locally; socks5h:// leaves the
        # hostname in the request so the target proxy performs DNS resolution.
        request_host = host if self.target.scheme == "socks5h" else self._resolve_local(host, port)
        remote.sendall(b"\x05\x01\x00" + self._pack_socks_address(request_host, port))
        reply_code = self._read_socks_reply(remote)
        if reply_code:
            raise ConnectionError(f"动态代理 CONNECT 失败（SOCKS5 code {reply_code}）")

    def _target_socks4_connect(self, remote: socket.socket, host: str, port: int) -> None:
        try:
            ip_address = ipaddress.ip_address(host)
        except ValueError:
            ip_address = None
        remote_name = self.target.scheme == "socks4a" and ip_address is None
        if remote_name:
            address = b"\x00\x00\x00\x01"
        else:
            address = socket.inet_aton(self._resolve_local(host, port, ipv4_only=True))
        username = self.target.username.encode("utf-8")
        if len(username) > 255:
            raise ValueError("SOCKS4 用户名过长")
        request = struct.pack("!BBH", 4, 1, port) + address + username + b"\x00"
        if remote_name:
            encoded = host.encode("idna")
            if len(encoded) > 255:
                raise ValueError("SOCKS4 目标域名过长")
            request += encoded + b"\x00"
        remote.sendall(request)
        response = _read_exact(remote, 8)
        if response[0] != 0 or response[1] != 0x5A:
            raise ConnectionError(f"动态代理 CONNECT 失败（SOCKS4 code {response[1]:#x}）")

    # Compatibility with callers/tests written against the original helper.
    def _target_socks_connect(self, remote: socket.socket, host: str, port: int) -> None:
        if self.target.scheme in {"socks4", "socks4a"}:
            return self._target_socks4_connect(remote, host, port)
        return self._target_socks5_connect(remote, host, port)

    def _target_http_connect(self, remote: socket.socket, host: str, port: int) -> socket.socket:
        """经上游连接到 HTTP 目标代理，并在目标代理上建立 CONNECT 隧道。"""
        return self._http_proxy_connect(remote, host, port, self.target, timeout=self.timeout)

    @staticmethod
    def _read_http_request_data(
        client: socket.socket,
        *,
        timeout: float | None = None,
    ) -> tuple[str, str, str, bytes, bytes]:
        """Read one HTTP proxy request header block and preserve over-read bytes."""
        request = bytearray()
        deadline = time.monotonic() + float(timeout) if timeout is not None else None
        marker = -1
        while marker < 0:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("HTTP 代理请求头读取超时")
                try:
                    readable, _, _ = select.select([client], [], [], remaining)
                except (OSError, ValueError):
                    readable = [client]
                if not readable:
                    raise TimeoutError("HTTP 代理请求头读取超时")
            chunk = client.recv(4096)
            if not chunk:
                raise ConnectionError("HTTP 代理客户端提前断开")
            request.extend(chunk)
            marker = request.find(b"\r\n\r\n")
            if marker < 0 and len(request) > 64 * 1024:
                raise ValueError("HTTP 代理请求头过大")
        if marker > 64 * 1024:
            raise ValueError("HTTP 代理请求头过大")
        end = marker + 4
        headers = bytes(request[:end])
        pending = bytes(request[end:])
        first_line = headers.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
        parts = first_line.split(" ", 2)
        if len(parts) != 3:
            raise ValueError("HTTP 代理请求行无效")
        return parts[0].upper(), parts[1], parts[2], headers, pending

    @classmethod
    def _read_http_request(cls, client: socket.socket) -> tuple[str, str, str]:
        method, destination, version, _, _ = cls._read_http_request_data(client)
        return method, destination, version

    @staticmethod
    def _split_host_port(destination: str) -> tuple[str, int]:
        text = str(destination or "")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in text):
            raise ValueError("HTTP 代理目标格式含非法字符")
        try:
            if text.startswith("["):
                if "]:" not in text:
                    raise ValueError
                host, port_text = text.rsplit("]:", 1)
                if not host.endswith("]"):
                    raise ValueError
                host = host[1:-1]
            else:
                if text.count(":") != 1:
                    raise ValueError
                host, port_text = text.rsplit(":", 1)
            port = int(port_text)
        except (ValueError, IndexError) as exc:
            raise ValueError("HTTP 代理目标格式无效") from exc
        ProxyChainRelay._validate_host(host)
        if not 1 <= port <= 65535:
            raise ValueError("HTTP 代理目标端口无效")
        return host, port

    @staticmethod
    def _absolute_destination(destination: str) -> tuple[str, int, str]:
        parsed = urlsplit(destination)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            raise ValueError("HTTP 代理请求必须使用 absolute-form URL")
        try:
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        except ValueError as exc:
            raise ValueError("HTTP 代理目标端口无效") from exc
        if not 1 <= port <= 65535:
            raise ValueError("HTTP 代理目标端口无效")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        return parsed.hostname, port, path

    @staticmethod
    def _rewrite_proxy_headers(raw: bytes, *, username: str = "", password: str = "") -> bytes:
        """Remove client-side proxy headers and optionally inject target auth."""
        marker = raw.find(b"\r\n\r\n")
        if marker < 0:
            return raw
        lines = raw[:marker].split(b"\r\n")
        output = [lines[0]]
        for line in lines[1:]:
            lower = line.lower()
            if lower.startswith((b"proxy-authorization:", b"proxy-connection:")):
                continue
            output.append(line)
        if username or password:
            output.append(
                ("Proxy-Authorization: " + _proxy_authorization(username, password)).encode("latin1")
            )
        # The relay handles one absolute-form request per target connection.
        # Ask the target to close after its response so a second client request
        # cannot be sent over a connection bound to a different origin.
        output = [line for line in output if not line.lower().startswith(b"connection:")]
        output.append(b"Connection: close")
        return b"\r\n".join(output) + b"\r\n\r\n" + raw[marker + 4 :]

    @staticmethod
    def _send_with_deadline(sock: socket.socket, data: bytes, timeout: float) -> None:
        view = memoryview(data)
        deadline = time.monotonic() + max(0.1, float(timeout or 15.0))
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("代理链写入超时")
            try:
                _, writable, exceptional = select.select([], [sock], [sock], remaining)
            except (OSError, ValueError) as exc:
                raise ConnectionError("代理链写入端已关闭") from exc
            if exceptional or not writable:
                raise TimeoutError("代理链写入超时")
            try:
                sent = sock.send(view)
            except (BlockingIOError, InterruptedError, socket.timeout, ssl.SSLWantWriteError, ssl.SSLWantReadError):
                continue
            if not sent:
                raise ConnectionError("代理链写入端已关闭")
            view = view[sent:]

    @staticmethod
    def _to_origin_form(raw: bytes, method: str, destination: str, version: str) -> bytes:
        _, _, path = ProxyChainRelay._absolute_destination(destination)
        marker = raw.find(b"\r\n\r\n")
        if marker < 0:
            return raw
        lines = raw[:marker].split(b"\r\n")
        lines[0] = f"{method} {path} {version}".encode("latin1")
        lines = [
            line for line in lines
            if not line.lower().startswith((b"proxy-authorization:", b"proxy-connection:", b"connection:"))
        ]
        lines.append(b"Connection: close")
        return b"\r\n".join(lines) + b"\r\n\r\n" + raw[marker + 4 :]

    @staticmethod
    def _request_body_length(headers: bytes) -> int:
        """Return the first request body length and reject ambiguous framing."""
        values: list[str] = []
        transfer = ""
        for line in headers.split(b"\r\n")[1:]:
            name, sep, value = line.partition(b":")
            if not sep:
                continue
            key = name.strip().lower()
            text = value.decode("latin1", errors="replace").strip()
            if key == b"content-length":
                values.append(text)
            elif key == b"transfer-encoding":
                transfer = text.lower()
        if transfer and transfer not in {"identity"}:
            raise ValueError("HTTP 代理暂不支持 Transfer-Encoding 请求体")
        if not values:
            return 0
        if any(not value.isdigit() for value in values):
            raise ValueError("HTTP 代理 Content-Length 无效")
        lengths = {int(value, 10) for value in values}
        if len(lengths) != 1:
            raise ValueError("HTTP 代理 Content-Length 不一致")
        length = lengths.pop()
        if length > 16 * 1024 * 1024:
            raise ValueError("HTTP 代理请求体过大")
        return length

    @staticmethod
    def _send_http_error(client: socket.socket, status: int = 502, reason: str = "Bad Gateway") -> None:
        try:
            client.sendall(
                f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode("latin1")
            )
        except OSError:
            pass

    def _wrap_target_tls(self, remote: socket.socket) -> socket.socket:
        context = ssl.create_default_context()
        try:
            wrapped = context.wrap_socket(remote, server_hostname=self.target.host)
        except BaseException:
            try:
                remote.close()
            except BaseException:
                pass
            self._track(remote, False)
            raise
        self._adopt_remote(remote, wrapped)
        return wrapped

    def _handle_plain_http(
        self,
        client: socket.socket,
        method: str,
        destination: str,
        version: str,
        raw_request: bytes,
    ) -> socket.socket:
        host, port, _ = self._absolute_destination(destination)
        remote = None
        try:
            remote = self._connect_target_through_upstream(self.target.host, self.target.port)
            self._track(remote, True)
            if self.target.scheme == "https":
                remote = self._wrap_target_tls(remote)
            if self.target.scheme in {"socks4", "socks4a", "socks5", "socks5h"}:
                self._target_socks_connect(remote, host, port)
                raw_request = self._to_origin_form(raw_request, method, destination, version)
            else:
                raw_request = self._rewrite_proxy_headers(
                    raw_request,
                    username=self.target.username,
                    password=self.target.password,
                )
            self._send_with_deadline(remote, raw_request, self.timeout)
            return remote
        except BaseException:
            if remote is not None:
                self._track(remote, False)
                try:
                    remote.close()
                except BaseException:
                    pass
            raise

    @staticmethod
    def _read_request_body(
        client: socket.socket,
        headers: bytes,
        pending: bytes,
        length: int,
        timeout: float,
    ) -> bytes:
        if length <= 0:
            return b""
        body = bytearray(pending[:length])
        deadline = time.monotonic() + max(0.1, float(timeout or 15.0))
        while len(body) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("HTTP 代理请求体读取超时")
            try:
                readable, _, _ = select.select([client], [], [], remaining)
            except (OSError, ValueError):
                readable = [client]
            if not readable:
                raise TimeoutError("HTTP 代理请求体读取超时")
            chunk = client.recv(min(64 * 1024, length - len(body)))
            if not chunk:
                raise ConnectionError("HTTP 代理请求体提前断开")
            body.extend(chunk)
        return bytes(body)

    def _handle_client(self, client: socket.socket) -> None:
        remote: socket.socket | None = None
        established = False
        try:
            # The request/CONNECT phase has a bounded timeout; the tunnel does
            # not.  A fixed 0.5s socket timeout used to tear down slow HTTPS
            # streams even though select() had already indicated readability.
            client.settimeout(self.timeout)
            method, destination, version, headers, pending = self._read_http_request_data(
                client, timeout=self.timeout,
            )
            if method == "CONNECT":
                host, port = self._split_host_port(destination)
                remote = self._connect_target_through_upstream(self.target.host, self.target.port)
                if self.target.scheme == "https":
                    remote = self._wrap_target_tls(remote)
                if self.target.scheme in {"socks4", "socks4a", "socks5", "socks5h"}:
                    self._target_socks_connect(remote, host, port)
                else:
                    previous = remote
                    connected = self._target_http_connect(remote, host, port)
                    if connected is not None:
                        remote = self._adopt_remote(previous, connected)
                # Keep tunnel I/O interruptible; close() must be able to wake
                # both endpoints while relay workers are waiting or writing.
                self._send_with_deadline(
                    client,
                    b"HTTP/1.1 200 Connection Established\r\nConnection: keep-alive\r\n\r\n",
                    self.timeout,
                )
                established = True
                # A browser can put the TLS ClientHello in the same write as
                # the CONNECT headers.  Never discard those bytes while
                # parsing the local request.
                if pending:
                    self._send_with_deadline(remote, pending, self.timeout)
                    with self._traffic_lock:
                        self._upload_bytes += len(pending)
                self._prepare_relay_sockets(client, remote)
                self._relay(client, remote)
            else:
                body_length = self._request_body_length(headers)
                body = self._read_request_body(client, headers, pending, body_length, self.timeout)
                raw_request = headers + body
                remote = self._handle_plain_http(client, method, destination, version, raw_request)
                self._prepare_relay_sockets(client, remote)
                with self._traffic_lock:
                    self._upload_bytes += len(raw_request)
                established = True
                self._relay(client, remote, allow_client_to_remote=False)
        except Exception as exc:
            detail = str(exc or "").strip()
            self._last_error = f"{type(exc).__name__}: {detail[:240]}" if detail else type(exc).__name__
            logger.warning(
                "代理链连接失败 target=%s upstream=%s error=%s",
                mask_proxy_url(self.target.raw),
                mask_proxy_url(self.upstream.raw),
                self._last_error,
            )
            if not established:
                self._send_http_error(client)
        finally:
            for conn in (client, remote):
                if conn is not None:
                    self._track(conn, False)
                    try:
                        conn.close()
                    except OSError:
                        pass

    def _prepare_relay_sockets(self, client: socket.socket, remote: socket.socket) -> None:
        """Make tunnel I/O interruptible by ``close()`` and bounded writes."""
        for conn in (client, remote):
            try:
                conn.setblocking(False)
            except (AttributeError, OSError):
                try:
                    conn.settimeout(0.5)
                except (AttributeError, OSError):
                    pass

    def _relay(
        self,
        client: socket.socket,
        remote: socket.socket,
        *,
        allow_client_to_remote: bool = True,
    ) -> None:
        sockets = [client, remote]
        while not self._stop.is_set():
            # A buffered HTTP CONNECT response can already contain the first
            # tunnel bytes.  They are not visible to select(2), so drain such
            # adapters before waiting on file descriptors.
            buffered = [sock for sock in sockets if getattr(sock, "has_pending", False) is True]
            if buffered:
                readable = buffered
                exceptional = []
            else:
                try:
                    readable, _, exceptional = select.select(sockets, [], sockets, 1.0)
                except (OSError, ValueError):
                    return
            if exceptional:
                return
            if not readable:
                continue
            for source in readable:
                try:
                    data = source.recv(64 * 1024)
                except (BlockingIOError, InterruptedError, socket.timeout):
                    continue
                except OSError:
                    return
                if not data:
                    return
                if source is client and not allow_client_to_remote:
                    return
                destination = remote if source is client else client
                try:
                    self._send_with_deadline(destination, data, getattr(self, "timeout", 15.0))
                except (OSError, TimeoutError, ConnectionError):
                    return
                with self._traffic_lock:
                    if source is client:
                        self._upload_bytes += len(data)
                    else:
                        self._download_bytes += len(data)


def open_proxy_pool_proxy(
    selected_proxy: str | None = None,
    *,
    timeout: float = 15.0,
):
    """Resolve a proxy-pool target and optionally wrap it with its own upstream.

    The package/Agent upstream is intentionally not consulted here.  An empty
    ``PROXY_POOL_UPSTREAM_PROXY`` returns the selected target unchanged and no
    relay, so callers can use the same cleanup path for both modes.
    """
    from config import proxy as proxy_cfg

    target = str(
        proxy_cfg.pick_proxy() if selected_proxy is None else selected_proxy
        or ""
    ).strip()
    upstream = str(getattr(proxy_cfg, "PROXY_POOL_UPSTREAM_PROXY", "") or "").strip()
    if not target:
        return target, None

    # Parse even when no relay is needed so a bare selected value is normalized
    # to the same explicit HTTP URL used by the rest of the application.
    target_spec = parse_proxy_url(target, allow_bare=True)
    target = target_spec.raw
    if not upstream:
        return target, None

    relay = ProxyChainRelay(target, upstream, timeout=timeout).start()
    return relay.proxy_url, relay
