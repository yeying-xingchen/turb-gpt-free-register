"""Offline regressions for bounded retries of complete liveness checks."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from config import proxy as proxy_cfg
from core import account_liveness as liveness
from core import live_check_service as service


SUCCESS = {"ok": True, "status": "live", "access_token": "fixture-token"}
TEMPORARY = {"ok": False, "status": "failed", "retryable": True, "error": "临时连接失败"}
PROXY_A = "socks5://proxy-a.example:1080"
PROXY_B = "socks5://proxy-b.example:1080"


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.setattr(proxy_cfg, "PLAN_CHECK_PROXY", [])
    monkeypatch.setattr(proxy_cfg, "PROXY_POOL", [])
    monkeypatch.setattr(proxy_cfg, "PLAN_CHECK_UPSTREAM_PROXY", "")
    monkeypatch.setattr(proxy_cfg, "LIVE_CHECK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(proxy_cfg, "LIVE_CHECK_RETRY_DELAY", 2.0)
    slot = SimpleNamespace(release=Mock())
    monkeypatch.setattr(service, "_QUEUE_SLOTS", slot)
    monkeypatch.setattr(service.db, "mark_account_live_check_running", Mock(return_value=True))
    monkeypatch.setattr(service.db, "get_account", Mock(return_value={"email_source": "fixture-mail"}))
    save = Mock()
    monkeypatch.setattr(service.db, "update_account_liveness", save)
    log = Mock()
    monkeypatch.setattr(service, "_append_log", log)
    sleep = Mock()
    monkeypatch.setattr(service.time, "sleep", sleep)
    monkeypatch.setattr(service.random, "choice", lambda values: values[0])

    def route_for(explicit_proxy=None):
        selected = explicit_proxy or ""
        return {
            "proxy": selected,
            "network_route": "proxy" if selected else "direct",
            "proxy_mode": "proxy" if selected else "direct",
            "proxy_used": selected or None,
            "upstream_proxy": "",
            "upstream_proxy_used": None,
            "proxy_fallback_reason": None,
        }

    monkeypatch.setattr(service, "resolve_plan_check_route", route_for)
    opened = Mock(side_effect=lambda route, selected, timeout: (selected, None))
    monkeypatch.setattr(service, "open_plan_check_proxy", opened)
    check = Mock(return_value=SUCCESS)
    monkeypatch.setattr(service, "check_account_liveness", check)

    def run(proxy=None):
        return service._run_live_check(
            account_id=4242, email="retry@example.invalid", proxy=proxy, trigger="manual",
        )

    return SimpleNamespace(run=run, check=check, save=save, sleep=sleep, log=log, slot=slot, opened=opened)


def test_direct_retry_uses_fresh_sessions_and_saves_only_final_success(runner):
    runner.check.side_effect = [TEMPORARY, TEMPORARY, SUCCESS]
    result = runner.run(proxy="")
    assert result["ok"] is True
    assert result["attempts"] == result["max_attempts"] == 3
    assert [entry.kwargs["proxy"] for entry in runner.check.call_args_list] == ["", "", ""]
    states = [entry.kwargs["fingerprint_state"] for entry in runner.check.call_args_list]
    assert [state["force_fresh"] for state in states] == [False, True, True]
    assert len({id(state) for state in states}) == 3
    assert all(entry.kwargs["email_source"] == "fixture-mail" for entry in runner.check.call_args_list)
    assert all(entry.kwargs["clear_log"] is False for entry in runner.check.call_args_list)
    assert runner.sleep.call_args_list == [call(2.0), call(4.0)]
    runner.save.assert_called_once_with(4242, result)
    runner.slot.release.assert_called_once()
    assert 4242 not in service._RUNNING


def test_proxy_pool_rotates_then_retries_current_proxy_without_direct(runner, monkeypatch):
    monkeypatch.setattr(proxy_cfg, "PLAN_CHECK_PROXY", [PROXY_A, PROXY_B])
    runner.check.side_effect = [TEMPORARY, TEMPORARY, SUCCESS]
    result = runner.run()
    assert result["ok"] is True
    assert [entry.kwargs["proxy"] for entry in runner.check.call_args_list] == [PROXY_A, PROXY_B, PROXY_B]
    assert result["proxy_used"] == PROXY_B


def test_single_proxy_can_recover_on_next_complete_login(runner, monkeypatch):
    monkeypatch.setattr(proxy_cfg, "PROXY_POOL", [PROXY_A])
    runner.check.side_effect = [TEMPORARY, SUCCESS]
    result = runner.run()
    assert result["ok"] is True
    assert result["attempts"] == 2
    assert [entry.kwargs["proxy"] for entry in runner.check.call_args_list] == [PROXY_A, PROXY_A]


def test_explicit_proxy_without_pool_keeps_direct_fallback_and_retry(runner):
    runner.check.side_effect = [TEMPORARY, TEMPORARY, SUCCESS]
    result = runner.run(proxy=PROXY_A)
    assert result["ok"] is True
    assert [entry.kwargs["proxy"] for entry in runner.check.call_args_list] == [PROXY_A, "", ""]
    assert result["proxy_used"] is None


def test_pending_session_starts_new_login_after_polling_is_exhausted(runner):
    pending = liveness._failure_result(liveness.SessionNotReadyError("AT 尚未就绪"), "fixture-time")
    assert pending["error_code"] == "session_not_ready"
    assert pending["retryable"] is True
    runner.check.side_effect = [pending, SUCCESS]
    result = runner.run(proxy="")
    assert result["ok"] is True
    assert runner.check.call_count == 2


@pytest.mark.parametrize("failure", [
    {"ok": False, "status": "failed", "retryable": False, "error": "HTTP 403：凭据错误"},
    {"ok": False, "status": "failed", "http_status": 401, "error": "password invalid via proxy"},
    {"ok": False, "status": "deactivated", "retryable": True, "error": "account_deleted"},
    {"ok": False, "status": "failed", "retryable": True, "error": "HTTP 403 account_deactivated"},
    {"ok": False, "status": "failed", "error": "账号要求手机号验证"},
])
def test_terminal_errors_do_not_restart_login(failure, runner):
    runner.check.side_effect = [failure, SUCCESS]
    result = runner.run()
    assert result["ok"] is False
    assert result["attempts"] == 1
    runner.check.assert_called_once()
    runner.sleep.assert_not_called()
    runner.save.assert_called_once_with(4242, result)


@pytest.mark.parametrize("code", ["invalid_password", "invalid_totp", "invalid_code"])
def test_credential_error_code_blocks_full_retry_even_on_403(code):
    error = RuntimeError("HTTP 403")
    error.response = SimpleNamespace(status_code=403, text=json.dumps({"error": {"code": code}}))
    result = liveness._failure_result(error, "fixture-time")
    assert result["status"] == "failed"
    assert result["retryable"] is False
    assert service._retryable_live_check_result(result) is False


def test_cloudflare_challenge_is_waiting_and_not_retryable():
    error = RuntimeError("HTTP 403 challenge")
    error.response = SimpleNamespace(
        status_code=403,
        headers={"cf-mitigated": "challenge"},
        text="<html>challenge</html>",
    )
    result = liveness._failure_result(error, "fixture-time")
    assert result["status"] == "failed"
    assert result["error_code"] == "cloudflare_challenge"
    assert result["challenge_waiting"] is True
    assert result["retryable"] is False
    assert service._retryable_live_check_result(result) is False


@pytest.mark.parametrize("status", [403, 408, 425, 429, 500, 502, 503, 504])
def test_structured_http_status_is_retryable_without_english_message(status):
    result = {"ok": False, "status": "failed", "http_status": status, "error": "临时失败"}
    assert service._retryable_live_check_result(result) is True


def test_transport_exception_before_login_can_rotate_proxy(runner, monkeypatch):
    monkeypatch.setattr(proxy_cfg, "PLAN_CHECK_PROXY", [PROXY_A, PROXY_B])
    runner.opened.side_effect = [TimeoutError(), (PROXY_B, None)]
    result = runner.run()
    assert result["ok"] is True
    assert result["attempts"] == 2
    runner.check.assert_called_once()
    assert runner.check.call_args.kwargs["proxy"] == PROXY_B
    runner.slot.release.assert_called_once()


def test_relays_are_closed_between_attempts_and_after_success(runner, monkeypatch):
    monkeypatch.setattr(proxy_cfg, "PLAN_CHECK_PROXY", [PROXY_A])
    relay_a, relay_b = SimpleNamespace(close=Mock()), SimpleNamespace(close=Mock())
    runner.opened.side_effect = [("transport-a", relay_a), ("transport-b", relay_b)]

    def check(*args, **kwargs):
        if kwargs["proxy"] == "transport-a":
            raise ConnectionError("connection reset")
        relay_a.close.assert_called_once()
        return SUCCESS

    runner.check.side_effect = check
    result = runner.run()
    assert result["ok"] is True
    relay_a.close.assert_called_once()
    relay_b.close.assert_called_once()
    runner.slot.release.assert_called_once()


def test_unusable_exception_stops_retries_and_releases_queue(runner):
    runner.check.side_effect = liveness.AccountUnusableError("account_deactivated", error_code="account_deactivated")
    result = runner.run()
    assert result["status"] == "deactivated"
    runner.check.assert_called_once()
    runner.sleep.assert_not_called()
    runner.slot.release.assert_called_once()


def test_retry_limit_is_enforced_and_logged(runner):
    runner.check.return_value = TEMPORARY
    result = runner.run()
    assert result["ok"] is False
    assert result["attempts"] == 3
    assert runner.check.call_count == 3
    assert runner.sleep.call_count == 2
    assert any("重试次数已用尽" in entry.args[1] for entry in runner.log.call_args_list)
    runner.save.assert_called_once_with(4242, result)


def test_one_attempt_disables_full_login_retries(runner, monkeypatch):
    monkeypatch.setattr(proxy_cfg, "LIVE_CHECK_MAX_ATTEMPTS", 1)
    runner.check.return_value = TEMPORARY
    result = runner.run()
    assert result["attempts"] == result["max_attempts"] == 1
    runner.check.assert_called_once()
    runner.sleep.assert_not_called()


def test_exponential_delay_is_capped(runner, monkeypatch):
    monkeypatch.setattr(proxy_cfg, "LIVE_CHECK_MAX_ATTEMPTS", 5)
    monkeypatch.setattr(proxy_cfg, "LIVE_CHECK_RETRY_DELAY", 40.0)
    runner.check.return_value = TEMPORARY
    runner.run()
    assert runner.sleep.call_args_list == [call(40.0), call(60.0), call(60.0), call(60.0)]


@pytest.mark.parametrize("attempts,delay,expected", [
    (3, 2.0, (3, 2.0)), (0, -1, (1, 0.0)), (100, 100, (5, 60.0)),
    (None, None, (3, 2.0)), ("bad", "bad", (3, 2.0)),
    (float("inf"), float("nan"), (3, 2.0)), ("2", "0.5", (2, 0.5)),
])
def test_retry_settings_are_bounded(attempts, delay, expected):
    cfg = SimpleNamespace(LIVE_CHECK_MAX_ATTEMPTS=attempts, LIVE_CHECK_RETRY_DELAY=delay)
    assert service._live_retry_settings(cfg) == expected
