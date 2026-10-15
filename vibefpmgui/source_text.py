"""Reading source files: the one set of rules for the editor, Search,
Rename and Replace in Files (so they agree on a file's text, its encoding
and its line ends). No Qt.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

MAX_FILE_SIZE = 8 * 1024 * 1024  # (bigger files aren't searched: see search_text)


class SourceText(NamedTuple):
    text: str        # with "\n" line ends
    encoding: str    # "utf-8", or "latin-1" for a file that isn't UTF-8
    newline: str     # the line end most of its lines have: "\n" or "\r\n"
    mixed: bool      # it has more than one kind of line end


def line_endings(text: str) -> tuple[str, bool]:
    """(the line end most lines of a file's text have: "\\n" or "\\r\\n",
    whether it has more than one kind). Old Mac line ends (a lone "\\r") count
    as another kind: Qt reads them as line breaks, saved as the common one."""
    crlf = text.count("\r\n")
    lone_cr = text.count("\r") - crlf
    lf = text.count("\n") - crlf
    kinds = sum(1 for n in (crlf, lone_cr, lf) if n)
    return ("\r\n" if crlf > lf else "\n"), kinds > 1


def decode(raw: bytes) -> tuple[str, str]:
    """(text, encoding) of a file's bytes: UTF-8, or Latin-1 (which reads
    any bytes) for one that isn't."""
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("latin-1"), "latin-1"


def read_source(path: Path) -> SourceText:
    """A file's text as the editor shows it, and how to write it back (raises
    OSError if it can't be read)."""
    text, encoding = decode(path.read_bytes())
    newline, mixed = line_endings(text)
    return SourceText(text.replace("\r\n", "\n"), encoding, newline, mixed)


def search_text(path: Path) -> str | None:
    """A file's text ("\\n" line ends) to search, or None for one that's
    binary, huge or unreadable."""
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    return decode(raw)[0].replace("\r\n", "\n")
