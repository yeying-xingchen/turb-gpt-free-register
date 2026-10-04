"""Payment integration regression tests use an isolated SQLite database and fake HTTP."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import Mock, patch

from flask import Flask

from core import db, scan_api_service as service, scan_payment_store as store
from core.scan_api_client import ScanApiError
from webui.auth import init_auth, register_auth_routes
from webui.scan_routes import register_scan_routes

CDK = "CDK_private-payment-credential"
AT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhY2NvdW50In0.signature"
LINK = "https://payments.stripe.com/upi/instructions/example"


class ScanPaymentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        # Isolate every legacy migration source and the main runtime database.
        paths = {name: root / name for name, value in vars(db).items()
                 if name.startswith("_") and isinstance(value, Path)}
        paths.update(_DATA_DIR=root, _LOG_DIR=root / "logs", _SQLITE_READY=False, _SQLITE_READY_PATH=None)
        self.paths = patch.multiple(db, **paths)
        self.paths.start()
        self.addCleanup(self.paths.stop)
        db._ensure_sqlite()
        self.account = {"id": 7, "email": "buyer@example.test", "access_token": AT,
                        "extract_link_status": "success", "extract_link_type": "upi",
                        "extract_link_long_url": LINK}
        self.save_account(self.account)
        self.client = Mock(api_base="https://scan.example.test/api/v1", timeout=30)
        self.client.submit_upi.return_value = self.response("queued")
        self.client.get_task.return_value = self.response("succeeded")
        self.client_patch = patch.object(service, "_client", return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def save_account(self, account):
        with closing(db._sqlite_conn()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO accounts(id,email,payload) VALUES(?,?,?)",
                         (account["id"], account["email"], json.dumps(account)))

    def response(self, status, task_id="task-7", http_status=201, uncertain=False):
        return {"task": {"id": task_id, "status": status, "message": "平台任务状态"},
                "task_id": task_id, "request_id": "request-7", "http_status": http_status,
                "duplicate": False, "uncertain": uncertain}

    def submit(self, **kwargs):
        return service.submit_accounts(account_ids=kwargs.pop("account_ids", [7]),
                                       cdk=kwargs.pop("cdk", CDK), **kwargs)

    def test_public_submit_has_no_at_and_persists_before_http(self):
        def send(**kwargs):
            saved = store.latest(7)
            self.assertEqual(saved["status"], "submitting")
            self.assertEqual(saved["idempotency_key"], kwargs["idempotency_key"])
            self.assertEqual(saved["body"], {"link": LINK, "email": "buyer@example.test"})
            self.assertNotIn("access_token", kwargs)
            return self.response("queued")
        self.client.submit_upi.side_effect = send
        result = self.submit()
        self.assertEqual(result["created_count"], 1)
        self.assertFalse(db.get_account(7)["scan_request_ok"])
        self.assertEqual(db.get_account(7)["scan_request_provider"], "v1")
        saved = json.dumps(store.latest(7))
        self.assertNotIn(CDK, saved)
        self.assertNotIn(AT, saved)
        self.assertNotIn("credential_hash", json.dumps(service.local_status(7)))

    def test_masi_receives_full_selected_account_at(self):
        result = self.submit(provider="masi")
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(self.client.submit_upi.call_args.kwargs["access_token"], AT)
        self.assertEqual(self.client.submit_upi.call_args.kwargs["cdk"], CDK)
        self.assertNotIn(AT, json.dumps(store.latest(7)))

    def test_success_and_duplicate_click_never_send_second_post(self):
        first = self.submit(idempotency_key="click-00001")
        second = self.submit(idempotency_key="another-click-0002")
        self.assertEqual(first["created_count"], 1)
        self.assertEqual(second["duplicate_count"], 1)
        self.client.submit_upi.assert_called_once()

    def test_concurrent_submissions_send_once(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.submit(), range(2)))
        self.assertEqual(sum(result["created_count"] for result in results), 1)
        self.client.submit_upi.assert_called_once()

    def test_public_timeout_reuses_durable_key_body_after_reload(self):
        self.client.submit_upi.side_effect = [ScanApiError("timeout", uncertain=True), self.response("queued")]
        first = self.submit()
        self.assertEqual(first["unknown_count"], 1)
        self.assertTrue(first["unknown"][0]["can_retry_same_key"])
        saved = store.latest(7)
        self.account.update(email="changed@example.test", extract_link_long_url=LINK + "-changed")
        self.save_account(self.account)
        # The reopened dialog supplies a fresh local click key; the journal
        # still uses the original upstream key and frozen request payload.
        second = self.submit()
        self.assertEqual(second["created_count"], 1)
        calls = self.client.submit_upi.call_args_list
        self.assertEqual(calls[0].kwargs, calls[1].kwargs)
        self.assertEqual(saved["id"], store.latest(7)["id"])

    def test_masi_timeout_blocks_repost_even_after_lease_expired(self):
        self.client.submit_upi.side_effect = ScanApiError("timeout", uncertain=True)
        self.assertEqual(self.submit(provider="masi")["unknown_count"], 1)
        saved = store.latest(7)
        store.finish(saved, lease_until=0, status="unknown")
        result = self.submit(provider="masi")
        self.assertEqual(result["unknown_count"], 1)
        self.assertFalse(result["unknown"][0]["can_retry_same_key"])
        self.client.submit_upi.assert_called_once()

    def test_202_is_pending_and_then_only_queries_same_task(self):
        self.client.submit_upi.return_value = self.response("queued", http_status=202, uncertain=True)
        result = self.submit()
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["pending_count"], 1)
        self.assertEqual(store.latest(7)["status"], "submission_pending")
        self.submit()
        self.client.submit_upi.assert_called_once()
        query = service.query_accounts(account_ids=[7], cdk=CDK)
        self.assertEqual(query["items"][0]["status"], "succeeded")
        self.client.get_task.assert_called_once_with(cdk=CDK, task_id="task-7")
        self.assertTrue(db.get_account(7)["scan_request_ok"])

    def test_query_failures_preserve_remote_state(self):
        self.submit()
        self.client.get_task.side_effect = ScanApiError("lookup unavailable", status=503)
        result = service.query_accounts(account_ids=[7], cdk=CDK)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(store.latest(7)["status"], "queued")
        self.assertEqual(db.get_account(7)["scan_request_status"], "queued")

    def test_wrong_cdk_or_provider_cannot_repay_existing_order(self):
        self.submit()
        self.assertEqual(self.submit(cdk="different-cdk")["failed_count"], 1)
        self.assertEqual(self.submit(provider="masi")["failed_count"], 1)
        self.assertEqual(service.query_accounts(account_ids=[7], cdk="different-cdk")["failed_count"], 1)
        self.client.submit_upi.assert_called_once()
        self.client.get_task.assert_not_called()

    def test_one_rejection_does_not_make_whole_batch_success(self):
        self.save_account({**self.account, "id": 8, "email": "two@example.test", "extract_link_long_url": LINK + "-2"})
        self.client.submit_upi.side_effect = [self.response("queued"),
                                            ScanApiError("额度不足", status=402, code="INSUFFICIENT_CDK")]
        result = self.submit(account_ids=[7, 8])
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["failed"][0]["id"], 8)
        self.assertEqual(result["failed"][0]["code"], "INSUFFICIENT_CDK")

    def test_bad_link_and_non_upi_never_call_upstream(self):
        self.account["extract_link_long_url"] = "https://payments.stripe.com.attacker.test/link"
        self.save_account(self.account)
        self.assertEqual(self.submit()["failed_count"], 1)
        self.account.update(extract_link_long_url=LINK, extract_link_type="pix")
        self.save_account(self.account)
        self.assertEqual(self.submit()["failed_count"], 1)
        self.client.submit_upi.assert_not_called()

    def test_bool_and_float_account_ids_rejected(self):
        for value in (True, 7.1, "7", 0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.submit(account_ids=[value])

    def test_verifying_is_not_completed_even_if_expired(self):
        self.client.submit_upi.return_value = self.response("verifying")
        self.client.submit_upi.return_value["task"]["expiresAt"] = "2000-01-01T00:00:00Z"
        self.submit()
        self.assertEqual(store.latest(7)["status"], "verifying")
        self.assertFalse(db.get_account(7)["scan_request_ok"])

    def test_long_batch_keys_do_not_collide(self):
        self.assertNotEqual(service._account_key("x" * 127 + "a", 7),
                            service._account_key("x" * 127 + "b", 7))

    def test_unknown_retry_401_does_not_authorize_new_payment(self):
        for provider in ("v1", "orderhub"):
            with self.subTest(provider=provider):
                account_id = 7 if provider == "v1" else 8
                if account_id == 8:
                    row = {**self.account, "id": account_id}
                    row["extract_link_long_url"] = LINK + "-other"
                    self.save_account(row)
                self.client.submit_upi.side_effect = [
                    ScanApiError("timeout", uncertain=True),
                    ScanApiError("login expired", status=401, code="SESSION_REQUIRED"),
                ]
                first = self.submit(account_ids=[account_id], provider=provider)
                self.assertEqual(first["unknown_count"], 1)
                second = self.submit(account_ids=[account_id], provider=provider)
                self.assertEqual(second["unknown_count"], 1)
                self.assertEqual(store.latest(account_id)["status"], "unknown")
                calls = self.client.submit_upi.call_count
                denied = self.submit(account_ids=[account_id], provider="masi", cdk="different-key")
                self.assertEqual(denied["failed_count"], 1)
                self.assertEqual(self.client.submit_upi.call_count, calls)
        self.client.submit_upi.side_effect = None

    def test_orderhub_replay_requires_original_at(self):
        self.client.submit_upi.side_effect = ScanApiError("timeout", uncertain=True)
        result = self.submit(provider="orderhub")
        self.assertTrue(result["unknown"][0]["can_retry_same_key"])
        row = db.get_account(7)
        row["access_token"] = AT + "new"
        self.save_account(row)
        retry = self.submit(provider="orderhub")
        self.assertEqual(retry["failed_count"], 1)
        self.assertEqual(self.client.submit_upi.call_count, 1)
        self.assertEqual(store.latest(7)["status"], "unknown")
        self.assertNotIn(AT, json.dumps(store.latest(7)))

    def test_same_link_on_another_account_cannot_submit(self):
        self.submit()
        self.save_account({**self.account, "id": 8})
        result = self.submit(account_ids=[8])
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(self.client.submit_upi.call_count, 1)

    def test_can_fallback_requires_confirmed_failure(self):
        for status in ("failed", "rejected", "expired", "cancelled", "canceled", "released"):
            with self.subTest(status=status):
                self.assertTrue(store.can_fallback({"status": status, "task": {"status": status}}))
        for status in ("succeeded", "completed", "refunded", "unknown", "submitting", "queued",
                       "verifying", "submission_pending", "unexpected", "", None, []):
            with self.subTest(status=status):
                self.assertFalse(store.can_fallback({"status": status}))
        for record in (None, {}, {"status": "failed", "task": []},
                       {"status": "failed", "task": {"status": "succeeded"}},
                       {"status": "failed", "task": {"status": "unknown"}},
                       {"status": "failed", "task": {"verifying": True}},
                       {"status": "expired", "task": {"submission_uncertain": True}},
                       {"status": "failed", "charged": True},
                       {"status": "failed", "task": {"charged": True}},
                       {"status": "failed", "uncertain": True},
                       {"status": "rejected", "created": True},
                       {"status": "rejected", "uncertain_history": True}):
            with self.subTest(record=record):
                self.assertFalse(store.can_fallback(record))
        # A query may resolve a previous timeout to a confirmed task failure.
        self.assertTrue(store.can_fallback({"status": "failed", "uncertain_history": True,
                                            "task": {"status": "failed", "verifying": False}}))

    def test_every_confirmed_failure_allows_internal_fallback(self):
        for index, status in enumerate(("failed", "rejected", "expired", "cancelled", "canceled", "released")):
            with self.subTest(status=status):
                account_id = 20 + index
                self.save_account({**self.account, "id": account_id, "extract_link_long_url": LINK + str(index)})
                self.client.submit_upi.side_effect = [
                    ScanApiError("明确拒绝", status=402) if status == "rejected" else self.response(status),
                    self.response("queued", task_id="new-task"),
                ]
                self.submit(account_ids=[account_id])
                previous = store.latest(account_id)
                result = self.submit(account_ids=[account_id], cdk="next-cdk", fallback_from=previous["id"])
                self.assertEqual(result["created_count"], 1)
                current = store.latest(account_id)
                self.assertNotEqual(current["id"], previous["id"])
                self.assertEqual(current["fallback_from"], previous["id"])
                self.assertEqual(current["body"]["link"], previous["body"]["link"])
                self.assertNotIn("fallback_from", service.local_status(account_id))

    def test_multiple_failures_advance_candidates_even_with_equal_timestamps(self):
        self.client.submit_upi.side_effect = [self.response("failed"), self.response("expired"),
                                            self.response("released"), self.response("succeeded")]
        candidates = [("v1", CDK), ("v1", "second-cdk"), ("masi", "second-cdk"), ("orderhub", "third-cdk")]
        previous = None
        ids = []
        with patch.object(store.time, "time", return_value=1700000000.0):
            for provider, cdk in candidates:
                result = self.submit(provider=provider, cdk=cdk, idempotency_key="one-plus-run",
                                     fallback_from=previous["id"] if previous else None)
                self.assertEqual(result["created_count"], 1)
                current = store.latest(7)
                self.assertNotIn(current["id"], ids)
                ids.append(current["id"])
                self.assertEqual(current["provider"], provider)
                if previous:
                    self.assertEqual(current["fallback_from"], previous["id"])
                    self.assertEqual(current["idempotency_key"], previous["idempotency_key"])
                if provider == "masi":
                    # Cycling back to a candidate used earlier in this chain is forbidden.
                    calls = self.client.submit_upi.call_count
                    denied = self.submit(cdk=CDK, fallback_from=current["id"])
                    self.assertEqual(denied["failed_count"], 1)
                    self.assertEqual(self.client.submit_upi.call_count, calls)
                previous = current
        self.assertEqual(self.client.submit_upi.call_count, 4)
        self.assertEqual(store.latest(7)["status"], "succeeded")
        self.assertEqual(self.client.submit_upi.call_args.kwargs["access_token"], AT)
        self.assertNotIn("at_hash", self.client.submit_upi.call_args.kwargs)
        for reference in (ids[0], ids[-1]):
            denied = self.submit(cdk="fourth-cdk", fallback_from=reference)
            self.assertEqual(denied["failed_count"], 1)
        self.assertEqual(self.client.submit_upi.call_count, 4)

    def test_invalid_or_other_account_fallback_reference_never_sends(self):
        self.client.submit_upi.return_value = self.response("failed")
        self.submit()
        original = store.latest(7)
        self.save_account({**self.account, "id": 8, "extract_link_long_url": LINK + "-other"})
        self.submit(account_ids=[8])
        other = store.latest(8)
        for reference in ("", "missing-record", 7, True, [], {}, original["task_id"], other["id"]):
            with self.subTest(reference=reference):
                result = self.submit(cdk="next-cdk", fallback_from=reference)
                self.assertEqual(result["failed_count"], 1)
                self.assertEqual(store.latest(7)["id"], original["id"])
        self.assertEqual(self.client.submit_upi.call_count, 2)

    def test_fallback_requires_new_provider_credential_pair_even_if_base_changes(self):
        self.client.submit_upi.return_value = self.response("failed")
        self.submit()
        previous = store.latest(7)
        for api_base in (self.client.api_base, "https://another.example.test/api/v1"):
            with self.subTest(api_base=api_base):
                self.client.api_base = api_base
                result = self.submit(fallback_from=previous["id"])
                self.assertEqual(result["failed_count"], 1)
        self.client.submit_upi.assert_called_once()

    def test_success_or_uncertain_fallback_never_sends(self):
        responses = [self.response(status) for status in
                     ("succeeded", "completed", "refunded", "unknown", "queued", "verifying", "unexpected")]
        for flag in ("verifying", "submission_uncertain", "charged"):
            response = self.response("failed")
            response["task"][flag] = True
            responses.append(response)
        responses.extend([{**self.response("failed"), "charged": True},
                          self.response("failed", http_status=202),
                          ScanApiError("受理待核实", uncertain=True),
                          ScanApiError("已扣款", status=400, charged=True)])
        for index, response in enumerate(responses):
            with self.subTest(response=response):
                account_id = 30 + index
                self.save_account({**self.account, "id": account_id, "extract_link_long_url": LINK + str(account_id)})
                self.client.submit_upi.side_effect = [response]
                self.submit(account_ids=[account_id])
                previous = store.latest(account_id)
                calls = self.client.submit_upi.call_count
                self.assertFalse(store.can_fallback(previous))
                result = self.submit(account_ids=[account_id], cdk="next-cdk", fallback_from=previous["id"])
                self.assertEqual(result["failed_count"], 1)
                self.assertEqual(self.client.submit_upi.call_count, calls)
                self.assertEqual(store.latest(account_id), previous)

    def test_older_success_uncertainty_or_other_account_link_still_blocks_fallback(self):
        blockers = [(False, {"status": status}) for status in ("succeeded", "completed", "refunded", "unknown")]
        blockers += [(False, {"status": "failed", "task": {"verifying": True}}),
                     (False, {"status": "rejected", "uncertain_history": True, "task_id": ""}),
                     (True, {"status": "failed"}), (True, {"status": "rejected", "task_id": ""})]
        self.client.submit_upi.return_value = self.response("failed")
        for index, (other_account, changes) in enumerate(blockers):
            with self.subTest(other_account=other_account, changes=changes):
                account_id = 60 + index
                self.save_account({**self.account, "id": account_id, "extract_link_long_url": LINK + str(account_id)})
                self.submit(account_ids=[account_id])
                previous = store.latest(account_id)
                # Seed a legacy history hidden behind the latest failed record.
                old = {**previous, "id": "older-record-" + str(index), "created_at": previous["created_at"] - 1,
                       "account_id": 8 if other_account else account_id,
                       "idempotency_key": "older-key-" + str(index), "task": {}, **changes}
                with store._connection() as conn:
                    store._write(conn, old)
                calls = self.client.submit_upi.call_count
                result = self.submit(account_ids=[account_id], cdk="next-cdk", fallback_from=previous["id"])
                self.assertEqual(result["failed_count"], 1)
                self.assertEqual(self.client.submit_upi.call_count, calls)
                self.assertEqual(store.latest(account_id)["id"], previous["id"])

    def test_fallback_rechecks_terminal_state_inside_reservation(self):
        self.client.submit_upi.return_value = self.response("failed")
        self.submit()
        previous = store.latest(7)
        original_reserve = store.reserve

        def reserve_after_late_success(**kwargs):
            store.finish(previous, status="succeeded", task={"status": "succeeded"})
            return original_reserve(**kwargs)

        with patch.object(store, "reserve", side_effect=reserve_after_late_success):
            result = self.submit(cdk="next-cdk", fallback_from=previous["id"])
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(store.latest(7)["status"], "succeeded")
        self.client.submit_upi.assert_called_once()

    def test_concurrent_fallback_consumes_reference_once_before_http(self):
        self.client.submit_upi.return_value = self.response("failed")
        self.submit()
        previous = store.latest(7)
        self.client.submit_upi.reset_mock()
        barrier, loser_finished = Barrier(2), Event()
        original_reserve = store.reserve

        def synchronized_reserve(**kwargs):
            barrier.wait(timeout=5)
            try:
                return original_reserve(**kwargs)
            except ValueError:
                loser_finished.set()
                raise

        def send(**kwargs):
            self.assertTrue(loser_finished.wait(timeout=5))
            current = store.latest(7)
            self.assertEqual(current["status"], "submitting")
            self.assertEqual(current["fallback_from"], previous["id"])
            return self.response("queued", task_id="fallback-task")

        self.client.submit_upi.side_effect = send
        with patch.object(store, "reserve", side_effect=synchronized_reserve), ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.submit(cdk="next-cdk", idempotency_key="fallback-click",
                                                              fallback_from=previous["id"]), range(2)))
        self.assertEqual(sum(result["created_count"] for result in results), 1)
        self.assertEqual(sum(result["failed_count"] for result in results), 1)
        self.client.submit_upi.assert_called_once()
        replay = self.submit(cdk="next-cdk", idempotency_key="fallback-click", fallback_from=previous["id"])
        self.assertEqual(replay["failed_count"], 1)
        self.assertEqual(self.submit(cdk="next-cdk")["duplicate_count"], 1)
        self.client.submit_upi.assert_called_once()

    def test_old_rejection_key_cannot_replay_after_fallback(self):
        for index, outcome in enumerate((self.response("queued"), self.response("succeeded"),
                                         ScanApiError("timeout", uncertain=True))):
            with self.subTest(outcome=outcome):
                account_id = 80 + index
                self.save_account({**self.account, "id": account_id, "extract_link_long_url": LINK + str(account_id)})
                self.client.submit_upi.side_effect = [ScanApiError("额度不足", status=402), outcome]
                self.submit(account_ids=[account_id], idempotency_key="original-click")
                original = store.latest(account_id)
                self.submit(account_ids=[account_id], cdk="next-cdk", idempotency_key="fallback-click",
                            fallback_from=original["id"])
                current = store.latest(account_id)
                calls = self.client.submit_upi.call_count
                replay = self.submit(account_ids=[account_id], idempotency_key="original-click")
                self.assertEqual(replay["failed_count"], 1)
                self.assertEqual(self.client.submit_upi.call_count, calls)
                self.assertEqual(store.latest(account_id), current)

    def test_fallback_to_at_provider_requires_current_at(self):
        self.client.submit_upi.return_value = self.response("failed")
        self.submit()
        previous = store.latest(7)
        self.save_account({**self.account, "access_token": ""})
        for provider in ("masi", "orderhub"):
            result = self.submit(provider=provider, cdk="next-cdk", fallback_from=previous["id"])
            self.assertEqual(result["failed_count"], 1)
        self.client.submit_upi.assert_called_once()
        self.assertEqual(store.latest(7)["id"], previous["id"])

    def test_default_and_manual_routes_cannot_opt_in_to_internal_fallback(self):
        self.client.submit_upi.return_value = self.response("failed")
        self.submit()
        previous = store.latest(7)
        self.assertEqual(self.submit(cdk="next-cdk")["failed_count"], 1)
        self.assertEqual(self.submit()["duplicate_count"], 1)
        app = Flask(__name__)
        app.config["TESTING"] = True
        init_auth(app, auth_code="local-test")
        register_scan_routes(app)
        response = app.test_client().post("/api/accounts/scan-requests", headers={"X-Auth-Code": "local-test"},
                                         json={"account_ids": [7], "cdk": "next-cdk", "fallback_from": previous["id"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["failed_count"], 1)
        self.client.submit_upi.assert_called_once()
        self.assertEqual(store.latest(7)["id"], previous["id"])

    def test_orderhub_session_routes_use_opaque_httponly_handle(self):
        app = Flask(__name__)
        app.config["TESTING"] = True
        init_auth(app, auth_code="local-test")
        register_scan_routes(app)
        client = app.test_client()
        headers = {"X-Auth-Code": "local-test"}
        auth = {"identity": "orderhub-user:42", "api_base": self.client.api_base,
                "user": {"username": "00123456"}, "cookies": "private-upstream-cookie"}
        with patch("webui.scan_routes.orderhub_client.login", return_value=("opaque-handle", auth["user"])), \
             patch("webui.scan_routes.orderhub_client.get_session", return_value=auth), \
             patch("webui.scan_routes.orderhub_client.logout"):
            response = client.post("/api/payments/orderhub/login", json={"username": "00123456", "password": "ExamplePass1!"}, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertIn("HttpOnly", response.headers["Set-Cookie"])
            self.assertNotIn("private-upstream-cookie", str(response.headers) + response.text)
            self.assertNotIn("ExamplePass1!", response.text)
            submit = client.post("/api/accounts/scan-requests", json={"account_ids": [7], "provider": "orderhub", "auth_mode": "session"}, headers=headers)
            self.assertEqual(submit.json["created_count"], 1)
            self.assertEqual(self.client.session_auth, auth)
            query = client.post("/api/accounts/scan-requests/query", json={"account_ids": [7], "auth_mode": "session"}, headers=headers)
            self.assertEqual(query.json["count"], 1)
            self.assertNotIn("private-upstream-cookie", json.dumps(store.latest(7)))
            logout = client.post("/api/payments/orderhub/logout", json={}, headers=headers)
            self.assertEqual(logout.status_code, 200)
            with client.session_transaction() as browser_session:
                self.assertNotIn("orderhub_session_handle", browser_session)

    def test_account_poll_snapshot_clears_old_payment_error(self):
        self.client.submit_upi.side_effect = [ScanApiError("temporarily full", status=429), self.response("queued")]
        self.submit()
        first = db.list_account_plan_check_statuses()
        self.assertEqual(first["items"][0]["scan_request_error"], "temporarily full")
        self.submit()
        second = db.list_account_plan_check_statuses()
        self.assertEqual(second["items"][0]["scan_request_error"], "")
        self.assertNotEqual(first["revision"], second["revision"])
        self.assertNotIn(AT, json.dumps(second))

    def test_routes_enforce_auth_and_hide_credentials(self):
        app = Flask(__name__)
        app.config["TESTING"] = True
        init_auth(app, auth_code="local-test")
        register_auth_routes(app)
        register_scan_routes(app)
        client = app.test_client()
        self.assertEqual(client.post("/api/accounts/scan-requests", json={}).status_code, 401)
        headers = {"X-Auth-Code": "local-test"}
        self.assertEqual(client.post("/api/accounts/scan-requests", json=[], headers=headers).status_code, 400)
        response = client.post("/api/accounts/scan-requests", json={"account_ids": [7], "cdk": CDK}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.json["created_count"], 1)
        self.assertNotIn(CDK, response.text)
        self.assertNotIn(AT, response.text)
        status = client.get("/api/accounts/7/scan-request", headers=headers)
        self.assertEqual(status.json["item"]["task_id"], "task-7")
        self.assertNotIn("credential_hash", status.text)
        query = client.post("/api/accounts/scan-requests/query", json={"account_ids": [7], "cdk": CDK}, headers=headers)
        self.assertEqual(query.json["items"][0]["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
