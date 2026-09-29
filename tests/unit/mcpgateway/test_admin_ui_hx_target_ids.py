# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_admin_ui_hx_target_ids.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests that every ``hx-target="#id"`` selector names an element that exists.

htmx resolves ``hx-target`` at request time with a document query. When nothing
defines the id, the request aborts with ``htmx:targetError`` and the swap never
happens: the control looks dead and nothing reports why. The failure is invisible
in the rendered page, so it survives review -- four pagination links in the MCP
registry targeted an id that no template ever defined.

The check is a source-level invariant over the templates and the admin_ui
modules. It cannot judge whether the element chosen is the right one, only that
the id it names is defined somewhere. The scan is textual, so an id that appears
inside a string counts as defined; it can never prove an element is rendered,
only that the identifier exists.
"""

from pathlib import Path
import re
from typing import Dict, List, Set

# Fewer matches than these mean the scan stopped matching (a reformatted
# attribute, a moved directory) and the assertion below would pass while
# inspecting nothing.
MIN_TARGETS = 15
MIN_DEFINED_IDS = 800

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATES_DIR = REPO_ROOT / "mcpgateway" / "templates"
ADMIN_UI_DIR = REPO_ROOT / "mcpgateway" / "admin_ui"

# Only "#id" selectors are checked. A selector such as "closest div" or a
# server-rendered one like "#{{ server.id }}-menu" cannot be resolved statically
# and is deliberately left alone.
TARGET = re.compile(r"""hx-target\s*=\s*["']#([A-Za-z][\w:.\-]*)["']""")

# The lookbehind keeps attribute names that merely end in "id" (data-id,
# aria-describedby is not affected) from being read as element ids.
ID_ATTR = re.compile(r"""(?<![\w\-])id\s*=\s*["']([A-Za-z][\w:.\-]*)["']""")
ID_PROP = re.compile(r"""\.id\s*=\s*["']([A-Za-z][\w:.\-]*)["']""")


def defined_ids_in(text: str) -> Set[str]:
    """Return the element ids ``text`` defines, as attributes or JS assignments."""
    return set(ID_ATTR.findall(text)) | set(ID_PROP.findall(text))


def target_ids_in(text: str) -> List[str]:
    """Return the ids named by ``hx-target="#..."`` selectors in ``text``."""
    return TARGET.findall(text)


def _template_files() -> List[Path]:
    return sorted(TEMPLATES_DIR.rglob("*.html"))


def collect_defined_ids() -> Set[str]:
    """Every id defined anywhere in the templates or the admin_ui modules."""
    ids: Set[str] = set()
    for path in _template_files() + sorted(ADMIN_UI_DIR.rglob("*.js")):
        ids |= defined_ids_in(path.read_text(encoding="utf-8", errors="replace"))
    return ids


def collect_target_refs() -> Dict[str, List[str]]:
    """Map each targeted id to the ``file:line`` locations that target it."""
    refs: Dict[str, List[str]] = {}
    for path in _template_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            for target in target_ids_in(line):
                refs.setdefault(target, []).append(f"{path.relative_to(REPO_ROOT)}:{number}")
    return refs


class TestHxTargetIds:
    """Every hx-target selector must name a defined id."""

    def test_scanner_finds_the_targets(self) -> None:
        """A scan that matches nothing must not be able to pass silently."""
        refs = collect_target_refs()
        defined = collect_defined_ids()

        assert sum(len(locations) for locations in refs.values()) >= MIN_TARGETS, f"scanned only {sum(len(v) for v in refs.values())} hx-target selectors; the scan is no longer matching"
        assert len(defined) >= MIN_DEFINED_IDS, f"found only {len(defined)} defined ids; the id scan is no longer matching"

    def test_scanner_flags_a_dangling_target(self) -> None:
        """Positive control: a target whose id is defined nowhere is reported."""
        sample = '<div hx-target="#ghost"></div><div id="real"></div>'

        targets = target_ids_in(sample)
        defined = defined_ids_in(sample)

        assert targets == ["ghost"]
        assert defined == {"real"}
        assert targets[0] not in defined

    def test_scanner_reads_ids_assigned_in_javascript(self) -> None:
        """Ids created by admin_ui modules count as defined."""
        assert defined_ids_in('element.id = "built-in-js";') == {"built-in-js"}

    def test_id_scan_ignores_prefixed_attributes(self) -> None:
        """``data-id`` is not an element id and must not mask a dangling target."""
        assert defined_ids_in('<div data-id="decoy"></div>') == set()

    def test_every_hx_target_id_is_defined(self) -> None:
        """No hx-target may point at an id that nothing defines."""
        defined = collect_defined_ids()
        refs = collect_target_refs()

        dangling = {target: locations for target, locations in refs.items() if target not in defined}

        assert not dangling, "hx-target names an id that no template or admin_ui module defines:\n  " + "\n  ".join(
            f"#{target}: " + ", ".join(locations) for target, locations in sorted(dangling.items())
        )
