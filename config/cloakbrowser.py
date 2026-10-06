# -*- coding: utf-8 -*-
"""CloakBrowser 自动化注册配置。"""
from config.env_loader import apply_env_overrides

# 是否无头启动：False=显示窗口，True=无头。
CLOAK_HEADLESS: bool = True

# 是否启用 CloakBrowser humanize 行为。
CLOAK_HUMANIZE: bool = True

# 使用当前出口 IP 自动匹配时区/语言/WebRTC IP。
CLOAK_GEOIP: bool = True

# 显式指定 Cloak 语言/时区；留空则在 CLOAK_GEOIP=True 时按出口 IP 自动推断。
# 例如：CLOAK_LOCALE="ja-JP"，CLOAK_TIMEZONE="Asia/Tokyo"。
CLOAK_LOCALE: str = ""
CLOAK_TIMEZONE: str = ""

# 是否把本项目传入/代理池抽取的代理传给 CloakBrowser。
CLOAK_USE_PROXY: bool = True

# Pro license；留空则使用免费 binary。
CLOAK_LICENSE_KEY: str = ""

# 固定指纹 seed；留空则每次 launch 随机生成新指纹。
CLOAK_FINGERPRINT_SEED: str = ""

# 持久化用户目录；留空则临时上下文。若要固定账号画像/缓存，可填如 "./cloak-profiles/default"。
CLOAK_USER_DATA_DIR: str = ""

# 额外 Chromium 参数，例如 ["--fingerprint=12345"]。CLOAK_FINGERPRINT_SEED 会自动追加。
CLOAK_EXTRA_ARGS: list = []

# ---- 内存占用控制 ----
# 低内存模式：为启动参数追加 V8 老生代堆上限，抑制页面 JS 堆无限增长导致的
# 单个浏览器 RSS 膨胀。只设置上限，不改变指纹、WebGL 或语言时区参数。
CLOAK_MEMORY_SAVER: bool = True
# 低内存模式下单个渲染进程的 V8 老生代上限（MB）；0 表示不限制。
# 登录/注册页面通常在 150–350MB 之间，512MB 只兜住异常增长，正常流程不会触发。
CLOAK_JS_HEAP_MB: int = 512
# 同时存活的 Cloak 浏览器进程上限。0=按当前可用内存自动计算（推荐）；
# 显式填写时按 1–32 生效。浏览器是查活/注册里最重的资源，超出的任务会在启动前排队，
# 避免并发过高触发系统 OOM 或 swap。CLOAK_KEEP_BROWSER_OPEN=True 时不受此限制。
CLOAK_MAX_CONCURRENT: int = 0

# 与原 Roxy Selenium 流程共用的超时时间。
CLOAK_SELENIUM_TIMEOUT: int = 90

# 调试时保留浏览器不自动关闭。
CLOAK_KEEP_BROWSER_OPEN: bool = False

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'CLOAK_HEADLESS': 'bool', 'CLOAK_HUMANIZE': 'bool', 'CLOAK_GEOIP': 'bool', 'CLOAK_LOCALE': 'str', 'CLOAK_TIMEZONE': 'str', 'CLOAK_USE_PROXY': 'bool', 'CLOAK_LICENSE_KEY': 'str', 'CLOAK_FINGERPRINT_SEED': 'str', 'CLOAK_USER_DATA_DIR': 'str', 'CLOAK_SELENIUM_TIMEOUT': 'int', 'CLOAK_KEEP_BROWSER_OPEN': 'bool', 'CLOAK_MEMORY_SAVER': 'bool', 'CLOAK_JS_HEAP_MB': 'int', 'CLOAK_MAX_CONCURRENT': 'int'})
