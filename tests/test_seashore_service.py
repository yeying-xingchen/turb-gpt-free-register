# -*- coding: utf-8 -*-
"""seashore 发布者平台在服务层的接线：已保存 CDK、AT 必填、扣次不可重提。"""
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from core import db, payment_provider_store as credentials
from core import scan_api_service as service, scan_payment_store as store
from core.scan_api_client import ScanApiError

CDK = "PBK-04A0-3254-AEB7-9C31"
OTHER = "PBK-1111-2222-3333-4444"
AT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJidXllciJ9.signature"
LINK = "https://payments.stripe.com/upi/instructions/example"


class SeashoreServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        paths = {name: root / name for name, value in vars(db).items()
                 if name.startswith("_") and isinstance(value, Path)}
        paths.update(_DATA_DIR=root, _LOG_DIR=root / "logs", _SQLITE_READY=False, _SQLITE_READY_PATH=None)
        self.paths = patch.multiple(db, **paths)
        self.paths.start()
        self.addCleanup(self.paths.stop)
        db._ensure_sqlite()
        self.account = {"id": 7, "email": "buyer@example.test", "access_token": AT,
                        "extract_link_status": "success", "extract_link_type": "upi",
                        "extract_link_long_url": LINK}
        with closing(db._sqlite_conn()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO accounts(id,email,payload) VALUES(?,?,?)",
                         (7, self.account["email"], json.dumps(self.account)))
        self.client = Mock(api_base="https://seashore.lol/api/publisher", timeout=30)
        self.client.submit_upi.return_value = self.response("queued", task_id="ORD-1A2B3C4D")
        self.client.get_task.return_value = self.response("completed", task_id="ORD-1A2B3C4D")
        self.client_patch = patch.object(service, "_client", return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        credentials.ensure_defaults()

    def response(self, status, task_id="ORD-1A2B3C4D", http_status=200, uncertain=False,
                 provider_status=None, message="平台任务状态"):
        task = {"id": task_id, "status": status, "message": message,
                "providerStatus": provider_status or status, "canCancel": True}
        if status == "verifying":
            task["verifying"] = True
        return {"task": task, "task_id": task_id, "request_id": None, "http_status": http_status,
                "duplicate": False, "uncertain": uncertain}

    def submit(self, **kwargs):
        return service.submit_accounts(account_ids=kwargs.pop("account_ids", [7]),
                                       provider=kwargs.pop("provider", "seashore"), **kwargs)

    def test_submit_sends_email_at_and_saved_credential(self):
        saved = credentials.save_cdk(credentials.get_provider_by_type("seashore")["id"], {"cdk": CDK})
        result = self.submit(cdk_id=saved["id"])
        self.assertEqual(result["created_count"], 1)
        kwargs = self.client.submit_upi.call_args.kwargs
        self.assertEqual(kwargs["cdk"], CDK)  # 明文只到客户端，不落库
        self.assertEqual(kwargs["access_token"], AT)
        self.assertEqual(kwargs["link"], LINK)
        self.assertEqual(kwargs["email"], "buyer@example.test")
        self.assertEqual(store.latest(7)["provider"], "seashore")
        self.assertNotIn(CDK, json.dumps(store.latest(7)))
        self.assertNotIn(AT, json.dumps(store.latest(7)))
        self.assertFalse(store.public_view(store.latest(7))["can_retry_same_key"])

    def test_submit_requires_at_and_rejects_session_mode(self):
        self.account["access_token"] = ""
        with closing(db._sqlite_conn()) as conn, conn:
            conn.execute("UPDATE accounts SET payload=? WHERE id=7", (json.dumps(self.account),))
        result = self.submit(cdk=CDK)
        self.assertEqual(result["failed_count"], 1)
        self.assertIn("完整 AT", result["failed"][0]["error"])
        self.client.submit_upi.assert_not_called()
        with self.assertRaises(ValueError):
            service.submit_accounts(account_ids=[7], provider="seashore", cdk=CDK, auth_session={"identity": "x"})

    def test_saved_credential_must_match_platform(self):
        masi = credentials.save_cdk(credentials.get_provider_by_type("masi")["id"], {"cdk": OTHER})
        with self.assertRaises(ValueError):
            self.submit(cdk_id=masi["id"])
        self.client.submit_upi.assert_not_called()

    def test_query_and_verify_accept_saved_credential(self):
        saved = credentials.save_cdk(credentials.get_provider_by_type("seashore")["id"], {"cdk": CDK})
        self.submit(cdk_id=saved["id"])
        query = service.query_accounts(account_ids=[7], cdk_id=saved["id"])
        self.assertEqual(query["count"], 1)
        self.assertEqual(store.latest(7)["status"], "completed")
        self.client.get_task.assert_called_once_with(cdk=CDK, task_id="ORD-1A2B3C4D")
        # 落库的是原凭据摘要：换一条 CDK 不能查询原任务。
        denied = service.query_accounts(account_ids=[7], cdk=OTHER)
        self.assertEqual(denied["failed_count"], 1)
        self.assertIn("原 CDK", denied["failed"][0]["error"])
        self.client.verify_cdk.return_value = {"data": {"remaining_uses": 8, "balance": 8}, "http_status": 200}
        self.assertEqual(service.verify_cdk(provider="seashore", cdk_id=saved["id"])["data"]["remaining_uses"], 8)
        self.client.verify_cdk.assert_called_once_with(CDK)

    def test_charged_rejection_blocks_same_link_resubmit(self):
        self.client.submit_upi.side_effect = ScanApiError(
            "链接声明有效期异常（已扣 1 次，未创建任务）", status=400, code="paylink_ttl_untrusted",
            charged=True, created=False, uncertain=False)
        first = self.submit(cdk=CDK)
        self.assertEqual(first["failed_count"], 1)
        record = store.latest(7)
        self.assertEqual(record["status"], "rejected")
        self.assertTrue(record["charged"])
        # 同一条链接再点一次不会向平台重提，避免再被扣一次。
        with self.assertRaises(ValueError):
            store.reserve(account_id=7, provider="seashore", api_base=self.client.api_base, cdk=CDK,
                          body=record["body"], idempotency_key=record["idempotency_key"])
        self.assertEqual(self.client.submit_upi.call_count, 1)

    def test_uncertain_submit_is_never_resent_automatically(self):
        self.client.submit_upi.side_effect = ScanApiError("网络结果未确认", code="TRANSPORT_ERROR", uncertain=True)
        first = self.submit(cdk=CDK)
        self.assertEqual(first["unknown_count"], 1)
        record = store.latest(7)
        self.assertEqual(record["status"], "unknown")
        self.client.submit_upi.side_effect = None
        again = self.submit(cdk=CDK)
        self.assertEqual(again["unknown_count"], 1)
        self.assertEqual(self.client.submit_upi.call_count, 1)

    def test_not_activated_and_link_statuses_are_terminal(self):
        for upstream in ("not_activated", "expired", "failed", "cancelled"):
            self.assertIn(upstream, store.TERMINAL)
        self.assertNotIn("not_activated", store._FALLBACK_TERMINAL)
        record = {"status": "not_activated", "task": {"status": "not_activated"}}
        self.assertFalse(store.can_fallback(record))

    def test_verifying_state_blocks_fallback_until_settled(self):
        verifying = self.response("verifying", provider_status="submitted")
        self.assertTrue(verifying["task"]["verifying"])
        self.client.submit_upi.return_value = verifying
        self.submit(cdk=CDK)
        self.assertFalse(store.can_fallback(store.latest(7)))
        self.client.get_task.return_value = self.response("completed")
        service.query_accounts(account_ids=[7], cdk=CDK)
        self.assertEqual(store.latest(7)["status"], "completed")

    def test_provider_status_and_status_text_are_preserved(self):
        self.client.submit_upi.return_value = self.response(
            "queued", provider_status="pending", message="等待接单")
        self.submit(cdk=CDK)
        task = store.latest(7)["task"]
        self.assertEqual(task["providerStatus"], "pending")
        self.assertEqual(task["message"], "等待接单")


if __name__ == "__main__":
    unittest.main()
