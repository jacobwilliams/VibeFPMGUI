"""Go to Symbol in Project: find a module, type or procedure by name.

Type part of a name; the language server's matches (from the project and
its dependencies) are listed as you type. Enter or double-click opens the
file at the symbol.
"""

from __future__ import annotations

from pathlib import Path

from qtpy.QtCore import Qt, QTimer, Signal
from qtpy.QtWidgets import (
    QDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from . import lsp as lsp_mod

MAX_RESULTS = 200
LOCATION_ROLE = Qt.UserRole + 1


class SymbolSearchDialog(QDialog):
    chosen = Signal(str, int, int)  # file, line (1-based), column (0-based)

    def __init__(self, client: lsp_mod.LspClient, project_dir: Path | None, parent=None):
        super().__init__(parent)
        self.client = client
        self.project_dir = project_dir
        self.setWindowTitle("Go to Symbol in Project")
        self.resize(640, 420)
        self.query = QLineEdit()
        self.query.setPlaceholderText("Name of a module, type, procedure…")
        self.query.setClearButtonEnabled(True)
        self.results = QListWidget()
        self.results.setUniformItemSizes(True)
        self.note = QLabel("Type part of a name.")
        self.note.setStyleSheet("color: gray;")
        layout = QVBoxLayout(self)
        layout.addWidget(self.query)
        layout.addWidget(self.results, 1)
        layout.addWidget(self.note)

        self._timer = QTimer(self)  # search once typing pauses
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._search)
        self.query.textChanged.connect(self._timer.start)
        self.query.returnPressed.connect(self._accept_current)
        self.results.itemActivated.connect(self._accept_item)
        self.query.installEventFilter(self)
        self._asked = ""
        self._open_first = False  # Enter was pressed before the answer came

    def eventFilter(self, obj, event):
        # Up/Down in the text field move through the results.
        if obj is self.query and event.type() == event.Type.KeyPress and \
                event.key() in (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown):
            self.results.keyPressEvent(event)
            return True
        return super().eventFilter(obj, event)

    def _search(self):
        text = self.query.text().strip()
        self._asked = text
        if not text:
            self.results.clear()
            self.note.setText("Type part of a name.")
            return
        if not self.client.workspace_symbols(text, lambda result, asked=text: self._show(asked, result)):
            self.note.setText("The language server (fortls) isn't running.")

    def _show(self, asked: str, result):
        if asked != self._asked:
            return  # an older search
        symbols = [s for s in result or [] if isinstance(s, dict) and s.get("location")]
        lower = asked.lower()
        # Exact names first, then names starting with the text, then the rest.
        symbols.sort(key=lambda s: (s.get("name", "").lower() != lower,
                                    not s.get("name", "").lower().startswith(lower),
                                    len(s.get("name", "")), s.get("name", "").lower()))
        self.results.clear()
        for s in symbols[:MAX_RESULTS]:
            path = lsp_mod.from_uri(s["location"]["uri"])
            line = s["location"]["range"]["start"]["line"] + 1
            column = s["location"]["range"]["start"]["character"]
            shown = path
            if self.project_dir is not None:
                try:
                    shown = path.relative_to(self.project_dir.resolve())
                except ValueError:
                    pass
            kind = lsp_mod.SYMBOL_KINDS.get(s.get("kind"), "")
            container = f"{s['containerName']}::" if s.get("containerName") else ""
            item = QListWidgetItem(f"{container}{s.get('name', '')}   ({kind})   —   {shown}:{line}")
            item.setData(LOCATION_ROLE, (str(path), line, column))
            item.setToolTip(f"{path}:{line}")
            self.results.addItem(item)
        if self.results.count():
            self.results.setCurrentRow(0)
            if self._open_first:
                self._accept_item(self.results.item(0))
                return
        more = f" (first {MAX_RESULTS} shown)" if len(symbols) > MAX_RESULTS else ""
        self.note.setText(f"{len(symbols)} match{'es' * (len(symbols) != 1)}{more} — "
                          "Enter opens the selected one." if symbols else "No matches.")

    def _accept_current(self):
        if self._timer.isActive() or self.query.text().strip() != self._asked:
            self._open_first = True  # Enter before the search ran: open the best match
            self._timer.stop()
            self._search()
            return
        item = self.results.currentItem()
        if item is not None:
            self._accept_item(item)

    def _accept_item(self, item: QListWidgetItem):
        path, line, column = item.data(LOCATION_ROLE)
        self.accept()
        self.chosen.emit(path, line, column)
