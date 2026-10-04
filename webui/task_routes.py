"""Unified task-center views; execution stays with the owning service."""
from flask import jsonify, request

from core import registration_service, task_center_store


def _manual_otp_required() -> bool:
    """手动验证码模式：注册任务需要用户在 WebUI 任务旁提交邮箱验证码。"""
    from config import email as email_cfg

    return not bool(getattr(email_cfg, "USE_EMAIL_SERVICE", True))


def _with_registration_result(task):
    if task.get("job_id") is not None:
        info = registration_service.get_retry_info({**task, "id": task["job_id"]})
        task = {**task, "display_status": info["display_status"]}
        # 任务中心也要能提交手动验证码，否则用户只看到“请提交验证码”却无处输入。
        if _manual_otp_required():
            task["manual_otp_required"] = True
    return task


def register_task_routes(app):
    @app.get("/api/tasks/active")
    def task_center_active():
        items = [_with_registration_result(task) for task in task_center_store.list_active_tasks(limit=5000)]
        counts = task_center_store.task_status_counts()
        return jsonify({"ok": True, "items": items, "total": counts.get("active", len(items)),
                        "status_counts": counts})

    @app.get("/api/tasks/history")
    def task_center_history():
        page = max(1, request.args.get("page", default=1, type=int) or 1)
        page_size = max(1, min(100, request.args.get("page_size", default=20, type=int) or 20))
        result = task_center_store.list_history_tasks_page(
            limit=page_size, offset=(page - 1) * page_size,
        )
        result["items"] = [_with_registration_result(task) for task in result["items"]]
        return jsonify({**result, "ok": True, "page": page, "page_size": page_size})

    @app.get("/api/tasks/logs")
    def task_center_logs():
        """Read complete logs for several task-center IDs in one response."""
        raw_ids = request.args.get("task_ids", "")
        task_ids = [item.strip() for item in raw_ids.split(",") if item.strip()]
        if not task_ids:
            return jsonify({"ok": False, "error": "task_ids 不能为空"}), 400
        if len(task_ids) > 100:
            return jsonify({"ok": False, "error": "一次最多读取 100 个任务日志"}), 400
        items = []
        for task_id in task_ids:
            task = task_center_store.get_task(task_id)
            if task is None:
                continue
            if task.get("job_id") is not None:
                log = registration_service.read_job_log(task["job_id"])
            else:
                log = task_center_store.read_task_log(task_id)
            items.append({"task": _with_registration_result(task), "log": log})
        if not items:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        sections = []
        for item in items:
            task = item["task"]
            label = task.get("label") or task.get("job_type") or "任务"
            email = task.get("email") or ""
            heading = f"===== {label}"
            if email:
                heading += f" · {email}"
            heading += f" · {task['id']} ====="
            sections.append(f"{heading}\n{item['log']}")
        return jsonify({
            "ok": True,
            "items": items,
            "job": items[0]["task"],
            "log": "\n\n".join(sections),
            "complete": True,
        })

    @app.get("/api/accounts/tasks/latest")
    def account_latest_tasks():
        """Find the newest task-center execution for each requested account."""
        raw_ids = request.args.get("account_ids", "")
        account_ids = [item.strip() for item in raw_ids.split(",") if item.strip()]
        if not account_ids:
            return jsonify({"ok": False, "error": "account_ids 不能为空"}), 400
        try:
            items = task_center_store.list_latest_tasks_for_accounts(
                account_ids, request.args.get("job_type")
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "items": items, "task_ids": [item["id"] for item in items]})

    @app.get("/api/tasks/<task_id>/log")
    def task_center_log(task_id):
        task = task_center_store.get_task(task_id)
        if task is None:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        if task.get("job_id") is not None:
            log = registration_service.read_job_log(task["job_id"])
        else:
            log = task_center_store.read_task_log(task_id)
        return jsonify({
            "ok": True,
            "job": _with_registration_result(task),
            "log": log,
            "complete": True,
        })

    @app.post("/api/tasks/<task_id>/<action>")
    def task_center_action(task_id, action):
        handlers = {
            "pause": registration_service.pause_job,
            "resume": registration_service.resume_job,
            "cancel": registration_service.request_stop_job,
        }
        if action not in handlers:
            return jsonify({"ok": False, "error": "不支持的任务操作"}), 404
        task = task_center_store.get_task(task_id)
        if task is None:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        # Account operations own their execution and may already have submitted
        # remote side effects. Never simulate cancellation by changing a row.
        if task.get("job_id") is None or not task.get("capabilities", {}).get(action):
            return jsonify({"ok": False, "error": "该任务当前不支持此操作，请查看任务进度"}), 409
        result = handlers[action](task["job_id"])
        if not result.get("ok"):
            return jsonify(result), int(result.get("status") or 400)
        return jsonify({**result, "task_id": task_id})
