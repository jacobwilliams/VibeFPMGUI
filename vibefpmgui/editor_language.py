"""An editor tab's language-server features: hover, definitions, completion, references, rename, the outline.

A part of `vibefpmgui.editor.EditorTab`."""

from __future__ import annotations

import re

from qtpy.QtCore import Qt
from qtpy.QtGui import QPalette, QTextCursor
from qtpy.QtWidgets import QInputDialog, QLineEdit, QMessageBox

from . import hover_card
from . import lsp as lsp_mod
from .completion import global_rect
from .editor_shared import AUTO_COMPLETE_MIN, DEPTH_ROLE, WORD_RE
from .icons import symbol_icon


class EditorLanguageMixin:
    """The tab's language-server features (a part of `vibefpmgui.editor.EditorTab`)."""

    def attach_lsp(self, client: lsp_mod.LspClient | None):
        """Use a language server for this tab (Fortran files only; None detaches)."""
        if client is self.lsp:
            return
        self.detach_lsp()
        if client is None or not client.is_running() or not lsp_mod.is_fortran(self.path):
            return
        self.lsp = client
        client.open_document(self.path, self.editor.toPlainText())
        self.editor.hover_provider = self._request_hover
        self.outline_bar.show()
        self.refresh_outline()

    def detach_lsp(self):
        self._lsp_timer.stop()
        self._complete_timer.stop()
        self._signature_timer.stop()
        self.editor.hide_popups()
        if self.lsp is not None:
            self.lsp.close_document(self.path)
        self.lsp = None
        self.editor.hover_provider = None
        self._outline = []
        self.outline_box.clear()
        self.outline_bar.hide()

    def _lsp_sync(self):
        """Send the current text (once typing pauses), then refresh the outline."""
        self._lsp_timer.stop()
        if self.lsp is not None:
            self.lsp.change_document(self.path, self.editor.toPlainText())
            self.refresh_outline()

    def _flush_lsp(self):
        """Before a question: make sure the server has the latest text."""
        if self._lsp_timer.isActive():
            self._lsp_sync()

    def _request_hover(self, line: int, column: int, global_pos, lint_html: str):
        if self.lsp is None:
            return
        self._flush_lsp()

        text = self.editor.document().findBlockByNumber(line - 1).text()
        name = re.match(r"\w*", text[column:]).group(0)

        def answered(result):
            hover_card.set_text_color(self.editor.palette().color(QPalette.Text))
            info = lsp_mod.hover_info(result.get("contents")) if isinstance(result, dict) else None
            if info is not None:
                self.editor.show_hover(line, column, global_pos, hover_card.card_html(info, name, lint_html))

        self.lsp.hover(self.path, line, column, answered)

    def go_to_definition(self):
        """Ask where the name at the cursor is defined, and go there."""
        if self.lsp is None:
            return
        self._flush_lsp()
        word = self._name_at_cursor()
        if word is not None:
            line, column = word[0], word[1]
        else:
            cursor = self.editor.textCursor()
            line, column = cursor.blockNumber() + 1, cursor.positionInBlock()

        def answered(result):
            found = lsp_mod.first_location(result)
            if found is None:
                name = f"'{word[2]}'" if word else "the name at the cursor"
                self._update_status(f"no definition found for {name}")
                return
            path, to_line, to_column = found
            self.navigate_requested.emit(str(path), to_line, to_column)

        self.lsp.definition(self.path, line, column, answered)

    def go_to_implementation(self):
        """For a type-bound procedure: the procedure it binds (Go to
        Definition stops at the binding); for a generic interface: its first
        module procedure."""
        if self.lsp is None:
            return
        self._flush_lsp()
        word = self._name_at_cursor()
        if word is not None:
            line, column = word[0], word[1]
        else:
            cursor = self.editor.textCursor()
            line, column = cursor.blockNumber() + 1, cursor.positionInBlock()

        def answered(result):
            found = lsp_mod.first_location(result)
            if found is None:
                name = f"'{word[2]}'" if word else "the name at the cursor"
                self._update_status(f"no implementation found for {name} (only type-bound procedures "
                                    "and generic interfaces have one)")
                return
            path, to_line, to_column = found
            self.navigate_requested.emit(str(path), to_line, to_column)

        self.lsp.implementation(self.path, line, column, answered)

    def refresh_outline(self):
        if self.lsp is None:
            return
        self.lsp.document_symbols(self.path, self._show_outline)

    def _show_outline(self, result):
        if self.lsp is None:
            return
        document = self.editor.document()
        self._outline = lsp_mod.outline(result, lambda n: document.findBlockByNumber(n - 1).text())
        self.outline_box.blockSignals(True)
        self.outline_box.clear()
        # Each entry: a badge for its kind, then its name; the list indents
        # nested entries (see _OutlineDelegate).
        for depth, kind, name, first, _last in self._outline:
            self.outline_box.addItem(symbol_icon(kind), name)
            row = self.outline_box.count() - 1
            self.outline_box.setItemData(row, f"{kind} {name} (line {first})", Qt.ToolTipRole)
            self.outline_box.setItemData(row, depth, DEPTH_ROLE)
        self.outline_box.setPlaceholderText(f"Outline ({len(self._outline)})" if self._outline
                                            else "Outline (no procedures)")
        self.outline_box.blockSignals(False)
        self._outline_follow_cursor()

    def _outline_chosen(self, index: int):
        if 0 <= index < len(self._outline):
            self.editor.go_to_line(self._outline[index][3])

    def _outline_follow_cursor(self):
        """Show the innermost module/procedure the cursor is in."""
        if not self._outline:
            return
        line = self.editor.textCursor().blockNumber() + 1
        best = -1
        for i, (_depth, _kind, _name, first, last) in enumerate(self._outline):
            if first <= line <= last:
                best = i  # rows are in file order, so later = deeper
        if best != self.outline_box.currentIndex():
            self.outline_box.blockSignals(True)
            self.outline_box.setCurrentIndex(best)
            self.outline_box.blockSignals(False)

    def _typed(self, text: str):
        """After a key changed the text: offer completions or argument hints."""
        if self.lsp is None or not text:
            return
        ch = text[-1]
        if WORD_RE.match(ch):
            if not self.editor.completion_popup.isVisible():
                self._complete_timer.start()  # request_completion checks the length
        elif ch == "%":  # a type's components
            self._complete_timer.start()
        elif ch in "(," or ch == ")" and self.editor.signature_popup.isVisible():
            self._signature_timer.start()

    def _name_start(self) -> tuple[int, int, str, str]:
        """(block number, column where the name before the cursor starts,
        that partial name, the line's text) for the cursor's position."""
        cursor = self.editor.textCursor()
        text = cursor.block().text()
        column = cursor.positionInBlock()
        start = column
        while start > 0 and WORD_RE.match(text[start - 1]):
            start -= 1
        return cursor.blockNumber(), start, text[start:column], text

    def request_completion(self, manual: bool = True):
        """Ask for completions of the name being typed (Show Completions
        when ``manual``, or by itself once a few letters are typed)."""
        if self.lsp is None:
            return
        block, start, prefix, text = self._name_start()
        column = self.editor.textCursor().positionInBlock()
        after_percent = start > 0 and text[start - 1] == "%"
        if not manual:
            if lsp_mod.in_comment_or_string(text, column) or prefix[:1].isdigit():
                return
            if len(prefix) < AUTO_COMPLETE_MIN and not after_percent:
                return
        self._flush_lsp()

        def answered(result):
            cursor = self.editor.textCursor()
            if cursor.blockNumber() != block or cursor.positionInBlock() < start:
                return  # the cursor has moved on
            now = cursor.block().text()[start:cursor.positionInBlock()]
            if not all(WORD_RE.match(c) for c in now):
                return
            anchor_cursor = QTextCursor(cursor.block())
            anchor_cursor.setPosition(cursor.block().position() + start)
            items = lsp_mod.completions(result)
            shown = self.editor.completion_popup.open(items, (block, start), now,
                                                      global_rect(self.editor, anchor_cursor))
            if manual and not shown:
                self._update_status("no completions here")

        self.lsp.completion(self.path, block + 1, column, answered)

    def accept_completion(self, item):
        popup = self.editor.completion_popup
        start = popup.start
        popup.hide()
        if item is None or start is None:
            return
        cursor = self.editor.textCursor()
        if cursor.blockNumber() != start[0]:
            return
        block = cursor.block()
        cursor.setPosition(block.position() + start[1])
        cursor.setPosition(self.editor.textCursor().position(), QTextCursor.KeepAnchor)
        cursor.insertText(item.insert)
        self.editor.setTextCursor(cursor)

    def request_signature(self):
        """Show (or update, or hide) the argument hints for the call the
        cursor is in."""
        popup = self.editor.signature_popup
        if self.lsp is None:
            return
        cursor = self.editor.textCursor()
        block, column = cursor.blockNumber(), cursor.positionInBlock()
        before = cursor.block().text()[:column]
        index = lsp_mod.argument_index(before)
        if index is None or lsp_mod.in_comment_or_string(cursor.block().text(), column):
            popup.hide()
            return
        self._flush_lsp()

        def answered(result):
            now = self.editor.textCursor()
            if now.blockNumber() != block:
                return
            text = lsp_mod.signature_html(result, index)
            if not text:
                popup.hide()
                return
            line_start = QTextCursor(now.block())
            rect = global_rect(self.editor, now)
            rect.moveLeft(global_rect(self.editor, line_start).left() + self.editor.fontMetrics()
                          .horizontalAdvance(" ") * (len(before) - len(before.lstrip())))
            popup.open(text, block, rect)

        self.lsp.signature_help(self.path, block + 1, column, answered)

    def _cursor_moved_popups(self):
        """Keep the completion list filtered to what's typed (or close it),
        and the argument hints up to date."""
        cursor = self.editor.textCursor()
        block, column = cursor.blockNumber(), cursor.positionInBlock()
        popup = self.editor.completion_popup
        if popup.isVisible():
            start = popup.start
            typed = cursor.block().text()[start[1]:column] if start and start[0] == block else None
            if typed is None or column < start[1] or not all(WORD_RE.match(c) for c in typed):
                popup.hide()
            else:
                popup.filter(typed)
        hint = self.editor.signature_popup
        if hint.isVisible():
            if hint.line != block:
                hint.hide()
            else:
                self._signature_timer.start()

    def _name_at_cursor(self) -> tuple[int, int, str] | None:
        """(1-based line, start column, name) of the name at the cursor, or
        of the selected text if that is a single name."""
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            selected = cursor.selectedText()
            if re.fullmatch(r"[A-Za-z_]\w*", selected):
                start = QTextCursor(self.editor.document())
                start.setPosition(cursor.selectionStart())
                return start.blockNumber() + 1, start.positionInBlock(), selected
        text = cursor.block().text()
        i = cursor.positionInBlock()
        if not (i < len(text) and WORD_RE.match(text[i])):
            if i > 0 and WORD_RE.match(text[i - 1]):
                i -= 1
            else:
                return None
        start, end = i, i
        while start > 0 and WORD_RE.match(text[start - 1]):
            start -= 1
        while end < len(text) and WORD_RE.match(text[end]):
            end += 1
        word = text[start:end]
        return None if word[0].isdigit() else (cursor.blockNumber() + 1, start, word)

    def find_references(self):
        if self.lsp is None:
            return
        found = self._name_at_cursor()
        if found is None:
            self._update_status("put the cursor on a name to find its references")
            return
        line, column, name = found
        self._flush_lsp()
        self.lsp.references(self.path, line, column,
                            lambda result: self.references_found.emit(name, lsp_mod.locations(result)))

    def rename_symbol(self):
        if self.lsp is None:
            return
        found = self._name_at_cursor()
        if found is None:
            self._update_status("put the cursor on a name to rename it")
            return
        line, column, name = found
        new, ok = QInputDialog.getText(self, "Rename Symbol", f"Rename '{name}' everywhere it is used, to:",
                                       QLineEdit.Normal, name)
        new = new.strip()
        if not ok or not new or new == name:
            return
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", new):
            QMessageBox.warning(self, "Rename Symbol", f"'{new}' isn't a valid Fortran name.")
            return
        self._flush_lsp()

        def answered(result):
            edits = lsp_mod.workspace_edits(result)
            if not edits:
                self._update_status(f"'{name}' can't be renamed (fortls found nothing to change)")
                return
            self.rename_ready.emit(name, new, edits)

        self.lsp.rename(self.path, line, column, new, answered)

    def apply_text_edits(self, edits):
        """Apply language-server edits to the text as one Undo step."""
        document = self.editor.document()
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        for e in sorted(edits, key=lambda e: (e.line, e.column), reverse=True):
            start, end = document.findBlockByNumber(e.line), document.findBlockByNumber(e.end_line)
            if not start.isValid() or not end.isValid():
                continue
            cursor.setPosition(start.position() + min(e.column, start.length() - 1))
            cursor.setPosition(end.position() + min(e.end_column, end.length() - 1), QTextCursor.KeepAnchor)
            cursor.insertText(e.new_text)
        cursor.endEditBlock()
