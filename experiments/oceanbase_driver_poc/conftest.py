"""Pytest bootstrap for the OceanBase driver POC.

Puts the POC root on ``sys.path`` so ``common``, ``mysql_mode``, and ``oracle_mode``
resolve no matter which directory pytest was invoked from. The POC deliberately sits
outside the repository's ``tests/`` tree, so it configures itself rather than relying
on the gateway's test setup.
"""

from __future__ import annotations

import sys
from pathlib import Path

POC_ROOT = Path(__file__).resolve().parent

if str(POC_ROOT) not in sys.path:
    sys.path.insert(0, str(POC_ROOT))
