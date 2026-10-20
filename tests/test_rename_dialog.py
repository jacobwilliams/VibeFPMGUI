"""The Rename Symbol preview: each changed line as it will read, in the
editor's syntax colors, the new name highlighted (as the Search panel's)."""

from __future__ import annotations

from conftest import write_files
from qtpy.QtGui import QColor, QFont

from vibefpmgui import lsp as lsp_mod
from vibefpmgui.rename_dialog import LINE_ROLE, RenamePreviewDialog, renamed_spans
from vibefpmgui.result_tree import MATCH_COLOR

SOURCE = "module stats\ncontains\n  real function mean(x)\n    real :: x(:)\n    mean = sum(x) / size(x)\n" \
         "  end function mean\nend module stats\n"


def edit(line: int, column: int, old: str, new: str) -> lsp_mod.TextEdit:
    return lsp_mod.TextEdit(line, column, line, column + len(old), new)


def test_where_the_new_name_goes():
    """Each edit shifts those after it on the line ("mean" → "average": 3 longer)."""
    edits = [edit(4, 4, "mean", "average"), edit(2, 16, "mean", "average"), edit(4, 25, "x", "values")]
    assert renamed_spans(edits, 4) == [(4, 11), (28, 34)]  # (25 + 3: after the first one)
    assert renamed_spans(edits, 2) == [(16, 23)]
    assert renamed_spans(edits, 0) == []


def test_the_lines_are_syntax_colored_with_the_new_name(qtbot, tmp_path):
    path = write_files(tmp_path, {"src/stats.f90": SOURCE}) / "src/stats.f90"
    edits = {path: [edit(2, 16, "mean", "average"), edit(4, 4, "mean", "average"), edit(5, 15, "mean", "average")]}
    dialog = RenamePreviewDialog("mean", "average", edits, lambda p: p.read_text(), set(), tmp_path,
                                 QFont("Menlo"))
    qtbot.addWidget(dialog)
    node = dialog.tree.topLevelItem(0)
    rows = [node.child(i).data(0, LINE_ROLE) for i in range(node.childCount())]
    assert [(line, spans) for _path, line, spans in rows] == [(2, [(16, 23)]), (4, [(4, 11)]), (5, [(15, 22)])]
    assert dialog.lines(str(path))[2] == "  real function average(x)"  # (as it will read)
    formats = dialog.line_formats(str(path), 2)
    keyword, name = formats[len("  real ")], formats[len("  real function ")]  # ("function", "average")
    assert keyword is not None and keyword.foreground().color() != (name.foreground().color() if name else None)
    # Drawn: the new name's highlight behind it (the Search panel's color).
    node.setExpanded(True)
    dialog.resize(700, 300)
    dialog.show()
    qtbot.waitExposed(dialog)
    rect = dialog.tree.visualItemRect(node.child(0))
    image = dialog.tree.viewport().grab(rect).toImage()
    colors = {image.pixelColor(x, image.height() // 2).name() for x in range(image.width())}
    highlight = QColor(MATCH_COLOR)
    assert any(abs(QColor(c).red() - highlight.red()) < 60 and abs(QColor(c).green() - highlight.green()) < 60
               and QColor(c).blue() < 150 for c in colors)  # (yellowish, blended over the background)
