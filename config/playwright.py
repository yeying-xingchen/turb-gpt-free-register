# -*- coding: utf-8 -*-
"""本地 Playwright 注册/查活配置。

该驱动用真实 Chromium 页面完成 ChatGPT/OpenAI 登录，而不是用 curl_cffi
直接访问 ``authorize``。当出口对纯协议导航返回 403 时，浏览器网络栈通常仍
可以正常完成 Cloudflare/JS 初始化，因此可作为注册和查活的独立路线。
"""
from config.env_loader import apply_env_overrides, env_str

# 注册驱动可以设置为 playwright；默认仍保持原有 Roxy 兼容行为。
PLAYWRIGHT_HEADLESS: bool = True
PLAYWRIGHT_BROWSER: str = "chromium"
PLAYWRIGHT_EXECUTABLE_PATH: str = env_str("PLAYWRIGHT_EXECUTABLE_PATH", "")
PLAYWRIGHT_USER_DATA_DIR: str = ""
PLAYWRIGHT_LOCALE: str = "en-US"
PLAYWRIGHT_TIMEZONE_ID: str = ""
PLAYWRIGHT_VIEWPORT_WIDTH: int = 1440
PLAYWRIGHT_VIEWPORT_HEIGHT: int = 900
PLAYWRIGHT_DEVICE_SCALE_FACTOR: float = 1.0
PLAYWRIGHT_TIMEOUT: int = 45
PLAYWRIGHT_NAVIGATION_TIMEOUT: int = 90
PLAYWRIGHT_OTP_TIMEOUT: int = 180
PLAYWRIGHT_LIVE_CHECK_TIMEOUT: int = 180
# 403/边缘挑战交给 Chromium 执行；默认等待一轮约五秒后再重试。
PLAYWRIGHT_EDGE_CHALLENGE_WAIT: float = 5.0
PLAYWRIGHT_EDGE_RETRY_ATTEMPTS: int = 3
PLAYWRIGHT_KEEP_BROWSER_OPEN: bool = False
PLAYWRIGHT_START_URL: str = "https://chatgpt.com/auth/login"

# 查活策略：auto 先用纯协议，代理/直连遇到 403 等边缘错误后切到
# Playwright 浏览器执行 Cloudflare challenge；playwright/cf/cloudflare 则直接走浏览器。
LIVE_CHECK_DRIVER: str = "auto"

# 需要手动填写密码的已有账号，密码从账号 extra_json/数据库中读取。
# 页面出现 TOTP 时由 account_liveness 的本地 TOTP secret 自动生成验证码。
PLAYWRIGHT_ALLOW_PASSWORD_LOGIN: bool = True

apply_env_overrides(globals(), {
    "PLAYWRIGHT_HEADLESS": "bool",
    "PLAYWRIGHT_BROWSER": "str",
    "PLAYWRIGHT_EXECUTABLE_PATH": "str",
    "PLAYWRIGHT_USER_DATA_DIR": "str",
    "PLAYWRIGHT_LOCALE": "str",
    "PLAYWRIGHT_TIMEZONE_ID": "str",
    "PLAYWRIGHT_VIEWPORT_WIDTH": "int",
    "PLAYWRIGHT_VIEWPORT_HEIGHT": "int",
    "PLAYWRIGHT_DEVICE_SCALE_FACTOR": "float",
    "PLAYWRIGHT_TIMEOUT": "int",
    "PLAYWRIGHT_NAVIGATION_TIMEOUT": "int",
    "PLAYWRIGHT_OTP_TIMEOUT": "int",
    "PLAYWRIGHT_LIVE_CHECK_TIMEOUT": "int",
    "PLAYWRIGHT_EDGE_CHALLENGE_WAIT": "float",
    "PLAYWRIGHT_EDGE_RETRY_ATTEMPTS": "int",
    "PLAYWRIGHT_KEEP_BROWSER_OPEN": "bool",
    "PLAYWRIGHT_START_URL": "str",
    "LIVE_CHECK_DRIVER": "str",
    "PLAYWRIGHT_ALLOW_PASSWORD_LOGIN": "bool",
})
