"""Exercise the web bridge against a real headless browser and a local fixture.
All browser requests are intercepted; no third-party authentication is performed.
"""
import base64
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock
import pytest
from flask import Flask
from core import live_check_service as service, task_control, task_center_store
from webui.task_routes import register_task_routes


def test_real_headless_frame_click_and_cleanup(monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    executable = Path("/usr/bin/chromium")
    if not executable.is_file():
        pytest.skip("Local standard Chromium is unavailable")
    monkeypatch.setattr(service._EXECUTOR, "park_current", lambda: nullcontext())
    monkeypatch.setattr(task_center_store, "set_task_control_state", Mock())
    monkeypatch.setattr(service, "_append_log", Mock())
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_task_routes(app)
    client = app.test_client()
    account_id = 987654
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True, executable_path=str(executable))
            try:
                page = browser.new_page(viewport={"width": 800, "height": 600})
                page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body='''
                    <title>Just a moment... — local test fixture</title>
                    <button id="fixture" style="margin:40px;width:180px;height:60px"
                      onclick="document.title='Fixture completed';this.textContent='Done'">Local fixture confirmation</button>
                '''))
                page.goto("https://chatgpt.com/auth/login")
                with service._manual_wait(account_id, "fixture@example.invalid"):
                    endpoint = f"/api/tasks/manual-verifications/{account_id}"
                    assert client.get(endpoint + "/frame").status_code == 202
                    service._pump_manual_browser(account_id, page)
                    response = client.get(endpoint + "/frame")
                    assert response.status_code == 200
                    frame = response.get_json()["frame"]
                    assert base64.b64decode(frame["image"]).startswith(b"\xff\xd8")
                    assert (frame["width"], frame["height"]) == (800, 600)
                    bounds = page.locator("#fixture").bounding_box()
                    response = client.post(endpoint + "/input", json={
                        "revision": frame["revision"],
                        "x": bounds["x"] + bounds["width"] / 2,
                        "y": bounds["y"] + bounds["height"] / 2,
                    })
                    assert response.status_code == 200
                    assert "Just a moment" in page.title()  # route only queues the human input
                    service._pump_manual_browser(account_id, page)
                    assert page.title() == "Fixture completed"
                    assert page.locator("#fixture").inner_text() == "Done"
                    assert service._REMOTE_VERIFICATIONS[account_id]["frame"] is None
                assert client.get(endpoint + "/frame").status_code == 409
                assert account_id not in service._REMOTE_VERIFICATIONS
            finally:
                browser.close()
    finally:
        task_control.release(service.KIND, account_id)
