"""The test runner wrapper, Go to Symbol, About, platform helpers, ANSI
colors and the program's start."""

from __future__ import annotations

import shutil
import sys

import pytest
from qtpy.QtCore import Qt
from qtpy.QtGui import QColor
from qtpy.QtTest import QTest
from qtpy.QtWidgets import QApplication, QPlainTextEdit

from vibefpmgui import ansi, compat, lsp, test_runner
from vibefpmgui.about import AboutDialog, fpm_version, tool_version
from vibefpmgui.symbol_search import SymbolSearchDialog


# ------------------------------------------------------------ test runner
def test_runner_marks_start_and_end(capfd):
    code = test_runner.main([sys.executable, "-c", "print('hello from the test'); raise SystemExit(3)"])
    out = capfd.readouterr().out
    name = shutil.os.path.basename(sys.executable)
    assert code == 3
    assert f"@@vibefpm:start:{name.removesuffix('.exe')}@@" in out and "hello from the test" in out
    assert f"@@vibefpm:end:{name.removesuffix('.exe')}:3:" in out


def test_runner_with_an_inner_runner(capfd, monkeypatch):
    monkeypatch.setenv("VIBEFPM_INNER_RUNNER", f"{sys.executable} -c \"import sys; print('inner', sys.argv[1:])\"")
    assert test_runner.main(["prog", "arg"]) == 0
    assert "inner ['prog', 'arg']" in capfd.readouterr().out


def test_runner_problems(capfd):
    assert test_runner.main([]) == 2
    assert test_runner.main(["/no/such/program"]) == 127
    assert "could not run /no/such/program" in capfd.readouterr().out


# --------------------------------------------------------- go to symbol
class FakeServer:
    def __init__(self, project, running=True):
        self.project, self.running, self.asked = project, running, []

    def workspace_symbols(self, text, callback):
        self.asked.append(text)
        if not self.running:
            return False
        foo = lsp.to_uri((self.project / "src/foo.f90").resolve())
        found = [{"name": "bump", "kind": 12, "containerName": "foo",
                  "location": {"uri": foo, "range": {"start": {"line": 4, "character": 2}}}},
                 {"name": "bumper", "kind": 12, "location": {"uri": foo, "range": {"start": {"line": 9, "character": 0}}}},
                 {"name": "no location"}]
        callback([s for s in found if text.lower() in s["name"]])
        return True


def test_symbol_search(qtbot, project):
    server = FakeServer(project)
    dialog = SymbolSearchDialog(server, project)
    qtbot.addWidget(dialog)
    chosen = []
    dialog.chosen.connect(lambda *place: chosen.append(place))
    QTest.keyClicks(dialog.query, "bump")
    qtbot.waitUntil(lambda: dialog.results.count() == 2, timeout=2_000)
    assert dialog.results.item(0).text().startswith("foo::bump") and "src/foo.f90:5" in dialog.results.item(0).text()
    assert "2 matches" in dialog.note.text()
    QTest.keyClick(dialog.query, Qt.Key_Down)
    assert dialog.results.currentRow() == 1
    QTest.keyClick(dialog.query, Qt.Key_Return)
    assert chosen == [(str((project / "src/foo.f90").resolve()), 10, 0)]


def test_symbol_search_enter_before_the_answer(qtbot, project):
    dialog = SymbolSearchDialog(FakeServer(project), project)
    qtbot.addWidget(dialog)
    chosen = []
    dialog.chosen.connect(lambda *place: chosen.append(place))
    dialog.query.setText("bump")
    QTest.keyClick(dialog.query, Qt.Key_Return)  # (before the search ran: the best match)
    assert chosen and chosen[0][1] == 5
    dialog.query.setText("zzz")
    dialog._search()
    assert dialog.note.text() == "No matches."
    stopped = SymbolSearchDialog(FakeServer(project, running=False), project)
    qtbot.addWidget(stopped)
    stopped.query.setText("x")
    stopped._search()
    assert "isn't running" in stopped.note.text()


def test_go_to_symbol_needs_the_server(window):
    window.go_to_symbol()  # (fortls is off in the tests: nothing happens)


# ----------------------------------------------------------------- about
def test_about(qtbot, monkeypatch):
    fpm = shutil.which("fpm") or "fpm"
    dialog = AboutDialog(fpm, "gfortran", "fpm's default")
    qtbot.addWidget(dialog)
    from qtpy.QtWidgets import QPushButton

    copy = next(b for b in dialog.findChildren(QPushButton) if b.text() == "Copy Versions")
    copy.click()  # (the versions as plain text, for bug reports)
    text = QApplication.clipboard().text()
    assert text.startswith("VibeFPMGUI ") and "Compiler:" in text and "fpm's default" in text
    assert copy.text() == "Copied"
    assert dialog.windowTitle() == "About VibeFPMGUI"
    assert tool_version(["/no/such/tool", "--version"]) == ""
    if shutil.which("fpm"):
        assert fpm_version(fpm)[0].isdigit()
    AboutDialog._copy("copied text")
    assert QApplication.clipboard().text() == "copied text"


def test_about_from_the_window(window, monkeypatch):
    shown = []
    monkeypatch.setattr(AboutDialog, "exec", lambda self: shown.append(self.windowTitle()) or 0)
    window.show_about()
    assert shown == ["About VibeFPMGUI"]
    assert window.effective_compiler()[1] in ("fpm's default", "FPM_FC environment variable",
                                              "Compiler field in Build options")


# -------------------------------------------------------- platform helpers
def test_compat():
    assert compat.split_args('a "b c" d') == ["a", "b c", "d"]
    assert compat.split_args(compat.join_args(["x y", "z"])) == ["x y", "z"]
    assert compat.split_args(compat.quote_arg("it's")) == ["it's"]
    assert compat.file_manager_name() and compat.reveal_label()
    compat.set_windows_app_id()  # (a no-op off Windows)


# ------------------------------------------------------------ ANSI colors
def colors_of(edit) -> dict[str, tuple]:
    out, it = {}, edit.document().firstBlock().begin()
    while not it.atEnd():
        fmt = it.fragment().charFormat()
        out[it.fragment().text()] = (fmt.foreground().color(), fmt.background().color(), fmt.fontWeight() > 500,  # (Qt 6: 400 normal, 700 bold)
                                     fmt.fontItalic(), fmt.fontUnderline())
        it += 1
    return out


def test_ansi_codes(qtbot):
    edit = QPlainTextEdit()
    qtbot.addWidget(edit)
    writer = ansi.AnsiWriter(edit)
    writer.write("\x1b[1;3;4mfancy\x1b[22;23;24m plain\x1b[38;5;196mred256\x1b[39m"
                 "\x1b[38;2;10;20;30mtrue\x1b[48;5;21mbg\x1b[49;0m end\x1b[0m")
    colors = colors_of(edit)
    assert colors["fancy"][2:] == (True, True, True)
    assert colors[" plain"][2:] == (False, False, False)
    assert colors["red256"][0] == QColor(255, 0, 0)
    assert colors["true"][0] == QColor(10, 20, 30)
    writer.write("\x1b[31mnot colored", ansi=False)
    assert "\x1b[31mnot colored" in edit.toPlainText()


# ---------------------------------------------------------- program start
def test_main_opens_a_window(project, monkeypatch):
    from vibefpmgui import main_window

    opened = []
    monkeypatch.setattr(QApplication, "exec", lambda *a: 0)
    real_init = main_window.MainWindow.__init__

    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(main_window.MainWindow, "__init__", init)
    assert main_window.main(["vibefpmgui", str(project)]) == 0  # (a folder: its fpm.toml)
    window = opened[0]
    assert window.manifest_path.name == "fpm.toml" and window.remember_last
    window.close()
    assert main_window.main(["vibefpmgui", "--new-window", str(project / "fpm.toml")]) == 0
    assert opened[1].remember_last is False
    opened[1].close()


@pytest.mark.parametrize("text", ["", "x"])
def test_split_runner(text):
    from vibefpmgui.main_window import split_runner

    args, runner = split_runner(["--runner", "mpirun -np 2", "--flag", text] if text else [])
    assert runner == ("mpirun -np 2" if text else "")
    assert "--runner" not in args


def test_about_logo_changes_color_while_shown(qtbot, monkeypatch):
    import re

    from vibefpmgui import icons
    from vibefpmgui.about import AnimatedLogo

    def fills(svg: bytes) -> list[str]:
        return re.findall(r"fill:(#[0-9a-f]{6})", svg.decode())

    original = fills(icons.LOGO_PATH.read_bytes())
    assert len(original) == 5
    assert fills(icons.recolored_logo(0.0)) == fills(icons.recolored_logo(1.0)) == original
    assert all(a != b for a, b in zip(fills(icons.recolored_logo(0.3)), original))
    logo = AnimatedLogo(64)
    qtbot.addWidget(logo)
    assert not logo.timer.isActive()
    monkeypatch.setattr(AnimatedLogo, "PERIOD", 1.0)
    logo.show()
    qtbot.waitExposed(logo)
    assert logo.timer.isActive()
    qtbot.waitUntil(lambda: logo.turn > 0.1, timeout=2_000)
    assert not logo.grab().isNull()
    logo.hide()
    assert not logo.timer.isActive()  # (no work when it isn't seen)


def test_about_qt_in_the_help_menu(window, monkeypatch):
    from qtpy.QtGui import QAction
    from qtpy.QtWidgets import QMessageBox

    shown = []
    monkeypatch.setattr(QMessageBox, "aboutQt", lambda parent, title: shown.append((parent, title)))
    assert window.about_qt_action.menuRole() == QAction.AboutQtRole
    window.about_qt_action.trigger()
    assert shown == [(window, "About Qt")]
