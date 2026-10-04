"""提链完整日志：Legacy 事件流、Lumen 客户端、入队拒绝与任务中心读取。"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from config import extract_link as extract_cfg
from core import db, extract_link_service as service, operation_log, task_center_store
from core.lumen_flow_client import LumenClient

CDK = "EXTRACT-secret-123456"
AT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature"
TASK_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def http(monkeypatch):
    session = MagicMock()
    session.__enter__.return_value = session
    response = MagicMock(status_code=200, headers={"X-Trace": "trace-1"})
    response.json.return_value = {"results": [{"outcome": "ACCEPTED",
                                              "task": {"taskId": TASK_ID, "status": "RUNNING"}}]}
    session.request.return_value = response
    monkeypatch.setattr(requests, "Session", lambda: session)
    yield session


def test_lumen_client_logs_plaintext_credentials(http):
    with operation_log.operation(operation_log.EXTRACT, 31, truncate=False, header="提链（Lumen Flow）",
                                 provider_name="Lumen", link_type="upi", secrets=(CDK, AT)):
        task = LumenClient("https://lumen.example.test").submit(
            cdk=CDK, access_token=AT, payment_method="UPI")
    assert task["taskId"] == TASK_ID

    text = operation_log.read(operation_log.EXTRACT, 31)
    for expected in ("HTTP →", "HTTP ←", CDK, AT, "public/api/checkout/flow/submit-batch",
                     "ACCEPTED", "status=200", "trace-1"):
        assert expected in text, expected


def test_lumen_client_masks_credentials_when_disabled(monkeypatch, http):
    monkeypatch.setattr(extract_cfg, "EXTRACT_LOG_CREDENTIALS", False)
    with operation_log.operation(operation_log.EXTRACT, 32, truncate=False, header="提链（Lumen Flow）",
                                 secrets=(CDK, AT)):
        LumenClient("https://lumen.example.test").submit(cdk=CDK, access_token=AT, payment_method="UPI")

    text = operation_log.read(operation_log.EXTRACT, 32)
    assert CDK not in text and AT not in text
    assert "[凭据已脱敏]" in text
    assert TASK_ID in text


def test_legacy_run_logs_events_and_is_readable_from_task_center(monkeypatch):
    db._ensure_sqlite()
    db._save_accounts([{"id": 41, "email": "extract@example.test", "access_token": AT}])
    assert db.claim_account_extract(41, trigger="manual", link_type="upi")
    monkeypatch.setattr(service, "_legacy_create_job",
                        lambda **kwargs: {"job_id": "job-41", "cdk_remaining": 9})

    def events(**kwargs):
        yield "log", {"message": "开始提取链接"}
        yield "result", {"result": {"long_url": "https://payments.stripe.com/upi/instructions/ok"}}

    monkeypatch.setattr(service, "_iter_sse_events", events)
    assert service._QUEUE_SLOTS.acquire(blocking=False)
    result = service._legacy_run_extract(account_id=41, email="extract@example.test", access_token=AT,
                                        link_type="upi", cdk=CDK, trigger="manual",
                                        metadata={"provider_name": "Legacy Extractor"})
    assert result["status"] == "success"

    text = operation_log.read(operation_log.EXTRACT, 41)
    for expected in ("提链（Legacy）", CDK, AT, "job-41", "开始提取链接", "提链成功"):
        assert expected in text, expected

    task = task_center_store.list_latest_tasks_for_accounts([41], "extract_link")[0]
    detail = task_center_store.read_task_log(task["id"])
    assert "===== 提链日志 · extract-link-41.log =====" in detail
    assert "job-41" in detail and CDK in detail


def test_legacy_failure_is_logged_as_unknown(monkeypatch):
    db._ensure_sqlite()
    db._save_accounts([{"id": 42, "email": "fail@example.test", "access_token": AT}])
    assert db.claim_account_extract(42, trigger="manual", link_type="upi")
    monkeypatch.setattr(service, "_legacy_create_job", lambda **kwargs: {"job_id": "job-42"})

    def events(**kwargs):
        raise RuntimeError("事件流断开")
        yield  # pragma: no cover - 生成器语义

    monkeypatch.setattr(service, "_iter_sse_events", events)
    assert service._QUEUE_SLOTS.acquire(blocking=False)
    result = service._legacy_run_extract(account_id=42, email="fail@example.test", access_token=AT,
                                        link_type="upi", cdk=CDK, trigger="manual")
    assert result["status"] == "unknown"
    text = operation_log.read(operation_log.EXTRACT, 42)
    assert "事件流断开" in text
    assert "提链结束（unknown）" in text


def test_enqueue_rejection_is_logged():
    provider = {"id": 0, "name": "Legacy Extractor", "provider_type": "extract",
                "api_base": "https://extract.example.test"}
    service._log_enqueue(51, provider, "upi", CDK, accepted=False, trigger="manual",
                         access_token=AT, cdk_id=None)
    text = operation_log.read(operation_log.EXTRACT, 51)
    assert "提链任务未入队" in text
    assert "未入队" in text
    assert CDK in text and AT in text


def test_enqueue_acceptance_is_logged():
    provider = {"id": 3, "name": "UPI-GIT5", "provider_type": "upi_git5",
                "api_base": "https://upi.example.test"}
    service._log_enqueue(52, provider, "upi", CDK, accepted=True, trigger="manual_bulk",
                         access_token=AT, proxy_url="http://proxy.example:8080", cdk_id=7)
    text = operation_log.read(operation_log.EXTRACT, 52)
    assert "提链任务已入队" in text
    assert "本地提链队列已接受" in text
    assert "proxy.example:8080" in text


class FakeSseResponse:
    status_code = 200
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._lines)


def test_legacy_sse_events_are_logged(monkeypatch):
    session = MagicMock()
    session.get.return_value = FakeSseResponse([
        b"event: log\n", 'data: {"message": "\u5f00\u59cb\u63d0\u53d6"}\n'.encode(), b"\n",
        b"event: done\n", b"data: {}\n", b"\n",
    ])
    monkeypatch.setattr(service, "_session", lambda: session)
    with operation_log.operation(operation_log.EXTRACT, 61, truncate=False,
                                 header="提链（Legacy）", secrets=(CDK,)):
        events = list(service._iter_sse_events(job_id="job-61", cdk=CDK,
                                               api_base="https://extract.example.test"))
    assert [name for name, _ in events] == ["log", "done"]
    text = operation_log.read(operation_log.EXTRACT, 61)
    assert "提链事件流" in text
    assert "提链事件" in text
    assert "开始提取" in text


def test_plus_activation_log_merges_extract_and_payment():
    with operation_log.operation(operation_log.EXTRACT, 71, truncate=False, header="提链（Legacy）") as log:
        log.step("提链完成", job_id="job-71")
    with operation_log.operation(operation_log.PAYMENT, 71, truncate=False, header="提交支付") as log:
        log.step("提交完成", task_id="task-71")
    detail = task_center_store._operation_log_detail("plus_activation", 71)
    assert "===== 提链日志" in detail
    assert "===== 提交支付日志" in detail
    assert "job-71" in detail and "task-71" in detail

