"""Offline coverage for liveness data saving without traffic request tracking."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import browser_data_saver as module


@pytest.fixture
def saver_factory(monkeypatch):
    monkeypatch.setattr(module._cfg, "BROWSER_DATA_SAVER_MODE", True)
    monkeypatch.setattr(module._cfg, "BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", ["image"])
    monkeypatch.setattr(module._cfg, "BROWSER_DATA_SAVER_BLOCKED_URL_PATTERNS", ["**/avatar.png"])

    def create(**kwargs):
        context = Mock()
        saver = module.BrowserDataSaver(label="offline", **kwargs)
        saver.install_playwright(context)
        handler = context.route.call_args.args[1]
        return saver, context, handler

    return create


def route_for(url="https://static.example/avatar.png", resource_type="image"):
    return SimpleNamespace(
        request=SimpleNamespace(url=url, resource_type=resource_type),
        abort=Mock(), continue_=Mock(),
    )


def test_disabled_tracking_keeps_counts_without_accumulating_ids(saver_factory):
    saver, _, handler = saver_factory(track_blocked_requests=False)
    routes = [route_for() for _ in range(100)]
    for route in routes:
        handler(route)
        route.abort.assert_called_once_with("blockedbyclient")
        route.continue_.assert_not_called()
    assert saver._blocked_playwright_requests == set()
    assert all(not saver.was_playwright_blocked(route.request) for route in routes)
    snapshot = saver.snapshot()
    assert snapshot["data_saver_blocked_count"] == 100
    assert snapshot["data_saver_blocked_by_type"] == {"image": 100}
    assert snapshot["data_saver_blocked_by_url_pattern"] == {"**/avatar.png": 100}


@pytest.mark.parametrize("kwargs", [{}, {"track_blocked_requests": True}])
def test_default_and_explicit_tracking_preserve_one_time_consumption(saver_factory, kwargs):
    saver, _, handler = saver_factory(**kwargs)
    route = route_for()
    handler(route)
    assert saver._blocked_playwright_requests == {id(route.request)}
    assert not saver.was_playwright_blocked(route_for().request)
    assert saver.was_playwright_blocked(route.request)
    assert not saver.was_playwright_blocked(route.request)
    assert saver.snapshot()["data_saver_blocked_count"] == 1


@pytest.mark.parametrize("tracking", [False, True])
def test_stop_clears_tracking_unroutes_once_and_preserves_counts(saver_factory, tracking):
    saver, context, handler = saver_factory(track_blocked_requests=tracking)
    route = route_for()
    handler(route)
    saver.stop()
    saver.stop()
    context.unroute.assert_called_once_with("**/*", handler)
    assert saver._blocked_playwright_requests == set()
    assert saver._context is None
    assert saver._route_handler is None
    assert not saver.was_playwright_blocked(route.request)
    assert saver.snapshot()["data_saver_blocked_count"] == 1
    late_route = route_for()
    handler(late_route)
    late_route.continue_.assert_called_once()
    late_route.abort.assert_not_called()
    assert saver.snapshot()["data_saver_blocked_count"] == 1


def test_stop_cleans_references_even_if_unroute_fails(saver_factory):
    saver, context, handler = saver_factory()
    handler(route_for())
    context.unroute.side_effect = RuntimeError("context closed")
    saver.stop()
    saver.stop()
    context.unroute.assert_called_once_with("**/*", handler)
    assert saver._blocked_playwright_requests == set()
    assert saver._context is None
    assert saver._route_handler is None


def test_disabled_tracking_still_allows_challenges_and_critical_requests(saver_factory):
    saver, _, handler = saver_factory(track_blocked_requests=False)
    for route in (
        route_for("https://auth.example/challenge/avatar.png"),
        route_for("https://auth.example/app.js", "script"),
        route_for("https://auth.example/api/auth/session", "fetch"),
    ):
        handler(route)
        route.continue_.assert_called_once()
        route.abort.assert_not_called()
    assert saver.snapshot()["data_saver_blocked_count"] == 0
