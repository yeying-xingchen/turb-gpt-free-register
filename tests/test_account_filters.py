# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


class AccountFilterTests(unittest.TestCase):
    def test_status_filter_matrix(self):
        self.assertTrue(db._account_matches_status_filter({"live_check_status": "live", "live_check_ok": True}, "live"))
        self.assertFalse(db._account_matches_status_filter({"live_check_status": "live", "live_check_ok": False}, "live"))
        self.assertFalse(db._account_matches_status_filter({"live_check_status": "failed"}, "live"))
        self.assertTrue(db._account_matches_status_filter({"live_check_status": "failed"}, "failed"))
        self.assertTrue(db._account_matches_status_filter({"plan_type": "free", "plus_trial_eligible": True}, "trial"))
        self.assertFalse(db._account_matches_status_filter({"plan_type": "plus", "plus_trial_eligible": True}, "trial"))
        self.assertTrue(db._account_matches_status_filter({"live_check_status": "deactivated"}, "deactivated"))
        self.assertTrue(db._account_matches_status_filter({"codex_status": "deactivated"}, "deactivated"))
        self.assertFalse(db._account_matches_status_filter({"live_check_status": "queued"}, "deactivated"))
        self.assertTrue(db._account_matches_status_filter({"plan_type": "plus"}, "plus"))
        self.assertFalse(db._account_matches_status_filter({"plan_type": "free", "plus_trial_eligible": True}, "plus"))

    def test_api_forwards_status_filter_and_returns_compact_rows(self):
        rows = [{"id": 1, "email": "live@example.com", "plan_type": "plus", "live_check_status": "live", "live_check_ok": True, "access_token": "secret"}]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accounts_path = root / "accounts.json"
            accounts_path.write_text(json.dumps(rows), encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", accounts_path), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"), \
                 patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), \
                 patch.object(db, "_TOKENS_TXT", root / "tokens.txt"), \
                 patch.object(db, "_VIEWER_HTML", root / "viewer.html"):
                client = create_app(auth_code="test-auth").test_client()
                response = client.get("/api/accounts?paged=1&page=1&page_size=20&status=live", headers={"X-Auth-Code": "test-auth"})
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["total"], 1)
                self.assertEqual(payload["items"][0]["id"], 1)
                self.assertTrue(payload["items"][0]["has_access_token"])
                self.assertNotIn("access_token", payload["items"][0])

    def test_list_accounts_page_filters_status_and_archived(self):
        rows = [
            {"id": 1, "email": "plus@example.com", "plan_type": "plus", "live_check_status": "live", "live_check_ok": True},
            {"id": 2, "email": "free@example.com", "plan_type": "free", "plus_trial_eligible": True, "live_check_status": "failed"},
            {"id": 3, "email": "dead@example.com", "plan_type": "free", "live_check_status": "deactivated"},
            {"id": 5, "email": "failed@example.com", "plan_type": "free", "live_check_status": "failed", "plus_trial_eligible": False},
            {"id": 4, "email": "archived-live@example.com", "plan_type": "plus", "live_check_status": "live", "live_check_ok": True, "archived": True},
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accounts_path = root / "accounts.json"
            accounts_path.write_text(json.dumps(rows), encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", accounts_path), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"), \
                 patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), \
                 patch.object(db, "_TOKENS_TXT", root / "tokens.txt"), \
                 patch.object(db, "_VIEWER_HTML", root / "viewer.html"):
                live = db.list_accounts_page(status_filter="live")
                self.assertEqual(live["total"], 1)
                self.assertEqual(live["items"][0]["id"], 1)

                dead = db.list_accounts_page(status_filter="deactivated")
                self.assertEqual(dead["total"], 1)
                self.assertEqual(dead["items"][0]["id"], 3)

                failed = db.list_accounts_page(status_filter="failed")
                self.assertEqual(failed["total"], 2)
                self.assertEqual({item["id"] for item in failed["items"]}, {2, 5})

                trial = db.list_accounts_page(status_filter="trial")
                self.assertEqual(trial["total"], 1)
                self.assertEqual(trial["items"][0]["id"], 2)

                plus = db.list_accounts_page(status_filter="plus")
                self.assertEqual(plus["total"], 1)
                self.assertEqual(plus["items"][0]["id"], 1)

                archived_live = db.list_accounts_page(status_filter="live", archived="only")
                self.assertEqual(archived_live["total"], 1)
                self.assertEqual(archived_live["items"][0]["id"], 4)

                snapshot = db.list_account_plan_check_statuses(status_filter="live")
                self.assertEqual(snapshot["total"], 1)
                self.assertEqual(snapshot["items"][0]["id"], 1)
                self.assertEqual(snapshot["items"][0]["live_check_status"], "live")


if __name__ == "__main__":
    unittest.main()
