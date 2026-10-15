"""Shared test setup.

The tests run with Qt's "offscreen" platform: no windows appear, and they
work on machines without a display (CI). To watch them, set
``QT_QPA_PLATFORM`` yourself, e.g. ``QT_QPA_PLATFORM=cocoa pixi run test``.

Every test gets its own settings file (``VIBEFPMGUI_SETTINGS``), so your own
settings are never read or changed, with the language server and linter off
(they are separate programs, and slow to start).
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # (before Qt is imported)

import pytest
from qtpy.QtCore import QCoreApplication, QEvent


@pytest.fixture(autouse=True)
def settings_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "settings.ini"
    path.write_text("[General]\nfortls=false\nlint=false\n")
    monkeypatch.setenv("VIBEFPMGUI_SETTINGS", str(path))
    return path


def write_files(root: Path, files: dict[str, str | bytes]) -> Path:
    """Create files (and their folders) under ``root``."""
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    return root


FOO_F90 = """\
module foo
  integer :: Count = 1
  integer :: count_all
contains
  subroutine bump()
    count = count + 1 ! count
  end subroutine
end module foo
"""


@pytest.fixture
def project(tmp_path) -> Path:
    """A small fpm project with a local dependency inside it, and files the
    search should leave out (ignored by git, hidden, build output, binary)."""
    return write_files(tmp_path / "proj", {
        "fpm.toml": 'name = "proj"\n[dependencies]\ndl = { path = "dep_local" }\n',
        ".gitignore": "out/\n*.log\n",
        "src/foo.f90": FOO_F90,
        "src/bin.dat": b"count\0binary",
        "test/check.f90": "program check\n  use foo\n  print *, count\nend program check\n",
        "app/main.f90": "program main\n  use foo\n  call bump()\nend program main\n",
        "out/x.f90": "count in an ignored folder\n",
        "run.log": "count in an ignored file\n",
        ".hidden/h.f90": "count in a hidden folder\n",
        "build/b.f90": "count in the build folder\n",
        "dep_local/fpm.toml": 'name = "dl"\n',
        "dep_local/src/dl.f90": "module dl\n  integer :: count\nend module dl\n",
    })


@pytest.fixture
def conflict_project(tmp_path) -> Path:
    """A project whose dependencies ask for test-drive in different ways
    (not downloaded: nothing here needs the network)."""
    url = "https://github.com/fortran-lang/test-drive"
    return write_files(tmp_path / "conflict", {
        "fpm.toml": f'name = "conflictdemo"\n[dependencies]\nlibA = {{ path = "libA" }}\n'
                    f'libB = {{ path = "libB" }}\ntest-drive = {{ git = "{url}", tag = "v0.5.0" }}\n',
        "libA/fpm.toml": f'name = "libA"\n[dependencies]\ntest-drive = {{ git = "{url}", tag = "v0.4.0" }}\n',
        "libB/fpm.toml": f'name = "libB"\n[dependencies]\ntest-drive = {{ git = "{url}.git", tag = "v0.5.0" }}\n',
    })


def close_window(window):
    """Close without "Save changes?" questions."""
    for editor in window.editors():
        editor.editor.document().setModified(False)
        editor._disk_newline = editor.newline  # (converted line endings are unsaved too)
        editor._disk_mixed = editor.mixed_newlines
    window.close()


@pytest.fixture
def make_window(qtbot):
    """make_window(manifest) -> a shown MainWindow, closed after the test."""
    from vibefpmgui.main_window import MainWindow

    windows = []

    def make(manifest: Path | str | None):
        window = MainWindow(str(manifest) if manifest else None)
        window.resize(1300, 850)
        window.show()
        qtbot.waitExposed(window)
        windows.append(window)
        return window

    yield make
    for window in windows:
        close_window(window)
        # Deleted, not only closed (hidden): a closed window lived on with
        # its thousands of widgets, and every later style or theme change
        # restyled them all (seconds per test, late in the run).
        window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.fixture
def window(make_window, project):
    """The main window with the sample project open."""
    w = make_window(project / "fpm.toml")
    return w


def open_editor(window, path: Path):
    """Open a file in the built-in editor; its EditorTab."""
    window.open_file(str(path), force_editor=True)
    editor = window.current_editor()
    assert editor is not None and editor.path == path.resolve()
    return editor
