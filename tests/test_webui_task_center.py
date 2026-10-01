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


if __name__ == "__main__":
    unittest.main()
