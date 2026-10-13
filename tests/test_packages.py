"""About → Packages…: the environment's installed packages."""

from __future__ import annotations

import json
import subprocess

import pytest
from qtpy.QtWidgets import QApplication

from vibefpmgui import packages as pk

PIXI = json.dumps([
    {"name": "gfortran", "version": "15.3.0", "build": "h500d687_7", "kind": "conda", "is_explicit": True,
     "source": "https://conda.anaconda.org/conda-forge", "license": "BSD-3-Clause", "size_bytes": 31000,
     "url": "https://conda.anaconda.org/conda-forge/osx-arm64/gfortran-15.3.0-h500d687_7.conda"},
    {"name": "libgfortran5", "version": "16.2.0", "build": "hdb7a957_7", "kind": "conda", "is_explicit": False,
     "source": "https://conda.anaconda.org/conda-forge", "license": "GPL-3.0-only", "size_bytes": 2_500_000},
    {"name": "fortitude_lint", "version": "0.9.2", "build": None, "kind": "pypi", "is_explicit": True,
     "source": "https://pypi.org/simple", "size_bytes": 16_433_558, "url": "https://files.pythonhosted.org/x.whl"},
])
CONDA = json.dumps([
    {"base_url": "https://conda.anaconda.org/conda-forge", "build_number": 7, "build_string": "h500d687_7",
     "channel": "conda-forge", "dist_name": "gfortran-15.3.0-h500d687_7", "name": "gfortran",
     "platform": "osx-arm64", "version": "15.3.0"},
    {"base_url": "https://pypi.org/", "build_string": "pypi_0", "channel": "pypi", "name": "fprettify",
     "platform": "pypi", "version": "0.3.7"},
])


def test_parse_pixi():
    gfortran, libgfortran, fortitude = pk.parse_pixi(PIXI)
    assert (gfortran.name, gfortran.version, gfortran.kind, gfortran.requested) == ("gfortran", "15.3.0", "conda", True)
    assert gfortran.channel_name == "conda-forge" and gfortran.page == "https://anaconda.org/conda-forge/gfortran"
    assert not libgfortran.requested and libgfortran.size == 2_500_000
    assert fortitude.kind == "pypi" and fortitude.channel_name == "pypi.org"
    assert fortitude.page == "https://pypi.org/project/fortitude_lint/"


def test_parse_conda():
    gfortran, fprettify = pk.parse_conda(CONDA)
    assert (gfortran.build, gfortran.kind, gfortran.channel_name) == ("h500d687_7", "conda", "conda-forge")
    assert fprettify.kind == "pypi"  # (installed with pip into the conda environment)


def fake_run(monkeypatch, output):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(pk.subprocess, "run", run)
    return calls


def test_which_environment(monkeypatch, tmp_path):
    calls = fake_run(monkeypatch, PIXI)
    listing = pk.list_packages({"PIXI_PROJECT_MANIFEST": "/p/pixi.toml", "PIXI_ENVIRONMENT_NAME": "dev",
                                "PIXI_EXE": "/bin/pixi"})
    assert "pixi environment “dev”" in listing.source and len(listing.packages) == 3
    assert calls[-1] == ["/bin/pixi", "list", "--json", "--manifest-path", "/p/pixi.toml", "--environment", "dev"]
    conda = tmp_path / "conda"
    conda.write_text("")
    calls = fake_run(monkeypatch, CONDA)
    listing = pk.list_packages({"CONDA_PREFIX": "/envs/fpm", "CONDA_DEFAULT_ENV": "fpm", "CONDA_EXE": str(conda)})
    assert "conda environment “fpm”" in listing.source and len(listing.packages) == 2
    assert calls[-1] == [str(conda), "list", "--json", "--prefix", "/envs/fpm"]
    listing = pk.list_packages({})  # (neither: the Python packages the app sees)
    assert "not a pixi or conda environment" in listing.source
    assert any(p.name.lower() == "pyside6" for p in listing.packages)


def test_a_failing_listing_says_why(monkeypatch):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "Error: no environment\n")

    monkeypatch.setattr(pk.subprocess, "run", run)
    listing = pk.list_packages({"PIXI_PROJECT_MANIFEST": "/p/pixi.toml", "PIXI_EXE": "/bin/pixi"})
    assert listing.packages == [] and "no environment" in listing.error


@pytest.fixture
def dialog(qtbot):
    listing = pk.Listing("pixi environment “default” of /p/pixi.toml", pk.parse_pixi(PIXI))
    dialog = pk.PackagesDialog(lister=lambda: listing)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: len(dialog.packages) == 3, timeout=5_000)  # (listed in the background)
    return dialog


def test_the_table(dialog):
    table = dialog.table
    assert [table.item(r, 0).text() for r in range(3)] == ["fortitude_lint", "gfortran", "libgfortran5"]
    assert "3 packages" in dialog.source.text()
    row = next(r for r in range(3) if table.item(r, 0).text() == "gfortran")
    assert table.item(row, 4).text() == "conda-forge" and table.item(row, 7).text() == "✓"
    assert table.item(row, 6).text() == "30.3 KB"
    table.sortByColumn(6, pk.Qt.DescendingOrder)  # (by size: bytes, not text)
    assert table.item(0, 0).text() == "fortitude_lint"
    dialog.filter.setText("GFORTRAN")
    assert [p.name for p in dialog.visible_packages()] == ["libgfortran5", "gfortran"] or \
        sorted(p.name for p in dialog.visible_packages()) == ["gfortran", "libgfortran5"]
    assert dialog.count.text() == "2 of 3"
    dialog.copy_list()
    assert "gfortran 15.3.0 h500d687_7 conda" in QApplication.clipboard().text()
    assert "fortitude_lint" not in QApplication.clipboard().text()  # (only the ones shown)


def test_double_click_opens_the_page(dialog, monkeypatch):
    opened = []
    monkeypatch.setattr(pk.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    row = next(r for r in range(3) if dialog.table.item(r, 0).text() == "fortitude_lint")
    dialog.table.cellDoubleClicked.emit(row, 0)
    assert opened == ["https://pypi.org/project/fortitude_lint/"]


def test_about_has_a_packages_button(qtbot, monkeypatch):
    from qtpy.QtWidgets import QPushButton

    from vibefpmgui.about import AboutDialog

    shown = []
    monkeypatch.setattr(pk.PackagesDialog, "exec", lambda self: shown.append(self.windowTitle()) or 0)
    monkeypatch.setattr(pk, "list_packages", lambda: pk.Listing("test", []))
    about = AboutDialog("fpm", "gfortran")
    qtbot.addWidget(about)
    next(b for b in about.findChildren(QPushButton) if b.text() == "Packages…").click()
    assert shown == ["Installed Packages"]
