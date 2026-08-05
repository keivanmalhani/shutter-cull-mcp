"""Root allowlist. The one thing an agent can never talk its way past.

The server is launched with one or more --root arguments. Every path an
agent supplies is resolved through symlinks and then checked for
containment inside one of those roots. There is no tool that adds a root,
and no argument that widens one: the allowlist is fixed at process start
by the human who launched the server.

This is the same posture as shutter-mcp, kept deliberately identical so
the two servers are auditable against each other, with one addition that
read-only shutter-mcp never needed: containment is re-checked immediately
before every write, not only at proposal time, because a symlink can be
swapped between the two.
"""

from __future__ import annotations

from pathlib import Path


class RootAccessError(Exception):
    """A path is outside every allowlisted root, or does not exist."""


class Allowlist:
    """An immutable set of resolved roots plus containment checks."""

    def __init__(self, raw_roots: list[str]) -> None:
        if not raw_roots:
            raise RootAccessError(
                "At least one --root is required. This server refuses to run "
                "without an explicit allowlist."
            )
        roots: list[Path] = []
        for raw in raw_roots:
            if not raw or not str(raw).strip():
                raise RootAccessError("Root path must not be empty.")
            candidate = Path(raw).expanduser()
            try:
                resolved = candidate.resolve(strict=True)
            except FileNotFoundError as exc:
                raise RootAccessError(f"Root does not exist: {raw}") from exc
            except OSError as exc:
                raise RootAccessError(f"Could not resolve root '{raw}': {exc}") from exc
            if not resolved.is_dir():
                raise RootAccessError(f"Root is not a directory: {raw}")
            roots.append(resolved)
        self._roots = tuple(roots)

    @property
    def roots(self) -> tuple[Path, ...]:
        return self._roots

    def resolve_within(self, raw_path: str) -> Path:
        """Resolve raw_path and return it only if it sits inside a root.

        Resolution happens before the containment test, so a symlink
        pointing outside the allowlist is rejected on where it lands, not
        on where it sits.
        """
        if not raw_path or not str(raw_path).strip():
            raise RootAccessError("Path must not be empty.")
        candidate = Path(raw_path).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise RootAccessError(f"Path does not exist: {raw_path}") from exc
        except OSError as exc:
            raise RootAccessError(f"Could not resolve path '{raw_path}': {exc}") from exc

        for root in self._roots:
            if resolved == root or resolved.is_relative_to(root):
                return resolved
        allowed = ", ".join(str(r) for r in self._roots)
        raise RootAccessError(
            f"Path is outside every allowlisted root: {raw_path}. "
            f"This server may only touch: {allowed}"
        )

    def contains(self, resolved_path: Path) -> bool:
        """Containment test for an already-resolved path. No filesystem access."""
        return any(
            resolved_path == root or resolved_path.is_relative_to(root)
            for root in self._roots
        )
