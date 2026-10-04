import unittest

from core import db
from core.chatgpt_plan import parse_accounts_check
from webui.app import _compact_account_for_list


class PlanPromoDetailsTests(unittest.TestCase):
    def parse(self, campaigns, plan="free"):
        return parse_accounts_check({"accounts": {"default": {
            "account": {"plan_type": plan},
            "eligible_promo_campaigns": campaigns,
        }}})

    def test_all_campaigns_preserved_without_changing_plus_logic(self):
        campaigns = {
            "plus": {"id": "plus-1-month-free", "metadata": {
                "discount": {"percentage": 100},
                "duration": {"num_periods": 1, "period": "month"},
                "no_auto_renewal_at_discount_end": False,
            }},
            "go": {"id": "go-discount", "metadata": {"custom_field": "保留未知字段"}},
        }
        result = self.parse(campaigns)
        self.assertTrue(result["plus_trial_eligible"])
        self.assertEqual(result["eligible_promo_campaigns"], campaigns)
        self.assertEqual(result["plus_trial_discount_percentage"], 100)
        other = self.parse({"go": campaigns["go"]})
        self.assertFalse(other["plus_trial_eligible"])
        self.assertIn("go", other["eligible_promo_campaigns"])
        self.assertEqual(self.parse(campaigns, "plus")["eligible_promo_campaigns"], {})
        self.assertEqual(self.parse({})["eligible_promo_campaigns"], {})

    def test_persistence_list_polling_and_failure_preservation(self):
        rows = [{"id": 1, "email": "test@example.test"}]
        result = self.parse({"go": {"id": "go-offer"}})
        db._save_collection("accounts", rows)
        db.update_account_plan_check(acc_id=1, result=result)
        row = db.get_account(1)
        self.assertEqual(row["eligible_promo_campaigns"], result["eligible_promo_campaigns"])
        self.assertEqual(_compact_account_for_list(row)["eligible_promo_campaigns"], result["eligible_promo_campaigns"])
        snapshot = db.list_account_plan_check_statuses()
        self.assertEqual(snapshot["items"][0]["eligible_promo_campaigns"], result["eligible_promo_campaigns"])
        db.update_account_plan_check(acc_id=1, result=self.parse({"go": {"id": "changed"}}))
        self.assertNotEqual(snapshot["revision"], db.list_account_plan_check_statuses()["revision"])
        db.update_account_plan_check(acc_id=1, result={"ok": False, "error": "timeout"})
        self.assertEqual(db.get_account(1)["eligible_promo_campaigns"], {"go": {"id": "changed"}})
        db.update_account_plan_check(acc_id=1, result=self.parse({}))
        self.assertEqual(db.get_account(1)["eligible_promo_campaigns"], {})

    def test_compact_list_exposes_plan_metadata_but_no_credentials(self):
        """Nuxt 账号列表需要旧版套餐单元用到的查询元信息，且不得泄露凭据。"""
        db._save_collection("accounts", [{
            "id": 1,
            "email": "plan-meta@example.test",
            "access_token": "private-access-token",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "extra_json": '{"registration_password": "private-password"}',
        }])
        db.update_account_plan_check(acc_id=1, result={
            "ok": True,
            "current_plan_type": "plus",
            "checked_at": "2026-02-01T10:00:00+00:00",
            "billing_period": "monthly",
            "billing_currency": "USD",
            "expires_at": "2026-03-01T00:00:00+00:00",
            "network_route": "proxy",
            "proxy_used": "http://127.0.0.1:7890",
            "eligible_promo_campaigns": {"go": {"id": "go-offer"}},
        })
        item = _compact_account_for_list(db.get_account(1))
        for key in (
            "plan_checked_at", "plan_last_success_at", "plan_check_network_route",
            "plan_check_proxy_used", "billing_period", "billing_currency",
            "plan_expires_at", "eligible_promo_campaigns",
        ):
            self.assertIn(key, item, key)
        self.assertEqual(item["plan_check_network_route"], "proxy")
        self.assertEqual(item["billing_period"], "monthly")
        self.assertEqual(item["eligible_promo_campaigns"], {"go": {"id": "go-offer"}})
        self.assertNotIn("plan_check_result_json", item)
        for forbidden in ("password", "registration_password", "access_token", "totp_secret", "extra_json", "copy_line"):
            self.assertNotIn(forbidden, item)


if __name__ == "__main__":
    unittest.main()
