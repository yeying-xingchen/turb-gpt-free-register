# -*- coding: utf-8 -*-
"""sub2api 对接配置。"""
from config.env_loader import apply_env_overrides

# 生成 Codex Agent Token 成功后，是否自动同步/追加到 sub2api.json。
SUB2API_AUTO_EXPORT: bool = True

# sub2api 配置文件输出路径；相对路径按项目根目录解析。
SUB2API_OUTPUT_PATH: str = "sub2api.json"

# 可选代理键；写入 account.proxy_key，并在 sub2api.json proxies 为空时初始化 proxies[0].proxy_key。
SUB2API_PROXY_KEY: str = ""

apply_env_overrides(globals(), {
    'SUB2API_AUTO_EXPORT': 'bool',
    'SUB2API_OUTPUT_PATH': 'str',
    'SUB2API_PROXY_KEY': 'str',
})
