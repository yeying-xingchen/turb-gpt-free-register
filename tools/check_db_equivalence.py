# -*- coding: utf-8 -*-
"""行为等价性检查：同一份数据库上分别跑旧/新实现，逐字段比对读接口输出。

用法::

    python tools/check_db_equivalence.py --repo /tmp/orig-repo --data-dir /tmp/eq-db --out /tmp/old.json
    python tools/check_db_equivalence.py --repo .            --data-dir /tmp/eq-db --out /tmp/new.json
    python tools/check_db_equivalence.py --compare /tmp/old.json /tmp/new.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _normalize(value):
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in sorted(value.items()) if k != "revision"}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def _scenarios(db) -> dict:
    ids = [row["id"] for row in db.list_accounts(limit=400, archived="all")]
    emails = [str(row.get("email") or "") for row in db.list_accounts(limit=120, archived="all")]
    emails = [e for e in emails if e]
    groups = [g["group_name"] for g in db.list_account_groups()]
    sample = {
        "ids": ids,
        "emails": emails,
        "groups": groups,
        "plan": "plus",
    }
    scenarios: dict = {
        "accounts_page:default": lambda: db.list_accounts_page(limit=50, offset=0, archived="0"),
        "accounts_page:deep": lambda: db.list_accounts_page(limit=50, offset=120, archived="0"),
        "accounts_page:all": lambda: db.list_accounts_page(limit=30, offset=0, archived="all"),
        "accounts_page:archived": lambda: db.list_accounts_page(limit=30, offset=0, archived="1"),
        "accounts_page:plan_plus": lambda: db.list_accounts_page(limit=30, archived="0", plan_filter="plus"),
        "accounts_page:plan_free": lambda: db.list_accounts_page(limit=30, archived="0", plan_filter="free"),
        "accounts_page:plan_promo": lambda: db.list_accounts_page(limit=30, archived="0", plan_filter="promo"),
        "accounts_page:plan_free_no_trial": lambda: db.list_accounts_page(limit=30, archived="0", plan_filter="free_no_trial"),
        "accounts_page:codex": lambda: db.list_accounts_page(limit=30, archived="0", codex_filter="success"),
        "accounts_page:codex_dead": lambda: db.list_accounts_page(limit=30, archived="0", codex_filter="deactivated"),
        "accounts_page:live_failed": lambda: db.list_accounts_page(limit=30, archived="0", live_filter="failed"),
        "accounts_page:live_never": lambda: db.list_accounts_page(limit=30, archived="0", live_filter="never"),
        "accounts_page:totp": lambda: db.list_accounts_page(limit=30, archived="0", totp_filter="enabled"),
        "accounts_page:at_expired": lambda: db.list_accounts_page(limit=30, archived="0", at_filter="expired"),
        "accounts_page:at_valid": lambda: db.list_accounts_page(limit=30, archived="0", at_filter="valid"),
        "accounts_page:at_unknown": lambda: db.list_accounts_page(limit=30, archived="0", at_filter="unknown"),
        "accounts_page:redeemed": lambda: db.list_accounts_page(limit=30, archived="all", redemption_filter="redeemed"),
        "accounts_page:unredeemed": lambda: db.list_accounts_page(limit=30, archived="0", redemption_filter="unredeemed"),
        "accounts_page:q": lambda: db.list_accounts_page(limit=30, archived="0", q="user0001234"),
        "accounts_page:q_upper": lambda: db.list_accounts_page(limit=30, archived="0", q="USER0001234"),
        "accounts_page:date": lambda: db.list_accounts_page(limit=30, archived="0", date_from="2020-01-01", date_to="2030-12-31"),
        "accounts_page:combo": lambda: db.list_accounts_page(
            limit=30, archived="0", plan_filter="free", live_filter="success", totp_filter="disabled",
            redemption_filter="unredeemed",
        ),
        "accounts_ids": lambda: db.list_account_ids_page(limit=500, archived="0"),
        "accounts_ids_at": lambda: db.list_account_ids_page(limit=200, archived="0", at_filter="expired"),
        "plan_check_status": lambda: db.list_account_plan_check_statuses(limit=40, archived="0"),
        "plan_check_status_filtered": lambda: db.list_account_plan_check_statuses(
            limit=40, archived="0", plan_filter="plus"
        ),
        "account_groups": lambda: db.list_account_groups(),
        "account_groups_public": lambda: db.list_account_groups(public_only=True),
        "redeem_stock": lambda: db.redeem_stock_summary(),
        "get_account_by_email": lambda: [
            db.get_account_by_email(e) or {} for e in emails[:8]
        ],
        "get_account_by_email_missing": lambda: db.get_account_by_email("nobody@example.com"),
        "get_account_by_email_unicode": lambda: db.get_account_by_email("Üser@Example.com"),
        "find_by_emails": lambda: db.find_accounts_by_emails(emails[:60]),
        "find_by_emails_filtered": lambda: db.find_accounts_by_emails(
            emails[:60], archived="0", at_filter="valid"
        ),
        "find_by_emails_unknown": lambda: db.find_accounts_by_emails(["nobody@example.com"]),
        "email_pool_page": lambda: db.list_email_pool_page(source="all", limit=30, offset=0),
        "email_pool_outlook": lambda: db.list_email_pool_page(source="outlook", limit=30),
        "email_pool_search": lambda: db.list_email_pool_page(source="all", limit=30, q="pool0001234"),
        "email_pool_search_upper": lambda: db.list_email_pool_page(source="all", limit=30, q="POOL0001234"),
        "outlook_summary": lambda: db.outlook_pool_summary(),
        "generic_summary": lambda: db.generic_api_email_pool_summary(),
        "imap_summary": lambda: db.imap_email_pool_summary(),
        "domain_summary": lambda: db.domain_email_pool_summary(),
        "jobs_page": lambda: db.list_jobs_page(limit=20, offset=0),
        "jobs_history": lambda: db.list_history_jobs_page(limit=20, offset=0),
        "jobs_counts": lambda: db.job_status_counts(),
        "jobs_active": lambda: db.list_active_jobs(limit=200),
        "codex_page": lambda: db.list_codex_accounts_page(limit=20),
        "codex_summary": lambda: db.codex_accounts_summary(),
        "count_accounts": lambda: db.count_accounts(),
        "get_account": lambda: [db.get_account(i) or {} for i in ids[:5]],
        "list_redeem_codes": lambda: db.list_redeem_codes(limit=20),
    }
    return scenarios


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--only", default=None)
    parser.add_argument("--compare", nargs=2, default=None)
    args = parser.parse_args(argv)

    if args.compare:
        old = json.loads(Path(args.compare[0]).read_text("utf-8"))
        new = json.loads(Path(args.compare[1]).read_text("utf-8"))
        keys = sorted(set(old) | set(new))
        diffs = []
        for key in keys:
            if old.get(key) != new.get(key):
                diffs.append(key)
        print(f"scenarios: {len(keys)}  identical: {len(keys) - len(diffs)}  different: {len(diffs)}")
        for key in diffs:
            print(f"  DIFF {key}")
            before = json.dumps(old.get(key), ensure_ascii=False, sort_keys=True)
            after = json.dumps(new.get(key), ensure_ascii=False, sort_keys=True)
            for pos in range(min(len(before), len(after))):
                if before[pos] != after[pos]:
                    print(f"    old: ...{before[max(0, pos - 120):pos + 160]}")
                    print(f"    new: ...{after[max(0, pos - 120):pos + 160]}")
                    break
            else:
                print(f"    old len={len(before)} new len={len(after)}")
                print(f"    old: ...{before[-200:]}")
                print(f"    new: ...{after[-200:]}")
        return 1 if diffs else 0

    repo = str(Path(args.repo).resolve())
    data_dir = str(Path(args.data_dir).resolve())
    os.environ["TURB_DATA_DIR"] = data_dir
    os.environ["TURB_ENV_FILE"] = str(Path(data_dir) / ".env")
    sys.path.insert(0, repo)
    from core import db  # noqa: E402

    assert str(Path(db.__file__).resolve()).startswith(repo), db.__file__
    db._ensure_sqlite()
    only = [token for token in (args.only or "").split(",") if token]
    out = {}
    for name, fn in _scenarios(db).items():
        if only and not any(token in name for token in only):
            continue
        try:
            out[name] = _normalize(fn())
        except Exception as exc:
            out[name] = {"__error__": f"{type(exc).__name__}: {exc}"}
    text = json.dumps(out, ensure_ascii=False, sort_keys=True, indent=1)
    if args.out:
        Path(args.out).write_text(text, "utf-8")
        print(f"wrote {args.out} ({len(out)} scenarios)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
