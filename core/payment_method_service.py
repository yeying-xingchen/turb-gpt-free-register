# -*- coding: utf-8 -*-
"""Background payment-method qualification for registered accounts.

The checkout implementation is loaded from the configured
``qualification-test`` directory when available, with the vendored copy as a
fallback.  It only creates/reads a checkout and never confirms or starts a
payment.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse

from config import payment as payment_cfg
from config import proxy as proxy_cfg
from core import db

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_CHECKER = Path(__file__).with_name("payment_checker.py")
_EXTERNAL_CHECKER = _PROJECT_ROOT.parent / "qualification-test" / "gcash_checker.py"
_DEFAULT_REGIONS = {
    "gcash": {"channel": "gcash", "country": "PH", "currency": "PHP", "plan": "plus"},
    "card": {"channel": "card", "country": "PH", "currency": "PHP", "plan": "plus"},
    "paypal_uk": {"channel": "paypal", "country": "GB", "currency": "GBP", "plan": "plus"},
    "paypal_nl": {"channel": "paypal", "country": "NL", "currency": "EUR", "plan": "plus"},
    "ideal_nl": {"channel": "ideal", "country": "NL", "currency": "EUR", "plan": "plus"},
    "momo_vn": {"channel": "momo", "country": "VN", "currency": "VND", "plan": "plus"},
    "gopay_id": {"channel": "gopay", "country": "ID", "currency": "IDR", "plan": "plus"},
    "upi_in": {"channel": "upi", "country": "IN", "currency": "INR", "plan": "plus"},
    "blik_pl": {"channel": "blik", "country": "PL", "currency": "PLN", "plan": "plus"},
    "pix_br": {"channel": "pix", "country": "BR", "currency": "BRL", "plan": "plus"},
}


def _setting(name: str, default: Any) -> Any:
    try:
        from config.env_loader import load_env
        # Do not overwrite an explicit process environment value with a blank
        # value from .env; WebUI saves already refresh os.environ itself.
        load_env(override=False)
    except Exception:
        pass
    value = os.getenv(name)
    if value is not None and str(value).strip() != "":
        return value
    return getattr(payment_cfg, name, default)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(_setting(name, default))
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(_setting(name, default))
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _checker_path() -> Path:
    configured = str(_setting("PAYMENT_QUALIFICATION_PATH", "") or "").strip()
    candidates: list[Path] = []
    if configured:
        configured_path = Path(configured).expanduser().resolve()
        # This integration is deliberately limited to the requested sibling
        # checkout project or this repository's exact vendored adapter.  Do not
        # turn a WebUI setting into an arbitrary Python-code execution primitive.
        if configured_path.is_dir():
            candidate = configured_path / "gcash_checker.py"
        else:
            candidate = configured_path
        if candidate not in {_EXTERNAL_CHECKER.resolve(), _LOCAL_CHECKER.resolve()}:
            raise ValueError("PAYMENT_QUALIFICATION_PATH 必须指向项目内置适配器或 qualification-test/gcash_checker.py")
        candidates.append(candidate)
    # If the configured path is blank, prefer the portable vendored adapter;
    # an explicit PAYMENT_QUALIFICATION_PATH loads the sibling
    # qualification-test implementation (with its compatibility shim below).
    candidates.extend([_LOCAL_CHECKER, _EXTERNAL_CHECKER])
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    raise FileNotFoundError("找不到支付方式检测模块；请确认 qualification-test/gcash_checker.py 存在")


def _load_checker():
    checker_path = _checker_path()
    sentinel_path = checker_path.with_name("sentinel_token.py")
    if not sentinel_path.exists():
        sentinel_path = checker_path.with_name("payment_sentinel_token.py")
    module_name = "_turb_payment_checker"
    # Make sibling imports (including the checker’s own helper modules) resolve
    # exactly as they do when running qualification-test directly.
    source_dir = str(checker_path.parent)
    path_added = source_dir not in sys.path
    if path_added:
        sys.path.insert(0, source_dir)
    previous = sys.modules.get("sentinel_token")
    sentinel_name = "_turb_payment_sentinel_token"
    if sentinel_path.exists():
        sentinel_spec = importlib.util.spec_from_file_location(sentinel_name, sentinel_path)
        if sentinel_spec is None or sentinel_spec.loader is None:
            raise ImportError(f"无法加载支付 Sentinel 模块: {sentinel_path}")
        sentinel_module = importlib.util.module_from_spec(sentinel_spec)
        sys.modules[sentinel_name] = sentinel_module
        # qualification-test/gcash_checker.py historically imports this name.
        sys.modules["sentinel_token"] = sentinel_module
    else:
        sentinel_module = None
    try:
        if sentinel_module is not None:
            sentinel_spec.loader.exec_module(sentinel_module)
        spec = importlib.util.spec_from_file_location(module_name, checker_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载支付检测模块: {checker_path}")
        module = importlib.util.module_from_spec(spec)
        module.__file__ = str(checker_path)
        sys.modules[module_name] = module
        if checker_path.resolve() == _EXTERNAL_CHECKER.resolve():
            # Older qualification-test revisions constructed the custom result
            # positionally and silently shifted every field after `qualified`.
            # Apply the compatibility correction while loading that external
            # source; the vendored adapter already contains the keyword form.
            source = checker_path.read_text(encoding="utf-8")
            old_custom = 'return QualificationResult(available, meta["checkout_session_id"], target_channel, target_channel if available else "", _amount(state), _currency(state), True, f"{target_channel} channel published" if available else f"{country} checkout 未发布 {target_channel}", channels, details, availability, account_email, token, country)'
            new_custom = '''return QualificationResult(\n                qualified=available, checkout_session_id=meta["checkout_session_id"],\n                target_channel=target_channel,\n                processor_entity=target_channel if available else "",\n                payment_method_type=target_channel if available else "",\n                checkout_amount=_amount(state), checkout_currency=_currency(state),\n                proxy_configured=True,\n                evidence=f"{target_channel} channel published" if available else f"{country} checkout 未发布 {target_channel}",\n                available_channels=channels, channel_details=details,\n                channel_availability=availability, account_email=account_email,\n                access_token=token, country=country)'''
            old_stripe = '''return QualificationResult(\n                    available, meta["checkout_session_id"], target_channel,\n                    meta["processor_entity"] if available else "",\n                    target_channel if available else "", _amount(state), _currency(state), True,\n                    f"{target_channel} channel published (Stripe Checkout)" if available else f"{country} Stripe checkout 未发布 {target_channel}",\n                    channels, details, availability, account_email, token, country,\n                )'''
            new_stripe = '''return QualificationResult(\n                    qualified=available, checkout_session_id=meta["checkout_session_id"],\n                    target_channel=target_channel,\n                    processor_entity=meta["processor_entity"] if available else "",\n                    payment_method_type=target_channel if available else "",\n                    checkout_amount=_amount(state), checkout_currency=_currency(state),\n                    proxy_configured=True,\n                    evidence=f"{target_channel} channel published (Stripe Checkout)" if available else f"{country} Stripe checkout 未发布 {target_channel}",\n                    available_channels=channels, channel_details=details,\n                    channel_availability=availability, account_email=account_email,\n                    access_token=token, country=country,\n                )'''
            source = source.replace(old_custom, new_custom).replace(old_stripe, new_stripe)
            # Be tolerant of harmless formatting changes in future sibling
            # revisions while still correcting only this known constructor.
            source = re.sub(
                r'return QualificationResult\(available, meta\["checkout_session_id"\], target_channel, '
                r'target_channel if available else "", _amount\(state\), _currency\(state\), True, '
                r'.*?, channels, details, availability, account_email, token, country\)',
                new_custom,
                source,
                count=1,
                flags=re.DOTALL,
            )
            source = re.sub(
                r'return QualificationResult\(\s*available, meta\["checkout_session_id"\], target_channel,\s*'
                r'meta\["processor_entity"\] if available else "",\s*target_channel if available else "", '
                r'_amount\(state\), _currency\(state\), True,\s*.*?,\s*channels, details, availability, '
                r'account_email, token, country,?\s*\)',
                new_stripe,
                source,
                count=1,
                flags=re.DOTALL,
            )
            if "return QualificationResult(available, meta[\"checkout_session_id\"]" in source:
                raise RuntimeError("qualification-test checker 的 QualificationResult 构造格式不受支持")
            code = compile(source, str(checker_path), "exec")
            exec(code, module.__dict__)
        else:
            spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("sentinel_token", None)
        else:
            sys.modules["sentinel_token"] = previous
        if path_added:
            try:
                sys.path.remove(source_dir)
            except ValueError:
                pass


_CHECKER_LOCK = threading.Lock()
_CHECKER = None
_CHECKER_PATH: Path | None = None


def _checker():
    global _CHECKER, _CHECKER_PATH
    selected_path = _checker_path().resolve()
    if _CHECKER is None or _CHECKER_PATH != selected_path:
        with _CHECKER_LOCK:
            if _CHECKER is None or _CHECKER_PATH != selected_path:
                _CHECKER = _load_checker()
                _CHECKER_PATH = selected_path
    return _CHECKER


def available_region_names() -> list[str]:
    return list(_DEFAULT_REGIONS)


def _regions(selected: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    if selected is None:
        raw = str(_setting("PAYMENT_METHOD_CHECK_REGIONS", "") or "").strip()
        names = [x.strip().lower() for x in raw.replace("\n", ",").split(",") if x.strip()]
    else:
        names = [str(x).strip().lower() for x in selected if str(x).strip()]
    if not names:
        names = list(_DEFAULT_REGIONS)
    out = []
    for name in names:
        preset = dict(_DEFAULT_REGIONS.get(name) or {})
        if preset:
            preset.update({"name": name, "preset": name})
            out.append(preset)
    return out or [{"name": "gcash", "preset": "gcash", **_DEFAULT_REGIONS["gcash"]}]


def _proxy_map() -> dict[str, str]:
    raw = _setting("PAYMENT_METHOD_CHECK_PROXIES", [])
    if isinstance(raw, str):
        values = [line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()]
    elif isinstance(raw, (list, tuple)):
        values = [str(item).strip() for item in raw if str(item).strip()]
    else:
        values = []
    out = {}
    for value in values:
        if "=" in value:
            name, proxy = value.split("=", 1)
            if name.strip() and proxy.strip():
                out[name.strip().lower()] = proxy.strip()
    return out


def _proxy_for(region_name: str) -> str:
    overrides = _proxy_map()
    value = overrides.get(str(region_name or "").lower()) or overrides.get("default")
    if value:
        return value
    value = str(_setting("PAYMENT_METHOD_CHECK_PROXY", "") or "").strip()
    if value:
        return value
    # A configured pool is the last fallback.  Operators should use a proxy
    # whose exit country matches each selected checkout region.
    pool = getattr(proxy_cfg, "PROXY_POOL", []) or []
    return str(pool[0] if pool else "").strip()


def _redact_error(value: Any) -> str:
    text = str(value or "")
    # Avoid accidentally writing bearer/JWT/proxy credentials to the DB or UI.
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~-]+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)(https?://)([^/@\s]+):([^/@\s]+)@", r"\1[redacted]@", text)
    text = re.sub(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[token-redacted]", text)
    text = re.sub(r"\b(?:oaics|cs_(?:live|test))_[A-Za-z0-9]+\b", "[checkout-id-redacted]", text)
    return text[:500]


def _qualification_api_url() -> str:
    """Return the optional qualification-test HTTP API endpoint.

    The API base is intentionally opt-in. With no base configured the vendored
    or sibling checker is used directly; with a base configured all checkout
    work remains in the qualification-test service process.
    """
    base = str(_setting("PAYMENT_QUALIFICATION_API_BASE", "") or "").strip().rstrip("/")
    if not base:
        return ""
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("PAYMENT_QUALIFICATION_API_BASE 必须是无账号密码的 http(s) URL")
    path = str(_setting("PAYMENT_QUALIFICATION_API_PATH", "/api/gcash/check") or "/api/gcash/check").strip()
    if not path.startswith("/") or not path.startswith("/api/"):
        raise ValueError("PAYMENT_QUALIFICATION_API_PATH 必须是 /api/ 下的路径")
    return f"{base}{path}"


def _remote_qualification_post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = str(_setting("PAYMENT_QUALIFICATION_API_KEY", "") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib_request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024)
    except urllib_error.HTTPError as exc:
        # Do not include the remote response body: qualification APIs can echo
        # checkout diagnostics and credentials in an error document.
        raise RuntimeError(f"qualification API HTTP {exc.code}") from None
    except urllib_error.URLError as exc:
        raise RuntimeError(f"qualification API network error: {_redact_error(exc.reason)}") from None
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("qualification API 返回了无效 JSON") from None
    if not isinstance(result, dict):
        raise RuntimeError("qualification API 返回格式无效")
    if not result.get("ok"):
        raise RuntimeError(_redact_error(result.get("error") or "qualification API 检测失败"))
    return result


async def _remote_qualification_check(access_token: str, proxy: str, region: dict[str, Any], url: str, timeout: float) -> dict[str, Any]:
    payload = {
        # The remote service requires these fields to perform the checkout
        # check. They are sent over the configured API transport only and are
        # never included in the returned account state.
        "token": access_token,
        "proxy": proxy,
        "preset": region.get("preset"),
        "target_channel": region.get("channel"),
    }
    return await asyncio.to_thread(_remote_qualification_post, url, payload, timeout)


def _region_result(result: Any, region: dict[str, Any]) -> dict[str, Any]:

    data = result.as_dict() if hasattr(result, "as_dict") else dict(result or {})
    # Keep only display-safe capability data.  In particular, do not retain
    # access_token, account_email, checkout session IDs, or raw Stripe payloads.
    raw_details = data.get("channel_details") or []
    safe_details = []
    for item in raw_details:
        if not isinstance(item, dict) or "selected" in item:
            continue
        safe = {}
        for key in ("name", "raw_type", "region"):
            value = item.get(key)
            if value not in (None, ""):
                safe[key] = str(value)[:120]
        if safe:
            safe_details.append(safe)
    raw_channels = data.get("available_channels") or []
    channels = [str(x)[:120] for x in raw_channels] if isinstance(raw_channels, (list, tuple, set)) else [str(raw_channels)[:120]]
    raw_availability = data.get("channel_availability") or {}
    availability = {str(k)[:120]: bool(v) for k, v in raw_availability.items()} if isinstance(raw_availability, dict) else {}
    return {
        "name": region.get("name") or region.get("preset"),
        "preset": region.get("preset"),
        "channel": region.get("channel"),
        "country": region.get("country"),
        "currency": region.get("currency"),
        "qualified": bool(getattr(result, "qualified", data.get("qualified"))),
        "available_channels": channels,
        "channel_details": safe_details,
        "channel_availability": availability,
        "checkout_amount": data.get("checkout_amount"),
        "evidence": _redact_error(data.get("evidence")),
    }


async def _check_account_async(access_token: str, regions: list[dict[str, Any]], retries: int) -> dict:
    remote_url = _qualification_api_url()
    checker = None if remote_url else _checker()
    region_rows: list[dict[str, Any]] = []
    region_errors: list[dict[str, str]] = []
    for region in regions:
        proxy = _proxy_for(str(region.get("name") or region.get("preset") or ""))
        if not proxy:
            region_errors.append({"region": str(region.get("name") or region.get("preset")), "error": "未配置支付方式检测代理"})
            continue
        preset = {key: region[key] for key in ("channel", "country", "currency", "plan") if region.get(key) is not None}
        last_error = ""
        for attempt in range(max(1, retries)):
            try:
                if remote_url:
                    result = await _remote_qualification_check(
                        access_token, proxy, region, remote_url,
                        timeout=_float_setting("PAYMENT_METHOD_CHECK_TIMEOUT", 180.0, 30.0, 900.0),
                    )
                else:
                    result = await checker.check_gcash(
                        access_token,
                        proxy,
                        target_channel=str(region["channel"]),
                        target_channels=[str(region["channel"])],
                        preset=preset,
                        retries=1,
                    )
                region_rows.append(_region_result(result, region))
                last_error = ""
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {_redact_error(exc)}"
                if attempt + 1 < max(1, retries):
                    await asyncio.sleep(min(3.0, 0.5 * (attempt + 1)))
        if last_error:
            region_errors.append({"region": str(region.get("name") or region.get("preset")), "error": last_error})

    available: list[str] = []
    details: list[dict[str, Any]] = []
    availability: dict[str, bool] = {}
    for region in region_rows:
        for channel in region["available_channels"]:
            if str(channel).lower() not in {str(item).lower() for item in available}:
                available.append(channel)
        details.extend(region["channel_details"])
        for channel, value in region["channel_availability"].items():
            availability[channel] = bool(availability.get(channel) or value)
    details.append({"selected": availability, "checkout_provider": "qualification-test"})
    primary = region_rows[0] if region_rows else {}
    return {
        "ok": bool(region_rows),
        "qualified": any(item["qualified"] for item in region_rows),
        "channel": primary.get("channel") or "",
        "target_channel": primary.get("channel") or "",
        "country": primary.get("country") or "",
        "currency": primary.get("currency") or "",
        "checkout_amount": primary.get("checkout_amount"),
        "proxy_configured": bool(region_rows),
        "evidence": " | ".join(f"{r['name']}: {r['evidence']}" for r in region_rows if r.get("evidence")),
        "available_channels": available,
        "channel_details": details,
        "channel_availability": availability,
        "regions": region_rows,
        "checked_regions": [r.get("preset") for r in regions],
        "region_errors": region_errors,
        "error": "；".join(f"{x['region']}: {x['error']}" for x in region_errors),
    }


# A single loop owns curl_cffi AsyncSession/Sentinel caches.  Worker threads
# submit coroutines here instead of creating one event loop per account.
_ASYNC_LOOP = asyncio.new_event_loop()
threading.Thread(target=_ASYNC_LOOP.run_forever, name="payment-method-asyncio", daemon=True).start()


def _run_payment_check(*, account_id: int, email: str, access_token: str, trigger: str, regions: list[str] | None = None, nonce: str | None = None) -> dict:
    try:
        if not db.mark_account_payment_method_check_running(account_id, nonce=nonce):
            return {"ok": False, "error": "账号已删除或支付方式查询状态已被重置"}
        delay = _float_setting("PAYMENT_METHOD_CHECK_MIN_INTERVAL", 0.8, 0.0, 30.0)
        if delay:
            time.sleep(delay)
        region_configs = _regions(regions)
        retries = _int_setting("PAYMENT_METHOD_CHECK_RETRIES", 2, 1, 5)
        timeout = _float_setting("PAYMENT_METHOD_CHECK_TIMEOUT", 180.0, 30.0, 900.0)
        future = asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(_check_account_async(access_token, region_configs, retries), timeout=timeout),
            _ASYNC_LOOP,
        )
        try:
            result = future.result(timeout=timeout + 10.0)
        except Exception as exc:
            future.cancel()
            result = {"ok": False, "checked_at": datetime.now().isoformat(timespec="seconds"), "error": f"{type(exc).__name__}: {_redact_error(exc)}"}
        result["checked_at"] = result.get("checked_at") or datetime.now().isoformat(timespec="seconds")
        result["trigger"] = trigger
        result["proxy_used"] = "configured" if any(_proxy_for(str(r.get("name") or r.get("preset"))) for r in region_configs) else ""
        if not db.update_account_payment_method_check(acc_id=account_id, result=result, nonce=nonce):
            logger.warning("[Payment] ownership lost before result write: account_id=%s", account_id)
        if result.get("ok"):
            logger.info("[Payment] 支付方式查询成功: %s, regions=%s", email, result.get("checked_regions"))
        else:
            logger.warning("[Payment] 支付方式查询失败: %s, error=%s", email, result.get("error"))
        return result
    except Exception as exc:
        result = {"ok": False, "checked_at": datetime.now().isoformat(timespec="seconds"), "error": f"{type(exc).__name__}: {_redact_error(exc)}"}
        try:
            db.update_account_payment_method_check(acc_id=account_id, result=result, nonce=nonce)
        except Exception:
            logger.exception("[Payment] 写入异常状态失败: account_id=%s", account_id)
        logger.exception("[Payment] 后台查询异常: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()


_WORKERS = _int_setting("PAYMENT_METHOD_CHECK_WORKERS", 2, 1, 16)
_QUEUE_LIMIT = _int_setting("PAYMENT_METHOD_CHECK_QUEUE_LIMIT", 200, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="payment-method")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def enqueue_account_payment_method_check(*, account_id: int, email: str, access_token: str, trigger: str = "manual", regions: list[str] | None = None) -> dict:
    if not str(access_token or "").strip():
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if regions is not None:
        requested = [str(item).strip().lower() for item in regions if str(item).strip()]
        invalid = [item for item in requested if item not in _DEFAULT_REGIONS]
        if invalid or not requested:
            return {"accepted": False, "busy": False, "error": f"不支持的支付方式地区预设: {', '.join(invalid or requested)}", "supported_regions": available_region_names()}
        if len(requested) > 20:
            return {"accepted": False, "busy": False, "error": "单次最多选择 20 个支付方式地区预设"}
    else:
        requested = None
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "支付方式查询队列已满，请稍后重试"}
    nonce = db.claim_account_payment_method_check(int(account_id), trigger=trigger, return_nonce=True)
    if not nonce:
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查询支付方式"}
    try:
        _EXECUTOR.submit(
            _run_payment_check,
            account_id=int(account_id), email=str(email or ""), access_token=str(access_token),
            trigger=str(trigger or "manual"), regions=requested, nonce=str(nonce),
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {"ok": False, "checked_at": datetime.now().isoformat(timespec="seconds"), "error": f"支付方式查询入队失败: {type(exc).__name__}: {_redact_error(exc)}"}
        db.update_account_payment_method_check(acc_id=int(account_id), result=result, nonce=str(nonce))
        return {"accepted": False, "busy": False, "error": result["error"]}
    return {"accepted": True, "busy": False, "account_id": int(account_id), "email": str(email or ""), "status": "queued", "trigger": str(trigger or "manual")}


def queue_settings() -> dict:
    return {
        "workers": _int_setting("PAYMENT_METHOD_CHECK_WORKERS", _WORKERS, 1, 16),
        "queue_limit": _int_setting("PAYMENT_METHOD_CHECK_QUEUE_LIMIT", _QUEUE_LIMIT, 1, 5000),
        "timeout": _float_setting("PAYMENT_METHOD_CHECK_TIMEOUT", 180.0, 30.0, 900.0),
        "retries": _int_setting("PAYMENT_METHOD_CHECK_RETRIES", 2, 1, 5),
        "regions": [item["name"] for item in _regions()],
    }
