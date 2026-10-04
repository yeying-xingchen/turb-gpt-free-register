"""UPI-GIT5 batch helpers shared by the extract scheduler and task actions."""
from __future__ import annotations

from urllib.parse import urlsplit

from core.upi_git5_client import UpiGit5Error, _safe_message

TERMINAL = {"success", "failed", "stopped"}


def entry_proxies(values):
    """Require explicit upstream-reachable proxies, never use the local proxy pool."""
    if not isinstance(values, list) or not 1 <= len(values) <= 5000:
        raise ValueError("UPI-GIT5 需要 1–5000 个入口代理，请填写服务端可访问的代理 URL")
    result = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("入口代理必须是非空 URL")
        value = value.strip()
        try:
            parts = urlsplit(value)
            port = parts.port
            if (parts.scheme not in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
                    or not parts.hostname or (port is not None and port <= 0)
                    or any(c.isspace() or ord(c) < 32 for c in value)):
                raise ValueError
        except ValueError:
            raise ValueError("入口代理 URL 无效，请使用 HTTP(S) 或 SOCKS URL") from None
        result.append(value)
    return result


def identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise UpiGit5Error("UPI-GIT5 返回无效任务 ID", uncertain=True)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise UpiGit5Error("UPI-GIT5 返回无效任务 ID", uncertain=True)
    return value


def bind_jobs(entries, jobs):
    """Bind once at creation; never use response array order when polling.

    The spec does not define the base of `index`. Accept complete zero/one based
    index sets, or an unambiguous email mapping. Partial/ambiguous mappings fail
    closed so no account receives another account's link.
    """
    if not isinstance(jobs, list) or len(jobs) != len(entries) or any(not isinstance(j, dict) for j in jobs):
        raise UpiGit5Error("批次已受理，但任务列表不完整；请核对原批次", uncertain=True)
    indices = [j.get("index", j.get("batch_index")) for j in jobs]
    base = None
    if all(type(i) is int for i in indices):
        if set(indices) == set(range(len(entries))):
            base = 0
        elif set(indices) == set(range(1, len(entries) + 1)):
            base = 1
    by_email = {}
    for entry in entries:
        email = str(entry.get("email") or "").casefold()
        by_email.setdefault(email, []).append(entry)
    result = {}
    used = set()
    for job, index in zip(jobs, indices):
        job_id = identifier(job.get("job_id", job.get("id")))
        email = str(job.get("account_email") or "").casefold()
        entry = entries[index - base] if base is not None else None
        matches = by_email.get(email, []) if email else []
        if entry is None:
            if len(matches) != 1:
                raise UpiGit5Error("无法确定批次任务对应的账号，请核对原批次", uncertain=True)
            entry = matches[0]
        elif len(matches) == 1 and matches[0]["account_id"] != entry["account_id"]:
            raise UpiGit5Error("批次任务的索引与邮箱不一致，请核对原批次", uncertain=True)
        if job_id in result or entry["account_id"] in used:
            raise UpiGit5Error("批次返回重复任务，请核对原批次", uncertain=True)
        result[job_id] = entry
        used.add(entry["account_id"])
    return result


def state(job, *, batch_id, secrets=()):
    """Persist allowlisted artifacts only. Extraction and payment are separate."""
    if not isinstance(job, dict):
        raise UpiGit5Error("任务响应格式无效", uncertain=True)
    upstream = job.get("status")
    status = {"queued": "queued", "running": "running", "done": "success",
              "error": "failed", "cancelled": "stopped"}.get(upstream, "unknown")
    payment = job.get("payment") if isinstance(job.get("payment"), dict) else {}
    result = {"ok": status == "success", "status": status, "link_type": "upi",
              "job_id": identifier(job.get("id")), "task_id": batch_id}
    text = job.get("text") or job.get("error")
    if isinstance(text, str) and text:
        result["message"] = _safe_message(text, secrets)
    if status in {"failed", "unknown"}:
        result["error"] = _safe_message(job.get("error") or "任务状态未确认，请刷新原任务", secrets)
    if isinstance(payment.get("status"), str):
        result["payment_status"] = _safe_message(payment["status"], secrets)
    if type(job.get("percent")) in (int, float):
        result["progress"] = max(0, min(100, int(job["percent"])))
    payload = job.get("result")
    if isinstance(payload, dict):
        # Job.result is intentionally free-form in the OpenAPI contract.
        aliases = {
            "long_url": ("long_url", "url", "checkout_url", "payment_url", "longUrl", "checkoutUrl", "paymentUrl", "hostedUrl"),
            "copy_paste": ("copy_paste", "copyPaste", "upi_url", "upi_uri"),
            "image_url_png": ("image_url_png", "imageUrlPng", "qr_url", "qrUrl"),
            "image_url_svg": ("image_url_svg", "imageUrlSvg"),
            "expires_at": ("expires_at", "expiresAt"),
        }
        artifact = {}
        for target, keys in aliases.items():
            value = next((payload[k] for k in keys if isinstance(payload.get(k), (str, int, float)) and payload[k] != ""), None)
            if value is None:
                continue
            value = str(value)
            if any(isinstance(s, str) and s and s in value for s in secrets):
                continue
            if target in {"long_url", "image_url_png", "image_url_svg"}:
                try:
                    parsed = urlsplit(value)
                    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                            or parsed.password or any(c.isspace() for c in value)):
                        continue
                except ValueError:
                    continue
            artifact[target] = value
        if artifact:
            result["result"] = artifact
    return result


def redact(value, secrets=()):
    """CDK status responses can contain echoed codes, including nested messages."""
    if isinstance(value, dict):
        return {key: ("[redacted]" if str(key).lower() in {
            "code", "cdk", "token", "access_token", "session_token", "password", "entry_proxies"
        } else redact(item, secrets)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    return _safe_message(value, secrets) if isinstance(value, str) else value
