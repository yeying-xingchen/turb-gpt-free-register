"""提交支付 / 提链完整日志的写入契约（默认明文凭据，可切换脱敏）。"""
from __future__ import annotations

import pytest

from config import extract_link as extract_cfg
from config import scan_api as payment_cfg
from core import operation_log

CDK = "PAYMENT-secret-123456"
AT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature"
LINK = "https://payments.stripe.com/upi/instructions/test"


def test_payment_log_records_full_http_flow_with_plaintext_credentials():
    log = operation_log.start(operation_log.PAYMENT, 7, header="提交支付", provider="v1", secrets=(CDK,))
    log.step("开始提交支付", cdk=CDK, access_token=AT, link=LINK)
    log.request(method="POST", url="https://scan.example.test/api/v1/scan-requests",
                headers={"X-CDK-Code": CDK, "Idempotency-Key": "scan-key-1"},
                body={"channel": "upi", "link": LINK, "email": "buyer@example.test"},
                timeout=30, secrets=(CDK,))
    log.response(status=201, headers={"X-Request-Id": "request-1"},
                 body={"data": {"task": {"id": "task-1", "status": "queued"}}}, elapsed=0.125, secrets=(CDK,))
    log.failure(RuntimeError("示例异常"), note="示例异常说明")
    log.close()

    text = operation_log.read(operation_log.PAYMENT, 7)
    assert operation_log.payment_log_path(7).name == "scan-payment-7.log"
    for expected in ("提交支付", "provider=v1", f"cdk={CDK}", f"access_token={AT}", "HTTP →",
                     "https://scan.example.test/api/v1/scan-requests", "X-CDK-Code", "HTTP ←",
                     "status=201", "request-1", "task-1", "0.125s", "示例异常说明"):
        assert expected in text, expected


def test_extract_log_masks_credentials_when_switch_is_off(monkeypatch):
    monkeypatch.setattr(extract_cfg, "EXTRACT_LOG_CREDENTIALS", False)
    log = operation_log.start(operation_log.EXTRACT, 9, header="提链（Legacy）", cdk=CDK, secrets=(CDK, AT))
    log.step("创建提链任务", cdk=CDK, access_token=AT, link_type="upi")
    log.request(method="POST", url="https://extract.example.test/api/extract",
                headers={"X-CDK": CDK}, body={"cdk": CDK, "token": AT}, secrets=(CDK, AT))
    log.close()

    text = operation_log.read(operation_log.EXTRACT, 9)
    assert operation_log.extract_log_path(9).name == "extract-link-9.log"
    assert CDK not in text and AT not in text
    assert "[凭据已脱敏]" in text
    assert "link_type=upi" in text


def test_logs_append_across_runs_and_rotate_when_large(monkeypatch):
    first = operation_log.start(operation_log.PAYMENT, 3, header="第一次提交")
    first.step("第一步")
    first.close()
    second = operation_log.start(operation_log.PAYMENT, 3, header="第二次提交")
    second.step("第二步")
    second.close()
    text = operation_log.read(operation_log.PAYMENT, 3)
    for expected in ("第一次提交", "第二次提交", "第一步", "第二步"):
        assert expected in text

    monkeypatch.setattr(payment_cfg, "PAYMENT_LOG_MAX_BYTES", 4096)
    log = operation_log.start(operation_log.PAYMENT, 4, header="轮转")
    for index in range(200):
        log.step("填充", payload="x" * 200, index=index)
    log.close()
    assert operation_log.payment_log_path(4).with_name("scan-payment-4.log.1").exists()


def test_batch_operation_writes_every_account_log():
    with operation_log.batch_operation(operation_log.EXTRACT, [1, 2, 3], header="批量提链") as log:
        log.step("批次已受理", batch_id="batch-1")
    for account_id in (1, 2, 3):
        text = operation_log.read(operation_log.EXTRACT, account_id)
        assert "批量提链" in text and "batch-1" in text


def test_disabled_log_writes_nothing(monkeypatch):
    monkeypatch.setattr(payment_cfg, "PAYMENT_LOG_ENABLED", False)
    with operation_log.operation(operation_log.PAYMENT, 5, header="关闭") as log:
        log.step("不会写入")
    assert operation_log.read(operation_log.PAYMENT, 5) == ""


def test_read_and_clear_round_trip():
    with operation_log.operation(operation_log.PAYMENT, 6, header="清理") as log:
        log.step("写入一行")
    assert "写入一行" in operation_log.read(operation_log.PAYMENT, 6)
    assert operation_log.clear(operation_log.PAYMENT, 6) is True
    assert operation_log.read(operation_log.PAYMENT, 6) == ""


def test_long_values_are_truncated(monkeypatch):
    monkeypatch.setattr(payment_cfg, "PAYMENT_LOG_VALUE_LIMIT", 200)
    with operation_log.operation(operation_log.PAYMENT, 8, header="截断") as log:
        log.step("超长字段", payload="y" * 5000)
    text = operation_log.read(operation_log.PAYMENT, 8)
    assert "已截断" in text
    assert len(text) < 2000


def test_unknown_kind_and_missing_log_are_safe():
    with pytest.raises(ValueError):
        operation_log.log_path("unknown", 1)
    assert operation_log.read(operation_log.PAYMENT, 9999) == ""
