# -*- coding: utf-8 -*-
"""RoxyBrowser 本地 API 客户端。"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import requests

from config import roxybrowser as _cfg
from config.proxy import normalize_proxy_url, redact_proxy_url

logger = logging.getLogger(__name__)

# Roxy 的 /browser/create 在多 worker 同时到达时可能返回“正在创建中”。
# 为所有客户端实例共享创建时隙，确保进程内请求起始时间至少错开配置的间隔。
_CREATE_SLOT_LOCK = threading.Lock()
_NEXT_CREATE_SLOT = 0.0
# 创建 Profile 到拿到调试地址期间串行化，避免 Roxy 内核/端口竞争。
_ROXY_WINDOW_CREATE_LOCK = threading.Lock()


def _wait_for_create_slot() -> None:
    global _NEXT_CREATE_SLOT

    interval = max(0.0, float(getattr(_cfg, "ROXY_CREATE_INTERVAL", 1.5) or 0.0))
    if interval <= 0:
        return

    with _CREATE_SLOT_LOCK:
        now = time.monotonic()
        wait_for = max(0.0, _NEXT_CREATE_SLOT - now)
        _NEXT_CREATE_SLOT = max(now, _NEXT_CREATE_SLOT) + interval

    if wait_for > 0:
        logger.info("[Roxy] /browser/create 请求错峰，等待 %.2fs", wait_for)
        time.sleep(wait_for)


@dataclass
class RoxyOpenResult:
    profile_id: str
    raw: dict
    debugger_address: str | None = None
    webdriver_url: str | None = None
    ws_endpoint: str | None = None
    created_by_run: bool = False


def _strip_slashes(value: str) -> str:
    return str(value or "").strip().strip("/")


def _join_url(base: str, path: str) -> str:
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _mask_proxy(proxy_url: str) -> str:
    return redact_proxy_url(proxy_url) or "direct"


_PROXY_URL_RE = re.compile(
    r"(?P<url>(?:(?:https?|wss?|socks(?:4a?|5h?)?)://)[^\s\"'<>]+)",
    re.IGNORECASE,
)
_URL_SENSITIVE_KEYS = frozenset({
    "access_token", "access-token", "auth", "authorization", "code", "credential",
    "id_token", "id-token", "jwt", "login_hint", "password", "refresh_token",
    "refresh-token", "secret", "session", "session_id", "session-id", "state",
    "token", "token_type", "token-type",
})
_URL_SENSITIVE_KEY_RE = re.compile(
    r"(?:access[_-]?token|auth(?:orization)?|code|credential|id[_-]?token|jwt|"
    r"login[_-]?hint|password|refresh[_-]?token|secret|session(?:[_-]?id)?|state|token(?:[_-]?type)?)",
    re.IGNORECASE,
)
_URL_PUNCTUATION = ",.;)]}>'" + '"'


def _redact_url(value: str) -> str:
    """Redact credentials and token-like query/fragment values in a URL.

    Debugger/WebDriver/WS URLs commonly carry an opaque token in either a query
    parameter or a path segment.  Keep the scheme/host/port useful for diagnosis,
    but never persist or log the opaque values.
    """
    text = str(value or "")
    if not text:
        return text
    trailing = ""
    while text and text[-1] in _URL_PUNCTUATION:
        trailing = text[-1] + trailing
        text = text[:-1]
    try:
        parsed = urlsplit(text)
    except Exception:
        return "***" if text else text
    if not parsed.scheme or not parsed.netloc:
        return text + trailing
    try:
        # urlsplit.username/password can themselves expose credentials in a
        # webdriver command executor URL; preserve only the endpoint.
        hostname = parsed.hostname or ""
        host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
        port = f":{parsed.port}" if parsed.port is not None else ""
        netloc = f"{host}{port}"
    except Exception:
        netloc = parsed.netloc.split("@", 1)[-1]
    query_parts = []
    try:
        for key, item in parse_qsl(parsed.query, keep_blank_values=True):
            if _URL_SENSITIVE_KEY_RE.search(str(key)):
                item = "***"
            query_parts.append((key, item))
        query = urlencode(query_parts, doseq=True).replace("%2A%2A%2A", "***")
    except Exception:
        query = "" if parsed.query else parsed.query
    fragment = "***" if parsed.fragment else ""
    # WebSocket/debugger/WebDriver endpoints sometimes use an opaque path token
    # rather than query parameters (for example /devtools/browser/<uuid> or
    # /wd/hub/session/<session-id>).  Keep route names but mask the identifier.
    path = parsed.path
    pieces = path.split("/")
    lower_pieces = [item.lower() for item in pieces]
    mask_indices = set()
    for index, piece in enumerate(lower_pieces):
        # Chrome DevTools websocket: /devtools/browser/<opaque-id>.
        if piece in {"devtools", "debugger"}:
            if index + 2 < len(lower_pieces) and lower_pieces[index + 1] in {"browser", "page", "worker", "frame"}:
                mask_indices.add(index + 2)
            elif index + 1 < len(lower_pieces) and lower_pieces[index + 1] not in {"browser", "page", "worker", "frame"}:
                mask_indices.add(index + 1)
        # Selenium command executor: /wd/hub/session/<opaque-id>.
        if piece == "wd" and index + 3 < len(lower_pieces) and lower_pieces[index + 1:index + 3] == ["hub", "session"]:
            mask_indices.add(index + 3)
        elif piece in {"webdriver", "session"} and index + 1 < len(lower_pieces):
            mask_indices.add(index + 1)
    for index in mask_indices:
        if pieces[index]:
            pieces[index] = "***"
    if (
        parsed.scheme.lower() in {"ws", "wss"}
        and any(item == "devtools" for item in lower_pieces)
        and pieces
        and pieces[-1]
        and pieces[-1] != "***"
    ):
        pieces[-1] = "***"
    path = "/".join(pieces)
    return urlunsplit((parsed.scheme, netloc, path, query, fragment)) + trailing


def _redact_text(value) -> str:
    """Redact proxy URLs, tokenized URLs, and JSON credential fields."""
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(_redact_sensitive(value), ensure_ascii=False)
        except Exception:
            pass
    text = str(value or "")
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if parsed is not None:
        try:
            return json.dumps(_redact_sensitive(parsed), ensure_ascii=False)
        except Exception:
            pass

    def replace(match):
        raw = match.group("url")
        stripped = raw.rstrip(_URL_PUNCTUATION)
        trailing = raw[len(stripped):]
        if re.match(r"socks(?:4a?|5h?)://", stripped, re.IGNORECASE):
            return _mask_proxy(stripped) + trailing
        return _redact_url(stripped) + trailing

    return _PROXY_URL_RE.sub(replace, text)


def _redact_sensitive(value, *, _key: str = "", _proxy_context: bool = False):
    """Return a log/persistence-safe copy without proxy credentials or secrets.

    Roxy responses and request bodies are provider-controlled dictionaries, so do
    not assume a fixed nesting shape. Any proxy URL is normalized through the
    shared redactor; credential-bearing fields are replaced before logging or
    returning data to account persistence.
    """
    key = str(_key or "").lower()
    proxy_key = any(token in key for token in (
        "proxy", "proxyinfo", "proxy_info", "proxyurl", "proxy_url",
        "proxypooltarget", "proxy_pool_target", "transport",
    ))
    proxy_context = _proxy_context or proxy_key
    sensitive_key = any(token in key for token in (
        "password", "passwd", "secret", "token", "credential", "authorization", "auth",
    )) or (proxy_context and any(token in key for token in (
        "username", "user_name", "userid", "user_id", "account",
    )))
    # A sensitive key may contain a nested object/list (for example
    # {"token": {"value": "..."}}); mask the complete value before walking it.
    if sensitive_key:
        return "***"
    if isinstance(value, dict):
        safe = {}
        for raw_key, raw_value in value.items():
            normalized_key = str(raw_key).lower()
            # Keep a credential-free proxy-pool route for audit/persistence.
            # Never retain the raw username/password-bearing URL.
            if normalized_key in {"proxy_pool_target", "proxypooltarget"}:
                safe[str(raw_key)] = _safe_proxy_target(raw_value) if raw_value else ""
            else:
                safe[str(raw_key)] = _redact_sensitive(
                    raw_value,
                    _key=str(raw_key),
                    _proxy_context=proxy_context,
                )
        return safe
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(item, _key=key, _proxy_context=proxy_context) for item in value]
    if isinstance(value, str):
        proxy_scalar_key = (
            key in {"proxy", "url", "proxyurl", "proxy_url", "target", "raw", "upstream"}
            or key.endswith("_url")
            or key.endswith("url")
            or key.endswith("_target")
            or key.endswith("target")
        )
        if proxy_context and proxy_scalar_key:
            # This also handles legacy host:port:user:password proxy strings,
            # which do not contain ``://`` but are accepted by normalize_proxy_url.
            return _mask_proxy(value)
        if proxy_key and "://" in value:
            return _mask_proxy(value)
        if "://" in value:
            return _redact_text(value)
    return value


def _safe_proxy_target(value: str | None) -> str:
    """Persist only a credential-free proxy route, never raw pool credentials."""
    return _mask_proxy(value) if value else ""


def _proxy_url_to_roxy_info(proxy_url: str) -> dict:
    """
    将 config/proxy.py 里的代理 URL 转成 Roxy /browser/create 的 proxyInfo。

    支持：
      http://user:pass@host:port
      https://user:pass@host:port
      socks5://user:pass@host:port
      socks5h://user:pass@host:port  -> Roxy 侧按 SOCKS5 处理
    """
    text = str(proxy_url or "").strip()
    if not text:
        raise ValueError("代理为空")
    text = normalize_proxy_url(text)
    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https", "socks4", "socks4a", "socks5", "socks5h"):
        raise ValueError(f"Roxy 暂不支持该代理协议: {scheme or '-'}")
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"代理格式缺少 host/port: {_mask_proxy(text)}")

    # Roxy exposes SOCKS4 as one protocol category.  SOCKS4A differs only in
    # where the destination hostname is resolved, which is not separately
    # representable in the Roxy create payload; map both to SOCKS4 rather than
    # rejecting otherwise valid proxy-pool entries.
    protocol = {
        "http": "HTTP",
        "https": "HTTPS",
        "socks4": "SOCKS4",
        "socks4a": "SOCKS4",
        "socks5": "SOCKS5",
        "socks5h": "SOCKS5",
    }[scheme]
    # Roxy /browser/create 官方字段是：
    # proxyMethod / proxyCategory / ipType / protocol / host / port / proxyUserName / proxyPassword / checkChannel
    # 之前误用了 proxyType/proxyHost/proxyPort/proxyAccount，Roxy 会忽略，导致创建窗口实际未设置代理。
    info = {
        "moduleId": 0,
        "proxyMethod": "custom",
        "proxyCategory": protocol,
        "ipType": "IPV6" if ":" in str(parsed.hostname or "") else "IPV4",
        "protocol": protocol,
        "host": parsed.hostname,
        "port": str(parsed.port),
    }
    if parsed.username:
        info["proxyUserName"] = unquote(parsed.username)
    if parsed.password:
        info["proxyPassword"] = unquote(parsed.password)
    check_channel = str(getattr(_cfg, "ROXY_PROXY_CHECK_CHANNEL", "") or "").strip()
    if check_channel:
        info["checkChannel"] = check_channel
    return info


def _dig(payload: dict, *keys: str):
    cur = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _first(payload: dict, paths: list[tuple[str, ...]]) -> str:
    for path in paths:
        value = _dig(payload, *path)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _workspace_id_value() -> str | int:
    raw = str(getattr(_cfg, "ROXY_WORKSPACE_ID", "") or "").strip()
    if not raw:
        return ""
    return int(raw) if raw.isdigit() else raw


def _project_id_value() -> str | int:
    raw = str(getattr(_cfg, "ROXY_PROJECT_ID", "") or "").strip()
    if not raw:
        return ""
    return int(raw) if raw.isdigit() else raw


def _apply_data_saver_open_args(params: dict) -> dict:
    """在 Roxy 启动参数中尽早关闭图片加载，覆盖无扩展名图片 URL。

    Network.setBlockedURLs 只能按 URL 后缀拦截，而 Roxy 浏览器在 Selenium 连接
    前就已经启动；使用 Chromium 开关可以让图片在首个页面请求前就被禁用。该开关
    只在用户明确开启省流量模式且包含 image 类型时追加。
    """
    try:
        from config import browser as _browser_cfg

        if not bool(getattr(_browser_cfg, "BROWSER_DATA_SAVER_MODE", False)):
            return params
        raw_types = getattr(_browser_cfg, "BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", [])
        if isinstance(raw_types, str):
            types = {item.strip().lower() for item in raw_types.replace(",", "\n").splitlines() if item.strip()}
        else:
            types = {str(item or "").strip().lower() for item in (raw_types or []) if str(item or "").strip()}
        if "image" not in types and "images" not in types and "img" not in types:
            return params

        current = params.get("args")
        if isinstance(current, (list, tuple)):
            args = list(current)
        elif current:
            args = [str(current)]
        else:
            args = []
        switch = "--blink-settings=imagesEnabled=false"
        if switch not in args:
            args.append(switch)
        params["args"] = args
    except Exception as exc:
        logger.debug("[Roxy] 添加省流量图片启动参数失败，继续使用原参数：%s", exc)
    return params


def _random_roxy_os() -> str:
    raw = str(getattr(_cfg, "ROXY_RANDOM_OS_CHOICES", "Windows,macOS") or "Windows,macOS")
    choices = [
        x.strip()
        for part in raw.replace("\n", ",").replace(";", ",").split(",")
        for x in [part]
        if x.strip()
    ]
    valid = {"Windows", "macOS", "Linux", "IOS", "Android"}
    choices = [x for x in choices if x in valid]
    if not choices:
        choices = ["Windows", "macOS"]
    return random.choice(choices)


def _random_roxy_profile_name() -> str:
    prefix = str(getattr(_cfg, "ROXY_PROFILE_NAME_PREFIX", "rb") or "rb").strip() or "rb"
    # Roxy 环境名每次创建都不同：前缀 + 毫秒时间戳 + 随机 4 位十六进制。
    return f"{prefix}-{int(time.time() * 1000)}-{random.randrange(0x10000):04x}"


class RoxyBrowserClient:
    def __init__(self, api_base: str | None = None, token: str | None = None):
        self.api_base = (api_base or _cfg.ROXY_API_BASE).strip()
        self.token = (token if token is not None else _cfg.ROXY_API_TOKEN).strip()
        self._proxy_pool_relay = None
        self._proxy_pool_target = ""
        self.http = requests.Session()
        if self.token:
            # 官方文档要求所有接口请求头必须加 token。这里同时兼容 token / Authorization。
            self.http.headers.update({
                "token": self.token,
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            })

    def close(self, *, close_relay: bool = True) -> None:
        """Release the HTTP session and, by default, the proxy-pool relay.

        The client is used around several setup/teardown failure paths.  Make
        this operation idempotent and ensure a relay exception cannot prevent
        the requests session from being closed.  ``close_relay=False`` is only
        for the intentional ``KEEP_BROWSER_OPEN`` debugging mode; the default
        (and context-manager behavior) always releases both resources.
        """
        if close_relay:
            try:
                self.close_proxy_pool_relay()
            except Exception as exc:
                logger.debug("[Roxy] 关闭代理链失败，继续关闭 HTTP session：%s", _redact_text(exc))
        http, self.http = getattr(self, "http", None), None
        if http is not None:
            try:
                http.close()
            except Exception as exc:
                logger.debug("[Roxy] 关闭 HTTP session 失败：%s", _redact_text(exc))

    def __enter__(self) -> "RoxyBrowserClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        text = str(exc or "").lower()
        return (
            "timeout" in text
            or "timed out" in text
            or "connection" in text
            or "temporarily" in text
            or "http 500" in text
            or "http 502" in text
            or "http 503" in text
            or "http 504" in text
            or "http 429" in text
        )

    def request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> dict:
        url = _join_url(self.api_base, path)
        method_u = method.upper()
        is_create = str(path or "").rstrip("/").endswith("/create") or "browser/create" in str(path or "")
        if is_create:
            # /browser/create is side-effectful: a timeout can still mean the
            # profile was created remotely.  Default to one attempt and only
            # retry when explicitly configured by the operator.
            max_attempts = max(1, int(getattr(_cfg, "ROXY_CREATE_RETRIES", 1) or 1))
            base_delay = max(0.5, float(getattr(_cfg, "ROXY_CREATE_RETRY_DELAY", 3) or 3))
        else:
            max_attempts = max(1, int(getattr(_cfg, "ROXY_API_RETRIES", 3) or 3))
            base_delay = max(0.5, float(getattr(_cfg, "ROXY_API_RETRY_DELAY", 2) or 2))
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                logger.debug(
                    "[Roxy] %s %s params=%s body=%s attempt=%s/%s",
                    method, _redact_text(url), _redact_sensitive(params), _redact_sensitive(json_body), attempt, max_attempts,
                )
                resp = self.http.request(
                    method_u,
                    url,
                    params=params or None,
                    json=json_body if json_body is not None else None,
                    timeout=max(5, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
                )
                text = resp.text or ""
                try:
                    payload = resp.json()
                except Exception:
                    payload = {"raw": text}
                if not (200 <= resp.status_code < 300):
                    raise RuntimeError(
                        f"Roxy API 请求失败 {method_u} {_redact_text(path)} HTTP {resp.status_code}: "
                        f"{_redact_text(text)[:500]}"
                    )
                if isinstance(payload, dict):
                    code = payload.get("code")
                    ok = payload.get("ok")
                    success = payload.get("success")
                    if code not in (None, 0, 200, "0", "200") and ok is not True and success is not True:
                        msg = payload.get("msg") or payload.get("message") or payload.get("error") or _redact_sensitive(payload)
                        raise RuntimeError(f"Roxy API 返回失败 {method_u} {_redact_text(path)}: {_redact_text(msg)}")
                if attempt > 1:
                    logger.info("[Roxy] API 重试成功：%s %s attempt=%s/%s", method_u, _redact_text(path), attempt, max_attempts)
                return payload if isinstance(payload, dict) else {"data": payload}
            except Exception as exc:
                last_exc = exc
                retryable = self._is_retryable_error(exc)
                if attempt >= max_attempts or not retryable:
                    raise
                delay = base_delay * attempt
                logger.warning(
                    "[Roxy] API 请求失败，将在 %.1fs 后使用相同%s重试：%s %s attempt=%s/%s error=%s",
                    delay,
                    " create payload/name " if is_create else "请求参数",
                    method_u, _redact_text(path), attempt, max_attempts, _redact_text(exc),
                )
                time.sleep(delay)
        raise last_exc or RuntimeError(f"Roxy API 请求失败 {method_u} {_redact_text(path)}")

    def try_request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> tuple[bool, dict | str]:
        """宽松请求：用于探测不同 Roxy 版本接口，失败不抛出。"""
        try:
            return True, self.request(method, path, params=params, json_body=json_body)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {_redact_text(exc)}"

    @staticmethod
    def _extract_workspace_items(payload: dict) -> list[dict]:
        """解析 /browser/workspace：团队 rows + project_details 项目列表；兼容递归兜底。"""
        out = []

        # 官方结构：data.rows[].id/workspaceName/project_details[].projectId/projectName
        rows = None
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                rows = data.get("rows") or data.get("list") or data.get("records")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                wid = row.get("id") or row.get("workspaceId") or row.get("workspace_id")
                wname = row.get("workspaceName") or row.get("workspace_name") or row.get("name") or str(wid or "")
                projects = row.get("project_details") or row.get("projectDetails") or row.get("projects") or []
                if isinstance(projects, list) and projects:
                    for proj in projects:
                        if not isinstance(proj, dict):
                            continue
                        pid = proj.get("projectId") or proj.get("project_id") or proj.get("id")
                        pname = proj.get("projectName") or proj.get("project_name") or proj.get("name") or str(pid or "")
                        if wid:
                            out.append({
                                "id": str(wid),
                                "name": str(wname),
                                "projectId": str(pid or ""),
                                "projectName": str(pname or ""),
                                "label": f"{wname} / {pname} ({wid}/{pid})" if pid else f"{wname} ({wid})",
                                "raw": {"workspace": row, "project": proj},
                            })
                elif wid:
                    out.append({
                        "id": str(wid),
                        "name": str(wname),
                        "projectId": "",
                        "projectName": "",
                        "label": f"{wname} ({wid})",
                        "raw": row,
                    })

        if out:
            return out

        # 兜底：递归抽 workspace/team/company 结构。
        def pick_id_name(item: dict) -> tuple[str, str]:
            wid = _first(item, [
                ("workspaceId",), ("workspace_id",), ("workspaceID",),
                ("teamId",), ("team_id",), ("teamID",),
                ("companyId",), ("company_id",), ("orgId",), ("org_id",),
                ("id",), ("value",), ("key",),
            ])
            name = _first(item, [
                ("workspaceName",), ("workspace_name",),
                ("teamName",), ("team_name",),
                ("companyName",), ("company_name",),
                ("orgName",), ("org_name",),
                ("name",), ("label",), ("title",), ("remark",),
            ])
            return wid, name

        def looks_like_workspace(item: dict) -> bool:
            keys = {str(k).lower() for k in item.keys()}
            joined = " ".join(keys)
            return any(x in joined for x in ("workspace", "team", "company", "org")) or ("id" in keys and "name" in keys)

        def walk(node):
            if isinstance(node, dict):
                wid, name = pick_id_name(node)
                if wid and looks_like_workspace(node):
                    out.append({"id": wid, "name": name or wid, "projectId": "", "projectName": "", "label": f"{name or wid} ({wid})", "raw": node})
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)
        dedup = {}
        for item in out:
            raw_keys = {str(k).lower() for k in (item.get("raw") or {}).keys()}
            if "dirid" in raw_keys and not any(k in raw_keys for k in ("workspaceid", "teamid", "companyid")):
                continue
            key = f"{item.get('id')}::{item.get('projectId','')}"
            dedup[key] = item
        return list(dedup.values())

    def list_workspaces(self) -> dict:
        """
        获取 Roxy 团队/工作区列表。
        Roxy 不同版本路径可能有差异，因此先试配置路径，再试常见路径。
        """
        configured = str(getattr(_cfg, "ROXY_WORKSPACE_LIST_PATH", "") or "").strip()
        method = str(getattr(_cfg, "ROXY_WORKSPACE_LIST_METHOD", "GET") or "GET").upper()
        candidates = []
        if configured:
            candidates.append((method, configured))
        candidates.extend([
            ("GET", "/browser/workspace"),
            ("POST", "/browser/workspace"),
            ("GET", "/workspace/list"),
            ("POST", "/workspace/list"),
            ("GET", "/workspace"),
            ("POST", "/workspace"),
            ("GET", "/team/list"),
            ("POST", "/team/list"),
            ("GET", "/team"),
            ("POST", "/team"),
            ("GET", "/workspaces"),
            ("GET", "/teams"),
            ("GET", "/user/workspace/list"),
            ("POST", "/user/workspace/list"),
            ("GET", "/user/team/list"),
            ("POST", "/user/team/list"),
            ("GET", "/api/workspace/list"),
            ("POST", "/api/workspace/list"),
            ("GET", "/api/team/list"),
            ("POST", "/api/team/list"),
            ("GET", "/browser/workspace/list"),
            ("POST", "/browser/workspace/list"),
            ("GET", "/browser/team/list"),
            ("POST", "/browser/team/list"),
        ])

        errors = []
        seen = set()
        for m, path in candidates:
            key = (m, path)
            if key in seen:
                continue
            seen.add(key)
            ok, payload = self.try_request(m, path)
            if not ok:
                errors.append({"method": m, "path": path, "error": payload})
                continue
            items = self._extract_workspace_items(payload if isinstance(payload, dict) else {})
            if items:
                return {"ok": True, "path": path, "method": m, "items": items, "raw": payload}
            errors.append({"method": m, "path": path, "error": "响应中未解析到团队/工作区列表", "payload": payload})

        return {"ok": False, "items": [], "errors": errors}

    def create_profile(self, payload: dict | None = None) -> str:
        body = dict(getattr(_cfg, "ROXY_PROFILE_CREATE_PAYLOAD", {}) or {})
        if payload:
            body.update(payload)
        random_name_enabled = bool(getattr(_cfg, "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", True))
        if random_name_enabled:
            # 覆盖 ROXY_PROFILE_CREATE_PAYLOAD 里的固定 name，避免所有 Roxy 窗口同名。
            body["name"] = _random_roxy_profile_name()
        random_os_enabled = bool(getattr(_cfg, "ROXY_RANDOM_OS_ON_CREATE", True))
        if random_os_enabled:
            # 每次创建环境随机 Windows / macOS；覆盖 ROXY_PROFILE_CREATE_PAYLOAD 里的固定 os。
            body["os"] = _random_roxy_os()
            # osVersion 跟 os 强绑定，随机 OS 时不沿用固定版本，避免 macOS 版本传给 Windows。
            body.pop("osVersion", None)
        else:
            default_os = str(getattr(_cfg, "ROXY_DEFAULT_OS", "macOS") or "macOS").strip()
            if default_os:
                # Roxy 官方枚举大小写敏感：Windows / macOS / Linux / IOS / Android。
                body.setdefault("os", default_os)
            default_os_version = str(getattr(_cfg, "ROXY_DEFAULT_OS_VERSION", "") or "").strip()
            if default_os_version:
                body.setdefault("osVersion", default_os_version)
        workspace_id = _workspace_id_value()
        if workspace_id:
            # Roxy 官方 /browser/create 要求 workspaceId。
            body.setdefault("workspaceId", workspace_id)
        if not body.get("workspaceId"):
            raise RuntimeError(
                "Roxy 创建环境需要 workspaceId。请在 config/roxybrowser.py 或 WebUI 的 RoxyBrowser 配置中填写 ROXY_WORKSPACE_ID，"
                "或直接在 ROXY_PROFILE_CREATE_PAYLOAD 里加入 {'workspaceId': '你的工作区ID'}。"
            )
        project_id = _project_id_value()
        if project_id:
            body.setdefault("projectId", project_id)
        if bool(getattr(_cfg, "ROXY_CREATE_USE_PROXY_POOL", False)) and not body.get("proxyInfo"):
            from config import proxy as _proxy_cfg
            from core.proxy_chain import open_proxy_pool_proxy

            target_proxy = _proxy_cfg.pick_proxy()
            try:
                proxy_url, relay = open_proxy_pool_proxy(target_proxy)
                self._proxy_pool_relay = relay
                self._proxy_pool_target = str(target_proxy or "").strip()
                if proxy_url:
                    proxy_info = _proxy_url_to_roxy_info(proxy_url)
                    body["proxyInfo"] = proxy_info
                    logger.info(
                        "[Roxy] 创建环境启用代理池：target=%s transport=%s type=%s host=%s port=%s",
                        _mask_proxy(target_proxy), _mask_proxy(proxy_url),
                        proxy_info.get("protocol") or proxy_info.get("proxyCategory"),
                        proxy_info.get("host"),
                        proxy_info.get("port"),
                    )
                else:
                    logger.warning("[Roxy] 已启用 ROXY_CREATE_USE_PROXY_POOL，但 PROXY_POOL 为空，本次创建环境不设置代理")
            except Exception:
                self.close_proxy_pool_relay()
                raise
        logger.info(
            "[Roxy] 创建环境参数：workspaceId=%s projectId=%s name=%s random_name=%s os=%s osVersion=%s random_os=%s",
            body.get("workspaceId"),
            body.get("projectId") or "-",
            body.get("name") or "-",
            random_name_enabled,
            body.get("os") or "-",
            body.get("osVersion") or "-",
            random_os_enabled,
        )
        _wait_for_create_slot()
        try:
            result = self.request(_cfg.ROXY_CREATE_METHOD, _cfg.ROXY_CREATE_PATH, json_body=body)
            profile_id = _first(result, [
                ("id",), ("dirId",), ("dir_id",), ("profile_id",), ("profileId",), ("browser_id",),
                ("data", "id"), ("data", "dirId"), ("data", "dir_id"),
                ("data", "profile_id"), ("data", "profileId"), ("data", "browser_id"),
            ])
            if not profile_id:
                raise RuntimeError(
                    f"Roxy 创建环境成功但未返回 dirId/profile_id: {_redact_sensitive(result)}"
                )
            return profile_id
        except Exception:
            # A failed create/open setup must not leave its local relay alive.
            # The remote profile id is unknown on a failed create response, so
            # profile deletion is handled by open_profile once an id is known.
            self.close_proxy_pool_relay()
            raise

    @staticmethod
    def _normalize_profile_id(value: str | None) -> str:
        text = str(value or "").strip()
        # WebUI/人工配置里常用 - 表示“未配置”，这里统一按空处理。
        if text in ("-", "—", "无", "空", "none", "None", "null", "NULL"):
            return ""
        return text

    def open_profile(self, profile_id: str | None = None) -> RoxyOpenResult:
        with _ROXY_WINDOW_CREATE_LOCK:
            return self._open_profile_locked(profile_id)

    def _open_profile_locked(self, profile_id: str | None = None) -> RoxyOpenResult:
        one_profile = bool(getattr(_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True))
        configured_pid = self._normalize_profile_id(profile_id if profile_id is not None else getattr(_cfg, "ROXY_PROFILE_ID", ""))
        if one_profile and configured_pid:
            # The caller receives no RoxyOpenResult on this validation failure;
            # release the client session just as for post-create failures.
            self.close()
            raise RuntimeError(
                "已启用 ROXY_ONE_PROFILE_PER_ACCOUNT=True（一号一环境），"
                "不能配置/传入固定 ROXY_PROFILE_ID；请留空以便每个账号创建新环境。"
            )

        pid = configured_pid
        created_by_run = False
        open_returned = False
        try:
            # Everything after create_profile belongs inside this protected
            # block.  Formatting a malformed open path, invalid params, or a
            # bad response must not orphan the just-created remote profile.
            if not pid:
                pid = self.create_profile()
                created_by_run = True
                logger.info("[Roxy] 已创建临时环境：%s", pid)

            path = str(_cfg.ROXY_OPEN_PATH).format(profile_id=pid)
            params = dict(getattr(_cfg, "ROXY_OPEN_EXTRA_PARAMS", {}) or {})
            # Roxy 官方 /browser/open body: {workspaceId, dirId, args, forceOpen, headless}
            params.setdefault("workspaceId", _workspace_id_value())
            params.setdefault("dirId", int(pid) if str(pid).isdigit() else pid)
            params.setdefault("args", [])
            params.setdefault("forceOpen", True)
            _apply_data_saver_open_args(params)
            # ROXY_OPEN_HEADLESS 是显式开关，优先级应高于 ROXY_OPEN_EXTRA_PARAMS，
            # 否则 extra 里残留 headless=False 会导致 WebUI 保存无头后仍弹窗口。
            params["headless"] = bool(getattr(_cfg, "ROXY_OPEN_HEADLESS", False))
            logger.info("[Roxy] open 参数：profile=%s headless=%s keep_open=%s", pid, params.get("headless"), getattr(_cfg, "ROXY_KEEP_BROWSER_OPEN", False))
            result = self.request(
                _cfg.ROXY_OPEN_METHOD,
                path,
                params=params if _cfg.ROXY_OPEN_METHOD.upper() == "GET" else None,
                json_body=params if _cfg.ROXY_OPEN_METHOD.upper() != "GET" else None,
            )
            open_returned = True
            debugger_address = self._extract_debugger_address(result)
            safe_result = _redact_sensitive(result)
            if self._proxy_pool_target and isinstance(safe_result, dict):
                safe_result["proxy_pool_target"] = _safe_proxy_target(self._proxy_pool_target)
            logger.info("[Roxy] open 返回摘要: debugger=%s raw=%s", _redact_text(debugger_address), json.dumps(safe_result, ensure_ascii=False)[:800])
            webdriver_url = _first(result, [
                ("webdriver",), ("webDriver",), ("webdriver_url",), ("webdriverUrl",),
                ("selenium",), ("selenium_url",), ("seleniumUrl",),
                ("data", "webdriver"), ("data", "webDriver"), ("data", "webdriver_url"), ("data", "webdriverUrl"),
                ("data", "selenium"), ("data", "selenium_url"), ("data", "seleniumUrl"),
            ]) or None
            ws_endpoint = _first(result, [
                ("ws",), ("wsEndpoint",), ("ws_endpoint",), ("debuggerWsUrl",),
                ("data", "ws"), ("data", "wsEndpoint"), ("data", "ws_endpoint"), ("data", "debuggerWsUrl"),
            ]) or None
            if not debugger_address and not webdriver_url:
                raise RuntimeError(
                    "Roxy 已打开环境但未返回 Selenium/调试地址，请检查 ROXY_OPEN_PATH 或接口响应: "
                    f"{_redact_sensitive(result)}"
                )
            # Never add raw proxy_pool_target to opened.raw.  The target is
            # available internally for masked diagnostics only.  Raw endpoint
            # values stay in memory solely for Selenium's connection.
            return RoxyOpenResult(
                pid,
                safe_result,
                debugger_address=debugger_address,
                webdriver_url=webdriver_url,
                ws_endpoint=ws_endpoint,
                created_by_run=created_by_run,
            )
        except Exception:
            # A caller may never receive RoxyOpenResult (for example a malformed
            # ROXY_OPEN_PATH).  Cleanup must therefore happen here, independently
            # of registration-level finally blocks.
            try:
                self.close_proxy_pool_relay()
            except Exception as exc:
                logger.debug("[Roxy] 打开失败后关闭代理链失败：%s", exc)
            if pid and (created_by_run or open_returned):
                try:
                    # A configured profile is closed only after Roxy accepted
                    # the open request; pre-open formatting/request failures
                    # must not disturb an unrelated existing profile.
                    self.close_profile(pid)
                except Exception as exc:
                    logger.debug("[Roxy] 打开失败后关闭环境失败：%s", _redact_text(exc))
                if created_by_run:
                    try:
                        # Failed setup is always disposable, even when normal-run
                        # deletion is disabled or one-profile mode was toggled off.
                        self.delete_profile(pid)
                    except Exception as exc:
                        logger.debug("[Roxy] 打开失败后删除环境失败：%s", _redact_text(exc))
            try:
                self.close()
            except Exception as exc:
                logger.debug("[Roxy] 打开失败后释放客户端资源失败：%s", exc)
            raise

    def close_profile(self, profile_id: str) -> None:
        if not profile_id:
            return
        try:
            path = str(_cfg.ROXY_CLOSE_PATH).format(profile_id=profile_id)
            body = {
                "workspaceId": _workspace_id_value(),
                "dirId": int(profile_id) if str(profile_id).isdigit() else profile_id,
            }
            self.request(
                _cfg.ROXY_CLOSE_METHOD,
                path,
                params=body if str(_cfg.ROXY_CLOSE_METHOD).upper() == "GET" else None,
                json_body=body if str(_cfg.ROXY_CLOSE_METHOD).upper() != "GET" else None,
            )
            logger.info("[Roxy] 已关闭环境：%s", profile_id)
        except Exception as exc:
            logger.warning("[Roxy] 关闭环境失败：%s", _redact_text(exc))

    def delete_profile(self, profile_id: str) -> None:
        if not profile_id:
            return
        try:
            path = str(getattr(_cfg, "ROXY_DELETE_PATH", "/browser/delete")).format(profile_id=profile_id)
            method = str(getattr(_cfg, "ROXY_DELETE_METHOD", "POST") or "POST")
            body = {
                "workspaceId": _workspace_id_value(),
                "dirIds": [int(profile_id) if str(profile_id).isdigit() else profile_id],
            }
            self.request(
                method,
                path,
                params=body if method.upper() == "GET" else None,
                json_body=body if method.upper() != "GET" else None,
            )
            logger.info("[Roxy] 已删除环境：%s", profile_id)
        except Exception as exc:
            logger.warning("[Roxy] 删除环境失败：%s", _redact_text(exc))

    def cleanup_profile(self, opened: RoxyOpenResult | None, *, force: bool = False) -> None:
        """清理 profile；失败路径可用 ``force`` 覆盖 keep-open 调试设置。"""
        keep_open = bool(getattr(_cfg, "ROXY_KEEP_BROWSER_OPEN", False)) and not force
        try:
            if not opened or not opened.profile_id:
                return
            # KEEP_BROWSER_OPEN intentionally keeps both the browser and its
            # relay alive after a successful run.  A forced setup failure does
            # not receive that exception.
            if keep_open:
                logger.info("[Roxy] ROXY_KEEP_BROWSER_OPEN=True，保留环境及代理链：%s", opened.profile_id)
                return
            self.close_profile(opened.profile_id)
            should_delete = (
                (force or bool(getattr(_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True)))
                and (force or bool(getattr(_cfg, "ROXY_DELETE_PROFILE_AFTER_RUN", True)))
                and bool(opened.created_by_run)
            )
            if should_delete:
                self.delete_profile(opened.profile_id)
        finally:
            if not keep_open:
                try:
                    self.close_proxy_pool_relay()
                except Exception as exc:
                    logger.debug("[Roxy] 清理 profile 后关闭代理链失败：%s", _redact_text(exc))

    def close_proxy_pool_relay(self) -> None:
        relay, self._proxy_pool_relay = self._proxy_pool_relay, None
        if relay is None:
            return
        try:
            last_error = getattr(relay, "last_error", "")
        except Exception:
            last_error = ""
        if last_error:
            try:
                upstream = getattr(getattr(relay, "upstream", None), "raw", "")
            except Exception:
                upstream = ""
            logger.warning(
                "[Roxy] 代理链最后一次错误：target=%s upstream=%s error=%s",
                _mask_proxy(self._proxy_pool_target),
                _mask_proxy(upstream),
                _redact_text(last_error),
            )
        try:
            relay.close()
        except Exception as exc:
            logger.debug("[Roxy] 代理链 close 失败：%s", _redact_text(exc))

    def proxy_transport_snapshot(self) -> dict | None:
        """返回本轮 Roxy 经本地代理链传输的全浏览器流量。"""
        relay = self._proxy_pool_relay
        if relay is None:
            return None
        try:
            return relay.traffic_snapshot()
        except Exception:
            return None

    @staticmethod
    def _extract_debugger_address(payload: dict) -> str | None:
        value = _first(payload, [
            ("debuggerAddress",), ("debugger_address",), ("debugAddress",),
            ("debuggingPortUrl",), ("debugging_port_url",),
            ("remoteDebuggingAddress",), ("remote_debugging_address",),
            ("http",), ("debugHttp",), ("debug_http",),
            ("data", "debuggerAddress"), ("data", "debugger_address"), ("data", "debugAddress"),
            ("data", "debuggingPortUrl"), ("data", "debugging_port_url"),
            ("data", "remoteDebuggingAddress"), ("data", "remote_debugging_address"),
            ("data", "http"), ("data", "debugHttp"), ("data", "debug_http"),
        ])
        if value:
            value = value.strip()
            # 兼容 http://127.0.0.1:xxxx / 127.0.0.1:xxxx / :xxxx / 9222
            value = value.replace("http://", "").replace("https://", "").strip("/")
            if value.startswith(":") and value[1:].isdigit():
                return f"127.0.0.1{value}"
            if value.isdigit():
                return f"127.0.0.1:{value}"
            if ":" in value and not value.startswith(":"):
                return value
        port = _first(payload, [
            ("debuggingPort",), ("debugging_port",), ("debug_port",), ("port",),
            ("data", "debuggingPort"), ("data", "debugging_port"), ("data", "debug_port"), ("data", "port"),
        ])
        if port:
            port = str(port).strip()
            if port.startswith(":"):
                port = port[1:]
            if port.isdigit():
                return f"127.0.0.1:{port}"
        return None
