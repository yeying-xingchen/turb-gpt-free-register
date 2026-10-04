"""提交支付完整日志：HTTP 层留痕、任务中心读取与脱敏开关。"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from config import scan_api as payment_cfg
from core import db, operation_log, scan_api_service as service, task_center_store
from core.scan_api_client import ScanApiClient

CDK = "PAYMENT-secret-123456"
AT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature"
LINK = "https://payments.stripe.com/upi/instructions/test"
SUBMIT_KEY = "submit-key-12345"


@pytest.fixture
def http(monkeypatch):
    session = MagicMock()
    session.__enter__.return_value = session
    response = MagicMock(status_code=201, headers={"X-Request-Id": "request-9"})
    response.json.return_value = {"data": {"task": {"id": "task-9", "status": "queued"}}}
    session.request.return_value = response
    monkeypatch.setattr(requests, "Session", lambda: session)
    yield session


def test_scan_api_client_logs_request_and_response(http):
    with operation_log.operation(operation_log.PAYMENT, 21, truncate=False, header="提交支付",
                                 provider="v1", secrets=(CDK,)):
        client = ScanApiClient(api_base="https://scan.example.test/api/v1")
        result = client.submit_upi(cdk=CDK, link=LINK, email="buyer@example.test",
                                   idempotency_key=SUBMIT_KEY)
    assert result["task_id"] == "task-9"

    text = operation_log.read(operation_log.PAYMENT, 21)
    for expected in ("HTTP →", "HTTP ←", CDK, "X-CDK-Code", SUBMIT_KEY, LINK, "buyer@example.test",
                     "status=201", "request-9", "task-9"):
        assert expected in text, expected


def test_scan_api_client_masks_credentials_when_disabled(monkeypatch, http):
    monkeypatch.setattr(payment_cfg, "PAYMENT_LOG_CREDENTIALS", False)
    with operation_log.operation(operation_log.PAYMENT, 22, truncate=False, header="提交支付",
                                 secrets=(CDK,)):
        ScanApiClient(api_base="https://scan.example.test/api/v1").submit_upi(
            cdk=CDK, link=LINK, email="buyer@example.test", idempotency_key=SUBMIT_KEY)

    text = operation_log.read(operation_log.PAYMENT, 22)
    assert CDK not in text
    assert "[凭据已脱敏]" in text
    assert "task-9" in text


def seed_account(account_id=7, email="buyer@example.test"):
    db._ensure_sqlite()
    db._save_accounts([{"id": account_id, "email": email, "access_token": AT,
                        "extract_link_status": "success", "extract_link_type": "upi",
                        "extract_link_long_url": LINK}])


def fake_client(monkeypatch, account_id=7, email="buyer@example.test"):
    client = MagicMock(api_base="https://scan.example.test/api/v1", timeout=30)
    client.submit_upi.return_value = {
        "task": {"id": f"task-{account_id}", "status": "queued", "message": "平台已受理"},
        "task_id": f"task-{account_id}", "request_id": "request-7", "http_status": 201,
        "duplicate": False, "uncertain": False}
    client.get_task.return_value = {
        "task": {"id": f"task-{account_id}", "status": "succeeded", "message": "平台任务状态"},
        "task_id": f"task-{account_id}", "request_id": "request-8", "http_status": 200,
        "duplicate": False, "uncertain": False}
    monkeypatch.setattr(service, "_client", lambda *args, **kwargs: client)
    return client


def test_submit_accounts_log_is_readable_from_task_center(monkeypatch):
    seed_account()
    fake_client(monkeypatch)
    result = service.submit_accounts(account_ids=[7], cdk=CDK)
    assert result["created_count"] == 1

    text = operation_log.read(operation_log.PAYMENT, 7)
    for expected in ("提交支付", "provider=v1", CDK, LINK, "task-7", "收到支付平台受理结果",
                     "本次提交结束", "批次提交结束"):
        assert expected in text, expected

    task = task_center_store.list_latest_tasks_for_accounts([7], "scan_payment")[0]
    assert task["job_type"] == "scan_payment"
    detail = task_center_store.read_task_log(task["id"])
    assert "===== 提交支付日志 · scan-payment-7.log =====" in detail
    assert CDK in detail and "task-7" in detail


def test_query_accounts_logs_remote_lookup(monkeypatch):
    seed_account()
    fake_client(monkeypatch)
    service.submit_accounts(account_ids=[7], cdk=CDK)
    result = service.query_accounts(account_ids=[7], cdk=CDK)
    assert result["count"] == 1

    text = operation_log.read(operation_log.PAYMENT, 7)
    assert "查询支付任务" in text
    assert "查询原支付任务" in text
    assert "收到查询结果" in text
    assert "status=succeeded" in text


def test_query_without_local_record_is_logged(monkeypatch):
    db._ensure_sqlite()
    fake_client(monkeypatch)
    result = service.query_accounts(account_ids=[77], cdk=CDK)
    assert result["failed_count"] == 1
    text = operation_log.read(operation_log.PAYMENT, 77)
    assert "查询支付任务失败" in text
    assert "没有已保存的支付任务" in text


def test_missing_account_submission_is_logged(monkeypatch):
    db._ensure_sqlite()
    fake_client(monkeypatch)
    result = service.submit_accounts(account_ids=[88], cdk=CDK)
    assert result["failed_count"] == 1
    text = operation_log.read(operation_log.PAYMENT, 88)
    assert "账号不存在" in text
