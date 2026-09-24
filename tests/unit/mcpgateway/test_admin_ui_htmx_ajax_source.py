# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_admin_ui_htmx_ajax_source.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests that every ``htmx.ajax()`` call site names its own request element.

htmx attributes a request to ``context.source``. Without one, ``issueAjaxRequest``
falls back to ``document.body``, so every anonymous call site shares a single
element's in-flight bookkeeping, and because the default queue strategy is
``last`` a request issued while another is running is dropped instead of being
queued. Naming a source also keeps the request element attached: htmx skips the
request entirely when the source is not in the document.

The check is a source-level invariant. It cannot judge whether the element chosen
at a call site is a sensible one, only that every call site names one.
"""

from pathlib import Path
import re
from typing import Iterator

CALL = re.compile(r"htmx\s*\.\s*ajax\s*\(")
SOURCE = re.compile(r"\bsource\s*:")

# Fewer call sites than this means the scan stopped matching (a reformatted call,
# a renamed helper), and the "every call site declares source" assertion below
# would then pass without inspecting anything.
MIN_CALL_SITES = 30

REPO_ROOT = Path(__file__).resolve().parents[3]

# A "/" after one of these starts a regular expression literal rather than a
# division, which matters because a literal like /"/g would otherwise be read as
# the start of a string and desynchronise the whole file.
REGEX_PREFIX = set("(,=:[!&|?{};+-*%~^<>")


def comment_spans(text: str) -> list:
    """Return the ``(start, end)`` spans of ``//``, ``/* */`` and regex-literal regions.

    Spans never begin inside a string literal, so an apostrophe in a comment
    ("document.body's") cannot leak into the argument scan.
    """
    spans = []
    i = 0
    n = len(text)
    prev = ""
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch in "\"'`":
            quote = ch
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    break
                i += 1
            i += 1
            prev = quote
        elif ch == "/" and nxt == "/":
            end = text.find("\n", i)
            end = n if end == -1 else end
            spans.append((i, end))
            i = end
        elif ch == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            spans.append((i, end))
            i = end
        elif ch == "/" and (prev == "" or prev in REGEX_PREFIX):
            j = i + 1
            in_class = False
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "[":
                    in_class = True
                elif text[j] == "]":
                    in_class = False
                elif text[j] == "/" and not in_class:
                    break
                elif text[j] == "\n":
                    break
                j += 1
            spans.append((i, j + 1))
            i = j + 1
            prev = "/"
        else:
            if not ch.isspace():
                prev = ch
            i += 1
    return spans


def _in_comment(spans: list, idx: int) -> bool:
    """Report whether ``idx`` falls inside one of ``spans``."""
    return any(start <= idx < end for start, end in spans)


def _on_commented_line(text: str, idx: int) -> bool:
    """Report whether ``idx`` is preceded by ``//`` on its own line, or sits in a block comment.

    Regular expression literals (``.replace(/&/g, '&amp;')``) look like a comment
    to the string walker above and can desynchronise it for a whole file. This
    coarser rule covers that case; a call site is only skipped when both rules
    agree it is prose, and a real call never follows ``//`` on its own line.
    """
    line_start = text.rfind("\n", 0, idx) + 1
    if text.rfind("//", line_start, idx) != -1:
        return True
    return text.rfind("/*", 0, idx) > text.rfind("*/", 0, idx)


def _matching_paren(text: str, open_idx: int, spans: list) -> int:
    """Return the index of the ``)`` closing the ``(`` at ``open_idx``, or -1.

    Comments and string literals are skipped, so braces or parentheses inside
    them do not affect the depth count.
    """
    depth = 0
    i = open_idx
    n = len(text)
    j = 0
    while j < len(spans) and spans[j][1] <= open_idx:
        j += 1
    while i < n:
        if j < len(spans) and spans[j][0] <= i < spans[j][1]:
            i = spans[j][1]
            continue
        if j < len(spans) and i >= spans[j][1]:
            j += 1
            continue
        ch = text[i]
        if ch in "\"'`":
            quote = ch
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\":
                    i += 1
                i += 1
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def call_sites(text: str) -> Iterator[tuple]:
    """Yield ``(line, arguments)`` for each ``htmx.ajax()`` call in ``text``.

    Prose that merely mentions the call is skipped, since the modules document
    the source requirement in comments next to the real call sites. A call whose
    arguments cannot be delimited is reported rather than skipped, so a parsing
    gap can never shrink the coverage silently.
    """
    spans = comment_spans(text)
    for match in CALL.finditer(text):
        if _in_comment(spans, match.start()) or _on_commented_line(text, match.start()):
            continue
        close = _matching_paren(text, match.end() - 1, spans)
        line = text.count("\n", 0, match.start()) + 1
        if close == -1:
            raise ValueError(f"unterminated htmx.ajax() call at line {line}; the argument scan needs fixing")
        yield line, text[match.end() - 1 : close + 1]


def scanned_files() -> list:
    """Return the admin UI templates and modules that may call ``htmx.ajax()``."""
    templates = sorted(REPO_ROOT.glob("mcpgateway/templates/*.html"))
    modules = sorted((REPO_ROOT / "mcpgateway/admin_ui").rglob("*.js"))
    return templates + modules


def all_call_sites() -> list:
    """Return ``(relative_path, line, arguments)`` for every call site found."""
    sites = []
    for path in scanned_files():
        text = path.read_text(encoding="utf-8")
        for line, args in call_sites(text):
            sites.append((path.relative_to(REPO_ROOT).as_posix(), line, args))
    return sites


def test_scanner_still_finds_the_call_sites():
    """Guard against the scan going quiet and the invariant passing vacuously."""
    sites = all_call_sites()
    assert len(sites) >= MIN_CALL_SITES, (
        f"only {len(sites)} htmx.ajax() call sites found across {len(scanned_files())} files; the scanner no longer matches the source, so the source invariant cannot be trusted"
    )


def test_scanner_flags_a_call_without_source(tmp_path: Path):
    """Guard the guard: the extraction must expose a source-less call."""
    js = tmp_path / "sample.js"
    js.write_text("window.htmx\n  .ajax('GET', url, { target: '#panel', swap: 'innerHTML' });\n", encoding="utf-8")

    sites = list(call_sites(js.read_text(encoding="utf-8")))

    assert len(sites) == 1
    assert not SOURCE.search(sites[0][1])


def test_scanner_ignores_comment_mentions(tmp_path: Path):
    """A comment mentioning the call is prose, not a call site."""
    js = tmp_path / "sample.js"
    js.write_text("// htmx.ajax() without a source shares document.body's in-flight state\n", encoding="utf-8")

    assert list(call_sites(js.read_text(encoding="utf-8"))) == []


def test_scanner_survives_quotes_inside_a_call(tmp_path: Path):
    """An apostrophe in a comment must not truncate the call it sits in."""
    js = tmp_path / "sample.js"
    js.write_text(
        "window.htmx.ajax('GET', url, {\n  // document.body's in-flight state, so this would otherwise be dropped\n  source: el,\n  target: '#panel',\n});\n",
        encoding="utf-8",
    )

    sites = list(call_sites(js.read_text(encoding="utf-8")))

    assert len(sites) == 1
    assert SOURCE.search(sites[0][1])


def test_every_call_site_passes_source():
    """Every call site must name the element the request belongs to."""
    missing = [(rel, line) for rel, line, args in all_call_sites() if not SOURCE.search(args)]

    assert missing == [], f"these htmx.ajax() call sites omit source, so the request is attributed to document.body and is dropped whenever another request is in flight: {missing}"
