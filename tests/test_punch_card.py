"""The hidden Punch Card command: the line at the cursor as an IBM 029
punched card, found only by typing its name in the Command Palette."""

from __future__ import annotations

import pytest
from conftest import open_editor
from qtpy.QtGui import QAction

from vibefpmgui.command_palette import COMMAND_ROLE, Command, revealed
from vibefpmgui.punch_card import PunchCard, PunchCardDialog, card_text, holes


@pytest.mark.parametrize("ch, rows", [
    ("0", (0,)), ("7", (7,)), ("A", (12, 1)), ("I", (12, 9)), ("J", (11, 1)), ("R", (11, 9)), ("S", (0, 2)),
    ("Z", (0, 9)), ("f", (12, 6)),  # (lower case: as upper, the 029 had no other)
    (" ", ()), ("&", (12,)), ("-", (11,)), ("/", (0, 1)), (".", (12, 3, 8)), ("(", (12, 5, 8)),
    (")", (11, 5, 8)), ("*", (11, 4, 8)), (",", (0, 3, 8)), ("=", (6, 8)), ("'", (5, 8)), ('"', (7, 8)),
    (":", (2, 8)), ("!", (11, 2, 8)),
])
def test_the_029_code(ch, rows):
    assert holes(ch) == rows


def test_what_it_can_t_punch():
    assert [holes(ch) for ch in "[]{}~^\\`é"] == [None] * 9


def test_a_line_as_punched():
    punched, unpunchable, left_off = card_text("\tx = a[1] ! " + "y" * 80)
    assert punched.startswith("        X = A[1] ! YYY") and len(punched) == 80  # (a tab: to column 9; upper case)
    assert unpunchable == ["[", "]"]
    assert left_off == "y" * 19  # (20 columns before the y's: 19 of them on the next card, as typed)


def test_the_holes_drawn(qtbot):
    card = PunchCard("C   1")
    qtbot.addWidget(card)
    card.resize(1000, 440)
    holes_drawn = {(column, row): rect for column, row, rect in card.hole_rects()}
    assert sorted(holes_drawn) == [(1, 3), (1, 12), (5, 1)]  # ("C": 12-3; "1": 1)
    zone, digit = holes_drawn[(1, 12)], holes_drawn[(1, 3)]
    assert zone.left() == digit.left() and zone.top() < digit.top()  # (one column: row 12 above row 3)
    assert holes_drawn[(5, 1)].left() > zone.left()


def test_only_found_by_typing_its_name():
    command = Command(("Punch Card",), QAction("Punch Card"))
    assert revealed("punch", command) and revealed("Punch Card", command) and revealed("punchcard", command)
    assert not revealed("", command) and not revealed("punc", command) and not revealed("card", command)


def test_in_the_command_palette_and_on_the_cursor_s_line(window, project, monkeypatch, qtbot):
    editor = open_editor(window, project / "src/foo.f90")
    editor.editor.go_to_line(5)
    shown = []
    monkeypatch.setattr(PunchCardDialog, "exec", lambda self: shown.append((self.index, self.card.text)) or 0)
    window.show_command_palette()
    palette = window._command_palette
    assert not any("Punch" in c.title for c in palette.commands)  # (in no menu)

    def listed() -> list[str]:
        return [palette.list.item(i).data(COMMAND_ROLE).name for i in range(palette.list.count())]

    for typed in ("", "punc", "card"):
        palette.field.setText(typed)
        assert "Punch Card" not in listed()  # (not shown: not enough of its name)
    palette.field.setText("punch")
    assert listed()[0] == "Punch Card"
    palette.run(palette.list.item(0).data(COMMAND_ROLE))  # (run once the palette has closed)
    qtbot.waitUntil(lambda: bool(shown), timeout=3000)
    assert shown == [(4, "  SUBROUTINE BUMP()")]  # (line 5, as punched)
