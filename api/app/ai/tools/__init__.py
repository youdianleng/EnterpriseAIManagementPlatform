"""The tool whitelist of DESIGN §6.2, and the graph's access to it.

Empty until tickets 39-40 — see `registry.py` for why that is a seam rather than a hole, and
for the structural half of constraint B (`ai/**` must reach no write repository, and no
write tool can even be described by `ToolKind`).
"""

from app.ai.tools.registry import Tool, ToolKind, registered

__all__ = ["Tool", "ToolKind", "registered"]
