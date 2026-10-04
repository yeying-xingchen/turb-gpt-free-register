"""Quantity redemption uses isolated stock and never calls live providers."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import db
from webui.app import create_app


def stock(count):
    for index in range(count):
        db.insert_account(
            email=f"quantity-{index}@example.test",
            access_token=f"private-at-{index}",
            plan_type="plus",
            totp_secret=f"TOTP{index}",
            extra={"registration_password": f"password-{index}"},
        )


def code_for(quantity):
    return db.create_redeem_code(quantity=quantity, account_group="默认分组")["code"]


def test_partial_redemptions_preserve_quota_and_never_repeat_accounts():
    stock(5)
    code = code_for(4)
    first = db.redeem_plus_accounts(code, quantity=2)
    assert (first["count"], first["remaining"]) == (2, 2)
    assert db.list_redeem_codes()[0]["status"] == "active"
    second = db.redeem_plus_accounts(code, quantity=1)
    assert (second["count"], second["remaining"]) == (1, 1)
    last = db.redeem_plus_accounts(code, quantity=1)
    assert (last["count"], last["remaining"]) == (1, 0)
    ids = [a["account_id"] for batch in (first, second, last) for a in batch["accounts"]]
    assert len(set(ids)) == 4
    item = db.list_redeem_codes()[0]
    assert item["status"] == "exhausted"
    assert item["redeemed_count"] == 4
    assert len(item["redeemed_accounts"]) == 4
    assert db.redeem_stock_summary()["available"] == 1
    with pytest.raises(db.RedeemError) as caught:
        db.redeem_plus_accounts(code, quantity=1)
    assert caught.value.code == "code_exhausted"
    assert caught.value.status == 410


def test_stock_only_needs_to_cover_this_batch():
    stock(2)
    code = code_for(10)
    result = db.redeem_plus_accounts(code, quantity=2)
    assert (result["count"], result["remaining"]) == (2, 8)
    assert db.redeem_stock_summary()["available"] == 0


def test_omitted_quantity_redeems_all_remaining_after_partial_batch():
    stock(3)
    code = code_for(3)
    db.redeem_plus_accounts(code, quantity=1)
    result = db.redeem_plus_accounts(code)
    assert (result["count"], result["remaining"]) == (2, 0)


@pytest.mark.parametrize("quantity", [True, False, 0, -1, 1001, 1.5, 1.0, "1", "", [], {}])
def test_invalid_quantity_never_consumes_quota_or_stock(quantity):
    stock(1)
    code = code_for(2)
    with pytest.raises(db.RedeemError) as caught:
        db.redeem_plus_accounts(code, quantity=quantity)
    assert caught.value.code == "invalid_quantity"
    assert caught.value.status == 400
    assert db.list_redeem_codes()[0]["redeemed_count"] == 0
    assert db.redeem_stock_summary()["available"] == 1


def test_quota_and_stock_failures_leave_previous_partial_claims_unchanged():
    stock(2)
    code = code_for(3)
    db.redeem_plus_accounts(code, quantity=1)
    for quantity, error in [(3, "insufficient_quota"), (2, "insufficient_stock")]:
        with pytest.raises(db.RedeemError) as caught:
            db.redeem_plus_accounts(code, quantity=quantity)
        assert caught.value.code == error
        assert caught.value.status == 409
        item = db.list_redeem_codes()[0]
        assert item["redeemed_count"] == 1
        assert item["remaining"] == 2
        assert len(item["redeemed_accounts"]) == 1
        assert db.redeem_stock_summary()["available"] == 1


def test_concurrent_batches_cannot_overdraw_quota():
    stock(4)
    code = code_for(3)

    def redeem():
        try:
            return db.redeem_plus_accounts(code, quantity=2)
        except db.RedeemError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: redeem(), range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("insufficient_quota") == 1
    assert db.list_redeem_codes()[0]["remaining"] == 1
    assert db.redeem_stock_summary()["available"] == 2


def test_api_returns_display_credentials_matching_manual_download():
    stock(3)
    code = code_for(3)
    client = create_app(auth_code="test-admin").test_client()
    response = client.post("/api/redeem", json={"cdk": code, "quantity": 2})
    assert response.status_code == 200
    body = response.get_json()
    assert (body["count"], body["remaining"]) == (2, 1)
    assert body["credentials"] == [
        "quantity-0@example.test---password-0---TOTP0",
        "quantity-1@example.test---password-1---TOTP1",
    ]
    assert "no-store" in response.headers["Cache-Control"]
    assert "private-at-" not in response.get_data(as_text=True)
    download = client.get(body["download_url"])
    assert download.status_code == 200
    assert all(line in download.get_data(as_text=True) for line in body["credentials"])
    assert "private-at-" not in download.get_data(as_text=True)
    last = client.post("/api/redeem", json={"cdk": code}).get_json()
    assert (last["count"], last["remaining"]) == (1, 0)


@pytest.mark.parametrize("quantity", [None, True, False, "2", 1.0, 1.5, 0, -1, 1001, [], {}])
def test_api_rejects_explicit_invalid_quantity(quantity):
    stock(1)
    code = code_for(1)
    client = create_app(auth_code="test-admin").test_client()
    response = client.post("/api/redeem", json={"cdk": code, "quantity": quantity})
    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_quantity"
    assert db.list_redeem_codes()[0]["redeemed_count"] == 0
    assert db.redeem_stock_summary()["available"] == 1
