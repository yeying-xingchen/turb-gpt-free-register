# -*- coding: utf-8 -*-
"""MoMo 一键开通配置。

配置统一通过项目根目录 ``.env`` 覆盖；自动支付 CDK 只应放在 .env
或进程环境中，绝不要提交到源码、账号记录或浏览器端。
"""
from config.env_loader import apply_env_overrides

# 功能开关。即使开启，未配置 PAYMENT_CDK 时服务也会拒绝自动支付任务。
MOMO_ACTIVATION_ENABLED: bool = True

# MoMo 公益 API v1 基址。
MOMO_ACTIVATION_API_BASE: str = "https://hahz7zf.ink"

# 是否在远端任务中提交自动支付。
MOMO_ACTIVATION_AUTO_PAY: bool = True

# 自动支付 CDK；仅从 .env/环境变量读取敏感值。
MOMO_ACTIVATION_PAYMENT_CDK: str = ""

# 发送给 MoMo 的入口代理池；为空时由服务端回退到通用 PROXY_POOL。
MOMO_ACTIVATION_ENTRY_PROXIES: list[str] = []

# 创建任务请求超时（秒）。服务运行时会限制到 API 支持的 8-120 范围。
MOMO_ACTIVATION_TIMEOUT: int = 25

# 试用天数（API 支持 1-90）。
MOMO_ACTIVATION_TRIAL_DAYS: int = 30

# 远端任务轮询间隔（秒）。
MOMO_ACTIVATION_POLL_INTERVAL: float = 2.0

# 单个远端任务最长等待时间（秒）。
MOMO_ACTIVATION_MAX_WAIT: int = 900

# 本地激活线程池和待处理队列上限。
MOMO_ACTIVATION_WORKERS: int = 2
MOMO_ACTIVATION_QUEUE_LIMIT: int = 100

apply_env_overrides(globals(), {
    "MOMO_ACTIVATION_ENABLED": "bool",
    "MOMO_ACTIVATION_API_BASE": "str",
    "MOMO_ACTIVATION_AUTO_PAY": "bool",
    "MOMO_ACTIVATION_PAYMENT_CDK": "str",
    "MOMO_ACTIVATION_ENTRY_PROXIES": "list_str_multiline",
    "MOMO_ACTIVATION_TIMEOUT": "int",
    "MOMO_ACTIVATION_TRIAL_DAYS": "int",
    "MOMO_ACTIVATION_POLL_INTERVAL": "float",
    "MOMO_ACTIVATION_MAX_WAIT": "int",
    "MOMO_ACTIVATION_WORKERS": "int",
    "MOMO_ACTIVATION_QUEUE_LIMIT": "int",
})
