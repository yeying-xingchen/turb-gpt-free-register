# UPI 支付与 Plus 开通

## 一键开通 Plus

管理员在现代或旧版账号列表中勾选账号（支持跨页），点击「开通 Plus」。在同一窗口选择提链服务商及提链 CDK、支付平台及支付凭据，可分别添加多个提链和支付方案，并用上移、下移调整尝试顺序（每类最多 20 项）。每项独立选择平台和凭据，支持同平台不同 CDK；完全相同的配置自动去重。可在「成功后转入分组」选择已有分组，再点击「开始开通 Plus」。默认「不自动转移」，无需手工复制链接或再次提交支付。

提链明确失败或未入队时自动尝试下一个提链方案；成功后使用该链接，依次尝试支付方案。支付明确拒收，或原订单已确认失败、过期、取消、释放后，才会切换到下一个支付方案；支付回退不重复提链。每个候选最多尝试一次，全部失败后显示「候选已用尽」。进行中的订单、网络超时、查询失败、提交结果未知、仍在核验或退款结果需核对时暂停回退，保留原任务。支付成功但 Plus 尚未生效只继续核验套餐，不再付款。进度显示当前候选序号；临时凭据仍只保留在内存中。

选择目标分组后，只有后台核验真实 Plus 套餐成功的账号才会自动转入该分组；失败、待核实和跳过的账号保留原分组。成功状态与转组在同一事务中保存，账号列表会自动更新。若目标分组在任务执行期间被删除或重命名，任务会提示重新选择分组，保留原分组；重新开通可选择有效分组继续核验，不会重复支付。目标分组随后台任务保存，关闭窗口不会丢失；重新打开窗口继续任务时，以本次选择为准。

后台按账号执行：查询真实套餐和试用资格 → 提取零元 UPI 链接 → 自动提交支付并查询原订单 → 再次查询套餐。只有查到 Plus 才显示「开通成功」；支付完成但套餐尚未生效时显示「需核实」，可再次点击开通 Plus 继续核验，不会再次付款。当前支持 free 且具备 Plus 试用资格的账号；已有 Plus、缺少 AT、已归档或不存在的账号会单独跳过。

提链接入复用 Extract、Lumen Flow 和 UPI-GIT5，支付平台与下表一致。UPI-GIT5 需要填写该服务端可访问的入口代理；Lumen 可选填代理。仅复用本开通流程生成、配置一致且仍有效的零元 UPI 链接；以前手动生成、其他服务商或配置生成、已过期及非 UPI 的旧链接，会按本次选择重新提链。若提链平台自身已受理支付，则继续核对该平台原任务，不另向支付平台提交。

任务在服务器执行，刷新或关闭页面后继续；账号列表自动显示排队、资格检查、提链、支付、套餐核验和最终结果。最多同时处理 4 个账号，队列上限 500。提链最多跟踪 30 分钟，支付最多跟踪 10 分钟；跟踪超时保留原任务供继续核对，不代表上游已取消。支付成功后会核验套餐最多 6 次，间隔 10 秒。

重复点击不会重复入队，进行中的开通任务禁止其他手动提链覆盖；已知支付任务继续查原订单。提交结果未知且没有任务 ID 时暂停，需通过「查询支付」或上游平台核对；不会自动重发付款。服务重启后标为「已中断」，重新选择原平台和原凭据点击「开通 Plus」继续核对；不可确认的旧提链也不会直接重提。临时提链和支付凭据只保留在后台任务内存，不写入开通记录或浏览器存储。现有提链 CDK 管理中的已保存凭据不受影响。

## 单独提交支付

在现代或旧版 WebUI 的账号列表中，先完成 UPI 提链，勾选账号，点击「提交支付」，选择平台和验证方式。支持跨页选择；前端逐个账号提交并显示结果。未提链成功、非 UPI 或缺少 Stripe HTTPS 支付页的账号会单独提示错误。

| 平台 | 默认地址 | 提交信息与认证 |
| --- | --- | --- |
| Astra Scan Workbench（默认） | `https://scan-qr.hixinghai.com/api/v1` | 支付 CDK；发送 UPI 链接和邮箱，**不发送账号 AT** |
| masi | `https://masi.cc.cd` | 支付 CDK；发送链接、邮箱、**所选账号完整 AT**，使用 `capacity_priority` 派单 |
| UPI OrderHub | `https://upi.xxsyun.xyz/api/v1` | Bearer API Key 或数字雇主账号登录；发送链接和**所选账号完整 AT**，使用 `auto` 派单 |

## OrderHub 连接与登录

配置页「支付提交」可以修改连接地址。提交窗口有两种验证方式：

- **API Key**：在平台账号中心生成 `ohk_…` 密钥后输入。后端直接使用 `Authorization: Bearer …`，保留本地请求的 `cdk` 参数名以便已有调用方接入。真实 CDK 用于在平台网页兑换额度；关闭旧 CDK 登录后，不能把真实 CDK 当登录凭据。
- **数字雇主账号**：输入 6–16 位纯数字账号，保留前导零；密码为 8–20 位，必须含大写字母、小写字母和特殊符号。点击登录后，密码输入框立即清空。上游 Cookie 仅保留在服务器内存，浏览器 HttpOnly 会话 Cookie 只包含本地随机句柄。

注册、管理员激活、兑换 CDK、生成或轮换 API Key，请通过窗口中的「平台账号中心」链接操作；API Key 本身没有账号管理、兑换或密钥管理权限。未激活、会话失效等平台错误会在窗口显示，不会假定获得额度。

本地数字账号会话最长保留 8 小时，退出时清除；服务重启后需要重新登录。此会话存于当前服务进程，多进程部署可使用 API Key 或保持会话路由一致。关闭支付窗口停止本地轮询，但不会退出数字账号或取消远端订单。

## 凭据和恢复

支付 CDK、OrderHub API Key、提链 CDK 和本站账号兑换 CDK 相互独立。支付凭据不写入配置、账号记录或浏览器持久存储；仅在当前支付窗口内存保留，关闭后清除；查询需重新输入原凭据。登录模式使用原雇主账号会话。

**例外：本地完整操作日志。** 每次提交/查询都会在 `注册日志/scan-payment-<账号ID>.log` 追加一段完整过程日志，默认**明文**记录支付 CDK、OrderHub API Key 和账号 AT（见下节「完整日志」）。日志是本地调试产物，按需清理；不写入账号记录和浏览器存储。

提交或查询后，窗口每 **5 秒**串行查询处理中任务；到达终态、查询失败或本地跟踪超过 10 分钟时停止。可随时重新打开「查询支付」。查询沿用原提交平台和地址，不随当前平台选择或配置改动改变。

- HTTP 202 表示提交结果待确认，不算成功创建；有任务 ID 时查询原任务。
- Astra 超时且没有任务 ID 时，可以显式使用原请求重试。请求发出前已保存原链接、邮箱和幂等键；重开窗口仍复用原请求。
- OrderHub 同样保留原幂等键；日志只保存 AT 的哈希，重试前检查账号 AT 是否仍相同。AT 已刷新时禁止改变原请求重试，请到原平台核对订单。
- masi 没有文档承诺的幂等键机制。超时、5xx 或结果未知时不会重提；请先在原平台核对订单与额度。
- 已保存任务 ID 的请求不会因再次点击提交而创建新单；活动订单和未知结果不能切换平台、凭据或链接再付。
- 曾经结果未知的提交，即使后续原请求重试返回认证错误，也保留未知状态，避免误把认证拒绝当作首次未建单的证据。
- 首次确定拒收、没有任务 ID 的请求可以在解决原因后再次提交；原订单终结后可以提交新提炼链接。
- 查询失败不会把原任务标为失败或退款。动态支付链接仅展示平台返回的本轮期限和核验截止时间，不自行增加 5 分钟或 24 小时，也不根据倒计时推断付款结果。
- `legacy` 完成显示「已按原有 AT 变化规则完成结算」；OrderHub 完成显示按平台核验规则结算。都不会据此自动修改本站保存的账号套餐。

工人容量、额度、授权、AT 与 Checkout 归属由平台实时校验。批量 HTTP 200 仍检查每条 `created / failed / duplicated`，错误码如 `ACCOUNT_MISMATCH`、`INSUFFICIENT_QUOTA`、`TEAM_UNAVAILABLE` 会保留，不统一解释为链接过期。HTTP、重试等待时间及平台是否建单/扣额度的明确字段一并保留。

## 配置与存储

配置页「支付提交」提供 `SCAN_API_BASE`（Astra）、`MASI_API_BASE`、`ORDERHUB_API_BASE` 和 `SCAN_API_TIMEOUT`。默认地址已设置；连接配置见 [scan_api.py](../config/scan_api.py)，支持现有环境变量加载机制。仅接受 HTTPS API 地址；无支付凭据的全局配置项。

现有 SQLite 数据库增加 `scan_submissions` 表，保存原平台、请求快照、幂等键、任务 ID 与状态，并同步账号列表摘要。数据库记录仍不保存原始 CDK、API Key、AT、密码、上游 Cookie 或订单密钥，只保存匹配原凭据/AT 所需的摘要；完整请求与响应只写在本地日志文件里。保留数据库以便进程重启后恢复原任务。

## 完整日志

「提交支付」的每一步都会写进 `注册日志/scan-payment-<账号ID>.log`（`注册日志` 目录即数据库日志目录，可用 `TURB_DATA_DIR` 调整）：

- 提交前：账号 ID、支付平台、平台地址与超时、本次凭据、幂等键、提链来源、复用的原快照。
- HTTP 层：`HTTP →` 行记录方法、完整 URL、请求头（含 `X-CDK-Code` / `Authorization`）、Cookie、请求体与超时；`HTTP ←` 行记录状态码、响应头、响应体与耗时。
- 结果：受理状态、任务 ID、`request_id`、duplicate/uncertain 判定、最终归类（created / duplicated / pending / failed / unknown）与批次汇总。
- 异常：`ERROR` 行记录异常类型、消息、平台状态码、错误码与 uncertain 标记；网络超时等未取得响应的情况单独标注。

查看方式：任务中心对应任务点「查看日志」，或直接读取文件；`GET /api/tasks/<任务ID>/log` 会返回该文件内容。

开关（配置页「支付提交」或 `.env`）：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `PAYMENT_LOG_ENABLED` | `True` | 关闭后不再写支付日志，业务不受影响 |
| `PAYMENT_LOG_CREDENTIALS` | `True` | `True` 按上文明文写入凭据；`False` 时 CDK/API Key/AT 渲染为 `[凭据已脱敏]` |
| `PAYMENT_LOG_VALUE_LIMIT` | `8000` | 请求体/响应体单字段截断字符数 |
| `PAYMENT_LOG_MAX_BYTES` | `5000000` | 单文件上限，超出后轮转为 `<文件>.1` |

日志按账号追加，重复点击、重试、查询都会续写，不覆盖历史。默认明文凭据是为了能直接核对与回放：**日志文件本身等同于凭据，请限制目录权限并避免随工单、截图或仓库外发。**

## 本地接口

接口复用 WebUI 登录鉴权，返回 `Cache-Control: no-store`。以下凭据均放在 JSON 请求体，不能放 URL：

- `POST /api/accounts/activate-plus`：`{account_ids:[7], extraction:{provider_id:1, cdk_id:2}, payment:{provider:"v1", cdk:"完整支付凭据", auth_mode:"key"}, success_group:"Plus 成品"}`。可选 `success_group` 为已有分组名（含默认分组），核验真实 Plus 后自动转入；空字符串明确关闭自动转组，省略或传 `null` 时继续使用该账号上次任务保存的选择（首次默认为不转移）。无效分组返回 HTTP 400，不提交开通任务。提链可用临时 `cdk` 替代 `cdk_id`，Legacy 环境 CDK 使用 `provider_id:0`；Lumen 可传 `proxy_url`，UPI-GIT5 必须传 `entry_proxies:["http://proxy.example:8080"]`。OrderHub 会话在 `payment` 中传 `provider:"orderhub", auth_mode:"session"`。返回 HTTP 202 与 `started/busy/skipped/failed` 数组及对应数量，仅表示排队受理。最终状态通过原账号列表和 `/api/accounts/plan-check-status` 的 `plus_activation_status/message/updated_at` 字段读取。
- `POST /api/accounts/scan-requests`：`{account_ids:[7], provider:"v1"|"masi"|"orderhub", cdk:"完整凭据", idempotency_key:"本次操作的稳定键"}`。默认 provider 为 v1。结果分别位于 `created`、`duplicated`、`pending`、`failed`、`unknown` 数组；`ok:true` 仅表示本批条目已处理。
- `POST /api/accounts/scan-requests/query`：`{account_ids:[7], cdk:"原凭据"}`；读取原平台任务，返回 `items`、`failed`、`unknown`。
- `GET /api/accounts/7/scan-request`：读取本地非敏感状态，不发送远端查询。
- `POST /api/payments/verify`：`{provider:"v1"|"masi"|"orderhub", cdk:"完整凭据"}`；验证额度，不创建任务。
- `POST /api/payments/orderhub/login`：`{username:"00123456", password:"ExamplePass1!"}`；返回安全用户字段并设置本地 HttpOnly 会话。
- `GET /api/payments/orderhub/session`：读取本地登录状态。
- `POST /api/payments/orderhub/logout`：`{}`；退出本地与远端会话。

`activate-plus` 的 `extraction`、`payment` 兼容原单对象，也可分别传按优先级排列的非空数组（各 1–20 项）；两类配置独立回退。例如：

```json
{
  "account_ids": [7],
  "extraction": [
    {"provider_id": 1, "cdk_id": 2},
    {"provider_id": 3, "cdk": "备用提链凭据"}
  ],
  "payment": [
    {"provider": "v1", "cdk": "首选支付凭据", "auth_mode": "key"},
    {"provider": "masi", "cdk": "备用支付凭据", "auth_mode": "key"},
    {"provider": "orderhub", "auth_mode": "session"}
  ],
  "success_group": "Plus 成品"
}
```

恢复任务时，候选中须包含原提链配置及原支付凭据；服务器会匹配当前原任务，从该项继续查询，无需把它移到列表首位。已尝试失败的前序候选不会因恢复而再次支付。原请求只有一个配置时保持既有行为。任一候选格式无效会在入队前返回 HTTP 400，避免后台只执行部分未验证配置。OrderHub 会话候选共用当前已登录的雇主会话。

OrderHub 会话模式在提交、查询或额度接口传 `auth_mode:"session"`，无需 `cdk`。脚本应保留本地 HttpOnly Cookie。API Key 模式用 `auth_mode:"key"`（默认）。

链接、邮箱及 AT 从所选账号读取，本地提交接口不接受手工覆盖这些字段，也不允许客户端指定价格和额度。建议脚本逐条请求并及时处理结果。

## 本地验证

```bash
.venv/bin/python -m pytest -q tests/test_plus_activation.py tests/test_scan_api.py tests/test_scan_api_clients.py tests/test_scan_payment_log.py tests/test_operation_log.py
node --test tests/test_plus_activation_ui.js tests/test_scan_payment_ui.js
node --check webui/static/extract-links.js
node --check webui/static/console.js
```

测试使用临时数据库和模拟网络，不会创建真实支付任务或消耗 CDK。
