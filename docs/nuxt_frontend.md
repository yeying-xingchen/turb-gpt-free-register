# Nuxt 前端

控制台已重写为 Nuxt 4、Vue 3 和 TypeScript。页面使用 Vue 组件和路由，直接对接现有 Flask JSON API，未加载历史控制台脚本。

## 安装与生产运行

需要 Node.js 22.12 或更高版本，推荐 Node.js 24 LTS。在项目根目录执行：

```bash
npm --prefix frontend ci
npm --prefix frontend run typecheck
npm --prefix frontend run build
.venv/bin/python web.py
```

也可以执行 `./webui.sh start`；缺少构建产物时脚本自动安装锁定依赖并构建。更新前端后运行 `./webui.sh build`，然后刷新浏览器。如果同时更新了 Python 路由，需要重启原 WebUI 进程。

构建目录为 `frontend/.output/public/`，Flask 提供其中的 HTML、`/_nuxt/` 资源和品牌图标。生产环境不需要另一个 Node 服务。已有数据库、运行锁、任务队列、配置和启动参数继续使用原后端。未构建时页面会返回 HTTP 503 并显示构建命令，API 仍可正常使用。

只有带内容哈希的资源长期缓存，入口 HTML 和业务 API 不缓存。部署应包含完整构建目录；更新时保留旧哈希资源一段时间可避免已打开的浏览器请求旧分块时出现 404。

## 本地开发

先启动 Flask 后端，再在另一个终端启动 Nuxt：

```bash
# 终端一，项目根目录
.venv/bin/python web.py --port 5000

# 终端二，项目根目录
npm --prefix frontend run dev
```

开发页面默认位于 `http://127.0.0.1:3000`。Nuxt 将 `/api` 代理到 `http://127.0.0.1:5000`，登录 Cookie 由浏览器同源携带。若后端端口不同：

```bash
NUXT_API_PROXY=http://127.0.0.1:8000 npm --prefix frontend run dev
```

`NUXT_API_PROXY` 只用于开发代理，填写后端源地址，不包含 `/api`。生产构建始终请求当前站点的 `/api`。

## 页面与能力

| 路由 | 内容 |
| --- | --- |
| `/` | 注册概览、注册数量/并发、任务分页、日志、手动验证码 |
| `/accounts` | 账号筛选分页、跨页选择、导入、分组、查活/套餐/额度、2FA、邮箱换绑、提链/支付/Plus、Agent 与导出 |
| `/tasks` | 统一任务进度、历史、按类型/状态/关键词筛选、完整详细日志，后端允许的暂停/恢复/取消，以及手动模式下的邮箱验证码提交 |
| `/mailboxes` | Outlook、API、IMAP、域名邮箱池及批量操作 |
| `/codex` | 账号授权状态、重试/停止、凭证文件归档与下载 |
| `/redemptions` | 兑换码、分组公开库存、交付记录 |
| `/providers` | 「提链供应商」与「支付平台」两个标签页：供应商/CDK 管理，以及四个支付平台的已保存 CDK 与额度查询 |
| `/settings` | 根据后端字段元数据生成的完整配置表单，分组、搜索、保存与辅助操作 |
| `/login` | 授权码登录与持久会话 |
| `/redeem` | 无需管理员登录的公开兑换、结果恢复及凭据下载 |

历史模板暂保留用于兼容排查，控制台现在支持三个入口：Nuxt 新前端（`/`）、旧版侧边栏（`/?ui=modern`）和历史版顶部导航（`/?ui=legacy`）。登录后每个管理页面的底部或顶部都提供另外两个入口；普通 `/` 始终进入 Nuxt，旧版偏好 Cookie 不会改变默认界面。

## 账号批量操作

`/accounts` 的批量操作按钮与旧版控制台一致，直接平铺在账号列表工具栏下方，按「检查与安全 / 订阅与支付 / Codex 与导出 / 整理账号」四组排在一块，不再使用需要展开的下拉菜单。未选择账号时全部禁用；点击后进入统一的账号操作弹窗，只有删除会二次确认后立即执行。同一套动作在行内「详情 / 操作」中仍可对单个账号使用。

## 账号套餐信息

`/accounts` 的「套餐与订阅」列与账号详情保持与旧版控制台一致的信息量，由 `frontend/app/components/accounts/PlanInfo.vue` 和 `frontend/app/utils/accounts.ts` 的 `accountPlanInfo()` 渲染：

- 套餐名统一归一化（`chatgptplusplan` → `Plus`）；付费套餐显示账期、币种、到期时间、续费时间和折扣。
- 套餐标签沿用旧版 pill 的原始小写名（`free`/`plus`/`pro`…）并带底色：已开通套餐与「free 且有优惠/可试用」为绿底，「free 但资格未知」为中性灰底，从未查询为虚线浅底。
- `eligible_promo_campaigns` 展开为逐条优惠摘要（如 `Plus：免费/1个月`），点击「优惠详情」打开完整活动字段弹窗。
- 套餐查询过程可见：排队中/查询中/查询失败（含失败原因和上次成功时间），并保留查询时间与网络路径（代理/直连回退）。
- free 且资格未知时显示「待查资格」，已确认可试用时显示「可试用 Plus」，避免把未知当成不可试用。
- 订阅挽留期（`subscription_became_delinquent_at` / `subscription_grace_period_end_at`）和订阅查询错误单独提示。

这些字段都是非敏感状态串，`/api/accounts` 的轻量列表按需返回（空值不返回）；密码、AT、2FA 密钥和 Agent 凭据仍然只在复制或导出时读取。

## 账号额度、用量与「银行重置」

`/accounts` 在「套餐与订阅」之后是「额度 / 用量」和「银行重置」两列：「额度 / 用量」把余额与限流窗口合并在一列，由 `frontend/app/components/accounts/QuotaUsage.vue`（余额用 `accountQuotaInfo()`，两条进度条用 `accountUsageInfo()`）渲染；「银行重置」由 `QuotaInfo.vue`（`field="reset"`）渲染。数据来自参考「额度.har」实现（外加同族 `wham/usage`）的三个 chatgpt.com 只读接口：

- 「额度 / 用量」第一行 = `GET /backend-api/accounts/{account_id}/remaining_balance`。优先展示服务端返回的余额原值（字符串不重新格式化）与币种，`unlimited` 为真时显示「不限量」。
- 「额度 / 用量」下面两条进度条 = `GET /backend-api/wham/usage` 的 `rate_limit.primary_window` / `secondary_window`，填充比例是**已用**百分比（`used_percent`），条上同时给出紧凑倒计时（如「4 小时后重置」，完整时间在悬停提示里）。窗口按 `limit_window_seconds` 的真实长度归类并生成进度条标签：短窗口写「5h」，长窗口写「周」，团队套餐的整月窗口写「月」，不假设 primary 一定是 5 小时窗口。100% 显示「已用尽」并标红，≥80% 标黄，`used_percent=0` 且剩余重置时间仍是整个窗口（`started=false`）时进度条留空并标为「0%」（未使用），没有数据时进度条留空并显示「无数据」。`rate_limit.limit_reached` / `rate_limit_reached_type` 与 `usage_plan_type` 放在悬停提示里。
- 「银行重置」= `GET /backend-api/wham/rate-limit-reset-credits`，展示 `available_count` 张可用重置券与最近一张的到期时间；`applicable_available_count` 与可用张数不同时补一行「可应用 N 张」（未触限时上游返回 0）。悬停提示会列出每张券的状态与到期时间。
- 三个接口都不消耗额度、不改账号状态。任一接口失败只影响自己那一部分并保留上次成功的数值，`quota_error` / `reset_credits_error` / `usage_error` 在提示里给出；排队中/查询中显示「查询中…」，从未查询显示「未查询」，查过但没有可识别数值显示「无数据」。
- `remaining_balance` 返回的是**剩余**值：`0` 表示没有剩余，**不是**「一次都没用过」。`wham/usage` 的 `credits.has_credits` 能把两种 0 分开：`has_credits=false` → 余额行显示「无额度权益」（本来就没有这类额度），`has_credits=true` 且余额为 0 → 提示「余额已用完」；字段缺失时提示保持「可能已经用完，也可能该账号没有这类额度」。`重置券 0 张` 表示没有可用券（未发放、已用完或已过期）。
- 余额端点失败或字段不可识别时，用 `wham/usage` 的 `credits.balance` 兜底，提示里会标注「余额数值来自 wham/usage 的 credits.balance」。
- 账号详情弹窗（`OperationsModal.vue`）保留四个独立区块（额度余额 / 银行重置券 / 5 小时窗口 / 周·月窗口），`UsageInfo.vue` 在详情里也画同样的进度条。

刷新方式有两种，结果都写回账号记录（`quota_*` / `reset_credits_*` / `usage_*` 字段，原始响应另有 `quota_result_json`）：

- 「查套餐」在同一个会话里顺带请求这三个接口（`core/chatgpt_plan.py` 调用 `core/chatgpt_quota.py`），因此一次查套餐就能同时刷新套餐、额度、用量和重置券。
- 工具栏「查额度」、行内「查额度」和账号详情的「查询额度、用量与重置券」走 `core/quota_check_service.py` 独立队列（任务中心类型 `quota_check`，标签「查询额度与用量」），接口为 `POST /api/accounts/check-quota[-bulk]`，可用 `QUOTA_CHECK_*` 调整并发与节流，网络策略复用 `PLAN_CHECK_PROXY_*`。

## 账号兑换状态

`/accounts` 的「状态」列第一行固定显示该账号是否已被兑换码领取（`redeemed` / `unredeemed`）。数据来自 `redeem_claims` 表：`/api/accounts` 的列表项始终带 `redeemed` 布尔值，已兑换时额外返回领取时间 `redeemed_at`（鼠标悬停徽章可见），但不返回 CDK、密码或 Token。

- 账号列表默认隐藏已被兑换码领取的账号（`redemption=unredeemed`）；工具栏的「显示已兑换账号」按钮切到不限制兑换状态，按钮随后变成「隐藏已兑换账号」，可随时回到默认视图。旧版控制台账号列表的「显示已兑换」开关行为一致，并在邮箱旁用「已兑换」标记区分。
- 高级筛选保留「兑换状态」（已兑换 / 未兑换）；`redemption=redeemed|unredeemed` 作为 SQL `EXISTS (SELECT 1 FROM redeem_claims …)` 条件下推到 `COUNT/LIMIT/OFFSET`，分页总数与列表一致。`/api/accounts/plan-check-status` 也接受同一参数，保证列表与轻量状态轮询看到的账号集合完全一致。
- 「按邮箱选中」沿用同一筛选条件；默认隐藏已兑换账号时，被兑换状态筛掉的邮箱与其他筛选一致地计入“未匹配”，需要匹配已兑换账号时先点「显示已兑换账号」。

## AT 过期与查活状态筛选

高级筛选新增「AT 状态」和「查活状态」两个下拉，账号列表「状态」列在 AT 过期时额外显示「AT · 已过期」徽章（旧版控制台在查活状态下方显示「AT: 已过期」）。

- `at_status=expired|valid|unknown`：`expired` 判定顺序与列表徽章完全一致——先看套餐查询写回的 `token_expired`，再看 `token_expires_at` 是否已到点（用 `julianday` 比较，时间流逝会让旧结果自然失效），最后回退到直接解析 `access_token` 的 JWT `exp`（SQLite 自定义函数 `account_at_expired`）。因此从未查过套餐的新账号也能被筛出来，`unknown` 只表示既没有过期标记、也没有可用 AT 的账号。
- `live_status=failed|success|deactivated|checking|cancelled|never`：按 `live_check_status` 过滤。`failed` 只表示查活失败（密码/验证码错误、限流、网络阻断等），不含 `deactivated`（明确废号）和 `cancelled`（用户取消）；`never` 匹配状态为空的账号。
- 两个条件都下推到 `COUNT/LIMIT/OFFSET`，分页总数、列表、`/api/accounts/ids`（全选筛选结果）、`/api/accounts/lookup`（按邮箱选中）和 `/api/accounts/plan-check-status`（轻量轮询）使用同一口径，轮询不会把筛选外的账号合并回列表。
- 无法识别的取值命中空集合而不是放宽成全部账号；只有 `at_expired` 布尔结论会下发到前端，`access_token` 本身仍只在复制或导出时读取。
- 查活成功换发新 AT 时会同步刷新 `token_expired` / `token_expires_at`，新注册或导入的账号也会在写入时记录过期时间，避免刚刷新过 AT 的账号仍被筛成过期。

## 全选与自动分批

账号页支持两种「全选」，都只收集账号 ID，不再把整库账号对象塞进浏览器内存（页面用 `selectedIds` 保存选中 ID，`selectedRows` 只缓存确实加载过的行，操作弹窗只预览前 200 行）：

- 「选择全部筛选结果（N）」：按当前筛选条件（归档/套餐/Codex/2FA/分组/兑换/日期/关键词/AT/查活状态）收集全部命中账号，N 取该筛选的总数。旧版控制台按钮同名，沿用 `getAccountLookupFilters()` 的同一份筛选快照并补上列表关键词。
- 「全选所有账号（M）」：忽略全部筛选，包含已兑换与已归档账号，M 是账号库总数。
- 两者都按页读取 `GET /api/accounts/ids`（`scope=filtered|all`，`page_size` 最大 5000，只返回 ID 不返回 payload），因此选择数量不受 5000 个上限限制；旧版控制台最多翻 4000 页，避免服务端 `total` 异常时死循环。
- 旧版控制台保留「本页全选」复选框的原有语义（只作用于当前页并保留其他页选择）；「按邮箱选中」也不再限制单次 5000 个邮箱，而是按 5000 一批查询后合并。

后端每个批量接口都有单次上限（查活/套餐/2FA/换邮箱/Agent/上传/补跑/停止/提链/开通 Plus/支付为 500，ZIP 下载为 1000，备注/分组/归档/删除/密文读取为 5000）。选中数量超过上限时两个前端都会自动分批提交：

- `frontend/app/utils/accounts.ts` 的 `accountBatchLimits` / `accountBatchLimit()` / `chunkAccountIds()` / `mergeAccountResults()` 是 Nuxt 端的唯一来源；`tests/test_nuxt_account_selection.js` 固定这些纯函数，`tests/test_webui_account_ids.py` 直接解析该文件并逐个接口验证后端仍接受对应批次大小。
- 操作弹窗会显示「将自动分 N 批执行并汇总结果」和实时进度；合并结果时数组合并、`*_count` 按合并后的数组长度重算，`id`/`*_id`/分页字段只保留首个值，避免拼出错误的账号或任务 ID。某一批硬失败时会提示「已提交 x/y 批」，方便刷新列表核对已生效的部分。
- 导出「邮箱」列不再依赖前端缓存的行，而是通过 `/api/accounts/secret-bulk` 的 `field=email` 按批读取。
- 旧版控制台使用同名的 `ACCOUNT_BATCH_LIMITS` / `chunkAccountIds()` / `mergeBatchResults()` / `postAccountBatches()`，`webui/static/extract-links.js` 的提链与开通 Plus 弹窗也按 500 分片。
- ZIP 下载（Agent / CPA 凭据）是单文件接口，无法合并，仍要求单次不超过 1000 个账号并给出明确提示；「提链任务管理」等只针对单账号的操作也保持一次一个账号。

## 代码组织

- `frontend/app/pages/`：页面路由。
- `frontend/app/layouts/default.vue`：桌面与移动端导航。
- `frontend/app/components/`：公共界面、账号操作、套餐信息及任务组件。
- `frontend/app/composables/`：统一 API 客户端、通知、可自动清理的轮询。
- `frontend/app/utils/`：账号操作与兑换恢复逻辑。
- `frontend/app/assets/css/main.css`：全局视觉样式和响应式布局。
- `webui/frontend.py`：Flask 静态产物托管与明确的管理页面路由。
- `webui/auth.py`：兼容原表单登录，同时提供 JSON 登录、登出和会话查询。

Nuxt 使用 `ssr: false` 生成静态 SPA；浏览器加载后获取业务数据。管理页面由 Flask 鉴权及客户端路由守卫保护，业务 API 继续执行后端鉴权。授权码不保存到浏览器持久存储，账号密码/Token 仅在用户请求复制或导出时读取。

任务中心的 `/api/tasks/<task_id>/log` 和 `/api/tasks/logs` 返回完整日志内容，不再截取注册日志尾部或账号状态事件数量。查活、2FA、邮箱换绑任务会读取原服务写入的详细日志文件；账号列表提交这些后台任务后，会自动定位最新任务并打开同一个完整日志弹窗，支持自动刷新、复制和下载。

## 任务中心筛选

`/tasks` 顶部的筛选栏对进行中和历史任务同时生效：

- 任务类型来自 `/api/tasks/active` 返回的 `filters.job_types`（`codex_retry` 由注册任务 payload 推导，与账号侧投影共用同一套标签），状态取统一状态分组（`failed` 覆盖 `deactivated`、`not_activated` 等来源状态）。
- 关键词按邮箱或任务 ID（`registration-12` / `account-7`）模糊匹配，`%`、`_` 和 `\` 按字面量转义。
- 筛选条件下推到 `registration_jobs` 与 `account_tasks` 的 `COUNT/LIMIT/OFFSET`，分页总数、状态统计和列表始终同一口径；旧版侧边栏的活跃任务徽章仍用 `global_status_counts` 的全局口径。未知类型或状态返回 400，而不是静默返回空列表。
- 表单值在点击「应用筛选」后才生效，轮询刷新不会打断正在输入的关键词；旧版控制台的任务中心提供同一组筛选。

公开兑换沿用 `redeem_recovery_v2:<SHA-256(CDK)>` 的标签页存储索引，并兼容旧版恢复记录。存储中只有请求 ID 与完成状态，没有兑换码或账号凭据。刷新后先恢复原批次，只有明确点击“继续兑换下一批”才创建新请求。HTTP 局域网环境同样支持哈希索引和安全随机 ID。浏览器禁止标签页存储时阻止提交，避免丢失恢复信息。

## 验证

```bash
npm --prefix frontend run typecheck
npm --prefix frontend run build
node --test tests/test_nuxt_plan_info.js
.venv/bin/python -m pytest -q tests/test_nuxt_frontend.py tests/test_webui_auth.py tests/test_webui_assets.py tests/test_redeem.py tests/test_redeem_delivery.py tests/test_redeem_quantity.py tests/test_task_center_api.py tests/test_account_password_privacy.py tests/test_plan_promo_details.py
```

套餐展示逻辑由 `tests/test_nuxt_plan_info.js` 直接加载 TypeScript 工具模块校验，不依赖构建；`tests/test_plan_promo_details.py` 额外确认轻量列表返回套餐元信息且不泄露凭据。

路由与登录测试使用临时静态目录，不依赖生产构建。浏览器集成测试需要构建产物、Python Playwright 与 Chromium：

```bash
.venv/bin/python -m playwright install chromium
RUN_NUXT_BROWSER_TESTS=1 .venv/bin/python -m pytest -q tests/test_nuxt_browser.py
```

浏览器测试使用临时数据库和随机本地端口，检查登录、导航、账号导入、套餐与优惠展示、兑换交付/恢复、移动端和错误状态；不执行真实注册、授权或支付。
