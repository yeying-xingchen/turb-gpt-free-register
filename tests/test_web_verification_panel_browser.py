"""Built task-page smoke test with all HTTP requests intercepted locally."""
import base64
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit
import pytest


def test_built_web_panel_sends_scaled_human_click():
    playwright = pytest.importorskip("playwright.sync_api")
    public = Path(__file__).resolve().parents[1] / "frontend/.output/public"
    executable = Path("/usr/bin/chromium")
    if not public.is_dir() or not executable.is_file():
        pytest.skip("Build frontend and install local standard Chromium first")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, executable_path=str(executable))
        try:
            fixture = browser.new_page(viewport={"width": 800, "height": 600})
            fixture.set_content('<title>Local fixture</title><h1>Manual verification fixture</h1>')
            image = base64.b64encode(fixture.screenshot(type="jpeg")).decode("ascii")
            fixture.close()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            inputs = []
            def route_request(route):
                path = urlsplit(route.request.url).path
                if path.startswith('/api/'):
                    result = {"ok": True, "items": [], "total": 0, "status_counts": {}, "pools": []}
                    if path == '/api/auth/session': result = {"authenticated": True}
                    elif path == '/api/tasks/manual-verifications':
                        result = {"ok": True, "items": [{"account_id": 7, "email": "fixture@example.invalid", "waiting_since": "2026-01-01T12:00:00"}]}
                    elif path.endswith('/frame'):
                        result = {"ok": True, "pending": False, "frame": {"image": image, "width": 800, "height": 600, "revision": "fixture:1"}}
                    elif path.endswith('/input'):
                        inputs.append(route.request.post_data_json)
                    route.fulfill(status=200, content_type='application/json', body=json.dumps(result))
                    return
                file = (public / path.lstrip('/')).resolve()
                if not file.is_relative_to(public.resolve()):
                    route.fulfill(status=404, body=''); return
                if file.is_dir(): file = file / 'index.html'
                if not file.is_file(): file = public / 'index.html'
                content_type = mimetypes.guess_type(str(file))[0] or 'application/octet-stream'
                route.fulfill(status=200, content_type=content_type, body=file.read_bytes())
            page.route('**/*', route_request)
            page.goto('http://fixture.invalid/tasks')
            page.get_by_role('button', name='网页验证', exact=True).click(timeout=15000)
            image_element = page.locator('img.verification-image')
            image_element.wait_for(state='visible')
            page.wait_for_function("document.querySelector('img.verification-image')?.complete")
            bounds = image_element.bounding_box()
            with page.expect_response(lambda r: r.url.endswith('/input') and r.request.method == 'POST'):
                page.mouse.click(bounds['x'] + bounds['width'] / 2, bounds['y'] + bounds['height'] / 2)
            assert len(inputs) == 1
            assert inputs[0]['revision'] == 'fixture:1'
            assert abs(inputs[0]['x'] - 400) <= 1
            assert abs(inputs[0]['y'] - 300) <= 1
            assert page.get_by_text('后台浏览器保持同一验证会话，无需桌面窗口。验证码请自己点击。', exact=True).is_visible()
        finally:
            browser.close()
