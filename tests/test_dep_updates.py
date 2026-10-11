"""Check for Dependency Updates, with git ls-remote mocked (no network)."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from conftest import write_files
from qtpy.QtWidgets import QMessageBox

from vibefpmgui import dep_updates as du
from vibefpmgui import manifest as m
from vibefpmgui.main_window import commit_web_url, compare_web_url, repo_web_url

URL = "https://github.com/fortran-lang/test-drive"
SHA = {name: (str(i) * 40)[:40] for i, name in enumerate(["head", "v0.4.0", "v0.5.0", "main", "dev", "v0.6.0rc1"], 1)}


def refs(**extra) -> dict[str, str]:
    """What ls_remote would answer for test-drive."""
    answer = {"HEAD": SHA["head"], "refs/heads/main": SHA["main"], "refs/heads/dev": SHA["dev"],
              "refs/tags/v0.4.0": SHA["v0.4.0"], "refs/tags/v0.5.0": SHA["v0.5.0"],
              "refs/tags/v0.6.0rc1": SHA["v0.6.0rc1"]}
    answer.update(extra)
    return answer


# ------------------------------------------------------------- ls-remote
def test_ls_remote_parses_git(monkeypatch):
    stdout = (f"{SHA['head']}\tHEAD\n{SHA['main']}\trefs/heads/main\n"
              f"{'a' * 40}\trefs/tags/v1.0\n{SHA['v0.5.0']}\trefs/tags/v1.0^{{}}\n")
    seen = {}

    def run(args, **kwargs):
        seen.update(args=args, env=kwargs["env"])
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    answer = du.ls_remote(URL)
    assert answer["refs/tags/v1.0"] == SHA["v0.5.0"]  # (an annotated tag: its commit)
    assert answer["HEAD"] == SHA["head"]
    assert seen["args"][:3] == ["git", "ls-remote", URL] and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize(("behaviour", "message"), [
    ("fail", "Repository not found"),
    ("missing", "git was not found"),
    ("slow", "no answer"),
])
def test_ls_remote_failures(monkeypatch, behaviour, message):
    def run(args, **kwargs):
        if behaviour == "missing":
            raise FileNotFoundError
        if behaviour == "slow":
            raise subprocess.TimeoutExpired(args, 30)
        return SimpleNamespace(returncode=128, stdout="", stderr="fatal: Repository not found\n")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError, match=message):
        du.ls_remote(URL)


def test_newest_tag():
    tags = ["v0.9", "v0.10.0", "v0.10.0rc1", "nightly", "release-2.0.1"]
    assert du.newest_tag(tags) == "release-2.0.1"
    assert du.newest_tag(["v1.0", "v1.1-beta"]) == "v1.0"
    assert du.newest_tag(["v1.0", "v1.1-beta"], allow_prerelease=True) == "v1.1-beta"
    assert du.newest_tag(["main"]) == ""


# ---------------------------------------------------------------- checks
@pytest.mark.parametrize(("kind", "ref", "current", "answer", "status", "words"), [
    ("tag", "v0.4.0", "", refs(), du.UPDATE, "v0.4.0 → v0.5.0"),
    ("tag", "v0.5.0", "", refs(), du.UP_TO_DATE, "newest tag"),
    ("tag", "v0.3.0", "", refs(), du.ERROR, "no longer exists"),
    ("tag", "v0.6.0rc1", "", refs(), du.UP_TO_DATE, "newest"),  # (a pre-release pin: pre-releases count)
    ("branch", "main", SHA["main"], refs(), du.UP_TO_DATE, "Up to date with branch main"),
    ("branch", "dev", SHA["main"], refs(), du.NEW_COMMITS, "new commits"),
    ("branch", "gone", "", refs(), du.ERROR, "Could not find branch gone"),
    ("", "", "", refs(), du.INFO, "Not downloaded yet"),
    ("", "", SHA["head"][:7], refs(), du.UP_TO_DATE, "default branch"),
    ("rev", SHA["v0.5.0"][:10], "", refs(), du.TAG_AVAILABLE, "which is tag v0.5.0"),
    ("rev", "abcdef1", "", refs(), du.TAG_AVAILABLE, "newest tag is v0.5.0"),
    ("rev", "abcdef1", "", {"HEAD": SHA["head"]}, du.INFO, "no tags"),
])
def test_check_dependency(monkeypatch, kind, ref, current, answer, status, words):
    monkeypatch.setattr(du, "ls_remote", lambda url: answer)
    check = du.check_dependency(du.DepCheck("test-drive", URL, kind, ref, current))
    assert check.status == status and words in check.message


def test_check_unreachable(monkeypatch):
    def fail(url):
        raise RuntimeError("Could not resolve host")

    monkeypatch.setattr(du, "ls_remote", fail)
    check = du.check_dependency(du.DepCheck("x", URL))
    assert check.status == du.ERROR and "Could not resolve host" in check.message


def test_git_dependencies(conflict_project):
    checks = du.git_dependencies(m.resolve(conflict_project / "fpm.toml"))
    assert [(c.name, c.ref_kind, c.ref, c.editable) for c in checks] == [("test-drive", "tag", "v0.5.0", True)]
    assert checks[0].declared_in == (conflict_project / "fpm.toml").resolve() or \
        checks[0].declared_in.name == "fpm.toml"
    assert checks[0].requested == "tag v0.5.0"


# ---------------------------------------------------------- editing tags
def test_set_ref_inline_and_table(tmp_path):
    manifest = tmp_path / "fpm.toml"
    manifest.write_text('name = "p"\n# keep this comment\n[dependencies]\n'
                        f'td = {{ git = "{URL}", tag = "v0.4.0" }}\n\n'
                        f'[dev-dependencies.other]\ngit = "{URL}"\nrev = "abcdef1"\n')
    assert du.set_tag(manifest, "td", "v0.4.0", "v0.5.0")
    assert du.set_ref(manifest, "other", "rev", "abcdef1", "tag", "v0.5.0")
    text = manifest.read_text()
    assert 'tag = "v0.5.0" }' in text and 'tag = "v0.5.0"\n' in text and "# keep this comment" in text
    assert not du.set_tag(manifest, "td", "v9.9", "v1.0")  # (not there: nothing changed)
    assert not du.set_tag(manifest, "nope", "v0.5.0", "v1.0")


def test_read_cache_revs(tmp_path):
    dep = tmp_path / "build" / "dependencies" / "td"
    write_files(dep, {".git/HEAD": "ref: refs/heads/main\n", ".git/refs/heads/main": SHA["main"] + "\n"})
    write_files(tmp_path / "build" / "dependencies" / "packed",
                {".git/HEAD": "ref: refs/heads/main\n", ".git/packed-refs": f"{SHA['dev']} refs/heads/main\n"})
    assert du.read_cache_revs(tmp_path) == {"td": SHA["main"], "packed": SHA["dev"]}


def test_web_urls():
    assert repo_web_url("git@github.com:fortran-lang/fpm.git") == "https://github.com/fortran-lang/fpm"
    assert repo_web_url(URL + ".git") == URL
    assert commit_web_url(URL, SHA["main"]).endswith("/commit/" + SHA["main"])
    assert commit_web_url(URL, "main") is None
    assert "compare" in compare_web_url(URL, SHA["v0.4.0"], SHA["v0.5.0"])
    assert repo_web_url("not a url") is None


# ---------------------------------------------------- the dialog, in the app
def test_updates_dialog_in_the_window(qtbot, make_window, conflict_project, monkeypatch):
    monkeypatch.setattr(du, "ls_remote", lambda url: refs(**{"refs/tags/v0.7.0": "7" * 40}))
    window = make_window(conflict_project / "fpm.toml")
    shown = {}

    def exec_dialog(dialog):
        shown["dialog"] = dialog
        qtbot.waitUntil(lambda: dialog._done == len(dialog.checks), timeout=10_000)
        shown["summary"] = dialog.summary.text()
        dialog.update_tag_requested.emit(dialog.checks[0])  # ("Update" on the row)
        return 0

    monkeypatch.setattr(du.UpdatesDialog, "exec", exec_dialog)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.No)
    window.check_dependency_updates()
    assert "1 with a newer tag" in shown["summary"]
    check = shown["dialog"].checks[0]
    assert (check.status, check.latest_tag) == (du.UPDATE, "v0.7.0")
    text = (conflict_project / "fpm.toml").read_text()
    assert 'tag = "v0.7.0"' in text and "v0.5.0" not in text  # (the root's own declaration)
    assert "now asks for tag v0.7.0 (was v0.5.0)" in asked[0]  # (and Update deps isn't run: No)
    assert window.process is None
