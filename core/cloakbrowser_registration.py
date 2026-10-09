# -*- coding: utf-8 -*-
"""通过 CloakBrowser + Playwright 适配层执行 ChatGPT 注册。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from config import cloakbrowser as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data, post_register_dwell, registration_timestamp
from core.browser_data_saver import BrowserDataSaver
from core.browser_traffic import PlaywrightTrafficTracker
from core.cloakbrowser_driver import build_cloak_driver
from core.email_provider import acquire_email_after_input, wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay
from core.registration_service import StopRequested, report_current_job_progress

# 复用 Roxy 注册流程里已维护好的页面操作函数。
from core.roxy_registration import (  # noqa: F401
    _maybe_accept, _submit_email_and_wait_next, _fill_password_page_if_present,
    _clear_otp_inputs, _type_otp, _click_continue, _wait_after_email_otp_submit,
    _click_resend_email_otp, _complete_profile_page, _fetch_chatgpt_session, _check_manual_stop,
    _current_email_submit_next_state, _page_snapshot, _is_profile_like, _is_email_verification_page,
)

logger = logging.getLogger(__name__)


def _wait_for_registration_step(driver, *, after_otp: bool = False, timeout: float = 30) -> str:
    """确认真实页面阶段；验证码提交无错误但未跳转时不能当作验证成功。"""
    deadline = time.monotonic() + timeout
    last_state = None
    while True:
        _check_manual_stop()
        # 资料页可能没有 OTP，先识别它，避免在认证站点反复请求跨域 session。
        if _is_profile_like(_page_snapshot(driver)):
            return "profile"
        last_state = _current_email_submit_next_state(driver)
        if last_state == "logged_in" or (last_state == "otp" and not after_otp):
            return last_state
        if last_state in {"password", "login_password"}:
            raise RuntimeError("注册仍停留在密码页，尚未进入邮箱验证或资料填写阶段")
        if time.monotonic() >= deadline:
            stage = "验证码提交后页面未跳转" if after_otp else "等待注册下一阶段超时"
            raise RuntimeError(f"{stage}，当前状态={last_state or 'unknown'}")
        time.sleep(0.5)


def run_cloak_registration(
    email: str | None,
    name: str,
    birthday: str,
    proxy: str = None,
    otp_code: str = None,
    batch_dir: Path | None = None,
    on_email_acquired: Callable[[str], None] | None = None,
) -> dict:
    """CloakBrowser 自动化注册入口。"""
    driver = None
    opened = None
    create_acknowledged = False
    openai_password: str | None = None
    traffic_tracker: PlaywrightTrafficTracker | None = None
    data_saver: BrowserDataSaver | None = None
    network_traffic: dict | None = None

    def _stop_observers() -> None:
        nonlocal traffic_tracker, data_saver, network_traffic
        # 每个观察器只停止一次；非关键的统计失败不能覆盖注册结果或跳过浏览器关闭。
        tracker, traffic_tracker = traffic_tracker, None
        saver, data_saver = data_saver, None
        if tracker is not None:
            logger.info("[Cloak注册] 正在汇总浏览器流量统计")
            try:
                network_traffic = tracker.stop()
            except Exception:
                logger.warning("[Cloak注册] 汇总流量统计失败", exc_info=True)
        if saver is not None:
            try:
                saver.stop()
            except Exception:
                logger.warning("[Cloak注册] 停止省流量观察器失败", exc_info=True)

    try:
        report_current_job_progress(30, "启动浏览器", "正在启动 Cloak 浏览器或等待可用额度")
        driver, opened = build_cloak_driver(proxy=proxy)
        try:
            traffic_tracker = PlaywrightTrafficTracker(driver.context, label="Cloak")
        except Exception as exc:
            # 统计失败不应影响注册主流程。
            logger.warning("[Cloak注册] 初始化浏览器流量统计失败，继续注册：%s: %s", type(exc).__name__, str(exc)[:180])
        data_saver = BrowserDataSaver(label="Cloak")
        if traffic_tracker is not None:
            traffic_tracker.attach_data_saver(data_saver)
        data_saver.install_playwright(driver.context)
        logger.info("[Cloak注册] 开始：%s，profile=%s", email, opened.profile_id)

        report_current_job_progress(35, "打开注册页面", "正在加载登录页，等待邮箱输入框")
        otp_after_ts = time.time()
        logger.info("[Cloak注册] 打开登录页：https://chatgpt.com/auth/login")
        driver.get("https://chatgpt.com/auth/login")
        human_delay("navigate")
        _maybe_accept(driver)
        _check_manual_stop()

        def _email_supplier_after_input() -> str:
            nonlocal email
            _check_manual_stop()
            email = acquire_email_after_input(email)
            if on_email_acquired:
                on_email_acquired(email)
            return email

        next_state = _submit_email_and_wait_next(
            driver,
            email,
            attempts=3,
            email_supplier=_email_supplier_after_input,
        )
        _check_manual_stop()

        if not email:
            raise RuntimeError("页面已进入后续阶段，但尚未分配本次注册邮箱，请使用新的浏览器会话")
        if next_state not in {"logged_in", "profile"}:
            report_current_job_progress(40, "处理密码页", "正在确认密码页或邮箱验证码页")
            # 保留验证码页切换到密码创建页的能力，以保存注册密码。
            openai_password = _fill_password_page_if_present(driver, email, timeout=25)
            _check_manual_stop()
            next_state = _wait_for_registration_step(driver)

        current_otp = otp_code
        max_otp_attempts = 3
        for otp_attempt in range(1, max_otp_attempts + 1):
            if next_state != "otp":
                break
            _check_manual_stop()
            report_current_job_progress(50, "等待邮箱验证码", f"正在等待邮箱验证码（{otp_attempt}/{max_otp_attempts}）")
            if current_otp is None:
                logger.info("[Cloak注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                try:
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                except StopRequested:
                    raise
                except Exception as exc:
                    _check_manual_stop()
                    if otp_attempt >= max_otp_attempts:
                        raise
                    logger.warning(
                        "[Cloak注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                        otp_attempt + 1,
                        max_otp_attempts,
                        type(exc).__name__,
                        str(exc)[:180],
                    )
                    otp_after_ts = time.time()
                    _click_resend_email_otp(driver, timeout=25)
                    human_delay("api")
                    current_otp = None
                    continue
            _check_manual_stop()
            logger.info("[Cloak注册][OTP] 已收到验证码，准备提交")
            report_current_job_progress(60, "验证邮箱", "已收到验证码，正在提交并等待页面跳转")
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            human_delay("otp_input")
            _check_manual_stop()
            if _is_email_verification_page(driver):
                try:
                    _click_continue(driver)
                except StopRequested:
                    raise
                except Exception as exc:
                    logger.info("[Cloak注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", str(exc)[:120])

            outcome = _wait_after_email_otp_submit(driver, timeout=10)
            _check_manual_stop()
            if outcome == "accepted":
                next_state = _wait_for_registration_step(driver, after_otp=True)
                break
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            otp_after_ts = time.time()
            _click_resend_email_otp(driver, timeout=25)
            human_delay("api")
            current_otp = None

        if next_state == "profile":
            report_current_job_progress(70, "填写注册资料", "正在填写姓名和生日")
            profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
            if profile_submitted:
                create_acknowledged = True
                human_delay("post_auth")

        report_current_job_progress(80, "获取登录会话", "正在等待 ChatGPT 登录会话")
        session_info = _fetch_chatgpt_session(driver, timeout=120)
        access_token = session_info["accessToken"]
        session_email = str((session_info.get("user") or {}).get("email") or "").strip()
        if session_email and session_email.casefold() != email.strip().casefold():
            raise RuntimeError("浏览器当前登录账号与本次注册邮箱不一致，请使用新的浏览器会话")
        registered_at = registration_timestamp()
        create_acknowledged = True
        logger.info("[Cloak注册] 已拿到 accessToken：%s", email)

        if _twofa_cfg.ENABLE_2FA:
            logger.warning("[Cloak注册] 当前 CloakBrowser 自动化路径暂不执行 2FA 设置，已跳过")
        totp_secret = None

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg
            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                from core.roxy_codex_oauth import run_roxy_codex_oauth
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=True，复用当前 CloakBrowser 窗口执行 Codex 授权")
                report_current_job_progress(85, "Codex 授权", "注册已完成，正在执行 Codex 授权")
                codex_result = run_roxy_codex_oauth(
                    email,
                    reuse_existing_profile=True,
                    existing_driver=driver,
                    existing_opened=opened,
                    force=True,
                    clear_existing_state=True,
                )
            else:
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except StopRequested:
            raise
        except Exception as exc:
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        # 统计注册浏览器关闭前的完整会话；注册后停留期间的网络请求也计入。
        report_current_job_progress(88, "保存注册结果", "正在完成注册后停留并保存账号")
        post_register_dwell(email, label="Cloak注册")
        _stop_observers()
        logger.info("[Cloak注册] 流量统计完成，正在保存账号：%s", email)
        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=((opened.raw or {}).get("proxy_pool_target") if opened else None) or proxy or None,
            batch_dir=batch_dir,
            registered_at=registered_at,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "cloakbrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "registration_password": openai_password,
                "codex": codex_result,
                "network_traffic": network_traffic,
            },
        )
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {
            "success": bool(codex_ok),
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": totp_secret,
            "codex": codex_result,
            "network_traffic": network_traffic,
            "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}",
        }
    except Exception as exc:
        _stop_observers()
        logger.error("[Cloak注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Cloak注册] 失败详情", exc_info=True)
        try:
            if email:
                from core.email_provider import release_email
                release_email(email, status="failed" if create_acknowledged else "available", note=f"Cloak注册失败: {str(exc)[:180]}")
        except Exception:
            pass
        return {
            "success": False,
            "email": email,
            "network_traffic": network_traffic,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    finally:
        _stop_observers()
        if driver and not bool(_cfg.CLOAK_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass
