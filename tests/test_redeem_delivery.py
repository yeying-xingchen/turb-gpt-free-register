"""Allocation and delivery must commit together and survive response loss."""
from pathlib import Path
import multiprocessing
import subprocess
import sys
from unittest.mock import patch

import pytest

from core import db, redeem_delivery as delivery
from webui.app import create_app

REQUEST_ID = "a" * 32
OTHER_ID = "b" * 32


def stock(quantity=3):
    for i in range(quantity):
        db.insert_account(email=f"delivery-{i}@example.test", access_token="fixture-at",
                          plan_type="plus", totp_secret="FIXTURE-TOTP",
                          extra={"registration_password": f"fixture-password-{i}"})
    return db.create_redeem_code(quantity=quantity, account_group="默认分组")["code"]


def client():
    return create_app(auth_code="fixture-admin").test_client()


def claim(code, request_id=REQUEST_ID, quantity=1):
    return client().post("/api/redeem", json={"cdk": code, "quantity": quantity, "request_id": request_id})


def test_lost_response_replays_original_allocation_after_application_restart():
    code = stock()
    with patch("webui.app._enqueue_plan_checks_after_redeem") as enqueue:
        first = claim(code)
        assert first.status_code == 200
        db._SQLITE_READY = False
        replay = claim(code)
        assert replay.status_code == 200
        assert replay.json["resumed"] is True
        assert replay.json["download_url"] == first.json["download_url"]
        assert replay.json["expires_at"] == first.json["expires_at"]
        assert enqueue.call_count == 1
    assert db.list_redeem_codes()[0]["redeemed_count"] == 1
    assert db.redeem_stock_summary()["available"] == 2


def test_download_can_retry_within_fixed_window_and_contains_original_snapshot():
    code = stock(1)
    response = claim(code)
    first = client().get(response.json["download_url"])
    second = client().get(response.json["download_url"])
    assert first.status_code == second.status_code == 200
    assert first.data == second.data
    assert b"fixture-password-0" in second.data
    assert b"fixture-at" not in second.data
    assert "no-store" in second.headers["Cache-Control"]


def test_delivery_is_readable_in_another_python_process():
    code = stock(1)
    response = claim(code)
    download_id = response.json["download_url"].rsplit("/", 1)[1]
    script = """
import sys
from core import db
from core.redeem_delivery import read_download
db._SQLITE_PATH = db._DEFAULT_SQLITE_PATH = __import__('pathlib').Path(sys.argv[1])
item = read_download(sys.argv[2])
print(bool(item and b'fixture-password-0' in item['content']))
"""
    process = subprocess.run([sys.executable, "-c", script, str(db._active_sqlite_path()), download_id],
                             cwd=Path(__file__).resolve().parents[1], capture_output=True,
                             text=True, timeout=15, check=True)
    assert process.stdout.strip() == "True"


def test_expiry_is_checked_without_creating_another_delivery():
    code = stock()
    with patch.object(delivery.time, "time", return_value=1000):
        first = claim(code)
    with patch.object(delivery.time, "time", return_value=1600):
        assert client().get(first.json["download_url"]).status_code == 404
        replay = claim(code)
        assert replay.status_code == 410
        assert replay.json["code"] == "delivery_expired"
        assert replay.json["remaining"] == 2
        assert claim(code, OTHER_ID).status_code == 200
    assert db.list_redeem_codes()[0]["redeemed_count"] == 2


def test_recovery_keys_and_normalized_codes_preserve_independent_batches():
    code_a = stock(6)
    code_b = db.create_redeem_code(quantity=3, account_group="默认分组")["code"]
    first = claim(code_a)
    assert claim(code_b).status_code == 200
    assert claim(code_b, OTHER_ID).status_code == 200
    replay = claim(" \t" + " ".join(code_a.lower()) + "\n")
    assert replay.status_code == 200
    assert replay.json["resumed"] is True
    assert replay.json["credentials"] == first.json["credentials"]
    assert replay.json["download_url"] == first.json["download_url"]
    codes = {item["code"]: item for item in db.list_redeem_codes()}
    assert codes[code_a]["redeemed_count"] == 1
    assert codes[code_b]["redeemed_count"] == 2


def test_each_explicit_new_batch_has_its_own_receipt_and_inventory_is_not_reused():
    code = stock(3)
    first = claim(code)
    second = claim(code, OTHER_ID)
    assert first.status_code == second.status_code == 200
    assert first.json["download_url"] != second.json["download_url"]
    assert client().get(first.json["download_url"]).data != client().get(second.json["download_url"]).data
    assert claim(code).json["remaining"] == 1
    assert db.list_redeem_codes()[0]["redeemed_count"] == 2


def test_expiring_old_batch_cannot_remove_new_batch_or_forget_its_request():
    code = stock(3)
    with patch.object(delivery.time, "time", return_value=1000):
        first = claim(code)
    with patch.object(delivery.time, "time", return_value=1500):
        second = claim(code, OTHER_ID)
    with patch.object(delivery.time, "time", return_value=1601):
        assert client().get(first.json["download_url"]).status_code == 404
        assert client().get(second.json["download_url"]).status_code == 200
        assert claim(code).status_code == 410
    assert db.list_redeem_codes()[0]["redeemed_count"] == 2


def test_replay_cannot_change_original_quantity():
    code = stock()
    assert claim(code).status_code == 200
    replay = claim(code, quantity=2)
    assert replay.status_code == 409
    assert replay.json["code"] == "request_conflict"
    assert db.list_redeem_codes()[0]["redeemed_count"] == 1


def _simultaneous_claim(root, code, barrier, results):
    # Spawned processes bind their own module state before opening storage.
    db.configure_storage(root)
    db._ensure_sqlite()
    barrier.wait(timeout=15)
    result = db.redeem_plus_accounts(code, quantity=1, request_id=REQUEST_ID)
    results.put((result["resumed"], result["download_id"]))


def test_simultaneous_same_request_in_two_processes_allocates_once(tmp_path):
    code = stock(3)
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_simultaneous_claim,
                                  args=(str(tmp_path), code, barrier, results)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        received = [results.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert sorted(item[0] for item in received) == [False, True]
        assert received[0][1] == received[1][1]
        assert db.list_redeem_codes()[0]["redeemed_count"] == 1
        assert db.redeem_stock_summary()["available"] == 2
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
        results.join_thread()


def test_wrong_requester_cannot_recover_exhausted_cdk():
    code = stock(1)
    assert claim(code).status_code == 200
    assert claim(code, OTHER_ID).status_code == 410
    assert client().get("/api/redeem/download/" + "0" * 64).status_code == 404


def test_failure_to_persist_delivery_rolls_back_allocation():
    code = stock(1)
    with patch.object(delivery, "save", side_effect=RuntimeError("fixture write failure")):
        response = claim(code)
    assert response.status_code == 500
    assert db.list_redeem_codes()[0]["redeemed_count"] == 0
    assert db.redeem_stock_summary()["available"] == 1
    assert claim(code).status_code == 200


def test_revocation_blocks_previously_issued_delivery():
    code = stock(1)
    response = claim(code)
    db.revoke_redeem_code(db.list_redeem_codes()[0]["id"])
    assert client().get(response.json["download_url"]).status_code == 404
    assert claim(code).status_code == 410


@pytest.mark.parametrize("request_id", ["", "short", True, 123, "x" * 129, "!" * 32])
def test_invalid_recovery_key_never_consumes_inventory(request_id):
    code = stock(1)
    assert claim(code, request_id).status_code == 400
    assert db.list_redeem_codes()[0]["redeemed_count"] == 0


def test_public_download_never_uses_admin_memory_exports():
    assert client().get("/api/redeem/download/" + "a" * 32).status_code == 404
    assert client().get("/api/downloads/" + "a" * 32).status_code == 401
