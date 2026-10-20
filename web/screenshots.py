"""Take the website's screenshots from the real app (in its Dark theme).

The window is drawn off screen (it never appears, and never takes the
focus). Run it on a *copy* of the demo project (it builds and tests it, makes
it a git repository, and asks fortls and Ollama about it), with its own
settings file so yours aren't touched:

    rm -rf /tmp/shots && mkdir /tmp/shots && cp -R examples/demo /tmp/shots/demo
    VIBEFPMGUI_SETTINGS=/tmp/shots/settings.ini pixi run python web/screenshots.py /tmp/shots/demo web/img

Give shot names (e.g. ``editor completion``) to take just those. The
debugging and Valgrind shots need gdb and Valgrind (Linux): take those in the
Docker container (docker/README.md), with the same command and
``QT_QPA_PLATFORM=offscreen``:

    docker/dev.sh run bash -c "'rm -rf /tmp/shots && mkdir /tmp/shots && cp -R examples/demo /tmp/shots/demo && \
        QT_QPA_PLATFORM=offscreen VIBEFPMGUI_SETTINGS=/tmp/shots/settings.ini \
        python web/screenshots.py /tmp/shots/demo web/img debug valgrind'"

Paths shown in the app are shortened (``~/projects/demo``) before each
capture, and a screenshot is refused (not saved) if any text in the window
still names this computer's user, the computer, or the home folder.
"""
import getpass
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

from qtpy.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer
from qtpy.QtGui import QPainter, QPixmap, QTextCursor
from qtpy.QtTest import QTest
from qtpy.QtWidgets import QApplication, QMainWindow, QWidget

# The terminal's shell: no startup files, a plain prompt (just the folder).
os.environ["SHELL"] = shutil.which("zsh") or shutil.which("bash") or "/bin/sh"
# Commits in the demo's repository: a made-up author, not this computer's user.
for key in ("GIT_AUTHOR", "GIT_COMMITTER"):
    os.environ[f"{key}_NAME"], os.environ[f"{key}_EMAIL"] = "Demo", "demo@example.com"

app = QApplication(sys.argv[:1])


class OffScreen(QObject):
    """Every other window (dialogs, popups) drawn off screen too: set as Qt
    prepares it, before it's shown, so none appears or takes the focus."""

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Polish and isinstance(obj, QWidget) and obj.isWindow()
                and not isinstance(obj, QMainWindow)):  # (the main window: set when it's made, below)
            obj.setAttribute(Qt.WA_DontShowOnScreen)
        return False


off_screen = OffScreen()
app.installEventFilter(off_screen)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # (the project's folder: the app's package)
from vibefpmgui import hover_card
from vibefpmgui import lsp as lsp_mod
from vibefpmgui.main_window import MainWindow
from vibefpmgui.rename_dialog import RenamePreviewDialog

P = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
OUT.mkdir(parents=True, exist_ok=True)
ONLY = set(sys.argv[3:])
MAX_W = 2400  # px; larger screenshots are scaled down
LINUX_ONLY = {"debug", "valgrind"}  # (gdb, Valgrind: taken in the container)
if not ONLY and sys.platform != "linux":
    print("(debug and valgrind: take them on Linux, e.g. in the Docker container; see the docstring)")

# Start clean: no saved settings, no build configurations or session, nothing built yet (dependencies stay).
if os.environ.get("VIBEFPMGUI_SETTINGS"):
    Path(os.environ["VIBEFPMGUI_SETTINGS"]).unlink(missing_ok=True)
(P / "vibefpm.json").unlink(missing_ok=True)
if not ONLY or ONLY & LINUX_ONLY:
    for d in (P / "build").glob("gfortran_*"):
        shutil.rmtree(d)
    (P / "build" / "compile_commands.json").unlink(missing_ok=True)

# Paths shown in the screenshots: no scratch folders or home directory.
HOME = str(Path.home())
REPO = str(Path(__file__).resolve().parents[1])
SCRUB = [(str(P.parent) + "/", "~/projects/"), ("/private" + str(P.parent) + "/", "~/projects/"),
         (REPO + "/.pixi/envs/default/bin/", ""), (REPO + "/", "~/VibeFPMGUI/"), (HOME, "~")]


def forbidden_names() -> list[str]:
    """What must not be in a screenshot: this computer's user and its names."""
    names = {getpass.getuser(), HOME, socket.gethostname().split(".")[0]}
    if sys.platform == "darwin":
        for what in ("ComputerName", "LocalHostName"):
            try:
                names.add(subprocess.run(["scutil", "--get", what], capture_output=True, text=True,
                                         check=False).stdout.strip())
            except OSError:
                pass
    variants = set()
    for name in filter(None, names):
        variants |= {name, name.replace("’", "'"), name.replace(" ", "-")}
    return sorted(v for v in variants if len(v) > 2 and v not in ("/", "~"))


FORBIDDEN = forbidden_names()


def scrub_text(text: str) -> str:
    text = text.replace("\u200b", "")
    for old, new in SCRUB:
        text = text.replace(old, new)
    return text


def scrub(root: QWidget):
    from qtpy.QtWidgets import (
        QLabel,
        QLineEdit,
        QPlainTextEdit,
        QTextBrowser,
        QTextEdit,
    )
    for widget in [root, *root.findChildren(QWidget)]:
        if isinstance(widget, QPlainTextEdit):
            doc = widget.document()
            for old, new in SCRUB:
                cursor = doc.find(old)
                while not cursor.isNull():
                    cursor.insertText(new)
                    cursor = doc.find(old, cursor)
        elif isinstance(widget, (QTextBrowser, QTextEdit)):
            html_text = widget.toHtml()
            if any(old in html_text.replace("\u200b", "") for old, _ in SCRUB):
                widget.setHtml(scrub_text(html_text))
        elif isinstance(widget, QLabel) and any(old in widget.text().replace("\u200b", "") for old, _ in SCRUB):
            widget.setText(scrub_text(widget.text()))
        elif isinstance(widget, QLineEdit):
            widget.setPlaceholderText(scrub_text(widget.placeholderText()))
            if any(old in widget.text() for old, _ in SCRUB):
                widget.setText(scrub_text(widget.text()))


def texts_in(root: QWidget) -> list[str]:
    """Every text the widget and its children show (to check before saving)."""
    from qtpy.QtWidgets import (
        QAbstractButton,
        QAbstractItemView,
        QComboBox,
        QLabel,
        QLineEdit,
        QPlainTextEdit,
        QTabBar,
        QTextEdit,
    )
    out = []
    for widget in [root, *root.findChildren(QWidget)]:
        if not widget.isVisible():
            continue
        out.append(widget.windowTitle())
        if isinstance(widget, (QPlainTextEdit, QTextEdit)):
            out.append(widget.toPlainText())
        elif isinstance(widget, QLabel):
            out.append(widget.text())
        elif isinstance(widget, QLineEdit):
            out += [widget.text(), widget.placeholderText()]
        elif isinstance(widget, QAbstractButton):
            out.append(widget.text())
        elif isinstance(widget, QComboBox):
            out += [widget.itemText(i) for i in range(widget.count())]
        elif isinstance(widget, QTabBar):
            out += [widget.tabText(i) for i in range(widget.count())]
        if isinstance(widget, QAbstractItemView) and widget.model() is not None:
            model, stack = widget.model(), [widget.rootIndex()]
            while stack and len(out) < 200_000:
                parent = stack.pop()
                try:
                    columns = model.columnCount(parent)
                except TypeError:  # (a list model's: one column, not asked for so)
                    columns = 1
                for r in range(min(model.rowCount(parent), 2000)):
                    for c in range(columns):
                        index = model.index(r, c, parent)
                        out.append(str(model.data(index) or ""))
                        if c == 0 and model.rowCount(index) > 0:
                            stack.append(index)
    return out


def check_private(*roots: QWidget):
    found = sorted({name for root in roots if root is not None for text in texts_in(root)
                    for name in FORBIDDEN if name and name in text})
    if found:
        raise RuntimeError(f"not saved: the screenshot would show {found}")


def wait(s=0.5):
    t = time.time()
    while time.time() - t < s:
        app.processEvents()


def wait_until(condition, timeout=60.0) -> bool:
    t = time.time()
    while time.time() - t < timeout:
        app.processEvents()
        if condition():
            return True
    return False


def wait_idle(w, timeout=300):
    """Until no fpm process has run for a second (Test/Run have two phases)."""
    t0, quiet = time.time(), None
    while time.time() - t0 < timeout:
        app.processEvents()
        if not w.busy() and w._phase is None:
            quiet = quiet or time.time()
            if time.time() - quiet > 1.5:
                return
        else:
            quiet = None


def save(pix: QPixmap, name: str, *shown: QWidget):
    """Save a screenshot, once the window (and ``shown``: popups in it) has
    been checked for this computer's names."""
    check_private(w, *shown)
    if pix.width() > MAX_W:
        pix = pix.scaledToWidth(MAX_W, Qt.SmoothTransformation)
    pix.save(str(OUT / f"{name}.png"))
    print("saved", name, pix.width(), "x", pix.height(), flush=True)


def opaque(widget: QWidget) -> QPixmap:
    """The widget's image over its window color (macOS draws parts of the
    window translucent, which grab() would leave black)."""
    from qtpy.QtGui import QPalette
    scrub(widget.window())
    r = widget.devicePixelRatioF()
    pix = QPixmap(int(widget.width() * r), int(widget.height() * r))
    pix.setDevicePixelRatio(r)
    pix.fill(widget.palette().color(QPalette.Window))
    painter = QPainter(pix)
    painter.drawPixmap(0, 0, widget.grab())
    painter.end()
    return pix


def composite(base_widget: QWidget, *popups: QWidget) -> QPixmap:
    """The widget's image with popups (separate windows) on top."""
    pix = opaque(base_widget)
    painter = QPainter(pix)
    origin = base_widget.mapToGlobal(QPoint(0, 0))
    for popup in popups:
        if popup is not None and popup.isVisible():
            scrub(popup)
            painter.drawPixmap(popup.mapToGlobal(QPoint(0, 0)) - origin, popup.grab())
    painter.end()
    return pix


def crop(pix: QPixmap, rect: QRect) -> QPixmap:
    r = pix.devicePixelRatio()
    out = pix.copy(QRect(int(rect.x() * r), int(rect.y() * r), int(rect.width() * r), int(rect.height() * r)))
    out.setDevicePixelRatio(r)
    return out


def area_of(*widgets: QWidget) -> QRect:
    """The window-relative rectangle around these widgets (their docks)."""
    rect = QRect()
    for widget in widgets:
        dock = w._dock_of.get(widget) if hasattr(w, "_dock_of") else None
        target = dock or widget
        top_left = target.mapTo(w, QPoint(0, 0))
        rect = rect.united(QRect(top_left, target.size()))
    return rect


def panel_shot(name: str, *widgets: QWidget):
    """The panels' docks, cropped from the window."""
    save(crop(opaque(w), area_of(*widgets)), name)


def roomy_bottom(on: bool = True):
    """The bottom panels taller and wider (for their screenshots), or back."""
    build = w.docks["build"]
    w.resizeDocks([build], [560 if on else 420], Qt.Vertical)
    w.resizeDocks([build], [1150 if on else 940], Qt.Horizontal)
    wait(0.5)


def shot(name):
    def deco(fn):
        if (ONLY and name not in ONLY) or (not ONLY and name in LINUX_ONLY):
            return fn
        try:
            fn()
        except Exception:  # noqa: BLE001
            print("FAILED", name, flush=True)
            traceback.print_exc()
        return fn
    return deco


def grab_modal(open_fn, name, delay=1.5, prepare=None):
    """Open a modal dialog, screenshot it after ``delay`` s, close it."""
    def take():
        dialog = QApplication.activeModalWidget()
        if dialog is None:
            print("no dialog for", name)
            return
        if prepare:
            prepare(dialog)
            wait(0.3)
        try:
            check_private(dialog)
            save(opaque(dialog), name, dialog)
        except Exception:  # noqa: BLE001
            print("FAILED", name, flush=True)
            traceback.print_exc()
        dialog.reject()
    QTimer.singleShot(int(delay * 1000), take)
    open_fn()


# Build configurations, as a team would commit them beside fpm.toml.
(P / "vibefpm.json").write_text("""{
  "vibefpm": 1,
  "configurations": [
    {"name": "gfortran debug", "profile": "debug", "compiler": "gfortran", "flags": "-fcheck=all"},
    {"name": "release", "profile": "release", "compiler": "gfortran"},
    {"name": "ci (checks + warnings)", "profile": "ci", "compiler": "gfortran"}
  ]
}
""")

w = MainWindow(str(P / "fpm.toml"))
w.setAttribute(Qt.WA_DontShowOnScreen)  # (drawn, but never on screen: no focus taken)
w.setUnifiedTitleAndToolBarOnMac(False)  # (the toolbar drawn by Qt, so it's in the screenshots)
# (Dark: macOS's own dark look; elsewhere a theme with its own dark colors, the system's being light.)
THEME = "dark" if sys.platform == "darwin" else "one-dark"
w.set_theme(THEME)
w.fortls_action.setChecked(True)
w.resize(1440, 900)
w.show()
wait(2.0)
w._set_graph_theme("auto")
w._set_graph_layout(next(iter(__import__("vibefpmgui.graph_view", fromlist=["LAYOUTS"]).LAYOUTS)))
tree = w.project_tree


def expand(rel):
    item = tree._items.get(str(P / rel))
    if item is not None:
        tree.view.expand(tree.proxy.mapFromSource(item.index()))


def show(widget: QWidget, seconds=0.6):
    w.show_panel(widget)
    wait(seconds)


# ------------------------------------------------------------ build + overview
if not ONLY or ONLY - LINUX_ONLY:
    w.compiler_edit.setText("")
    w.run_fpm(["build"], True, True)
    wait_idle(w)
    expand("src")
    expand("app")
    show(w.docks["graph"].widget())
    show(w.build_monitor, 1.5)
    w.open_file(str(P / "src" / "demo_parallel.f90"))  # (the editor not empty)
    w.resizeDocks([w.docks["graph"]], [600], Qt.Horizontal)  # (room for the package's details)
    wait(1.5)


@shot("overview")
def _():
    save(opaque(w), "overview")


@shot("build")
def _():
    panel_shot("build", w.build_monitor, w._content(w.docks["output"]))


@shot("graph")
def _():
    for theme in ("neon", "blueprint"):
        w._set_graph_theme(theme)
        wait(1.2)
        save(opaque(w.graph_view), f"graph-{theme}")
    w._set_graph_theme("auto")
    wait(0.8)


@shot("tree")
def _():
    show(w._content(w.docks["files"]))
    expand("test")
    expand("example")
    wait(0.3)
    group = next((tree.model.item(r) for r in range(tree.model.rowCount())
                  if tree.model.item(r).text() == "Dependencies"), None)
    if group is not None:
        tree.view.expand(tree.proxy.mapFromSource(group.index()))
    wait(0.5)
    save(opaque(tree), "tree")


@shot("configs")
def _():
    options = w._content(w.docks["options"])
    show(options)
    w.config_combo.setCurrentIndex(w.config_combo.findText("gfortran debug"))
    wait(0.5)
    save(opaque(options), "configs")
    w.config_combo.setCurrentIndex(0)  # (New configuration again: its options can change, for the shots after)
    wait(0.5)


@shot("tests")
def _():
    w.test_jobs_spin.setValue(4)  # (several at a time)
    w.run_tests()
    wait_idle(w)
    show(w.test_monitor, 0.8)
    panel_shot("tests", w.test_monitor, w._content(w.docks["output"]))
    w.test_jobs_spin.setValue(1)


@shot("run")
def _():
    w.run_programs()
    wait_idle(w)
    show(w.run_monitor, 0.8)
    panel_shot("run", w.run_monitor, w._content(w.docks["output"]))


@shot("outputs")
def _():
    show(w.outputs_view, 1.0)
    save(opaque(w.outputs_view), "outputs")


@shot("coverage")
def _():
    w.coverage_check.setChecked(True)
    w.run_tests()
    wait_idle(w)
    wait_until(lambda: bool(w.coverage_panel.findChildren(QWidget)), 30)
    w.open_file(str(P / "src" / "demo_stats.f90"))
    show(w.coverage_panel, 2.0)
    save(opaque(w), "coverage")
    w.coverage_check.setChecked(False)


# ------------------------------------------------------------------- editor
w.set_font_delta(2)  # larger text reads better in the cropped screenshots
wait(0.5)
# (The panels' smallest widths grow with the text, and the window with them: the
# window kept in proportion, 16:10, so its screenshots are the same shape as the others.)
w.resize(w.width(), round(w.width() * 900 / 1440))
w.open_file(str(P / "src" / "demo_stats.f90"))
editor = w.current_editor()
ed = editor.editor
wait(2.0)


def editor_area():
    """The editor tab area of the window (for crops)."""
    tl = w.tabs.mapTo(w, QPoint(0, 0))
    return QRect(tl.x(), tl.y(), w.tabs.width(), w.tabs.height())


def hover(line, word, suffix="("):
    text = ed.document().findBlockByNumber(line - 1).text()
    col = text.index(word + suffix) if suffix else text.index(word)
    cursor = QTextCursor(ed.document().findBlockByNumber(line - 1))
    cursor.setPosition(cursor.block().position() + col + 1)
    rect = ed.cursorRect(cursor)
    gp = ed.viewport().mapToGlobal(QPoint(rect.center().x(), rect.bottom()))
    ed._hover_word = (line, col)
    ed.word_at = lambda _p, l=line, c=col, n=word: (l, c, n)
    editor._request_hover(line, col, gp, "")
    wait(1.2)
    del ed.word_at


def around(popup, left, top, right, bottom):
    """A window-relative rectangle around a popup, kept inside the editor tabs."""
    g = popup.geometry()
    o = w.mapToGlobal(QPoint(0, 0))
    r = QRect(g.x() - o.x() - left, g.y() - o.y() - top, g.width() + left + right, g.height() + top + bottom)
    return r.intersected(editor_area())


@shot("editor")
def _():
    ed.go_to_line(1)
    hover(17, "mean")
    save(composite(w, ed.hover_popup), "editor", ed.hover_popup)
    ed.hover_popup.hide()


@shot("hover-intrinsic")
def _():
    hover(12, "sum")
    pix = composite(w, ed.hover_popup)
    r = ed.hover_popup.geometry()
    o = w.mapToGlobal(QPoint(0, 0))
    area = QRect(r.x() - o.x() - 40, r.y() - o.y() - 46, r.width() + 70, r.height() + 66)
    save(crop(pix, area.intersected(w.rect())), "hover-intrinsic", ed.hover_popup)
    ed.hover_popup.hide()


@shot("lint")
def _():
    issues = [i for i in ed.lint_issues if i.line == 4]
    cursor = QTextCursor(ed.document().findBlockByNumber(3))
    cursor.setPosition(cursor.block().position() + 8)
    rect = ed.cursorRect(cursor)
    gp = ed.viewport().mapToGlobal(QPoint(rect.center().x(), rect.bottom()))
    hover_card.set_text_color(ed.palette().color(ed.palette().ColorRole.Text))
    ed.hover_popup.show_at(gp, hover_card.lint_notes_html(issues), ("lint",))
    wait(1.0)
    ed.hover_popup.adjustSize()  # (off screen: sized to its text before it's drawn)
    wait(0.5)
    pix = composite(w, ed.hover_popup)
    save(crop(pix, around(ed.hover_popup, 330, 100, 60, 30)), "lint", ed.hover_popup)
    ed.hover_popup.hide()


@shot("problems")
def _():
    roomy_bottom()
    show(w.problems_panel, 1.5)
    panel_shot("problems", w.problems_panel)
    roomy_bottom(False)


def type_text(text):
    for i, chunk in enumerate(text.split("\n")):
        if i:
            QTest.keyClick(ed, Qt.Key_Return)
        if chunk:
            QTest.keyClicks(ed, chunk)


@shot("completion")
def _():
    ed.go_to_line(12)
    c = ed.textCursor()
    c.movePosition(QTextCursor.EndOfBlock)
    ed.setTextCursor(c)
    type_text("\nn = siz")
    wait(1.0)
    pix = composite(w, ed.completion_popup)
    save(crop(pix, around(ed.completion_popup, 260, 120, 60, 40)), "completion", ed.completion_popup)
    QTest.keyClick(ed, Qt.Key_Escape)
    type_text("e(x)\ns = variance(")
    wait(1.0)
    pix = composite(w, ed.signature_popup)
    save(crop(pix, around(ed.signature_popup, 200, 90, 80, 70)), "signature", ed.signature_popup)
    QTest.keyClick(ed, Qt.Key_Escape)
    ed.setPlainText((P / "src" / "demo_stats.f90").read_text())
    ed.document().setModified(False)
    wait(1.0)


@shot("outline")
def _():
    w.open_file(str(P / "src" / "demo_matrix.f90"))
    e2 = w.current_editor()
    wait(1.5)
    e2.editor.go_to_line(14)
    wait(0.5)
    e2.outline_box.showPopup()
    wait(0.8)
    popup = e2.outline_box.view().window()
    pix = composite(w, popup)
    area = editor_area()
    top = popup.mapToGlobal(QPoint(0, 0)).y() - w.mapToGlobal(QPoint(0, 0)).y() - 10
    save(crop(pix, QRect(area.x(), top, 700, 300)), "outline", popup)
    e2.outline_box.hidePopup()
    w.open_file(str(P / "src" / "demo_stats.f90"))


@shot("references")
def _():
    ed.go_to_position(10, 29)
    w.references_action.trigger()
    wait(1.0)
    save(opaque(w), "references")


@shot("rename")
def _():
    ed.go_to_position(10, 29)
    done = {}

    def answered(result):
        done["edits"] = lsp_mod.add_result_variable_edits(lsp_mod.workspace_edits(result), "mean", "average",
                                                          w._file_text)
    editor._flush_lsp()
    editor.lsp.rename(editor.path, 10, 27, "average", answered)
    wait_until(lambda: "edits" in done, 10)
    dialog = RenamePreviewDialog("mean", "average", done["edits"], w._file_text, {e.path for e in w.editors()},
                                 P, w._mono_font(), w)
    dialog.setAttribute(Qt.WA_DontShowOnScreen)
    dialog.show()
    wait(0.8)
    save(opaque(dialog), "rename", dialog)
    dialog.reject()


@shot("palette")
def _():
    w.show_command_palette()
    palette = w._command_palette
    wait(0.3)
    palette.field.setText("debug")
    wait(0.6)
    # (A popup: hidden at once off screen, its window never active. Drawn where it opens.)
    at = QPoint((w.width() - palette.width()) // 2, 40)
    pix = opaque(w)
    scrub(palette)
    painter = QPainter(pix)
    painter.drawPixmap(at, palette.grab())
    painter.end()
    area = QRect(at.x() - 80, at.y() - 30, palette.width() + 160, palette.height() + 70)
    check_private(palette)
    save(crop(pix, area.intersected(w.rect())), "palette")
    palette.hide()


@shot("complexity")
def _():
    roomy_bottom()
    show(w.complexity_panel, 0.5)
    w.refresh_complexity()
    wait_until(lambda: w.complexity_panel.procedures.rowCount() > 0, 30)
    table = w.complexity_panel.procedures
    row = next((r for r in range(table.rowCount())
                if (table.item(r, 0) and "parallel" in table.item(r, 0).text()) or r == 0), 0)
    table.setCurrentCell(row, 0)
    wait(1.5)
    panel_shot("complexity", w.complexity_panel)
    roomy_bottom(False)


@shot("git")
def _():
    git = shutil.which("git")
    if not (P / ".git").exists():
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "The demo project"]):
            subprocess.run([git, *args], cwd=P, check=True, capture_output=True)
    source = P / "src" / "demo_kinds.f90"  # (a file not open in the editor)
    original = source.read_text()
    source.write_text(original.replace("    public :: wp, ip", "    public :: wp, ip, sp")
                      .replace("    integer, parameter :: ip = int64",
                               "    integer, parameter :: ip = int64\n"
                               "    integer, parameter :: sp = real32  !! single precision, for output"))
    (P / "src" / "demo_extra.f90").write_text("module demo_extra\n  implicit none\nend module demo_extra\n")
    roomy_bottom()
    show(w.git_panel, 0.3)
    w.refresh_git()
    wait(3.0)
    panel_shot("git", w.git_panel)
    roomy_bottom(False)
    source.write_text(original)
    (P / "src" / "demo_extra.f90").unlink()


@shot("terminal")
def _():
    terminal = w.terminal_panel
    roomy_bottom()
    show(terminal, 1.5)
    terminal.view.setFocus()
    for command in ("fpm --version | head -1", "ls"):
        QTest.keyClicks(terminal.view, command)
        QTest.keyClick(terminal.view, Qt.Key_Return)
        wait(1.0)
    panel_shot("terminal", terminal)
    roomy_bottom(False)


@shot("styles")
def _():
    area = editor_area()
    for style in ("github-dark", "dracula", "solarized-light"):
        w.set_syntax_style(style)
        wait(0.8)
        save(crop(opaque(w), QRect(area.x(), area.y(), min(area.width(), 760), 360)), f"style-{style}")
    w.set_syntax_style("auto")
    wait(0.5)


@shot("themes")
def _():
    show(w.build_monitor)
    show(w.docks["graph"].widget())
    for theme in ("nord", "solarized-light"):
        w.set_theme(theme)
        wait(1.5)
        save(opaque(w), f"theme-{theme}")
    w.set_theme(THEME)
    wait(1.0)


@shot("format")
def _():
    messy = "program tidy\nuse demo_kinds,only:wp\nimplicit none\n      real(wp)::x\ndo i=1,3\nx=x+i\n   end do\nend program\n"
    w.open_file(str(P / "app" / "demo.F90"))
    e3 = w.current_editor()
    e3.editor.setPlainText(messy)
    wait(0.5)
    vp = e3.editor.mapTo(w, QPoint(0, 0))
    code = QRect(vp.x(), vp.y(), 470, 230)
    before = crop(opaque(w), code)
    w.format_action.trigger()
    wait(2.0)
    after = crop(opaque(w), code)
    save(before, "format-before")
    save(after, "format-after")
    e3.editor.setPlainText((P / "app" / "demo.F90").read_text())
    e3.editor.document().setModified(False)
    w.open_file(str(P / "src" / "demo_stats.f90"))


@shot("updates")
def _():
    grab_modal(w.check_dependency_updates, "updates", delay=12)


@shot("review")
def _():
    w.open_file(str(P / "src" / "demo_stats.f90"))
    wait(0.5)
    roomy_bottom()
    show(w.review_monitor, 0.5)

    def answered(start):
        """Until the AI has started (it may start in a moment) and finished."""
        wait_until(lambda: w._ai_thread is not None, 60)
        wait_until(lambda: w._ai_thread is None, 600)
        wait(1.5)
    answered(w.start_review())
    # Then a question in the chat, about the file.
    answered(w.send_chat("How could variance be computed in a single pass?", True))
    save(opaque(w), "review")
    roomy_bottom(False)


@shot("about")
def _():
    grab_modal(w.show_about, "about", delay=12)  # (its code typed far enough to read: 18 characters a second)


# ------------------------------------------------- Linux: debugging, Valgrind
@shot("debug")
def _():
    test = P / "test" / "check_debugger.f90"
    w.open_file(str(test), force_editor=True)
    code = w.current_editor().editor
    lines = test.read_text().splitlines()
    # Stopped where everything has its value (the checks), a disabled breakpoint before it (hollow).
    checks = next(i for i, line in enumerate(lines, 1) if line.strip() == "ok = ok .and. count == 55")
    loop = next(i for i, line in enumerate(lines, 1) if line.strip() == "squares(i) = i * i")
    for line in (loop, checks):
        code.toggle_breakpoint(line)
    code.toggle_breakpoint_enabled(loop)  # (a disabled one too: hollow)
    roomy_bottom()
    w.start_debugging("test", ["check_debugger"])
    if not wait_until(lambda: w.debug_session.state == "stopped", 300):
        print("debug console:", w.debug_panel.console.toPlainText()[-2000:], "\nstatus:",
              w.statusBar().currentMessage(), "\noutput:", w.console.toPlainText()[-2000:], flush=True)
        raise RuntimeError("the debugger didn't stop")
    wait_until(lambda: bool(w.debug_panel.variable_rows()), 30)
    panel = w.debug_panel
    locals_item = panel.variables.topLevelItem(0)
    for i in range(locals_item.childCount()):
        if locals_item.child(i).text(0) == "p":
            locals_item.child(i).setExpanded(True)
    wait(2.0)
    panel.evaluate_edit.setText("count * 2")
    QTest.keyClick(panel.evaluate_edit, Qt.Key_Return)
    wait(2.0)
    code.centerCursor()
    save(opaque(w), "debug")
    roomy_bottom(False)
    w.debug_session.stop()
    wait(1.0)


@shot("valgrind")
def _():
    w.run_valgrind("test", "check_debugger")
    wait_idle(w, 600)
    output = w._content(w.docks["output"])
    show(output, 0.5)
    w.resizeDocks([w.docks["output"]], [1000], Qt.Horizontal)  # (room for Valgrind's lines)
    w.resizeDocks([w.docks["output"]], [520], Qt.Vertical)
    wait(0.5)
    w.console.moveCursor(QTextCursor.End)
    w.console.ensureCursorVisible()
    wait(0.5)
    panel_shot("valgrind", output)


for e in w.editors():
    e.editor.document().setModified(False)
w.lsp_client.stop()
w.close()
