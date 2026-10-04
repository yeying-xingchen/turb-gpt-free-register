# UPI-GIT5 提链接入

UPI-GIT5 已作为新的 `upi_git5` 提链供应商接入，支付方式为 `upi`。支持单账号，以及将同次选中的多个账号合并为一个上游批次；不需要额外 Python 依赖或数据库迁移。

## 使用

1. 更新代码后重启项目的 WebUI 服务，刷新浏览器。
2. 打开账号工具栏的“管理服务商 / CDK”，新增服务商，类型选 **UPI-GIT5**。API 地址会填入 `https://upi.agiapi.top`，默认支付方式为 UPI。
3. 添加提链 CDK，或在每次提链时输入一次性 CDK。可先验证 CDK，查看剩余、可用和预留次数。
4. 选择符合 `free` 且可 Plus 试用条件、具备 access_token 的账号，点击提链并选择该供应商。
5. 在“UPI 入口代理”中填写上游服务可连接的代理 URL，每行一个，再提交。支持 HTTP(S) 与 SOCKS URL。不会自动使用本地服务器代理池；本机地址通常无法被远端 UPI 服务访问。
6. 在账号列表查看进度、结果和独立的支付状态。可刷新原任务、取消执行中的任务、获取 UPI 二维码。使用一次性 CDK 的任务，后续操作需重新输入原 CDK；保存过的 CDK 会自动按所属供应商读取。

单次 WebUI 请求最多 500 个账号。无资格、无 AT、不存在的账号会跳过；已运行或待核实的账号由服务端排除，其余账号合并提交一次。账号 token 数组与入口代理数组均传给上游，不强制数量相等。文档没有定义代理与账号的分配规则，由上游决定。

## 本地请求

管理供应商沿用 `/api/extract-link/providers` 与 CDK 管理接口。API 地址、供应商名称、CDK 均通过管理页保存，不会因本次代码更新修改现有供应商默认配置。

提交接口：

- `POST /api/accounts/extract-link`：`account_id` 为单账号 ID。
- `POST /api/accounts/extract-link-bulk`：`account_ids` 为账号 ID 数组。

示例请求体（需已有 WebUI 登录会话或鉴权请求头）：

```json
{
  "account_ids": [101, 102],
  "provider_id": 3,
  "cdk_id": 7,
  "link_type": "upi",
  "entry_proxies": ["http://user:password@proxy.example:8080"],
  "use_promo": true,
  "promo_campaign": "plus-1-month-free"
}
```

`cdk_id` 可替换为 `cdk` 字符串，两者只能提供一个。`use_promo`、`promo_campaign`、`payment_provider_id` 为可选项，省略时采用上游默认值；`payment_provider_id` 接受 `foarge`、`xxsyun`、`astrascan`。当前表单使用默认促销设置；可选支付平台及促销参数支持通过本地 API 传入。

任务操作：`POST /api/accounts/<id>/extract-link/refresh`、`/cancel`、`/qr`。已保存 CDK 的任务发送 `{}`，一次性 CDK 任务发送 `{"cdk":"原提链CDK"}`。二维码接口使用上游 `image/png` 响应，返回 `image_url_png` 的 PNG data URL；不会把租户会话 token 交给浏览器。接口响应禁止缓存。

## 状态与恢复

创建批次使用 `POST /api/link-cdk/session` 换取租户会话，后续通过 `X-Link-CDK-Session` 请求上游。任务完成后尽力注销该会话；刷新、取消和二维码操作重新获取会话。只读轮询遇到 401 会更新会话，429 或临时服务错误可在超时窗口内重试读取；批次创建 POST 不会自动重试。

上游 `batch_id` 保存为账号的 `extract_link_task_id`，创建响应中的 `job_id` 保存为 `extract_link_job_id`。初次绑定账号时验证完整索引集合或唯一邮箱，后续按任务 ID 更新，避免响应乱序造成结果串号。批次部分成功时，后续查询失败不会覆盖已完成账号。

上游任务状态映射：`queued → queued`、`running → running`、`done → success`、`error → failed`、`cancelled → stopped`。支付状态单独保存，**Checkout 成功不表示已付款或已开通 Plus**；可继续手动刷新支付状态。当前接入不提供上游支付配置管理表单。

提交超时、成功响应缺字段、批次结束但缺少部分账号结果时，账号进入“待核实”。保留已经拿到的批次 / 任务 ID，刷新原任务核对；重启或经过很长时间也不会自动重新提链，避免重复扣次。没有取得任何上游 ID 时，需要先向服务商核对受理情况；本地没有自动解除此保护或自动重提功能。存在进行中或可恢复任务时，不能删除供应商、修改 API 地址 / 类型，或删除、替换其 CDK。

## 完整日志

所有提链方式（Legacy、Lumen Flow、UPI-GIT5）都会把完整过程写入 `注册日志/extract-link-<账号ID>.log`：

- 入队：是否入队、供应商与地址、提链类型、CDK、账号 AT、入口代理；未入队也会记录原因。
- 请求：`HTTP →` 行记录方法、完整 URL、请求头（含 `X-Link-CDK-Session` 等）与请求体；`HTTP ←` 行记录状态码、响应头、响应体与耗时。
- 过程：Legacy 的每个 SSE 事件与事件数据、Lumen 的受理与轮询状态、UPI-GIT5 的租户会话、批次 ID、账号与 `job_id` 绑定、每次批次进度与终态。
- 结果：每个账号的终态、链接等产物（二维码只记体积）、错误与 uncertain 判定。

查看方式：任务中心对应任务点「查看日志」，或直接读取文件；`GET /api/tasks/<任务ID>/log` 返回该文件内容。

开关（配置页「提链」或 `.env`）：`EXTRACT_LOG_ENABLED`、`EXTRACT_LOG_CREDENTIALS`（默认 `True`，明文记录提链 CDK、账号 AT、租户会话 token 与入口代理）、`EXTRACT_LOG_VALUE_LIMIT`（默认 8000）、`EXTRACT_LOG_MAX_BYTES`（默认 5000000，超出后轮转为 `.1`）。日志按账号追加，不覆盖历史；**日志文件本身等同于凭据，请限制目录权限。** 上游 CDK 校验接口（`/api/extract-link/cdk*`）不绑定账号，因此不写这类日志。

## 验证范围

已使用隔离 SQLite 数据库和模拟 HTTP 验证：真实客户端到批次后台执行的完整链路、单账号、多个账号一次提交、乱序映射、任务缺失、部分成功、会话更新、限流、提交不确定性、队列释放、刷新取消、二进制二维码、鉴权、CDK 归属、凭据脱敏、提链完整日志（明文凭据与脱敏开关、任务中心读取），以及现有供应商兼容性。

没有使用真实 CDK、真实账号 AT 或可用代理请求上游。OpenAPI 将 `Job.result` 定义为自由对象，未约定实际链接字段；本地兼容常见链接 / 复制字段，并提供独立二维码接口。实际上游结果字段、代理连通性和支付流程仍需用测试账号实测确认。
