"""The HTML of the editor's hover card.

A header line (the kind as a colored badge, like the outline's, then the
name and what it is), fortitude's messages for that spot, the language
server's description (declaration or manual page, see
`vibefpmgui.lsp.hover_info`), and a footer with the related shortcuts.
"""

from __future__ import annotations

import html

from qtpy.QtGui import QColor, QPalette
from qtpy.QtWidgets import QApplication

from . import lsp
from .icons import SYMBOL_BADGES

# Kinds the outline has no badge for.
EXTRA_BADGES = {"variable": ("V", "#495057"), "parameter": ("K", "#5c940d"),
                "intrinsic": ("intrinsic", "#4263eb")}
LINT_COLORS = {"lint error": "#e03131", "lint": "#0ca678", "fortls error": "#e03131", "fortls": "#1098ad",
               "spelling": "#ae3ec9"}
# The heading over each tool's notes.
SOURCE_TITLES = {"fortitude": "FORTITUDE", "fortls": "FORTLS (checks the saved file)", "spelling": "SPELLING"}


_text_color = None  # the card's text color (set by the editor, see set_text_color)


def set_text_color(color: QColor):
    """The color the card's text is drawn in (the editor's, which follows
    the syntax style rather than the system's light/dark mode)."""
    global _text_color
    _text_color = QColor(color)


def _muted() -> str:
    color = QColor(_text_color or QApplication.palette().color(QPalette.Text))
    color.setAlpha(150)
    return color.name(QColor.HexArgb)


def badge(kind: str) -> str:
    letter, color = SYMBOL_BADGES.get(kind) or EXTRA_BADGES.get(kind) or ("?", "#868e96")
    return (f"<span style='background-color:{color}; color:white; font-weight:700; "
            f"font-size:small'>&nbsp;{html.escape(letter.upper() if len(letter) > 1 else letter)}&nbsp;</span>")


def lint_notes_html(issues, fix_ids: list[int] | None = None) -> str:
    """fortitude's (and fortls's) messages: a colored rule code, the
    message, and the fix, under a heading per tool.
    With ``fix_ids`` (each issue's number in the editor), fixable issues get
    an "Apply fix" link (``fix:N``), and misspelled words their suggestions
    (``spell:N:word``) and "Add to Dictionary" (``addword:N``)."""
    groups: dict[str, list[str]] = {}
    for n, issue in enumerate(issues):
        rows = groups.setdefault(getattr(issue, "source", "fortitude"), [])
        color = LINT_COLORS.get(issue.severity, "#0ca678")
        link = ""
        if fix_ids is not None and getattr(issue, "fixable", False):
            note = "" if issue.fix_safe else f" <i style='color:{_muted()}'>(unsafe — check it)</i>"
            link = f" &nbsp;<span style='font-size:small'><a href='fix:{fix_ids[n]}'>Apply fix</a>{note}</span>"
        if fix_ids is not None and hasattr(issue, "suggestions"):
            words = [f"<a href='spell:{fix_ids[n]}:{html.escape(w)}'>{html.escape(w)}</a>"
                     for w in issue.suggestions()]
            link = (f"<br><span style='font-size:small'>{', '.join(words) or 'No suggestions'}"
                    f" &nbsp;·&nbsp; <a href='addword:{fix_ids[n]}'>Add to Dictionary</a></span>")
        fix = (f"<br><span style='color:{_muted()}; font-size:small'>Fix: {html.escape(issue.fix)}</span>{link}"
               if issue.fix else link)
        rows.append(f"<tr><td valign='top' style='padding-right:6px'><span style='background-color:{color}; "
                    f"color:white; font-weight:700; font-size:small'>&nbsp;{html.escape(issue.code)}&nbsp;"
                    f"</span></td><td>{html.escape(issue.message)}{fix}</td></tr>")
    if not groups:
        return ""
    return "".join(
        "<table cellspacing='0' cellpadding='1' style='margin:0 0 2px 0'>"
        f"<tr><td colspan='2'><span style='color:{_muted()}; font-size:small'>"
        f"{SOURCE_TITLES.get(source, source.upper())}</span></td></tr>" + "".join(rows) + "</table>"
        for source, rows in groups.items())


def card_html(info: lsp.HoverInfo | None, name: str, lint_html: str = "") -> str:
    """The whole card for a name: header, lint messages, description, footer."""
    parts = []
    if info is not None:
        what = "" if info.kind == "intrinsic" else info.kind  # (the badge says it)
        header = f"{badge(info.kind)}&nbsp; <b>{html.escape(name)}</b>" if info.kind else f"<b>{html.escape(name)}</b>"
        if what:
            header += f" &nbsp;<span style='color:{_muted()}'>{html.escape(what)}</span>"
        if info.category:
            header += (f" &nbsp;<span style='color:{_muted()}; font-size:small'>· "
                       f"{html.escape(info.category)}</span>")
        parts.append(f"<p style='margin:0 0 4px 0'>{header}</p>")
        if info.summary:
            parts.append(f"<p style='margin:0 0 4px 0'><i>{html.escape(info.summary)}</i></p>")
    if lint_html:
        parts.append(lint_html)
        if info is not None:
            parts.append("<hr>")
    if info is not None:
        parts.append(info.body_html)
        if info.kind != "intrinsic":
            parts.append(f"<p style='margin:6px 0 0 0'><span style='color:{_muted()}; font-size:small'>"
                         "F12 definition &nbsp;·&nbsp; ⇧F12 references &nbsp;·&nbsp; F2 rename</span></p>")
    return "".join(parts)
