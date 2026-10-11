"""Take the website's screenshots from the real app (on screen, in dark mode).

Run it on a *copy* of the demo project (it builds it, and the rename preview
asks fortls about it), with its own settings file so yours aren't touched:

    cp -R examples/demo /tmp/shots/demo
    VIBEFPMGUI_SETTINGS=/tmp/shots/settings.ini pixi run python web/screenshots.py /tmp/shots/demo web/img

Give shot names (e.g. ``editor completion``) to retake just those. The window
appears on screen while it runs; macOS should be in dark mode. Paths shown in
the app are shortened (``~/projects/demo``) before each capture.
"""
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

from qtpy.QtCore import QPoint, QRect, Qt, QTimer
from qtpy.QtGui import QPainter, QPixmap, QTextCursor
from qtpy.QtTest import QTest
from qtpy.QtWidgets import QApplication, QWidget

app = QApplication(sys.argv[:1])
from vibefpmgui import hover_card
from vibefpmgui import lsp as lsp_mod
from vibefpmgui.main_window import MainWindow
from vibefpmgui.rename_dialog import RenamePreviewDialog

P = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
OUT.mkdir(parents=True, exist_ok=True)
ONLY = set(sys.argv[3:])
MAX_W = 2400  # px; larger screenshots are scaled down

# Start clean: no saved settings, nothing built yet (dependencies stay).
if os.environ.get("VIBEFPMGUI_SETTINGS"):
    Path(os.environ["VIBEFPMGUI_SETTINGS"]).unlink(missing_ok=True)
if not ONLY:
    for d in (P / "build").glob("gfortran_*"):
        shutil.rmtree(d)
    (P / "build" / "compile_commands.json").unlink(missing_ok=True)

# Paths shown in the screenshots: no scratch folders or home directory.
HOME = str(Path.home())
SCRUB = [(str(P.parent) + "/", "~/projects/"), (HOME + "/git/VibeFPMGUI/.pixi/envs/default/bin/", ""),
         (HOME + "/git/VibeFPMGUI/", "~/VibeFPMGUI/"), (HOME, "~")]


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


def wait(s=0.5):
    t = time.time()
    while time.time() - t < s:
        app.processEvents()


def wait_idle(w, timeout=300):
    """Until no fpm process has run for a second (Test/Run have two phases)."""
    t0, quiet = time.time(), None
    while time.time() - t0 < timeout:
        app.processEvents()
        if w.process is None and w._phase is None:
            quiet = quiet or time.time()
            if time.time() - quiet > 1.5:
                return
        else:
            quiet = None


def save(pix: QPixmap, name: str):
    if pix.width() > MAX_W:
        pix = pix.scaledToWidth(MAX_W, Qt.SmoothTransformation)
    pix.save(str(OUT / f"{name}.png"))
    print("saved", name, pix.width(), "x", pix.height(), flush=True)


def opaque(widget: QWidget) -> QPixmap:
    """The widget's image over its window colour (macOS draws parts of the
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
    """The widget's image with tooltip-like popups (separate windows) on top."""
    pix = opaque(base_widget)
    painter = QPainter(pix)
    origin = base_widget.mapToGlobal(QPoint(0, 0))
    for popup in popups:
        if popup is not None and popup.isVisible():
            painter.drawPixmap(popup.mapToGlobal(QPoint(0, 0)) - origin, popup.grab())
    painter.end()
    return pix


def crop(pix: QPixmap, rect: QRect) -> QPixmap:
    r = pix.devicePixelRatio()
    out = pix.copy(QRect(int(rect.x() * r), int(rect.y() * r), int(rect.width() * r), int(rect.height() * r)))
    out.setDevicePixelRatio(r)
    return out


def shot(name):
    def deco(fn):
        if ONLY and name not in ONLY:
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
        save(opaque(dialog), name)
        dialog.reject()
    QTimer.singleShot(int(delay * 1000), take)
    open_fn()


w = MainWindow(str(P / "fpm.toml"))
w.fortls_action.setChecked(True)
w.resize(1440, 900)
w.move(40, 40)
w.show()
w.raise_()
wait(2.0)
w._set_graph_theme("auto")
w._set_graph_layout(next(iter(__import__("vibefpmgui.graph_view", fromlist=["LAYOUTS"]).LAYOUTS)))
tree = w.project_tree


def expand(rel):
    item = tree._items.get(str(P / rel))
    if item is not None:
        tree.view.expand(tree.proxy.mapFromSource(item.index()))


# ------------------------------------------------------------ build + overview
w.compiler_edit.setText("")
w.run_fpm(["build"], True, True)
wait_idle(w)
expand("src")
expand("app")
w.tabs.setCurrentIndex(0)
w.monitor_tabs.setCurrentWidget(w.build_monitor)
wait(1.5)
w.graph_view.select_package(w.graph.root) if hasattr(w.graph_view, "select_package") else None
wait(0.5)


@shot("overview")
def _():
    save(opaque(w), "overview")


@shot("build")
def _():
    # The build grid and output panel (bottom half of the window).
    pix = opaque(w)
    top = w.bottom_split.mapTo(w, QPoint(0, 0))
    save(crop(pix, QRect(top.x(), top.y(), w.bottom_split.width(), w.bottom_split.height())), "build")


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
    expand("test")
    expand("example")
    wait(0.3)
    group = next((tree.model.item(r) for r in range(tree.model.rowCount())
                  if tree.model.item(r).text() == "Dependencies"), None)
    if group is not None:
        tree.view.expand(tree.proxy.mapFromSource(group.index()))
    wait(0.5)
    save(opaque(tree), "tree")


@shot("tests")
def _():
    w.run_tests()
    wait_idle(w)
    w.monitor_tabs.setCurrentWidget(w.test_monitor)
    wait(0.8)
    pix = opaque(w)
    top = w.bottom_split.mapTo(w, QPoint(0, 0))
    save(crop(pix, QRect(top.x(), top.y(), w.bottom_split.width(), w.bottom_split.height())), "tests")


@shot("run")
def _():
    w.run_programs()
    wait_idle(w)
    w.monitor_tabs.setCurrentWidget(w.run_monitor)
    wait(0.8)
    pix = opaque(w)
    top = w.bottom_split.mapTo(w, QPoint(0, 0))
    save(crop(pix, QRect(top.x(), top.y(), w.bottom_split.width(), w.bottom_split.height())), "run")


@shot("outputs")
def _():
    w.tabs.setCurrentWidget(w.outputs_view)
    wait(1.0)
    save(opaque(w.outputs_view), "outputs")


# ------------------------------------------------------------------- editor
w.set_font_delta(2)  # larger text reads better in the cropped screenshots
w.open_file(str(P / "src" / "demo_stats.f90"))
editor = w.current_editor()
ed = editor.editor
wait(2.0)


def editor_area(pix_widget=None):
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
    save(composite(w, ed.hover_popup), "editor")
    ed.hover_popup.hide()


@shot("hover-intrinsic")
def _():
    hover(12, "sum")
    pix = composite(w, ed.hover_popup)
    r = ed.hover_popup.geometry()
    o = w.mapToGlobal(QPoint(0, 0))
    area = QRect(r.x() - o.x() - 40, r.y() - o.y() - 46, r.width() + 70, r.height() + 66)
    save(crop(pix, area.intersected(w.rect())), "hover-intrinsic")
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
    wait(0.5)
    pix = composite(w, ed.hover_popup)
    save(crop(pix, around(ed.hover_popup, 330, 100, 60, 30)), "lint")
    ed.hover_popup.hide()


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
    save(crop(pix, around(ed.completion_popup, 260, 120, 60, 40)), "completion")
    QTest.keyClick(ed, Qt.Key_Escape)
    type_text("e(x)\ns = variance(")
    wait(1.0)
    pix = composite(w, ed.signature_popup)
    save(crop(pix, around(ed.signature_popup, 200, 90, 80, 70)), "signature")
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
    save(crop(pix, QRect(area.x(), top, 700, 300)), "outline")
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
    t = time.time()
    while "edits" not in done and time.time() - t < 10:
        app.processEvents()
    dialog = RenamePreviewDialog("mean", "average", done["edits"], w._file_text, {e.path for e in w.editors()},
                                 P, w._mono_font(), w)
    dialog.show()
    wait(0.8)
    save(opaque(dialog), "rename")
    dialog.reject()


@shot("styles")
def _():
    area = editor_area()
    for style in ("github-dark", "dracula", "solarized-light"):
        w.set_syntax_style(style)
        wait(0.8)
        save(crop(opaque(w), QRect(area.x(), area.y(), min(area.width(), 760), 360)), f"style-{style}")
    w.set_syntax_style("auto")
    wait(0.5)


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
    wait(0.8)
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
    w.start_review()
    t = time.time()
    while time.time() - t < 240:
        app.processEvents()
        if w._ai_thread is None and time.time() - t > 5:
            break
    wait(1.5)
    w.monitor_tabs.setCurrentWidget(w.review_monitor)
    wait(0.5)
    save(opaque(w), "review")


@shot("about")
def _():
    grab_modal(w.show_about, "about", delay=1.5)


for e in w.editors():
    e.editor.document().setModified(False)
w.lsp_client.stop()
w.close()
