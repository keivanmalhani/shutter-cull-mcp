"""shutter-cull-mcp: the write-capable agent interface for shutter-cull.

A separate server from shutter-mcp on purpose. shutter-mcp's whole security
story is that it cannot write, and that guarantee should never have to be
softened to add a feature. This server can write, so it carries its own
envelope: a fixed root allowlist, content-addressed plan tokens, staleness
re-verification, an exact-phrase confirmation, and one-step undo.

Spec: _hq/specs/shutter-cull-spec.md, "Agent interface, phase 3".
"""

__version__ = "0.1.0"
