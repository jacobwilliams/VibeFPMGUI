"""Search/replace edits from the AI: parsing, finding, applying."""

from __future__ import annotations

import pytest

from vibefpmgui import ai_edits as ed
from vibefpmgui.ai_edits import Edit, apply, apply_all, locate, parse_edits, report

CODE = """\
module m
  implicit none
contains
  subroutine s(a, n)
    integer, intent(in) :: n
    real :: a(n)
    integer :: i
    do i = 1, n
      a(i) = 0.0
    end do
  end subroutine s
end module m
"""


def block(search: str, replace: str) -> str:
    return f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE"


def edit(search: str, replace: str, text: str = CODE) -> Edit:
    return locate(text, Edit(search, replace))


# ------------------------------------------------------------- parsing
def test_parse_blocks_and_the_text_around_them():
    answer = ("I'll make `a` intent(out).\n\n" + block("    real :: a(n)", "    real, intent(out) :: a(n)")
              + "\n\nAnd rename the loop variable:\n```fortran\n"
              + block("    integer :: i", "    integer :: k") + "\n```\nDone.")
    edits = parse_edits(answer)
    assert [(e.search, e.replace) for e in edits] == [
        ("    real :: a(n)", "    real, intent(out) :: a(n)"), ("    integer :: i", "    integer :: k")]


def test_parse_only_complete_blocks():
    partial = block("a", "b") + "\n<<<<<<< SEARCH\nx\n=======\ny"  # (the second still being written)
    assert len(parse_edits(partial)) == 1
    assert len(parse_edits(partial + "\n>>>>>>> REPLACE")) == 2


def test_parse_fences_inside_a_block_and_marker_lookalikes():
    answer = "<<<<<<< SEARCH\n```fortran\nx = 1\n```\n=======\n```fortran\nx = 2 ! >>>>>>> REPLACE\n```\n>>>>>>> REPLACE"
    (e,) = parse_edits(answer)
    assert e.search == "x = 1" and e.replace == "x = 2 ! >>>>>>> REPLACE"  # (a marker inside a line isn't one)


def test_parse_crlf_and_empty_sides():
    (e,) = parse_edits("<<<<<<< SEARCH\r\n=======\r\nnew line\r\n>>>>>>> REPLACE\r\n")
    assert e.search == "" and e.replace == "new line"


# ------------------------------------------------------------- finding
def test_exact_match():
    e = edit("    integer :: i", "    integer :: k")
    assert e.located and e.how == "" and e.line == e.end_line == 7
    assert "    integer :: k\n" in apply(CODE, e)


def test_trailing_spaces_and_blank_lines_around_search():
    e = edit("\n    do i = 1, n   \n      a(i) = 0.0\n", "\n    do i = n, 1, -1\n      a(i) = 0.0\n")
    assert e.located and e.how == "spacing" and (e.line, e.end_line) == (8, 9)
    assert "    do i = n, 1, -1\n      a(i) = 0.0\n    end do" in apply(CODE, e)


@pytest.mark.parametrize("shift", [2, 6])  # (the file has 4)
def test_shifted_indentation_is_put_back(shift):
    pad = " " * shift
    e = edit(f"{pad}do i = 1, n\n{pad}  a(i) = 0.0\n{pad}end do", f"{pad}a = 0.0")
    assert e.located and e.how == "indentation"
    assert "    integer :: i\n    a = 0.0\n  end subroutine" in apply(CODE, e)  # (at the file's indentation)


def test_too_much_indentation_is_taken_off():
    e = edit("        integer :: i", "        integer :: i, j")
    assert e.located and e.how == "indentation" and "\n    integer :: i, j\n" in apply(CODE, e)


def test_line_numbers_copied_from_the_prompt():
    e = edit(" 7|     integer :: i\n 8|     do i = 1, n", " 7|     integer :: k\n 8|     do k = 1, n")
    assert e.located and e.how == "line numbers"
    assert "    integer :: k\n    do k = 1, n\n" in apply(CODE, e)


def test_not_found_and_ambiguous():
    e = edit("    real :: b(n)", "x")
    assert not e.located and e.status == ed.FAILED and e.problem.startswith("not found")
    e = edit("integer", "logical")
    assert not e.located and "matches 2 places" in e.problem
    e = edit("      a(i) = 0.0", "x", text=CODE + CODE.replace("module m", "module q"))
    assert "matches 2 places" in e.problem


def test_empty_search_adds_at_the_end():
    e = edit("", "! the end")
    assert apply(CODE, e) == CODE + "! the end\n"
    assert apply("x = 1", locate("x = 1", Edit("", "y = 2"))) == "x = 1\ny = 2\n"


def test_empty_replace_removes_the_lines():
    e = edit("    integer :: i\n", "")
    assert apply(CODE, e) == CODE.replace("    integer :: i\n", "")
    e = edit("    integer :: i", "")  # (without its line break: still the whole line)
    assert apply(CODE, e) == CODE.replace("    integer :: i\n", "")
    last = locate("a\nb", Edit("b", ""))
    assert apply("a\nb", last) == "a"


def test_tabs_and_fixed_form():
    fixed = "      PROGRAM P\n\tX = 1\n      END\n"
    e = locate(fixed, Edit("    X = 1", "    X = 2"), tab_width=4)
    assert e.located and apply(fixed, e) == "      PROGRAM P\n    X = 2\n      END\n"
    e = locate(fixed, Edit("      PROGRAM P", "      PROGRAM Q"))
    assert apply(fixed, e).startswith("      PROGRAM Q\n")  # (columns kept: exact text)


def test_case_matters():
    assert not edit("    INTEGER :: I", "x").located


def test_failed_edit_found_later_is_pending_again():
    e = edit("    integer :: j", "x")
    assert e.status == ed.FAILED
    locate(CODE.replace(":: i", ":: j"), e)
    assert e.located and e.status == ed.PENDING and not e.problem


# ------------------------------------------------------------- applying
def test_apply_all_from_the_end_and_skip_overlaps():
    edits = [edit("    integer :: i", "    integer :: k"), edit("module m\n  implicit", "module mm\n  implicit"),
             edit("    integer :: i\n    do i = 1, n", "x")]  # (overlaps the first)
    text, made = apply_all(CODE, edits)
    assert made == [edits[1], edits[0]]  # (in the file's order)
    assert text.startswith("module mm\n") and "    integer :: k\n    do i = 1, n" in text


def test_apply_needs_a_located_edit():
    with pytest.raises(ValueError):
        apply(CODE, edit("nowhere", "x"))


def test_report_says_what_became_of_the_edits():
    edits = [Edit("a", "b", line=4, status=ed.APPLIED), Edit("c", "d", line=9, status=ed.REJECTED),
             Edit("e", "f", status=ed.FAILED)]
    assert report(edits) == ("(About your previous answer's edits: edit 1, at line 4, was applied; "
                             "edit 2, at line 9, was rejected by the user; edit 3 wasn't found in the file.)")
    assert report([]) == ""
