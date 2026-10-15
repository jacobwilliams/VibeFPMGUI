"""Reading source files the same way everywhere, and the shared text helpers."""

from __future__ import annotations

from vibefpmgui import lsp
from vibefpmgui.editor_shared import word_bounds, word_start
from vibefpmgui.source_text import read_source, search_text


def test_read_source(tmp_path):
    utf8 = tmp_path / "a.f90"
    utf8.write_bytes("x = 1 ! café\r\ny = 2\r\nz = 3\n".encode())
    text, encoding, newline, mixed = read_source(utf8)
    assert text == "x = 1 ! café\ny = 2\nz = 3\n" and encoding == "utf-8"
    assert newline == "\r\n" and mixed  # (most lines: CRLF; one LF)
    latin = tmp_path / "b.f90"
    latin.write_bytes("! caf\xe9\n".encode("latin-1"))
    assert read_source(latin)[:2] == ("! café\n", "latin-1")


def test_search_text_skips_binary_and_huge_files(tmp_path, monkeypatch):
    from vibefpmgui import source_text

    (tmp_path / "bin.dat").write_bytes(b"count\0binary")
    (tmp_path / "big.f90").write_text("x" * 100)
    (tmp_path / "ok.f90").write_bytes(b"a\r\nb\n")
    assert search_text(tmp_path / "bin.dat") is None
    assert search_text(tmp_path / "missing.f90") is None
    monkeypatch.setattr(source_text, "MAX_FILE_SIZE", 50)
    assert search_text(tmp_path / "big.f90") is None
    assert search_text(tmp_path / "ok.f90") == "a\nb\n"


def test_word_at_a_column():
    line = "  call bump(count_1, 42)"
    assert word_bounds(line, 7) == (7, 11)    # (in "bump")
    assert word_bounds(line, 11) == (7, 11)   # (just after it)
    assert word_bounds(line, 12) == (12, 19)  # ("count_1")
    assert word_bounds(line, 21) is None      # (a number)
    assert word_bounds(line, 1) is None       # (spaces)
    assert word_start(line, 9) == 7 and word_start(line, 2) == 2


def test_comments_strings_and_arguments():
    assert lsp.code_spans("x = 'a!b' ! note") == [(0, 4), (9, 10)]
    assert lsp.in_comment_or_string("x = 'a!b' ! note", 6)      # (in the string)
    assert not lsp.in_comment_or_string("x = 'a!b' ! note", 9)  # (just after it)
    assert lsp.in_comment_or_string("x = 'a!b' ! note", 12)     # (in the comment)
    assert lsp.argument_index("call f(a, g(1, 2), ") == 2
    assert lsp.argument_index("call f(a, 'x,(y', ") == 2  # (commas and brackets in strings don't count)
    assert lsp.argument_index("x = arr[1, ") is None and lsp.argument_index("x = y") is None
