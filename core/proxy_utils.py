"""Common parsing helpers for HTTP and SOCKS proxy URLs."""
from __future__ import annotations

import socket
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit


SUPPORTED_PROXY_SCHEMES = frozenset({
    "http", "https", "socks4", "socks4a", "socks5", "socks5h",
})


@dataclass(frozen=True)
class ProxySpec:
    raw: str
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""


def parse_proxy_url(value: str, *, allow_bare: bool = False, default_scheme: str = "http") -> ProxySpec:
    """Parse a proxy URL into its transport components.

    Configuration accepts regular URLs as well as common legacy authority
    forms (``host:port:user:password``).  Canonicalizing through the same
    helper used by :mod:`config.proxy` keeps explicit legacy values and bare
    upstream values consistent; bare values remain opt-in and default to
    HTTP, which is the documented upstream-proxy convention.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("代理地址为空")
    text = text.replace("\\@", "@")
    has_scheme = "://" in text
    if not has_scheme and not allow_bare:
        raise ValueError("代理地址必须包含协议")
    scheme_hint = str(default_scheme or "http").strip().lower() or "http"
    if not has_scheme and scheme_hint not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError(f"不支持的代理协议: {scheme_hint}")
    try:
        # ``normalize_proxy_url`` handles legacy credentials, IPv6 authority,
        # percent encoding, and rejects path/query/fragment values.  Import it
        # lazily so this helper does not create an import cycle at startup.
        from config.proxy import normalize_proxy_url

        text = normalize_proxy_url(text, default_scheme=scheme_hint)
    except ValueError:
        raise
    if not text:
        raise ValueError("代理地址为空")
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise ValueError("代理 URL 无效") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError(f"不支持的代理协议: {scheme or '-'}")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("代理 URL 不应包含 path/query/fragment")
    if not parsed.hostname:
        raise ValueError("代理格式缺少 host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理端口无效") from exc
    if not port or not 1 <= port <= 65535:
        raise ValueError("代理格式缺少有效 port")
    return ProxySpec(
        raw=text,
        scheme=scheme,
        # URL authorities encode IPv6 scope separators as ``%25``; socket and
        # PySocks APIs expect the decoded interface marker (for example
        # ``fe80::1%eth0``).  Keep the canonical encoded form in ``raw``.
        host=unquote(parsed.hostname),
        port=port,
        username=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
    )


def mask_proxy_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        spec = parse_proxy_url(text, allow_bare=True)
    except ValueError:
        return "***"
    auth = "***:***@" if spec.username or spec.password else ""
    host = f"[{spec.host}]" if ":" in spec.host and not spec.host.startswith("[") else spec.host
    return f"{spec.scheme}://{auth}{host}:{spec.port}"


def validate_proxy_lines(lines) -> None:
    """Validate non-empty proxy-pool lines using the normalizer's semantics."""
    for index, line in enumerate(lines or [], 1):
        if not str(line or "").strip():
            continue
        try:
            parse_proxy_url(line, allow_bare=True)
        except ValueError as exc:
            raise ValueError(f"代理池第 {index} 行格式错误: {exc}") from exc


def diagnose_proxy_endpoint(value: str, *, timeout: float = 2.0) -> str:
    """Perform a scheme-appropriate, non-destructive endpoint check.

    SOCKS5/5H have a harmless greeting that can be probed without choosing a
    destination.  HTTP(S) and SOCKS4/4A do not have an equivalent greeting, so
    this function only verifies reachability for those schemes instead of
    sending a SOCKS5 packet to an HTTP or SOCKS4 server.
    """
    try:
        spec = parse_proxy_url(value, allow_bare=True)
        with socket.create_connection((spec.host, spec.port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            if spec.scheme in {"http", "https"}:
                return f"{spec.scheme.upper()} 代理端口可达；请通过实际 HTTP CONNECT 请求验证授权和目标访问"
            if spec.scheme in {"socks4", "socks4a"}:
                return f"{spec.scheme.upper()} 代理端口可达；请通过实际连接验证授权和目标访问"
            sock.sendall(b"\x05\x01\x00")
            response = sock.recv(512)
    except Exception:
        return ""
    if response.startswith(b"HTTP/"):
        first_line = response.splitlines()[0].decode("latin1", errors="replace")
        return f"代理端口返回 {first_line}，当前端口可能不是 SOCKS5 或授权/白名单不匹配"
    if response[:1] == b"\x05":
        return "SOCKS5 握手响应正常，可能是账号密码或目标地址被拒绝"
    return "代理端口返回了非 SOCKS5 响应，请确认协议和端口"
