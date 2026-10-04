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
| `/accounts` | 账号筛选分页、跨页选择、导入、分组、查活/套餐、2FA、邮箱换绑、提链/支付/Plus、Agent 与导出 |
| `/tasks` | 统一任务进度、历史、完整详细日志，后端允许的暂停/恢复/取消，以及手动模式下的邮箱验证码提交 |
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

## 账号兑换状态

`/accounts` 的「状态」列第一行固定显示该账号是否已被兑换码领取（`redeemed` / `unredeemed`）。数据来自 `redeem_claims` 表：`/api/accounts` 的列表项始终带 `redeemed` 布尔值，已兑换时额外返回领取时间 `redeemed_at`（鼠标悬停徽章可见），但不返回 CDK、密码或 Token。

- 高级筛选新增「兑换状态」（已兑换 / 未兑换）；`redemption=redeemed|unredeemed` 作为 SQL `EXISTS (SELECT 1 FROM redeem_claims …)` 条件下推到 `COUNT/LIMIT/OFFSET`，分页总数与列表一致。
- 「按邮箱选中」沿用同一筛选条件；被兑换状态筛掉的邮箱与其他筛选一致地计入“未匹配”。

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
