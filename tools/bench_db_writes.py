# -*- coding: utf-8 -*-
"""写入路径微基准：确认生成列/索引没有把写操作拖慢。

    python tools/bench_db_writes.py --repo . --data-dir /tmp/write-new
    python tools/bench_db_writes.py --repo /tmp/orig-repo --data-dir /tmp/write-old
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path


def _jwt(seed: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 4102444800, "sub": seed}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--rows", type=int, default=400)
    args = parser.parse_args(argv)

    repo = str(Path(args.repo).resolve())
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    for stale in data_dir.glob("turb.sqlite3*"):
        stale.unlink()
    os.environ["TURB_DATA_DIR"] = str(data_dir)
    os.environ["TURB_ENV_FILE"] = str(data_dir / ".env")
    sys.path.insert(0, repo)
    from core import db  # noqa: E402

    assert str(Path(db.__file__).resolve()).startswith(repo)
    db._ensure_sqlite()
    rows = args.rows

    started = time.perf_counter()
    ids = []
    for index in range(rows):
        ids.append(db.insert_account(
            email=f"write{index:06d}@example.test",
            access_token=_jwt(f"write{index:06d}@example.test"),
            extra={"registration_password": f"pw-{index}"},
            plan_type="plus" if index % 3 == 0 else "free",
        ))
    insert_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    for index, acc_id in enumerate(ids):
        db.update_account_plan_check(acc_id=acc_id, result={
            "ok": True, "plan_type": "plus" if index % 2 else "free", "checked_at": db._now(),
        })
    update_ms = (time.perf_counter() - started) * 1000

    snapshot = db._load_accounts()
    started = time.perf_counter()
    db._save_accounts(snapshot)
    save_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    for acc_id in ids[: max(1, rows // 4)]:
        db.archive_account(acc_id, True)
    archive_ms = (time.perf_counter() - started) * 1000

    print(f"repo={repo}")
    print(f"  insert_account x{rows}:          {insert_ms:8.1f} ms ({insert_ms / rows:.2f} ms/op)")
    print(f"  update_account_plan_check x{rows}: {update_ms:8.1f} ms ({update_ms / rows:.2f} ms/op)")
    print(f"  _save_accounts({len(snapshot)}):       {save_ms:8.1f} ms")
    print(f"  archive_account x{rows // 4}:        {archive_ms:8.1f} ms")
    print(f"  db size: {os.path.getsize(data_dir / 'turb.sqlite3') / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
