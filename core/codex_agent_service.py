# -*- coding: utf-8 -*-
"""Codex Agent Identity 生成后台队列。"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from core import db

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_OUTPUT_DIR = _PROJECT_ROOT / "codex_agent_accounts"
_WORKERS = 3
_QUEUE_LIMIT = 500
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="codex-agent")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def _safe_email_filename(email: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("@", ".", "-", "_") else "_" for ch in (email or "account"))


def _run_generate(*, account_id: int, email: str, access_token: str, trigger: str, verify_task: bool) -> dict:
    try:
        if not db.mark_account_codex_agent_running(account_id):
            return {"ok": False, "error": "账号已删除或 Codex Agent 状态已被重置"}
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = _OUTPUT_DIR / f"codex-agent-{_safe_email_filename(email)}.json"
        from core.codex_agent import create_codex_agent_identity

        auth_json = create_codex_agent_identity(
            access_token=access_token,
            output_path=str(output_path),
            verify_task=verify_task,
        )
        identity = auth_json.get("agent_identity") if isinstance(auth_json, dict) else {}
        result = {
            "ok": True,
            "status": "success",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "message": "Codex Agent Token 已生成",
            "agent_runtime_id": (identity or {}).get("agent_runtime_id"),
            "auth_path": str(output_path),
            "auth_json": auth_json,
        }
        db.update_account_codex_agent(account_id, result)
        logger.info("[CodexAgent] 生成成功: %s runtime=%s", email, result.get("agent_runtime_id") or "-")
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
        try:
            db.update_account_codex_agent(account_id, result)
        except Exception:
            logger.exception("[CodexAgent] 写入失败状态异常: account_id=%s", account_id)
        logger.exception("[CodexAgent] 生成失败: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_codex_agent(*, account_id: int, email: str, access_token: str, trigger: str = "manual", verify_task: bool = True) -> dict:
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "Codex Agent 队列已满"}
    try:
        if not db.claim_account_codex_agent(account_id, trigger=trigger):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在生成 Codex Agent Token"}
        fut = _EXECUTOR.submit(
            _run_generate,
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=trigger,
            verify_task=verify_task,
        )
        return {"accepted": True, "busy": False, "future": fut}
    except Exception:
        _QUEUE_SLOTS.release()
        raise
