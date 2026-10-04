"""Submit and query selected extracted accounts, keeping uncertain payments safe."""
from __future__ import annotations

import hashlib
import uuid

from core import db, operation_log, scan_payment_store as store
from core import payment_provider_store as credentials
from core.orderhub_client import OrderHubClient
from core.scan_api_client import (
    ScanApiClient, MasiClient, ScanApiError, _safe_message,
    validate_idempotency_key, validate_payment_link,
)
from core.seashore_client import SeashorePublisherClient

# 需要所选账号完整 AT 的平台（其余平台只发链接和邮箱）。
AT_PROVIDERS = frozenset({"masi", "orderhub", "seashore"})


def _validate_ids(account_ids) -> list[int]:
    if not isinstance(account_ids, list) or not 1 <= len(account_ids) <= 500:
        raise ValueError("account_ids 必须是 1–500 个账号 ID 的数组")
    if any(type(value) is not int or value <= 0 for value in account_ids):
        raise ValueError("account_ids 必须全部是正整数")
    return list(dict.fromkeys(account_ids))


def _credential(cdk):
    if not isinstance(cdk, str) or not cdk.strip() or any(ord(c) < 32 for c in cdk):
        raise ValueError("请提供完整的支付 CDK，不能包含控制字符")
    return cdk.strip()


def _client(provider: str, api_base=None):
    if provider == "v1":
        return ScanApiClient(api_base=api_base)
    if provider == "masi":
        return MasiClient(api_base=api_base)
    if provider == "orderhub":
        return OrderHubClient(api_base=api_base)
    if provider == "seashore":
        return SeashorePublisherClient(api_base=api_base)
    raise ValueError("请选择 Astra Scan Workbench、masi、UPI OrderHub 或 seashore 发布者 API")


def resolve_credential(*, provider, cdk=None, cdk_id=None, auth_session=None):
    """返回本次要用的明文凭据；OrderHub 登录会话用其合成身份。"""
    if auth_session:
        return auth_session["identity"]
    return credentials.resolve_credential(provider=provider, cdk=cdk, cdk_id=cdk_id)["cdk"]


def _auth_client(provider, api_base=None, auth_session=None):
    if auth_session and provider != "orderhub":
        raise ValueError("OrderHub 登录会话只能查询或提交 OrderHub 订单")
    client = _client(provider, api_base or (auth_session["api_base"] if auth_session else None))
    if auth_session:
        client.session_auth = auth_session
    return client


def _account_key(batch_key, account_id):
    # Do not truncate long user keys: differing suffixes must stay distinct.
    return "scan-" + hashlib.sha256(f"{batch_key}:{account_id}".encode()).hexdigest()


def _response(result, *, secrets=()):
    """Only persist normalized metadata, never arbitrary provider payloads."""
    task = result.get("task") or {}
    keys = {"id", "status", "message", "verificationPolicy", "verificationReason", "verifying",
            "expiresAt", "canCancel", "canRedispatch", "redispatchHint", "submission_uncertain",
            "verificationDeadline", "paymentWindowEndsAt", "paymentExpiryKind", "failureCode", "claimPenaltyExempt",
            "charged", "created", "providerStatus", "statusText", "resolvedAt", "createdAt"}
    snapshot = {}
    for key in keys:
        value = task.get(key)
        if isinstance(value, str):
            snapshot[key] = _safe_message(value, secrets)
        elif value is None or type(value) in (int, float, bool):
            snapshot[key] = value
    status = str(task.get("status") or "unknown").lower()
    uncertain = bool(result.get("uncertain")) or result.get("http_status") == 202
    task_id = result.get("task_id") or task.get("id") or ""
    return {
        "task": snapshot, "task_id": str(task_id),
        "status": ("submission_pending" if task_id else "unknown") if uncertain else status,
        "message": "提交结果待确认，请查询原任务" if uncertain else snapshot.get("message") or "",
        "error": "", "code": "", "retryable": None,
        "request_id": result.get("request_id"), "http_status": result.get("http_status"),
        "duplicate": bool(result.get("duplicate")),
        **{key: result[key] for key in ("charged", "created") if type(result.get(key)) is bool},
    }


def _errors(exc, secrets=()):
    if isinstance(exc, ScanApiError):
        return {"error": _safe_message(str(exc), secrets), "code": exc.code,
                "retryable": exc.retryable, "request_id": exc.request_id,
                "http_status": exc.status, "uncertain": exc.uncertain,
                "created": getattr(exc, "created", None), "charged": getattr(exc, "charged", None),
                "retry_after_seconds": getattr(exc, "retry_after_seconds", None)}
    if isinstance(exc, (ValueError, LookupError)):
        return {"error": _safe_message(str(exc), secrets), "uncertain": False}
    return {"error": "支付操作异常，请查询原记录核对受理情况", "uncertain": True}


def _body(account):
    if account.get("extract_link_status") != "success":
        raise ValueError("该账号尚未成功提炼支付链接")
    if str(account.get("extract_link_type") or "").lower() != "upi":
        raise ValueError("当前支付接入只支持已提炼的 UPI 链接")
    from urllib.parse import urlsplit
    def _hosted_instructions_link(value):
        link = str(value or "").strip()
        try:
            parts = urlsplit(link)
        except ValueError:
            return None
        if parts.scheme != "https" or not parts.hostname:
            return None
        return link

    candidates = (account.get("extract_link_hosted_instructions_url"),
                  account.get("extract_link_long_url"), account.get("extract_link_copy_paste"))
    link = None
    for index, candidate in enumerate(candidates):
        if not candidate:
            continue
        if index == 0:
            link = _hosted_instructions_link(candidate)
            if link:
                break
            continue
        try:
            link = validate_payment_link(candidate)
            break
        except ValueError:
            continue
    if not link:
        raise ValueError("该账号没有有效的 Stripe HTTPS UPI 支付页，请重新提链")
    body = {"link": link}
    if account.get("email"):
        body["email"] = str(account["email"]).strip()
    return body


def _submit_one(account_id, cdk, provider, batch_key, auth_session=None, fallback_from=None):
    # 完整日志：每次点击都追加一段，包含请求体、凭据、响应与最终归类。
    with operation_log.operation(
            operation_log.PAYMENT, account_id, truncate=False, header="提交支付", secrets=(cdk,),
            provider=provider, credential_source="orderhub-session" if auth_session else "credentials",
            credential=cdk, idempotency_key=batch_key, fallback_from=fallback_from or "") as log:
        account = db.get_account(account_id)
        if not account:
            log.failure(LookupError("账号不存在"), note="账号不存在，未提交")
            raise LookupError("账号不存在")
        client = _auth_client(provider, auth_session=auth_session)
        log.step("支付平台客户端已就绪", api_base=client.api_base, timeout=client.timeout)
        existing = store.latest(account_id)
        if existing and existing["status"] not in store.TERMINAL:
            # Resume the original snapshot even if extraction or email changed.
            body = existing["body"]
            log.step("复用未完成的支付请求快照", record_id=existing["id"], status=existing["status"],
                     task_id=existing.get("task_id") or "", body=body)
        else:
            body = _body(account)
            if provider == "seashore" and not body.get("email"):
                error = ValueError("发布者 API 需要所选账号的邮箱，请先补全账号邮箱")
                log.failure(error, note="缺少账号邮箱，未提交")
                raise error
            log.step("使用账号当前的零元 UPI 链接", link=body.get("link"), email=body.get("email"),
                     extract_link_provider_id=account.get("extract_link_provider_id"),
                     extract_link_provider_name=account.get("extract_link_provider_name"),
                     extract_link_task_id=account.get("extract_link_task_id") or "",
                     extract_link_job_id=account.get("extract_link_job_id") or "")
        needs_at = provider in AT_PROVIDERS
        access_token = str(account.get("access_token") or "").strip() if needs_at else ""
        if (needs_at and not access_token
                and not (fallback_from is None and existing and existing.get("task_id"))):
            error = ValueError("该支付平台需要所选账号的完整 AT，请先刷新账号 AT")
            log.failure(error, note="缺少账号 AT，未提交")
            raise error
        if access_token:
            log.add_secrets(access_token)
            log.detail("本次提交携带账号 AT", access_token=access_token)
        if provider == "orderhub":
            token_hash = hashlib.sha256(access_token.encode()).hexdigest()
            if existing and existing["status"] not in store.TERMINAL and not existing.get("task_id") and existing["body"].get("at_hash") != token_hash:
                error = ValueError("账号 AT 已变更，不能改变原请求重试；请在原平台查询待确认订单")
                log.failure(error, note="账号 AT 与冻结快照不一致，已阻止重试")
                raise error
            body = {**body, "at_hash": (existing["body"].get("at_hash", token_hash) if existing and existing["status"] not in store.TERMINAL else token_hash)}
            log.detail("OrderHub AT 摘要已确认", at_hash=body["at_hash"])
        record, should_send = store.reserve(
            account_id=account_id, provider=provider, api_base=client.api_base, cdk=cdk, body=body,
            idempotency_key=_account_key(batch_key, account_id), lease_seconds=client.timeout + 60,
            fallback_from=fallback_from,
        )
        log.step("提交前快照已落库", record_id=record["id"], sent=should_send, status=record["status"],
                 upstream_key=record["idempotency_key"], lease_until=record.get("lease_until"))
        if not should_send:
            view = store.public_view(record)
            if record.get("task_id"):
                log.step("已有原任务 ID，跳过重复提交", task_id=record["task_id"], status=view["status"])
                return "duplicated", view
            log.warn("未向上游重复提交", status=view["status"], message=view.get("message"))
            return ("pending" if view["status"] == "submitting" else "unknown"), view

        try:
            kwargs = {"cdk": cdk, **{k: v for k, v in record["body"].items() if k != "at_hash"}, "idempotency_key": record["idempotency_key"]}
            if provider in AT_PROVIDERS:
                kwargs["access_token"] = access_token
            log.step("开始提交支付", platform=provider, cdk=cdk, access_token=access_token or None,
                     link=kwargs.get("link"), email=kwargs.get("email"))
            result = client.submit_upi(**kwargs)
        except Exception as exc:
            error = _errors(exc, (cdk, access_token))
            uncertain = error.pop("uncertain") or record.get("uncertain_history", False)
            log.failure(exc, note="支付提交失败", secrets=())
            record = store.finish(record, status="unknown" if uncertain else "rejected",
                                  message=error["error"], **error)
            view = store.public_view(record)
            log.step("失败结果已记录", status=view["status"], message=view.get("message"),
                     task_id=view.get("task_id") or "")
            return "unknown" if uncertain else "failed", view

        changes = _response(result, secrets=(cdk, access_token))
        log.step("收到支付平台受理结果", http_status=changes.get("http_status"), status=changes["status"],
                 task_id=changes["task_id"] or "", duplicate=changes["duplicate"],
                 request_id=changes.get("request_id") or "", message=changes.get("message"),
                 task=changes.get("task"))
        try:
            record = store.finish(record, **changes)
        except Exception as exc:
            log.failure(exc, note="平台已受理但本地保存失败")
            return "unknown", {"id": account_id, "email": body.get("email", ""), **changes,
                               "error": "已收到平台响应，但本地保存失败，请保留任务 ID 并核对原任务"}
        category = "pending" if record["status"] == "submission_pending" else "unknown" if record["status"] == "unknown" else "duplicated" if changes["duplicate"] else "created"
        log.step("本次提交结束", category=category, status=record["status"],
                 task_id=record.get("task_id") or "", message=record.get("message"))
        return category, store.public_view(record)


def submit_accounts(*, account_ids, cdk=None, cdk_id=None, provider: str = "v1", idempotency_key=None,
                    auth_session=None, fallback_from=None) -> dict:
    ids = _validate_ids(account_ids)
    credential = resolve_credential(provider=provider, cdk=cdk, cdk_id=cdk_id, auth_session=auth_session)
    _auth_client(provider, auth_session=auth_session)
    batch_key = validate_idempotency_key(idempotency_key) if idempotency_key is not None else "scan-" + uuid.uuid4().hex
    output = {name: [] for name in ("created", "duplicated", "pending", "failed", "unknown")}
    for account_id in ids:
        try:
            category, item = _submit_one(account_id, credential, provider, batch_key, auth_session,
                                         fallback_from=fallback_from)
        except Exception as exc:
            error = _errors(exc, (credential,))
            category = "unknown" if error.pop("uncertain") else "failed"
            item = {"id": account_id, **error}
        output[category].append(item)
    output.update(ok=True, idempotency_key=batch_key)
    for name in ("created", "duplicated", "pending", "failed", "unknown"):
        output["duplicate_count" if name == "duplicated" else name + "_count"] = len(output[name])
    _log_batch_summary(ids, output)
    return output


def _log_batch_summary(ids, output):
    """在已有账号日志末尾追加一行批次汇总；没有日志的账号不新建文件。"""
    counts = {name: output.get(name + "_count", 0) for name in ("created", "duplicated", "pending", "failed", "unknown")}
    for account_id in ids:
        path = operation_log.payment_log_path(account_id)
        if not path.exists():
            continue
        log = operation_log.OperationLog(path, kind=operation_log.PAYMENT,
                                         account_id=account_id, truncate=False)
        log.step("批次提交结束", idempotency_key=output.get("idempotency_key"), **counts)
        log.close()


def query_accounts(*, account_ids, cdk=None, cdk_id=None, auth_session=None) -> dict:
    ids = _validate_ids(account_ids)
    if auth_session:
        credential = auth_session["identity"]
    elif cdk_id is not None:
        # 查询沿用的是原任务的凭据：按 id 取出明文后仍会与记录里的摘要核对。
        credential = credentials.resolve_saved(cdk_id)["cdk"]
    else:
        credential = _credential(cdk)
    items, failed, unknown = [], [], []
    for account_id in ids:
        with operation_log.operation(
                operation_log.PAYMENT, account_id, truncate=False, header="查询支付任务",
                secrets=(credential,), credential_source="orderhub-session" if auth_session else "credentials",
                credential=credential) as log:
            try:
                record = store.latest(account_id)
                if not record:
                    raise LookupError("该账号没有已保存的支付任务")
                log.step("读取本地支付记录", record_id=record["id"], provider=record["provider"],
                         status=record["status"], task_id=record.get("task_id") or "",
                         api_base=record["api_base"])
                if store.credential_hash(credential) != record["credential_hash"]:
                    raise ValueError("请使用提交该支付任务时的原 CDK")
                if not record.get("task_id"):
                    view = store.public_view(record)
                    view["message"] = ("没有任务 ID，可使用原请求安全重试" if view["can_retry_same_key"]
                                       else "任务受理尚未确认，请在原平台核对订单和额度")
                    log.warn("本地没有任务 ID，未发起远端查询", status=view["status"], message=view["message"])
                    unknown.append(view)
                    continue
                client = _auth_client(record["provider"], record["api_base"], auth_session)
                log.step("查询原支付任务", provider=record["provider"], task_id=record["task_id"],
                         api_base=record["api_base"])
                result = client.get_task(cdk=credential, task_id=record["task_id"])
                changes = _response(result, secrets=(credential,))
                log.step("收到查询结果", http_status=changes.get("http_status"), status=changes["status"],
                         task_id=changes["task_id"] or "", message=changes.get("message"), task=changes.get("task"))
                record = store.finish(record, **changes)
                items.append(store.public_view(record))
            except Exception as exc:
                error = _errors(exc, (credential,))
                error.pop("uncertain")
                log.failure(exc, note="查询支付任务失败")
                failed.append({"id": account_id, **error})
    return {"ok": True, "items": items, "count": len(items), "failed": failed,
            "failed_count": len(failed), "unknown": unknown, "unknown_count": len(unknown)}


def local_status(account_id):
    record = store.latest(account_id)
    if not record:
        raise LookupError("该账号没有已保存的支付任务")
    return store.public_view(record)


def verify_cdk(*, provider="v1", cdk=None, cdk_id=None, auth_session=None):
    credential = resolve_credential(provider=provider, cdk=cdk, cdk_id=cdk_id, auth_session=auth_session)
    return _auth_client(provider, auth_session=auth_session).verify_cdk(credential)
