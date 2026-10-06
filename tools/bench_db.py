# -*- coding: utf-8 -*-
"""SQLite 查询性能基准：合成数据 + 热点函数计时。

用法::

    # 生成并测量（一个进程内完成，仅用于快速迭代）
    python tools/bench_db.py --preset small

    # 生成数据到一个目录，稍后复用（推荐：先基线，再改代码，再复测）
    python tools/bench_db.py --preset large --data-dir /tmp/turb-bench --generate-only
    python tools/bench_db.py --data-dir /tmp/turb-bench --bench-only --json baseline.json

基准数据完全由合成内容构成，不会触碰仓库根目录的 turb.sqlite3。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

PRESETS = {
    "tiny": dict(accounts=200, pool=500, jobs=500, codex=200, events=500),
    "small": dict(accounts=2_000, pool=5_000, jobs=5_000, codex=2_000, events=2_000),
    "medium": dict(accounts=10_000, pool=20_000, jobs=20_000, codex=5_000, events=5_000),
    "large": dict(accounts=50_000, pool=100_000, jobs=100_000, codex=20_000, events=20_000),
}

GROUPS = ["默认分组", "batch-a", "batch-b", "batch-c"]
PLANS = ["free", "plus", "chatgptplusplan", "team", ""]
CODEX_STATUS = ["", "success", "failed", "pending", "deactivated"]
LIVE_STATUS = ["", "success", "failed", "deactivated", "unknown"]
PLAN_CHECK_STATUS = ["", "queued", "running", "success", "failed"]
POOL_SOURCES = ["outlook", "generic_api", "imap", "cloudflare_domain"]


def _jwt(exp: float, seed: str) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(exp), "sub": seed, "aud": ["https://api.openai.com/v1"]}).encode()
    ).decode().rstrip("=")
    signature = hashlib.sha1(seed.encode()).hexdigest()
    return f"{header}.{payload}.{signature}"


def _account_payload(rng: random.Random, acc_id: int, now: datetime) -> dict:
    domain = rng.choice(["example.com", "mail.test", "outlook.com", "gmail.com"])
    email = f"user{acc_id:07d}@{domain}"
    created = now - timedelta(minutes=rng.randint(0, 60 * 24 * 90))
    plan = rng.choice(PLANS)
    trial = rng.random() < 0.25
    campaigns = {}
    if plan == "free" and trial:
        campaigns = {
            "chatgptplusplan": {
                "metadata": {"plan_name": "chatgptplusplan", "discount": {"percentage": rng.choice([0, 10, 25, 50])}},
                "campaign_id": f"camp-{acc_id}",
            }
        }
    # 一半账号的 AT 有效，一半已过期；少量账号只有 token_expired 标记。
    valid_at = rng.random() < 0.5
    exp = (now + timedelta(days=7)).timestamp() if valid_at else (now - timedelta(days=3)).timestamp()
    payload = {
        "id": acc_id,
        "email": email,
        "password": f"Pw-{acc_id}-{rng.randrange(10**6):06d}",
        "access_token": _jwt(exp, email),
        "session_token": base64.urlsafe_b64encode(os.urandom(48)).decode(),
        "refresh_token": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "totp_secret": ("JBSWY3DPEHPK3PXP" if rng.random() < 0.4 else ""),
        "created_at": created.isoformat(timespec="seconds"),
        "updated_at": (created + timedelta(minutes=rng.randint(0, 5000))).isoformat(timespec="seconds"),
        "status": "registered",
        "archived": rng.random() < 0.12,
        "group_name": rng.choice(GROUPS),
        "email_source": rng.choice(POOL_SOURCES),
        "current_plan_type": plan,
        "plan_type": plan,
        "plus_trial_eligible": trial,
        "eligible_promo_campaigns": campaigns,
        "plan_check_status": rng.choice(PLAN_CHECK_STATUS),
        "plan_check_error": "",
        "plan_checked_at": created.isoformat(timespec="seconds"),
        "codex_status": rng.choice(CODEX_STATUS),
        "live_check_status": rng.choice(LIVE_STATUS),
        "live_checked_at": created.isoformat(timespec="seconds"),
        "totp_setup_status": rng.choice(["", "success", "failed", "running"]),
        "extract_link_status": rng.choice(["", "success", "failed", "running"]),
        "scan_request_status": rng.choice(["", "success", "failed"]),
        "note": ("备注 " + "x" * rng.randint(0, 40)) if rng.random() < 0.5 else "",
        "codex_agent_status": rng.choice(["", "success", "failed"]),
        "email_change_status": rng.choice(["", "success", "failed"]),
        "billing_period": "monthly",
        "renews_at": (now + timedelta(days=rng.randint(1, 30))).isoformat(timespec="seconds"),
        "plan_expires_at": (now + timedelta(days=rng.randint(1, 30))).isoformat(timespec="seconds"),
        "raw_profile": {"locale": "en-US", "blob": "y" * 600},
        "registration_meta": {"fingerprint": "z" * 300, "route": "proxy", "attempts": rng.randint(1, 4)},
    }
    return payload


def _pool_payload(rng: random.Random, row_id: int, source: str, now: datetime) -> dict:
    email = f"pool{row_id:07d}@{rng.choice(['example.com', 'mail.test'])}"
    created = now - timedelta(minutes=rng.randint(0, 60 * 24 * 60))
    payload = {
        "id": row_id,
        "email": email,
        "password": f"Pw-{row_id}",
        "source": source,
        "status": rng.choice(["available", "available", "available", "used", "failed"]),
        "created_at": created.isoformat(timespec="seconds"),
        "updated_at": created.isoformat(timespec="seconds"),
        "client_id": "client-" + str(row_id) if source == "generic_api" else "",
        "refresh_token": ("rt-" + "q" * 60) if source == "generic_api" else "",
        "code_url": f"https://mail.example.test/code?email={email}" if source == "generic_api" else "",
        "blob": "m" * 400,
    }
    return payload


def _job_payload(rng: random.Random, job_id: int, now: datetime) -> dict:
    created = now - timedelta(minutes=rng.randint(0, 60 * 24 * 60))
    status = rng.choice(["queued", "running", "success", "success", "failed"])
    root = job_id if rng.random() < 0.8 else max(1, job_id - rng.randint(1, 50))
    return {
        "id": job_id,
        "email": f"job{job_id:07d}@example.com",
        "status": status,
        "archived": rng.random() < 0.1,
        "created_at": created.isoformat(timespec="seconds"),
        "updated_at": (created + timedelta(minutes=rng.randint(0, 600))).isoformat(timespec="seconds"),
        "root_job_id": root,
        "retry_attempt": rng.randint(0, 3),
        "email_source": rng.choice(POOL_SOURCES),
        "error": ("注册失败 " + "e" * 80) if status == "failed" else "",
        "log": "l" * 800,
        "steps": [{"name": f"step{i}", "ok": True} for i in range(rng.randint(1, 8))],
    }


def _codex_payload(rng: random.Random, row_id: int, now: datetime) -> dict:
    created = now - timedelta(minutes=rng.randint(0, 60 * 24 * 60))
    return {
        "id": row_id,
        "email": f"codex{row_id:07d}@example.com",
        "created_at": created.isoformat(timespec="seconds"),
        "updated_at": created.isoformat(timespec="seconds"),
        "access_token": _jwt((now + timedelta(days=5)).timestamp(), f"codex{row_id}"),
        "refresh_token": "rt-" + "r" * 40,
        "account_id": f"acct-{row_id}",
        "exported": rng.random() < 0.5,
        "blob": "c" * 500,
    }


def generate(data_dir: Path, counts: dict, *, seed: int = 20251006) -> dict:
    data_dir.mkdir(parents=True, exist_ok=True)
    for stale in ("turb.sqlite3", "turb.sqlite3-wal", "turb.sqlite3-shm"):
        target = data_dir / stale
        if target.exists():
            target.unlink()
    os.environ["TURB_DATA_DIR"] = str(data_dir)
    os.environ["TURB_ENV_FILE"] = str(data_dir / ".env")

    from contextlib import closing

    from core import db

    db._ensure_sqlite()
    rng = random.Random(seed)
    now = datetime.now().replace(microsecond=0)
    n_accounts = counts["accounts"]
    n_pool = counts["pool"]
    n_jobs = counts["jobs"]
    n_codex = counts["codex"]
    n_events = counts.get("events", 1000)

    with closing(db._sqlite_conn()) as conn:
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("BEGIN")
        account_rows = []
        for acc_id in range(1, n_accounts + 1):
            payload = _account_payload(rng, acc_id, now)
            account_rows.append((
                acc_id, payload["email"], payload["status"], int(payload["archived"]),
                payload["created_at"], payload["updated_at"], json.dumps(payload, ensure_ascii=False),
            ))
        conn.executemany(
            "INSERT INTO accounts(id,email,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?)",
            account_rows,
        )

        pool_rows = []
        for row_id in range(1, n_pool + 1):
            source = POOL_SOURCES[(row_id - 1) % len(POOL_SOURCES)]
            payload = _pool_payload(rng, row_id, source, now)
            pool_rows.append((
                row_id, payload["email"], source, payload["status"], int(payload["archived"]) if "archived" in payload else 0,
                payload["created_at"], payload["updated_at"], json.dumps(payload, ensure_ascii=False),
            ))
        conn.executemany(
            "INSERT INTO email_pool(id,email,source,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?,?)",
            pool_rows,
        )

        job_rows = []
        for job_id in range(1, n_jobs + 1):
            payload = _job_payload(rng, job_id, now)
            job_rows.append((
                job_id, payload["email"], payload["status"], int(payload["archived"]),
                payload["created_at"], payload["updated_at"], json.dumps(payload, ensure_ascii=False),
            ))
        conn.executemany(
            "INSERT INTO registration_jobs(id,email,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?)",
            job_rows,
        )

        codex_rows = []
        for row_id in range(1, n_codex + 1):
            payload = _codex_payload(rng, row_id, now)
            codex_rows.append((
                row_id, f"codex-{row_id}.json", payload["email"], 0,
                payload["created_at"], payload["updated_at"], json.dumps(payload, ensure_ascii=False),
            ))
        conn.executemany(
            "INSERT INTO codex_accounts(id,filename,email,archived,created_at,updated_at,payload) "
            "VALUES(?,?,?,?,?,?,?)",
            codex_rows,
        )

        # 兑换码 / 领取记录：约 20% 账号已被领取。
        code_rows = []
        for code_id in range(1, 201):
            code_rows.append((
                code_id, f"TURB-{code_id:05d}", 50, 0, "active", None, "", rng.choice(GROUPS),
                now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
            ))
        conn.executemany(
            "INSERT INTO redeem_codes(id,code,quantity,redeemed_count,status,expires_at,note,account_group,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            code_rows,
        )
        claim_rows = []
        claim_id = 0
        for acc_id in range(1, n_accounts + 1):
            if rng.random() < 0.2:
                claim_id += 1
                claim_rows.append((
                    claim_id, rng.randint(1, 200), acc_id, f"user{acc_id:07d}@example.com",
                    now.isoformat(timespec="seconds"),
                ))
        conn.executemany(
            "INSERT INTO redeem_claims(id,code_id,account_id,email,claimed_at) VALUES(?,?,?,?,?)",
            claim_rows,
        )

        # 任务中心的租约表：让 Task Center 轮询也进入基准。
        stamp = now.isoformat(timespec="seconds")
        task_rows = []
        for task_id in range(1, min(n_events, n_accounts) + 1):
            task_rows.append((
                task_id, task_id, "plan_check", f"rec-{task_id}", "pending",
                rng.choice(["queued", "success", "failed", "running"]),
                f"task{task_id:07d}@example.com", rng.randint(0, 100), "queued", "", "",
                stamp, stamp, None, stamp, None,
            ))
        try:
            conn.executemany(
                "INSERT INTO account_tasks(id,account_id,job_type,source_record_id,status,source_status,"
                "email,progress,stage,progress_message,error_message,created_at,started_at,completed_at,"
                "updated_at,lease_until) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                task_rows,
            )
        except Exception as exc:  # 表结构变动时不让基准直接失败
            print(f"[warn] account_tasks seed skipped: {exc}", file=sys.stderr)
        conn.commit()
        conn.execute("PRAGMA optimize")
    return {"accounts": n_accounts, "pool": n_pool, "jobs": n_jobs, "codex": n_codex}


# --------------------------------------------------------------------------
# 基准场景
# --------------------------------------------------------------------------

def _scenarios(db) -> dict:
    accounts = db.count_accounts()
    sample_ids = [row["id"] for row in db.list_accounts(limit=200)]
    sample_emails = [str(row.get("email") or "") for row in db.list_accounts(limit=100)]
    sample_emails = [e for e in sample_emails if e] or ["nobody@example.com"]

    return {
        "accounts_page:default": lambda: db.list_accounts_page(limit=50, offset=0, archived="0"),
        "accounts_page:deep": lambda: db.list_accounts_page(limit=50, offset=2500, archived="0"),
        "accounts_page:archived": lambda: db.list_accounts_page(limit=50, offset=0, archived="1"),
        "accounts_page:all": lambda: db.list_accounts_page(limit=50, offset=0, archived="all"),
        "accounts_page:plan=plus": lambda: db.list_accounts_page(limit=50, offset=0, archived="0", plan_filter="plus"),
        "accounts_page:plan=free": lambda: db.list_accounts_page(limit=50, offset=0, archived="0", plan_filter="free"),
        "accounts_page:plan=promo": lambda: db.list_accounts_page(limit=50, offset=0, archived="0", plan_filter="promo"),
        "accounts_page:at=expired": lambda: db.list_accounts_page(limit=50, offset=0, archived="0", at_filter="expired"),
        "accounts_page:at=valid": lambda: db.list_accounts_page(limit=50, offset=0, archived="0", at_filter="valid"),
        "accounts_page:live=failed": lambda: db.list_accounts_page(limit=50, offset=0, archived="0", live_filter="failed"),
        "accounts_page:codex=success": lambda: db.list_accounts_page(limit=50, offset=0, archived="0", codex_filter="success"),
        "accounts_page:totp=enabled": lambda: db.list_accounts_page(limit=50, offset=0, archived="0", totp_filter="enabled"),
        "accounts_page:group": lambda: db.list_accounts_page(limit=50, offset=0, archived="0", group_filter="batch-a"),
        "accounts_page:redeemed": lambda: db.list_accounts_page(limit=50, offset=0, archived="all", redemption_filter="redeemed"),
        "accounts_page:unredeemed": lambda: db.list_accounts_page(limit=50, offset=0, archived="0", redemption_filter="unredeemed"),
        "accounts_page:q": lambda: db.list_accounts_page(limit=50, offset=0, archived="0", q="user0001234"),
        "accounts_page:date": lambda: db.list_accounts_page(limit=50, offset=0, archived="0", date_from="2025-01-01", date_to="2026-12-31"),
        "accounts_ids:5000": lambda: db.list_account_ids_page(limit=5000, offset=0, archived="0"),
        "plan_check_status:page": lambda: db.list_account_plan_check_statuses(limit=50, offset=0, archived="0"),
        "plan_check_status:5000": lambda: db.list_account_plan_check_statuses(limit=5000, offset=0, archived="0"),
        "account_groups": lambda: db.list_account_groups(),
        "find_by_emails:100": lambda: db.find_accounts_by_emails(sample_emails),
        "get_account_by_email": lambda: db.get_account_by_email(sample_emails[0]),
        "get_account": lambda: db.get_account(sample_ids[0]),
        "redeem_stock_summary": lambda: db.redeem_stock_summary(),
        "email_pool:page": lambda: db.list_email_pool_page(source="all", limit=50, offset=0),
        "email_pool:outlook": lambda: db.list_email_pool_page(source="outlook", limit=50, offset=0),
        "email_pool:search": lambda: db.list_email_pool_page(source="all", limit=50, offset=0, q="pool0001234"),
        "outlook_summary": lambda: db.outlook_pool_summary(),
        "count_accounts": lambda: db.count_accounts(),
        "jobs:page": lambda: db.list_jobs_page(limit=50, offset=0),
        "jobs:history": lambda: db.list_history_jobs_page(limit=20, offset=0),
        "jobs:status_counts": lambda: db.job_status_counts(),
        "jobs:active": lambda: db.list_active_jobs(limit=1000),
        "codex:page": lambda: db.list_codex_accounts_page(limit=50, offset=0),
        "codex:summary": lambda: db.codex_accounts_summary(),
        "account_total": lambda: accounts,
    }


def _run_once(fn) -> float:
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000.0


def run_bench(*, repeat: int = 5, warmup: int = 1, only: list[str] | None = None) -> dict:
    from core import db

    db._ensure_sqlite()
    results: dict[str, dict] = {}
    for name, fn in _scenarios(db).items():
        if only and not any(token in name for token in only):
            continue
        for _ in range(warmup):
            try:
                fn()
            except Exception as exc:  # 基准不应该吞掉错误
                results[name] = {"error": f"{type(exc).__name__}: {exc}"}
                break
        else:
            samples = []
            out = None
            for _ in range(repeat):
                start = time.perf_counter()
                out = fn()
                samples.append((time.perf_counter() - start) * 1000.0)
            results[name] = {
                "min_ms": round(min(samples), 3),
                "median_ms": round(statistics.median(samples), 3),
                "items": len(out.get("items", out)) if isinstance(out, dict) else len(out) if isinstance(out, list) else None,
            }
    return results


def _print_results(results: dict, baseline: dict | None = None) -> None:
    width = max((len(k) for k in results), default=10)
    print(f"{'scenario'.ljust(width)}  {'median ms':>10}  {'min ms':>10}  {'speedup':>8}")
    print("-" * (width + 34))
    for name, data in sorted(results.items(), key=lambda kv: -(kv[1].get("median_ms") or 0)):
        if "error" in data:
            print(f"{name.ljust(width)}  {'ERROR':>10}  {data['error']}")
            continue
        speedup = ""
        if baseline and name in baseline and baseline[name].get("median_ms"):
            factor = baseline[name]["median_ms"] / max(data["median_ms"], 1e-9)
            speedup = f"{factor:6.2f}x"
        print(f"{name.ljust(width)}  {data['median_ms']:10.3f}  {data['min_ms']:10.3f}  {speedup:>8}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQLite 查询性能基准")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="small")
    parser.add_argument("--data-dir", default=None, help="基准数据目录（默认临时目录）")
    parser.add_argument("--accounts", type=int, default=None)
    parser.add_argument("--pool", type=int, default=None)
    parser.add_argument("--jobs", type=int, default=None)
    parser.add_argument("--codex", type=int, default=None)
    parser.add_argument("--generate-only", action="store_true")
    parser.add_argument("--bench-only", action="store_true")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--only", default=None, help="只跑名称包含这些子串的场景（逗号分隔）")
    parser.add_argument("--json", default=None, help="把基准结果写入 JSON 文件")
    parser.add_argument("--baseline", default=None, help="读取基线 JSON 并打印加速比")
    parser.add_argument("--dump", default=None, help="把场景输出写入 JSON，用于优化前后结果比对")
    parser.add_argument("--keep", action="store_true", help="保留临时数据目录")
    args = parser.parse_args(argv)

    counts = dict(PRESETS[args.preset])
    for key in ("accounts", "pool", "jobs", "codex"):
        value = getattr(args, key)
        if value is not None:
            counts[key] = value

    temp_dir = None
    if args.data_dir:
        data_dir = Path(args.data_dir).expanduser().resolve()
    else:
        temp_dir = tempfile.mkdtemp(prefix="turb-bench-")
        data_dir = Path(temp_dir)

    if not args.bench_only:
        started = time.perf_counter()
        meta = generate(data_dir, counts)
        print(f"[gen] {meta} -> {data_dir} in {time.perf_counter() - started:.1f}s", file=sys.stderr)

    os.environ["TURB_DATA_DIR"] = str(data_dir)
    os.environ["TURB_ENV_FILE"] = str(data_dir / ".env")

    if args.generate_only:
        print(f"data dir: {data_dir}")
        return 0

    baseline = None
    if args.baseline:
        baseline = {k: v for k, v in json.loads(Path(args.baseline).read_text("utf-8")).items() if "error" not in v}

    only = [token for token in (args.only or "").split(",") if token]
    results = run_bench(repeat=args.repeat, only=only or None)
    _print_results(results, baseline)

    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), "utf-8")
        print(f"[out] {args.json}", file=sys.stderr)
    if args.dump:
        from core import db
        payload = {}
        for name, fn in _scenarios(db).items():
            if only and not any(token in name for token in only):
                continue
            try:
                payload[name] = _normalize(fn())
            except Exception as exc:
                payload[name] = {"__error__": f"{type(exc).__name__}: {exc}"}
        Path(args.dump).write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), "utf-8")
        print(f"[dump] {args.dump}", file=sys.stderr)
    if temp_dir and not args.keep:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return 0


def _normalize(value):
    """把查询结果规范化，便于优化前后逐字节比对。"""
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in sorted(value.items()) if k != "revision"}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
