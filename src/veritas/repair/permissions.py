"""Repair permissions.

Veritas may autonomously improve how existing evidence is represented,
implemented, documented or validated. It must never autonomously fabricate
missing evidence in order to satisfy its own evaluator. Permissions are how
that invariant is enforced mechanically rather than by prompt wording alone.

The models themselves live in :mod:`veritas.config`, because they are
configuration; this module is their home in the repair namespace.
"""

from __future__ import annotations

from veritas.config import (
    ACTION_PERMISSION,
    RepairAgentConfig,
    RepairConfig,
    RepairPermissions,
)

__all__ = [
    "ACTION_PERMISSION",
    "RepairAgentConfig",
    "RepairConfig",
    "RepairPermissions",
]
