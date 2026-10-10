"""Task-local callbacks for manual verification; browser objects stay on their owning thread."""
from contextlib import contextmanager, nullcontext
import threading

_LOCAL = threading.local()


def _noop():
    return None


_DEFAULT_CALLBACKS = (nullcontext, _noop)


def current_callbacks():
    return getattr(_LOCAL, "callbacks", _DEFAULT_CALLBACKS)


def _noop_page(page):
    return None


def current_browser_bridge():
    return getattr(_LOCAL, "browser_bridge", _noop_page)


@contextmanager
def bind_callbacks(wait, checkpoint, browser_bridge=None):
    previous = current_callbacks()
    previous_bridge = current_browser_bridge()
    _LOCAL.callbacks = (wait, checkpoint)
    _LOCAL.browser_bridge = browser_bridge or _noop_page
    try:
        yield
    finally:
        _LOCAL.callbacks = previous
        _LOCAL.browser_bridge = previous_bridge
