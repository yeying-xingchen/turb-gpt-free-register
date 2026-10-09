# -*- coding: utf-8 -*-
"""GitHub 更新检查配置。

只做「检查 + 一键快进更新」：定时通过 GitHub API 比对远端最新提交，发现更新时在
WebUI 提示；是否真的更新由用户在界面上手动确认，不会自动改写本地代码。
"""
from config.env_loader import apply_env_overrides

# 是否在 WebUI 启动后自动定时检查 GitHub 更新。
UPDATE_CHECK_ENABLED: bool = True

# 自动检查间隔（小时），默认 6；运行时限定 0.1–720（0.1 小时 = 6 分钟）。
UPDATE_CHECK_INTERVAL_HOURS: float = 6.0

# 检查与一键更新使用的 git 远程名；分支留空表示自动跟随当前分支的上游。
UPDATE_CHECK_REMOTE: str = "origin"
UPDATE_CHECK_BRANCH: str = ""

# 比对更新所用的 GitHub 仓库（owner/repo 或仓库链接）；留空时从远程地址自动识别。
UPDATE_CHECK_REPO: str = ""

# 可选 GitHub Token：匿名 API 每小时 60 次，配置后提升到 5000 次/小时，也支持私有仓库。
UPDATE_CHECK_GITHUB_TOKEN: str = ""

# 访问 GitHub API 的代理（如 http://127.0.0.1:7890）；留空直连。
UPDATE_CHECK_PROXY: str = ""

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    "UPDATE_CHECK_ENABLED": "bool",
    "UPDATE_CHECK_INTERVAL_HOURS": "float",
    "UPDATE_CHECK_REMOTE": "str",
    "UPDATE_CHECK_BRANCH": "str",
    "UPDATE_CHECK_REPO": "str",
    "UPDATE_CHECK_GITHUB_TOKEN": "str",
    "UPDATE_CHECK_PROXY": "str",
})
