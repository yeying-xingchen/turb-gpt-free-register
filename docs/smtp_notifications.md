# SMTP 管理员通知

系统配置页会自动出现 **SMTP 通知** 分组：页面根据配置接口提供的分组和字段动态渲染，无需增加前端页面。设置统一保存到项目根目录 `.env`（设置 `TURB_ENV_FILE` 时使用指定文件），保存后沿用现有配置热重载。运行时代码通过 `config.notifications.SMTP_*` 读取最新值。

## 配置示例

```dotenv
SMTP_ENABLED="True"
SMTP_HOST="smtp.example.com"
SMTP_PORT="465"
SMTP_SECURITY="ssl"
SMTP_USERNAME="sender@example.com"
SMTP_PASSWORD="your-app-password"
SMTP_FROM=""
SMTP_ADMIN_EMAILS="admin1@example.com;admin2@example.com"
SMTP_NOTIFY_TASKS="True"
SMTP_NOTIFY_UPLOADS="True"
SMTP_TIMEOUT="15"
```

| 配置键 | 默认值 | 说明 |
| --- | --- | --- |
| `SMTP_ENABLED` | `False` | 邮件通知总开关，未启用不会发送 |
| `SMTP_HOST` | 空 | SMTP 主机名或 IP |
| `SMTP_PORT` | `465` | 1–65535 的整数；SSL 常用 465，STARTTLS 常用 587 |
| `SMTP_SECURITY` | `ssl` | `ssl` 直接建立 TLS；`starttls` 连接后升级 TLS；`plain` 明文连接 |
| `SMTP_USERNAME` | 空 | SMTP 登录用户名，通常为邮箱；免认证服务器可留空 |
| `SMTP_PASSWORD` | 空 | SMTP 密码或服务商授权码；页面按敏感字段显示，保存在 `.env` |
| `SMTP_FROM` | 空 | 纯发件邮箱地址；空值使用 `SMTP_USERNAME` |
| `SMTP_ADMIN_EMAILS` | 空 | 管理员纯邮箱地址列表，支持逗号、分号或换行分隔 |
| `SMTP_NOTIFY_TASKS` | `True` | 每个任务终态通知开关 |
| `SMTP_NOTIFY_UPLOADS` | `True` | 每次上传汇总通知开关 |
| `SMTP_TIMEOUT` | `15` | SMTP 网络操作超时秒数，1–120 的整数 |

在页面中可使用逗号或分号填写多个管理员；`.env` 中也支持 `SMTP_ADMIN_EMAILS="admin1@example.com\nadmin2@example.com"`。不接受 `姓名 <邮箱>` 形式。用户名如果不是邮箱，必须另填 `SMTP_FROM`；免认证服务器也必须另填发件邮箱。

可先保持总开关关闭，分步保存服务器、登录信息、发件邮箱和管理员邮箱，最后开启。编辑器会检查本次提交的端口、超时、安全方式和非空邮箱格式；显式开启时检查合并后的必要字段。已有不完整的 SMTP 配置不会阻塞无关字段保存。修改启用中的多项设置时，也可先关闭、分步修改，再重新开启。邮箱格式检查只是基本语法检查，不验证邮箱是否存在或 SMTP 服务是否可达。

## 通知行为

- 每个任务进入终态时通知管理员，包括成功、失败和取消。通知以单个任务为单位，不是批次汇总；批量操作中的每个终态任务分别发送。
- 每次账号上传成功导入至少一个账号后发送一封汇总通知，仅包含本次新插入账号的邮箱。全部重复或全部无效、没有新账号导入时不发送。
- 通知以 HTML 展示，并附带纯文本备选。涉及账号身份时仅包含邮箱，不包含账号密码、访问令牌、刷新令牌、授权码或其他凭据。
- 发送由后台处理，使用持久化队列重试，不等待 SMTP 网络发送完成才返回业务操作结果。每封通知最多尝试 5 次，重试间隔依次为 30、60、120、240 秒。发送失败仅记录日志，暂无通知队列或失败状态 UI；SMTP 故障不会改变任务或上传本身的结果。
- 队列采用至少一次投递语义：如果 SMTP 服务器已经接收邮件，但进程在记录发送成功前崩溃，恢复后可能再次发送，管理员可能收到重复通知。重试次数有限，不保证服务器持续故障时最终送达。
- 默认关闭；未配置必要发送信息时不会发邮件。可分别关闭任务通知或上传通知。

本配置页提供配置保存功能，没有测试邮件按钮。排查时确认 SMTP 主机、端口与安全方式匹配，服务商已开放 SMTP，并使用其要求的专用授权码。
