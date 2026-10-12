"""Spell checking the editor's comments and strings, with pyspellchecker.

Only prose is checked: the comments and strings the syntax highlighter
found (all of a plain text or Markdown file). Words that look like code are
skipped: with digits, underscores or dots in them, ALL CAPS, camelCase, and
any word the file uses as a name or keyword. Fortran keywords missing from
the dictionary (`FORTRAN_WORDS`) and the user's own dictionary
(`user_words`) count as spelled right.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from functools import cache
from pathlib import Path
from typing import NamedTuple

try:
    from spellchecker import SpellChecker
except ImportError:  # (optional: no spell checking without it)
    SpellChecker = None

MAX_ISSUES = 2000  # per file (a file in another language would flag every word)
MAX_SUGGESTIONS = 5
LONG_WORD = 10  # longer than this: suggestions one edit away only (two is slow for long words)

WORDS_PATH = Path(__file__).with_name("resources") / "spelling-words.txt"


def _read_words(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {line.strip().lower() for line in lines if line.strip() and not line.startswith("#")}


# Fortran keywords pyspellchecker's dictionary doesn't have ("enddo", "namelist"…).
FORTRAN_WORDS = _read_words(WORDS_PATH)

user_words: set[str] = set()  # the user's dictionary (lowercase), kept in the settings by the window


def available() -> bool:
    return SpellChecker is not None


# pyspellchecker's dictionaries (see `languages`), by their codes.
LANGUAGE_NAMES = {"en": "English", "es": "Spanish", "fr": "French", "it": "Italian", "pt": "Portuguese",
                  "de": "German", "ru": "Russian", "ar": "Arabic", "lv": "Latvian", "eu": "Basque",
                  "nl": "Dutch"}
language = "en"  # the dictionary in use (View → Spelling Language; see set_language)


def languages() -> list[str]:
    """The codes of the dictionaries pyspellchecker has (none without it)."""
    return list(SpellChecker.languages()) if available() else []


def set_language(code: str):
    """Check with another language's dictionary (from the next check on)."""
    global language
    if code == language or code not in languages():
        return
    language = code
    _known.clear()
    suggestions.cache_clear()


@cache
def _checker(code: str = "en"):
    return SpellChecker(language=code)


@cache
def _short_checker(code: str = "en"):
    return SpellChecker(language=code, distance=1)


_known: dict[str, bool] = {}  # lowercase word -> in the dictionary


def is_known(word: str) -> bool:
    w = word.lower()
    if w in user_words or w in FORTRAN_WORDS:
        return True
    known = _known.get(w)
    if known is None:
        known = _known[w] = bool(_checker(language).known([w]))
    return known


@cache
def suggestions(word: str) -> tuple[str, ...]:
    """Likely corrections, most likely first, in the word's capitalization."""
    if not available():
        return ()
    w = word.lower()
    found = _short_checker(language).candidates(w) or set()
    if not found and len(w) <= LONG_WORD:
        found = _checker(language).candidates(w) or set()
    found.discard(w)
    ranked = sorted(found, key=lambda c: (-_checker(language).word_usage_frequency(c), c))[:MAX_SUGGESTIONS]
    if word[:1].isupper():
        ranked = [c[:1].upper() + c[1:] for c in ranked]
    return tuple(ranked)


class SpellingIssue(NamedTuple):
    """A word that isn't in the dictionary, shown like fortitude's issues."""
    line: int        # 1-based
    column: int      # 1-based
    end_line: int
    end_column: int
    word: str
    code: str = "spelling"
    fix: str = ""
    fix_safe: bool = False
    edits: tuple = ()
    source: str = "spelling"

    @property
    def message(self) -> str:
        return f"“{self.word}” isn't in the dictionary"

    @property
    def severity(self) -> str:
        return "spelling"

    @property
    def fixable(self) -> bool:
        return False

    def suggestions(self) -> tuple[str, ...]:
        return suggestions(self.word)

    def describe(self) -> str:
        return f"spelling: {self.message}"


# A comment that's really an instruction to the compiler: OpenMP, OpenACC,
# compiler directives ("!$omp parallel do", "!DIR$ IVDEP", "!GCC$ ATTRIBUTES").
DIRECTIVE_RE = re.compile(r"^\s*[!cC*]\s*(\$|dir\$|dec\$|gcc\$|ibm\*|acc\$|pgi\$)", re.IGNORECASE)
CHUNK_RE = re.compile(r"\S+")
EDGE = "\"'`‘’“”()[]{}<>,.;:!?*~#|=+&^%$@\\/"
WORD_CHARS = re.compile(r"^[^\W\d_]+(?:['’][^\W\d_]+)*$")  # (letters of any language)


def _words(segment: str):
    """(offset, word) of each word in a piece of prose worth checking."""
    for m in CHUNK_RE.finditer(segment):
        chunk = m.group()
        start = m.start()
        stripped = chunk.lstrip(EDGE)
        start += len(chunk) - len(stripped)
        stripped = stripped.rstrip(EDGE)
        offset = start
        for part in stripped.split("-"):  # ("well-known": both halves)
            word = part
            for suffix in ("'s", "’s"):
                if word.endswith(suffix):
                    word = word[:-2]
            if (len(word) >= 3 and WORD_CHARS.match(word) and not word.isupper()
                    and not any(c.isupper() for c in word[1:])):  # (not ALL CAPS or camelCase)
                yield offset, word
            offset += len(part) + 1


def check(text: str, prose: list[tuple[int, int]], names: set[str] = frozenset()) -> list[SpellingIssue]:
    """The misspelled words in ``text``'s ``prose`` ((position, length) of
    each comment or string). ``names``: words the file uses as names
    or keywords (lowercase), which aren't misspellings."""
    if not available():
        return []
    found: list[tuple[int, str]] = []
    for pos, length in prose:
        segment = text[pos:pos + length]
        if DIRECTIVE_RE.match(segment):
            continue
        for offset, word in _words(segment):
            if word.lower() not in names and not is_known(word):
                found.append((pos + offset, word))
                if len(found) >= MAX_ISSUES:
                    break
        if len(found) >= MAX_ISSUES:
            break
    if not found:
        return []
    line_starts = [0] + [m.end() for m in re.finditer("\n", text)]
    issues = []
    for pos, word in found:
        line = bisect_right(line_starts, pos)
        column = pos - line_starts[line - 1] + 1
        issues.append(SpellingIssue(line, column, line, column + len(word), word))
    return issues
