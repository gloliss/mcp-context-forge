"""L1: driver version reporting.

The conclusions document states a driver version, so the number has to be one a
reader can install. PyMySQL is the reason this is tested rather than assumed: its
``__version__`` attribute reports ``2.2.8`` while the distribution that ships it is
``1.2.0``, so reading the attribute would put a version in the deliverable that
corresponds to no installable release.
"""

from __future__ import annotations

import importlib.metadata

from common.versions import resolve_version


def test_distribution_metadata_wins_over_the_module_attribute() -> None:
    """The installed distribution version is authoritative."""

    class FakeModule:
        """A module whose declared version disagrees with its distribution."""

        __version__ = "2.2.8"

    assert resolve_version("pytest", FakeModule()) == importlib.metadata.version("pytest")
    assert resolve_version("pytest", FakeModule()) != "2.2.8"


def test_module_attribute_is_a_fallback() -> None:
    """A package with no distribution metadata still reports something usable."""

    class FakeModule:
        """A module with only a declared version."""

        __version__ = "1.2.4"

    assert resolve_version("definitely-not-an-installed-distribution", FakeModule()) == "1.2.4"


def test_unknown_when_nothing_is_available() -> None:
    """Neither source available yields an explicit unknown, never a guess."""
    assert resolve_version("definitely-not-an-installed-distribution") == "unknown"
    assert resolve_version("definitely-not-an-installed-distribution", object()) == "unknown"


def test_blank_module_attribute_is_not_accepted() -> None:
    """An empty declared version is no more useful than a missing one."""

    class FakeModule:
        """A module declaring an empty version."""

        __version__ = ""

    assert resolve_version("definitely-not-an-installed-distribution", FakeModule()) == "unknown"
