# -*- coding: utf-8 -*-
"""CloakBrowser 的 Selenium 风格轻量适配层。"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from config import cloakbrowser as _cfg

logger = logging.getLogger(__name__)

# 单个 Cloak 浏览器（Chromium + Playwright 驱动进程）在批量任务里的经验内存占用。
# 只用于自动推算并发上限，不是硬性预留。
_BROWSER_MEMORY_MB = 700
_MAX_AUTO_CONCURRENCY = 32
# 拿不到并发额度时的最长等待；超过后放行并告警，避免一次异常泄漏把整条队列卡死。
_GATE_WAIT_SECONDS = 180.0


def _check_registration_stop() -> None:
    """在浏览器额度等待期间响应注册任务暂停/取消。"""
    try:
        from core.registration_service import check_stop_requested
    except ImportError:
        return
    check_stop_requested()


@dataclass
class CloakOpenResult:
    profile_id: str = "cloakbrowser"
    raw: dict | None = None


class CloakElement:
    def __init__(self, page, locator=None, handle=None):
        self.page = page
        self.locator = locator
        self.handle = handle

    def _handle(self):
        if self.handle is not None:
            return self.handle
        return self.locator.element_handle(timeout=5000)

    def _eval(self, expression: str, arg: Any = None) -> Any:
        if self.locator is not None:
            try:
                return self.locator.evaluate(expression, arg, timeout=3000)
            except TypeError:
                return self.locator.evaluate(expression, arg)
        return self.handle.evaluate(expression, arg)

    def _eval_handle(self, expression: str, arg: Any = None) -> Any:
        h = self._handle()
        return h.evaluate_handle(expression, arg)

    def is_displayed(self) -> bool:
        try:
            if self.locator is not None:
                return bool(self.locator.is_visible(timeout=800))
            return bool(self.handle.evaluate("el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'"))
        except Exception:
            return False

    def is_enabled(self) -> bool:
        try:
            if self.locator is not None:
                return bool(self.locator.is_enabled(timeout=800))
            return bool(self.handle.evaluate("el => !el.disabled && el.getAttribute('aria-disabled') !== 'true'"))
        except Exception:
            return False

    def click(self) -> None:
        if self.locator is not None:
            self.locator.click(timeout=10000)
        else:
            self.handle.click(timeout=10000)

    def _focused(self) -> bool:
        try:
            return bool(self._eval("el => document.activeElement === el"))
        except Exception:
            return False

    def _focus(self) -> None:
        # 只有当前元素未获得焦点时才点击；反复 click 会改变插入光标位置，
        # 也会破坏 Control+A 后保留的选区。
        if not self._focused():
            self.click()

    def _keyboard_type(self, text: str) -> None:
        target = self.locator if self.locator is not None else self.handle
        type_text = getattr(target, "type", None)
        if callable(type_text):
            # Locator/ElementHandle.type 会把键盘事件发给当前元素，
            # 不会像 fill 一样替换已有值，也不会把异常转移到全局焦点元素。
            type_text(text, delay=35)
            return
        keyboard = getattr(self.page, "keyboard", None)
        if keyboard is not None:
            keyboard.type(text, delay=35)
            return
        press_sequentially = getattr(target, "press_sequentially", None)
        if callable(press_sequentially):
            press_sequentially(text, delay=35)
            return
        raise RuntimeError("Cloak 输入元素不支持键盘输入")

    def _keyboard_press(self, key: str) -> None:
        keyboard = getattr(self.page, "keyboard", None)
        if keyboard is None:
            target = self.locator if self.locator is not None else self.handle
            press = getattr(target, "press", None)
            if callable(press):
                press(key, timeout=10000)
                return
            raise RuntimeError("Cloak 输入元素不支持按键操作")
        keyboard.press(key)

    def clear(self) -> None:
        try:
            if self.locator is not None:
                self.locator.fill("", timeout=10000)
            else:
                self.handle.fill("", timeout=10000)
            return
        except Exception:
            # 非 input 元素或部分受控输入框不支持 fill，回退键盘清空。
            self._focus()
            import sys
            modifier = "Meta" if sys.platform == "darwin" else "Control"
            self._keyboard_press(f"{modifier}+a")
            self._keyboard_press("Backspace")

    @property
    def tag_name(self) -> str:
        try:
            return str(self._eval("el => el.tagName.toLowerCase()") or "")
        except Exception:
            return ""

    def send_keys(self, *values: str) -> None:
        """发送 Selenium 风格按键，同时保留输入框已有内容和光标状态。

        Playwright 的 ``fill`` 是替换语义，而上层注册流程会按字符多次调用
        ``send_keys``。这里使用真实键盘事件追加文本，避免邮箱最终只剩最后一
        个字符；快捷键只识别 Selenium 私有码，不把普通的 ``control`` 文本
        误判成 Control 键。
        """
        if not values:
            return
        self._focus()
        special = {
            "\ue003": "Backspace", "\ue004": "Tab", "\ue005": "Clear",
            "\ue006": "Enter", "\ue007": "Enter", "\ue00c": "Escape",
            "\ue00d": "Space", "\ue00e": "PageUp", "\ue00f": "PageDown",
            "\ue010": "End", "\ue011": "Home", "\ue012": "ArrowLeft",
            "\ue013": "ArrowUp", "\ue014": "ArrowRight", "\ue015": "ArrowDown",
            "\ue016": "Insert", "\ue017": "Delete",
        }
        modifiers = {
            "\ue008": "Shift", "\ue009": "Control", "\ue00a": "Alt",
            "\ue03d": "Meta",
        }
        null_key = "\ue000"
        pending_modifier: str | None = None
        text_buffer: list[str] = []

        def flush_text() -> None:
            if text_buffer:
                self._keyboard_type("".join(text_buffer))
                text_buffer.clear()

        for raw in values:
            value = "" if raw is None else str(raw)
            for char in value:
                if char == null_key:
                    pending_modifier = None
                    continue
                if char in modifiers:
                    flush_text()
                    pending_modifier = modifiers[char]
                    continue
                if char in special:
                    flush_text()
                    key = special[char]
                    if pending_modifier:
                        self._keyboard_press(f"{pending_modifier}+{key}")
                    else:
                        self._keyboard_press(key)
                    continue
                if pending_modifier:
                    flush_text()
                    self._keyboard_press(f"{pending_modifier}+{char}")
                else:
                    text_buffer.append(char)
        flush_text()
        # Selenium 的修饰键默认只作用于本次 send_keys 调用；下一次调用
        # 必须重新声明，避免前一次 Shift/Control 泄漏到普通邮箱文本。
        pending_modifier = None

    def get_attribute(self, name: str) -> str | None:
        try:
            if self.locator is not None:
                return self.locator.get_attribute(name, timeout=1000)
            return self.handle.get_attribute(name)
        except Exception:
            return None


class _SwitchTo:
    def __init__(self, driver: "CloakSeleniumDriver"):
        self._driver = driver

    def window(self, handle: str) -> None:
        self._driver._switch_window(handle)


def _available_memory_mb() -> float | None:
    """读取当前可用内存（MB）；读不到时返回 None，调用方退回不限制。"""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return float(line.split()[1]) / 1024.0
    except Exception:
        pass
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return pages * page_size / (1024.0 * 1024.0)
    except Exception:
        pass
    return None


class _BrowserGate:
    """限制同时存活的 Cloak 浏览器数量，避免并发过高把内存打满。

    上限可以按可用内存动态计算：内存越紧张，同时启动的浏览器越少；已经在跑的
    浏览器不受影响，只有新的启动请求会排队等待。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._in_use = 0
        self._local = threading.local()

    @staticmethod
    def _configured_limit() -> int:
        try:
            value = int(getattr(_cfg, "CLOAK_MAX_CONCURRENT", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            value = 0
        return max(0, min(_MAX_AUTO_CONCURRENCY, value))

    def limit(self) -> int | None:
        """返回当前允许的并发上限；None 表示不限制。"""
        configured = self._configured_limit()
        if configured:
            return configured
        available = _available_memory_mb()
        if available is None:
            return None
        return max(1, min(_MAX_AUTO_CONCURRENCY, int(available // _BROWSER_MEMORY_MB)))

    def acquire(self, *, timeout: float = _GATE_WAIT_SECONDS) -> bool:
        """占用一个浏览器额度；返回 False 表示等超时后放行。

        同一线程内可重入：已经持有额度的线程再次启动浏览器时不再排队，
        避免注册→授权这类同线程嵌套流程互相等待。等待期间在条件锁外检查
        当前注册任务的暂停/取消状态，取消时不会启动新的浏览器。
        """
        _check_registration_stop()
        depth = getattr(self._local, "depth", 0)
        if depth > 0:
            self._local.depth = depth + 1
            return True
        deadline = time.monotonic() + max(0.0, float(timeout))
        logged = False
        while True:
            with self._cond:
                limit = self.limit()
                if limit is None or self._in_use < limit:
                    self._in_use += 1
                    self._local.depth = 1
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # 额度长时间拿不到（例如某个浏览器卡死）时放行，保证队列能继续。
                    self._in_use += 1
                    self._local.depth = 1
                    return False
                if not logged:
                    logged = True
                    logger.info(
                        "[Cloak] 等待浏览器并发额度：运行中=%s 上限=%s 可用内存=%sMB",
                        self._in_use, limit, int(_available_memory_mb() or 0),
                    )
                self._cond.wait(min(0.5, remaining))
            # 不持有条件锁检查暂停；暂停期间可以等待恢复而不阻塞其他浏览器释放额度。
            _check_registration_stop()

    def release(self) -> None:
        depth = getattr(self._local, "depth", 0)
        if depth > 1:
            self._local.depth = depth - 1
            return
        self._local.depth = 0
        with self._cond:
            if self._in_use > 0:
                self._in_use -= 1
            self._cond.notify_all()

    def snapshot(self) -> dict:
        return {"in_use": self._in_use, "limit": self.limit()}


_BROWSER_GATE = _BrowserGate()


class CloakSeleniumDriver:
    """只实现本项目 Roxy Selenium 流程实际用到的 WebDriver 子集。"""

    def __init__(self, browser: Any, context: Any | None, page: Any, proxy_relay: Any | None = None,
                 gate_slot: bool = False):
        self.browser = browser
        self.context = context
        self.page = page
        self._proxy_relay = proxy_relay
        self._gate_slot = bool(gate_slot)
        self._page_load_timeout_ms = int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90) * 1000
        self.switch_to = _SwitchTo(self)

    @property
    def current_url(self) -> str:
        return str(getattr(self.page, "url", "") or "")

    @property
    def window_handles(self) -> list[str]:
        pages = self._pages()
        return [str(i) for i in range(len(pages))]

    def _pages(self) -> list[Any]:
        try:
            if self.context is not None:
                return list(self.context.pages)
        except Exception:
            pass
        try:
            contexts = list(getattr(self.browser, "contexts", []) or [])
            pages = []
            for ctx in contexts:
                pages.extend(list(getattr(ctx, "pages", []) or []))
            return pages or [self.page]
        except Exception:
            return [self.page]

    def _switch_window(self, handle: str) -> None:
        pages = self._pages()
        idx = int(handle)
        self.page = pages[idx]
        try:
            self.page.bring_to_front()
        except Exception:
            pass

    def set_page_load_timeout(self, seconds: int) -> None:
        self._page_load_timeout_ms = int(seconds) * 1000
        try:
            self.page.set_default_navigation_timeout(self._page_load_timeout_ms)
            self.page.set_default_timeout(self._page_load_timeout_ms)
        except Exception:
            pass

    def get(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def back(self) -> None:
        self.page.go_back(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def refresh(self) -> None:
        self.page.reload(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def quit(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception:
            pass
        relay, self._proxy_relay = self._proxy_relay, None
        if relay is not None:
            relay.close()
        self._release_gate_slot()

    def _release_gate_slot(self) -> None:
        """归还浏览器并发额度；重复调用安全。"""
        if self._gate_slot:
            self._gate_slot = False
            _BROWSER_GATE.release()

    def find_elements(self, by: Any, selector: str) -> list[CloakElement]:
        loc = self._locator(by, selector)
        try:
            count = min(int(loc.count()), 200)
        except Exception:
            count = 0
        return [CloakElement(self.page, loc.nth(i)) for i in range(count)]

    def find_element(self, by: Any, selector: str) -> CloakElement:
        els = self.find_elements(by, selector)
        if not els:
            raise RuntimeError(f"找不到页面元素: {selector}")
        return els[0]

    def _locator(self, by: Any, selector: str):
        by_s = str(by or "").lower()
        if "xpath" in by_s or str(selector).startswith("//"):
            return self.page.locator(f"xpath={selector}")
        return self.page.locator(selector)

    def execute_script(self, script: str, *args: Any) -> Any:
        return self._evaluate(script, args=args, async_mode=False)

    def execute_async_script(self, script: str, *args: Any) -> Any:
        return self._evaluate(script, args=args, async_mode=True)

    def execute_cdp_cmd(self, cmd: str, params: dict | None = None) -> Any:
        params = params or {}
        try:
            client = self.context.new_cdp_session(self.page) if self.context is not None else self.page.context.new_cdp_session(self.page)
            return client.send(cmd, params)
        except Exception as exc:
            logger.debug("[Cloak] CDP 命令失败 %s: %s", cmd, exc)
            return None

    def _serialize_args(self, args: tuple[Any, ...]) -> tuple[CloakElement | None, list[Any]]:
        """拆分 Selenium 脚本参数。

        Playwright 的 JSHandle/ElementHandle 不能可靠地嵌在 dict/list payload 中跨
        page.evaluate 传递；Selenium 脚本最常见模式是 `arguments[0]` 为元素，
        因此这里把第一个 CloakElement 作为真实 DOM `el` 传入，其它参数保持
        JSON 可序列化。
        """
        first_el = args[0] if args and isinstance(args[0], CloakElement) else None
        rest = list(args[1:] if first_el else args)
        cleaned = []
        for item in rest:
            if isinstance(item, CloakElement):
                # 极少数脚本会传多个元素；用真实 handle 直接会在嵌套 payload 中失效，
                # 这里退化为 None，比把错误对象传进 JS 更安全。
                cleaned.append(None)
            else:
                cleaned.append(item)
        return first_el, cleaned

    @staticmethod
    def _unwrap_js_result(page, handle: Any) -> Any:
        try:
            element = handle.as_element()
        except Exception:
            element = None
        if element is not None:
            return CloakElement(page, handle=element)
        try:
            return handle.json_value()
        except Exception as exc:
            msg = str(exc)
            if "Execution context was destroyed" in msg or "navigation" in msg.lower():
                logger.info("[Cloak] JS 执行后页面发生跳转，忽略返回值读取失败：%s", msg[:160])
                return {"ok": True, "reason": "navigation_after_script"}
            raise
        finally:
            try:
                handle.dispose()
            except Exception:
                pass

    def _evaluate(self, script: str, args: tuple[Any, ...], async_mode: bool) -> Any:
        first_el, serial_args = self._serialize_args(args)
        if async_mode:
            wrapper = """async ({script, args}) => {
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            element_wrapper = """async (el, payload) => {
              const args = [el, ...payload.args];
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', payload.script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            if first_el is not None:
                result = first_el._eval(element_wrapper, {"script": script, "args": serial_args})
            else:
                result = self.page.evaluate(wrapper, {"script": script, "args": serial_args})
            if isinstance(result, dict) and result.get("__cloak_timeout"):
                raise TimeoutError("execute_async_script timeout")
            return result

        # Selenium 脚本经常以 `return ...` 为主体；用 Function 保持语义。
        wrapper = """({script, args}) => {
          const fn = new Function(...args.map((_, i) => 'a' + i), script);
          return fn(...args);
        }"""
        element_wrapper = """(el, payload) => {
          const args = [el, ...payload.args];
          const fn = new Function(...args.map((_, i) => 'a' + i), payload.script);
          return fn(...args);
        }"""
        if first_el is not None:
            # 这些调用把元素仅作为脚本输入，普通 Selenium execute_script 语义下
            # 返回值通常是标量/普通对象。走 evaluate 可以避免 Cloak humanize 在
            # evaluate_handle 隔离世界里把元素参数包装成非 DOM 对象，进而触发
            # `el.scrollIntoView is not a function`。
            return first_el._eval(element_wrapper, {"script": script, "args": serial_args})
        handle = self.page.evaluate_handle(wrapper, {"script": script, "args": serial_args})
        return self._unwrap_js_result(self.page, handle)


def _normalize_proxy(proxy: str | None) -> str | None:
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    return proxy.replace("socks5h://", "socks5://")


_GEO_CACHE: dict[str, tuple[float, dict]] = {}
_GEO_CACHE_LOCK = threading.Lock()


def _geo_cache_settings() -> tuple[float, float, int]:
    """(成功 TTL, 失败 TTL, 条数上限)。"""
    from config import browser as _browser_cfg
    def _number(name: str, default: float) -> float:
        try:
            value = float(getattr(_browser_cfg, name, default))
        except (TypeError, ValueError, OverflowError):
            return default
        if value != value or value < 0:  # NaN / 负数按默认值处理
            return default
        return value
    def _count(name: str, default: int) -> int:
        try:
            value = int(getattr(_browser_cfg, name, default))
        except (TypeError, ValueError, OverflowError):
            return default
        return max(0, value)
    return (
        _number("IP_GEO_CACHE_TTL", 1800.0),
        _number("IP_GEO_FAILURE_CACHE_TTL", 60.0),
        _count("IP_GEO_CACHE_SIZE", 256),
    )


def _geo_cache_get(key: str) -> dict | None:
    with _GEO_CACHE_LOCK:
        entry = _GEO_CACHE.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            _GEO_CACHE.pop(key, None)
            return None
        return dict(value)


def _geo_cache_put(key: str, value: dict, ttl: float) -> None:
    if ttl <= 0:
        return
    with _GEO_CACHE_LOCK:
        if len(_GEO_CACHE) >= _geo_cache_size_limit():
            # 简单淘汰：dict 保持插入顺序，一次性清掉最早写入的四分之一。
            for oldest in list(_GEO_CACHE)[: max(1, len(_GEO_CACHE) // 4)]:
                _GEO_CACHE.pop(oldest, None)
        _GEO_CACHE[key] = (time.monotonic() + ttl, dict(value))


def _geo_cache_size_limit() -> int:
    return _geo_cache_settings()[2]


def _detect_cloak_exit_geo(proxy_url: str | None = None) -> dict:
    """按当前/代理出口检测地理信息，供 Cloak 显式 locale/timezone 使用。

    结果按出口缓存：同一代理在批量查活里会被反复使用，缓存命中时不再发起
    HTTP 查询，浏览器启动也就少一次完整网络往返。
    """
    try:
        import requests
        from config import browser as _browser_cfg
        endpoints = list(getattr(_browser_cfg, "IP_GEO_ENDPOINTS", []) or [])
        timeout = float(getattr(_browser_cfg, "IP_GEO_TIMEOUT", 6) or 6)
    except Exception:
        return {}
    success_ttl, failure_ttl, size_limit = _geo_cache_settings()
    cache_key = str(proxy_url or "direct")
    if size_limit > 0:
        cached = _geo_cache_get(cache_key)
        if cached is not None:
            logger.debug("[Cloak] 出口地理信息命中缓存：%s", cache_key)
            return cached
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    for url in endpoints:
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=timeout)
            if resp.status_code != 200:
                continue
            data = resp.json()
            timezone = data.get("timezone")
            if isinstance(timezone, dict):
                timezone = timezone.get("id") or timezone.get("name")
            geo = {
                "ip": data.get("ip") or data.get("query"),
                "country": (data.get("country") or data.get("country_code") or data.get("countryCode") or "").upper(),
                "region": data.get("region") or data.get("regionName"),
                "city": data.get("city"),
                "timezone": timezone or "",
                "org": data.get("org") or data.get("isp") or (data.get("connection") or {}).get("org"),
            }
            if geo.get("country") or geo.get("timezone"):
                logger.info(
                    "[Cloak] 出口IP地理信息：ip=%s country=%s city=%s timezone=%s",
                    geo.get("ip") or "?", geo.get("country") or "?", geo.get("city") or "?", geo.get("timezone") or "?",
                )
                if size_limit > 0:
                    _geo_cache_put(cache_key, geo, success_ttl)
                return geo
        except Exception as exc:
            logger.debug("[Cloak] 出口 IP 地理检测失败 endpoint=%s: %s: %s", url, type(exc).__name__, exc)
    if size_limit > 0:
        # 失败也短缓存，避免出口抖动时每次启动都把所有 endpoint 重试一遍。
        _geo_cache_put(cache_key, {}, failure_ttl)
    return {}


def _build_cloak_locale_options(proxy_url: str | None = None) -> dict:
    """生成 Cloak/Playwright 双层语言时区配置。"""
    explicit_locale = str(getattr(_cfg, "CLOAK_LOCALE", "") or "").strip()
    explicit_timezone = str(getattr(_cfg, "CLOAK_TIMEZONE", "") or "").strip()
    out = {}
    if explicit_locale:
        out["locale"] = explicit_locale
        # Accept-Language 用 config.browser 自动推断更完整；显式时给一个保守值。
        out["accept_language"] = f"{explicit_locale},{explicit_locale.split('-')[0]};q=0.9,en-US;q=0.8,en;q=0.7"
    if explicit_timezone:
        out["timezone"] = explicit_timezone
    if explicit_locale and explicit_timezone:
        return out
    if not bool(getattr(_cfg, "CLOAK_GEOIP", True)):
        return out
    try:
        from config.browser import build_browser_environment
        geo = _detect_cloak_exit_geo(proxy_url)
        profile = build_browser_environment(geo)
        out.setdefault("locale", str(profile.get("navigator_language") or ""))
        out.setdefault("timezone", str(profile.get("timezone_iana") or ""))
        out.setdefault("accept_language", str(profile.get("accept_language") or ""))
        out["geo"] = geo
    except Exception as exc:
        logger.debug("[Cloak] 构建自动语言/时区失败：%s: %s", type(exc).__name__, exc)
    return {k: v for k, v in out.items() if v}


def _memory_saver_args() -> list[str]:
    """低内存模式的启动参数。

    只追加 V8 老生代上限：页面 JS 堆是单个 Cloak 浏览器里最容易失控的部分，
    设上限能挡住异常增长把整机内存吃满。放在用户 CLOAK_EXTRA_ARGS 之前，
    用户显式配置的同名参数仍然优先。
    """
    if not bool(getattr(_cfg, "CLOAK_MEMORY_SAVER", True)):
        return []
    try:
        heap_mb = int(getattr(_cfg, "CLOAK_JS_HEAP_MB", 512) or 0)
    except (TypeError, ValueError, OverflowError):
        heap_mb = 512
    if heap_mb <= 0:
        return []
    heap_mb = max(64, min(4096, heap_mb))
    return [f"--js-flags=--max-old-space-size={heap_mb}"]


def build_cloak_driver(
    proxy: str | None = None,
    *,
    isolated: bool = False,
    force_proxy: bool = False,
) -> tuple[CloakSeleniumDriver, CloakOpenResult]:
    """启动 CloakBrowser 并返回 Selenium 风格 driver。

    CLOAK_USE_PROXY 或 force_proxy 启用时：
    proxy=None  时按 config.proxy.PROXY_POOL 随机抽取；
    proxy=""    时显式禁用代理；
    proxy="..." 时使用指定代理。
    isolated=True 忽略 CLOAK_USER_DATA_DIR，每次创建临时独立 browser/context。

    启动前会占用一个浏览器并发额度（CLOAK_MAX_CONCURRENT，0=按可用内存自动），
    driver.quit() 时归还；这样批量任务的浏览器峰值内存可控。
    """
    browser = context = proxy_relay = None
    proxy_pool_target = ""
    gate_slot = False
    keep_open = bool(getattr(_cfg, "CLOAK_KEEP_BROWSER_OPEN", False))
    if keep_open:
        # 调试保留浏览器时不会调用 quit()，不占用额度，避免把额度泄漏光。
        logger.debug("[Cloak] CLOAK_KEEP_BROWSER_OPEN=True，本次启动不占用浏览器并发额度")
    try:
        use_proxy = force_proxy or bool(getattr(_cfg, "CLOAK_USE_PROXY", True))
        if proxy is None and use_proxy:
            try:
                from config.proxy import pick_proxy
            except Exception:
                proxy = None
            else:
                from core.proxy_chain import open_proxy_pool_proxy
                proxy_pool_target = str(pick_proxy() or "").strip()
                proxy, proxy_relay = open_proxy_pool_proxy(proxy_pool_target)
        try:
            from cloakbrowser import launch, launch_persistent_context
        except ImportError as exc:
            raise RuntimeError("未安装 cloakbrowser，请执行：pip install cloakbrowser") from exc

        launch_args = _memory_saver_args() + list(getattr(_cfg, "CLOAK_EXTRA_ARGS", []) or [])
        seed = str(getattr(_cfg, "CLOAK_FINGERPRINT_SEED", "") or "").strip()
        if seed:
            launch_args.append(f"--fingerprint={seed}")

        proxy_url = _normalize_proxy(proxy) if use_proxy else None
        locale_opts = _build_cloak_locale_options(proxy_url)
        # geoip=True 交给 CloakBrowser 根据当前出口 IP 自动匹配 timezone/locale/WebRTC。
        # 之前只有显式 proxy_url 时才开启；如果用户走系统代理/VPN/透明代理，代码层面
        # 看不到 proxy_url，会误关 geoip，导致语言/时区不跟随出口。这里改为完全尊重配置。
        opts = {
            "headless": bool(getattr(_cfg, "CLOAK_HEADLESS", False)),
            "humanize": bool(getattr(_cfg, "CLOAK_HUMANIZE", True)),
            "geoip": bool(getattr(_cfg, "CLOAK_GEOIP", True)),
        }
        if locale_opts.get("locale"):
            opts["locale"] = locale_opts["locale"]
        if locale_opts.get("timezone"):
            opts["timezone"] = locale_opts["timezone"]
        if proxy_url:
            opts["proxy"] = proxy_url
        if launch_args:
            opts["args"] = launch_args
        license_key = str(getattr(_cfg, "CLOAK_LICENSE_KEY", "") or "").strip()
        if license_key:
            opts["license_key"] = license_key

        user_data_dir = "" if isolated else str(getattr(_cfg, "CLOAK_USER_DATA_DIR", "") or "").strip()
        logger.info(
            "[Cloak] 启动 CloakBrowser：headless=%s humanize=%s geoip=%s proxy=%s locale=%s timezone=%s accept_language=%s persistent=%s",
            opts.get("headless"), opts.get("humanize"), opts.get("geoip"),
            proxy_url or "无", opts.get("locale") or "自动/默认", opts.get("timezone") or "自动/默认",
            locale_opts.get("accept_language") or "自动/默认", bool(user_data_dir),
        )
        context_kwargs = {}
        if locale_opts.get("locale"):
            context_kwargs["locale"] = locale_opts["locale"]
        if locale_opts.get("timezone"):
            context_kwargs["timezone_id"] = locale_opts["timezone"]
        if locale_opts.get("accept_language"):
            context_kwargs["extra_http_headers"] = {"Accept-Language": locale_opts["accept_language"]}

        if not keep_open:
            # 只在真正要拉起浏览器前排队，出口地理查询/代理中继可以在等待期间并行完成。
            # 无论是否在额度内拿到，acquire 都占用了一个计数，退出时必须由 quit() 归还。
            acquired = _BROWSER_GATE.acquire()
            gate_slot = True
            if not acquired:
                logger.warning(
                    "[Cloak] 等待浏览器并发额度超时，仍继续启动；如频繁出现请调低并发或检查残留浏览器进程"
                )
            logger.info("[Cloak] 浏览器并发额度：%s", _BROWSER_GATE.snapshot())

        started_at = time.monotonic()
        if user_data_dir:
            context = launch_persistent_context(user_data_dir, **opts)
            browser = getattr(context, "browser", None) or context
            # persistent context 的 locale/timezone 已通过 launch_persistent_context 参数传入。
        else:
            browser = launch(**opts)
            context = browser.new_context(**context_kwargs)
        page = context.new_page()
        logger.info("[Cloak] 浏览器启动耗时 %.2fs", time.monotonic() - started_at)

        driver = CloakSeleniumDriver(
            browser=browser, context=context, page=page,
            proxy_relay=proxy_relay, gate_slot=gate_slot,
        )
        # Roxy/Cloak 共用部分页面操作函数；给共享函数一个显式日志前缀，
        # 避免 Cloak 注册流程里出现 `[Roxy注册]`。
        driver._registration_log_prefix = "[Cloak注册]"
        driver.set_page_load_timeout(int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90))
        return driver, CloakOpenResult(raw={
            "driver": "cloakbrowser",
            "proxy": proxy_url,
            "proxy_pool_target": proxy_pool_target or proxy_url,
            "locale": locale_opts,
            "options": {k: v for k, v in opts.items() if k != "license_key"},
        })
    except BaseException:
        # driver 尚未交给调用方；任意阶段失败都由这里释放已取得的资源。
        # persistent context 可能同时充当 browser，避免对同一对象重复关闭。
        for resource in (context, browser if browser is not context else None, proxy_relay):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.debug("[Cloak] 启动失败后的资源清理失败", exc_info=True)
        if gate_slot:
            _BROWSER_GATE.release()
        raise
