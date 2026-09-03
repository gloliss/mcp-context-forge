# -*- coding: utf-8 -*-
"""Merge the fix-admin-metrics migration head into the main line.

Revision ID: 3c9d5e7a1b2f
Revises: 8f7a6b5c4d3e, t2b3c4d5e6f7
Create Date: 2026-09-03 00:00:00.000000

Empty merge revision joining the post-upstream head (8f7a6b5c4d3e) with the
portable-tool-definitions head (t2b3c4d5e6f7) for a single-head graph.
"""

from typing import Sequence, Union

revision: str = "3c9d5e7a1b2f"
down_revision: Union[str, Sequence[str], None] = ("8f7a6b5c4d3e", "t2b3c4d5e6f7")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op merge upgrade."""
    pass


def downgrade() -> None:
    """No-op merge downgrade."""
    pass
