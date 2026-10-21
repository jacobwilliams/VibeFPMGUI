"""A file starting with UTF-8's byte-order mark (a Visual Studio .sln, say):
Qt's editor drops the mark from its text, so it's kept as the encoding
("utf-8-sig") and written back on save — saving lost it before, changing the
file's first line without anyone editing it."""

from __future__ import annotations

from conftest import open_editor

from vibefpmgui.source_text import decode, read_source

BOM = b"\xef\xbb\xbf"


def test_read_without_the_mark_kept_as_the_encoding(tmp_path):
    assert decode(BOM + b"x = 1\n") == ("x = 1\n", "utf-8-sig")
    assert decode(b"x = 1\n") == ("x = 1\n", "utf-8")
    path = tmp_path / "a.sln"
    path.write_bytes(BOM + b"line 1\r\nline 2\r\n")
    source = read_source(path)
    assert (source.text, source.encoding, source.newline) == ("line 1\nline 2\n", "utf-8-sig", "\r\n")


def test_saved_with_its_mark_and_line_endings(window, project):
    path = project / "src/win.f90"
    path.write_bytes(BOM + b"program win\r\nend program win\r\n")
    editor = open_editor(window, path)
    assert editor.editor.toPlainText() == "program win\nend program win\n"
    assert "UTF-8 with BOM" in editor.status.text()
    cursor = editor.editor.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    cursor.insertText("! more\n")
    assert editor.save()
    assert path.read_bytes() == BOM + b"program win\r\nend program win\r\n! more\r\n"
