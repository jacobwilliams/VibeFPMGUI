"""What a package's box in the dependency graph says under its name: the
tag, branch or commit it's pinned to (before the version its own fpm.toml
gives: the sidebar shows that one); else its version (not fpm's "0"); else
its kind."""

from __future__ import annotations

import pytest
from conftest import write_files

from vibefpmgui import manifest as m
from vibefpmgui.graph_view import _subtitle

CACHE = """\
[proj]
version = "0"
proj-dir = "."
[roots]
version = "0"
proj-dir = "build/dependencies/roots"
git = "https://x/roots"
obj = "1.6.0"
[pyplot]
version = "3.1.0"
proj-dir = "build/dependencies/pyplot"
git = "https://x/pyplot"
[nightly]
version = "0"
proj-dir = "build/dependencies/nightly"
git = "https://x/nightly"
[fixed]
version = "0"
proj-dir = "build/dependencies/fixed"
git = "https://x/fixed"
[face]
version = "0"
proj-dir = "build/dependencies/face"
git = "https://x/face"
[cached]
version = "2.0.1"
proj-dir = "build/dependencies/cached"
git = "https://x/cached"
"""


@pytest.fixture
def pinned(tmp_path):
    """Downloaded git dependencies (build/dependencies, build/cache.toml as fpm writes them)."""
    root = write_files(tmp_path / "proj", {
        "fpm.toml": 'name = "proj"\n[dependencies]\n'
                    'roots = { git = "https://x/roots", tag = "1.6.0" }\n'
                    'pyplot = { git = "https://x/pyplot", tag = "3.8.0" }\n'
                    'nightly = { git = "https://x/nightly", branch = "main" }\n'
                    'fixed = { git = "https://x/fixed", rev = "85dc6b98404a61d4" }\n'
                    'face = { git = "https://x/face" }\n'
                    'cached = { git = "https://x/cached" }\n',
        "build/cache.toml": CACHE,
        "build/dependencies/roots/fpm.toml": 'name = "roots"\n',  # (no version: fpm's cache says "0")
        "build/dependencies/pyplot/fpm.toml": 'name = "pyplot"\nversion = "3.1.0"\n',
        "build/dependencies/nightly/fpm.toml": 'name = "nightly"\n',
        "build/dependencies/fixed/fpm.toml": 'name = "fixed"\n',
        "build/dependencies/face/fpm.toml": 'name = "face"\n',
        "build/dependencies/cached/fpm.toml": 'name = "cached"\n',
    })
    return root


def test_the_box_labels(pinned):
    graph = m.resolve(pinned / "fpm.toml")
    labels = {name: _subtitle(pkg) for name, pkg in graph.packages.items()}
    assert labels == {
        "proj": "Project",
        "roots": "v1.6.0",     # (the tag it's pinned to, not "v0")
        "pyplot": "v3.8.0",    # (the tag, not its own fpm.toml's older 3.1.0)
        "nightly": "branch main",  # (a branch)
        "fixed": "85dc6b9",    # (a commit: its start)
        "face": "Git",         # (nothing pinned, no version)
        "cached": "v2.0.1",    # (a version fpm recorded, other than its "0")
    }
    assert graph.packages["roots"].version == ""  # (the details don't say "0" either)


def details(window, name: str) -> str:
    window.show_package(name)
    return " ".join(window.details.toPlainText().split())  # (the table's cells on one line)


def test_the_sidebar_shows_the_manifest_version(make_window, pinned):
    window = make_window(pinned / "fpm.toml")
    text = details(window, "pyplot")
    assert "Manifest version 3.1.0" in text and "Requested tag 3.8.0" in text
    assert "Version" not in text  # (its own fpm.toml's: called so, not just "Version")
    text = details(window, "roots")  # (its fpm.toml gives none)
    assert "Manifest version" not in text and "Version" not in text and "Requested tag 1.6.0" in text
    assert "Version 2.0.1" in details(window, "cached")  # (fpm recorded it; its fpm.toml gives none)
