# -*- coding: utf-8 -*-
"""
配置读写层（供 WebUI /api/config 使用）。

设计原则：
    1. 白名单：只暴露"运行时安全"的开关/数值/默认值，协议级常量
       （client_id / scope / sentinel 版本等）一律不开放，避免一改就废号。
    2. 所有 WebUI 可编辑项统一写入项目根 `.env`，不再修改 `config/*.py`。
    3. `config/*.py` 只保留默认值；运行时通过 config.env_loader 用 `.env` 覆盖。
    4. 读取时优先 `.env`，缺失时回退解析 `config/*.py` 默认值。
"""
import ast
import math
import os
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"
EXPLICIT_EMPTY_LIST_KEYS = {
    "PROXY_POOL",
    "PLAN_CHECK_PROXY",
    "PAYMENT_METHOD_CHECK_PROXIES",
    "DJB_PROXIES",
    "DJB_EXIT_PROXIES",
    "PAY153_ENTRY_PROXIES",
    "PAY153_EXIT_PROXIES",
    "MOMO_ACTIVATION_ENTRY_PROXIES",
}


# ============================================================
# 白名单：每个可编辑项声明它在哪个文件、键名、类型、分组、说明
# type 决定前端控件 + 写回时的字面量格式：
#   bool   -> True/False
#   int    -> 整数
#   str    -> 带引号字符串
#   list_str_multiline -> 多行字符串列表（PROXY_POOL 专用，整块替换）
# ============================================================

EDITABLE_FIELDS = [
    # ---- WebUI 授权 ----
    {
        "key": "WEBUI_AUTH_CODE", "file": "codex.py", "type": "str", "group": "WebUI 授权",
        "label": "WebUI 授权码", "help": "仅保存在 .env（WEBUI_AUTH_CODE），避免出现在进程命令行中；保存后重启 WebUI 生效",
        "storage": "env", "secret": True,
    },
    {
        "key": "WEBUI_SESSION_SECRET", "file": "codex.py", "type": "str", "group": "WebUI 授权",
        "label": "Session 签名密钥", "help": "可选，保存在 .env（WEBUI_SESSION_SECRET）；不填则从固定授权码派生，修改授权码会使已有登录失效",
        "storage": "env", "secret": True,
    },
    # ---- 功能开关 ----
    {
        "key": "ENABLE_CODEX_AUTO", "file": "codex.py", "type": "bool", "group": "功能开关",
        "label": "启用 Codex OAuth", "help": "注册成功后自动跑 Codex 授权（全新session+接码），落盘 codex-邮箱.json",
    },
    {
        "key": "REGISTRATION_DRIVER", "file": "roxybrowser.py", "type": "str", "group": "注册方式",
        "label": "注册驱动", "help": "默认推荐 roxy；protocol=纯协议，容易封号不建议；roxy=RoxyBrowser；cloak=CloakBrowser；browser_use=Browser Use Cloud+Playwright；skyvern=Skyvern Browser Sessions+Playwright",
    },
    {
        "key": "AUTO_PLAN_CHECK_AFTER_REGISTER", "file": "register.py", "type": "bool", "group": "注册方式",
        "label": "注册后自动查套餐", "help": "注册成功后自动入队查询套餐/Plus 资格；关闭后仅保存账号，不自动查套餐",
    },
    {
        "key": "LIVE_CHECK_DRIVER", "file": "playwright.py", "type": "str", "group": "查活/Playwright",
        "label": "查活驱动", "help": "auto=协议遇到边缘错误后自动切本地 Playwright；protocol=仅协议；playwright/cf/cloudflare=直接浏览器查活并尝试 CF challenge",
    },
    {
        "key": "PLAYWRIGHT_HEADLESS", "file": "playwright.py", "type": "bool", "group": "查活/Playwright",
        "label": "Playwright无头", "help": "True=后台无头 Chromium；False=显示浏览器窗口，便于调试",
    },
    {
        "key": "PLAYWRIGHT_EXECUTABLE_PATH", "file": "playwright.py", "type": "str", "group": "查活/Playwright",
        "label": "Chromium路径", "help": "可选。留空使用 Playwright 自带 Chromium；部署后请先执行 playwright install chromium",
    },
    {
        "key": "PLAYWRIGHT_LOCALE", "file": "playwright.py", "type": "str", "group": "查活/Playwright",
        "label": "浏览器语言", "help": "默认 en-US；应与出口地区保持一致",
    },
    {
        "key": "PLAYWRIGHT_TIMEZONE_ID", "file": "playwright.py", "type": "str", "group": "查活/Playwright",
        "label": "浏览器时区", "help": "可选，例如 Asia/Tokyo；留空使用系统时区",
    },
    {
        "key": "PLAYWRIGHT_TIMEOUT", "file": "playwright.py", "type": "int", "group": "查活/Playwright",
        "label": "页面操作超时(秒)", "help": "Playwright 元素操作默认超时",
    },
    {
        "key": "PLAYWRIGHT_NAVIGATION_TIMEOUT", "file": "playwright.py", "type": "int", "group": "查活/Playwright",
        "label": "导航超时(秒)", "help": "登录页/authorize 页面导航超时",
    },
    {
        "key": "PLAYWRIGHT_OTP_TIMEOUT", "file": "playwright.py", "type": "int", "group": "查活/Playwright",
        "label": "Playwright OTP等待(秒)", "help": "浏览器注册/查活等待邮箱验证码的上限",
    },
    {
        "key": "PLAYWRIGHT_LIVE_CHECK_TIMEOUT", "file": "playwright.py", "type": "int", "group": "查活/Playwright",
        "label": "Playwright查活超时(秒)", "help": "等待浏览器登录态 /api/auth/session 的上限",
    },
    {
        "key": "PLAYWRIGHT_EDGE_CHALLENGE_WAIT", "file": "playwright.py", "type": "float", "group": "查活/Playwright",
        "label": "边缘挑战等待(秒)", "help": "403/挑战页出现后让真实 Chromium 执行页面脚本的等待时间，默认 5 秒，程序会限制上限",
    },
    {
        "key": "PLAYWRIGHT_EDGE_RETRY_ATTEMPTS", "file": "playwright.py", "type": "int", "group": "查活/Playwright",
        "label": "边缘重试次数", "help": "首页、登录页和 session 边缘响应的最大尝试次数，程序限制为 1-4",
    },
    {
        "key": "PLAYWRIGHT_KEEP_BROWSER_OPEN", "file": "playwright.py", "type": "bool", "group": "查活/Playwright",
        "label": "保留Playwright浏览器", "help": "调试时开启；任务结束后不自动关闭浏览器",
    },

    # ---- CloakBrowser ----
    {
        "key": "CLOAK_HEADLESS", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak无头", "help": "True=无头运行；False=显示浏览器窗口",
    },
    {
        "key": "CLOAK_HUMANIZE", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak人工行为", "help": "启用 CloakBrowser humanize 鼠标/键盘/滚动行为",
    },
    {
        "key": "CLOAK_GEOIP", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak按出口定位", "help": "按当前出口 IP 自动匹配时区/语言/WebRTC IP；支持显式代理、系统代理/VPN",
    },
    {
        "key": "CLOAK_LOCALE", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak语言", "help": "留空自动；日本可填 ja-JP，美国 en-US",
    },
    {
        "key": "CLOAK_TIMEZONE", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak时区", "help": "留空自动；日本可填 Asia/Tokyo，美国 America/Los_Angeles",
    },
    {
        "key": "CLOAK_USE_PROXY", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak使用代理", "help": "把本项目传入或代理池抽取的代理传给 CloakBrowser",
    },
    {
        "key": "CLOAK_LICENSE_KEY", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak License", "help": "Pro license；留空使用免费 binary；仅保存在 .env，不会在配置列表明文返回", "storage": "env", "secret": True,
    },
    {
        "key": "CLOAK_FINGERPRINT_SEED", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak指纹Seed", "help": "留空每次随机；固定值可保持同一指纹",
    },
    {
        "key": "CLOAK_USER_DATA_DIR", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak用户目录", "help": "留空使用临时上下文；填写路径则持久化 cookies/cache",
    },
    {
        "key": "CLOAK_SELENIUM_TIMEOUT", "file": "cloakbrowser.py", "type": "int", "group": "CloakBrowser",
        "label": "Cloak超时", "help": "页面和元素等待超时时间，秒",
    },
    {
        "key": "CLOAK_KEEP_BROWSER_OPEN", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "保留Cloak浏览器", "help": "调试时开启，任务结束后不自动关闭",
    },

    # ---- Browser Use Cloud ----
    {
        "key": "BROWSER_USE_API_KEY", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "Browser Use API Key", "help": "保存在 .env（BROWSER_USE_API_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "BROWSER_USE_PROXY_COUNTRY_CODE", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "代理国家代码", "help": "两位国家码，如 jp/us/sg；配合 Browser Use 内置 residential proxy",
    },
    {
        "key": "BROWSER_USE_USE_PROXY", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "使用内置代理", "help": "True=连接参数带 proxyCountryCode；False=不强制传国家代理参数",
    },
    {
        "key": "BROWSER_USE_PROFILE_ID", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "Profile ID", "help": "可选。填写则复用 Browser Use profile 的 cookies/localStorage；批量建议留空",
    },
    {
        "key": "BROWSER_USE_CDP_BASE", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "CDP 地址", "help": "默认 wss://connect.browser-use.com",
    },
    {
        "key": "BROWSER_USE_TIMEOUT", "file": "browser_use.py", "type": "int", "group": "Browser Use",
        "label": "操作超时(秒)", "help": "Playwright 默认操作超时",
    },
    {
        "key": "BROWSER_USE_SESSION_TIMEOUT", "file": "browser_use.py", "type": "int", "group": "Browser Use",
        "label": "云端keepAlive(分钟)", "help": "传给 Browser Use connect URL 的 timeout/keepAlive；程序会自动限制到 1-240，建议 240",
    },
    {
        "key": "BROWSER_USE_FAST_MODE", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "快速模式", "help": "减少 Browser Use 额外等待和 humanize 延迟；建议开启，异常排查时可关闭",
    },
    {
        "key": "BROWSER_USE_LOG_TIMING", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "耗时日志", "help": "打印 Browser Use 各阶段耗时：连接、打开页面、邮箱、OTP、手机、callback",
    },
    {
        "key": "BROWSER_USE_KEEP_BROWSER_OPEN", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "保留远端会话", "help": "调试时可不主动 browser.close()；默认 False",
    },
    {
        "key": "BROWSER_USE_START_URL", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "起始 URL", "help": "默认 https://chatgpt.com/auth/login",
    },

    # ---- Skyvern Cloud Browser ----
    {
        "key": "SKYVERN_API_KEY", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "Skyvern API Key", "help": "保存在 .env（SKYVERN_API_KEY），用于创建 Skyvern Browser Session",
        "storage": "env", "secret": True,
    },
    {
        "key": "SKYVERN_API_BASE", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "API 地址", "help": "默认 https://api.skyvern.com",
    },
    {
        "key": "SKYVERN_BROWSER_SESSION_TIMEOUT", "file": "skyvern.py", "type": "int", "group": "Skyvern",
        "label": "Session 超时(分钟)", "help": "创建 Skyvern Browser Session 时传入的 timeout",
    },
    {
        "key": "SKYVERN_BROWSER_PROFILE_ID", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "Browser Profile ID", "help": "可选，复用 Skyvern browser profile",
    },
    {
        "key": "SKYVERN_PROXY_LOCATION", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "代理地区", "help": "可填 jp/us/gb 等简写；会自动转为 Skyvern 枚举，如 jp→RESIDENTIAL_JP；留空不传",
    },
    {
        "key": "SKYVERN_BROWSER_TYPE", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "浏览器类型", "help": "Skyvern 支持 msedge / chrome / stealth-chromium；旧值 chromium-headful 会自动转为 stealth-chromium",
    },
    {
        "key": "SKYVERN_AD_BLOCKER", "file": "skyvern.py", "type": "bool", "group": "Skyvern",
        "label": "广告拦截", "help": "创建 Skyvern Browser Session 时启用 ad_blocker",
    },
    {
        "key": "SKYVERN_GENERATE_BROWSER_PROFILE", "file": "skyvern.py", "type": "bool", "group": "Skyvern",
        "label": "保存浏览器Profile", "help": "Session 结束时是否让 Skyvern 生成/保存 browser profile",
    },
    {
        "key": "SKYVERN_KEEP_BROWSER_OPEN", "file": "skyvern.py", "type": "bool", "group": "Skyvern",
        "label": "保留浏览器", "help": "调试时可开启，任务结束后不主动关闭 Skyvern Browser Session",
    },
    {
        "key": "SKYVERN_START_URL", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "起始 URL", "help": "默认 https://chatgpt.com/auth/login",
    },
    {
        "key": "ROXY_API_BASE", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy API 地址", "help": "默认 http://127.0.0.1:50000；需在 Roxy 应用 API 配置中开启",
    },
    {
        "key": "ROXY_API_TOKEN", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy API Key", "help": "保存在 .env（ROXY_API_TOKEN），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "ROXY_PROFILE_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 环境ID", "help": "指定要打开的 Roxy 浏览器环境/Profile ID；留空则尝试创建临时环境",
    },
    {
        "key": "ROXY_WORKSPACE_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 工作区ID", "help": "创建一号一环境时必填，会作为 workspaceId 提交给 Roxy 创建 Profile 接口",
    },
    {
        "key": "ROXY_PROJECT_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 项目ID", "help": "从 /browser/workspace 的 project_details.projectId 获取；创建 Profile 时会作为 projectId 提交",
    },
    {
        "key": "ROXY_WORKSPACE_LIST_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "获取团队接口", "help": "默认 /browser/workspace；点击获取团队/项目时会先试此路径，再自动尝试常见兼容路径",
    },
    {
        "key": "ROXY_OPEN_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "打开接口路径", "help": "默认 /browser/open；如 Roxy 版本不同可在此调整",
    },
    {
        "key": "ROXY_CREATE_INTERVAL", "file": "roxybrowser.py", "type": "float", "group": "RoxyBrowser",
        "label": "创建环境间隔", "help": "多线程时相邻 /browser/create 请求的最小间隔，默认 1.5 秒；设为 0 可关闭",
    },
    {
        "key": "ROXY_OPEN_HEADLESS", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "无头启动窗口", "help": "打开 Roxy 环境时向 /browser/open 传 headless；False=显示窗口，True=无头启动",
    },
    {
        "key": "ROXY_CLOSE_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "关闭接口路径", "help": "默认 /browser/close",
    },
    {
        "key": "ROXY_KEEP_BROWSER_OPEN", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "保留浏览器", "help": "调试时可开启，任务结束后不自动关闭 Roxy 环境",
    },
    {
        "key": "ROXY_ONE_PROFILE_PER_ACCOUNT", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "一号一环境", "help": "每个账号强制创建新 Roxy Profile，用完关闭并删除，禁止复用固定环境",
    },
    {
        "key": "ROXY_DELETE_PROFILE_AFTER_RUN", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "结束后删除环境", "help": "一号一环境模式下，任务结束后删除本轮创建的 Roxy Profile",
    },
    {
        "key": "ROXY_RANDOM_OS_ON_CREATE", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境随机OS", "help": "创建 Roxy 环境时每次在 Windows / macOS 中随机，不固定 macOS",
    },
    {
        "key": "ROXY_RANDOM_OS_CHOICES", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "随机OS范围", "help": "逗号分隔，默认 Windows,macOS；Roxy 支持 Windows / macOS / Linux / IOS / Android",
    },
    {
        "key": "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境随机名称", "help": "创建 Roxy 环境时自动生成不同名称，避免固定 gpt-free-register",
    },
    {
        "key": "ROXY_PROFILE_NAME_PREFIX", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "随机名称前缀", "help": "默认 rb；实际名称格式类似 rb-时间戳-随机码",
    },
    {
        "key": "ROXY_CREATE_USE_PROXY_POOL", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境使用代理池", "help": "创建 Roxy 环境时从配置页「代理池」随机取一个代理，写入 Roxy proxyInfo",
    },
    {
        "key": "ROXY_PROXY_CHECK_CHANNEL", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "代理检测通道", "help": "写入 Roxy proxyInfo.checkChannel；留空则不传，默认 IPRust.io",
    },
    {
        "key": "ROXY_DELETE_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "删除接口路径", "help": "默认 /browser/delete；如 Roxy 版本不同可调整",
    },
    {
        "key": "CODEX_OAUTH_DRIVER", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "Codex授权驱动", "help": "默认推荐 roxy；protocol=原协议授权；roxy=用 RoxyBrowser；cloak=用 CloakBrowser；browser_use=用 Browser Use Cloud；skyvern=用 Skyvern；same_as_registration=跟随注册驱动",
    },
    {
        "key": "ROXY_CODEX_CALLBACK_TIMEOUT", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "Codex回调超时", "help": "Roxy Codex OAuth 等待 localhost:1455 callback 的最长秒数",
    },
    {
        "key": "ENABLE_2FA", "file": "twofa.py", "type": "bool", "group": "功能开关",
        "label": "启用 2FA(TOTP)", "help": "注册完成后自动设置动态口令（会多收一封 OTP 邮件）",
    },
    {
        "key": "TWOFA_WORKERS", "file": "twofa.py", "type": "int", "group": "功能开关",
        "label": "2FA并发数", "help": "同时执行的2FA设置任务数，默认4，范围1-16；修改后需重启服务",
    },
    {
        "key": "TWOFA_QUEUE_LIMIT", "file": "twofa.py", "type": "int", "group": "功能开关",
        "label": "2FA队列容量", "help": "允许排队等待的2FA任务总数，默认200",
    },
    {
        "key": "ENABLE_FLOW_TRIGGER", "file": "flow_trigger.py", "type": "bool", "group": "功能开关",
        "label": "启用 Flow 触发", "help": "注册成功后自动调用内部 Flow 接口（不影响注册结果）",
    },
    {
        "key": "ENABLE_HUMANIZE_DELAY", "file": "humanize.py", "type": "bool", "group": "人工节奏",
        "label": "启用随机停顿", "help": "在注册、OTP、授权等步骤之间加入随机等待，更接近人工操作节奏",
    },
    {
        "key": "HUMANIZE_DELAY_FACTOR", "file": "humanize.py", "type": "float", "group": "人工节奏",
        "label": "停顿倍率", "help": "随机停顿整体倍率；1.0=默认，0.5=减半，2.0=加倍",
    },
    {
        "key": "ENABLE_HUMANIZE_BROWSER_ACTIONS", "file": "humanize.py", "type": "bool", "group": "人工节奏",
        "label": "浏览器动作随机化", "help": "Roxy/Cloak 点击、输入、页面观察使用随机鼠标落点和逐字输入，降低机械操作痕迹",
    },
    # ---- 邮箱 / OTP ----
    {
        "key": "USE_EMAIL_SERVICE", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "自动取邮箱+收码", "help": "True=从邮箱池自动领邮箱并自动收 OTP；False=手动模式：用 REGISTER_EMAIL，OTP 在任务页手填",
    },
    {
        "key": "REGISTER_EMAIL", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "手动注册邮箱", "help": "USE_EMAIL_SERVICE=False 时必填。例如你的 outlook.com 地址；OTP 去网页邮箱看，再回任务页提交",
    },
    {
        "key": "REGISTER_NAME", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "显示名称", "help": "留空则自动生成英文名",
    },
    {
        "key": "OTP_MAX_WAIT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "OTP 最长等待(秒)", "help": "等待验证码邮件的最长秒数，超时判失败",
    },
    {
        "key": "OTP_POLL_INTERVAL", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "OTP 轮询间隔(秒)", "help": "每隔多少秒查一次新邮件",
    },
    {
        "key": "GENERIC_API_PROXY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "通用 API 取码代理", "help": "仅用于 generic_api 接口取码；支持带认证的代理 URL，凭证不会在配置列表明文返回；留空则沿用代理池回退或直连",
        "storage": "env", "secret": True,
    },
    {
        "key": "EMAIL_SOURCE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "邮箱来源", "help": "可填单个或多个，逗号分隔并按顺序兜底：outlook,generic_api,imap,cloudflare_domain,cloudflare,gptmail,mailnest,cloudmail,remail",
    },
    {
        "key": "IMAP_MAILBOX", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "通用 IMAP 收件箱", "help": "通用 IMAP 邮箱默认目录，通常为 INBOX；服务器、端口、用户名和密码在邮箱池导入",
    },
    {
        "key": "GPTMAIL_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "GPTMail API Key", "help": "选择 gptmail 邮箱来源时必填；保存在 .env，不会写入 config 源码",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare API 地址", "help": "Worker 临时邮箱 API 根地址，如 https://mail.example.com；选择 cloudflare 时必填",
        "storage": "env",
    },
    {
        "key": "CLOUDFLARE_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare API Key", "help": "匿名可空；admin 模式填 ADMIN_PASSWORD；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_AUTH_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 鉴权模式", "help": "none / bearer / x-api-key / x-admin-auth / query-key",
    },
    {
        "key": "CLOUDFLARE_CUSTOM_AUTH", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 全局密码", "help": "Worker PASSWORDS，注入 x-custom-auth；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_PATH_ACCOUNTS", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 创建路径", "help": "默认 /api/new_address；admin 常用 /admin/new_address",
    },
    {
        "key": "CLOUDFLARE_PATH_MESSAGES", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 邮件路径", "help": "默认 /api/mails",
    },
    {
        "key": "CLOUDFLARE_PATH_DOMAINS", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 域名路径", "help": "默认 /api/domains（预留）",
    },
    {
        "key": "CLOUDFLARE_PATH_TOKEN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare Token路径", "help": "默认 /api/token（fallback 预留）",
    },
    {
        "key": "CLOUDFLARE_DEFAULT_DOMAINS", "file": "email.py", "type": "list_str_multiline", "group": "邮箱 / OTP",
        "label": "Cloudflare 默认域名", "help": "收信域名，每行一个或逗号分隔；创建时轮询使用，可留空",
    },
    {
        "key": "CLOUDFLARE_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Cloudflare 请求超时(秒)", "help": "HTTP 请求超时，默认 20",
    },
    {
        "key": "CLOUDFLARE_NAME_LENGTH", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Cloudflare 随机名前缀长度", "help": "admin 创建时 local-part 长度，默认 10",
    },
    {
        "key": "OUTLOOK_FETCH_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Outlook取件模式", "help": "auto=远端优先，远端 402/DEPLOYMENT_DISABLED 自动切 Graph 直连；direct=只用 Microsoft Graph 直连；remote=只用远端服务",
    },
    {
        "key": "EMAIL_DOMAIN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "转发域名(cloudflare_domain)", "help": "仅 cloudflare_domain 使用：Email Routing 的域名，如 mydomain.com；与 EMAIL_SOURCE=cloudflare 无关",
    },
    {
        "key": "QQ_EMAIL", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QQ 邮箱地址", "help": "仅 cloudflare_domain：接收 Email Routing 转发的 QQ 邮箱，如 123456@qq.com",
    },
    {
        "key": "QQ_IMAP_PASSWORD", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QQ 邮箱 IMAP 授权码", "help": "仅 cloudflare_domain：QQ IMAP 授权码，保存在 .env，不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "MAIL_NEST_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "MailNest API Key", "help": "选择 mailnest 邮箱来源时必填；保存在 .env，不会写入 config 源码",
        "storage": "env", "secret": True,
    },
    {
        "key": "MAIL_NEST_PROJECT_CODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "MailNest 项目代码", "help": "项目代码 默认 chatgpt001 获取页面 mailnest.top/buy-email",
    },
    {
        "key": "CLOUDMAIL_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail API 地址", "help": "Cloud Mail Worker/API 地址，例如 https://mail.example.com",
    },
    {
        "key": "CLOUDMAIL_ADMIN_EMAIL", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail管理员邮箱", "help": "用于生成 Token；域名被平台隐藏时也会用它登录读取域名",
        "storage": "env",
    },
    {
        "key": "CLOUDMAIL_PASSWORD", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail 密码", "help": "用于自动获取 Token；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDMAIL_TOKEN_PATH", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail Token路径", "help": "固定使用 /api/public/genToken；如部署版本不同可修改",
    },
    {
        "key": "CLOUDMAIL_AUTH_TOKEN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail Token", "help": "CloudMail/Cloud Mail API Authorization Token；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDMAIL_DOMAINS", "file": "email.py", "type": "list_str_multiline", "group": "邮箱 / OTP",
        "label": "CloudMail 域名列表", "help": "可留空；运行时会自动从平台获取。也可点“获取 CloudMail 域名”缓存到这里",
    },
    {
        "key": "CLOUDMAIL_AUTO_ADD_USER", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "CloudMail自动创建用户", "help": "生成随机邮箱后调用 /api/public/addUser 创建用户",
    },
    {
        "key": "CLOUDMAIL_RANDOM_LOCAL_LENGTH", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "CloudMail随机名前缀长度", "help": "生成邮箱 local-part 的长度，建议 10-16",
    },
    {
        "key": "REMAIL_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Remail API 地址", "help": "默认 https://remail.aishop6.com；也可填写文档地址 https://remail.aishop6.com/docs",
        "external_url": "https://remail.aishop6.com/register?aff=AFFLGYQMTYIXH",
        "external_label": "打开 Remail 官网",
    },
    {
        "key": "REMAIL_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Remail API Key", "help": "Remail 控制台生成的 rk- 开头 API Key；选择 remail 来源时必填，保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "REMAIL_PROJECT_ID", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Remail 项目 ID", "help": "Remail API 项目列表中的 projectId，用于匹配 ChatGPT/OpenAI 验证码项目",
    },
    {
        "key": "REMAIL_EMAIL_SUFFIX", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Remail 邮箱后缀", "help": "下单时使用的邮箱后缀，默认 outlook.com；不要填写完整邮箱",
    },
    {
        "key": "REMAIL_SERVICE_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Remail 服务模式", "help": "code=短效接码；purchase=长效购买（可重复收件，默认）",
    },
    {
        "key": "REMAIL_SUPPLY_POLICY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Remail 库存策略", "help": "private_first 优先自有库存；public_only 只使用公开库存（默认）",
    },
    {
        "key": "REMAIL_ORDER_WAIT_SECONDS", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Remail 订单等待(秒)", "help": "下单后未立即返回 service token 时等待订单补齐凭证，默认 30 秒",
    },
    {
        "key": "REMAIL_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Remail 请求超时(秒)", "help": "Remail API 单次 HTTP 请求超时，默认 20 秒",
    },
    # ---- 浏览器地区画像 ----
    {
        "key": "BROWSER_LOCALE_PROFILE", "file": "browser.py", "type": "str", "group": "浏览器画像",
        "label": "地区画像", "help": "应与代理出口地区一致；可选 jp/cn/us/sg。当前本地代理实测为日本东京，推荐 jp",
    },

    {
        "key": "AUTO_BROWSER_LOCALE_FROM_IP", "file": "browser.py", "type": "bool", "group": "浏览器画像",
        "label": "按出口IP自动画像", "help": "开启后每个 BrowserSession 会用当前代理出口 IP 自动选择语言/时区；失败时回退到地区画像",
    },
    {
        "key": "IP_GEO_TIMEOUT", "file": "browser.py", "type": "float", "group": "浏览器画像",
        "label": "IP定位超时(秒)", "help": "出口 IP 地理信息接口的单次请求超时；接口失败会自动回退，不影响注册",
    },
    {
        "key": "BROWSER_DATA_SAVER_MODE", "file": "browser.py", "type": "bool", "group": "浏览器画像",
        "label": "本地浏览器省流量模式", "help": "仅 Roxy/Cloak 本地浏览器拦截图片和媒体等可选资源；Browser Use/Skyvern 云端浏览器不启用；默认关闭",
    },
    {
        "key": "BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", "file": "browser.py", "type": "list_str_multiline", "group": "浏览器画像",
        "label": "本地浏览器省流量拦截类型", "help": "仅 Roxy/Cloak 生效；每行一种，默认 image、media；可选 stylesheet、font、manifest、texttrack。不要填写 script/xhr/fetch/document/websocket",
    },
    {
        "key": "BROWSER_DATA_SAVER_BLOCKED_URL_PATTERNS", "file": "browser.py", "type": "list_str_multiline", "group": "浏览器画像",
        "label": "本地浏览器省流量 URL 屏蔽规则", "help": "仅 Roxy/Cloak 生效；每行一条 URL glob；默认拦截 RUM/广告统计和 Google GSI（不用 Google 登录时）。不要屏蔽核心 API/sentinel；填 [] 可关闭默认规则",
    },
    {
        "key": "BROWSER_TRAFFIC_DETAIL_LOG", "file": "browser.py", "type": "bool", "group": "浏览器画像",
        "label": "本地浏览器流量明细日志", "help": "仅 Roxy/Cloak 生效；注册结束时输出每个资源的 URL、类型、方法、状态码和上传/下载大小；URL 查询值会脱敏，默认关闭",
    },
    {
        "key": "BROWSER_TRAFFIC_DETAIL_MAX_ENTRIES", "file": "browser.py", "type": "int", "group": "浏览器画像",
        "label": "流量明细最多条数", "help": "按单请求总字节降序输出，默认 2000，最大 10000；用于后续分析可屏蔽资源",
    },
    {
        "key": "BROWSER_JS_COVERAGE_LOG", "file": "browser.py", "type": "bool", "group": "浏览器画像",
        "label": "记录本地浏览器 JS 覆盖率", "help": "仅 Roxy/Cloak 生效；通过 Chrome CDP 记录本次注册实际执行的 JS 函数和 offset；Browser Use/Skyvern 不启用；默认关闭",
    },
    {
        "key": "BROWSER_JS_COVERAGE_MAX_ENTRIES", "file": "browser.py", "type": "int", "group": "浏览器画像",
        "label": "本地浏览器 JS 覆盖率最多条数", "help": "仅 Roxy/Cloak 生效；日志最多输出的已执行函数数，同时限制保存的脚本摘要数量，默认 1000，最大 10000",
    },

    # ---- 代理池 ----
    {
        "key": "PROXY_POOL", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "代理池(每行一个)", "help": "每行一个代理 URL，留空行会被忽略；为空则不使用代理；凭证仅保存在 .env，不会在配置列表明文返回", "secret": True,
        "recommended_links": [
            {
                "label": "IPWO 家宽",
                "url": "https://www.ipwo.net/?code=XEP358YGZ",
                "description": "IPWO 住宅代理提供覆盖195+国家和地区的住宅 IP 资源，支持多地区网络环境配置，适用于 AI 应用、浏览器自动化、海外服务访问及数据采集等场景。重点！2GB 动态住宅流量无门槛发放，",
                "description_link_label": "领取入口",
                "description_link_url": "https://www.ipwo.net/?code=XEP358YGZ",
                "description_after_link": "，进群不定时 IP 福利发放。",
            },
            {
                "label": "IPRocket 家宽",
                "url": "https://iprocket.io?viteCode=1PVNyLuJ",
                "description": "高性价比家宽，可通过 TG 联系作者购买流量",
            },
            {
                "label": "Rola-IP 家宽",
                "url": "https://rola-ip.co/?code=0326C5HA",
                "description": "Roxy 合作伙伴高质量家宽，注册可享 15% 优惠",
            },
        ],
    },
    {
        "key": "PROXY_POOL_UPSTREAM_PROXY", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "代理池上游代理", "help": "可选；代理池每个目标代理通过此本地上游连接。支持认证代理 URL，凭证不会在配置列表明文返回；留空则不链式",
        "storage": "env", "secret": True,
    },
    {
        "key": "PLAN_CHECK_PROXY_MODE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "套餐/Agent网络模式", "help": "用于查套餐和生成 Agent Token；auto=本地代理可用则走代理、未监听则直连；proxy=强制代理；direct=强制直连",
    },
    {
        "key": "PLAN_CHECK_PROXY", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "套餐/Agent专用代理(每行一个)", "help": "用于查套餐、查活和生成 Agent Token；支持动态代理 URL，每行一条。凭证不会在配置列表明文返回，仅保存到 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "PLAN_CHECK_UPSTREAM_PROXY", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "套餐/Agent本地上游代理", "help": "可选；仅用于套餐/Agent专用代理，形成“本地代理 -> 动态代理 -> ChatGPT”的代理链。留空则不链式。凭证不会在配置列表明文返回，仅保存到 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "PLAN_CHECK_TIMEOUT", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent超时(秒)", "help": "查套餐和生成 Agent Token 的单次请求超时，建议 10-20 秒；独立于注册请求超时",
    },
    {
        "key": "PLAN_CHECK_MAX_ATTEMPTS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐/Agent最大尝试次数", "help": "查套餐和生成 Agent Token 遇到 403、429、5xx 或网络错误时的重试次数，建议 3 次",
    },
    {
        "key": "PLAN_CHECK_RETRY_DELAY", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent重试间隔(秒)", "help": "查套餐和生成 Agent Token 的重试间隔，按尝试次数递增；服务端 Retry-After 优先",
    },
    {
        "key": "PLAN_CHECK_REGISTRATION_RECHECK_DELAY", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "新账号资格复查延迟(秒)", "help": "新注册 free 账号未发现试用资格或首次查询失败时复查一次；0 表示关闭",
    },
    {
        "key": "PLAN_CHECK_WORKERS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐查询并发数", "help": "自动、手动和批量查套餐共用；Agent Token 生成使用独立队列；建议 2-4 个线程",
    },
    {
        "key": "PLAN_CHECK_QUEUE_LIMIT", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐查询队列上限", "help": "防止异常批量操作无限堆积，建议 100-1000",
    },
    {
        "key": "PLAN_CHECK_MIN_INTERVAL", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent请求最小间隔(秒)", "help": "限制查套餐和生成 Agent Token 的请求启动频率，降低 429 风险",
    },
    {
        "key": "PLAN_CHECK_JITTER", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent请求随机抖动(秒)", "help": "在查套餐和生成 Agent Token 的最小间隔上增加随机延迟，避免请求过于规律",
    },
    # ---- 支付方式资格检测 ----
    {
        "key": "PAYMENT_QUALIFICATION_PATH", "file": "payment.py", "type": "str", "group": "支付方式",
        "label": "qualification-test 路径", "help": "本地模式使用；填写 /home/.../qualification-test 可加载该项目的检测代码",
    },
    {
        "key": "PAYMENT_QUALIFICATION_API_BASE", "file": "payment.py", "type": "str", "group": "支付方式",
        "label": "qualification-test API 基址", "help": "填写独立 qualification-test 服务地址后，使用其 POST API；例如 http://127.0.0.1:18097。留空则本地执行 checker",
    },
    {
        "key": "PAYMENT_QUALIFICATION_API_PATH", "file": "payment.py", "type": "str", "group": "支付方式",
        "label": "qualification-test API 路径", "help": "默认 /api/gcash/check；必须是 /api/ 下的 POST 路径",
    },
    {
        "key": "PAYMENT_QUALIFICATION_API_KEY", "file": "payment.py", "type": "str", "group": "支付方式",
        "label": "qualification-test API 密钥", "help": "可选；以 Authorization: Bearer 方式发送到独立检测服务",
        "storage": "env", "secret": True,
    },
    {
        "key": "PAYMENT_METHOD_AUTO_CHECK_AFTER_REGISTER", "file": "payment.py", "type": "bool", "group": "支付方式",
        "label": "注册后自动查支付方式", "help": "注册成功后异步查询配置地区的 Checkout 可用支付方式；只读取方式，不确认/发起支付",
    },
    {
        "key": "PAYMENT_METHOD_CHECK_REGIONS", "file": "payment.py", "type": "str", "group": "支付方式",
        "label": "检测地区预设", "help": "逗号分隔：gcash,card,paypal_uk,paypal_nl,ideal_nl,momo_vn,gopay_id,upi_in,blik_pl,pix_br",
    },
    {
        "key": "PAYMENT_METHOD_CHECK_PROXY", "file": "payment.py", "type": "str", "group": "支付方式",
        "label": "支付检测代理", "help": "通用目标国家出口代理；留空则尝试代理池。可能含认证信息，仅保存到 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "PAYMENT_METHOD_CHECK_PROXIES", "file": "payment.py", "type": "list_str_multiline", "group": "支付方式",
        "label": "支付检测分地区代理", "help": "每行 preset=proxy，例如 paypal_uk=http://user:pass@host:port；支持 default=proxy",
        "storage": "env", "secret": True,
    },
    {
        "key": "PAYMENT_METHOD_CHECK_WORKERS", "file": "payment.py", "type": "int", "group": "支付方式",
        "label": "支付检测并发数", "help": "支付方式查询后台线程数，建议 1-3",
    },
    {
        "key": "PAYMENT_METHOD_CHECK_QUEUE_LIMIT", "file": "payment.py", "type": "int", "group": "支付方式",
        "label": "支付检测队列上限", "help": "限制待执行账号数量",
    },
    {
        "key": "PAYMENT_METHOD_CHECK_TIMEOUT", "file": "payment.py", "type": "float", "group": "支付方式",
        "label": "支付检测超时(秒)", "help": "一个账号全部地区检测的最大时间，建议 180-900",
    },
    {
        "key": "PAYMENT_METHOD_CHECK_RETRIES", "file": "payment.py", "type": "int", "group": "支付方式",
        "label": "支付检测重试次数", "help": "单个地区网络失败时的重试次数，建议 1-3",
    },
    {
        "key": "PAYMENT_METHOD_CHECK_MIN_INTERVAL", "file": "payment.py", "type": "float", "group": "支付方式",
        "label": "支付检测间隔(秒)", "help": "账号任务启动前的节流间隔，降低集中请求风险",
    },
    # ---- 提链 ----
    {
        "key": "EXTRACT_LINK_BACKEND", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链后端", "help": "cdk=原 CDK 服务；pay153=pay153-checkout-link；djbnb=DJB 提链 API（/api/tasks 轮询）",
    },
    {
        "key": "EXTRACT_LINK_API_BASE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链服务地址(cdk)", "help": "cdk 后端使用：填写提链服务 API 地址",
    },
    {
        "key": "EXTRACT_LINK_CDK", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链 CDK", "help": "cdk 后端使用：创建提链任务和监听任务事件；成功提链扣 1 次",
        "storage": "env", "secret": True,
    },
    {
        "key": "DJB_API_BASE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "DJB API 地址", "help": "djbnb 后端使用，例如 https://www.djbnb.xyz；对应 /api/tasks、/api/card/check",
    },
    {
        "key": "DJB_CARD_CODE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "DJB 卡密", "help": "djbnb 后端使用；提链成功后按服务规则扣次数，仅保存到 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "DJB_PROXIES", "file": "extract_link.py", "type": "list_str_multiline", "group": "提链",
        "label": "DJB 建单代理池", "help": "djbnb custom 模式每行一条代理；留空时复用通用代理池 PROXY_POOL",
        "storage": "env", "secret": True,
    },
    {
        "key": "DJB_EXIT_PROXIES", "file": "extract_link.py", "type": "list_str_multiline", "group": "提链",
        "label": "DJB 出口代理池", "help": "djbnb 可选出口/账单侧代理池，每行一条；留空时由服务端复用主池",
        "storage": "env", "secret": True,
    },
    {
        "key": "DJB_PROXY_MODE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "DJB 代理模式", "help": "custom=使用自定义代理；builtin=使用服务端内置代理（需服务端开启）",
    },
    {
        "key": "DJB_PARAMS_TEXT", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "DJB 附加参数", "help": "JSON 对象字符串，例如 {\"country\":\"ID\",\"currency\":\"IDR\"}",
    },
    {
        "key": "DJB_CONCURRENCY", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "DJB 任务并发", "help": "发送给 DJB 的请求并发，最终受卡密等级和服务端上限约束",
    },
    {
        "key": "DJB_POLL_INTERVAL_MS", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "DJB 轮询间隔(毫秒)", "help": "查询 /api/tasks/{id} 的间隔，建议 1000-3000",
    },
    {
        "key": "DJB_TIMEOUT_MS", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "DJB 任务窗口(毫秒)", "help": "客户端等待窗口，服务端固定最大 10 分钟",
    },
    {
        "key": "PAY153_API_BASE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "pay153 服务地址", "help": "pay153 后端使用：pay153-checkout-link 服务地址，例如 http://127.0.0.1:18082",
    },
    {
        "key": "PAY153_INTERNAL_KEY", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "pay153 内部密钥", "help": "pay153 后端使用：X-Pay153-Internal-Key 请求头；设置后可绕过公开队列 IP RPM，并允许未配置代理池时走 pay153 动态代理",
        "storage": "env", "secret": True,
    },
    {
        "key": "PAY153_PLAN", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "pay153 计划", "help": "pay153 后端使用：plus / pro / team / codex_low",
    },
    {
        "key": "PAY153_COUNTRY", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "pay153 国家", "help": "pay153 后端使用：结算国家代码，留空由服务端自动选择，如 US / BR / NL",
    },
    {
        "key": "PAY153_CURRENCY", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "pay153 币种", "help": "pay153 后端使用：结算币种，留空由服务端自动选择，如 USD / BRL / EUR",
    },
    {
        "key": "PAY153_ENTRY_PROXIES", "file": "extract_link.py", "type": "list_str_multiline", "group": "提链",
        "label": "pay153 入口代理池", "help": "pay153 后端使用：每行一条代理；留空且配置了内部密钥时走 pay153 动态代理",
        "storage": "env", "secret": True,
    },
    {
        "key": "PAY153_EXIT_PROXIES", "file": "extract_link.py", "type": "list_str_multiline", "group": "提链",
        "label": "pay153 出口代理池", "help": "pay153 后端使用：每行一条代理；hosted/pix/momo 等路径可留空沿用入口代理",
        "storage": "env", "secret": True,
    },
    {
        "key": "PAY153_RETRY_COUNT", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "pay153 重试次数", "help": "pay153 后端使用：外层重试次数，每次重建 Checkout/设备标识/支付参数，建议 1-10",
    },
    {
        "key": "PAY153_USE_PROMO", "file": "extract_link.py", "type": "bool", "group": "提链",
        "label": "pay153 使用优惠", "help": "pay153 后端使用：Plus 计划请求优惠（试用零元）",
    },
    {
        "key": "EXTRACT_LINK_QUEUE_LIMIT", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "提链队列上限", "help": "后台提链队列可同时接收的最大账号任务数",
    },
    {
        "key": "EXTRACT_LINK_REQUEST_TIMEOUT", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "提链请求超时(秒)", "help": "单次提链 API 请求的最大等待时间",
    },
    {
        "key": "EXTRACT_LINK_EVENT_TIMEOUT", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "提链事件超时(秒)", "help": "cdk SSE 事件流的最大等待时间",
    },
    {
        "key": "PAY153_POLL_INTERVAL_MS", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "pay153 轮询间隔(毫秒)", "help": "pay153 checkout 进度轮询间隔",
    },
    {
        "key": "PAY153_POLL_TIMEOUT", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "pay153 任务窗口(秒)", "help": "pay153 checkout 任务最大等待时间",
    },
    {
        "key": "EXTRACT_LINK_TYPE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链类型", "help": "cdk 支持 pix / upi / kakao_pay / ideal；pay153 支持 hosted / ph_short / paypal / ideal / twint / upi / pix / momo / gcash / kakao；djbnb 支持 paypal / gopay / gcash / momo / upi / card / pix / ideal",
    },
    {
        "key": "EXTRACT_LINK_WORKERS", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "提链并发数", "help": "批量提链后台线程数，建议 1-4",
    },
    # ---- MoMo 一键开通 ----
    {
        "key": "MOMO_ACTIVATION_ENABLED", "file": "momo_activation.py", "type": "bool", "group": "MoMo一键开通",
        "label": "启用一键开通", "help": "启用账号列表中的 MoMo 一键开通入口；关闭后不会向远端提交任务",
    },
    {
        "key": "MOMO_ACTIVATION_API_BASE", "file": "momo_activation.py", "type": "str", "group": "MoMo一键开通",
        "label": "MoMo API 地址", "help": "默认 https://hahz7zf.ink；后端只调用公开 v1 API，不把任务密钥返回前端", "external_url": "https://hahz7zf.ink/api-docs", "external_label": "打开 API 文档",
    },
    {
        "key": "MOMO_ACTIVATION_AUTO_PAY", "file": "momo_activation.py", "type": "bool", "group": "MoMo一键开通",
        "label": "自动支付", "help": "提交任务时自动发起 MoMo 支付；开启后必须填写 payment CDK。此操作可能消耗试用/支付权益",
    },
    {
        "key": "MOMO_ACTIVATION_PAYMENT_CDK", "file": "momo_activation.py", "type": "str", "group": "MoMo一键开通",
        "label": "自动支付 CDK", "help": "仅保存在 .env；服务端只在远端创建任务时临时使用，不会写入账号记录或返回浏览器", "storage": "env", "secret": True,
    },
    {
        "key": "MOMO_ACTIVATION_ENTRY_PROXIES", "file": "momo_activation.py", "type": "list_str_multiline", "group": "MoMo一键开通",
        "label": "MoMo 入口代理池", "help": "每行一个代理 URL；为空时回退通用 PROXY_POOL。代理凭据仅发送给 MoMo 服务，不写入任务结果", "storage": "env", "secret": True,
    },
    {
        "key": "MOMO_ACTIVATION_TIMEOUT", "file": "momo_activation.py", "type": "int", "group": "MoMo一键开通",
        "label": "创建任务超时(秒)", "help": "创建 MoMo 任务的 HTTP 超时，范围 8-120",
    },
    {
        "key": "MOMO_ACTIVATION_TRIAL_DAYS", "file": "momo_activation.py", "type": "int", "group": "MoMo一键开通",
        "label": "试用天数", "help": "发送给 MoMo API 的 trial_days，范围 1-90，默认 30",
    },
    {
        "key": "MOMO_ACTIVATION_POLL_INTERVAL", "file": "momo_activation.py", "type": "float", "group": "MoMo一键开通",
        "label": "轮询间隔(秒)", "help": "查询远端任务状态的间隔，建议 1-5 秒",
    },
    {
        "key": "MOMO_ACTIVATION_MAX_WAIT", "file": "momo_activation.py", "type": "int", "group": "MoMo一键开通",
        "label": "任务最长等待(秒)", "help": "单个远端任务的最大等待时间；超时会尝试取消远端任务",
    },
    {
        "key": "MOMO_ACTIVATION_WORKERS", "file": "momo_activation.py", "type": "int", "group": "MoMo一键开通",
        "label": "后台线程数", "help": "一键开通本地后台线程数，建议 1-3",
    },
    {
        "key": "MOMO_ACTIVATION_QUEUE_LIMIT", "file": "momo_activation.py", "type": "int", "group": "MoMo一键开通",
        "label": "队列上限", "help": "本地一键开通队列可接受的最大任务数",
    },
    # ---- Codex 配置 ----
    {
        "key": "SUB2API_AUTO_EXPORT", "file": "sub2api.py", "type": "bool", "group": "Codex",
        "label": "Agent sub2 自动同步", "help": "生成 Codex Agent Token 成功后自动同步到 sub2api",
    },
    {
        "key": "SUB2API_SYNC_MODE", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 同步模式", "help": "api=直接上传接口；file=写本地json；both=接口+本地json",
    },
    {
        "key": "SUB2API_API_BASE", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 API基址", "help": "sub2api 服务地址；Agent Token 上传和 Codex OAuth 共用，例如 http://127.0.0.1:8080",
    },
    {
        "key": "SUB2API_API_KEY", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 API Key", "help": "sub2api 管理接口 API Key；请求头使用 x-api-key；为空则不带鉴权头", "storage": "env", "secret": True,
    },
    {
        "key": "SUB2API_API_TIMEOUT", "file": "sub2api.py", "type": "int", "group": "Codex",
        "label": "sub2 超时", "help": "sub2api 请求超时秒数",
    },
    {
        "key": "SUB2API_OUTPUT_PATH", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 本地路径", "help": "仅 SUB2API_SYNC_MODE=file/both 时使用；相对路径按项目根目录解析",
    },
    {
        "key": "SUB2API_PROXY_KEY", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 代理键", "help": "可选；写入 account.proxy_key，并在 proxies 为空时初始化 proxies[0].proxy_key；仅保存在 .env，不会在配置列表明文返回", "storage": "env", "secret": True,
    },
    # ---- 接码平台 ----
    # ---- Codex：基础 / CPA / sub2api 配置 ----
    {
        "key": "CODEX_AUTH_URL_SOURCE", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "授权地址来源", "help": "cpa=CPA生成并上传CPA；sub2=sub2生成并上传sub2；local=本地PKCE",
    },
    {
        "key": "CPA_MANAGEMENT_URL", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "CPA 管理地址", "help": "例如 http://localhost:8317/admin/oauth；程序会取 origin 调用 /v0/management/*",
    },
    {
        "key": "CPA_MANAGEMENT_KEY", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "管理密钥", "help": "保存在 .env（CPA_MANAGEMENT_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "CPA_REQUEST_TIMEOUT", "file": "codex.py", "type": "int", "group": "Codex",
        "label": "CPA 超时(秒)", "help": "请求 CPA 管理接口的超时时间",
    },
    {
        "key": "CPA_SAVE_CALLBACK_RECEIPT", "file": "codex.py", "type": "bool", "group": "Codex",
        "label": "保存CPA回执", "help": "CPA 未返回完整授权文件时，本地仍保存一份回调提交记录",
    },

    {
        "key": "SMS_PROVIDER", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "接码通道", "help": "grizzly / smsbower / l / h；smsbower 使用 SMSBower handler_api，l/h 使用本地取号服务",
    },
    {
        "key": "SMS_COUNTRY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "国家代码", "help": "传给接码平台的 country；SMSBower 按其国家表填写，GrizzlySMS 常用美国=187；H 通道作为 H_API.md 的 country",
    },
    {
        "key": "SMS_SERVICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "服务/项目代码", "help": "GrizzlySMS/L/SMSBower 作为 service；SMSBower 的 OpenAI (ChatGPT) 推荐填 dr，填 openai/chatgpt 时程序会自动转换；H 通道作为 projectId",
    },
    {
        "key": "SMS_MAX_PRICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "最高号码价格", "help": "透传给接码平台的 maxPrice；留空不限。SMSBower 可用它筛选价格/号码等级",
    },
    {
        "key": "SMS_MAX_RETRIES", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "换号重试次数", "help": "一个号收不到短信/被OpenAI拒时换下一个号，最多重试几次",
    },
    {
        "key": "SMS_CODE_WAIT", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "单号等短信(秒)", "help": "单个号等待短信到达的最长秒数，超时则换号",
    },
    {
        "key": "SMS_API_KEY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "GrizzlySMS API密钥", "help": "GrizzlySMS 平台 API Key，保存在 .env（SMS_API_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "SMSBOWER_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "SMSBower API 地址", "help": "默认 https://smsbower.page/stubs/handler_api.php",
    },
    {
        "key": "SMSBOWER_API_KEY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "SMSBower API密钥", "help": "SMSBower 控制台 API Key，保存在 .env，不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "SMSBOWER_USE_V2", "file": "codex.py", "type": "bool", "group": "接码平台",
        "label": "SMSBower 使用V2取号", "help": "官方客户端文档使用 getNumber；通常保持关闭。仅在确认账号支持 getNumberV2 时开启",
    },
    {
        "key": "SMSBOWER_PROVIDER_IDS", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "SMSBower 供应商筛选", "help": "可选，供应商 ID 用逗号分隔；留空由平台自动选择",
    },
    {
        "key": "SMSBOWER_EXCEPT_PROVIDER_IDS", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "SMSBower 排除供应商", "help": "可选，排除的供应商 ID 用逗号分隔",
    },
    {
        "key": "SMSBOWER_PHONE_EXCEPTION", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "SMSBower 排除号码前缀", "help": "可选，号码前缀用逗号分隔；用于避开已知不可用号段",
    },
    {
        "key": "SMSBOWER_MIN_PRICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "SMSBower 最低价格", "help": "可选，透传 minPrice；与最高价格一起限定号码价格区间",
    },
    {
        "key": "H_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H API 地址", "help": "H 取号服务基础地址，例如 http://localhost:8788",
    },
    {
        "key": "H_ADMIN_AUTH_CODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 授权码", "help": "保存在 .env（H_ADMIN_AUTH_CODE），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "H_PHONE_PREFIX", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 号码前缀", "help": "H 返回号码不含国家码时填写，例如美国 10 位本地号填 1；留空则不补",
    },
    {
        "key": "H_PHONE_ACQUIRE_MODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 取号方式", "help": "reusable=优先复用历史可用号码；new=每次都取一个新号码",
    },
    {
        "key": "L_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L API 地址", "help": "L 取号服务基础地址，例如 http://localhost:8788",
    },
    {
        "key": "L_ADMIN_AUTH_CODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L 授权码", "help": "保存在 .env（L_ADMIN_AUTH_CODE），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "L_PHONE_PREFIX", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L 号码前缀", "help": "L 返回号码不含国家码时填写，例如美国 10 位本地号填 1；留空则不补",
    },
]

_FIELD_BY_KEY = {f["key"]: f for f in EDITABLE_FIELDS}


# ============================================================
# 读：解析源码取当前值（不 import，避免缓存/副作用）
# ============================================================

def _config_path(filename: str) -> Path:
    path = (_CONFIG_DIR / filename).resolve()
    # 防目录穿越：必须落在 config/ 下
    if _CONFIG_DIR not in path.parents:
        raise ValueError(f"非法配置路径: {filename}")
    return path


def _literal_default_from_expr(node):
    """尽量从赋值表达式中取“源码默认值”，不执行模块代码。

    兼容：
      KEY = "literal"
      KEY: str = env_str("KEY", "default")
      KEY = env_bool("KEY", True)
      KEY = env_value("KEY", 123, "int")
    """
    try:
        return ast.literal_eval(node)
    except Exception:
        pass

    if isinstance(node, ast.Call):
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        # env_str/env_bool/env_int/env_float/env_list 的第二个位置参数是默认值。
        if func_name in {"env_str", "env_bool", "env_int", "env_float", "env_list"}:
            if len(node.args) >= 2:
                try:
                    return ast.literal_eval(node.args[1])
                except Exception:
                    return None
            return None

        # env_value(key, default, vtype)
        if func_name == "env_value" and len(node.args) >= 2:
            try:
                return ast.literal_eval(node.args[1])
            except Exception:
                return None

    return None


def _find_assignment_value_node(source: str, key: str):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id == key:
                return node.value
    return None


def _parse_value_from_source(source: str, key: str, vtype: str):
    """从源码里解析 KEY 的当前值。失败返回 None。"""
    if vtype == "list_str_multiline":
        # 用 AST 解析整个模块，取这个赋值的 list 字面量
        value_node = _find_assignment_value_node(source, key)
        if value_node is None:
            return None
        try:
            val = ast.literal_eval(value_node)
            if isinstance(val, (list, tuple)):
                return [str(x) for x in val]
        except (ValueError, SyntaxError):
            return None
        return None

    # 标量：优先 AST 取默认值，避免 env_str("KEY", "") 被当成普通字符串。
    value_node = _find_assignment_value_node(source, key)
    if value_node is not None:
        value = _literal_default_from_expr(value_node)
        if value is not None:
            return value

    # AST 失败时再回退到旧的正则解析。
    m = re.search(
        rf"^{re.escape(key)}\s*(?::[^=\n]+)?=\s*(.+?)\s*(?:#.*)?$",
        source, re.MULTILINE,
    )
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw


def _parse_env_typed_value(raw: str, fallback, vtype: str):
    """把 .env 字符串按字段类型转换；失败时回退 fallback。"""
    from config.env_loader import env_value
    return env_value("__NO_SUCH_ENV_KEY__", fallback, vtype) if raw is None else _coerce_raw_value(raw, fallback, vtype)


def _coerce_raw_value(raw: str, fallback, vtype: str):
    try:
        if raw is None or str(raw).strip() == "":
            return fallback
        if vtype == "bool":
            return str(raw).strip().lower() in ("true", "1", "yes", "on", "y")
        if vtype == "int":
            return int(str(raw).strip())
        if vtype == "float":
            return float(str(raw).strip())
        if vtype == "list_str_multiline":
            text = str(raw)
            try:
                val = ast.literal_eval(text)
                if isinstance(val, (list, tuple)):
                    return [str(x).strip() for x in val if str(x).strip()]
            except Exception:
                pass
            return [line.strip() for line in text.splitlines() if line.strip()]
        return str(raw)
    except Exception:
        return fallback


def get_config() -> list[dict]:
    """返回所有可编辑项的当前值 + 元信息，供前端渲染表单。

    优先读取 `.env` / 环境变量；没有配置时回退到 `config/*.py` 默认值。
    """
    from config.env_loader import load_env, read_env_file
    load_env(override=True)
    env_file_values = read_env_file()

    out = []
    for field in EDITABLE_FIELDS:
        key = field["key"]
        path = _config_path(field["file"])
        source = path.read_text(encoding="utf-8") if path.exists() else ""
        fallback = _parse_value_from_source(source, key, field["type"])

        if key in env_file_values:
            raw_env_value = env_file_values[key]
            if field["type"] == "list_str_multiline" and key in EXPLICIT_EMPTY_LIST_KEYS and str(raw_env_value).strip() == "":
                value = []
            else:
                value = _coerce_raw_value(raw_env_value, fallback, field["type"])
        elif os.getenv(key) is not None:
            value = _coerce_raw_value(os.getenv(key, ""), fallback, field["type"])
        else:
            value = fallback

        if field["type"] in ("str", "list_str_multiline"):
            value = _normalize_config_value(value, field["type"])
        item = dict(field)
        item["storage"] = "env"
        if field.get("secret"):
            # Never send proxy/API credentials to the WebUI.  The form can
            # replace a configured secret, while an empty submission preserves
            # the existing .env value (see update_config below).
            item["secret_configured"] = bool(value) if field["type"] != "list_str_multiline" else bool(value)
            item["value"] = [] if field["type"] == "list_str_multiline" else ""
        else:
            item["value"] = value
        out.append(item)
    return out


# ============================================================
# 写：统一写 .env，不修改 config/*.py
# ============================================================


_PLACEHOLDER_EMPTY = {
    "", "-", "—", "无", "空", "none", "null", "n/a", "na", "未设置", "未配置",
}


def _normalize_config_value(value, vtype: str):
    """把前端/历史占位空值规范化，避免 '-' 被当成真实配置。"""
    if vtype == "str":
        s = "" if value is None else str(value).strip()
        if s.lower() in {x.lower() for x in _PLACEHOLDER_EMPTY}:
            return ""
        return s
    if vtype == "list_str_multiline":
        if value is None:
            return []
        if isinstance(value, str):
            lines = value.splitlines()
        elif isinstance(value, (list, tuple)):
            lines = list(value)
        else:
            lines = [str(value)]
        out = []
        for item in lines:
            s = str(item or "").strip()
            if not s or s.lower() in {x.lower() for x in _PLACEHOLDER_EMPTY}:
                continue
            out.append(s)
        return out
    return value


def _format_literal(value, vtype: str) -> str:
    """把前端传来的值格式化成 Python 字面量字符串。"""
    if vtype == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on")
        return "True" if value else "False"
    if vtype == "int":
        return str(int(value))
    if vtype == "float":
        return repr(float(value))
    if vtype == "str":
        s = str(value)
        # 用 repr 保证转义安全，但统一成双引号风格
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise ValueError(f"_format_literal 不支持的类型: {vtype}")


def _replace_scalar(source: str, key: str, literal: str) -> str:
    """替换 `KEY[: 类型] = 旧值` 行的右值，保留行内注释和类型标注。"""
    pattern = re.compile(
        rf"^(?P<head>{re.escape(key)}\s*(?::[^=\n]+)?=\s*)"
        rf"(?P<val>.+?)"
        rf"(?P<tail>\s*(?:#.*)?)$",
        re.MULTILINE,
    )
    if not pattern.search(source):
        raise ValueError(f"未在源码中找到可替换的赋值: {key}")
    return pattern.sub(lambda m: f"{m.group('head')}{literal}{m.group('tail')}", source, count=1)


def _replace_proxy_pool(source: str, lines: list[str]) -> str:
    """整块替换 PROXY_POOL = [ ... ] 列表字面量（保留前面的赋值头）。"""
    items = [ln.strip() for ln in lines if ln.strip()]
    if items:
        body = "\n".join(
            '    "' + it.replace("\\", "\\\\").replace('"', '\\"') + '",'
            for it in items
        )
        literal = "[\n" + body + "\n]"
    else:
        literal = "[]"

    # 匹配 PROXY_POOL = [ ... ]（含跨行），用 AST 定位起止偏移最稳
    tree = ast.parse(source)
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "PROXY_POOL":
                src_lines = source.splitlines(keepends=True)
                start = node.value.lineno          # 值（[）所在行，1-based
                end = node.value.end_lineno        # 值（]）所在行，1-based
                col = node.value.col_offset         # [ 在起始行的列偏移
                # 保留起始行 [ 之前的内容（即 "PROXY_POOL = " 或 "PROXY_POOL: list = "）
                prefix = src_lines[start - 1][:col]
                # 保留结束行 ] 之后的内容（行内注释 / 换行）
                end_line = src_lines[end - 1]
                suffix = end_line[node.value.end_col_offset:]
                new_lines = (
                    src_lines[: start - 1]
                    + [prefix + literal + suffix]
                    + src_lines[end:]
                )
                return "".join(new_lines)
    raise ValueError("未找到 PROXY_POOL 赋值")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _format_env_value(value, vtype: str, fallback=None) -> str:
    """把前端值格式化成适合写入 .env 的字符串。

    数字输入框在用户清空、编辑中间态（例如 ``1.``）或浏览器把 ``NaN``
    序列化为 JSON ``null`` 时，空数字应清除 .env 覆盖并恢复源码默认值。
    ``fallback`` 仅供旧调用方兼容，默认不替换显式空值。
    """
    if value is None and fallback is not None:
        value = fallback
    if vtype == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on", "y")
        return "True" if value else "False"
    if vtype in ("int", "float"):
        # `.env` 的空值由 env_loader 解释为“使用 config/*.py 默认值”。
        if value is None or (isinstance(value, str) and not value.strip()):
            return ""
        number_label = "整数" if vtype == "int" else "数字"
        if isinstance(value, bool):
            raise ValueError(f"配置值必须是有效的{number_label}，收到: {value!r}")
        try:
            if vtype == "int":
                if isinstance(value, float):
                    if not math.isfinite(value) or not value.is_integer():
                        raise ValueError
                    number = int(value)
                else:
                    raw = str(value).strip()
                    if not re.fullmatch(r"[+-]?\d+", raw):
                        raise ValueError
                    number = int(raw)
            else:
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"配置值必须是有效的{number_label}，收到: {value!r}") from exc
        return str(number) if vtype == "int" else repr(number)
    if vtype == "list_str_multiline":
        lines = _normalize_config_value(value, vtype)
        return "\n".join(lines) if lines else "[]"
    if vtype == "str":
        return _normalize_config_value(value, vtype)
    return "" if value is None else str(value)


def update_config(updates: dict) -> dict:
    """批量更新配置。所有 WebUI 可编辑项只写项目根 `.env`。"""
    from config.env_loader import write_env_values, load_env

    updated, ignored = [], []
    env_updates: dict[str, str] = {}

    for key, value in updates.items():
        field = _FIELD_BY_KEY.get(key)
        if field is None:
            ignored.append(key)
            continue
        if field.get("secret"):
            # Secret values are masked in get_config. An empty form submission
            # preserves the existing .env value; use the explicit sentinel to
            # clear one intentionally.
            if value == "__CLEAR_SECRET__" or (
                field["type"] == "list_str_multiline"
                and isinstance(value, (list, tuple))
                and any(str(item).strip() == "__CLEAR_SECRET__" for item in value)
            ):
                env_updates[key] = ""
            elif value is None or (isinstance(value, str) and not value.strip()):
                continue
            elif isinstance(value, (list, tuple)) and not any(str(x).strip() for x in value):
                continue
            else:
                env_updates[key] = _format_env_value(value, field["type"])
        else:
            # Explicit null/empty numeric values clear the .env override; they
            # must not silently fall back to the currently loaded default.
            env_updates[key] = _format_env_value(value, field["type"])
        updated.append(key)


    env_updated = write_env_values(env_updates) if env_updates else []
    if env_updated:
        load_env(override=True)

    return {"updated": updated, "ignored": ignored, "env_updated": env_updated}
