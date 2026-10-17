"""Project files' and Find in Files' reading of .gitignore files."""

from __future__ import annotations

import subprocess
import time

from conftest import write_files

from vibefpmgui.project_search import Query, Root, iter_files
from vibefpmgui.project_tree import GitIgnore


def test_rules_as_git_reads_them(tmp_path):
    root = write_files(tmp_path / "repo", {
        ".gitignore": "*.log\nout/\n\n!keep.log\n",
        "src/.gitignore": "# generated\nauto_*.f90\n",
        "src/sub/.gitignore": "!auto_kept.f90\n",
        "src/a.f90": "", "src/auto_x.f90": "", "src/sub/auto_kept.f90": "", "src/sub/auto_y.f90": "",
        "run.log": "", "keep.log": "", "out/x.f90": "", "src/out/y.f90": "", "notes/out": "",
    })
    (root / ".git").mkdir()
    rules = GitIgnore(root)
    expected = {
        "src/a.f90": None,
        "src/auto_x.f90": f"{root.resolve() / 'src/.gitignore'}:2",
        "src/sub/auto_kept.f90": None,  # (re-included by the deeper file)
        "src/sub/auto_y.f90": f"{root.resolve() / 'src/.gitignore'}:2",
        "run.log": f"{root.resolve() / '.gitignore'}:1",
        "keep.log": None,
        "notes/out": None,  # ("out/": only a folder)
    }
    for relative, where in expected.items():
        path = root / relative
        assert rules.check(path, False) == where, relative
        assert rules.check_name(path.parent.resolve(), path.name, False) == where, relative
    assert rules.check(root / "out", True) == f"{root.resolve() / '.gitignore'}:2"
    assert rules.check(root / "src/out", True) == f"{root.resolve() / '.gitignore'}:2"  # (at any depth)
    assert rules.check(tmp_path / "elsewhere.log", False) is None  # (outside the repository)


def test_the_files_searched_leave_out_the_ignored(tmp_path):
    root = write_files(tmp_path / "repo", {".gitignore": "*.log\nbuild_out/\n", "a.f90": "", "x.log": "",
                                           "build_out/b.f90": "", "src/c.f90": "", "src/d.log": ""})
    subprocess.run(["git", "init", "-q"], cwd=root, check=False)
    found = sorted(p.relative_to(root.resolve()).as_posix()
                   for _r, p in iter_files([Root("r", root, False)], Query("", include="")))
    assert found == ["a.f90", "src/c.f90"]  # (".gitignore": a hidden file)


def test_a_big_tree_s_rules_are_worked_out_per_folder(tmp_path):
    files = {".gitignore": "*.o\nignored/\n"}
    for d in range(40):
        for i in range(50):
            files[f"src/part{d}/f{i}.f90"] = ""
    root = write_files(tmp_path / "repo", files)
    (root / ".git").mkdir()
    rules = GitIgnore(root)
    started = time.perf_counter()
    for d in range(40):
        folder = (root / f"src/part{d}").resolve()
        for i in range(50):
            assert rules.check_name(folder, f"f{i}.f90", False) is None
    assert time.perf_counter() - started < 1.0  # (2000 files: per file, path arithmetic made this seconds)
    assert len(rules._chains) == 40
