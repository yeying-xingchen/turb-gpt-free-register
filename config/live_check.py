# -*- coding: utf-8 -*-
"""账号查活驱动配置，独立于注册与 Codex 授权驱动。"""
from config.env_loader import apply_env_overrides

# "protocol" = 纯协议重新登录；"cloak" = CloakBrowser 浏览器重新登录。
# 仅支持以上两种取值，不自动跟随 REGISTRATION_DRIVER / CODEX_OAUTH_DRIVER。
# Cloak 查活复用 config.cloakbrowser 的运行参数，但强制使用临时独立上下文，
# 结束时关闭浏览器；代理由现有查活网络路由决定。
LIVE_CHECK_DRIVER: str = "protocol"

# 批量查活的后台并发线程数（1–16）；可在任务中心运行中调整。
LIVE_CHECK_WORKERS: int = 3

# 浏览器查活（cloak）的省流量拦截：只拦图片/媒体/字体等可选资源和
# BROWSER_DATA_SAVER_BLOCKED_URL_PATTERNS 里确认过的遥测 URL，验证码/challenge 资源放行。
# 登录页不再下载整页图片资源，页面更小、渲染更快，渲染进程内存也更低。
# 与 BROWSER_DATA_SAVER_MODE 独立：后者影响注册，本项只作用于查活。
LIVE_CHECK_DATA_SAVER: bool = True

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {"LIVE_CHECK_DRIVER": "str", "LIVE_CHECK_WORKERS": "int", "LIVE_CHECK_DATA_SAVER": "bool"})
