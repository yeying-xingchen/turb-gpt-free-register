"""
Codex Agent Identity 自动注册脚本
by 久雾

流程：
1. 通过 ChatGPT session JWT 获取账号信息
2. 生成 Ed25519 密钥对
3. 在 auth.openai.com 注册 agent
4. 生成 Codex CLI 可用的 auth.json

依赖：curl_cffi, cryptography
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from typing import Any

from curl_cffi import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PrivateFormat,
    PublicFormat,
    NoEncryption,
    load_pem_private_key,
)


# ============================================================
#  常量
# ============================================================

AUTHAPI_BASE = "https://auth.openai.com/api/accounts"
CHATGPT_BASE = "https://chatgpt.com"
IMPERSONATE = "chrome"

CHROME_VERSION = "146"
USER_AGENT = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    f"AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{CHROME_VERSION}.0.0.0 Safari/537.36"
)

# Codex CLI agent 版本信息
AGENT_VERSION = "0.138.0-alpha.6"
AGENT_HARNESS_ID = "codex-cli"
RUNNING_LOCATION = "local"


# ============================================================
#  日志
# ============================================================

_COLORS = {
    "INFO": "\033[36m",
    "WARN": "\033[33m",
    "ERROR": "\033[31m",
    "OK": "\033[32m",
}
_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"


def _log(step: str, msg: str, level: str = "INFO") -> None:
    ts = time.strftime("%H:%M:%S")
    color = _COLORS.get(level, _COLORS["INFO"])
    lvl = f"{color}{level:<5}{_RESET}"
    print(f"{_DIM}{ts}{_RESET} {lvl} {step:<16} │ {msg}")


def _banner(title: str) -> None:
    line = "─" * 52
    print(f"\n{_BOLD}{_COLORS['INFO']}┌{line}┐{_RESET}")
    print(f"{_BOLD}{_COLORS['INFO']}│ {title:^50} │{_RESET}")
    print(f"{_BOLD}{_COLORS['INFO']}└{line}┘{_RESET}\n")


# ============================================================
#  Ed25519 密钥对生成
# ============================================================

def generate_ed25519_keypair() -> tuple[str, str]:
    """
    生成 Ed25519 密钥对。

    :return: (private_key_pkcs8_base64, public_key_ssh)
    """
    private_key = Ed25519PrivateKey.generate()

    # PKCS8 DER 格式私钥 → base64
    pkcs8_der = private_key.private_bytes(
        encoding=Encoding.DER,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
    private_key_b64 = base64.b64encode(pkcs8_der).decode()

    # 原始公钥字节
    public_key = private_key.public_key()
    pub_bytes = public_key.public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )

    # 构造 SSH 公钥格式: ssh-ed25519 base64(blob)
    ssh_header = b"ssh-ed25519"
    blob = bytearray()
    blob.extend(len(ssh_header).to_bytes(4, "big"))
    blob.extend(ssh_header)
    blob.extend(len(pub_bytes).to_bytes(4, "big"))
    blob.extend(pub_bytes)
    ssh_b64 = base64.b64encode(bytes(blob)).decode()
    public_key_ssh = f"ssh-ed25519 {ssh_b64}"

    return private_key_b64, public_key_ssh


# ============================================================
#  JWT 解码（不验证签名，仅提取 claims）
# ============================================================

def decode_jwt_claims(jwt_token: str) -> dict[str, Any]:
    """
    解码 JWT payload（不验证签名）。

    :param jwt_token: JWT 字符串
    :return: claims dict
    """
    parts = jwt_token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format")

    # JWT payload 是 base64url 编码
    payload_b64 = parts[1]
    # 补齐 padding
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding

    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(payload_bytes)


# ============================================================
#  Session 获取
# ============================================================

def get_session_from_cookies(cookies: dict[str, str]) -> dict[str, Any]:
    """
    使用 cookies 调用 /api/auth/session 获取 accessToken 和账号信息。

    :param cookies: chatgpt.com 的 cookies dict
    :return: session 数据
    """
    r = requests.get(
        f"{CHATGPT_BASE}/api/auth/session",
        cookies=cookies,
        headers={"user-agent": USER_AGENT},
        impersonate=IMPERSONATE,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def get_session_from_access_token(access_token: str) -> dict[str, Any]:
    """
    如果已有 JWT access token，直接解码获取信息。

    :param access_token: ChatGPT session JWT
    :return: 包含 accessToken, accountId, email, userId, planType 的 dict
    """
    claims = decode_jwt_claims(access_token)
    auth_info = claims.get("https://api.openai.com/auth", {})
    profile = claims.get("https://api.openai.com/profile", {})

    return {
        "accessToken": access_token,
        "accountId": auth_info.get("chatgpt_account_id", ""),
        "userId": auth_info.get("chatgpt_user_id", ""),
        "email": profile.get("email", ""),
        "planType": auth_info.get("chatgpt_plan_type", "free"),
    }


# ============================================================
#  Agent 注册
# ============================================================

def register_agent(
    access_token: str,
    public_key_ssh: str,
) -> str:
    """
    在 auth.openai.com 注册 agent。

    :param access_token: ChatGPT session JWT
    :param public_key_ssh: SSH 格式的 Ed25519 公钥
    :return: agent_runtime_id
    """
    r = requests.post(
        f"{AUTHAPI_BASE}/v1/agent/register",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        json={
            "abom": {
                "agent_version": AGENT_VERSION,
                "agent_harness_id": AGENT_HARNESS_ID,
                "running_location": RUNNING_LOCATION,
            },
            "agent_public_key": public_key_ssh,
        },
        impersonate=IMPERSONATE,
        timeout=15,
    )

    if r.status_code != 200:
        raise RuntimeError(f"Agent registration failed: {r.status_code} {r.text}")

    data = r.json()
    agent_runtime_id = data.get("agent_runtime_id")
    if not agent_runtime_id:
        raise RuntimeError(f"No agent_runtime_id in response: {data}")

    return agent_runtime_id


# ============================================================
#  Task 注册（验证密钥对可用性）
# ============================================================

def register_task(
    access_token: str,
    agent_runtime_id: str,
    private_key_pkcs8_b64: str,
) -> str:
    """
    在 auth.openai.com 注册 task（验证密钥对可用性）。
    Codex CLI 启动时会自动执行此步骤。

    :param access_token: ChatGPT session JWT（仅用于验证，实际 Codex CLI 用密钥签名）
    :param agent_runtime_id: agent 运行时 ID
    :param private_key_pkcs8_b64: PKCS8 base64 私钥
    :return: encrypted_task_id
    """
    # 加载私钥
    pkcs8_der = base64.b64decode(private_key_pkcs8_b64)
    pem = b"-----BEGIN PRIVATE KEY-----\n" + base64.encodebytes(pkcs8_der) + b"-----END PRIVATE KEY-----\n"
    private_key = load_pem_private_key(pem, password=None)

    # 签名 payload: {agent_runtime_id}:{timestamp}
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = f"{agent_runtime_id}:{timestamp}"
    signature = private_key.sign(payload.encode())
    signature_b64 = base64.b64encode(signature).decode()

    r = requests.post(
        f"{AUTHAPI_BASE}/v1/agent/{agent_runtime_id}/task/register",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        json={
            "timestamp": timestamp,
            "signature": signature_b64,
        },
        impersonate=IMPERSONATE,
        timeout=15,
    )

    if r.status_code != 200:
        raise RuntimeError(f"Task registration failed: {r.status_code} {r.text}")

    data = r.json()
    return data.get("encrypted_task_id", "")


# ============================================================
#  auth.json 生成
# ============================================================

def generate_auth_json(
    agent_runtime_id: str,
    private_key_pkcs8_b64: str,
    account_id: str,
    chatgpt_user_id: str,
    email: str,
    plan_type: str = "free",
    chatgpt_account_is_fedramp: bool = False,
) -> dict[str, Any]:
    """
    生成 Codex CLI 的 auth.json。

    :return: auth.json dict
    """
    return {
        "auth_mode": "agent_identity",
        "agent_identity": {
            "agent_runtime_id": agent_runtime_id,
            "agent_private_key": private_key_pkcs8_b64,
            "account_id": account_id,
            "chatgpt_user_id": chatgpt_user_id,
            "email": email,
            "plan_type": plan_type,
            "chatgpt_account_is_fedramp": chatgpt_account_is_fedramp,
        },
    }


# ============================================================
#  完整流程
# ============================================================

def create_codex_agent_identity(
    access_token: str,
    output_path: str | None = None,
    verify_task: bool = True,
) -> dict[str, Any]:
    """
    完整流程：从 ChatGPT session JWT 创建 Codex Agent Identity auth.json。

    :param access_token: ChatGPT session JWT（从 /api/auth/session 获取的 accessToken）
    :param output_path: auth.json 输出路径，默认当前目录
    :param verify_task: 是否验证 task 注册（可选）
    :return: auth.json dict
    """
    _banner("Codex Agent Identity 注册  ·  by 久雾")

    # Step 1: 解码 JWT 获取账号信息
    _log("Step 1", "解码 JWT 获取账号信息...")
    session = get_session_from_access_token(access_token)
    account_id = session["accountId"]
    chatgpt_user_id = session["userId"]
    email = session["email"]
    plan_type = session["planType"]

    if not account_id or not chatgpt_user_id:
        raise RuntimeError(f"JWT 缺少必要字段: account_id={account_id}, user_id={chatgpt_user_id}")

    _log("Step 1", f"account_id={account_id}", "OK")
    _log("Step 1", f"user_id={chatgpt_user_id}", "OK")
    _log("Step 1", f"email={email}", "OK")
    _log("Step 1", f"plan_type={plan_type}", "OK")

    # Step 2: 生成 Ed25519 密钥对
    _log("Step 2", "生成 Ed25519 密钥对...")
    private_key_b64, public_key_ssh = generate_ed25519_keypair()
    _log("Step 2", f"private_key={private_key_b64[:40]}...", "OK")
    _log("Step 2", f"public_key={public_key_ssh[:50]}...", "OK")

    # Step 3: 注册 agent
    _log("Step 3", "在 auth.openai.com 注册 agent...")
    agent_runtime_id = register_agent(access_token, public_key_ssh)
    _log("Step 3", f"agent_runtime_id={agent_runtime_id}", "OK")

    # Step 4: 验证 task 注册（可选）
    if verify_task:
        _log("Step 4", "验证 task 注册...")
        try:
            task_id = register_task(access_token, agent_runtime_id, private_key_b64)
            _log("Step 4", f"task_id={task_id[:40]}...", "OK")
        except Exception as e:
            _log("Step 4", f"验证失败（不影响 auth.json）: {e}", "WARN")

    # Step 5: 生成 auth.json
    _log("Step 5", "生成 auth.json...")
    auth_json = generate_auth_json(
        agent_runtime_id=agent_runtime_id,
        private_key_pkcs8_b64=private_key_b64,
        account_id=account_id,
        chatgpt_user_id=chatgpt_user_id,
        email=email,
        plan_type=plan_type,
        chatgpt_account_is_fedramp=False,
    )

    if output_path is None:
        output_path = os.path.join(os.getcwd(), "auth.json")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(auth_json, f, indent=2, ensure_ascii=False)

    _log("Step 5", f"已保存到 {output_path}", "OK")

    return auth_json


# ============================================================
#  入口
# ============================================================

def main() -> None:
    """
    使用方式：

    1. 直接传入 JWT：
       python codex_agent.py --token "eyJhbGci..."

    2. 传入 JSON 文件（包含 accessToken）：
       python codex_agent.py --file session.json

    3. 交互式输入：
       python codex_agent.py
    """

    import argparse

    parser = argparse.ArgumentParser(description="Codex Agent Identity 自动注册")
    parser.add_argument("--token", type=str, help="ChatGPT session JWT (accessToken)")
    parser.add_argument("--file", type=str, help="包含 accessToken 的 JSON 文件路径")
    parser.add_argument("--output", "-o", type=str, default="auth.json", help="输出路径 (默认: auth.json)")
    parser.add_argument("--no-verify", action="store_true", help="跳过 task 注册验证")
    args = parser.parse_args()

    access_token = None

    if args.token:
        access_token = args.token
    elif args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            data = json.load(f)
            access_token = data.get("accessToken") or data.get("access_token")
    else:
        # 交互式输入
        print("请输入 ChatGPT session JWT (accessToken)：")
        print("（从 chatgpt.com /api/auth/session 获取）")
        access_token = input("> ").strip()

    if not access_token:
        print("错误：未提供 access_token")
        sys.exit(1)

    create_codex_agent_identity(
        access_token=access_token,
        output_path=args.output,
        verify_task=not args.no_verify,
    )


if __name__ == "__main__":
    main()
