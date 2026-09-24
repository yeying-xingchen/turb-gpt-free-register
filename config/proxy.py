# -*- coding: utf-8 -*-
"""
代理池配置

每次注册随机抽取一个代理，保证不同 sid 之间彼此独立，避免风控关联。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）
"""
import ipaddress
import random
import re
from urllib.parse import quote, unquote, urlsplit

from config.env_loader import apply_env_overrides


_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks4a", "socks5", "socks5h"})
_PROXY_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")


def _proxy_format_error(reason: str) -> ValueError:
    return ValueError(f"代理格式无效：{reason}")


def _normalise_proxy_host(host: str) -> str:
    # Host 中允许已经 percent-encoded 的 IPv6 zone id（例如 %25eth0），
    # 但最终输出必须是 URI 形式的 %25，不能把裸 % 交给 curl。
    host = unquote(str(host or "").strip())
    if not host:
        raise _proxy_format_error("缺少 host")
    if any(ch.isspace() for ch in host) or any(ch in host for ch in "/?#@\\[]"):
        raise _proxy_format_error("host 无效")
    if ":" in host:
        # IPv6 代理必须使用方括号，避免与 host:port 分隔符混淆。
        address, separator, zone = host.partition("%")
        try:
            ipaddress.ip_address(address)
        except ValueError as exc:
            raise _proxy_format_error("IPv6 host 无效或未使用方括号") from exc
        if separator:
            if not zone or any(ch.isspace() for ch in zone) or any(ch in zone for ch in "/?#@[]"):
                raise _proxy_format_error("IPv6 zone id 无效")
            return f"[{address}%25{quote(zone, safe='')}]"
        return f"[{address}]"
    if "%" in host:
        raise _proxy_format_error("非 IPv6 host 不应包含 zone id")
    return host


def _normalise_proxy_port(port: str) -> str:
    port = str(port or "").strip()
    if not re.fullmatch(r"[0-9]+", port):
        raise _proxy_format_error("port 必须是十进制数字")
    number = int(port)
    if not 1 <= number <= 65535:
        raise _proxy_format_error("port 必须在 1 到 65535 之间")
    return str(number)


def _quote_proxy_credential(value: str | None) -> str:
    if value is None:
        return ""
    # 先解码再编码，避免已经写成 %40 的凭据被二次编码成 %2540。
    return quote(unquote(str(value)), safe="")


def _endpoint_looks_standard(endpoint: str) -> bool:
    """判断 endpoint 是否像标准 host[:port]（支持方括号 IPv6）。"""
    endpoint = str(endpoint or "")
    if endpoint.startswith("["):
        close = endpoint.find("]")
        if close <= 1:
            return False
        suffix = endpoint[close + 1 :]
        return not suffix or (suffix.startswith(":") and suffix.count(":") == 1)
    return endpoint.count(":") <= 1


def _endpoint_has_explicit_port(endpoint: str) -> bool:
    """Return whether an authority suffix has a syntactically valid port."""
    endpoint = str(endpoint or "")
    if endpoint.startswith("["):
        close = endpoint.find("]")
        if close <= 1 or not endpoint[close + 1 :].startswith(":"):
            return False
        return _valid_proxy_port_text(endpoint[close + 2 :])
    if endpoint.count(":") != 1:
        return False
    host, port = endpoint.rsplit(":", 1)
    return bool(host) and _valid_proxy_port_text(port)


def _valid_proxy_port_text(value: str | None) -> bool:
    return bool(re.fullmatch(r"[0-9]+", str(value or ""))) and 1 <= int(value) <= 65535


def _looks_like_forward_legacy_authority(authority: str) -> bool:
    """判断明确的 host:port:user:password 旧式 authority。"""
    if str(authority or "").startswith("["):
        return False
    fields = str(authority or "").split(":", 3)
    return len(fields) == 4 and bool(fields[0]) and _valid_proxy_port_text(fields[1]) and bool(fields[2])


def _looks_like_legacy_authority(authority: str) -> bool:
    """判断旧式 host:port:user:password 或 user:password:host:port。"""
    authority = str(authority or "")
    if authority.startswith("["):
        close = authority.find("]")
        if close <= 1:
            return False
        suffix = authority[close + 1 :]
        if not suffix.startswith(":"):
            return False
        fields = suffix[1:].split(":", 2)
        return len(fields) == 3 and _valid_proxy_port_text(fields[0]) and bool(fields[1])
    if _looks_like_forward_legacy_authority(authority):
        return True
    # 另一种常见格式是 user:password:host:port；当第二段不像端口、
    # 最后一段是端口时按该格式解释，避免把 @/空格密码拆坏。
    fields = authority.split(":", 3)
    return len(fields) == 4 and bool(fields[0]) and bool(fields[1]) and bool(fields[2]) and _valid_proxy_port_text(fields[3])


def _parse_proxy_endpoint(
    endpoint: str,
    *,
    allow_legacy_auth: bool,
    allow_missing_port: bool = False,
) -> tuple[str, str | None, str | None, str | None]:
    """解析 host[:port]，及可选的旧式 user/password 后缀。"""
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        raise _proxy_format_error("缺少 host/port")

    if endpoint.startswith("["):
        close = endpoint.find("]")
        if close < 0:
            raise _proxy_format_error("IPv6 host 缺少右方括号")
        host = endpoint[1:close]
        suffix = endpoint[close + 1 :]
        if suffix and not suffix.startswith(":"):
            raise _proxy_format_error("缺少 port")
        fields = suffix[1:].split(":", 2) if suffix else []
    else:
        fields = endpoint.split(":", 3)
        if len(fields) < 2:
            if not allow_missing_port:
                raise _proxy_format_error("格式应为 host:port 或 host:port:user:password")
            host, fields = endpoint, []
        else:
            host = fields.pop(0)

    if not fields:
        if not allow_missing_port:
            raise _proxy_format_error("缺少 port")
        port = None
    elif not fields[0]:
        raise _proxy_format_error("port 不能为空")
    else:
        port = _normalise_proxy_port(fields.pop(0))
    if not allow_legacy_auth and fields:
        raise _proxy_format_error("host/port 后存在重复认证信息")
    if allow_legacy_auth and fields and len(fields) != 2:
        raise _proxy_format_error("旧式代理格式必须是 host:port:user:password")
    if len(fields) > 2:
        raise _proxy_format_error("认证信息最多包含 username/password")
    username = fields[0] if fields else None
    password = fields[1] if len(fields) > 1 else None
    if username == "":
        raise _proxy_format_error("username 不能为空")
    return (
        _normalise_proxy_host(host),
        port,
        username,
        password,
    )


def _parse_proxy_authority(
    authority: str,
    *,
    legacy: bool,
) -> tuple[str, str | None, str | None, str | None]:
    """解析标准 authority 和代理商常见的 host:port:user:password。"""
    authority = str(authority or "").strip()
    if not authority:
        raise _proxy_format_error("缺少 host/port")

    if not legacy and "@" in authority:
        userinfo, endpoint = authority.rsplit("@", 1)
        username, separator, password = userinfo.partition(":")
        if not separator:
            password = None
        if not username:
            raise _proxy_format_error("username 不能为空")
        host, port, endpoint_user, endpoint_password = _parse_proxy_endpoint(
            endpoint, allow_legacy_auth=False, allow_missing_port=True,
        )
        if endpoint_user is not None or endpoint_password is not None:
            raise _proxy_format_error("host/port 后存在重复认证信息")
        return host, port, username, password

    if legacy and not authority.startswith("["):
        reverse = authority.split(":", 3)
        if (
            len(reverse) == 4
            and bool(reverse[0])
            and bool(reverse[1])
            and bool(reverse[2])
            and re.fullmatch(r"[0-9]+", reverse[3] or "")
            # Prefer the unambiguous host:port:user:password form. Otherwise
            # values such as host:8080:user:1234 would be incorrectly reversed.
            and not _looks_like_forward_legacy_authority(authority)
        ):
            # Also accept user:password:host:port, used by some proxy vendors.
            host, port, _, _ = _parse_proxy_endpoint(
                f"{reverse[2]}:{reverse[3]}",
                allow_legacy_auth=False,
                allow_missing_port=False,
            )
            return host, port, reverse[0], reverse[1]
    return _parse_proxy_endpoint(
        authority,
        allow_legacy_auth=legacy,
        allow_missing_port=not legacy,
    )


def normalize_proxy_url(value: str | None, default_scheme: str = "http") -> str:
    """把代理配置归一化成 curl/requests 可接受的 URL。

    除标准 ``scheme://[user:password@]host:port`` 外，也兼容代理商常用的
    ``host:port:user:password``（默认按 HTTP 代理处理）。显式带 scheme 但仍
    使用四段 authority 的旧值也会被修正，例如 ``socks5://host:port:user:pass``。
    """
    text = str(value or "").strip()
    if not text:
        return ""

    match = _PROXY_SCHEME_RE.match(text)
    if match:
        scheme = match.group(1).lower()
        if scheme not in _PROXY_SCHEMES:
            raise _proxy_format_error(f"不支持的协议 {scheme!r}")
        try:
            parsed = urlsplit(text)
        except ValueError as exc:
            raise _proxy_format_error("URL authority 无效") from exc
        if not parsed.netloc:
            raise _proxy_format_error("缺少 host/port")
        if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise _proxy_format_error("代理 URL 不应包含 path/query/fragment")
        authority = parsed.netloc
    else:
        scheme = str(default_scheme or "http").strip().lower() or "http"
        authority = text

    endpoint = authority.rsplit("@", 1)[-1]
    # A final ``@host[:port]`` is unambiguous URL userinfo, even when an
    # earlier user/password component happens to look like host:port.  This
    # prevents credentials such as ``user:8080:foo@host:8080`` from being
    # mistaken for the legacy ``host:port:user:password`` form.
    if "@" in authority and (
        match is not None
        or _endpoint_has_explicit_port(endpoint)
        or (
            # A scheme-less ``user:password@host:port`` is standard URL
            # userinfo.  If the suffix has no explicit port, let legacy
            # parsing handle the ambiguous ``host:port:user:p@ss`` form.
            _endpoint_looks_standard(endpoint)
            and not _looks_like_forward_legacy_authority(authority)
        )
    ):
        legacy = False
    else:
        # Legacy passwords may contain an unescaped '@'; in that case the
        # suffix is not a standard endpoint and the old parser remains the
        # appropriate fallback.
        legacy = _looks_like_legacy_authority(authority) or not _endpoint_looks_standard(endpoint)
    host, port, username, password = _parse_proxy_authority(authority, legacy=legacy)
    auth = ""
    if username is not None or password is not None:
        auth = _quote_proxy_credential(username)
        if password is not None:
            auth += ":" + _quote_proxy_credential(password)
        auth += "@"
    port_suffix = f":{port}" if port is not None else ""
    return f"{scheme}://{auth}{host}{port_suffix}"


def redact_proxy_url(value: str | None) -> str:
    """返回代理 URL 的安全摘要，绝不输出 username/password。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        canonical = normalize_proxy_url(text)
        parsed = urlsplit(canonical)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        auth = "***:***@" if parsed.username is not None or parsed.password is not None else ""
        return f"{parsed.scheme}://{auth}{host}{port}"
    except Exception:
        # 配置错误也不能把原始凭据带入日志或错误响应。
        return "configured-proxy"


# 本地代理入口；实际出口地区以代理/分流规则为准。
# 推荐使用 socks5h://（DNS 在代理端解析），避免本地 DNS 与出口 IP 地区错配。
PROXY_POOL = []

# 代理池使用的本地上游代理。填写后形成：本地上游 -> 代理池目标代理 -> ChatGPT；
# 留空则直接使用代理池中的目标代理，不启动链式中继。
PROXY_POOL_UPSTREAM_PROXY = ""

# 套餐/Plus 试用资格查询与 Codex Agent Token 生成共用这组独立网络策略，
# 避免批量请求被注册代理池中的临时本地代理拖垮，也避免无条件直连造成出口策略失控。
#   auto   = 优先使用 PLAN_CHECK_PROXY 或代理池；没有代理时才按 direct 运行
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或代理池，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Codex Agent Token 生成专用代理。留空时 auto/proxy 模式从 PROXY_POOL 选择。
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY = []

# 套餐查询 / Codex Agent Token 生成的上游代理。填写后形成：本地代理 -> 动态代理 -> ChatGPT。
# 例如 http://127.0.0.1:7897；留空则直接连接 PLAN_CHECK_PROXY。
PLAN_CHECK_UPSTREAM_PROXY = ""

# 查套餐 / 生成 Codex Agent Token 使用独立的短超时和有限重试，避免后台任务长时间卡住。
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 3
PLAN_CHECK_RETRY_DELAY = 2.0

# 新注册账号的权益可能存在短暂同步延迟。首次查询失败，或返回 free 且暂未发现
# Plus 试用资格时，等待该秒数后再复查一次；设为 0 可关闭复查。
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0

# 自动、手动和批量套餐查询共用同一个后台队列；Codex Agent Token 使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 3
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 1.0
PLAN_CHECK_JITTER = 0.8



def normalize_proxy_list(values, default_scheme: str = "http") -> list[str]:
    """Normalize a multiline proxy list and omit empty entries."""
    if values is None:
        return []
    if isinstance(values, str):
        values = values.splitlines()
    return [
        normalized
        for value in values
        if (normalized := normalize_proxy_url(value, default_scheme=default_scheme))
    ]


def pick_proxy() -> str:
    """随机抽取并归一化一个代理 URL；池为空时返回空串（即不使用代理）。"""
    if not PROXY_POOL:
        return ""
    raw = random.choice(PROXY_POOL)
    try:
        return normalize_proxy_url(raw)
    except ValueError as exc:
        # 不把带凭据的原始代理写入日志；配置错误应尽早暴露而不是让 curl/Chromium
        # 在后面返回难以诊断的 Unsupported proxy syntax。
        raise ValueError(f"代理池条目无效: {redact_proxy_url(raw)} ({exc})") from exc


# 兼容入口：默认每次进程启动随机选一个，作为本次注册全程的固定代理
PROXY = pick_proxy()

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'PROXY_POOL_UPSTREAM_PROXY': 'str',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'list_str_multiline',
    'PLAN_CHECK_UPSTREAM_PROXY': 'str',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
})
PROXY_POOL = normalize_proxy_list(PROXY_POOL)
PLAN_CHECK_PROXY = normalize_proxy_list(PLAN_CHECK_PROXY)
PROXY = pick_proxy()
