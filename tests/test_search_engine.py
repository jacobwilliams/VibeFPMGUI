"""Find in Files: the search itself (no windows)."""

from __future__ import annotations

import re

import pytest

from vibefpmgui import manifest as m
from vibefpmgui.lsp import apply_edits
from vibefpmgui.project_search import (
    Query,
    compile_query,
    find_matches,
    glob_matches,
    is_searched,
    preview_replacement,
    replacement_edits,
    search,
    search_roots,
)


def run(project, **options) -> dict[str, list[tuple[int, int, str]]]:
    """{file (+ "@package" for dependencies): [(line, column, matched text)]}."""
    query = Query(**options)
    roots = search_roots(m.resolve(project / "fpm.toml"), project)
    found = []
    search(roots, query, compile_query(query), {}, found.append)
    return {r.relative + (f"@{r.root.name}" if r.root.dependency else ""):
            [(x.line, x.column, x.text) for x in r.matches] for r in found}


def test_roots(project):
    roots = search_roots(m.resolve(project / "fpm.toml"), project)
    assert [(r.name, r.dependency) for r in roots] == [("proj", False), ("dl", True)]


def test_plain_search_skips_ignored_hidden_build_and_binary(project):
    results = run(project, text="count")
    # The local dependency lives inside the project folder: listed once, as itself.
    assert sorted(results) == ["src/dl.f90@dl", "src/foo.f90", "test/check.f90"]
    assert results["src/foo.f90"] == [(2, 13, "Count"), (3, 13, "count"), (6, 4, "count"),
                                      (6, 12, "count"), (6, 24, "count")]


def test_ignored_files_option(project):
    results = run(project, text="count", ignored=True)
    assert {"out/x.f90", "run.log", ".hidden/h.f90"} <= set(results)
    assert "build/b.f90" not in results and "src/bin.dat" not in results


def test_case_word_regex(project):
    assert run(project, text="Count", case=True) == {"src/foo.f90": [(2, 13, "Count")]}
    word = run(project, text="count", word=True)
    assert (3, 13, "count") not in word["src/foo.f90"]  # count_all
    assert len(word["src/foo.f90"]) == 4
    assert run(project, text=r"count\s*=", regex=True) == {
        "src/foo.f90": [(2, 13, "Count ="), (6, 4, "count =")]}


def test_include_exclude_and_dependencies(project):
    assert sorted(run(project, text="count", include="src/")) == ["src/dl.f90@dl", "src/foo.f90"]
    assert sorted(run(project, text="count", include="*.F90")) == \
        ["src/dl.f90@dl", "src/foo.f90", "test/check.f90"]  # (case-insensitive)
    assert sorted(run(project, text="count", exclude="test")) == ["src/dl.f90@dl", "src/foo.f90"]
    assert sorted(run(project, text="count", dependencies=False)) == ["src/foo.f90", "test/check.f90"]


def test_open_editor_text_is_searched(project):
    query = Query("count")
    roots = search_roots(m.resolve(project / "fpm.toml"), project)
    found = []
    edited = {(project / "app/main.f90").resolve(): "! count (unsaved)\n"}
    search(roots, query, compile_query(query), edited, found.append)
    assert "app/main.f90" in {r.relative for r in found}


def test_limit_stops_the_search(project):
    query = Query("count")
    roots = search_roots(m.resolve(project / "fpm.toml"), project)
    found = []
    _searched, truncated = search(roots, query, compile_query(query), {}, found.append, limit=3)
    assert truncated and sum(len(r.matches) for r in found) == 3


def test_bad_regex_and_empty_query():
    with pytest.raises(re.error):
        compile_query(Query("count(", regex=True))
    assert compile_query(Query("")) is None


def test_find_matches_positions_and_preview():
    text = "ab\ncd ab\n" + "x" * 80 + "ab tail"
    matches = find_matches(text, compile_query(Query("ab")))
    assert [(f.line, f.column) for f in matches] == [(1, 0), (2, 3), (3, 80)]
    long = matches[2]
    assert long.before.startswith("…") and long.text == "ab" and long.after == " tail"
    multi = find_matches("ab\ncd ab\n", compile_query(Query(r"b\ncd", regex=True)))
    assert [(f.line, f.column, f.end_line, f.end_column, f.text) for f in multi] == [(1, 1, 2, 2, "b")]


def test_replacement_edits():
    text = "count = count + 1 ! count\n"
    query = Query("(count) = (count)", regex=True)
    edits = replacement_edits(text, compile_query(query), query, r"\2 = \1 * 2")
    assert apply_edits(text, edits) == "count = count * 2 + 1 ! count\n"
    query = Query("count", word=True)
    only_second = replacement_edits(text, compile_query(query), query, "n", keep={(1, 8)})
    assert apply_edits(text, only_second) == "count = n + 1 ! count\n"
    with pytest.raises(re.error):
        query = Query("(count)", regex=True)
        replacement_edits(text, compile_query(query), query, r"\5")


def test_preview_replacement():
    query = Query("(a)(b)", regex=True)
    assert preview_replacement("ab", compile_query(query), query, r"\2\1") == "ba"
    assert preview_replacement("ab", compile_query(Query("ab")), Query("ab"), "x") == "x"


@pytest.mark.parametrize(("path", "globs", "expected"), [
    ("src/a/b.f90", ["src"], True),
    ("x/src/b.f90", ["src/"], True),
    ("b.f90", ["*.F90"], True),
    ("srcx/b.f90", ["src"], False),
    ("src/b.f90", ["src/*.f90"], True),
    ("src/b.c", ["*.f90"], False),
])
def test_glob_matches(path, globs, expected):
    assert glob_matches(path, globs) is expected


def test_is_searched_matches_the_walk(project):
    roots = search_roots(m.resolve(project / "fpm.toml"), project)
    root, dep = roots
    others = {dep.directory.resolve()}
    assert is_searched(root, (project / "src/foo.f90").resolve(), Query("x"), others)
    assert not is_searched(root, (project / "out/x.f90").resolve(), Query("x"), others)
    assert is_searched(root, (project / "out/x.f90").resolve(), Query("x", ignored=True), others)
    assert not is_searched(root, (project / "dep_local/src/dl.f90").resolve(), Query("x"), others)
    assert not is_searched(root, (project / "src/foo.f90").resolve(), Query("x", exclude="src/"), others)
