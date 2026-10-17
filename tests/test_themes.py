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
    themes.chosen_style = themes.AUTOMATIC
    themes.apply(themes.SYSTEM)  # (the app is shared by the tests)


def app_style() -> str:
    return themes.style_name(QApplication.style()).lower()


def window_color() -> str:
    return QApplication.palette().color(QPalette.Window).name()


def test_colorful_themes_use_their_palette(window):
    native = themes.style_name(QApplication.style())
    window.set_theme("nord")
    assert window_color() == "#2e3440" and themes.style_name(QApplication.style()).lower() == "fusion"
    assert QApplication.palette().color(QPalette.Highlight) == QColor("#88c0d0")
    assert window.theme_actions["nord"].isChecked() and not window.theme_actions[themes.SYSTEM].isChecked()
    assert window.settings.value("app_theme") == "nord"
    window.set_theme(themes.SYSTEM)  # (back: the platform's look again)
    assert themes.style_name(QApplication.style()).lower() == native.lower() and window_color() != "#2e3440"
    assert isinstance(QApplication.style(), themes.StillStyle)


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


def test_panels_never_animate(window):
    """(Qt's zero-length panel animations could loop forever on macOS.)"""
    from qtpy.QtWidgets import QStyle

    for key in (themes.SYSTEM, "nord"):
        window.set_theme(key)
        assert QApplication.style().styleHint(QStyle.SH_Widget_Animation_Duration) == 0
        assert window.style().styleHint(QStyle.SH_Widget_Animation_Duration, None, window) == 0


def test_every_panel_follows_the_theme(qtbot, window):
    """(The Coverage panel stayed dark in a light theme: it wasn't told.)
    Every panel with light and dark colors follows a switch."""
    from vibefpmgui.build_monitor import app_is_light

    panels = [dock.widget() for dock in window.docks.values() if hasattr(dock.widget(), "is_light")]
    assert window.coverage_panel in panels
    for key in ("nord", "ocean", "nord"):  # (dark, light, dark)
        window.set_theme(key)
        qtbot.wait(50)  # (the windows are told of the new palette through the event loop)
        light = app_is_light()
        assert light == (not themes.THEMES[key].dark)
        wrong = [type(p).__name__ for p in panels if p.is_light != light]
        assert not wrong, (key, wrong)


# ------------------------------------------------------------ View → Window Style
# (Fusion and Qt's plain Windows style are on every platform; the platform's own
# style is whatever it is here, Fusion on the tests' offscreen screen.)
def test_choosing_a_window_style(window):
    assert {"fusion", "windows"} <= {name.lower() for name in themes.styles()}
    native = themes.native_style().lower()
    window.set_window_style("Windows")
    assert app_style() == "windows" and window.settings.value("app_style") == "Windows"
    window.set_theme("light")  # (System / Light / Dark: the chosen style all the same)
    assert app_style() == "windows"
    window.set_window_style(themes.AUTOMATIC)  # (the theme's again: the platform's)
    assert app_style() == native
    window.set_theme("nord")  # (a colorful theme: Fusion, for its colors)
    assert app_style() == "fusion"
    window.set_window_style("Windows")
    assert app_style() == "windows" and window_color() == "#2e3440"  # (its palette still set)


def test_the_window_style_is_remembered(make_window, project, settings_file):
    settings_file.write_text("[General]\nfortls=false\nlint=false\napp_style=Windows\n")
    make_window(project / "fpm.toml")
    assert themes.chosen_style == "Windows" and app_style() == "windows"


def test_a_style_this_platform_hasn_t_is_automatic(make_window, project, settings_file):
    settings_file.write_text("[General]\nfortls=false\nlint=false\napp_style=NoSuchStyle\n")
    make_window(project / "fpm.toml")  # (e.g. "macOS" saved, then opened on Windows)
    assert app_style() == themes.native_style().lower()


def test_the_window_style_menu(window):
    window._fill_window_style_menu()
    actions = [a for a in window.window_style_menu.actions() if a.text()]
    assert actions[0].data() == themes.AUTOMATIC and actions[0].isChecked()
    assert [a.data() for a in actions[1:]] == themes.styles() and "Fusion" in themes.styles()
    next(a for a in actions if a.data() == "Windows").trigger()
    assert app_style() == "windows"
    window._fill_window_style_menu()
    assert next(a for a in window.window_style_menu.actions() if a.data() == "Windows").isChecked()
