"""Offline regressions for Cloak registration transitions and cleanup."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import cloakbrowser_registration as cloak
from core import email_provider, registration_service


@pytest.fixture
def flow(monkeypatch):
    driver = Mock()
    opened = SimpleNamespace(profile_id="test", raw={})
    tracker, saver = Mock(), Mock()
    tracker.stop.return_value = {"total_bytes": 42}
    mocks = {
        "build_cloak_driver": Mock(return_value=(driver, opened)),
        "PlaywrightTrafficTracker": Mock(return_value=tracker),
        "BrowserDataSaver": Mock(return_value=saver),
        "report_current_job_progress": Mock(),
        "human_delay": Mock(),
        "_maybe_accept": Mock(),
        "_check_manual_stop": Mock(),
        "_submit_email_and_wait_next": Mock(return_value="password"),
        "_fill_password_page_if_present": Mock(return_value="generated-password"),
        "_wait_for_registration_step": Mock(side_effect=["otp", "profile"]),
        "wait_for_otp": Mock(return_value="123456"),
        "_clear_otp_inputs": Mock(),
        "_type_otp": Mock(),
        "_click_continue": Mock(),
        "_is_email_verification_page": Mock(return_value=True),
        "_wait_after_email_otp_submit": Mock(return_value="accepted"),
        "_click_resend_email_otp": Mock(),
        "_complete_profile_page": Mock(return_value=True),
        "_fetch_chatgpt_session": Mock(return_value={"accessToken": "test-token"}),
        "post_register_dwell": Mock(),
        "save_account_data": Mock(return_value=7),
        "resolve_email_source": Mock(return_value="outlook"),
    }
    for name, value in mocks.items():
        monkeypatch.setattr(cloak, name, value)
    monkeypatch.setattr(cloak._cfg, "CLOAK_KEEP_BROWSER_OPEN", False)
    monkeypatch.setattr(cloak._twofa_cfg, "ENABLE_2FA", False)
    from config import codex
    monkeypatch.setattr(codex, "ENABLE_CODEX_AUTO", False)
    release = Mock()
    monkeypatch.setattr(email_provider, "release_email", release)
    return SimpleNamespace(driver=driver, tracker=tracker, saver=saver, release=release, **mocks)


def run_registration(**kwargs):
    return cloak.run_cloak_registration("test@example.com", "Test User", "1990-01-01", **kwargs)


def test_password_otp_profile_flow_reports_stages_and_closes_once(flow):
    result = run_registration()
    assert result["success"] is True
    assert result["network_traffic"] == {"total_bytes": 42}
    flow._type_otp.assert_called_once_with(flow.driver, "123456")
    flow._complete_profile_page.assert_called_once()
    flow.driver.quit.assert_called_once()
    flow.tracker.stop.assert_called_once()
    flow.saver.stop.assert_called_once()
    flow.release.assert_not_called()
    stages = [call.args[1] for call in flow.report_current_job_progress.call_args_list]
    assert "等待邮箱验证码" in stages
    assert "填写注册资料" in stages
    assert "获取登录会话" in stages
    assert "保存注册结果" in stages


def test_logged_in_state_skips_password_otp_and_profile(flow):
    flow._submit_email_and_wait_next.return_value = "logged_in"
    assert run_registration()["success"] is True
    flow._fill_password_page_if_present.assert_not_called()
    flow.wait_for_otp.assert_not_called()
    flow._complete_profile_page.assert_not_called()


@pytest.mark.parametrize("state", ["profile", "logged_in"])
def test_password_can_advance_without_otp(flow, state):
    flow._wait_for_registration_step.side_effect = [state]
    assert run_registration()["success"] is True
    flow.wait_for_otp.assert_not_called()
    assert flow._complete_profile_page.called is (state == "profile")


def test_reused_session_for_other_email_is_not_saved(flow):
    flow._submit_email_and_wait_next.return_value = "logged_in"
    flow._fetch_chatgpt_session.return_value = {
        "accessToken": "another-token", "user": {"email": "other@example.com"},
    }
    result = run_registration()
    assert result["success"] is False
    assert "账号与本次注册邮箱不一致" in result["error"]
    flow.save_account_data.assert_not_called()
    assert flow.release.call_args.kwargs["status"] == "available"


def test_reused_session_email_comparison_is_case_insensitive(flow):
    flow._submit_email_and_wait_next.return_value = "logged_in"
    flow._fetch_chatgpt_session.return_value = {
        "accessToken": "test-token", "user": {"email": " Test@Example.com "},
    }
    assert run_registration()["success"] is True


def test_missing_email_in_reused_session_is_rejected(flow):
    flow._submit_email_and_wait_next.return_value = "logged_in"
    result = cloak.run_cloak_registration(None, "Test User", "1990-01-01")
    assert result["success"] is False
    assert "尚未分配" in result["error"]
    flow.save_account_data.assert_not_called()


def test_stop_while_waiting_for_otp_does_not_resend_or_save(flow):
    flow.wait_for_otp.side_effect = registration_service.StopRequested("用户手动停止")
    result = run_registration()
    assert result["success"] is False
    assert "StopRequested" in result["error"]
    flow.wait_for_otp.assert_called_once()
    flow._click_resend_email_otp.assert_not_called()
    flow.save_account_data.assert_not_called()
    flow.driver.quit.assert_called_once()


def test_auto_submitted_otp_does_not_click_next_page_continue(flow):
    flow._is_email_verification_page.return_value = False
    assert run_registration()["success"] is True
    flow._click_continue.assert_not_called()
    flow._complete_profile_page.assert_called_once()


def test_invalid_otp_retries_with_new_code(flow):
    flow._wait_after_email_otp_submit.side_effect = ["invalid", "accepted"]
    flow.wait_for_otp.side_effect = ["111111", "222222"]
    assert run_registration()["success"] is True
    flow._click_resend_email_otp.assert_called_once()
    assert [call.args[1] for call in flow._type_otp.call_args_list] == ["111111", "222222"]


def test_otp_timeout_resends_before_waiting_again(flow):
    flow.wait_for_otp.side_effect = [TimeoutError("取码超时"), "222222"]
    assert run_registration()["success"] is True
    flow._click_resend_email_otp.assert_called_once()
    flow._type_otp.assert_called_once_with(flow.driver, "222222")


def test_unconfirmed_otp_does_not_fill_profile_or_resend(flow):
    flow._wait_for_registration_step.side_effect = ["otp", RuntimeError("验证码提交后页面未跳转")]
    result = run_registration()
    assert result["success"] is False
    assert "未跳转" in result["error"]
    flow._complete_profile_page.assert_not_called()
    flow._click_resend_email_otp.assert_not_called()


@pytest.mark.parametrize("observer", ["tracker", "saver"])
def test_observer_shutdown_failure_preserves_success(flow, observer):
    getattr(flow, observer).stop.side_effect = RuntimeError("observer failed")
    assert run_registration()["success"] is True
    flow.driver.quit.assert_called_once()
    flow.tracker.stop.assert_called_once()
    flow.saver.stop.assert_called_once()


def test_cleanup_failure_preserves_original_registration_error(flow):
    flow._submit_email_and_wait_next.side_effect = RuntimeError("registration failed")
    flow.saver.stop.side_effect = RuntimeError("cleanup failed")
    result = run_registration()
    assert result["error"] == "RuntimeError: registration failed"
    flow.driver.quit.assert_called_once()


def test_session_without_profile_submission_is_not_released_as_available(flow):
    flow._submit_email_and_wait_next.return_value = "logged_in"
    flow.save_account_data.side_effect = RuntimeError("storage failed")
    assert run_registration()["success"] is False
    assert flow.release.call_args.kwargs["status"] == "failed"


def test_wait_for_step_recognizes_profile_without_session_request(monkeypatch):
    monkeypatch.setattr(cloak, "_check_manual_stop", Mock())
    monkeypatch.setattr(cloak, "_page_snapshot", Mock(return_value={
        "url": "https://auth.openai.com/about-you", "inputs": [{"name": "name"}],
    }))
    state = Mock()
    monkeypatch.setattr(cloak, "_current_email_submit_next_state", state)
    assert cloak._wait_for_registration_step(Mock(), timeout=0) == "profile"
    state.assert_not_called()


@pytest.mark.parametrize("state", ["otp", None])
def test_wait_after_otp_needs_positive_transition(monkeypatch, state):
    monkeypatch.setattr(cloak, "_check_manual_stop", Mock())
    monkeypatch.setattr(cloak, "_page_snapshot", Mock(return_value={}))
    monkeypatch.setattr(cloak, "_current_email_submit_next_state", Mock(return_value=state))
    with pytest.raises(RuntimeError, match="验证码提交后页面未跳转"):
        cloak._wait_for_registration_step(Mock(), after_otp=True, timeout=0)


def test_wait_after_otp_tolerates_intermediate_page(monkeypatch):
    monkeypatch.setattr(cloak, "_check_manual_stop", Mock())
    monkeypatch.setattr(cloak, "_page_snapshot", Mock(return_value={}))
    states = Mock(side_effect=["otp", None, "logged_in"])
    monkeypatch.setattr(cloak, "_current_email_submit_next_state", states)
    monkeypatch.setattr(cloak.time, "sleep", Mock())
    assert cloak._wait_for_registration_step(Mock(), after_otp=True) == "logged_in"
    assert states.call_count == 3


def test_current_job_progress_updates_active_job(monkeypatch):
    monkeypatch.setattr(registration_service, "_THREAD_CTX", SimpleNamespace(job_id=7))
    monkeypatch.setattr(registration_service, "check_stop_requested", Mock())
    update = Mock()
    monkeypatch.setattr(registration_service, "_set_job_progress", update)
    registration_service.report_current_job_progress(50, "等待邮箱验证码", "等待新码")
    update.assert_called_once_with(7, 50, "等待邮箱验证码", "等待新码")


def test_current_job_progress_is_noop_without_job_context(monkeypatch):
    monkeypatch.setattr(registration_service, "_THREAD_CTX", SimpleNamespace())
    monkeypatch.setattr(registration_service, "check_stop_requested", Mock())
    update = Mock()
    monkeypatch.setattr(registration_service, "_set_job_progress", update)
    registration_service.report_current_job_progress(50, "等待邮箱验证码")
    update.assert_not_called()


def test_current_job_progress_respects_cancellation(monkeypatch):
    monkeypatch.setattr(registration_service, "_THREAD_CTX", SimpleNamespace(job_id=7))
    stop = Mock(side_effect=registration_service.StopRequested("stopped"))
    monkeypatch.setattr(registration_service, "check_stop_requested", stop)
    update = Mock()
    monkeypatch.setattr(registration_service, "_set_job_progress", update)
    with pytest.raises(registration_service.StopRequested):
        registration_service.report_current_job_progress(50, "等待邮箱验证码")
    update.assert_not_called()
