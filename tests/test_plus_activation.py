"""Regression coverage for Plus activation; isolated SQLite and no upstream HTTP."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask

from core import db, plus_activation_service as service, plus_activation_store as store
from core import scan_api_service as payments, scan_payment_store as payment_store
from core.scan_api_client import ScanApiError
from webui import auth as web_auth
from webui.scan_routes import register_scan_routes

AT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhY3RpdmF0aW9uIn0.private-signature"
LINK_CDK = "link-code-private-regression"
PAY_CDK = "pay-code-private-regression"
LINK = "https://payments.stripe.com/upi/instructions/activation-new"
FREE = {"ok": True, "current_plan_type": "free", "plus_trial_eligible": True}
PLUS = {"ok": True, "current_plan_type": "plus", "plus_trial_eligible": False}
ENDPOINT = "/api/accounts/activate-plus"


class CapturingExecutor:
    """Workers run only when the test explicitly advances the queue."""
    def __init__(self):
        self.calls = []
        self.pending = []
        self.lock = threading.Lock()

    def submit(self, function, *args, **kwargs):
        future = Future()
        with self.lock:
            self.calls.append((function, args, kwargs))
            self.pending.append((function, args, kwargs, future))
        return future

    def run_all(self):
        while self.pending:
            function, args, kwargs, future = self.pending.pop(0)
            future.set_result(function(*args, **kwargs))


class ActivationFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        paths = {name: root / name for name, value in vars(db).items()
                 if name.startswith("_") and isinstance(value, Path)}
        paths.update(_DATA_DIR=root, _LOG_DIR=root / "logs", _SQLITE_READY=False,
                     _SQLITE_READY_PATH=None)
        self.start_patch(patch.multiple(db, **paths))
        db._ensure_sqlite()
        self.account = {"id": 7, "email": "activation@example.test", "access_token": AT,
                        "current_plan_type": "free", "plus_trial_eligible": True}
        self.save_account(self.account)
        self.events = []
        self.plan_results = [dict(FREE), dict(PLUS)]
        self.executor = CapturingExecutor()
        self.start_patch(patch.object(service, "_EXECUTOR", self.executor))
        self.start_patch(patch.object(service, "_QUEUE_SLOTS", threading.BoundedSemaphore(20)))
        self.start_patch(patch.object(service, "_VERIFY_ATTEMPTS", 2))
        self.sleep = self.start_patch(patch.object(service.time, "sleep",
                                                   side_effect=AssertionError("unexpected polling delay")))
        self.start_patch(patch.object(service.plan_check_service, "_wait_for_rate_slot"))
        self.plan = self.start_patch(patch.object(service.plan_check_service, "check_account_plan",
                                                  side_effect=self.check_plan))
        self.provider = self.start_patch(patch.object(service.extraction, "_resolve_provider", return_value={
            "id": 0, "provider_type": "extract", "enabled": True,
            "api_base": "https://extract.example.test", "name": "Fake extraction",
        }))
        self.extraction_result = self.successful_extraction_result()
        self.extract = self.start_patch(patch.object(service.extraction, "enqueue_account_extract",
                                                     side_effect=self.extract_success))
        self.refresh = self.start_patch(patch.object(service.extraction, "task_action",
                                                     side_effect=AssertionError("unexpected extraction refresh")))
        self.client = Mock(api_base="https://scan.example.test/api/v1", timeout=1)
        self.client.submit_upi.side_effect = self.submit_upi
        self.client.get_task.side_effect = self.get_task
        self.start_patch(patch.object(payments, "_client", return_value=self.client))
        self.http = self.start_patch(patch("requests.sessions.Session.request",
                                           side_effect=AssertionError("real HTTP is forbidden")))
        self.curl = self.start_patch(patch("curl_cffi.requests.Session.request",
                                           side_effect=AssertionError("real HTTP is forbidden")))
        self.addCleanup(self.http.assert_not_called)
        self.addCleanup(self.curl.assert_not_called)

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def save_account(self, account):
        with closing(db._sqlite_conn()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO accounts(id,email,payload) VALUES(?,?,?)",
                         (account["id"], account["email"], json.dumps(account)))

    def update_account(self, account_id=7, **changes):
        account = db.get_account(account_id)
        account.update(changes)
        self.save_account(account)
        return account

    def check_plan(self, token):
        result = self.plan_results[0]
        if len(self.plan_results) > 1:
            result = self.plan_results.pop(0)
        self.events.append("plan:" + str(result.get("current_plan_type")))
        return dict(result)

    @staticmethod
    def response(status, task_id="activation-task-7", *, uncertain=False, http_status=200):
        return {"task": {"id": task_id, "status": status, "message": "Fake payment status"},
                "task_id": task_id, "request_id": "activation-request-7",
                "http_status": http_status, "uncertain": uncertain, "duplicate": False}

    @staticmethod
    def successful_extraction_result():
        return {"ok": True, "status": "success", "link_type": "upi", "task_id": "new-extract-task",
                "result": {"long_url": LINK, "expires_at": "2099-01-01T00:00:00Z"}}

    def extract_success(self, **kwargs):
        self.events.append("extract")
        provider = self.provider.return_value
        accepted = db.claim_account_extract(
            kwargs["account_id"], trigger=kwargs["trigger"], link_type=kwargs["link_type"],
            provider_id=kwargs["provider_id"], provider_type=provider["provider_type"],
            provider_name=provider["name"], cdk_id=kwargs.get("cdk_id"),
        )
        if accepted:
            db.mark_account_extract_running(kwargs["account_id"])
            db.update_account_extract(kwargs["account_id"], self.extraction_result)
        return {"accepted": accepted, "busy": not accepted}

    def seed_checkout(self, status="interrupted", provider_type="lumen"):
        """Create provenance through a real activation, then pause at extraction."""
        self.provider.return_value["provider_type"] = provider_type
        self.extraction_result = {"ok": False, "status": status, "task_id": "original-extract-task"}
        self.activate()
        account = db.get_account(7)
        self.assertEqual(account["extract_link_trigger"], "plus_activation")
        self.assertTrue(account["plus_activation_checkout_key"])
        self.assert_no_payment()
        self.extract.reset_mock()
        self.plan.reset_mock()
        self.events.clear()
        self.plan_results = [dict(FREE), dict(PLUS)]
        self.extraction_result = self.successful_extraction_result()
        return account

    def submit_upi(self, **kwargs):
        self.events.append("submit")
        return self.response("queued", http_status=201)

    def get_task(self, **kwargs):
        self.events.append("query")
        return self.response("succeeded")

    def enqueue(self, account_ids=None, *, payment_options=None, extraction_options=None, success_group=None):
        if extraction_options is None:
            extraction_options = {"provider_id": 0, "cdk": LINK_CDK}
            if self.provider.return_value["provider_type"] == "upi_git5":
                extraction_options["entry_proxies"] = ["http://proxy.example.test:8080"]
        return service.enqueue_accounts(
            account_ids=[7] if account_ids is None else account_ids,
            extraction_options=extraction_options,
            payment_options={"provider": "v1", "cdk": PAY_CDK, "auth_mode": "key"}
            if payment_options is None else payment_options,
            success_group=success_group,
        )

    def activate(self, **kwargs):
        result = self.enqueue(**kwargs)
        self.executor.run_all()
        return result

    def assert_no_payment(self):
        self.client.submit_upi.assert_not_called()
        self.client.get_task.assert_not_called()
        self.assertIsNone(payment_store.latest(7))

    def assert_safe(self, value):
        serialized = json.dumps(value, ensure_ascii=False)
        for secret in (AT, LINK_CDK, PAY_CDK, "private-upstream-cookie"):
            self.assertNotIn(secret, serialized)
        return serialized

    def make_web_client(self):
        self.start_patch(patch.multiple(web_auth, _AUTH_CODE=None, _GENERATED=False))
        app = Flask(__name__)
        app.config["TESTING"] = True
        web_auth.init_auth(app, auth_code="local-test")
        web_auth.register_auth_routes(app)
        register_scan_routes(app)
        return app.test_client()

    @staticmethod
    def request_body():
        return {"account_ids": [7], "extraction": {"provider_id": 0, "cdk": LINK_CDK},
                "payment": {"provider": "v1", "cdk": PAY_CDK, "auth_mode": "key"}}


class PlusActivationServiceTests(ActivationFixture):
    def test_first_activation_extracts_submits_queries_then_verifies_actual_plus(self):
        def submit(**kwargs):
            saved = payment_store.latest(7)
            self.assertEqual(saved["status"], "submitting")
            self.assertEqual(saved["idempotency_key"], kwargs["idempotency_key"])
            self.assertEqual(db.get_account(7)["current_plan_type"], "free")
            return self.submit_upi(**kwargs)
        self.client.submit_upi.side_effect = submit
        result = self.enqueue()
        self.assertEqual(result["started_count"], 1)
        self.assertEqual(result["started"][0]["status"], "queued")
        self.extract.assert_not_called()
        self.client.submit_upi.assert_not_called()
        self.executor.run_all()
        self.assertEqual(self.events, ["plan:free", "extract", "submit", "query", "plan:plus"])
        self.extract.assert_called_once_with(account_id=7, email=self.account["email"],
                                             access_token=AT, trigger="plus_activation",
                                             provider_id=0, cdk=LINK_CDK, link_type="upi", payment_amount=0)
        self.assertEqual(self.client.submit_upi.call_args.kwargs["link"], LINK)
        self.assertNotIn("access_token", self.client.submit_upi.call_args.kwargs)
        self.client.get_task.assert_called_once_with(cdk=PAY_CDK, task_id="activation-task-7")
        self.assertEqual([call.args for call in self.plan.call_args_list], [(AT,), (AT,)])
        account = db.get_account(7)
        self.assertEqual(account["plus_activation_status"], "succeeded")
        self.assertEqual(account["current_plan_type"], "plus")
        self.assertTrue(account["scan_request_ok"])
        self.assert_safe(result)
        self.assert_safe(payment_store.latest(7))

    def test_successful_payment_cannot_mark_free_account_plus_and_retry_only_verifies(self):
        self.plan_results = [dict(FREE)]
        self.sleep.side_effect = None
        self.activate()
        account = db.get_account(7)
        self.assertTrue(account["scan_request_ok"])
        self.assertEqual(account["current_plan_type"], "free")
        self.assertEqual(account["plus_activation_status"], "needs_attention")
        self.assertEqual(self.plan.call_count, 3)
        self.plan_results = [dict(PLUS)]
        retry = self.activate()
        self.assertEqual(retry["started_count"], 1)
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")
        self.client.submit_upi.assert_called_once()
        self.client.get_task.assert_called_once()
        self.extract.assert_called_once()

    def test_failed_plan_response_with_plus_text_is_not_verification(self):
        self.plan_results = [dict(FREE), {"ok": False, "current_plan_type": "plus", "error": "timeout"}]
        self.sleep.side_effect = None
        self.activate()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "needs_attention")
        self.assertEqual(db.get_account(7)["current_plan_type"], "free")
        self.client.submit_upi.assert_called_once()

    def test_duplicate_ids_and_repeated_click_queue_one_worker(self):
        first = self.enqueue([7, 7])
        second = self.enqueue()
        self.assertEqual(first["started_count"], 1)
        self.assertEqual(second["started_count"], 0)
        self.assertEqual(second["busy_count"], 1)
        self.assertEqual(len(self.executor.calls), 1)
        self.executor.run_all()
        self.client.submit_upi.assert_called_once()
        self.extract.assert_called_once()

    def test_concurrent_clicks_atomically_queue_once(self):
        barrier = threading.Barrier(4)
        def click(_):
            barrier.wait(timeout=5)
            return self.enqueue()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(click, range(4)))
        self.assertEqual(sum(item["started_count"] for item in results), 1)
        self.assertEqual(sum(item["busy_count"] for item in results), 3)
        self.assertEqual(len(self.executor.calls), 1)
        self.executor.run_all()
        self.client.submit_upi.assert_called_once()

    def test_extraction_failure_never_pays_and_redacts_progress(self):
        self.extraction_result = {"ok": False, "status": "failed",
                                  "error": f"failed {LINK_CDK} {PAY_CDK} {AT}"}
        self.activate()
        account = db.get_account(7)
        self.assertEqual(account["plus_activation_status"], "failed")
        self.assert_safe(store.public_view(account))
        self.assert_no_payment()

    def test_rejected_extraction_enqueue_never_pays(self):
        self.extract.side_effect = None
        self.extract.return_value = {"accepted": False, "busy": False, "error": "queue full"}
        self.activate()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "failed")
        self.assert_no_payment()

    def test_unknown_payment_is_never_automatically_resubmitted(self):
        self.client.submit_upi.side_effect = ScanApiError("submission timed out", uncertain=True)
        self.activate()
        original = payment_store.latest(7)
        self.assertEqual(original["status"], "unknown")
        self.assertEqual(db.get_account(7)["plus_activation_status"], "needs_attention")
        payment_store.finish(original, status="unknown", lease_until=0)
        for _ in range(2):
            self.activate()
        self.client.submit_upi.assert_called_once()
        self.client.get_task.assert_not_called()
        self.extract.assert_called_once()
        latest = payment_store.latest(7)
        self.assertEqual(latest["id"], original["id"])
        self.assertEqual(latest["idempotency_key"], original["idempotency_key"])
        self.assertEqual(latest["status"], "unknown")

    def test_accepted_uncertain_payment_queries_original_task_without_reposting(self):
        self.client.submit_upi.side_effect = None
        self.client.submit_upi.return_value = self.response("queued", uncertain=True, http_status=202)
        self.activate()
        self.client.submit_upi.assert_called_once()
        self.client.get_task.assert_called_once_with(cdk=PAY_CDK, task_id="activation-task-7")
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_restart_recovery_queries_original_task_and_rejects_stale_worker_updates(self):
        self.update_account(extract_link_status="success", extract_link_type="upi", extract_link_long_url=LINK)
        payments.submit_accounts(account_ids=[7], cdk=PAY_CDK)
        original = payment_store.latest(7)
        claimed, accepted = store.claim(7)
        self.assertTrue(accepted)
        old_run = claimed["plus_activation_run_id"]
        store.update(7, old_run, "paying", "waiting on original task", step="paying")
        self.assertEqual(store.recover_interrupted(), 1)
        self.assertEqual(store.recover_interrupted(), 0)
        self.assertEqual(db.get_account(7)["plus_activation_status"], "interrupted")
        self.plan_results = [dict(PLUS)]
        self.enqueue()
        self.assertNotEqual(db.get_account(7)["plus_activation_run_id"], old_run)
        self.assertFalse(store.update(7, old_run, "failed", "stale worker must not overwrite"))
        self.executor.run_all()
        self.client.submit_upi.assert_called_once()
        self.client.get_task.assert_called_once_with(cdk=PAY_CDK, task_id=original["task_id"])
        self.extract.assert_not_called()
        self.assertEqual(payment_store.latest(7)["id"], original["id"])
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_wrong_credentials_or_changed_provider_cannot_repay_unresolved_order(self):
        self.client.get_task.side_effect = ScanApiError("temporary lookup failure", status=503)
        self.activate()
        original = payment_store.latest(7)
        self.assertEqual(original["status"], "queued")
        for payment in ({"provider": "v1", "cdk": "wrong-payment-key"},
                        {"provider": "masi", "cdk": PAY_CDK},
                        {"provider": "orderhub", "cdk": "another-payment-key"}):
            with self.subTest(payment=payment):
                self.activate(payment_options=payment)
                self.assertEqual(db.get_account(7)["plus_activation_status"], "needs_attention")
                self.assertEqual(payment_store.latest(7)["id"], original["id"])
        self.client.submit_upi.assert_called_once()
        self.client.get_task.assert_called_once()
        self.extract.assert_called_once()
        self.client.get_task.side_effect = self.get_task
        self.plan_results = [dict(PLUS)]
        self.activate()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")
        self.client.submit_upi.assert_called_once()

    def test_missing_token_plus_archived_and_missing_accounts_skip_without_workers(self):
        self.update_account(access_token="")
        self.save_account({**self.account, "id": 8, "current_plan_type": "plus"})
        self.save_account({**self.account, "id": 9, "archived": True})
        result = self.enqueue([7, 8, 9, 404])
        self.assertEqual(result["skipped_count"], 4)
        self.assertEqual({item["id"] for item in result["skipped"]}, {7, 8, 9, 404})
        self.assertEqual(result["started_count"], 0)
        self.assertEqual(self.executor.calls, [])
        self.plan.assert_not_called()
        self.extract.assert_not_called()
        self.assert_no_payment()

    def test_actual_ineligible_plan_does_not_extract_or_pay_despite_cached_eligibility(self):
        self.plan_results = [{**FREE, "plus_trial_eligible": False}]
        self.activate()
        self.assertNotEqual(db.get_account(7)["plus_activation_status"], "succeeded")
        self.assertFalse(db.get_account(7)["plus_trial_eligible"])
        self.extract.assert_not_called()
        self.assert_no_payment()

    def test_actual_plus_plan_finishes_without_extraction_or_payment(self):
        self.plan_results = [dict(PLUS)]
        self.activate()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")
        self.extract.assert_not_called()
        self.assert_no_payment()

    def test_failed_precheck_never_extracts_or_pays(self):
        self.plan_results = [{"ok": False, "error": "invalid AT"}]
        self.activate()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "failed")
        self.extract.assert_not_called()
        self.assert_no_payment()

    def test_old_expired_link_is_replaced_before_only_new_link_is_paid(self):
        self.update_account(extract_link_status="success", extract_link_type="upi",
                            extract_link_long_url=LINK + "-expired", extract_link_expires_at="2000-01-01T00:00:00Z")
        self.activate()
        self.extract.assert_called_once()
        self.client.submit_upi.assert_called_once()
        self.assertEqual(self.client.submit_upi.call_args.kwargs["link"], LINK)
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_old_non_upi_link_is_replaced_before_payment(self):
        self.update_account(extract_link_status="success", extract_link_type="pix",
                            extract_link_long_url=LINK + "-pix")
        self.activate()
        self.extract.assert_called_once()
        self.assertEqual(self.client.submit_upi.call_args.kwargs["link"], LINK)
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_new_extraction_with_expired_link_never_pays(self):
        def expired(**kwargs):
            self.extract_success(**kwargs)
            self.update_account(extract_link_expires_at=946684800000)
            return {"accepted": True, "busy": False}
        self.extract.side_effect = expired
        self.activate()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "needs_attention")
        self.assert_no_payment()

    def test_new_extraction_with_non_upi_link_never_pays(self):
        def non_upi(**kwargs):
            self.extract_success(**kwargs)
            self.update_account(extract_link_type="pix")
            return {"accepted": True, "busy": False}
        self.extract.side_effect = non_upi
        self.activate()
        self.assertNotEqual(db.get_account(7)["plus_activation_status"], "succeeded")
        self.assert_no_payment()

    def test_progress_is_safe_and_snapshot_revision_changes_within_same_second(self):
        self.update_account(password="private-account-password", refresh_token="private-refresh-token",
                            plus_activation_payment_options={"cdk": PAY_CDK},
                            plus_activation_extraction_options={"cdk": LINK_CDK})
        with patch.object(db, "_now", return_value="2026-01-01T00:00:00"), \
             patch.object(store.time, "time", return_value=1767225600.0):
            result = self.enqueue()
            first = db.list_account_plan_check_statuses()
            run_id = db.get_account(7)["plus_activation_run_id"]
            store.update(7, run_id, "checking", "checking eligibility")
            second = db.list_account_plan_check_statuses()
            store.update(7, run_id, "checking", "eligibility result received")
            third = db.list_account_plan_check_statuses()
        self.assertNotEqual(first["revision"], second["revision"])
        self.assertNotEqual(second["revision"], third["revision"])
        item = third["items"][0]
        self.assertEqual(item["plus_activation_status"], "checking")
        self.assertEqual(item["plus_activation_message"], "eligibility result received")
        self.assertEqual(item["plus_activation_updated_at"], 1767225600.0)
        self.assertEqual(set(result["started"][0]), {"id", "status", "message", "updated_at"})
        for value in (result, first, second, third, store.public_view(db.get_account(7))):
            serialized = self.assert_safe(value)
            for forbidden in (run_id, "private-account-password", "private-refresh-token",
                              "plus_activation_step", "credential_hash", "plus_activation_payment_options",
                              "plus_activation_extraction_options"):
                self.assertNotIn(forbidden, serialized)

    def test_interrupted_extraction_refreshes_original_task_until_success(self):
        self.seed_checkout()
        self.sleep.side_effect = [None, AssertionError("original extraction was not refreshed")]
        statuses = iter(("running", "success"))
        def refresh(account_id, action, *, cdk):
            status = next(statuses)
            db.update_account_extract(account_id, {
                "status": status, "ok": status == "success", "link_type": "upi",
                "task_id": "original-extract-task", "result": {"long_url": LINK},
            })
        self.refresh.side_effect = refresh
        self.activate()
        self.assertEqual(self.refresh.call_count, 2)
        for call in self.refresh.call_args_list:
            self.assertEqual(call.args, (7, "refresh"))
            self.assertEqual(call.kwargs, {"cdk": LINK_CDK})
        self.assertEqual(db.get_account(7)["extract_link_task_id"], "original-extract-task")
        self.extract.assert_not_called()
        self.client.submit_upi.assert_called_once()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_manual_valid_upi_is_replaced_by_zero_amount_activation_checkout(self):
        self.assertTrue(db.claim_account_extract(7, trigger="manual", link_type="upi",
                                                provider_id=0, provider_type="lumen"))
        manual = self.successful_extraction_result()
        manual["result"]["long_url"] = LINK + "-manual-nonzero"
        db.update_account_extract(7, manual)
        self.activate()
        self.extract.assert_called_once()
        kwargs = self.extract.call_args.kwargs
        self.assertEqual(kwargs["trigger"], "plus_activation")
        self.assertEqual(kwargs["payment_amount"], 0)
        self.assertEqual(self.client.submit_upi.call_args.kwargs["link"], LINK)
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_matching_activation_checkout_is_reused_without_new_extraction(self):
        original = self.seed_checkout()
        db.update_account_extract(7, self.successful_extraction_result())
        self.activate()
        self.extract.assert_not_called()
        self.assertEqual(db.get_account(7)["plus_activation_checkout_key"], original["plus_activation_checkout_key"])
        self.client.submit_upi.assert_called_once()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_changed_extraction_credential_replaces_successful_checkout(self):
        original = self.seed_checkout()
        previous = self.successful_extraction_result()
        previous["result"]["long_url"] = LINK + "-old-credential"
        db.update_account_extract(7, previous)
        self.activate(extraction_options={"provider_id": 0, "cdk": "new-extraction-code"})
        self.extract.assert_called_once()
        self.assertEqual(self.extract.call_args.kwargs["cdk"], "new-extraction-code")
        self.assertNotEqual(db.get_account(7)["plus_activation_checkout_key"], original["plus_activation_checkout_key"])
        self.assertEqual(self.client.submit_upi.call_args.kwargs["link"], LINK)
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_changed_extraction_credential_cannot_resume_unresolved_checkout(self):
        self.seed_checkout()
        self.activate(extraction_options={"provider_id": 0, "cdk": "wrong-extraction-code"})
        self.extract.assert_not_called()
        self.refresh.assert_not_called()
        self.assert_no_payment()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "needs_attention")

    def assert_confirmed_failure_retries(self, status, provider_type):
        self.seed_checkout(status=status, provider_type=provider_type)
        def confirm(account_id, action, *, cdk):
            self.events.append("confirm:" + status)
            db.update_account_extract(account_id, {"ok": False, "status": status,
                                                   "task_id": "original-extract-task"})
        self.refresh.side_effect = confirm
        self.activate()
        self.refresh.assert_called_once_with(7, "refresh", cdk=LINK_CDK)
        self.extract.assert_called_once()
        self.assertEqual(self.events, ["plan:free", "confirm:" + status, "extract", "submit", "query", "plan:plus"])
        self.assertEqual(db.get_account(7)["extract_link_task_id"], "new-extract-task")
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")
        self.client.submit_upi.assert_called_once()

    def test_failed_lumen_task_retries_only_after_remote_terminal_confirmation(self):
        self.assert_confirmed_failure_retries("failed", "lumen")

    def test_stopped_upi_task_retries_only_after_remote_terminal_confirmation(self):
        self.assert_confirmed_failure_retries("stopped", "upi_git5")

    def test_locally_failed_but_remote_running_task_is_polled_without_new_extraction(self):
        self.seed_checkout(status="failed")
        statuses = iter(("running", "success"))
        self.sleep.side_effect = [None, AssertionError("remote running task was not refreshed")]
        def refresh(account_id, action, *, cdk):
            status = next(statuses)
            db.update_account_extract(account_id, {"ok": status == "success", "status": status,
                                                   "result": {"long_url": LINK}})
        self.refresh.side_effect = refresh
        self.activate()
        self.assertEqual(self.refresh.call_count, 2)
        self.extract.assert_not_called()
        self.assertEqual(db.get_account(7)["extract_link_task_id"], "original-extract-task")
        self.client.submit_upi.assert_called_once()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_failed_task_with_unavailable_remote_confirmation_is_not_reextracted(self):
        self.seed_checkout(status="failed")
        self.refresh.side_effect = RuntimeError("original platform unavailable")
        self.activate()
        self.refresh.assert_called_once_with(7, "refresh", cdk=LINK_CDK)
        self.extract.assert_not_called()
        self.assert_no_payment()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "needs_attention")

    def test_new_extract_claim_clears_previous_payment_and_links_for_every_provider(self):
        for provider in ("extract", "lumen", "upi_git5"):
            with self.subTest(provider=provider):
                self.save_account({**self.account, "extract_link_status": "success",
                    "extract_link_provider_type": provider, "extract_link_payment_status": "paid",
                    "extract_link_long_url": LINK + "-previous", "extract_link_copy_paste": LINK + "-copy",
                    "extract_link_task_id": "old-task", "extract_link_job_id": "old-job",
                    "extract_link_expires_at": "2000-01-01T00:00:00Z",
                    "extract_link_result_json": '{"payment_status":"paid"}',
                })
                self.assertTrue(db.claim_account_extract(7, trigger="plus_activation", link_type="upi",
                                                        provider_id=0, provider_type=provider))
                claimed = db.get_account(7)
                for key in ("payment_status", "long_url", "copy_paste", "task_id", "job_id", "expires_at", "result_json"):
                    self.assertFalse(claimed.get("extract_link_" + key), key)
                db.update_account_extract(7, self.successful_extraction_result())
                current = db.get_account(7)
                self.assertIsNone(current.get("extract_link_payment_status"))
                self.assertEqual(current["extract_link_long_url"], LINK)

    def test_old_lumen_paid_flag_cannot_skip_payment_for_new_checkout(self):
        self.provider.return_value["provider_type"] = "lumen"
        self.update_account(extract_link_status="failed", extract_link_provider_type="lumen",
                            extract_link_payment_status="paid", extract_link_long_url=LINK + "-old")
        self.activate()
        self.extract.assert_called_once()
        self.client.submit_upi.assert_called_once()
        self.assertEqual(self.client.submit_upi.call_args.kwargs["link"], LINK)
        self.assertIsNone(db.get_account(7).get("extract_link_payment_status"))
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_restarted_manually_refreshed_running_task_keeps_polling_original_task(self):
        original = self.seed_checkout()
        store.update(7, original["plus_activation_run_id"], "extracting", "worker interrupted", step="extracting")
        self.assertEqual(store.recover_interrupted(), 1)
        db.recover_interrupted_extract_links()
        # Simulate a user's task refresh after restart, with no extraction worker left.
        db.update_account_extract(7, {"status": "running", "task_id": "original-extract-task"})
        def refresh(account_id, action, *, cdk):
            db.update_account_extract(account_id, {"ok": True, "status": "success",
                                                   "result": {"long_url": LINK}})
        self.refresh.side_effect = refresh
        self.activate()
        self.refresh.assert_called_once_with(7, "refresh", cdk=LINK_CDK)
        self.extract.assert_not_called()
        self.assertEqual(db.get_account(7)["extract_link_task_id"], "original-extract-task")
        self.client.submit_upi.assert_called_once()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_plus_plan_with_interrupted_verification_resumes_to_succeeded(self):
        claimed, accepted = store.claim(7)
        self.assertTrue(accepted)
        store.update(7, claimed["plus_activation_run_id"], "verifying", "verification interrupted", step="verifying")
        self.update_account(current_plan_type="plus")
        self.assertEqual(store.recover_interrupted(), 1)
        self.plan_results = [dict(PLUS)]
        result = self.activate()
        self.assertEqual(result["started_count"], 1)
        self.plan.assert_called_once_with(AT)
        self.extract.assert_not_called()
        self.assert_no_payment()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_extraction_provider_already_paid_only_verifies_even_when_link_expired(self):
        self.update_account(extract_link_status="success", extract_link_type="upi",
                            extract_link_long_url=LINK, extract_link_expires_at="2000-01-01T00:00:00Z",
                            extract_link_payment_status="paid")
        self.plan_results = [dict(PLUS)]
        self.activate()
        self.plan.assert_called_once_with(AT)
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")
        self.extract.assert_not_called()
        self.assert_no_payment()

    def test_unknown_extraction_provider_payment_cannot_switch_to_selected_payment(self):
        self.update_account(extract_link_status="success", extract_link_type="upi",
                            extract_link_long_url=LINK, extract_link_payment_status="unknown")
        self.activate()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "needs_attention")
        self.extract.assert_not_called()
        self.assert_no_payment()

    def test_selected_extraction_api_base_is_used_without_environment_fallback(self):
        from unittest.mock import MagicMock
        base = "https://selected-provider.example.test"
        session = Mock()
        session.post.return_value = Mock(status_code=201)
        session.post.return_value.json.return_value = {"job_id": "selected-job"}
        cdk_response = Mock(status_code=200)
        cdk_response.json.return_value = {"remaining": 1}
        events_response = MagicMock(status_code=200)
        events_response.__enter__.return_value = events_response
        events_response.__iter__.return_value = iter([
            b"event: complete\n", b'data: {"status":"success"}\n', b"\n",
        ])
        session.get.side_effect = [cdk_response, events_response]
        with patch.object(service.extraction, "_session", return_value=session), \
             patch.object(service.extraction, "_api_base", side_effect=AssertionError("must use selected provider")):
            job = service.extraction._legacy_create_job(token=AT, link_type="upi", cdk=LINK_CDK, api_base=base)
            balance = service.extraction._legacy_query_cdk(LINK_CDK, api_base=base)
            events = list(service.extraction._iter_sse_events(job_id="selected-job", cdk=LINK_CDK, api_base=base))
        self.assertEqual(job, {"job_id": "selected-job"})
        self.assertEqual(balance, {"remaining": 1})
        self.assertEqual(events, [("complete", {"status": "success"})])
        self.assertEqual(session.post.call_args.args[0], base + "/api/extract")
        self.assertEqual(session.post.call_args.kwargs["json"], {"token": AT, "link_type": "upi", "cdk": LINK_CDK})
        self.assertEqual(session.get.call_args_list[0].args[0], base + "/api/cdk?code=" + LINK_CDK)
        self.assertEqual(session.get.call_args_list[1].args[0], base + "/api/jobs/selected-job/events?cdk=" + LINK_CDK)

    def test_invalid_account_ids_rejected_before_any_worker_or_payment(self):
        for ids in ([], [True], [7.0], ["7"], [0], [-1], None, list(range(1, 502))):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                service.enqueue_accounts(account_ids=ids,
                    extraction_options={"provider_id": 0, "cdk": LINK_CDK},
                    payment_options={"provider": "v1", "cdk": PAY_CDK})
        self.assertEqual(self.executor.calls, [])
        self.assert_no_payment()


class PlusActivationGroupTests(ActivationFixture):
    def setUp(self):
        super().setUp()
        self.update_account(group_name="待开通")
        db.update_account_group_meta("Plus 成品", redeem_prefix="PLUS", public_stock=True)

    def test_moves_only_after_real_plus_verification_and_updates_group_filters(self):
        def check(token):
            self.assertEqual(db.get_account(7)["group_name"], "待开通")
            return self.check_plan(token)
        self.plan.side_effect = check
        self.enqueue(success_group=" Plus 成品 ")
        queued = db.get_account(7)
        self.assertEqual(queued["group_name"], "待开通")
        self.assertEqual(queued["plus_activation_success_group"], "Plus 成品")
        self.executor.run_all()
        account = db.get_account(7)
        self.assertEqual(account["group_name"], "Plus 成品")
        self.assertEqual(account["plus_activation_status"], "succeeded")
        self.assertIn("已转入分组「Plus 成品」", account["plus_activation_message"])
        self.assertEqual(db.list_account_plan_check_statuses(group_filter="待开通")["total"], 0)
        snapshot = db.list_account_plan_check_statuses(group_filter="Plus 成品")
        self.assertEqual(snapshot["items"][0]["group_name"], "Plus 成品")
        group = next(g for g in db.list_account_groups() if g["group_name"] == "Plus 成品")
        self.assertEqual(group["total"], 1)
        self.assertEqual(group["redeem_prefix"], "PLUS")
        self.assertTrue(group["public_stock"])

    def test_no_target_keeps_original_group(self):
        self.activate(success_group="")
        account = db.get_account(7)
        self.assertEqual(account["plus_activation_status"], "succeeded")
        self.assertEqual(account["group_name"], "待开通")
        self.assertNotIn("已转入", account["plus_activation_message"])

    def test_paid_but_still_free_keeps_group_then_resume_moves_without_repayment(self):
        self.plan_results = [dict(FREE)]
        self.sleep.side_effect = None
        self.activate(success_group="Plus 成品")
        account = db.get_account(7)
        self.assertTrue(account["scan_request_ok"])
        self.assertEqual(account["plus_activation_status"], "needs_attention")
        self.assertEqual(account["group_name"], "待开通")
        self.plan_results = [dict(PLUS)]
        self.activate()
        self.assertEqual(db.get_account(7)["group_name"], "Plus 成品")
        self.client.submit_upi.assert_called_once()
        self.extract.assert_called_once()

    def test_failed_plan_response_does_not_move_even_when_it_mentions_plus(self):
        self.plan_results = [dict(FREE), {**PLUS, "ok": False}]
        self.sleep.side_effect = None
        self.activate(success_group="Plus 成品")
        self.assertEqual(db.get_account(7)["group_name"], "待开通")
        self.assertEqual(db.get_account(7)["plus_activation_status"], "needs_attention")

    def test_ineligible_account_keeps_group_without_payment(self):
        self.plan_results = [{**FREE, "plus_trial_eligible": False}]
        self.activate(success_group="Plus 成品")
        self.assertEqual(db.get_account(7)["group_name"], "待开通")
        self.assertEqual(db.get_account(7)["plus_activation_status"], "failed")
        self.assert_no_payment()

    def test_actual_plus_precheck_moves_without_payment(self):
        self.plan_results = [dict(PLUS)]
        self.activate(success_group="Plus 成品")
        self.assertEqual(db.get_account(7)["group_name"], "Plus 成品")
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")
        self.extract.assert_not_called()
        self.assert_no_payment()

    def test_busy_click_cannot_replace_target_group(self):
        self.enqueue(success_group="Plus 成品")
        self.assertEqual(self.enqueue(success_group="")["busy_count"], 1)
        self.assertEqual(db.get_account(7)["plus_activation_success_group"], "Plus 成品")
        self.executor.run_all()
        self.assertEqual(db.get_account(7)["group_name"], "Plus 成品")

    def test_restart_preserves_target_and_stale_completion_cannot_move(self):
        original, _ = store.claim(7, success_group="Plus 成品")
        old_run = original["plus_activation_run_id"]
        store.update(7, old_run, "verifying", "waiting for Plus", step="verifying")
        self.assertEqual(store.recover_interrupted(), 1)
        self.plan_results = [dict(PLUS)]
        self.enqueue()
        self.assertFalse(store.complete(7, old_run, "stale completion"))
        self.assertEqual(db.get_account(7)["group_name"], "待开通")
        self.executor.run_all()
        self.assertEqual(db.get_account(7)["group_name"], "Plus 成品")
        self.assert_no_payment()

    def test_resume_can_explicitly_disable_transfer(self):
        original, _ = store.claim(7, success_group="Plus 成品")
        store.update(7, original["plus_activation_run_id"], "verifying", "waiting", step="verifying")
        store.recover_interrupted()
        self.plan_results = [dict(PLUS)]
        self.activate(success_group="")
        account = db.get_account(7)
        self.assertEqual(account["group_name"], "待开通")
        self.assertEqual(account["plus_activation_status"], "succeeded")
        self.assert_no_payment()

    def test_deleted_target_keeps_group_and_allows_retry_with_another_target(self):
        self.enqueue(success_group="Plus 成品")
        db.delete_account_group("Plus 成品")
        self.executor.run_all()
        account = db.get_account(7)
        self.assertEqual(account["group_name"], "待开通")
        self.assertEqual(account["current_plan_type"], "plus")
        self.assertEqual(account["plus_activation_status"], "needs_attention")
        self.assertEqual(account["plus_activation_step"], "verifying")
        self.assertIn("目标分组已不存在", account["plus_activation_message"])
        self.assertNotIn("Plus 成品", [g["group_name"] for g in db.list_account_groups()])
        self.activate(success_group=db.DEFAULT_ACCOUNT_GROUP)
        self.assertEqual(db.get_account(7)["group_name"], db.DEFAULT_ACCOUNT_GROUP)
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")
        self.client.submit_upi.assert_called_once()
        self.extract.assert_called_once()

    def test_group_move_and_success_roll_back_together_on_write_failure(self):
        self.enqueue(success_group="Plus 成品")
        write = store._write
        def fail_success(conn, account_id, account, **changes):
            write(conn, account_id, account, **changes)
            if changes.get("status") == "succeeded":
                raise RuntimeError("simulated commit failure")
        with patch.object(store, "_write", side_effect=fail_success):
            self.executor.run_all()
        self.assertEqual(db.get_account(7)["group_name"], "待开通")
        self.assertEqual(db.get_account(7)["plus_activation_status"], "needs_attention")
        self.activate()
        self.assertEqual(db.get_account(7)["group_name"], "Plus 成品")
        self.client.submit_upi.assert_called_once()

    def test_archived_during_final_plan_check_is_not_moved(self):
        def check(token):
            result = self.check_plan(token)
            if result["current_plan_type"] == "plus":
                db.archive_account(7)
            return result
        self.plan.side_effect = check
        self.activate(success_group="Plus 成品")
        self.assertEqual(db.get_account(7)["group_name"], "待开通")
        self.assertEqual(db.get_account(7)["plus_activation_status"], "needs_attention")
        self.assertIn("已归档", db.get_account(7)["plus_activation_message"])

    def test_batch_moves_successful_accounts_and_leaves_skipped_accounts(self):
        self.save_account({**self.account, "id": 8, "email": "eight@example.test", "group_name": "待开通"})
        self.save_account({**self.account, "id": 9, "email": "nine@example.test", "group_name": "待开通", "access_token": ""})
        self.plan_results = [dict(PLUS)]
        result = self.activate(account_ids=[7, 8, 9], success_group="Plus 成品")
        self.assertEqual(result["started_count"], 2)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual([db.get_account(i)["group_name"] for i in (7, 8, 9)], ["Plus 成品", "Plus 成品", "待开通"])
        self.assert_no_payment()

    def test_legacy_group_without_metadata_is_a_valid_target(self):
        self.save_account({**self.account, "id": 8, "group_name": "旧分组"})
        self.plan_results = [dict(PLUS)]
        self.activate(success_group="旧分组")
        self.assertEqual(db.get_account(7)["group_name"], "旧分组")

    def test_route_accepts_target_and_rejects_invalid_names_before_scheduling(self):
        client = self.make_web_client()
        for target in (False, 1, [], {}, " ", "x" * 61, "Plus\n成品", "Plus\u200b成品", "不存在"):
            with self.subTest(target=target):
                response = client.post(ENDPOINT, json={**self.request_body(), "success_group": target},
                                       headers={"X-Auth-Code": "local-test"})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json["ok"])
        self.assertEqual(self.executor.calls, [])
        self.provider.assert_not_called()
        self.assert_no_payment()
        response = client.post(ENDPOINT, json={**self.request_body(), "success_group": "Plus 成品"},
                               headers={"X-Auth-Code": "local-test"})
        self.assertEqual(response.status_code, 202)
        self.executor.run_all()
        self.assertEqual(db.get_account(7)["group_name"], "Plus 成品")


class PlusActivationRouteTests(ActivationFixture):
    def test_route_requires_local_auth_then_returns_safe_202_counts(self):
        client = self.make_web_client()
        body = self.request_body()
        self.assertEqual(client.post(ENDPOINT, json=body).status_code, 401)
        self.assertEqual(self.executor.calls, [])
        response = client.post(ENDPOINT, json=body, headers={"X-Auth-Code": "local-test"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["started_count"], 1)
        for key in ("started", "busy", "skipped", "failed"):
            self.assertEqual(response.json[key + "_count"], len(response.json[key]))
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assert_safe(response.json)
        self.executor.run_all()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_malformed_json_and_non_object_bodies_return_400_without_scheduling(self):
        client = self.make_web_client()
        for body in ("{", "null", "[]", '"string"', "7", "true", ""):
            with self.subTest(body=body):
                response = client.post(ENDPOINT, data=body, content_type="application/json",
                                       headers={"X-Auth-Code": "local-test"})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json["ok"])
        self.assertEqual(self.executor.calls, [])
        self.assert_no_payment()

    def test_invalid_nested_options_return_400_without_payment(self):
        client = self.make_web_client()
        for field, invalid in (("payment", []), ("payment", None), ("extraction", "wrong"),
                               ("extraction", None), ("payment", {"auth_mode": "invalid"})):
            with self.subTest(field=field, invalid=invalid):
                body = self.request_body()
                body[field] = invalid
                response = client.post(ENDPOINT, json=body, headers={"X-Auth-Code": "local-test"})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.executor.calls, [])
        self.assert_no_payment()

    def test_nested_payment_session_auth_uses_opaque_handle_and_original_identity(self):
        client = self.make_web_client()
        auth = {"identity": "orderhub-user:42", "api_base": self.client.api_base,
                "user": {"username": "00123456"}, "cookies": "private-upstream-cookie"}
        with client.session_transaction() as browser_session:
            browser_session["orderhub_session_handle"] = "opaque-payment-handle"
        body = self.request_body()
        body["payment"] = {"provider": "orderhub", "auth_mode": "session"}
        body["auth_mode"] = "key"
        with patch("webui.scan_routes.orderhub_client.get_session", return_value=auth) as get_session:
            response = client.post(ENDPOINT, json=body, headers={"X-Auth-Code": "local-test"})
            self.assertEqual(response.status_code, 202)
            get_session.assert_called_once_with("opaque-payment-handle")
            self.executor.run_all()
        self.assertEqual(self.client.session_auth, auth)
        kwargs = self.client.submit_upi.call_args.kwargs
        self.assertEqual(kwargs["cdk"], auth["identity"])
        self.assertEqual(kwargs["access_token"], AT)
        self.client.get_task.assert_called_once_with(cdk=auth["identity"], task_id="activation-task-7")
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")
        self.assert_safe(response.json)
        self.assert_safe(payment_store.latest(7))
        self.assert_safe(db.list_account_plan_check_statuses())
        self.assertNotIn("opaque-payment-handle", response.text)

    def test_expired_nested_payment_session_cannot_fall_back_to_key_payment(self):
        client = self.make_web_client()
        body = self.request_body()
        body["payment"].update(provider="orderhub", auth_mode="session")
        with patch("webui.scan_routes.orderhub_client.get_session", side_effect=ValueError("session expired")):
            response = client.post(ENDPOINT, json=body, headers={"X-Auth-Code": "local-test"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.executor.calls, [])
        self.assert_no_payment()


class PlusActivationFallbackTests(ActivationFixture):
    @staticmethod
    def extracts():
        return [{"provider_id": 0, "cdk": LINK_CDK},
                {"provider_id": 1, "cdk": LINK_CDK + "-backup"}]

    @staticmethod
    def payments():
        return [{"provider": "v1", "cdk": PAY_CDK},
                {"provider": "masi", "cdk": PAY_CDK + "-backup"},
                {"provider": "orderhub", "cdk": PAY_CDK + "-third"}]

    def test_extraction_failure_tries_next_and_only_pays_successful_link(self):
        def extract(**kwargs):
            self.extraction_result = ({"status": "failed", "error": "quota exhausted"}
                                     if kwargs["provider_id"] == 0 else self.successful_extraction_result())
            return self.extract_success(**kwargs)
        self.extract.side_effect = extract
        self.activate(extraction_options=self.extracts())
        self.assertEqual([c.kwargs["provider_id"] for c in self.extract.call_args_list], [0, 1])
        self.assertEqual(self.client.submit_upi.call_args.kwargs["link"], LINK)
        self.client.submit_upi.assert_called_once()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_extraction_exhaustion_does_not_pay(self):
        self.extraction_result = {"status": "failed", "error": "no quota"}
        self.activate(extraction_options=self.extracts())
        self.assertEqual(self.extract.call_count, 2)
        self.assertEqual(db.get_account(7)["plus_activation_status"], "failed")
        self.assertIn("候选已用尽", db.get_account(7)["plus_activation_message"])
        self.assert_no_payment()

    def test_exhausted_extraction_candidates_are_not_resubmitted_on_resume(self):
        self.provider.return_value["provider_type"] = "lumen"
        self.extraction_result = {"status": "failed", "task_id": "failed-extract-task"}
        self.refresh.side_effect = lambda account_id, *args, **kwargs: db.update_account_extract(account_id, {"status": "failed"})
        self.plan_results = [dict(FREE)]
        self.activate(extraction_options=self.extracts())
        failed_key = db.get_account(7)["plus_activation_failed_checkout_key"]
        self.assertEqual([c.kwargs["provider_id"] for c in self.extract.call_args_list], [0, 1])
        self.activate(extraction_options=self.extracts())
        self.assertEqual([c.kwargs["provider_id"] for c in self.extract.call_args_list], [0, 1])
        self.assertEqual(db.get_account(7)["plus_activation_failed_checkout_key"], failed_key)
        self.assertEqual(db.get_account(7)["plus_activation_status"], "failed")
        self.assertIn("候选已用尽", db.get_account(7)["plus_activation_message"])
        self.assert_no_payment()
        self.assertNotIn(failed_key, json.dumps(store.public_view(db.get_account(7))))

    def test_interrupted_extraction_confirmed_failed_advances_without_recreating_it(self):
        self.provider.return_value["provider_type"] = "lumen"
        self.extraction_result = {"status": "interrupted", "task_id": "original-task"}
        self.activate(extraction_options=self.extracts())
        self.refresh.side_effect = lambda account_id, *args, **kwargs: db.update_account_extract(account_id, {"status": "failed"})
        self.plan_results = [dict(FREE), dict(PLUS)]
        self.extraction_result = self.successful_extraction_result()
        self.activate(extraction_options=self.extracts())
        self.assertEqual([c.kwargs["provider_id"] for c in self.extract.call_args_list], [0, 1])
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")
        self.client.submit_upi.assert_called_once()

    def test_rejected_enqueue_falls_back_without_releasing_activation_claim(self):
        def extract(**kwargs):
            if kwargs["provider_id"] == 0:
                return {"accepted": False, "busy": False, "error": "queue unavailable"}
            self.assertEqual(self.enqueue()["busy_count"], 1)
            return self.extract_success(**kwargs)
        self.extract.side_effect = extract
        self.activate(extraction_options=self.extracts())
        self.assertEqual(self.extract.call_count, 2)
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_uncertain_extraction_does_not_switch_candidates(self):
        for status in ("unknown", "interrupted", "awaiting_blik"):
            with self.subTest(status=status):
                self.save_account(self.account)
                self.extract.reset_mock()
                self.plan_results = [dict(FREE)]
                self.extraction_result = {"status": status}
                self.activate(extraction_options=self.extracts())
                self.extract.assert_called_once()
                self.assertEqual(db.get_account(7)["plus_activation_status"], "needs_attention")
                self.assert_no_payment()

    def test_remote_failed_extraction_is_confirmed_before_switch(self):
        self.provider.return_value["provider_type"] = "lumen"
        def extract(**kwargs):
            self.extraction_result = ({"status": "failed", "task_id": "original-task"}
                                     if kwargs["provider_id"] == 0 else self.successful_extraction_result())
            return self.extract_success(**kwargs)
        self.extract.side_effect = extract
        self.refresh.side_effect = lambda account_id, *args, **kwargs: db.update_account_extract(account_id, {"status": "failed"})
        self.activate(extraction_options=self.extracts())
        self.refresh.assert_called_once_with(7, "refresh", cdk=LINK_CDK)
        self.assertEqual(self.extract.call_count, 2)
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_remote_running_extraction_blocks_fallback_after_local_failure(self):
        self.provider.return_value["provider_type"] = "lumen"
        self.extraction_result = {"status": "failed", "task_id": "original-task"}
        self.refresh.side_effect = lambda account_id, *args, **kwargs: db.update_account_extract(account_id, {"status": "running"})
        self.activate(extraction_options=self.extracts())
        self.refresh.assert_called_once()
        self.extract.assert_called_once()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "needs_attention")
        self.assert_no_payment()

    def test_payment_rejections_advance_in_order_without_reextracting(self):
        def submit(**kwargs):
            if kwargs["cdk"] != PAY_CDK + "-third":
                raise ScanApiError("quota exhausted", status=402, created=False, charged=False)
            return self.response("completed", task_id="third-provider-task")
        self.client.submit_upi.side_effect = submit
        self.activate(payment_options=self.payments())
        self.assertEqual([c.kwargs["cdk"] for c in self.client.submit_upi.call_args_list],
                         [p["cdk"] for p in self.payments()])
        keys = [c.kwargs["idempotency_key"] for c in self.client.submit_upi.call_args_list]
        self.assertEqual(len(set(keys)), 3)
        self.extract.assert_called_once()
        self.assertEqual(payment_store.latest(7)["provider"], "orderhub")
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")
        self.assert_safe(payment_store.latest(7))

    def test_same_payment_provider_with_different_cdks_can_fall_back(self):
        options = [{"provider": "v1", "cdk": PAY_CDK}, {"provider": "v1", "cdk": PAY_CDK + "-backup"}]
        self.client.submit_upi.side_effect = [ScanApiError("invalid CDK", status=401), self.response("completed")]
        self.activate(payment_options=options)
        self.assertEqual([c.kwargs["cdk"] for c in self.client.submit_upi.call_args_list], [p["cdk"] for p in options])
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_accepted_payment_failure_falls_back_after_query(self):
        self.client.submit_upi.side_effect = lambda **kwargs: self.response("queued", "task-" + str(self.client.submit_upi.call_count))
        self.client.get_task.side_effect = [self.response("failed", "task-1"), self.response("succeeded", "task-2")]
        self.activate(payment_options=self.payments())
        self.assertEqual(self.client.submit_upi.call_count, 2)
        self.assertEqual(self.client.get_task.call_count, 2)
        self.assertEqual(payment_store.latest(7)["provider"], "masi")
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_unknown_submission_never_falls_back(self):
        self.client.submit_upi.side_effect = ScanApiError("timeout", uncertain=True)
        self.activate(payment_options=self.payments())
        self.client.submit_upi.assert_called_once()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "needs_attention")
        self.assertEqual(payment_store.latest(7)["status"], "unknown")

    def test_query_failure_never_falls_back(self):
        self.client.get_task.side_effect = ScanApiError("lookup unavailable", status=503)
        self.activate(payment_options=self.payments())
        self.client.submit_upi.assert_called_once()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "needs_attention")
        self.assertEqual(payment_store.latest(7)["status"], "queued")

    def test_all_payment_failures_stop_after_each_candidate_once(self):
        self.client.submit_upi.side_effect = lambda **kwargs: self.response("failed", "task-" + str(self.client.submit_upi.call_count))
        self.activate(payment_options=self.payments())
        self.assertEqual(self.client.submit_upi.call_count, 3)
        self.assertEqual(db.get_account(7)["plus_activation_status"], "failed")
        self.assertIn("候选已用尽", db.get_account(7)["plus_activation_message"])
        self.client.get_task.assert_not_called()
        self.extract.assert_called_once()
        self.plan_results = [dict(FREE)]
        self.activate(payment_options=self.payments())
        self.assertEqual(self.client.submit_upi.call_count, 3)
        self.extract.assert_called_once()

    def test_paid_but_unverified_does_not_try_other_payments(self):
        self.plan_results = [dict(FREE)]
        self.sleep.side_effect = None
        self.activate(payment_options=self.payments())
        self.client.submit_upi.assert_called_once()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "needs_attention")

    def test_duplicate_candidates_are_only_attempted_once(self):
        self.extraction_result = {"status": "failed"}
        extract = self.extracts()[0]
        self.activate(extraction_options=[extract, dict(extract)])
        self.extract.assert_called_once()
        self.save_account(self.account)
        self.extract.reset_mock()
        self.extraction_result = self.successful_extraction_result()
        self.plan_results = [dict(FREE)]
        self.client.submit_upi.side_effect = ScanApiError("invalid CDK", status=401)
        payment = self.payments()[0]
        self.activate(payment_options=[payment, dict(payment)])
        self.client.submit_upi.assert_called_once()

    def test_resume_matches_original_payment_in_middle_of_list(self):
        self.client.submit_upi.side_effect = [ScanApiError("no quota", status=402), self.response("queued", "backup-task")]
        self.client.get_task.side_effect = ScanApiError("lookup unavailable", status=503)
        self.activate(payment_options=self.payments())
        self.assertEqual(payment_store.latest(7)["provider"], "masi")
        self.client.get_task.side_effect = lambda **kwargs: self.response("succeeded", "backup-task")
        self.plan_results = [dict(PLUS)]
        self.activate(payment_options=self.payments())
        self.assertEqual(self.client.submit_upi.call_count, 2)
        self.assertEqual(self.client.get_task.call_args.kwargs["cdk"], PAY_CDK + "-backup")
        self.extract.assert_called_once()
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_resume_matches_original_extract_in_middle_of_list(self):
        self.provider.return_value["provider_type"] = "lumen"
        def extract(**kwargs):
            self.extraction_result = ({"status": "failed"} if kwargs["provider_id"] == 0
                                     else {"status": "interrupted", "task_id": "backup-extract-task"})
            return self.extract_success(**kwargs)
        self.extract.side_effect = extract
        self.activate(extraction_options=self.extracts())
        self.assertEqual(db.get_account(7)["extract_link_provider_id"], 1)
        self.refresh.side_effect = lambda account_id, *args, **kwargs: db.update_account_extract(account_id, self.successful_extraction_result())
        self.plan_results = [dict(FREE), dict(PLUS)]
        self.activate(extraction_options=self.extracts())
        self.assertEqual(self.extract.call_count, 2)
        self.refresh.assert_called_once_with(7, "refresh", cdk=LINK_CDK + "-backup")
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")

    def test_invalid_fallback_lists_do_not_schedule_any_account(self):
        client = self.make_web_client()
        for field in ("extraction", "payment"):
            for invalid in ([], [None], [False], [[]], ["wrong"], [{}] * 21):
                with self.subTest(field=field, invalid=invalid):
                    body = self.request_body()
                    body[field] = invalid
                    response = client.post(ENDPOINT, json=body, headers={"X-Auth-Code": "local-test"})
                    self.assertEqual(response.status_code, 400)
        self.assertEqual(self.executor.calls, [])
        self.assert_no_payment()

    def test_route_accepts_mixed_key_and_orderhub_session_candidates(self):
        client = self.make_web_client()
        auth = {"identity": "orderhub-user:42", "api_base": self.client.api_base,
                "user": {"username": "00123456"}, "cookies": "private-upstream-cookie"}
        body = self.request_body()
        body["extraction"] = self.extracts()
        body["payment"] = [self.payments()[0], {"provider": "orderhub", "auth_mode": "session"}]
        self.client.submit_upi.side_effect = [ScanApiError("no quota", status=402), self.response("completed")]
        with patch("webui.scan_routes.orderhub_client.get_session", return_value=auth):
            response = client.post(ENDPOINT, json=body, headers={"X-Auth-Code": "local-test"})
            self.assertEqual(response.status_code, 202)
            self.executor.run_all()
        self.assertEqual([c.kwargs["cdk"] for c in self.client.submit_upi.call_args_list], [PAY_CDK, auth["identity"]])
        self.assertEqual(db.get_account(7)["plus_activation_status"], "succeeded")
        self.assert_safe(response.json)
        self.assert_safe(payment_store.latest(7))

    def test_verifying_or_charged_failure_does_not_switch_payment(self):
        for kwargs in ({"created": True, "charged": True}, {"uncertain": True}):
            with self.subTest(kwargs=kwargs):
                self.save_account(self.account)
                # Each subcase uses another isolated account to retain payment history.
                account_id = 8 if kwargs.get("charged") else 9
                self.save_account({**self.account, "id": account_id, "email": f"{account_id}@example.test"})
                self.client.submit_upi.reset_mock()
                self.plan_results = [dict(FREE)]
                self.client.submit_upi.side_effect = ScanApiError("submission failed", status=400, **kwargs)
                self.activate(account_ids=[account_id], payment_options=self.payments())
                self.client.submit_upi.assert_called_once()
                self.assertEqual(db.get_account(account_id)["plus_activation_status"], "needs_attention")

    def test_later_invalid_candidate_rejects_whole_batch_before_scheduling(self):
        for field, value in (("extraction_options", [self.extracts()[0], {"provider_id": 1, "cdk": ""}]),
                             ("payment_options", [self.payments()[0], {"provider": "v1", "cdk": ""}])):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.enqueue(**{field: value})
        self.assertEqual(self.executor.calls, [])
        self.extract.assert_not_called()
        self.assert_no_payment()

    def test_final_error_redacts_credentials_from_all_candidates(self):
        credentials = [p["cdk"] for p in self.payments()] + [p["cdk"] for p in self.extracts()]
        self.client.submit_upi.side_effect = ScanApiError("rejected " + " ".join(credentials), status=402)
        self.activate(extraction_options=self.extracts(), payment_options=self.payments())
        view = json.dumps(store.public_view(db.get_account(7)))
        for credential in credentials:
            self.assertNotIn(credential, view)


class LegacyExtractionFailureTests(ActivationFixture):
    def run_legacy(self, *, events=(), error=None):
        self.assertTrue(db.claim_account_extract(7, trigger="plus_activation", link_type="upi", provider_id=0, provider_type="extract"))
        with patch.object(service.extraction, "_QUEUE_SLOTS", Mock()), \
             patch.object(service.extraction, "_legacy_create_job", return_value={"job_id": "legacy-task"}, side_effect=error), \
             patch.object(service.extraction, "_iter_sse_events", return_value=iter(events)):
            return service.extraction._legacy_run_extract(account_id=7, email=self.account["email"], access_token=AT,
                link_type="upi", cdk=LINK_CDK, trigger="plus_activation")

    def test_transport_error_is_unknown_instead_of_fallback_failure(self):
        result = self.run_legacy(error=TimeoutError("request timed out"))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(db.get_account(7)["extract_link_status"], "unknown")

    def test_stream_ending_without_result_is_unknown(self):
        result = self.run_legacy(events=[("log", {"message": "running"}), ("done", {})])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(db.get_account(7)["extract_link_job_id"], "legacy-task")

    def test_explicit_upstream_error_is_failure(self):
        result = self.run_legacy(events=[("error", {"message": "CDK exhausted"})])
        self.assertEqual(result["status"], "failed")
        self.assertIn("CDK exhausted", result["error"])


if __name__ == "__main__":
    unittest.main()
