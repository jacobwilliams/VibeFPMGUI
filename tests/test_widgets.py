"""Small shared widgets: the history field (the Debug panel's evaluate field)."""

from __future__ import annotations

import pytest
from qtpy.QtCore import Qt
from qtpy.QtTest import QTest

from vibefpmgui.widgets import HistoryLineEdit


@pytest.fixture
def field(qtbot):
    field = HistoryLineEdit()
    qtbot.addWidget(field)
    return field


def send(field, text: str):
    field.setText(text)
    QTest.keyClick(field, Qt.Key_Return)


def keys(field, *pressed) -> str:
    for key in pressed:
        QTest.keyClick(field, key)
    return field.text()


def test_enter_sends_the_text_and_clears_the_field(qtbot, field):
    sent = []
    field.submitted.connect(sent.append)
    send(field, "  a(2:3)  ")
    assert sent == ["a(2:3)"] and field.text() == ""
    send(field, "   ")  # (nothing to send: nothing remembered)
    assert sent == ["a(2:3)"] and field.history == ["a(2:3)"]


def test_up_and_down_go_through_what_was_sent(field):
    for text in ("n", "p%x", "a(1)"):
        send(field, text)
    assert keys(field, Qt.Key_Up) == "a(1)"
    assert keys(field, Qt.Key_Up, Qt.Key_Up) == "n"
    assert keys(field, Qt.Key_Up) == "n"  # (the first: stays)
    assert keys(field, Qt.Key_Down) == "p%x"
    assert keys(field, Qt.Key_Down, Qt.Key_Down) == ""  # (past the last: the empty field again)
    assert keys(field, Qt.Key_Down) == ""


def test_what_was_being_typed_comes_back(field):
    send(field, "n")
    field.setText("half typ")
    assert keys(field, Qt.Key_Up) == "n"
    assert keys(field, Qt.Key_Down) == "half typ"


def test_a_recalled_entry_sent_again_goes_last_once(field):
    for text in ("n", "p%x"):
        send(field, text)
    keys(field, Qt.Key_Up, Qt.Key_Up)
    QTest.keyClick(field, Qt.Key_Return)  # ("n" again)
    send(field, "n")  # (the same twice in a row: kept once)
    assert field.history == ["n", "p%x", "n"]
    assert keys(field, Qt.Key_Up) == "n" and keys(field, Qt.Key_Up) == "p%x"


def test_each_field_has_its_own_history(qtbot, field):
    other = HistoryLineEdit()
    qtbot.addWidget(other)
    send(field, "n")
    assert other.history == [] and keys(other, Qt.Key_Up) == ""
