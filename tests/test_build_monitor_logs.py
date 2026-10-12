"""Finding a compiled file's log in build/ quickly (once per file a build
finishes: it must not list every build folder each time)."""

from __future__ import annotations

import os
import time

from vibefpmgui.build_monitor import LogFinder


def make_build(project, configs=6, files=400):
    """build/<compiler>_<hash>/<package>/ folders full of logs, as fpm leaves them."""
    folder = None
    for c in range(configs):
        folder = project / "build" / f"gfortran_{c:016X}" / "demo"
        folder.mkdir(parents=True)
        for i in range(files):
            (folder / f"src_file{i}.f90.o.log").write_text("")
            (folder / f"src_file{i}.f90.o").write_text("")
    (project / "build" / "dependencies").mkdir()  # (not a configuration)
    return folder  # (the last configuration's)


def test_finds_logs_by_path_without_listing(tmp_path):
    folder = make_build(tmp_path)
    since = time.time() - 60
    finder = LogFinder(tmp_path)
    source = tmp_path / "src" / "file7.f90"
    found = finder.find("file7.f90", [source], since)
    assert folder / "src_file7.f90.o.log" in [log for _t, log in found]  # (folder: the last one)
    assert len(found) == 6  # (one per configuration)
    assert finder.listed == 0  # (straight to the names: no folder listed)
    old = since + 120  # (written before this build: not this build's)
    assert finder.find("file7.f90", [source], old) == []


def test_a_dependency_s_log_name(tmp_path):
    folder = make_build(tmp_path, configs=1, files=0)
    log = folder / "build_dependencies_fmin_src_fmin.F90.o.log"
    log.write_text("")
    source = tmp_path / "build" / "dependencies" / "fmin" / "src" / "fmin.F90"
    found = LogFinder(tmp_path).find("fmin.F90", [source], time.time() - 60)
    assert [p for _t, p in found] == [log]


def test_unknown_paths_list_each_folder_once(tmp_path):
    make_build(tmp_path, configs=2, files=50)
    finder = LogFinder(tmp_path)
    since = time.time() - 60
    for i in range(50):  # (no paths known: listings, kept until a folder changes)
        assert len(finder.find(f"file{i}.f90", [], since)) == 2
    assert finder.listed == 2
    folder = tmp_path / "build" / "gfortran_0000000000000000" / "demo"
    (folder / "src_new.f90.o.log").write_text("")
    future = time.time() + 5
    os.utime(folder, (future, future))  # (the folder changed: listed again)
    assert len(finder.find("new.f90", [], since)) == 1 and finder.listed == 3


def test_new_configurations_are_seen(tmp_path):
    make_build(tmp_path, configs=1, files=1)
    finder = LogFinder(tmp_path)
    assert len(finder.folders()) == 1
    folder = tmp_path / "build" / "lfortran_00000000000000FF" / "demo"
    folder.mkdir(parents=True)
    future = time.time() + 5
    os.utime(tmp_path / "build", (future, future))
    assert len(finder.folders()) == 2


def test_quick_for_a_big_build(tmp_path):
    make_build(tmp_path, configs=6, files=400)
    finder = LogFinder(tmp_path)
    since = time.time() - 60
    started = time.monotonic()
    for i in range(400):  # (every file of a 400-file build finishing)
        assert finder.find(f"file{i}.f90", [tmp_path / "src" / f"file{i}.f90"], since)
    assert time.monotonic() - started < 2.0
