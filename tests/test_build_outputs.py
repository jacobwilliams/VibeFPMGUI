"""The Build outputs panel's right-click menus."""

from __future__ import annotations

from pathlib import Path

import pytest
from qtpy.QtWidgets import QApplication

from vibefpmgui import build_outputs as bo
from vibefpmgui.compat import reveal_label


@pytest.fixture
def card(qtbot, tmp_path):
    folder = tmp_path / "build" / "gfortran_0123456789ABCDEF"
    (folder / "app").mkdir(parents=True)
    program = folder / "app" / "hello"
    program.write_text("")
    lib = folder / "libdemo.a"
    lib.write_text("")
    build = bo.BuildFolder(folder, "gfortran", programs=[("program", program)], libraries=[lib])
    config = bo.Configuration("debug · gfortran", [build], True, "-O0 -g", current=True)
    card = bo.ConfigCard(config, bo.PALETTES["light"])
    qtbot.addWidget(card)
    card.show()
    qtbot.waitExposed(card)
    return card


def menu_at(card, kind: str):
    label = next(w for w, (k, _) in card.rows.items() if k == kind)
    return card.context_menu(label.geometry().center())


def actions(menu) -> dict:
    return {a.text(): a for a in menu.actions() if a.text()}


def test_a_program_s_menu(qtbot, card):
    program = str(card.config.programs[0][1])
    menu = actions(menu_at(card, "program"))
    assert list(menu) == ["Run", "Run in Terminal", "Debug", "Run with Valgrind", reveal_label(),
                          "Copy Path"]
    for text, signal in (("Run", card.run_requested), ("Run in Terminal", card.run_in_terminal_requested),
                         (reveal_label(), card.reveal_requested)):
        with qtbot.waitSignal(signal) as blocker:
            menu[text].trigger()
        assert blocker.args == [program]
    menu["Copy Path"].trigger()
    assert QApplication.clipboard().text() == program
    with qtbot.waitSignal(card.debug_requested) as blocker:
        menu["Debug"].trigger()
    assert blocker.args == ["program", Path(program).name]


def test_a_debug_button_beside_run(qtbot, card):
    from qtpy.QtWidgets import QToolButton

    buttons = {b.text(): b for b in card.findChildren(QToolButton)}
    assert {"Run", "Debug"} <= set(buttons)
    with qtbot.waitSignal(card.debug_requested) as blocker:
        buttons["Debug"].click()
    assert blocker.args == ["program", card.config.programs[0][1].name]


def test_a_program_s_name_without_exe():
    assert bo.executable_name(Path("build/x/app/demo.exe")) == "demo"
    assert bo.executable_name(Path("build/x/test/check_fast")) == "check_fast"


def test_a_library_s_menu(qtbot, card):
    menu = actions(menu_at(card, "library"))
    assert list(menu) == [reveal_label(), "Copy Path"]  # (nothing to run)
    menu["Copy Path"].trigger()
    assert QApplication.clipboard().text().endswith("libdemo.a")


def test_the_configuration_s_menu(qtbot, card):
    menu = actions(card.context_menu(card.rect().topLeft()))
    assert list(menu) == [reveal_label(), "Copy Flags", "Delete…"]
    with qtbot.waitSignal(card.reveal_requested) as blocker:
        menu[reveal_label()].trigger()
    assert blocker.args == [str(card.config.folders[0].path)]
    menu["Copy Flags"].trigger()
    assert QApplication.clipboard().text() == "-O0 -g"
    with qtbot.waitSignal(card.delete_requested) as blocker:
        menu["Delete…"].trigger()
    assert blocker.args == [card.config]
