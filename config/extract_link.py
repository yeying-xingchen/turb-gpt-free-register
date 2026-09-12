# -*- coding: utf-8 -*-
"""Plus 试用提链服务配置。

支持三种后端：
  - cdk    原 CDK 提链服务（EXTRACT_LINK_API_BASE / EXTRACT_LINK_CDK，SSE 事件流）
  - pay153 pay153-checkout-link 提链服务（PAY153_API_BASE，/api/checkout + 轮询）
  - djbnb   DJB 提链 API（DJB_API_BASE / DJB_CARD_CODE，/api/tasks + 轮询）
"""
from config.env_loader import apply_env_overrides

# 提链后端：cdk / pay153 / djbnb
EXTRACT_LINK_BACKEND: str = "cdk"

# ==================== cdk 后端 ====================
# 提链服务地址
EXTRACT_LINK_API_BASE: str = ""

# 提链 CDK；创建任务和监听事件都需要。
EXTRACT_LINK_CDK: str = ""

# ==================== djbnb 后端 ====================
# DJB API 地址；请显式配置你信任的服务地址（官方文档：https://www.djbnb.xyz/api-doc.html）
DJB_API_BASE: str = ""

# DJB 卡密（CDK）；成功生成链接后按服务规则扣次
DJB_CARD_CODE: str = ""

# DJB 建单代理池与出口/账单侧代理池。每行一条，支持 host:port:user:pass、http:// 或 socks5://。
DJB_PROXIES: list[str] = []
DJB_EXIT_PROXIES: list[str] = []

# DJB 任务并发、轮询和有效窗口（服务端会进一步限流）
DJB_CONCURRENCY: int = 3
DJB_POLL_INTERVAL_MS: int = 2000
DJB_TIMEOUT_MS: int = 600000
DJB_PROXY_MODE: str = "custom"
DJB_PARAMS_TEXT: str = "{}"

# ==================== pay153 后端 ====================
# pay153-checkout-link 服务地址，例如 http://127.0.0.1:18082
PAY153_API_BASE: str = ""

# pay153 内部请求密钥（X-Pay153-Internal-Key 请求头，可选）。
# 设置后可绕过公开队列 IP RPM，并允许在未填写代理池时使用 pay153 侧动态代理。
PAY153_INTERNAL_KEY: str = ""

# pay153 计划参数：plus / pro / team / codex_low
PAY153_PLAN: str = "plus"

# pay153 结算国家 / 币种；留空时由 pay153 服务端自动选择
PAY153_COUNTRY: str = ""
PAY153_CURRENCY: str = ""

# pay153 入口 / 出口代理池（每行一条代理）。留空且配置了内部密钥时走 pay153 动态代理。
PAY153_ENTRY_PROXIES: list[str] = []
PAY153_EXIT_PROXIES: list[str] = []

# pay153 外层重试次数（每次重建 Checkout / 设备标识 / 支付参数）
PAY153_RETRY_COUNT: int = 3

# pay153 Plus 计划是否请求优惠（试用零元）
PAY153_USE_PROMO: bool = True

# pay153 进度轮询间隔（毫秒）与整体超时（秒）
PAY153_POLL_INTERVAL_MS: int = 1200
PAY153_POLL_TIMEOUT: int = 900

# ==================== 共用 ====================
# 提链类型。cdk 后端支持 pix / upi / kakao_pay / ideal；
# pay153 后端支持 hosted / ph_short / paypal / ideal / twint / upi / pix / momo / gcash / kakao。
EXTRACT_LINK_TYPE: str = "pix"

# 后台提链并发与超时
EXTRACT_LINK_WORKERS: int = 3
EXTRACT_LINK_QUEUE_LIMIT: int = 500
EXTRACT_LINK_REQUEST_TIMEOUT: int = 30
EXTRACT_LINK_EVENT_TIMEOUT: int = 180

apply_env_overrides(globals(), {
    'EXTRACT_LINK_BACKEND': 'str',
    'EXTRACT_LINK_API_BASE': 'str',
    'EXTRACT_LINK_CDK': 'str',
    'DJB_API_BASE': 'str',
    'DJB_CARD_CODE': 'str',
    'DJB_PROXIES': 'list_str_multiline',
    'DJB_EXIT_PROXIES': 'list_str_multiline',
    'DJB_CONCURRENCY': 'int',
    'DJB_POLL_INTERVAL_MS': 'int',
    'DJB_TIMEOUT_MS': 'int',
    'DJB_PROXY_MODE': 'str',
    'DJB_PARAMS_TEXT': 'str',
    'PAY153_API_BASE': 'str',
    'PAY153_INTERNAL_KEY': 'str',
    'PAY153_PLAN': 'str',
    'PAY153_COUNTRY': 'str',
    'PAY153_CURRENCY': 'str',
    'PAY153_ENTRY_PROXIES': 'list_str_multiline',
    'PAY153_EXIT_PROXIES': 'list_str_multiline',
    'PAY153_RETRY_COUNT': 'int',
    'PAY153_USE_PROMO': 'bool',
    'PAY153_POLL_INTERVAL_MS': 'int',
    'PAY153_POLL_TIMEOUT': 'int',
    'EXTRACT_LINK_TYPE': 'str',
    'EXTRACT_LINK_WORKERS': 'int',
    'EXTRACT_LINK_QUEUE_LIMIT': 'int',
    'EXTRACT_LINK_REQUEST_TIMEOUT': 'int',
    'EXTRACT_LINK_EVENT_TIMEOUT': 'int',
})
