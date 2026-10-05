"""Unified task-center views and controls; execution stays with the owning service."""
from flask import jsonify, request

from core import codex_retry_service, registration_service, task_center_store, task_control

ACTIONS = ("pause", "resume", "cancel")
# 任务中心的并发设置写回 .env，配置页与重启后保持一致。
CONCURRENCY_KEYS = {
    "registration": "REGISTRATION_WORKERS",
    "live_check": "LIVE_CHECK_WORKERS",
    "plan_check": "PLAN_CHECK_WORKERS",
    "codex_agent": "CODEX_AGENT_WORKERS",
    "totp_setup": "TWOFA_WORKERS",
    "email_change": "EMAIL_CHANGE_WORKERS",
    "extract_link": "EXTRACT_LINK_WORKERS",
    "plus_activation": "PLUS_ACTIVATION_WORKERS",
}
# 热加载配置后需要重新应用并发数的后台队列。
_SETTINGS_SERVICES = (
    "registration_service", "live_check_service", "plan_check_service", "codex_agent_service",
    "twofa_service", "email_change_service", "extract_link_service", "plus_activation_service",
)


def _apply_runtime_settings() -> None:
    import importlib

    for name in _SETTINGS_SERVICES:
        module = importlib.import_module(f"core.{name}")
        handler = getattr(module, "apply_settings", None)
        if callable(handler):
            handler()


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


def _task_filters():
    """解析任务中心筛选参数；未知类型/状态返回 400，避免静默匹配不到数据。"""
    args = request.args
    try:
        job_type, status, keyword = task_center_store.normalize_filters(
            args.get("job_type"), args.get("status"), args.get("q") or args.get("keyword"),
        )
    except ValueError as exc:
        return None, (jsonify({"ok": False, "error": str(exc)}), 400)
    return {"job_type": job_type, "status": status, "keyword": keyword}, None


def register_task_routes(app):
    @app.get("/api/tasks/active")
    def task_center_active():
        filters, error = _task_filters()
        if error:
            return error
        items = [_with_registration_result(task)
                 for task in task_center_store.list_active_tasks(limit=5000, **filters)]
        counts = task_center_store.task_status_counts(**filters)
        # 侧边栏徽章是全局活跃任务数：有筛选时单独统计，避免导航数字跟着筛选缩水。
        global_counts = counts if not any(filters.values()) else task_center_store.task_status_counts()
        return jsonify({"ok": True, "items": items, "total": counts.get("active", len(items)),
                        "status_counts": counts, "global_status_counts": global_counts,
                        "pools": task_center_store.concurrency_overview(),
                        "filters": task_center_store.filter_options()})

    @app.get("/api/tasks/history")
    def task_center_history():
        filters, error = _task_filters()
        if error:
            return error
        page = max(1, request.args.get("page", default=1, type=int) or 1)
        page_size = max(1, min(100, request.args.get("page_size", default=20, type=int) or 20))
        result = task_center_store.list_history_tasks_page(
            limit=page_size, offset=(page - 1) * page_size, **filters,
        )
        result["items"] = [_with_registration_result(task) for task in result["items"]]
        return jsonify({**result, "ok": True, "page": page, "page_size": page_size,
                        "filters": task_center_store.filter_options()})

    @app.get("/api/tasks/concurrency")
    def task_center_concurrency():
        return jsonify({"ok": True, "pools": task_center_store.concurrency_overview()})

    @app.post("/api/tasks/concurrency")
    def task_center_set_concurrency():
        """运行中修改某类后台任务的并发数，立即对排队和运行中的任务生效。"""
        data = request.get_json(silent=True) or {}
        job_type = str(data.get("job_type") or data.get("pool") or "").strip()
        if not job_type:
            return jsonify({"ok": False, "error": "缺少 job_type"}), 400
        try:
            workers = int(data.get("workers"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": f"workers 必须是 1–{task_control.MAX_WORKERS} 的整数"}), 400
        if not 1 <= workers <= task_control.MAX_WORKERS:
            return jsonify({"ok": False, "error": f"workers 必须是 1–{task_control.MAX_WORKERS} 的整数"}), 400
        result = task_control.set_pool_workers(job_type, workers)
        if not result.get("ok"):
            return jsonify(result), 404

        label = task_center_store.LABELS.get(job_type, job_type)
        payload = {"ok": True, "job_type": job_type, "workers": result["workers"], "persisted": "",
                   "message": f"已将{label}并发调整为 {result['workers']}"}
        key = CONCURRENCY_KEYS.get(job_type)
        if key:
            from webui import config_editor
            try:
                config_editor.update_config({key: workers})
                import config as config_pkg
                config_pkg.reload_all()
                _apply_runtime_settings()
                payload["persisted"] = key
            except Exception as exc:  # 写盘失败不影响本次内存生效
                app.logger.exception("保存并发配置失败")
                payload["warning"] = f"已对当前队列生效，但写入配置失败（{type(exc).__name__}）：{exc}"
        payload["pools"] = task_center_store.concurrency_overview()
        return jsonify(payload)

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
        if action not in ACTIONS:
            return jsonify({"ok": False, "error": "不支持的任务操作"}), 404
        task = task_center_store.get_task(task_id)
        if task is None:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        if not task.get("capabilities", {}).get(action):
            return jsonify({"ok": False, "error": "该任务当前不支持此操作，请查看任务进度"}), 409
        # 账号操作由各自的后台队列执行；控制层只发暂停/取消信号并同步状态投影。
        if task.get("job_id") is None:
            return _account_task_action(task_id, task, action)
        handlers = {
            "pause": registration_service.pause_job,
            "resume": registration_service.resume_job,
            "cancel": registration_service.request_stop_job,
        }
        result = handlers[action](task["job_id"])
        if not result.get("ok"):
            return jsonify(result), int(result.get("status") or 400)
        if action == "cancel" and task.get("job_type") == "codex_retry" and task.get("email"):
            # Codex 授权可能阻塞在浏览器/接码等待，补一条停止信号让线程尽快退出。
            codex_retry_service.request_stop(task["email"])
        return jsonify({**result, "task_id": task_id})

    def _account_task_action(task_id, task, action):
        job_type = str(task.get("job_type") or "")
        account_id = task.get("account_id")
        result = task_control.apply_action(action, job_type, account_id)
        if not result.get("ok"):
            return jsonify(result), int(result.get("status") or 400)
        # 运行中的取消先显示“取消中”，其余情况直接显示目标状态。
        if action == "cancel" and task.get("status") not in {"pending", "paused"}:
            display_state = "stopping"
        else:
            display_state = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[action]
        task_center_store.set_task_control_state(job_type, account_id, display_state)
        return jsonify({**result, "task_id": task_id, "display_state": display_state})
