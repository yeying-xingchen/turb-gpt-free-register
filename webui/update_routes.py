# -*- coding: utf-8 -*-
"""GitHub 更新检查接口（已登录用户可用）。

    GET  /api/update/status   读取上次检查结论（必要时后台补一次检查）
    POST /api/update/check    立即检查一次
    POST /api/update/apply    一键快进更新（要求工作区干净且可快进）
"""
from __future__ import annotations

import logging
import threading

from flask import jsonify

from core import update_checker

logger = logging.getLogger(__name__)

_kick_lock = threading.Lock()


def _kick_background_check() -> None:
    """从未检查过、或换了检查目标时补一次后台检查，让页面尽快看到结论。"""
    if update_checker.update_check_in_progress():
        return
    status = update_checker.get_status()
    if not status.get("enabled", True):
        return
    if status.get("checked_at") and update_checker.cache_is_current():
        return
    with _kick_lock:
        if update_checker.update_check_in_progress():
            return
        threading.Thread(
            target=update_checker.check_for_update,
            kwargs={"trigger": "startup"},
            name="update-check-initial",
            daemon=True,
        ).start()


def register_update_routes(app) -> None:
    @app.get("/api/update/status")
    def api_update_status():
        _kick_background_check()
        return jsonify({"ok": True, **update_checker.get_status()})

    @app.post("/api/update/check")
    def api_update_check():
        # 检查失败属于业务结果（网络/仓库/配额），照常 200 返回原因，由卡片展示。
        result = update_checker.check_for_update(trigger="manual")
        return jsonify({"ok": True, **result})

    @app.post("/api/update/apply")
    def api_update_apply():
        result = update_checker.apply_update()
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error") or "更新失败", **result}), 400
        # 更新后立刻复核一次，让界面显示真实的最新状态。
        try:
            update_checker.check_for_update(trigger="after_apply")
        except Exception:
            logger.exception("更新后复核失败")
        return jsonify({"ok": True, **result})
