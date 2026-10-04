"""Browser checks against the generated Nuxt app and an isolated Flask instance."""
import os
import re
from pathlib import Path
from threading import Thread

import pytest
from werkzeug.serving import make_server

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
DIST = Path(__file__).resolve().parents[1] / 'frontend/.output/public'
pytestmark = pytest.mark.skipif(
    os.environ.get('RUN_NUXT_BROWSER_TESTS') != '1' or not (DIST / 'index.html').exists(),
    reason='Set RUN_NUXT_BROWSER_TESTS=1 and build Nuxt to run browser checks',
)


@pytest.fixture
def live_app(monkeypatch):
    from core import account_import, db
    from webui.app import create_app

    monkeypatch.setattr(account_import, 'fetch_account_user_name', lambda *a, **kw: {'ok': True, 'user_name': 'Imported User'})
    for index in range(3):
        db.insert_account(email=f'demo{index}@example.test', access_token=f'private-at-{index}',
                          plan_type='plus', totp_secret='JBSWY3DPEHPK3PXP',
                          extra={'registration_password': f'private-password-{index}'})
    app = create_app(auth_code='browser-test-auth')
    app.config.update(TESTING=True, NUXT_DIST_DIR=DIST)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def browser_page(live_app):
    with playwright.sync_playwright() as driver:
        options = {'headless': True}
        if executable := os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'):
            options['executable_path'] = executable
        browser = driver.chromium.launch(**options)
        context = browser.new_context(viewport={'width': 1440, 'height': 1050})
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        yield page, live_app, errors
        context.close()
        browser.close()


def login(page, base, destination='/'):
    page.goto(base + destination)
    expect(page.get_by_role('heading', name='欢迎回来')).to_be_visible()
    page.get_by_label('授权码', exact=True).fill('browser-test-auth')
    page.get_by_role('button', name='进入工作空间').click()
    page.wait_for_url(base + destination)


def test_login_navigation_mobile_and_session_expiry(browser_page):
    page, base, errors = browser_page
    page.goto(base + '/accounts')
    page.get_by_label('授权码', exact=True).fill('wrong')
    page.get_by_role('button', name='进入工作空间').click()
    expect(page.get_by_role('alert')).to_contain_text('授权码错误')
    page.get_by_label('授权码', exact=True).fill('browser-test-auth')
    page.get_by_role('button', name='进入工作空间').click()
    page.wait_for_url(base + '/accounts')
    expect(page.get_by_role('heading', name='账号管理', exact=True)).to_be_visible()
    for path, heading in [('/', '注册与概览'), ('/tasks', '任务中心'), ('/mailboxes', '邮箱池'),
                          ('/codex', 'Codex 授权'), ('/redemptions', '兑换管理'),
                          ('/providers', '提链服务'), ('/settings', '系统配置')]:
        page.locator(f'nav[aria-label="主导航"] a[href="{path}"]').click()
        expect(page.get_by_role('heading', name=heading, exact=True).first).to_be_visible()
        page.reload(wait_until='networkidle')
        expect(page.get_by_role('heading', name=heading, exact=True).first).to_be_visible()
        assert page.locator('.alert-error:visible').count() == 0, path
    page.goto(base + '/', wait_until='networkidle')
    output = DIST.parents[1] / 'test-results'
    output.mkdir(exist_ok=True)
    page.screenshot(path=str(output / 'dashboard.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.get_by_role('button', name='打开导航').click()
    page.locator('nav[aria-label="主导航"] a[href="/accounts"]').click()
    expect(page.get_by_role('heading', name='账号管理', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='打开导航')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    page.screenshot(path=str(output / 'accounts-mobile.png'), full_page=True, animations='disabled')
    page.context.clear_cookies()
    page.get_by_role('button', name='刷新', exact=True).first.click()
    expect(page.get_by_role('heading', name='欢迎回来')).to_be_visible()
    assert not errors


def test_account_import_dialog_and_sensitive_data(browser_page):
    page, base, errors = browser_page
    login(page, base, '/accounts')
    expect(page.get_by_role('button', name='demo0@example.test', exact=True)).to_be_visible()
    assert 'private-at-0' not in page.content()
    assert 'private-password-0' not in page.content()
    page.get_by_role('button', name='导入账号', exact=True).click()
    dialog = page.get_by_role('dialog', name='导入已有账号')
    expect(dialog).to_be_visible()
    dialog.get_by_label('账号内容').fill('imported@example.test---example-password---JBSWY3DPEHPK3PXP---example-at')
    dialog.get_by_role('button', name='导入账号', exact=True).click()
    expect(dialog.locator('.import-result')).to_be_visible()
    dialog.get_by_role('button', name='关闭', exact=True).click()
    expect(page.get_by_role('button', name='imported@example.test', exact=True)).to_be_visible()
    page.get_by_role('button', name='demo0@example.test', exact=True).click()
    expect(page.get_by_role('dialog')).to_be_visible()
    page.keyboard.press('Escape')
    expect(page.locator('dialog[open]')).to_have_count(0)
    page.get_by_role('button', name='导入账号', exact=True).click()
    expect(page.get_by_role('dialog', name='导入已有账号').get_by_label('账号内容')).to_have_value('')
    page.keyboard.press('Escape')
    # 每一行都显示兑换状态；账号被 CDK 领取后该行切换为「已兑换」。
    from core import db
    row = page.locator('tbody tr', has=page.get_by_role('button', name='demo0@example.test', exact=True))
    expect(row.get_by_text('未兑换', exact=True)).to_be_visible()
    db.redeem_plus_accounts(db.create_redeem_code(quantity=1, account_group='默认分组')['code'])
    page.reload(wait_until='networkidle')
    claimed = page.locator('tbody tr', has=page.get_by_role('button', name='demo0@example.test', exact=True))
    expect(claimed.get_by_text('已兑换', exact=True)).to_be_visible()
    assert not errors


def test_public_redemption_restore_next_batch_and_download(browser_page):
    from core import db
    page, base, errors = browser_page
    code = db.create_redeem_code(quantity=2, account_group='默认分组')['code']
    page.goto(base + '/redeem')
    page.get_by_label('兑换码', exact=True).fill(code)
    page.get_by_role('button', name='兑换 / 恢复原结果', exact=True).click()
    expect(page.get_by_role('heading', name='兑换成功 · 1 个账号')).to_be_visible()
    credentials = page.locator('pre.credentials').inner_text()
    storage = page.evaluate('JSON.stringify(sessionStorage)')
    assert code not in storage and 'private-password' not in storage
    with page.expect_download() as download:
        page.get_by_role('link', name='下载凭据').click()
    downloaded = Path(download.value.path()).read_text()
    assert downloaded.startswith('# ChatGPT 登录凭据')
    assert downloaded.endswith(credentials + '\n')
    page.reload()
    page.get_by_label('兑换码', exact=True).fill(code)
    page.get_by_role('button', name='兑换 / 恢复原结果', exact=True).click()
    expect(page.get_by_role('heading', name='已恢复原批次 · 1 个账号')).to_be_visible()
    assert page.locator('pre.credentials').inner_text() == credentials
    assert db.list_redeem_codes()[0]['redeemed_count'] == 1
    page.get_by_role('button', name='继续兑换下一批（剩余 1 个）').click()
    expect(page.get_by_role('heading', name='兑换成功 · 1 个账号')).to_be_visible()
    assert db.list_redeem_codes()[0]['redeemed_count'] == 2
    assert page.locator('pre.credentials').first.inner_text() != credentials
    assert not errors


def test_provider_forms_and_settings_save(browser_page, monkeypatch):
    from core import twofa_service
    from config import env_loader
    page, base, errors = browser_page
    monkeypatch.setattr(twofa_service, 'apply_settings', lambda: None)
    login(page, base, '/providers')
    page.get_by_role('button', name='添加供应商', exact=True).click()
    dialog = page.get_by_role('dialog', name='添加供应商', exact=True)
    dialog.get_by_label('名称', exact=True).fill('Browser test provider')
    dialog.get_by_label('API 地址', exact=True).fill('https://example.test')
    dialog.get_by_role('button', name='保存供应商').click()
    expect(page.locator('dialog[open]')).to_have_count(0)
    expect(page.locator('.provider-detail h2')).to_have_text('Browser test provider')
    page.get_by_role('button', name='添加 CDK', exact=True).click()
    dialog = page.get_by_role('dialog', name='添加 CDK', exact=True)
    dialog.get_by_label('CDK', exact=True).fill('private-test-provider-abcd')
    dialog.get_by_label('备注', exact=True).fill('Browser credential')
    dialog.get_by_role('button', name='保存 CDK').click()
    expect(page.locator('.provider-detail .data-table')).to_contain_text('abcd')
    assert 'private-test-provider-abcd' not in page.content()
    page.get_by_role('button', name='添加供应商', exact=True).click()
    dialog = page.get_by_role('dialog', name='添加供应商', exact=True)
    dialog.get_by_label('名称', exact=True).fill('Browser test provider')
    dialog.get_by_label('API 地址', exact=True).fill('https://example.test')
    dialog.get_by_role('button', name='保存供应商').click()
    expect(dialog.get_by_role('alert')).to_contain_text('已存在')
    page.keyboard.press('Escape')
    page.goto(base + '/settings')
    page.get_by_label('搜索配置项').fill('ROXY_API_BASE')
    page.get_by_label('Roxy API 地址', exact=True).fill('http://127.0.0.1:55555')
    page.get_by_role('button', name='保存更改（1）', exact=True).click()
    expect(page.get_by_role('button', name='保存更改', exact=True)).to_be_disabled()
    assert env_loader.read_env_file()['ROXY_API_BASE'] == 'http://127.0.0.1:55555'
    assert not errors


def test_account_task_submission_opens_complete_log(browser_page):
    page, base, errors = browser_page
    login(page, base, "/accounts")

    def submit_live(route):
        route.fulfill(
            status=202,
            content_type="application/json",
            body='{"ok":true,"message":"已入队 1 个查活任务","started":[{"id":1,"email":"demo0@example.test","status":"queued"}],"started_count":1}',
        )

    def latest_task(route):
        route.fulfill(
            content_type="application/json",
            body='{"ok":true,"task_ids":["account-901"],"items":[]}',
        )

    def complete_log(route):
        route.fulfill(
            content_type="application/json",
            body='{"ok":true,"complete":true,"job":{"id":"account-901","job_type":"live_check","email":"demo0@example.test","status":"running"},"log":"[查活] 已入队\\n[查活] 完整登录尝试 1/3\\n[查活] 完成：账号正常"}',
        )

    page.route("**/api/accounts/check-live-bulk", submit_live)
    page.route("**/api/accounts/tasks/latest**", latest_task)
    page.route("**/api/tasks/logs**", complete_log)
    row = page.locator("tr").filter(has_text="demo0@example.test").first
    row.get_by_role("button", name="查活", exact=True).click()
    operation = page.get_by_role("dialog", name="查活 / 刷新 AT")
    expect(operation).to_be_visible()
    operation.get_by_role("button", name="确认查活 / 刷新 AT", exact=True).click()
    log_dialog = page.get_by_role("dialog", name="查活任务日志")
    expect(log_dialog).to_be_visible()
    expect(log_dialog.locator(".log-content")).to_contain_text("完整登录尝试 1/3")
    expect(log_dialog).to_contain_text("完整日志")
    assert not errors


def test_account_plan_column_shows_promo_and_billing_detail(browser_page):
    from core import db
    page, base, errors = browser_page
    accounts = db.list_accounts(limit=10, archived="all")
    free_email = accounts[0]["email"]
    plus_email = accounts[1]["email"]
    db.update_account_plan_check(acc_id=accounts[0]["id"], result={
        "ok": True, "current_plan_type": "free", "plus_trial_eligible": True,
        "checked_at": "2026-02-01T10:00:00+00:00",
        "network_route": "proxy", "proxy_used": "http://127.0.0.1:7890",
        "eligible_promo_campaigns": {"plus": {"id": "plus-1-month-free", "metadata": {
            "plan_name": "chatgptplusplan", "discount": {"percentage": 100},
            "duration": {"num_periods": 1, "period": "month"},
            "no_auto_renewal_at_discount_end": False,
        }}},
    })
    db.update_account_plan_check(acc_id=accounts[1]["id"], result={
        "ok": True, "current_plan_type": "plus",
        "checked_at": "2026-02-01T10:00:00+00:00",
        "billing_period": "monthly", "billing_currency": "USD",
        "expires_at": "2026-03-01T00:00:00+00:00",
    })
    # 单独插入一个 free 且从未查过资格的账号，用于确认标签底色与“待查资格”提示。
    pending_email = "free-pending@example.test"
    db.insert_account(email=pending_email, access_token="private-at-pending",
                      plan_type="free", totp_secret="JBSWY3DPEHPK3PXP")
    login(page, base, "/accounts")

    free_row = page.locator("tr").filter(has_text=free_email).first
    free_tag = free_row.locator(".plan-tag")
    expect(free_tag).to_have_text("free")
    expect(free_tag).to_have_class(re.compile(r"plan-tag-free-active"))
    expect(free_row).to_contain_text("可试用 Plus")
    expect(free_row).to_contain_text("Plus：免费/1个月")

    free_row.get_by_role("button", name="优惠详情（1）").click()
    promo_dialog = page.get_by_role("dialog", name="可用套餐优惠")
    expect(promo_dialog).to_contain_text("plus-1-month-free")
    expect(promo_dialog).to_contain_text("100%")
    expect(promo_dialog).to_contain_text("1 个月")
    promo_dialog.get_by_role("button", name="关闭对话框").click()

    plus_row = page.locator("tr").filter(has_text=plus_email).first
    plus_tag = plus_row.locator(".plan-tag")
    expect(plus_tag).to_have_text("plus")
    expect(plus_tag).to_have_class(re.compile(r"plan-tag-paid"))
    expect(plus_row).to_contain_text("月付")
    expect(plus_row).to_contain_text("USD")
    expect(plus_row).to_contain_text("到期")

    # free 且未查资格：标签仍是 free，但换成中性的未确认底色。
    pending_row = page.locator("tr").filter(has_text=pending_email).first
    pending_tag = pending_row.locator(".plan-tag")
    expect(pending_tag).to_have_text("free")
    expect(pending_tag).to_have_class(re.compile(r"plan-tag-free-idle"))
    expect(pending_row).to_contain_text("待查资格")
    assert tag_background(pending_tag) != tag_background(free_tag)
    assert not errors


def tag_background(tag):
    return tag.evaluate("el => getComputedStyle(el).backgroundColor")


def test_redemption_recovers_after_lost_response(browser_page):
    from core import db
    page, base, errors = browser_page
    code = db.create_redeem_code(quantity=2, account_group='默认分组')['code']
    page.goto(base + '/redeem')
    page.get_by_label('兑换码', exact=True).fill(code)
    def lose_response(route):
        response = route.fetch()
        assert response.ok
        route.abort('failed')
    page.route('**/api/redeem', lose_response, times=1)
    page.get_by_role('button', name='兑换 / 恢复原结果', exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('结果可能已受理')
    assert db.list_redeem_codes()[0]['redeemed_count'] == 1
    original_storage = page.evaluate('JSON.stringify(sessionStorage)')
    page.get_by_role('button', name='兑换 / 恢复原结果', exact=True).click()
    expect(page.get_by_role('heading', name='已恢复原批次 · 1 个账号')).to_be_visible()
    assert db.list_redeem_codes()[0]['redeemed_count'] == 1
    assert original_storage.replace('false', 'true') == page.evaluate('JSON.stringify(sessionStorage)')
    assert not errors


def test_task_center_submits_manual_email_otp(browser_page, monkeypatch):
    """手动验证码模式下，任务中心必须能在任务旁输入并提交 6 位验证码。"""
    from config import email as email_config
    from core import db, manual_otp

    page, base, errors = browser_page
    monkeypatch.setattr(email_config, 'USE_EMAIL_SERVICE', False)
    job = db.create_job('manual')
    db.update_job(int(job['id']), status='running', email='manual-otp@example.test',
                  stage='等待邮箱验证码', progress_message='请打开邮箱提交 6 位验证码', progress=50)
    login(page, base, '/tasks')
    row = page.locator('tr').filter(has_text='manual-otp@example.test').first
    expect(row).to_be_visible()
    code_input = row.get_by_label('邮箱验证码')
    code_input.fill('123456')
    # 任务列表每 5 秒自动刷新一次，输入中的验证码不能被刷新清空。
    page.wait_for_timeout(5600)
    assert code_input.input_value() == '123456'
    row.get_by_role('button', name='提交验证码', exact=True).click()
    expect(page.get_by_text('验证码已提交')).to_be_visible()
    assert manual_otp.pop_manual_otp('manual-otp@example.test') == '123456'
    assert not errors
