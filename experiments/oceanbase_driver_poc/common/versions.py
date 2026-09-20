"""Accurate driver version reporting.

The conclusions document has to state the driver version a result was produced with,
so the number in the report has to be the one a reader can install.

``module.__version__`` is not a reliable source for that. PyMySQL ships
``__version__ = "2.2.8"`` inside the 1.2.0 distribution, so reading the attribute
would put a version in the deliverable that does not correspond to any installable
release. Distribution metadata is the authoritative source; the attribute is only a
fallback for packages that publish no metadata.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any


def resolve_version(distribution: str, module: Any | None = None) -> str:
    """Return the installed version of a driver distribution.

    Args:
        distribution: The distribution name, as installed (for example ``"pymysql"``).
        module: The imported module, used only as a fallback.

    Returns:
        The distribution version, the module's declared ``__version__`` when the
        distribution cannot be resolved, or ``"unknown"`` when neither is available.
    """
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        pass

    declared = getattr(module, "__version__", None)
    if isinstance(declared, str) and declared:
        return declared
    return "unknown"
