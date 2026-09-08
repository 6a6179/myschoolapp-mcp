"""Tool-facing exceptions.

The MCP Python SDK (2.x) only forwards the message of a ``ToolError`` to the
client; any other exception is reported as an opaque
"Error executing tool <name>". These subclasses keep the ValueError /
RuntimeError semantics used throughout the code (and the tests) while
making sure the model actually sees *why* a call was refused.
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError


class UserError(ToolError, ValueError):
    """Bad arguments or configuration — the caller can fix this."""


class SessionError(ToolError, RuntimeError):
    """Upstream/session failure the tool could not recover from."""
