"""View → Theme: the whole window's colors."""

from __future__ import annotations

import pytest
from qtpy.QtGui import QColor, QPalette
from qtpy.QtWidgets import QApplication

from vibefpmgui import highlighter as hl
from vibefpmgui import themes


@pytest.fixture(autouse=True)
def back_to_system():
    yield
    themes.apply(themes.SYSTEM)  # (the app is shared by the tests)


def window_color() -> str:
    return QApplication.palette().color(QPalette.Window).name()


def test_colorful_themes_use_their_palette(window):
    native = QApplication.style().name()
    window.set_theme("nord")
    assert window_color() == "#2e3440" and QApplication.style().name().lower() == "fusion"
    assert QApplication.palette().color(QPalette.Highlight) == QColor("#88c0d0")
    assert window.theme_actions["nord"].isChecked() and not window.theme_actions[themes.SYSTEM].isChecked()
    assert window.settings.value("app_theme") == "nord"
    window.set_theme(themes.SYSTEM)  # (back: the platform's look again)
    assert QApplication.style().name().lower() == native.lower() and window_color() != "#2e3440"


def test_every_theme_applies(window):
    for key, theme in themes.THEMES.items():
        window.set_theme(key)
        assert themes.current == key
        if not theme.native:
            assert window_color() == QColor(theme.window).name()
            dark = QApplication.palette().color(QPalette.Window).lightness() < 128
            assert dark == theme.dark  # (light or dark, as it says)
            text = QApplication.palette().color(QPalette.Text)
            base = QApplication.palette().color(QPalette.Base)
            assert abs(text.lightness() - base.lightness()) > 100  # (readable)


def test_automatic_syntax_colors_follow_the_theme(window, project):
    from conftest import open_editor

    editor = open_editor(window, project / "src/foo.f90")
    hl.chosen_style = hl.AUTO
    window.set_theme("dracula")
    assert hl.style_name(QApplication.palette()) == "dracula"
    assert editor.highlighter._style_name == "dracula"
    window.set_syntax_style("monokai")  # (chosen: the theme doesn't change it)
    window.set_theme("solarized-light")
    assert editor.highlighter._style_name == "monokai"
    window.set_syntax_style(hl.AUTO)


def test_remembered(make_window, project, settings_file):
    settings_file.write_text("[General]\nfortls=false\nlint=false\napp_theme=gruvbox-dark\n")
    window = make_window(project / "fpm.toml")
    assert themes.current == "gruvbox-dark" and window.theme_actions["gruvbox-dark"].isChecked()
    assert window_color() == "#282828"


def test_an_unknown_theme_is_system():
    themes.apply("no-such-theme")
    assert themes.current == themes.SYSTEM
