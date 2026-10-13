"""*About → Packages…*: the packages installed in the environment the app runs in.

In a pixi environment (``pixi run``, ``pixi shell``) they come from ``pixi list
--json``; in an activated conda environment from ``conda list --json`` (or
mamba / micromamba); otherwise they're the Python packages the app can see.
Shown in a sortable, filterable table.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from qtpy.QtCore import Qt, QUrl
from qtpy.QtGui import QDesktopServices
from qtpy.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .background import call_in_background

LIST_TIMEOUT = 60  # seconds (a first `pixi list` may have to solve the environment)


class Package(NamedTuple):
    name: str
    version: str
    build: str = ""
    kind: str = ""        # "conda" or "pypi"
    channel: str = ""     # conda channel or package index
    license: str = ""
    size: int = 0         # bytes (0: not known)
    requested: bool = False  # asked for in the manifest (not only needed by another package)
    url: str = ""         # where it was downloaded from ("" if not known: see page)

    @property
    def channel_name(self) -> str:
        """The channel or index, short: "conda-forge", "pypi.org"."""
        text = self.channel.rstrip("/")
        if "pypi.org" in text:
            return "pypi.org"
        return text.rsplit("/", 1)[-1] if "://" in text else text

    @property
    def page(self) -> str:
        """The package's web page (anaconda.org or PyPI)."""
        if self.kind == "pypi":
            return f"https://pypi.org/project/{self.name}/"
        channel = self.channel.rstrip("/").rsplit("/", 1)[-1] or "conda-forge"
        return f"https://anaconda.org/{channel}/{self.name}"


class Listing(NamedTuple):
    source: str               # where the list is from, e.g. "pixi environment default of …/pixi.toml"
    packages: list[Package]
    error: str = ""


# ------------------------------------------------------------------ parsing
def parse_pixi(text: str) -> list[Package]:
    """``pixi list --json`` -> packages."""
    out = []
    for item in json.loads(text):
        if not isinstance(item, dict) or not item.get("name"):
            continue
        out.append(Package(
            str(item["name"]), str(item.get("version") or ""), str(item.get("build") or ""),
            str(item.get("kind") or ""), str(item.get("source") or item.get("index_url") or ""),
            str(item.get("license") or ""), int(item.get("size_bytes") or 0),
            bool(item.get("is_explicit")), str(item.get("url") or "")))
    return out


def parse_conda(text: str) -> list[Package]:
    """``conda list --json`` (also mamba and micromamba) -> packages."""
    out = []
    for item in json.loads(text):
        if not isinstance(item, dict) or not item.get("name"):
            continue
        channel = str(item.get("channel") or "")
        pypi = channel == "pypi" or item.get("platform") == "pypi"
        base = str(item.get("base_url") or "")
        # (No download URL: conda list doesn't say whether it was a .conda or a .tar.bz2.)
        out.append(Package(
            str(item["name"]), str(item.get("version") or ""), str(item.get("build_string") or ""),
            "pypi" if pypi else "conda", base or channel))
    return out


def python_packages() -> list[Package]:
    """The Python packages the app can import (no pixi or conda environment)."""
    from importlib import metadata

    out = {}
    for dist in metadata.distributions():
        meta = dist.metadata
        name = meta.get("Name") or ""  # (.get: a missing field is None, not deprecated)
        if name and name.lower() not in out:
            out[name.lower()] = Package(name, dist.version or "", kind="pypi", license=meta.get("License") or "",
                                        url=meta.get("Home-page") or "")
    return sorted(out.values(), key=lambda p: p.name.lower())


# ---------------------------------------------------------------- listing
def _run(args: list[str]) -> str:
    out = subprocess.run(args, capture_output=True, text=True, timeout=LIST_TIMEOUT, check=False)
    if out.returncode != 0:
        lines = [line for line in (out.stderr or out.stdout).splitlines() if line.strip()]
        raise RuntimeError(lines[-1] if lines else f"{Path(args[0]).name} exited with code {out.returncode}")
    return out.stdout


def list_packages(environ=os.environ) -> Listing:
    """The environment the app runs in, and its packages (blocking: run it
    in the background)."""
    manifest = environ.get("PIXI_PROJECT_MANIFEST")
    if manifest:
        env_name = environ.get("PIXI_ENVIRONMENT_NAME") or "default"
        source = f"pixi environment “{env_name}” of {manifest}"
        pixi = environ.get("PIXI_EXE") or shutil.which("pixi")
        if not pixi:
            return Listing(source, [], "pixi wasn't found to list the packages.")
        try:
            return Listing(source, parse_pixi(_run([pixi, "list", "--json", "--manifest-path", manifest,
                                                    "--environment", env_name])))
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            return Listing(source, [], f"pixi list failed: {exc}")
    prefix = environ.get("CONDA_PREFIX")
    if prefix:
        name = environ.get("CONDA_DEFAULT_ENV") or Path(prefix).name
        source = f"conda environment “{name}” ({prefix})"
        for exe in (environ.get("CONDA_EXE"), environ.get("MAMBA_EXE"),
                    shutil.which("conda"), shutil.which("mamba"), shutil.which("micromamba")):
            if exe and Path(exe).exists():
                try:
                    return Listing(source, parse_conda(_run([exe, "list", "--json", "--prefix", prefix])))
                except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
                    return Listing(source, [], f"{Path(exe).name} list failed: {exc}")
        return Listing(source, [], "conda (or mamba, micromamba) wasn't found to list the packages.")
    return Listing(f"Python packages of {sys.prefix} (not a pixi or conda environment)", python_packages())


def human_size(n: int) -> str:
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


# ------------------------------------------------------------------- dialog
MAX_COLUMN_WIDTH = 220  # pixels (but the name's; the full text is in the tooltip)
COLUMNS = ["Name", "Version", "Build", "Type", "Channel / Index", "License", "Size", "Requested", "URL"]


class _SizeItem(QTableWidgetItem):
    """Sorts by the number of bytes, not the text."""

    def __lt__(self, other):
        return (self.data(Qt.UserRole) or 0) < (other.data(Qt.UserRole) or 0)


class PackagesDialog(QDialog):
    """The packages of the app's environment, in a table (filled in when
    the listing, run in the background, is done)."""

    def __init__(self, parent=None, lister=None):
        """``lister``: what makes the list (list_packages, looked up now: tests replace it)."""
        super().__init__(parent)
        self.setWindowTitle("Installed Packages")
        self.packages: list[Package] = []
        self.source = QLabel("Listing the packages…")
        self.source.setWordWrap(True)
        self.source.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by name…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        self.count = QLabel()
        top = QHBoxLayout()
        top.addWidget(self.filter, 1)
        top.addWidget(self.count)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.cellDoubleClicked.connect(lambda row, _c: self._open_page(row))
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.setToolTip("Double-click a package for its page (anaconda.org or PyPI)")
        copy = QPushButton("Copy List")
        copy.setToolTip("Copy the packages shown, one per line (name, version, build, type): for bug reports")
        copy.clicked.connect(self.copy_list)
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(copy)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.source)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        self.resize(980, 560)
        call_in_background(lister or list_packages, self.show_listing, self)

    def show_listing(self, listing: Listing):
        self.packages = listing.packages
        text = f"<b>{len(listing.packages)} packages</b> in the {listing.source}"
        if listing.error:
            text = f"{listing.source}<br><span style='color:#e03131'>{listing.error}</span>"
        self.source.setText(text)
        self.table.setSortingEnabled(False)  # (while filling: rows would move under us)
        self.table.setRowCount(len(listing.packages))
        for row, p in enumerate(listing.packages):
            values = [p.name, p.version, p.build, p.kind, p.channel_name, p.license, human_size(p.size),
                      "✓" if p.requested else "", p.url or p.page]
            for column, value in enumerate(values):
                item = _SizeItem(value) if column == 6 else QTableWidgetItem(value)
                if column == 6:
                    item.setData(Qt.UserRole, p.size)
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if column == 7:
                    item.setTextAlignment(Qt.AlignCenter)
                    item.setToolTip("Asked for in the manifest" if p.requested else "Needed by another package")
                if column in (4, 5, 8) and value:
                    item.setToolTip(p.channel if column == 4 else value)  # (the full text: the column may be narrow)
                item.setData(Qt.UserRole + 1, row)  # (which package: rows move when sorted)
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        header = self.table.horizontalHeader()
        header.resizeSections(QHeaderView.ResizeToContents)
        for column in range(1, len(COLUMNS) - 1):  # (a long license mustn't push the rest out of view)
            header.resizeSection(column, min(header.sectionSize(column), MAX_COLUMN_WIDTH))
        self._apply_filter()

    def _package(self, row: int) -> Package | None:
        item = self.table.item(row, 0)
        index = item.data(Qt.UserRole + 1) if item is not None else None
        return self.packages[index] if index is not None and 0 <= index < len(self.packages) else None

    def _apply_filter(self):
        needle = self.filter.text().strip().lower()
        shown = 0
        for row in range(self.table.rowCount()):
            p = self._package(row)
            hide = bool(needle) and (p is None or needle not in p.name.lower())
            self.table.setRowHidden(row, hide)
            shown += not hide
        self.count.setText(f"{shown} of {len(self.packages)}" if needle else "")

    def visible_packages(self) -> list[Package]:
        return [p for row in range(self.table.rowCount())
                if not self.table.isRowHidden(row) and (p := self._package(row)) is not None]

    def copy_list(self):
        lines = [f"{p.name} {p.version} {p.build} {p.kind}".rstrip() for p in self.visible_packages()]
        QApplication.clipboard().setText("\n".join(lines))

    def _open_page(self, row: int):
        p = self._package(row)
        if p is not None:
            QDesktopServices.openUrl(QUrl(p.page))

    def _context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        p = self._package(row)
        if p is None:
            return
        menu = QMenu(self)
        menu.addAction("Open Package Page", lambda: self._open_page(row))
        menu.addAction("Copy Name and Version", lambda: QApplication.clipboard().setText(f"{p.name} {p.version}"))
        if p.url:
            menu.addAction("Copy URL", lambda: QApplication.clipboard().setText(p.url))
        menu.exec(self.table.viewport().mapToGlobal(pos))
        menu.deleteLater()
