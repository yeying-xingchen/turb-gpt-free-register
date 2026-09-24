# -*- coding: utf-8 -*-
"""从项目根目录 .env 加载密钥/敏感配置。

设计目标：
  - 重要 API Key 不进 git 跟踪的 config/*.py 默认值
  - config 模块启动 / reload 时读取环境变量
  - WebUI 可读写 .env 中的密钥字段
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_LOADED = False
# Track Secret values that were actually supplied by .env.  This lets a blank
# .env placeholder preserve an external/process-only Secret, while still
# allowing an explicit WebUI clear to remove a value previously loaded from
# .env rather than accidentally resurrecting it.
_DOTENV_APPLIED_SECRET_VALUES: dict[str, str] = {}
_DOTENV_APPLIED_ENV_PATH: Path | None = None
# Keys explicitly cleared through WebUI.  A blank `.env` normally preserves an
# external/process-only fallback, but an intentional clear must suppress it
# until a later nonblank value is supplied.
_SUPPRESSED_PROCESS_SECRET_KEYS: set[str] = set()

# 这些多行列表字段允许用空值显式覆盖为 []。
# 例如 WebUI 清空代理池后会写入 PROXY_POOL="" / PROXY_POOL="[]"，不能再回退到源码默认本地代理。
EXPLICIT_EMPTY_LIST_ENV_KEYS = {
    "PROXY_POOL",
    "PLAN_CHECK_PROXY",
    "PAYMENT_METHOD_CHECK_PROXIES",
    "DJB_PROXIES",
    "DJB_EXIT_PROXIES",
    "PAY153_ENTRY_PROXIES",
    "PAY153_EXIT_PROXIES",
    "MOMO_ACTIVATION_ENTRY_PROXIES",
}

# 统一管理：env key -> 说明（.env.example 用）
SECRET_ENV_KEYS: dict[str, str] = {
    "WEBUI_AUTH_CODE": "WebUI 登录授权码",
    "WEBUI_SESSION_SECRET": "WebUI Session Cookie 签名密钥",
    "BROWSER_USE_API_KEY": "Browser Use Cloud API Key",
    "SKYVERN_API_KEY": "Skyvern API Key",
    "CLOAK_LICENSE_KEY": "CloakBrowser License Key",
    "ROXY_API_TOKEN": "RoxyBrowser 本地 API Token",
    "PROXY_POOL": "代理池（可能包含认证信息）",
    "PLAN_CHECK_PROXY": "套餐查询专用代理（可能包含认证信息）",
    "GENERIC_API_PROXY": "通用邮箱 API 代理（可能包含认证信息）",
    "PLAN_CHECK_UPSTREAM_PROXY": "套餐查询本地上游代理地址（用于代理链）",
    "PROXY_POOL_UPSTREAM_PROXY": "代理池本地上游代理地址（用于代理链）",
    "PAYMENT_METHOD_CHECK_PROXY": "支付方式检测代理（可能包含认证信息）",
    "PAYMENT_METHOD_CHECK_PROXIES": "支付方式检测按地区代理映射（可能包含认证信息）",
    "PAYMENT_QUALIFICATION_PATH": "支付方式检测模块路径",
    "PAYMENT_QUALIFICATION_API_BASE": "支付方式检测服务 API 基地址",
    "PAYMENT_QUALIFICATION_API_PATH": "支付方式检测服务 API 路径",
    "PAYMENT_QUALIFICATION_API_KEY": "支付方式检测服务 API 密钥",
    "QQ_IMAP_PASSWORD": "QQ 邮箱 IMAP 授权码（不是 QQ 密码）",
    "GPTMAIL_API_KEY": "GPTMail API Key",
    "CLOUDFLARE_API_KEY": "Cloudflare Worker 临时邮箱 API Key / ADMIN_PASSWORD",
    "CLOUDFLARE_CUSTOM_AUTH": "Cloudflare Worker 全局密码 x-custom-auth",
    "MAIL_NEST_API_KEY": "MailNest API Key",
    "CLOUDMAIL_AUTH_TOKEN": "CloudMail Authorization Token",
    "CLOUDMAIL_PASSWORD": "CloudMail 登录密码",
    "REMAIL_API_KEY": "Remail 开放 API Key",
    "CPA_MANAGEMENT_KEY": "CPA 管理接口密钥",
    "EXTRACT_LINK_CDK": "提链服务 CDK",
    "DJB_CARD_CODE": "DJB 提链服务卡密（CDK）",
    "DJB_PROXIES": "DJB 建单代理池（可能包含认证信息）",
    "DJB_EXIT_PROXIES": "DJB 出口/账单代理池（可能包含认证信息）",
    "PAY153_INTERNAL_KEY": "pay153-checkout-link 内部请求密钥（X-Pay153-Internal-Key）",
    "PAY153_ENTRY_PROXIES": "pay153 入口代理池（可能包含认证信息）",
    "PAY153_EXIT_PROXIES": "pay153 出口代理池（可能包含认证信息）",
    "SUB2API_API_KEY": "sub2api 管理接口 API Key",
    "SUB2API_PROXY_KEY": "sub2api 代理键",
    "SUB2API_API_TOKEN": "sub2api 管理接口鉴权 Token（旧配置名，兼容）",
    "SUB2_CODEX_API_TOKEN": "sub2 Codex 兼容 API Token",
    "SMS_API_KEY": "接码平台 API Key（如 GrizzlySMS）",
    "SMSBOWER_API_KEY": "SMSBower API Key",
    "L_ADMIN_AUTH_CODE": "本地 L 接码服务 ADMIN_AUTH_CODE",
    "H_ADMIN_AUTH_CODE": "本地 H 接码服务 ADMIN_AUTH_CODE",
    "MOMO_ACTIVATION_PAYMENT_CDK": "MoMo 自动支付 CDK",
    "MOMO_ACTIVATION_ENTRY_PROXIES": "MoMo 入口代理池（可能包含认证信息）",
}


def env_path() -> Path:
    return _ENV_PATH


def load_env(*, override: bool = False) -> Path:
    """加载项目根 .env 到进程环境。可重复调用（reload 时用 override=True）。

    优先使用 python-dotenv；未安装时使用本文件内置的轻量 parser，避免配置读取强依赖。
    Secret 的空 `.env` 占位符不会覆盖仅存在于当前进程的 fallback Secret；但如果该值
    之前确实由 `.env` 加载，则 WebUI 写入空值时会清掉旧的 dotenv 值。这样既保留
    process-only fallback，也不破坏非空 `.env` 的热加载和显式清除。
    """
    global _LOADED, _DOTENV_APPLIED_SECRET_VALUES, _DOTENV_APPLIED_ENV_PATH
    file_values = read_env_file() if _ENV_PATH.exists() else {}
    if _DOTENV_APPLIED_ENV_PATH != _ENV_PATH:
        _DOTENV_APPLIED_SECRET_VALUES = {}
        _DOTENV_APPLIED_ENV_PATH = _ENV_PATH
    previous_dotenv = dict(_DOTENV_APPLIED_SECRET_VALUES)
    before_values = {key: os.environ.get(key) for key in SECRET_ENV_KEYS}

    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        if _ENV_PATH.exists():
            for key, value in file_values.items():
                if override or key not in os.environ:
                    os.environ[key] = value
        else:
            # 没有项目 .env 时仍保留当前进程环境。
            pass
    else:
        if _ENV_PATH.exists():
            load_dotenv(dotenv_path=_ENV_PATH, override=override)
        else:
            # 仍然允许系统环境变量生效
            load_dotenv(override=override)

    applied: dict[str, str] = {}
    for key in SECRET_ENV_KEYS:
        file_has_key = key in file_values
        file_value = str(file_values.get(key) or "")
        before = before_values.get(key)
        current = os.environ.get(key)
        previous = previous_dotenv.get(key)

        if file_has_key and file_value.strip():
            # A newly supplied nonblank value supersedes an explicit clear.
            _SUPPRESSED_PROCESS_SECRET_KEYS.discard(key)
            # override=True（热加载）时 current 应等于文件值；override=False
            # 且已有外部环境变量时，保留外部值并不把它标记成 dotenv-owned。
            if current == file_value:
                applied[key] = file_value
            continue

        if file_has_key and not file_value.strip():
            if key in _SUPPRESSED_PROCESS_SECRET_KEYS:
                os.environ[key] = ""
                continue
            if previous is not None and before == previous:
                # WebUI 明确把此前由 .env 提供的值清空。
                os.environ[key] = ""
            elif before is not None and str(before).strip():
                # process-only / 外部环境 Secret 优先于空占位符。
                os.environ[key] = before
            # 无此前值时保留 dotenv 写入的空字符串。
            continue

        if previous is not None and before == previous:
            # 删除 .env 中此前由它提供的 Secret 时同步清除旧覆盖值。
            os.environ[key] = ""

    _DOTENV_APPLIED_SECRET_VALUES = applied
    _LOADED = True
    return _ENV_PATH


def ensure_loaded() -> None:
    if not _LOADED:
        load_env(override=False)


def env_str(key: str, default: str = "") -> str:
    ensure_loaded()
    value = os.getenv(key)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip()


def _escape_env_value(value: str) -> str:
    # 统一双引号，避免空格/特殊字符问题
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "")
    )
    return f'"{escaped}"'


def read_env_file() -> dict[str, str]:
    """解析 .env 文件为 dict（不依赖 os.environ）。"""
    if not _ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for raw in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
            val = val.replace("\\n", "\n").replace("\\\"", '"').replace("\\\\", "\\")
        out[key] = val
    return out


def write_env_values(updates: dict[str, str]) -> list[str]:
    """更新 .env 中的若干 key；不存在则追加。返回实际写入的 key 列表。"""
    if not updates:
        return []

    existing_lines: list[str] = []
    if _ENV_PATH.exists():
        existing_lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()

    remaining = {str(k): ("" if v is None else str(v)) for k, v in updates.items()}
    for key, value in remaining.items():
        if key not in SECRET_ENV_KEYS:
            continue
        if str(value).strip():
            _SUPPRESSED_PROCESS_SECRET_KEYS.discard(key)
        else:
            _SUPPRESSED_PROCESS_SECRET_KEYS.add(key)
    written: list[str] = []
    out_lines: list[str] = []
    key_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

    for line in existing_lines:
        m = key_re.match(line)
        if not m:
            out_lines.append(line)
            continue
        key = m.group(1)
        if key in remaining:
            out_lines.append(f"{key}={_escape_env_value(remaining.pop(key))}")
            written.append(key)
        else:
            out_lines.append(line)

    if remaining:
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.append("# ---- updated by WebUI / config.env_loader ----")
        for key, value in remaining.items():
            out_lines.append(f"{key}={_escape_env_value(value)}")
            written.append(key)

    text = "\n".join(out_lines).rstrip() + "\n"
    tmp = _ENV_PATH.with_suffix(".env.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(_ENV_PATH)

    # An explicit empty Secret is a deliberate clear, not a masked/omitted
    # value.  Set it empty before reload so load_env cannot mistake a
    # process-only fallback for an external value that should be preserved.
    for key, value in remaining.items():
        if key in SECRET_ENV_KEYS and not str(value).strip():
            os.environ[key] = ""
    for key, value in updates.items():
        if str(key) in SECRET_ENV_KEYS and not str(value or "").strip():
            os.environ[str(key)] = ""

    # 让当前进程立刻看到新值
    load_env(override=True)
    return written


def _coerce_env_value(raw: str, default, vtype: str | None = None):
    if vtype is None:
        if isinstance(default, bool):
            vtype = "bool"
        elif isinstance(default, int) and not isinstance(default, bool):
            vtype = "int"
        elif isinstance(default, float):
            vtype = "float"
        elif isinstance(default, (list, tuple)):
            vtype = "list_str_multiline"
        else:
            vtype = "str"
    if vtype == "bool":
        return str(raw).strip().lower() in ("true", "1", "yes", "on", "y")
    if vtype == "int":
        return int(str(raw).strip())
    if vtype == "float":
        return float(str(raw).strip())
    if vtype == "list_str_multiline":
        text = str(raw)
        # 兼容旧值：PROXY_POOL='["http://..."]'
        try:
            import ast
            val = ast.literal_eval(text)
            if isinstance(val, (list, tuple)):
                return [str(x).strip() for x in val if str(x).strip()]
        except Exception:
            pass
        return [line.strip() for line in text.splitlines() if line.strip()]
    return str(raw).strip()


def env_value(key: str, default=None, vtype: str | None = None):
    ensure_loaded()
    raw = os.getenv(key)
    # `.env.example` 和 WebUI 里常见 `KEY=` / `KEY=""` 这种空配置。
    # 空值表示“未配置，使用 config/*.py 里的默认值”，否则 bool 默认 True
    # 会被空字符串误覆盖成 False，str/list 默认值也会被误清空。
    if raw is None:
        return default
    if str(raw).strip() == "":
        if vtype == "list_str_multiline" and key in EXPLICIT_EMPTY_LIST_ENV_KEYS:
            return []
        return default
    try:
        return _coerce_env_value(raw, default, vtype)
    except Exception:
        return default


def env_bool(key: str, default: bool = False) -> bool:
    return bool(env_value(key, default, "bool"))


def env_int(key: str, default: int = 0) -> int:
    return int(env_value(key, default, "int"))


def env_float(key: str, default: float = 0.0) -> float:
    return float(env_value(key, default, "float"))


def env_list(key: str, default: list[str] | None = None) -> list[str]:
    return list(env_value(key, default or [], "list_str_multiline"))


def apply_env_overrides(namespace: dict, schema: dict[str, str] | None = None) -> None:
    """用 .env/环境变量覆盖模块 globals() 中的配置常量。

    schema: {KEY: type}，type 支持 bool/int/float/str/list_str_multiline。
    没传 schema 时，会对 namespace 里已有的大写常量按默认值类型推断。
    """
    ensure_loaded()
    keys = schema.keys() if schema else [k for k in namespace if k.isupper()]
    for key in keys:
        if os.getenv(key) is None:
            continue
        default = namespace.get(key)
        vtype = schema.get(key) if schema else None
        namespace[key] = env_value(key, default, vtype)
