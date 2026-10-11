"""The HTML of the editor's hover card.

A header line (the kind as a coloured badge, like the outline's, then the
name and what it is), fortitude's messages for that spot, the language
server's description (declaration or manual page, see
:func:`.lsp.hover_info`), and a footer with the related shortcuts.
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
LINT_COLORS = {"lint error": "#e03131", "lint": "#0ca678"}


_text_color = None  # the card's text colour (set by the editor, see set_text_color)


def set_text_color(color: QColor):
    """The colour the card's text is drawn in (the editor's, which follows
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


def lint_notes_html(issues) -> str:
    """fortitude's messages: a coloured rule code, the message, and the fix."""
    rows = []
    for issue in issues:
        color = LINT_COLORS.get(issue.severity, "#0ca678")
        fix = (f"<br><span style='color:{_muted()}; font-size:small'>Fix: {html.escape(issue.fix)}</span>"
               if issue.fix else "")
        rows.append(f"<tr><td valign='top' style='padding-right:6px'><span style='background-color:{color}; "
                    f"color:white; font-weight:700; font-size:small'>&nbsp;{html.escape(issue.code)}&nbsp;"
                    f"</span></td><td>{html.escape(issue.message)}{fix}</td></tr>")
    if not rows:
        return ""
    return ("<table cellspacing='0' cellpadding='1' style='margin:0 0 2px 0'>"
            f"<tr><td colspan='2'><span style='color:{_muted()}; font-size:small'>FORTITUDE</span></td></tr>"
            + "".join(rows) + "</table>")


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
