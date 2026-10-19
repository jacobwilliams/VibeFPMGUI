"""The dependency graph: the Graph menu, a package's details and menu, updates."""

from __future__ import annotations

import html
import json
import re
import tomllib
from itertools import pairwise
from pathlib import Path

from qtpy.QtCore import Qt, QUrl
from qtpy.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence, QPalette
from qtpy.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import dep_updates
from .. import manifest as m
from ..compat import reveal_label
from ..graph_view import (
    EDGE_STYLES,
    KIND_LABELS,
    LAYOUTS,
    THEME_CHOICES,
    DependencyGraphView,
)
from ..project_tree import reveal_in_file_manager
from ..widgets import run_dialog


def repo_web_url(git_url: str) -> str | None:
    """The web page of a GitHub/GitLab/Codeberg/Bitbucket repository."""
    url = git_url.strip()
    match = re.match(r"^git@([^:]+):(.+?)(?:\.git)?/?$", url)
    if match:
        host, path = match.groups()
    else:
        match = re.match(r"^(?:https?|git|ssh)://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$", url)
        if not match:
            return None
        host, path = match.groups()
    if not any(h in host for h in ("github.", "gitlab.", "codeberg.", "bitbucket.")):
        return None
    return f"https://{host}/{path}"


def commit_web_url(git_url: str, rev: str) -> str | None:
    base = repo_web_url(git_url)
    if not base or not re.fullmatch(r"[0-9a-fA-F]{7,40}", rev or ""):
        return None
    if "bitbucket." in base:
        return f"{base}/commits/{rev}"
    if "gitlab." in base:
        return f"{base}/-/commit/{rev}"
    return f"{base}/commit/{rev}"


def compare_web_url(git_url: str, old: str, new: str) -> str | None:
    """A page listing the commits between two revisions (GitHub/GitLab)."""
    base = repo_web_url(git_url)
    if not base or not old or not new:
        return None
    if "gitlab." in base:
        return f"{base}/-/compare/{old}...{new}"
    if "github." in base or "codeberg." in base:
        return f"{base}/compare/{old}...{new}"
    return None


def toml_line(name: str, spec) -> str:
    """A dependency as one fpm.toml line: ``name = "1.0"`` or
    ``name = { git = "…", tag = "…" }``."""
    def key(k):
        return k if re.fullmatch(r"[A-Za-z0-9_-]+", str(k)) else json.dumps(str(k))

    def value(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, dict):
            return "{ " + ", ".join(f"{key(k)} = {value(x)}" for k, x in v.items()) + " }"
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        return json.dumps(str(v))  # (a TOML basic string)

    return f"{key(name)} = {value(spec)}"


class PackagesMixin:
    """The dependency graph and its packages (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _build_graph_panel(self):
        """The Dependency graph panel: the graph, the selected package's details and the legend."""
        self.graph_view = DependencyGraphView()
        self.graph_view.package_selected.connect(self.show_package)
        self.graph_view.package_activated.connect(self.open_package_manifest)
        self.graph_view.package_context_menu.connect(self._package_menu)

        self.graph_menu = self._build_graph_menu()
        self.menuBar().addMenu(self.graph_menu)

        self.legend = QLabel()
        self.legend.setWordWrap(True)

        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        self.details.anchorClicked.connect(QDesktopServices.openUrl)
        graph_split = QSplitter(Qt.Horizontal)
        graph_split.addWidget(self.graph_view)
        graph_split.addWidget(self.details)
        graph_split.setStretchFactor(0, 3)
        graph_split.setStretchFactor(1, 1)
        graph_split.setSizes([700, 300])

        graph_tab = QWidget()
        gl = QVBoxLayout(graph_tab)
        gl.addWidget(graph_split, 1)
        gl.addWidget(self.legend)
        return graph_tab

    def _build_graph_menu(self) -> QMenu:
        """Graph menu: layout, theme and edge style choices (radio groups)."""
        menu = QMenu("&Graph", self)
        self.graph_style_actions: dict[str, dict[str, QAction]] = {}
        for title, key, choices, setter in [
            ("Layout", "layout", LAYOUTS, self._set_graph_layout),
            ("Theme", "theme", THEME_CHOICES, self._set_graph_theme),
            ("Edges", "edges", EDGE_STYLES, self._set_graph_edges),
        ]:
            sub = menu.addMenu(title)
            group = QActionGroup(self)
            group.setExclusive(True)
            self.graph_style_actions[key] = {}
            for value, label in choices.items():
                action = QAction(label, self, checkable=True)
                action.triggered.connect(lambda _=False, v=value, f=setter: f(v))
                group.addAction(action)
                sub.addAction(action)
                self.graph_style_actions[key][value] = action
        menu.addSeparator()
        self.dependencies_dev_action = QAction("Show Dependencies' Dev-Dependencies", self, checkable=True)
        self.dependencies_dev_action.setChecked(self.settings.value("graph_dependencies_dev", False, type=bool))
        self.dependencies_dev_action.setStatusTip(
            "Also show what the dependencies list as dev-dependencies (fpm doesn't download those: "
            "only the project's own)")
        self.dependencies_dev_action.toggled.connect(self._set_dependencies_dev)
        menu.addAction(self.dependencies_dev_action)
        menu.addSeparator()
        for text, slot, shortcut in [("Fit to window", lambda: self.graph_view.fit(), "Ctrl+0"),
                                     ("Re-layout", lambda: self.graph_view.relayout(), "Ctrl+Shift+L"),
                                     ("Export image…", self.export_graph, "Ctrl+E")]:
            action = QAction(text, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(slot)
            menu.addAction(action)
        return menu

    def _check_style_action(self, key: str, value: str):
        action = self.graph_style_actions[key].get(value)
        if action:
            action.setChecked(True)

    def _set_graph_layout(self, value: str):
        self.graph_view.set_layout(value)
        self._check_style_action("layout", value)
        self.settings.setValue("graph_layout", value)

    def _set_graph_theme(self, value: str):
        self.graph_view.set_theme(value)
        self._check_style_action("theme", value)
        self.settings.setValue("graph_theme", value)
        self._update_legend()
        self.build_monitor.set_package_colors(self.graph_view.package_colors())

    def _set_graph_edges(self, value: str):
        self.graph_view.set_edge_style(value)
        self._check_style_action("edges", value)
        self.settings.setValue("graph_edges", value)

    def _set_dependencies_dev(self, shown: bool):
        self.settings.setValue("graph_dependencies_dev", shown)
        self.reload()

    def _update_legend(self):
        self.legend.setText(self.graph_view.legend_html())

    def export_graph(self):
        if not self.graph:
            return
        default = str((self.manifest_path.parent if self.manifest_path else Path.home())
                      / f"{self.graph.root}-dependencies.png")
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export dependency graph", default, "PNG image (*.png);;SVG image (*.svg)")
        if filename:
            if self.graph_view.export_image(filename):
                self.statusBar().showMessage(f"Saved {filename}")
            else:
                QMessageBox.warning(self, "Export failed", f"Could not write {filename}")

    def check_dependency_updates(self, only: str | None = None):
        """Commands → Check for Dependency Updates… (only runs when asked);
        ``only``: just that dependency (from its menu in the graph)."""
        if not self.graph:
            return
        checks = dep_updates.git_dependencies(self.graph)
        if only:
            checks = [c for c in checks if c.name == only]
        dialog = dep_updates.UpdatesDialog(checks, compare_web_url, self)
        dialog.update_tag_requested.connect(lambda c: self._update_dependency_tag(c, dialog))
        dialog.update_deps_requested.connect(lambda: (dialog.accept(), self.run_fpm(["update"], False, True)))
        if checks:
            self._update_check_thread = dep_updates.start_check(checks, dialog, self)
        run_dialog(dialog)  # (results still coming then go nowhere: Qt disconnects them)

    def _update_dependency_tag(self, check, dialog):
        """Point one dependency at its newest tag in its fpm.toml."""
        if self.busy():
            QMessageBox.information(dialog, "Update dependency", "Wait for the running command to finish first.")
            return
        # A tag pin moves to the newest tag; a commit pin becomes that tag.
        if not dep_updates.set_ref(check.declared_in, check.name, check.ref_kind, check.ref,
                                   "tag", check.latest_tag):
            QMessageBox.warning(
                dialog, "Update dependency",
                f"Couldn't find {check.ref_kind} = \"{check.ref}\" for {check.name} in "
                f"{check.declared_in} to change it automatically; it is opened in the editor instead.")
            dialog.accept()
            self.open_file(str(check.declared_in), force_editor=True)
            return
        was = check.ref if check.ref_kind == "tag" else f"commit {check.ref[:7]}"
        self.statusBar().showMessage(f"{check.name}: {was} → tag {check.latest_tag} in "
                                     f"{Path(check.declared_in).name}", 6000)
        answer = QMessageBox.question(
            dialog, "Update dependency",
            f"{check.name} now asks for tag {check.latest_tag} (was {was}) in {check.declared_in}.\n\n"
            "Run Update deps now to download it?")
        dialog.accept()
        self.reload()  # fpm.toml changed
        if answer == QMessageBox.Yes:
            self.run_fpm(["update"], False, True)

    def _report_dependency_changes(self):
        """After "fpm update": which dependencies moved to another commit."""
        before, self._revs_before = getattr(self, "_revs_before", None), None
        if before is None or not self.manifest_path:
            return
        after = dep_updates.read_cache_revs(self.manifest_path.parent)
        changed = {n: (before.get(n), rev) for n, rev in after.items() if before.get(n) != rev}
        if not changed:
            self._append("\nNo dependency changed commit.\n")
            return
        lines = ["\nDependency changes:"]
        for name, (old, new) in sorted(changed.items()):
            pkg = self.graph.packages.get(name) if self.graph else None
            url = compare_web_url(pkg.git, old, new) if pkg and pkg.git and old else None
            lines.append(f"  {name}: {(old or 'new')[:7]} → {new[:7]}" + (f"  {url}" if url else ""))
        self._append("\n".join(lines) + "\n", bold=False)
        self.statusBar().showMessage(f"Update deps: {len(changed)} dependenc"
                                     f"{'ies' if len(changed) != 1 else 'y'} changed", 6000)

    def show_package(self, name: str):
        if not self.graph or name not in self.graph.packages:
            return
        pkg = self.graph.packages[name]
        e = html.escape
        rows = [("Type", KIND_LABELS[pkg.kind])]
        # The version: a dependency's own fpm.toml's (which the tag it's pinned to,
        # below, takes precedence over), or else the one asked for or fpm recorded.
        own = pkg.manifest_version if pkg.kind != m.ROOT else ""
        other = pkg.version if pkg.version != own else ""
        for label, value in [("Version", other), ("Manifest version", own), ("License", pkg.license),
                             ("Author", pkg.author)]:
            if value:
                rows.append((label, e(value)))
        if pkg.git:
            web = repo_web_url(pkg.git)
            rows.append(("Git", f'<a href="{e(web)}">{e(pkg.git)}</a>' if web else e(pkg.git)))
        if pkg.git_ref:
            rows.append(("Requested", e(pkg.git_ref)))
        if pkg.git_rev:
            # The exact commit fpm checked out (from build/cache.toml).
            commit_url = commit_web_url(pkg.git, pkg.git_rev)
            short = e(pkg.git_rev[:7])
            rows.append(("Commit", (f'<a href="{e(commit_url)}" title="{e(pkg.git_rev)}">{short}</a>'
                                    if commit_url else f'<span title="{e(pkg.git_rev)}">{short}</span>')
                         + f' <span style="color:gray">({e(pkg.git_rev)})</span>'))
        if pkg.homepage:
            rows.append(("Homepage", f'<a href="{e(pkg.homepage)}">{e(pkg.homepage)}</a>'))
        if pkg.directory:
            url = QUrl.fromLocalFile(str(pkg.directory)).toString()
            rows.append(("Location", f'<a href="{e(url)}">{e(str(pkg.directory))}</a>'))
        deps = self.graph.children(name)
        users = self.graph.parents(name)
        if deps:
            rows.append(("Depends on", ", ".join(
                e(d.child) + (" <i>(dev)</i>" if d.dev else "") for d in deps)))
        if users:
            rows.append(("Used by", ", ".join(e(u.parent) + (" <i>(dev)</i>" if u.dev else "") for u in users)))
        table = "".join(f"<tr><td valign='top'><b>{k}</b>&nbsp;</td><td>{v}</td></tr>"
                        for k, v in rows)
        body = f"<h3>{e(pkg.name)}</h3>"
        if pkg.description:
            body += f"<p>{e(pkg.description)}</p>"
        body += f"<table>{table}</table>"
        if pkg.error:
            body += f"<p style='color:#c92a2a'>{e(pkg.error)}</p>"
        conflict = m.conflicts(self.graph).get(name)
        if conflict:
            used = m.spec_key(conflict[0])
            items = "".join(f"<li><b>{e(d.parent)}</b>{' (dev)' if d.dev else ''} asks for "
                            f"{e(m.describe_spec(d.spec))}"
                            + (" — <b>used</b>" if i == 0 else " <i>(the same)</i>" if m.spec_key(d) == used
                               else "") + "</li>"
                            for i, d in enumerate(conflict))
            # In a red box, so it stands out (Qt's rich text draws borders on tables only).
            dark = self.details.palette().color(QPalette.Base).lightness() < 128
            red, fill = ("#ff6b6b", "#3d2224") if dark else ("#e03131", "#fff0f0")
            body += (f"<table width='100%' cellpadding='6' cellspacing='0' "
                     f"style='margin-top:10px; border:1.5px solid {red}; border-collapse:collapse'><tr><td bgcolor='{fill}'>"
                     f"<p style='color:{red}; margin-top:0'><b>⚠ Version conflict</b></p>"
                     f"<p>{e(name)} is asked for in different ways:</p><ul>{items}</ul>"
                     "<p style='margin-bottom:0'>fpm uses only one of them (the declaration closest "
                     "to the project), so the others get a version they didn't ask for. Pinning the "
                     "same version everywhere — or overriding it in the project's own fpm.toml — "
                     "avoids surprises.</p></td></tr></table>")
        if pkg.manifest:
            body += "<p><i>Double-click the node to open its fpm.toml.</i></p>"
        self.details.setHtml(body)

    def _package_menu(self, name: str, pos):
        """Right-click on a package in the dependency graph."""
        menu = self.package_menu(name)
        if menu is not None:
            menu.exec(pos)
            menu.deleteLater()

    def package_menu(self, name: str) -> QMenu | None:
        """The menu for a package in the dependency graph (None: not one)."""
        pkg = self.graph.packages.get(name) if self.graph else None
        if pkg is None:
            return None
        is_root = pkg.kind == m.ROOT
        on_disk = pkg.directory is not None and pkg.directory.is_dir()
        menu = QMenu(self)

        def add(text, slot, enabled=True, tip=""):
            action = menu.addAction(text)
            action.triggered.connect(slot)
            action.setEnabled(bool(enabled))
            if tip:
                action.setToolTip(tip)
            return action

        add("Open fpm.toml", lambda: self.open_package_manifest(name), pkg.manifest and pkg.manifest.is_file())
        add("Show in Project Files", lambda: self._show_package_files(pkg), on_disk)
        add(reveal_label(), lambda: reveal_in_file_manager(str(pkg.directory)), on_disk)
        if not is_root:
            has_manifest = pkg.manifest and pkg.manifest.is_file()
            add("Open as Project in New Window", lambda: self.open_in_new_window(pkg.manifest), has_manifest,
                "Open this package as a project in its own window (e.g. to work on a local "
                "library), keeping this project open")
            add("Open as Project Here", lambda: self.open_manifest(pkg.manifest), has_manifest,
                "Replace the project in this window with this package")
        links = []
        if pkg.git and (url := repo_web_url(pkg.git)):
            links.append(("Open Repository", url))
        if pkg.git and pkg.git_rev and (url := commit_web_url(pkg.git, pkg.git_rev)):
            links.append((f"Open Checked-out Commit ({pkg.git_rev[:7]})", url))
        if pkg.homepage:
            links.append(("Open Homepage", pkg.homepage))
        if links:
            menu.addSeparator()
            for text, url in links:
                add(text, lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
        if pkg.git and not is_root and pkg.kind != m.DEV_ONLY:  # (fpm doesn't fetch a dependency's dev-dependency)
            menu.addSeparator()
            add("Check for Updates", lambda: self.check_dependency_updates(only=name))
            add("Update This Dependency", lambda: self.run_fpm(["update", name], False, True), not self.busy(),
                f"fpm update {name}: fetch it again (or download it if it's missing)")
        if not is_root:
            menu.addSeparator()
            line = self._dependency_line(name)
            add("Copy Dependency Line", lambda: self._copy_dependency_line(line), line,
                f"Copy “{line}” to paste into another fpm.toml" if line else "")
            add("Why Is This Here?", lambda: self._why_is_this_here(name))
        menu.setToolTipsVisible(True)
        return menu

    def _show_package_files(self, pkg):
        if not self.project_tree.reveal_package(pkg.directory):
            self.statusBar().showMessage(f"{pkg.name} isn't listed in Project Files", 4000)

    def _dependency_line(self, name: str) -> str:
        """How ``name`` is declared, as one fpm.toml line (e.g. ``fmin = {
        git = "…", tag = "1.1.3" }``), from the manifest of a package that
        uses it (preferring the project's own); "" if not found."""
        parents = sorted((self.graph.packages[e.parent] for e in self.graph.parents(name)
                          if e.parent in self.graph.packages),
                         key=lambda p: (p.kind != m.ROOT, p.kind != m.PATH))
        for parent in parents:
            try:
                data = m.load_toml(parent.manifest) if parent.manifest else {}
            except (OSError, tomllib.TOMLDecodeError):
                continue
            tables = [data.get("dependencies"), data.get("dev-dependencies")]
            for section in ("executable", "test", "example"):
                tables += [t.get("dependencies") for t in data.get(section) or [] if isinstance(t, dict)]
            for table in tables:
                if isinstance(table, dict) and name in table:
                    return toml_line(name, table[name])
        return ""

    def _copy_dependency_line(self, line: str):
        QApplication.clipboard().setText(line)
        self.statusBar().showMessage(f"Copied: {line}", 5000)

    def _why_is_this_here(self, name: str):
        """Every chain of dependencies from the project down to ``name``,
        highlighted in the graph and listed in the details panel."""
        chains: list[list[str]] = []

        def walk(node, path):
            if len(chains) >= 50:  # (plenty to see why)
                return
            if node == name:
                chains.append(path)
                return
            for edge in self.graph.children(node):
                if edge.child not in path:
                    walk(edge.child, [*path, edge.child])

        walk(self.graph.root, [self.graph.root])
        if not chains:
            self.statusBar().showMessage(f"{name} isn't reached from {self.graph.root}", 5000)
            return
        dev = {(e.parent, e.child) for e in self.graph.edges if e.dev}
        e = html.escape

        def show(chain):
            out = e(chain[0])
            for a, b in pairwise(chain):
                out += f" → <b>{e(b)}</b>" if b == name else f" → {e(b)}"
                if (a, b) in dev:
                    out += " <i>(dev)</i>"
            return out

        direct = any(len(c) == 2 for c in chains)
        summary = ("a direct dependency of the project" if direct and len(chains) == 1 else
                   f"needed through {len(chains)} chain{'s' * (len(chains) != 1)}")
        self.details.setHtml(f"<h3>Why is {e(name)} here?</h3><p>{e(name)} is {summary}:</p><ul>"
                             + "".join(f"<li>{show(c)}</li>" for c in chains)
                             + "</ul><p><i>Click the background of the graph to clear the highlight.</i></p>")
        self.graph_view.show_chains(name, chains)
        self.statusBar().showMessage(f"{name}: {summary}", 6000)

    def open_package_manifest(self, name: str):
        pkg = self.graph.packages.get(name) if self.graph else None
        if pkg and pkg.manifest:
            self.open_file(str(pkg.manifest))
