"""Offline value/selection contracts for the Selenium input adapter."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from selenium.webdriver.common.keys import Keys

from core import cloakbrowser_driver as cloak


class InputPage:
    def __init__(self):
        self.active = None
        self.keys = []
        self.keyboard = SimpleNamespace(press=self.press, type=self.type)

    def type(self, text, **kwargs):
        self.active.insert(text)

    def press(self, key, **kwargs):
        self.keys.append(key)
        node = self.active
        if key in ("Control+a", "Control+A", "Meta+a", "Meta+A"):
            # Meta+A is accepted on Linux without selecting the value.
            if key.startswith("Control"):
                node.start, node.end = 0, len(node.value)
        elif key == "Backspace":
            if node.start == node.end:
                node.start = max(0, node.start - 1)
            node.insert("")
        elif key == "Delete":
            if node.start == node.end:
                node.end = min(len(node.value), node.end + 1)
            node.insert("")
        elif key == "ArrowLeft":
            node.start = node.end = max(0, node.start - 1)
        elif key == "Enter":
            node.submitted += 1
        elif key.startswith("Shift+"):
            node.insert(key.removeprefix("Shift+").upper())
        elif "+" not in key:
            node.insert(key)


class InputNode:
    """Click collapses selection; native typing replaces only the selection."""

    def __init__(self, page, value="", *, fill_supported=True):
        self.page = page
        self.value = value
        self.start = self.end = len(value)
        self.fill_supported = fill_supported
        self.focusable = True
        self.clicks = 0
        self.submitted = 0
        self.type_error = None
        self.fill_calls = []

    def evaluate(self, expression, arg=None, **kwargs):
        # Model focus: keep an existing selection, append when newly focused.
        if not self.focusable:
            return False
        if self.page.active is not self:
            self.page.active = self
            self.start = self.end = len(self.value)
        return True

    def click(self, **kwargs):
        if not self.focusable:
            raise RuntimeError("input cannot be focused")
        self.clicks += 1
        self.page.active = self
        self.start = self.end = len(self.value)

    def fill(self, value, **kwargs):
        self.fill_calls.append(value)
        if not self.fill_supported:
            raise RuntimeError("element does not support fill")
        self.value = value
        self.start = self.end = len(value)

    def insert(self, text):
        self.value = self.value[:self.start] + text + self.value[self.end:]
        self.start += len(text)
        self.end = self.start

    def type(self, text, **kwargs):
        if self.type_error:
            raise self.type_error
        assert self.page.active is self
        self.insert(text)

    press_sequentially = type

    def press(self, key, **kwargs):
        assert self.page.active is self
        self.page.press(key)


@pytest.fixture(params=["locator", "handle"])
def field(request):
    page = InputPage()
    node = InputNode(page)
    element = cloak.CloakElement(page, **{request.param: node})
    return SimpleNamespace(page=page, node=node, element=element)


def test_chunks_append_instead_of_replacing_prior_input(field):
    for chunk in ("user", "@", "example", ".test"):
        field.element.send_keys(chunk)
    assert field.node.value == "user@example.test"
    assert field.node.fill_calls == []


@pytest.mark.parametrize("text", ["control@example.test", "COMMANDer42!", "Control", "command"])
def test_words_that_resemble_modifier_names_are_literal_text(field, text):
    field.element.send_keys(text)
    assert field.node.value == text
    assert field.page.keys == []


def test_send_keys_preserves_zero_and_empty_input_does_not_clear(field):
    field.element.send_keys("item", 0)
    field.element.send_keys("")
    assert field.node.value == "item0"


def test_separate_select_all_and_backspace_calls_keep_selection(field):
    field.node.value = "previous-value"
    field.element.send_keys(Keys.CONTROL, "a")
    field.element.send_keys(Keys.BACKSPACE)
    field.element.send_keys("new", "-value")
    assert field.node.value == "new-value"
    assert field.node.clicks == 0
    assert field.page.keys[:2] == ["Control+a", "Backspace"]


def test_select_all_then_replacement_matches_profile_spinbutton_sequence(field):
    field.node.value = "1990"
    field.element.send_keys(Keys.CONTROL, "a")
    field.element.send_keys("2001")
    assert field.node.value == "2001"


def test_already_focused_input_inserts_at_current_caret(field):
    field.node.value = "ac"
    field.page.active = field.node
    field.node.start = field.node.end = 1
    field.element.send_keys("b")
    assert field.node.value == "abc"


def test_first_input_appends_to_existing_value(field):
    field.node.value = "prefix:"
    field.node.start = field.node.end = 0
    field.element.send_keys("suffix")
    assert field.node.value == "prefix:suffix"


def test_keys_edit_and_submit_instead_of_inserting_private_characters(field):
    field.element.send_keys("abc", Keys.ARROW_LEFT, Keys.BACKSPACE, "X", Keys.DELETE, Keys.ENTER)
    assert field.node.value == "aX"
    assert field.node.submitted == 1
    assert field.page.keys == ["ArrowLeft", "Backspace", "Delete", "Enter"]


def test_null_releases_modifier_before_following_text(field):
    field.node.value = "old"
    field.element.send_keys(Keys.CONTROL, "a", Keys.NULL, "new")
    assert field.node.value == "new"


def test_modifiers_are_specific_and_end_with_each_call(field):
    field.element.send_keys(Keys.SHIFT, "ab")
    field.element.send_keys("c")
    field.element.send_keys(Keys.CONTROL, "c")
    field.element.send_keys(Keys.COMMAND, "a")
    assert field.node.value == "ABc"
    assert field.page.keys == ["Shift+a", "Shift+b", "Control+c", "Meta+a"]


def test_failed_focus_never_types_into_another_active_field(field):
    other = InputNode(field.page, "keep-me")
    field.page.active = other
    field.node.focusable = False
    field.node.fill_supported = False
    with pytest.raises(RuntimeError):
        field.element.send_keys("private-input")
    assert other.value == "keep-me"


def test_typing_error_is_not_retried_against_global_keyboard(field):
    field.node.type_error = RuntimeError("typing failed")
    with pytest.raises(RuntimeError, match="typing failed"):
        field.element.send_keys("secret")
    assert field.node.value == ""


def test_clear_uses_fill_without_extra_clicks(field):
    field.node.value = "old"
    field.element.clear()
    assert field.node.value == ""
    assert field.node.clicks == 0


@pytest.mark.parametrize("platform,expected", [("linux", "Control+a"), ("win32", "Control+a"), ("darwin", "Meta+a")])
def test_clear_fallback_uses_platform_select_all(field, monkeypatch, platform, expected):
    import sys

    monkeypatch.setattr(sys, "platform", platform)
    field.node.value = "old"
    field.node.fill_supported = False
    original_press = field.page.press

    def press(key, **kwargs):
        if key == "Meta+a" and platform == "darwin":
            field.page.keys.append(key)
            field.node.start, field.node.end = 0, len(field.node.value)
        else:
            original_press(key, **kwargs)

    field.page.press = press
    field.page.keyboard.press = press
    field.element.clear()
    assert field.node.value == ""
    assert field.page.keys == [expected, "Backspace"]


def test_shared_registration_typing_keeps_the_complete_value(field, monkeypatch):
    from core import roxy_registration as roxy

    field.node.value = "old-value"
    monkeypatch.setattr(roxy, "_browser_actions_enabled", lambda: True)
    monkeypatch.setattr(roxy, "_human_scroll_to", lambda *args: None)
    monkeypatch.setattr(roxy, "_human_click", lambda driver, el, **kwargs: el.click())
    monkeypatch.setattr(roxy, "human_delay", lambda *args: None)
    monkeypatch.setattr(roxy, "time", SimpleNamespace(sleep=lambda seconds: None))
    monkeypatch.setattr(roxy.random, "random", lambda: 0.5)
    monkeypatch.setattr("platform.system", lambda: "Linux")
    fallback = Mock(side_effect=AssertionError("native input should not need a JS setter"))
    monkeypatch.setattr(roxy, "_set_element_value", fallback)

    roxy._human_type_text(SimpleNamespace(execute_script=Mock()), field.element, "control@example.test")

    assert field.node.value == "control@example.test"
    fallback.assert_not_called()
