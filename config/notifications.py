# -*- coding: utf-8 -*-
"""管理员 SMTP 通知配置；运行时通过模块属性读取以支持热重载。"""
from config.env_loader import apply_env_overrides

SMTP_ENABLED: bool = False
SMTP_HOST: str = ""
SMTP_PORT: int = 465
SMTP_SECURITY: str = "ssl"  # ssl / starttls / plain
SMTP_USERNAME: str = ""
SMTP_PASSWORD: str = ""
SMTP_FROM: str = ""  # 留空时使用 SMTP_USERNAME
SMTP_ADMIN_EMAILS: str = ""  # 逗号、分号或换行分隔
SMTP_NOTIFY_TASKS: bool = True
SMTP_NOTIFY_UPLOADS: bool = True
SMTP_TIMEOUT: int = 15

apply_env_overrides(globals())
