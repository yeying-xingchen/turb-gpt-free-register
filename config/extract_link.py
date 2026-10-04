# -*- coding: utf-8 -*-
"""Plus 试用提链服务配置。"""
from config.env_loader import apply_env_overrides

# 提链服务地址
EXTRACT_LINK_API_BASE: str = ""

# 提链 CDK；创建任务和监听事件都需要。
EXTRACT_LINK_CDK: str = ""

# 提链类型：pix / upi / kakao_pay / ideal
EXTRACT_LINK_TYPE: str = "pix"

# 后台提链并发与超时
EXTRACT_LINK_WORKERS: int = 3
EXTRACT_LINK_QUEUE_LIMIT: int = 500
EXTRACT_LINK_REQUEST_TIMEOUT: int = 30
EXTRACT_LINK_EVENT_TIMEOUT: int = 180

# 提链完整操作日志：每个账号一份 extract-link-<id>.log，位于数据库日志目录。
# EXTRACT_LOG_CREDENTIALS=True 时，提链 CDK / 账号 AT / 租户会话 token / 入口代理
# 会以明文写入本地日志文件；改为 False 或写入 .env 可切回脱敏。
EXTRACT_LOG_ENABLED: bool = True
EXTRACT_LOG_CREDENTIALS: bool = True
# 单个字段（请求体 / 响应体 / SSE 事件）写入日志的最大字符数。
EXTRACT_LOG_VALUE_LIMIT: int = 8000
# 单个日志文件的最大字节数，超出后轮转为 <文件>.1。
EXTRACT_LOG_MAX_BYTES: int = 5_000_000

apply_env_overrides(globals(), {
    'EXTRACT_LINK_API_BASE': 'str',
    'EXTRACT_LINK_CDK': 'str',
    'EXTRACT_LINK_TYPE': 'str',
    'EXTRACT_LINK_WORKERS': 'int',
    'EXTRACT_LINK_QUEUE_LIMIT': 'int',
    'EXTRACT_LINK_REQUEST_TIMEOUT': 'int',
    'EXTRACT_LINK_EVENT_TIMEOUT': 'int',
    'EXTRACT_LOG_ENABLED': 'bool',
    'EXTRACT_LOG_CREDENTIALS': 'bool',
    'EXTRACT_LOG_VALUE_LIMIT': 'int',
    'EXTRACT_LOG_MAX_BYTES': 'int',
})
