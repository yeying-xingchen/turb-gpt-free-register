# -*- coding: utf-8 -*-
"""Public scan API v1 configuration.

The submitting user's CDK is intentionally not configured here. It is supplied
for one submit/query request and is never persisted by the application.
"""
from config.env_loader import apply_env_overrides

# Public API v1 base URL. Keep the /api/v1 suffix.
SCAN_API_BASE: str = "https://scan-qr.hixinghai.com/api/v1"
MASI_API_BASE: str = "https://masi.cc.cd"
ORDERHUB_API_BASE: str = "https://upi.xxsyun.xyz/api/v1"
# 发布者 API：CDK 直接作为 Authorization: Bearer 凭据，负责接单与支付结果查询。
SEASHORE_API_BASE: str = "https://seashore.lol/api/publisher"

# Upstream request timeout in seconds.
SCAN_API_TIMEOUT: int = 30

# Identifies this application to the public API without including credentials.
SCAN_API_USER_AGENT: str = "turb-gpt-free-register/1.0"

# 提交支付的完整操作日志：每个账号一份 scan-payment-<id>.log，位于数据库日志目录。
# PAYMENT_LOG_CREDENTIALS=True 时，支付 CDK / OrderHub API Key / 账号 AT / 订单密钥
# 会以明文写入本地日志文件，便于直接核对与回放；改为 False 或写入 .env 可切回脱敏。
PAYMENT_LOG_ENABLED: bool = True
PAYMENT_LOG_CREDENTIALS: bool = True
# 单个字段（请求体 / 响应体）写入日志的最大字符数。
PAYMENT_LOG_VALUE_LIMIT: int = 8000
# 单个日志文件的最大字节数，超出后轮转为 <文件>.1。
PAYMENT_LOG_MAX_BYTES: int = 5_000_000

# 开通 Plus 批次的后台并发线程数（1–16）；可在任务中心运行中调整。
PLUS_ACTIVATION_WORKERS: int = 4

apply_env_overrides(globals(), {
    "PLUS_ACTIVATION_WORKERS": "int",
    "SCAN_API_BASE": "str",
    "MASI_API_BASE": "str",
    "ORDERHUB_API_BASE": "str",
    "SEASHORE_API_BASE": "str",
    "SCAN_API_TIMEOUT": "int",
    "SCAN_API_USER_AGENT": "str",
    "PAYMENT_LOG_ENABLED": "bool",
    "PAYMENT_LOG_CREDENTIALS": "bool",
    "PAYMENT_LOG_VALUE_LIMIT": "int",
    "PAYMENT_LOG_MAX_BYTES": "int",
})
