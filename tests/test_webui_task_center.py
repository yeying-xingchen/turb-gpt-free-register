# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class TaskCenterWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.get_retry_info", return_value={
        "retryable": False, "retry_action": None, "retry_label": None, "display_status": "paused",
    })
    @patch("webui.app.db.job_status_counts", return_value={"active": 1, "paused": 1})
    @patch("webui.app.db.list_active_jobs")
    def test_active_snapshot_contains_progress(self, list_active_jobs, _counts, _retry_info):
        list_active_jobs.return_value = [{
            "id": 41, "status": "paused", "email": "demo@example.com",
            "progress": 42, "stage": "等待验证码", "progress_message": "任务已暂停，等待恢复",
            "created_at": "2026-01-01T00:00:00", "started_at": "2026-01-01T00:01:00",
        }]

        response = self.client.get("/api/jobs/active")

        self.assertEqual(response.status_code, 200)
        item = response.get_json()["items"][0]
        self.assertEqual(item["progress"], 42)
        self.assertEqual(item["stage"], "等待验证码")
        self.assertEqual(item["progress_message"], "任务已暂停，等待恢复")

    @patch("webui.app.svc.request_stop_job", return_value={"ok": True, "state": "stopping", "job_id": 41})
    @patch("webui.app.svc.resume_job", return_value={"ok": True, "state": "running", "job_id": 41})
    @patch("webui.app.svc.pause_job", return_value={"ok": True, "state": "paused", "job_id": 41})
    def test_task_controls_delegate_to_service(self, pause, resume, cancel):
        self.assertEqual(self.client.post("/api/jobs/41/pause", json={}).status_code, 200)
        self.assertEqual(self.client.post("/api/jobs/41/resume", json={}).status_code, 200)
        self.assertEqual(self.client.post("/api/jobs/41/cancel", json={}).status_code, 200)
        pause.assert_called_once_with(41)
        resume.assert_called_once_with(41)
        cancel.assert_called_once_with(41)


    @patch("webui.task_routes.registration_service.get_retry_info", return_value={"display_status": "running"})
    @patch("webui.task_routes.task_center_store.task_status_counts", return_value={"active": 2, "running": 2})
    @patch("webui.task_routes.task_center_store.list_active_tasks")
    def test_registration_tasks_expose_manual_otp_flag_only_in_manual_mode(
        self, list_active_tasks, _counts, _retry_info
    ):
        """任务中心必须能给等待验证码的注册任务提供输入入口。"""
        list_active_tasks.return_value = [
            {
                "id": "registration-9", "job_id": 9, "job_type": "registration", "status": "running",
                "source_status": "running", "email": "demo@example.com",
                "capabilities": {"pause": True, "resume": False, "cancel": True},
            },
            {
                "id": "account-3", "job_id": None, "job_type": "live_check", "status": "running",
                "source_status": "running", "email": "demo@example.com",
                "capabilities": {"pause": False, "resume": False, "cancel": False},
            },
        ]

        with patch("config.email.USE_EMAIL_SERVICE", False):
            items = self.client.get("/api/tasks/active").get_json()["items"]
            self.assertTrue(items[0]["manual_otp_required"])
            # 账号类任务没有注册任务，不能出现验证码输入入口。
            self.assertNotIn("manual_otp_required", items[1])
            self.assertEqual(items[0]["display_status"], "running")

        with patch("config.email.USE_EMAIL_SERVICE", True):
            items = self.client.get("/api/tasks/active").get_json()["items"]
            self.assertNotIn("manual_otp_required", items[0])

    @patch("webui.task_routes.registration_service.get_retry_info", return_value={"display_status": "success"})
    @patch("webui.task_routes.task_center_store.list_history_tasks_page")
    def test_history_snapshot_keeps_manual_otp_flag_for_registration(self, list_history, _retry_info):
        list_history.return_value = {
            "items": [{
                "id": "registration-4", "job_id": 4, "job_type": "registration", "status": "failed",
                "source_status": "failed", "email": "demo@example.com", "capabilities": {},
            }],
            "total": 1,
        }
        with patch("config.email.USE_EMAIL_SERVICE", False):
            items = self.client.get("/api/tasks/history").get_json()["items"]
        self.assertTrue(items[0]["manual_otp_required"])


if __name__ == "__main__":
    unittest.main()
