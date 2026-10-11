"""What fpm has left in a project's ``build/`` folder, and a tab to show it.

fpm splits one build over several folders named ``<compiler>_<hash>``, where
the hash covers the settings that affect that step: one folder for compiled
files and modules, one for the library archive, one for linked programs. A
build with other settings (profile, flags, features) adds another set and old
sets are never removed.

The hashes can't be turned back into settings, so the GUI records, for every
build it runs, which folders that build created or changed together with the
settings used (`ConfigRecords`). Folders it has no record for (built
before this feature, or by fpm on the command line) are grouped by when they
were last written.

``build/compile_commands.json`` (the exact compiler command for each source
file of the most recent build) is read by `read_compile_commands`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from qtpy.QtCore import Qt, Signal
from qtpy.QtGui import QFont
from qtpy.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .build_monitor import PALETTES, app_is_light
from .compat import WINDOWS, quote_arg, reveal_label

BUILD_DIR_RE = re.compile(r"^(?P<compiler>.+)_(?P<hash>[0-9A-F]{16})$")
PROGRAM_KINDS = {"app": "program", "test": "test", "example": "example"}
GROUP_GAP_SECONDS = 120  # unrecorded folders written this close together form one build


@dataclass
class BuildFolder:
    path: Path
    compiler: str
    newest: float = 0.0           # newest file modification time
    size: int = 0                 # bytes
    programs: list[tuple[str, Path]] = field(default_factory=list)  # (kind, path)
    libraries: list[Path] = field(default_factory=list)
    modules: int = 0
    objects: int = 0

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def role(self) -> str:
        if self.programs:
            return "programs"
        if self.objects or self.modules:
            return "compiled files"
        if self.libraries:
            return "library"
        return "other"


@dataclass
class Configuration:
    label: str                    # e.g. "release · gfortran"
    folders: list[BuildFolder]
    recorded: bool                # built by the GUI with known settings
    flags: str = ""
    built: float = 0.0
    used: float = 0.0             # last built or found up to date by the GUI
    current: bool = False

    @property
    def size(self) -> int:
        return sum(f.size for f in self.folders)

    @property
    def programs(self) -> list[tuple[str, Path]]:
        return sorted((p for f in self.folders for p in f.programs),
                      key=lambda kp: (list(PROGRAM_KINDS.values()).index(kp[0]), kp[1].name))

    @property
    def libraries(self) -> list[Path]:
        return [lib for f in self.folders for lib in f.libraries]

    @property
    def modules(self) -> int:
        return sum(f.modules for f in self.folders)

    @property
    def objects(self) -> int:
        return sum(f.objects for f in self.folders)


# ----------------------------------------------------------------- scanning
def build_folders(project_dir: Path) -> list[Path]:
    build = project_dir / "build"
    if not build.is_dir():
        return []
    return sorted(p for p in build.iterdir() if p.is_dir() and BUILD_DIR_RE.match(p.name))


def _is_program(path: Path) -> bool:
    if os.name == "nt":
        return path.suffix.lower() == ".exe"
    return os.access(path, os.X_OK) and not path.suffix


def scan_folder(path: Path) -> BuildFolder:
    match = BUILD_DIR_RE.match(path.name)
    folder = BuildFolder(path, match.group("compiler") if match else "?")
    for dirpath, _dirs, files in os.walk(path):
        rel_top = Path(dirpath).relative_to(path).parts[:1]
        for name in files:
            file = Path(dirpath) / name
            try:
                stat = file.stat()
            except OSError:
                continue
            folder.size += stat.st_size
            folder.newest = max(folder.newest, stat.st_mtime)
            suffix = file.suffix.lower()
            if suffix in (".mod", ".smod"):
                folder.modules += 1
            elif suffix in (".o", ".obj"):
                folder.objects += 1
            elif suffix in (".a", ".lib") or (suffix in (".so", ".dylib", ".dll") and name.startswith("lib")):
                folder.libraries.append(file)
            elif rel_top and rel_top[0] in PROGRAM_KINDS and _is_program(file):
                folder.programs.append((PROGRAM_KINDS[rel_top[0]], file))
    return folder


def snapshot(project_dir: Path) -> dict[str, float]:
    """Folder name -> newest file time, to see what a build changed."""
    return {p.name: scan_folder(p).newest for p in build_folders(project_dir)}


def changed_folders(before: dict[str, float], after: dict[str, float]) -> list[str]:
    return sorted(name for name, newest in after.items() if newest > before.get(name, -1))


# --------------------------------------------------------- compile commands
def read_compile_commands(project_dir: Path) -> tuple[dict[str, list[str]], str | None]:
    """{resolved source path: argument list} from build/compile_commands.json,
    plus the name of the build folder the commands compile into."""
    path = project_dir / "build" / "compile_commands.json"
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}, None
    commands: dict[str, list[str]] = {}
    folder = None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or "file" not in entry:
            continue
        args = entry.get("arguments") or shlex.split(entry.get("command", ""))
        base = Path(entry.get("directory") or project_dir)
        source = (base / entry["file"]).resolve()
        commands[str(source)] = list(args)
        if folder is None and "-J" in args:
            j = args.index("-J")
            if j + 1 < len(args):
                folder = Path(args[j + 1]).name
    return commands, folder


def format_command(args: list[str]) -> str:
    """A shell-quoted command, one option per line for readability."""
    if not args:
        return ""
    lines = [quote_arg(args[0])]
    i = 1
    while i < len(args):
        part = quote_arg(args[i])
        # Keep an option and its value together ("-o file", "-J dir").
        if args[i] in ("-o", "-J", "-I", "-c") and i + 1 < len(args):
            part += " " + quote_arg(args[i + 1])
            i += 1
        lines.append(part)
        i += 1
    # Line continuation: "\" in POSIX shells, "^" in the Windows shell.
    return f" {'^' if WINDOWS else chr(92)}\n    ".join(lines)


# ------------------------------------------------------------------ records
class ConfigRecords:
    """Per-project record of which folders each GUI build wrote, stored in
    the app's settings as JSON: [{label, flags, folders, built}, ...]."""

    def __init__(self, settings, key: str):
        self.settings, self.key = settings, key

    def load(self) -> list[dict]:
        try:
            data = json.loads(self.settings.value(self.key, "[]"))
            return [r for r in data if isinstance(r, dict) and r.get("folders")]
        except (TypeError, ValueError):
            return []

    def save(self, records: list[dict]):
        self.settings.setValue(self.key, json.dumps(records))

    def last_used(self) -> tuple[str, str] | None:
        """(label, flags) of the last build the GUI ran, if any."""
        try:
            data = json.loads(self.settings.value(self.key + "-last", "null"))
            return (data["label"], data["flags"]) if data else None
        except (TypeError, ValueError, KeyError):
            return None

    def _set_last(self, label: str, flags: str):
        self.settings.setValue(self.key + "-last", json.dumps({"label": label, "flags": flags}))

    def record_build(self, label: str, flags: str, folders: list[str]):
        """``folders`` were just written by a build with these settings."""
        self._set_last(label, flags)
        if not folders:
            return
        records = self.load()
        for r in records:  # a folder belongs to one configuration only
            r["folders"] = [f for f in r["folders"] if f not in folders]
        existing = next((r for r in records if r.get("label") == label and r.get("flags") == flags), None)
        if existing is None:
            existing = {"label": label, "flags": flags, "folders": []}
            records.append(existing)
        existing["folders"] = sorted(set(existing["folders"]) | set(folders))
        existing["built"] = existing["used"] = time.time()
        self.save([r for r in records if r["folders"]])

    def mark_used(self, label: str, flags: str):
        """A build with these settings found nothing to do: it's still current."""
        self._set_last(label, flags)
        records = self.load()
        for r in records:
            if r.get("label") == label and r.get("flags") == flags:
                r["used"] = time.time()
                self.save(records)
                return

    def forget(self, folders: list[str]):
        records = self.load()
        for r in records:
            r["folders"] = [f for f in r["folders"] if f not in folders]
        self.save([r for r in records if r["folders"]])


def configurations(project_dir: Path, records: list[dict],
                   last_used: tuple[str, str] | None = None) -> list[Configuration]:
    """Group the build folders into configurations, newest first.
    ``last_used`` is the (label, flags) of the GUI's last build."""
    folders = {p.name: scan_folder(p) for p in build_folders(project_dir)}
    _, current_compile = read_compile_commands(project_dir)
    configs: list[Configuration] = []
    claimed: set[str] = set()
    for r in records:
        present = [folders[n] for n in r["folders"] if n in folders and n not in claimed]
        if not present:
            continue
        claimed.update(f.name for f in present)
        config = Configuration(r.get("label", "?"), present, True, r.get("flags", ""),
                               r.get("built") or max(f.newest for f in present))
        config.used = r.get("used") or config.built
        configs.append(config)
    # Folders without a record: group those written at about the same time.
    loose = sorted((f for n, f in folders.items() if n not in claimed), key=lambda f: f.newest)
    group: list[BuildFolder] = []
    for f in loose + [None]:
        if f is not None and (not group or f.newest - group[-1].newest <= GROUP_GAP_SECONDS):
            group.append(f)
            continue
        if group:
            compilers = sorted({g.compiler for g in group})
            configs.append(Configuration(f"{', '.join(compilers)} · built outside the app",
                                         group, False, "", max(g.newest for g in group)))
        group = [f] if f is not None else []
    configs.sort(key=lambda c: c.built, reverse=True)
    # "Current": the configuration of the GUI's last build. If that build's
    # settings have no recorded folders (they were built outside the app),
    # nothing is marked. Without any GUI build, use the folder the latest
    # compile commands point at, else the newest.
    if last_used is not None:
        for c in configs:
            if c.recorded and (c.label, c.flags) == tuple(last_used):
                c.current = True
                break
    else:
        for c in configs:
            if current_compile and any(f.name == current_compile for f in c.folders):
                c.current = True
                break
        else:
            if configs:
                configs[0].current = True
    return configs


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def human_age(t: float) -> str:
    seconds = max(0, time.time() - t)
    if seconds < 60:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'s' * (n != 1)} ago"
    return ""


def delete_folders(paths: list[Path]) -> list[str]:
    """Remove build folders; returns error messages (empty if all went)."""
    errors = []
    for p in paths:
        try:
            shutil.rmtree(p)
        except OSError as exc:
            errors.append(f"{p.name}: {exc}")
    return errors


# --------------------------------------------------------------------- view
class ConfigCard(QFrame):
    run_requested = Signal(str)
    reveal_requested = Signal(str)
    delete_requested = Signal(object)  # Configuration

    def __init__(self, config: Configuration, pal: dict):
        super().__init__()
        self.config = config
        self.setObjectName("ConfigCard")
        accent = pal["accent1"] if config.current else pal["line"]
        self.setStyleSheet(f"""
            #ConfigCard {{ background: {pal['bg']}; border: 1px solid {accent};
                           border-radius: 10px; }}
            #ConfigCard QLabel {{ background: transparent; color: {pal['text']}; }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)

        top = QHBoxLayout()
        title = QLabel(config.label)
        font = QFont(QApplication.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 1)
        title.setFont(font)
        top.addWidget(title)
        if config.current:
            pill = QLabel("current")
            pill.setStyleSheet(f"background: {pal['accent1']}; color: white; border-radius: 8px;"
                               f" padding: 1px 8px; font-weight: 600;")
            top.addWidget(pill)
        top.addStretch(1)
        reveal = QToolButton()
        reveal.setText(reveal_label())
        reveal.clicked.connect(lambda: self.reveal_requested.emit(str(config.folders[0].path)))
        delete = QToolButton()
        delete.setText("Delete…")
        delete.setToolTip("Delete this configuration's build folders to free disk space")
        delete.clicked.connect(lambda: self.delete_requested.emit(config))
        top.addWidget(reveal)
        top.addWidget(delete)
        layout.addLayout(top)

        muted = pal["muted"]
        bits = [f"built {human_age(config.built)}", human_size(config.size),
                f"{len(config.folders)} folder{'s' * (len(config.folders) != 1)}"]
        if config.modules:
            bits.append(f"{config.modules} modules")
        if config.objects:
            bits.append(f"{config.objects} compiled files")
        info = QLabel(f"<span style='color:{muted}'>" + " · ".join(bits) + "</span>")
        info.setToolTip("\n".join(f"{f.name}  ({f.role}, {human_size(f.size)})" for f in config.folders))
        layout.addWidget(info)
        if config.flags:
            flags = QLabel(f"<span style='color:{muted}'>flags:</span> <code>{config.flags}</code>")
            flags.setWordWrap(True)
            flags.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(flags)
        if not config.recorded:
            note = QLabel(f"<span style='color:{muted}'><i>No record of the settings used — "
                          "built before this view existed, or by fpm outside the app.</i></span>")
            note.setWordWrap(True)
            layout.addWidget(note)

        for lib in config.libraries:
            row = QLabel(f"📚 <b>{lib.name}</b> <span style='color:{muted}'>library · "
                         f"{human_size(lib.stat().st_size) if lib.exists() else ''}</span>")
            row.setToolTip(str(lib))
            layout.addWidget(row)
        for kind, program in config.programs:
            row = QHBoxLayout()
            icon = {"program": "▶", "test": "🧪", "example": "💡"}.get(kind, "•")
            label = QLabel(f"{icon} <b>{program.name}</b> <span style='color:{muted}'>{kind}</span>")
            label.setToolTip(str(program))
            row.addWidget(label)
            row.addStretch(1)
            run = QPushButton("Run")
            run.setToolTip(f"Run {program} directly (no rebuild), with the Program args")
            run.setAttribute(Qt.WA_LayoutUsesWidgetRect)
            run.clicked.connect(lambda _=False, p=str(program): self.run_requested.emit(p))
            row.addWidget(run)
            layout.addLayout(row)
        if not config.programs and not config.libraries:
            layout.addWidget(QLabel(f"<span style='color:{muted}'>No programs or library here "
                                    "(compiled files only).</span>"))


class BuildOutputsView(QWidget):
    """The "Build outputs" tab."""

    run_requested = Signal(str)
    reveal_requested = Signal(str)
    delete_requested = Signal(object)   # list[Configuration]
    refresh_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.configs: list[Configuration] = []
        self.summary = QLabel()
        refresh = QToolButton()
        refresh.setText("Refresh")
        refresh.clicked.connect(self.refresh_requested)
        self.prune_button = QToolButton()
        self.prune_button.setText("Delete All Except Current…")
        self.prune_button.clicked.connect(
            lambda: self.delete_requested.emit([c for c in self.configs if not c.current]))
        header = QHBoxLayout()
        header.addWidget(self.summary, 1)
        header.addWidget(self.prune_button)
        header.addWidget(refresh)

        self.cards = QWidget()
        self.cards_layout = QVBoxLayout(self.cards)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(10)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(self.cards)
        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet("color: gray;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.addLayout(header)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.empty, 1)
        self._scroll = scroll
        self.set_configurations([], None)

    def set_configurations(self, configs: list[Configuration], project_dir: Path | None):
        self.configs = configs
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.widget():
                # Hide now: deleteLater only happens on a later event-loop pass.
                item.widget().hide()
                item.widget().setParent(None)
                item.widget().deleteLater()
        pal = PALETTES["light" if app_is_light() else "dark"]
        for config in configs:
            card = ConfigCard(config, pal)
            card.run_requested.connect(self.run_requested)
            card.reveal_requested.connect(self.reveal_requested)
            card.delete_requested.connect(lambda c: self.delete_requested.emit([c]))
            card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
            self.cards_layout.addWidget(card)
        self.cards_layout.addStretch(1)
        has_current = any(c.current for c in configs)
        total = sum(c.size for c in configs)
        folders = sum(len(c.folders) for c in configs)
        if project_dir is None:
            message = "Open an fpm.toml to see its build outputs."
        elif not configs:
            message = "Nothing has been built yet (no build/<compiler>_<hash> folders)."
        else:
            message = ""
        summary = (f"<b>{len(configs)} configuration{'s' * (len(configs) != 1)}</b> · "
                   f"{folders} folders · {human_size(total)}") if configs else ""
        if configs and not has_current:
            summary += ("<br><span style='color:gray'>The settings of the last build aren't "
                        "recorded yet (their folders were built outside the app) — Clean and "
                        "build again to record them.</span>")
        self.summary.setText(summary)
        self.prune_button.setEnabled(len(configs) > 1 and has_current)
        self.prune_button.setToolTip("" if has_current or not configs else
                                     "No configuration is marked current yet")
        self.empty.setText(message)
        self.empty.setVisible(bool(message))
        self._scroll.setVisible(not message)

