# -*- coding: utf-8 -*-
import unittest

from core import db
from core.chatgpt_plan import parse_subscription
from webui.app import _compact_account_for_list


class SubscriptionGracePeriodTests(unittest.TestCase):
    def test_parse_subscription_maps_grace_period_fields(self):
        result = parse_subscription({
            "active_start": "2025-01-01T00:00:00Z",
            "active_until": "2025-02-01T00:00:00Z",
            "became_delinquent_timestamp": "2025-01-15T00:00:00Z",
            "grace_period_end_timestamp": "2025-01-22T00:00:00Z",
            "billing_currency": "USD",
            "billing_period": "monthly",
            "plan_type": "chatgptplusplan",
        })

        self.assertEqual(result["subscription_became_delinquent_at"], "2025-01-15T00:00:00Z")
        self.assertEqual(result["subscription_grace_period_end_at"], "2025-01-22T00:00:00Z")
        self.assertEqual(result["subscription_plan_type"], "chatgptplusplan")

    def test_grace_period_is_persisted_and_cleared_when_api_returns_null(self):
        rows = [{"id": 1, "email": "test@example.test"}]
        result = {
            "ok": True,
            "checked_at": "2025-01-15T00:00:00",
            "current_plan_type": "plus",
            "subscription_became_delinquent_at": "2025-01-15T00:00:00Z",
            "subscription_grace_period_end_at": "2025-01-22T00:00:00Z",
            "subscription_checked_at": "2025-01-15T00:00:01",
            "subscription_http_status": 200,
            "subscription_error": None,
        }
        db._save_collection("accounts", rows)
        db.update_account_plan_check(acc_id=1, result=result)
        compact = _compact_account_for_list(db.get_account(1))
        self.assertEqual(compact["subscription_grace_period_end_at"], "2025-01-22T00:00:00Z")
        self.assertEqual(compact["subscription_became_delinquent_at"], "2025-01-15T00:00:00Z")

        result["subscription_became_delinquent_at"] = None
        result["subscription_grace_period_end_at"] = None
        db.update_account_plan_check(acc_id=1, result=result)
        compact = _compact_account_for_list(db.get_account(1))
        self.assertNotIn("subscription_grace_period_end_at", compact)
        self.assertNotIn("subscription_became_delinquent_at", compact)


if __name__ == "__main__":
    unittest.main()
