"""Server-side Plus activation: eligibility -> UPI checkout -> payment -> plan verification."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from core import db, extract_link_service as extraction, plan_check_service
from core import plus_activation_store as store, scan_api_service as payments
from core import scan_payment_store as payment_store
from core.scan_api_client import _safe_message
from core.upi_git5_extract import entry_proxies

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="plus-activation")
_QUEUE_SLOTS = threading.BoundedSemaphore(500)
_POLL_INTERVAL = 5
_EXTRACT_TIMEOUT = 1800
_PAYMENT_TIMEOUT = 600
_VERIFY_ATTEMPTS = 6
_VERIFY_INTERVAL = 10
_PAID = {"succeeded", "completed"}


class ActivationStop(Exception):
    def __init__(self, message, status="needs_attention"):
        super().__init__(message)
        self.status = status


def _is_plus(account):
    plan = str(account.get("current_plan_type") or account.get("plan_type") or "").lower()
    return "plus" in plan and "free" not in plan


def option_list(value, name):
    """Accept the original single configuration or an ordered fallback list."""
    items = [value] if isinstance(value, dict) else value
    if not isinstance(items, list) or not 1 <= len(items) <= 20 or any(not isinstance(item, dict) for item in items):
        raise ValueError(f"{name} 必须是对象或包含 1–20 个配置对象的数组")
    return items


def _extract_settings(extract):
    provider_id = extract.get("provider_id")
    if type(provider_id) is not int or provider_id < 0:
        raise ValueError("请选择提链服务商")
    if extract.get("link_type", "upi") != "upi" or extract.get("payment_amount", 0) != 0:
        raise ValueError("一键开通仅支持零元 UPI 提链")
    if extract.get("payment_provider_id") is not None:
        raise ValueError("请通过 payment 选择支付平台")
    if extract.get("cdk") is not None and extract.get("cdk_id") is not None:
        raise ValueError("提链 CDK 和 cdk_id 只能提供一个")
    if extract.get("cdk") is not None:
        value = extract["cdk"]
        if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("提链 CDK 必须是非空字符串，不能包含控制字符")
    if extract.get("cdk_id") is not None and (type(extract["cdk_id"]) is not int or extract["cdk_id"] <= 0):
        raise ValueError("提链 cdk_id 必须是正整数")
    provider = extraction._resolve_provider(provider_id)
    code, cdk_id = extraction._resolve_cdk(provider, cdk=extract.get("cdk"), cdk_id=extract.get("cdk_id"))
    options = {"provider_id": provider_id, "link_type": "upi", "payment_amount": 0}
    options.update({"cdk_id": cdk_id} if cdk_id is not None else {"cdk": code})
    if provider["provider_type"] == "upi_git5":
        options["entry_proxies"] = entry_proxies(extract.get("entry_proxies"))
    elif provider["provider_type"] == "lumen" and extract.get("proxy_url"):
        options["proxy_url"] = entry_proxies([extract["proxy_url"]])[0]
    return options, code


def _payment_settings(payment, auth_session):
    mode = payment.get("auth_mode", "key")
    if mode not in ("key", "session"):
        raise ValueError("支付验证方式无效")
    if mode == "session" and not auth_session:
        raise ValueError("OrderHub 登录会话已失效，请重新登录")
    payment_provider = payment.get("provider", "v1")
    if mode == "session" and payment.get("cdk_id") is not None:
        raise ValueError("登录会话方式不需要 cdk_id")
    # 已保存的支付 CDK 与临时 cdk 二选一，服务端只在此处解析成明文。
    credential = auth_session["identity"] if auth_session else payments.resolve_credential(
        provider=payment_provider, cdk=payment.get("cdk"), cdk_id=payment.get("cdk_id"))
    payments._auth_client(payment_provider, auth_session=auth_session)
    return {"provider": payment_provider, "cdk": credential, "auth_session": auth_session}


def _settings(extract, payment, auth_session):
    extracts = option_list(extract, "extraction")
    payment_items = option_list(payment, "payment")
    sessions = auth_session if isinstance(auth_session, list) else [auth_session] * len(payment_items)
    if len(sessions) != len(payment_items):
        raise ValueError("支付会话与候选配置数量不一致")
    extraction_candidates, payment_candidates = [], []
    seen_extract, seen_payment = set(), set()
    for item in extracts:
        options, code = _extract_settings(item)
        key = _checkout_key(options, code)
        if key not in seen_extract:
            seen_extract.add(key)
            extraction_candidates.append((options, code))
    for item, session in zip(payment_items, sessions):
        candidate = _payment_settings(item, session)
        key = (candidate["provider"], payment_store.credential_hash(candidate["cdk"]))
        if key not in seen_payment:
            seen_payment.add(key)
            payment_candidates.append(candidate)
    return extraction_candidates, payment_candidates


def enqueue_accounts(*, account_ids, extraction_options, payment_options, auth_session=None, success_group=None):
    ids = payments._validate_ids(account_ids)
    success_group = store.validate_success_group(success_group)
    extraction_candidates, payment_candidates = _settings(extraction_options, payment_options, auth_session)
    result = {key: [] for key in ("started", "busy", "skipped", "failed")}
    for account_id in ids:
        account = db.get_account(account_id)
        if not account or account.get("archived"):
            result["skipped"].append({"id": account_id, "reason": "账号不存在或已归档"})
            continue
        if _is_plus(account) and account.get("plus_activation_status") in {None, "", "succeeded"}:
            result["skipped"].append({"id": account_id, "reason": "账号已是 Plus"})
            continue
        if not str(account.get("access_token") or "").strip():
            result["skipped"].append({"id": account_id, "reason": "缺少 access_token，请先刷新 AT"})
            continue
        if not _QUEUE_SLOTS.acquire(blocking=False):
            result["failed"].append({"id": account_id, "error": "Plus 开通队列已满"})
            continue
        claimed = False
        try:
            account, claimed = store.claim(account_id, success_group=success_group)
            if not claimed:
                result["busy"].append(store.public_view(account))
                _QUEUE_SLOTS.release()
                continue
            run_id = account["plus_activation_run_id"]
            _EXECUTOR.submit(_run, account_id, run_id, extraction_candidates, payment_candidates)
            result["started"].append(store.public_view(account))
        except Exception:
            _QUEUE_SLOTS.release()
            if claimed:
                store.update(account_id, account["plus_activation_run_id"], "failed", "本地开通队列提交失败，请重试")
            result["failed"].append({"id": account_id, "error": "本地开通队列提交失败，请重试"})
    return {"ok": True, **result, **{key + "_count": len(items) for key, items in result.items()}}


def _account(account_id, run_id):
    account = db.get_account(account_id)
    if not account or account.get("plus_activation_run_id") != run_id:
        raise ActivationStop("账号已删除或开通任务已变更")
    if account.get("archived"):
        raise ActivationStop("账号已归档，开通已暂停")
    return account


def _progress(account_id, run_id, status, message, step=None, checkout_key=None, failed_checkout_key=None):
    if not store.update(account_id, run_id, status, message, step=step, checkout_key=checkout_key,
                        failed_checkout_key=failed_checkout_key):
        raise ActivationStop("账号已删除或开通任务已变更")


def _check_plan(account_id, run_id):
    account = _account(account_id, run_id)
    token = str(account.get("access_token") or "").strip()
    if not token:
        raise ActivationStop("账号缺少 AT，请先刷新 AT")
    plan_check_service._wait_for_rate_slot()
    result = plan_check_service.check_account_plan(token)
    db.update_account_plan_check(acc_id=account_id, result=result)
    return result


def _complete(account_id, run_id, message):
    try:
        if not store.complete(account_id, run_id, message):
            raise ActivationStop("账号已删除或开通任务已变更")
    except LookupError as exc:
        _progress(account_id, run_id, "needs_attention", str(exc), "verifying")


def _verify(account_id, run_id):
    _progress(account_id, run_id, "verifying", "支付已完成，正在核验 Plus 套餐", "verifying")
    for attempt in range(_VERIFY_ATTEMPTS):
        result = _check_plan(account_id, run_id)
        if result.get("ok") and _is_plus(result):
            _complete(account_id, run_id, "Plus 已开通，套餐核验成功")
            return
        if attempt + 1 < _VERIFY_ATTEMPTS:
            time.sleep(_VERIFY_INTERVAL)
    raise ActivationStop("支付已完成，暂未查到 Plus 生效；稍后点击开通 Plus 继续核验，不会再次支付")


def _expired(account):
    value = account.get("extract_link_expires_at")
    if not value:
        return False
    try:
        if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
            stamp = float(value)
            if stamp > 100_000_000_000:
                stamp /= 1000
            return stamp <= time.time()
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp() <= time.time()
    except (ValueError, OverflowError):
        return False


def _refresh_extract(account, code, options):
    if account.get("extract_link_provider_type") not in {"lumen", "upi_git5"}:
        raise ActivationStop("原提链结果待核实，请先在提链平台核对原任务")
    if account.get("extract_link_provider_id") != options["provider_id"]:
        raise ActivationStop("请使用原提链服务商和原 CDK 恢复任务")
    extraction.task_action(account["id"], "refresh", cdk=code)


def _checkout_key(options, code):
    identity = {"provider_id": options["provider_id"], "cdk": code, "link_type": "upi", "payment_amount": 0,
                "proxy_url": options.get("proxy_url"), "entry_proxies": options.get("entry_proxies")}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _extract(account_id, run_id, options, code, label="", *, retry_failed=True):
    account = _account(account_id, run_id)
    status = account.get("extract_link_status")
    previous_step = account.get("plus_activation_step")
    checkout_key = _checkout_key(options, code)
    trusted = (account.get("extract_link_trigger") == "plus_activation"
               and account.get("plus_activation_checkout_key") == checkout_key)
    refresh_remote = status in {"unknown", "interrupted"} or (
        status in {"queued", "running"} and account.get("extract_link_provider_type") in {"lumen", "upi_git5"}
        and bool(account.get("extract_link_task_id") or account.get("extract_link_job_id")))
    if status in {"queued", "running", "awaiting_blik", "unknown", "interrupted"} and not trusted:
        raise ActivationStop("原提链仍在执行或待核实，请使用原开通配置恢复；其他提链任务请先核对结果")
    if refresh_remote:
        _refresh_extract(account, code, options)
        account = _account(account_id, run_id)
        status = account.get("extract_link_status")
    # A local read error can look like a failed checkout. Only a fresh upstream
    # terminal response permits another charged extraction after a failed run.
    confirmed_failure = False
    if previous_step == "extracting" and status in {"failed", "stopped"}:
        if refresh_remote:
            confirmed_failure, refresh_remote = True, False
        elif account.get("extract_link_provider_type") in {"lumen", "upi_git5"} and (account.get("extract_link_task_id") or account.get("extract_link_job_id")):
            _refresh_extract(account, code, options)
            account = _account(account_id, run_id)
            status = account.get("extract_link_status")
            confirmed_failure = status in {"failed", "stopped"}
            refresh_remote = not confirmed_failure
    if confirmed_failure and not retry_failed:
        raise ActivationStop(account.get("extract_link_error") or "原提链任务已确认失败", "failed")
    if status == "success" and _upstream_payment(account_id, run_id, account, options, code):
        return _account(account_id, run_id)
    needs_new = status == "success" and not trusted
    if status == "success" and trusted:
        try:
            payments._body(account)
            needs_new = _expired(account)
        except ValueError:
            needs_new = True
    _progress(account_id, run_id, "extracting", label + "正在提取 UPI 支付链接", "extracting")
    if needs_new or status not in {"success", "queued", "running", "awaiting_blik", "unknown", "interrupted"}:
        if previous_step == "extracting" and not needs_new and not confirmed_failure:
            raise ActivationStop("上次提链受理结果未确认，请先核对原提链任务后再继续")
        _progress(account_id, run_id, "extracting", label + "正在创建零元 UPI 提链任务", "extracting", checkout_key)
        queued = extraction.enqueue_account_extract(
            account_id=account_id, email=account.get("email") or "", access_token=account["access_token"],
            trigger="plus_activation", **options,
        )
        if not queued.get("accepted"):
            if queued.get("busy"):
                raise ActivationStop("账号已有其他提链任务，请等待原任务完成后再开通")
            _progress(account_id, run_id, "extracting", label + "提链未入队", "checking")
            raise ActivationStop(queued.get("error") or "提链未入队", "failed")
        refresh_remote = False
    deadline = time.monotonic() + _EXTRACT_TIMEOUT
    while True:
        account = _account(account_id, run_id)
        status = account.get("extract_link_status")
        if status == "success":
            if _upstream_payment(account_id, run_id, account, options, code):
                return _account(account_id, run_id)
            if _expired(account):
                raise ActivationStop("原支付链接已过期，请重新提链后再开通 Plus")
            payments._body(account)
            return account
        if status in {"failed", "stopped"}:
            raise ActivationStop(account.get("extract_link_error") or "提链失败，未提交支付", "failed")
        if status in {"unknown", "interrupted", "awaiting_blik"}:
            raise ActivationStop("提链结果待核实，请先刷新原提链任务；未提交新的支付")
        if time.monotonic() >= deadline:
            raise ActivationStop("等待提链超时，请刷新原提链任务后继续")
        time.sleep(_POLL_INTERVAL)
        if refresh_remote:
            _refresh_extract(account, code, options)


def _wait_payment(account_id, run_id, payment, record, label=""):
    credential = payment["cdk"]
    if record["provider"] != payment["provider"] or record["credential_hash"] != payment_store.credential_hash(credential):
        raise ActivationStop("该账号已有支付记录，请选择原支付平台和原凭据继续查询")
    _progress(account_id, run_id, "paying", label + "正在等待支付完成", "paying")
    deadline = time.monotonic() + _PAYMENT_TIMEOUT
    while True:
        _account(account_id, run_id)
        status = record["status"]
        if status in _PAID:
            return
        if status in payment_store.TERMINAL:
            raise ActivationStop(record.get("error") or record.get("message") or f"支付终止：{status}", "failed")
        if not record.get("task_id"):
            raise ActivationStop("原支付提交结果待确认，请在“查询支付”中核对原任务；不会自动重提")
        if time.monotonic() >= deadline:
            raise ActivationStop("支付仍在处理中，请点击开通 Plus 或查询支付继续查询原任务")
        result = payments.query_accounts(account_ids=[account_id], cdk=credential, auth_session=payment["auth_session"])
        if result["failed"]:
            raise ActivationStop(result["failed"][0].get("error") or "支付查询失败，请核对原任务")
        record = payment_store.latest(account_id)
        if record["status"] not in payment_store.TERMINAL:
            time.sleep(_POLL_INTERVAL)


def _upstream_payment(account_id, run_id, account, options, code):
    """Do not cross-submit a checkout already paid by the extraction provider."""
    status = str(account.get("extract_link_payment_status") or "").lower()
    if status in {"", "not_submitted", "unpaid", "not_paid"}:
        return False
    deadline = time.monotonic() + _PAYMENT_TIMEOUT
    while status not in _PAID | {"paid", "success"}:
        if status in {"failed", "cancelled", "canceled", "expired", "refunded", "unknown",
                      "submit_failed", "tracking_failed", "quota_insufficient"}:
            raise ActivationStop("提链平台已有支付记录，请先核对该平台原订单")
        if time.monotonic() >= deadline:
            raise ActivationStop("提链平台支付尚未结束，请刷新原任务继续核对")
        _progress(account_id, run_id, "paying", "提链平台已受理支付，正在查询原任务", "paying")
        _refresh_extract(account, code, options)
        account = _account(account_id, run_id)
        status = str(account.get("extract_link_payment_status") or "").lower()
        if status in {"", "not_submitted", "unpaid", "not_paid"}:
            raise ActivationStop("提链平台支付状态变化，请核对原订单")
        if status not in _PAID | {"paid", "success"}:
            time.sleep(_POLL_INTERVAL)
    return True


def _extract_start(account, candidates):
    key = account.get("plus_activation_checkout_key")
    return next((i for i, (options, code) in enumerate(candidates) if _checkout_key(options, code) == key), 0)


def _extract_candidates(account_id, run_id, candidates):
    account = _account(account_id, run_id)
    start = _extract_start(account, candidates)
    if (len(candidates) > 1 and account.get("extract_link_status") in {"failed", "stopped", None}
            and account.get("plus_activation_checkout_key") == account.get("plus_activation_failed_checkout_key")
            and account.get("plus_activation_failed_checkout_key") == _checkout_key(*candidates[start])):
        start += 1
        if start >= len(candidates):
            raise ActivationStop("提链候选已用尽，请核对失败原因后调整候选配置", "failed")
        _progress(account_id, run_id, "extracting", f"继续备用提链方案 {start + 1}/{len(candidates)}",
                  "checking", _checkout_key(*candidates[start]))
    for index in range(start, len(candidates)):
        options, code = candidates[index]
        label = f"提链方案 {index + 1}/{len(candidates)}：" if len(candidates) > 1 else ""
        try:
            return _extract(account_id, run_id, options, code, label, retry_failed=len(candidates) == 1), options, code
        except ActivationStop as exc:
            if exc.status != "failed" or len(candidates) == 1:
                raise
            account = _account(account_id, run_id)
            if (account.get("extract_link_status") in {"failed", "stopped"}
                    and account.get("extract_link_provider_type") in {"lumen", "upi_git5"}
                    and (account.get("extract_link_task_id") or account.get("extract_link_job_id"))):
                # A failed read is not evidence that the charged task failed.
                _refresh_extract(account, code, options)
                account = _account(account_id, run_id)
                if account.get("extract_link_status") == "success":
                    return _extract(account_id, run_id, options, code, label, retry_failed=len(candidates) == 1), options, code
                if account.get("extract_link_status") not in {"failed", "stopped"}:
                    raise ActivationStop("原提链任务尚未确认失败，请继续核对原任务") from exc
            _progress(account_id, run_id, "extracting", label + "已确认失败", "checking",
                      failed_checkout_key=_checkout_key(options, code))
            if index + 1 == len(candidates):
                raise ActivationStop(f"提链候选已用尽（{len(candidates)} 项）：{exc}", "failed") from exc
            _progress(account_id, run_id, "extracting",
                      f"提链方案 {index + 1} 已失败，自动回退到方案 {index + 2}/{len(candidates)}",
                      "checking", _checkout_key(*candidates[index + 1]))


def _payment_matches(record, payment):
    return (record["provider"] == payment["provider"]
            and record["credential_hash"] == payment_store.credential_hash(payment["cdk"]))


def _pay_candidates(account_id, run_id, candidates, record=None):
    start, fallback_from = 0, None
    if record:
        match = next((i for i, payment in enumerate(candidates) if _payment_matches(record, payment)), None)
        if record["status"] not in payment_store.TERMINAL:
            if match is None:
                raise ActivationStop("该账号已有支付记录，请在候选中包含原支付平台和原凭据继续查询")
            start = match
        elif len(candidates) > 1:
            if not payment_store.can_fallback(record):
                raise ActivationStop("原支付订单不能确认未支付，请先核对原订单")
            start = match + 1 if match is not None else 0
            fallback_from, record = record["id"], None
        else:
            record = None  # Preserve the existing retry behavior for rejected submissions.
    if start >= len(candidates):
        raise ActivationStop("支付候选已用尽，请核对失败原因后调整候选配置", "failed")
    for index in range(start, len(candidates)):
        payment = candidates[index]
        label = f"支付方案 {index + 1}/{len(candidates)}：" if len(candidates) > 1 else ""
        try:
            if record is None:
                _progress(account_id, run_id, "paying", label + "提链成功，正在提交支付", "paying")
                _account(account_id, run_id)
                kwargs = {"fallback_from": fallback_from} if fallback_from else {}
                response = payments.submit_accounts(
                    account_ids=[account_id], idempotency_key=f"plus-{run_id}-{index}", **payment, **kwargs)
                if response.get("unknown"):
                    raise ActivationStop("支付提交结果待核实，请查询原记录；不会切换支付方案")
                if response["failed"]:
                    raise ActivationStop(response["failed"][0].get("error") or "支付提交失败", "failed")
                record = payment_store.latest(account_id)
                if not record:
                    raise ActivationStop("支付提交结果待核实，请查询原记录")
            _wait_payment(account_id, run_id, payment, record, label)
            return
        except ActivationStop as exc:
            if exc.status != "failed" or index + 1 == len(candidates):
                if exc.status == "failed" and len(candidates) > 1:
                    raise ActivationStop(f"支付候选已用尽（{len(candidates)} 项）：{exc}", "failed") from exc
                raise
            latest = payment_store.latest(account_id)
            if latest and not payment_store.can_fallback(latest):
                raise ActivationStop("支付结果仍需核对，已暂停回退；请查询原订单") from exc
            fallback_from = latest["id"] if latest else None
            record = None
            _progress(account_id, run_id, "paying",
                      f"支付方案 {index + 1} 已失败，自动回退到方案 {index + 2}/{len(candidates)}", "paying")


def _run(account_id, run_id, extraction_candidates, payment_candidates):
    secrets = [payment["cdk"] for payment in payment_candidates]
    for options, code in extraction_candidates:
        secrets.extend([code, options.get("proxy_url"), *(options.get("entry_proxies") or [])])
    try:
        account = _account(account_id, run_id)
        secrets.append(account.get("access_token"))
        options, code = extraction_candidates[_extract_start(account, extraction_candidates)]
        record = payment_store.latest(account_id)
        if account.get("plus_activation_step") == "verifying" or (record and record["status"] in _PAID):
            _verify(account_id, run_id)
            return
        if record and record["status"] not in payment_store.TERMINAL:
            _pay_candidates(account_id, run_id, payment_candidates, record)
            _verify(account_id, run_id)
            return
        same_link = record and record["body"].get("link") in {
            account.get("extract_link_hosted_instructions_url"),
            account.get("extract_link_long_url"), account.get("extract_link_copy_paste")}
        fallback = same_link and len(payment_candidates) > 1 and payment_store.can_fallback(record)
        if (same_link and not fallback and (record["status"] != "rejected" or record.get("task_id"))):
            raise ActivationStop("原支付订单已终止，请核对原订单并重新提链后再开通", "failed")
        if account.get("extract_link_status") == "success" and _upstream_payment(account_id, run_id, account, options, code):
            _verify(account_id, run_id)
            return
        _progress(account_id, run_id, "checking", "正在检查套餐和 Plus 试用资格")
        result = _check_plan(account_id, run_id)
        if not result.get("ok"):
            raise ActivationStop("套餐查询失败，请先刷新 AT 或查询套餐后重试", "failed")
        if _is_plus(result):
            _complete(account_id, run_id, "账号已是 Plus，套餐核验成功")
            return
        if str(result.get("current_plan_type") or result.get("plan_type") or "").lower() != "free" or not result.get("plus_trial_eligible"):
            raise ActivationStop("账号不符合 free（可 Plus 试用）资格，未提交提链或支付", "failed")
        account, options, code = _extract_candidates(account_id, run_id, extraction_candidates)
        if not _upstream_payment(account_id, run_id, account, options, code):
            _pay_candidates(account_id, run_id, payment_candidates, record if fallback else None)
        _verify(account_id, run_id)
    except ActivationStop as exc:
        store.update(account_id, run_id, exc.status, _safe_message(str(exc), secrets))
    except Exception:
        store.update(account_id, run_id, "needs_attention", "开通流程中断，请核对原提链或支付任务后继续")
    finally:
        _QUEUE_SLOTS.release()
