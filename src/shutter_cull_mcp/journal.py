"""Undo journal. Every write this server makes is reversible.

Reversibility is what turns "an agent can write to your photo library"
from reckless into merely serious. Before each sidecar is touched, its
prior state is captured: either the exact bytes that were there, or the
fact that no file existed. undo_last_apply puts every one of them back.

Two deliberate limits, both documented rather than papered over:

- The journal is in memory, so it dies with the server. A restarted server
  offers no undo for a previous session's writes. Persisting it would mean
  this server writing files outside the sidecars it was asked to write,
  which is a worse trade than losing undo across restarts.
- Undo restores what this server changed. If something else edited a
  sidecar in between, undo would clobber that edit, so the journal
  re-checks each sidecar's fingerprint first and skips the ones that moved,
  reporting them by name instead of silently overwriting.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


class JournalError(Exception):
    """Nothing to undo, or the undo could not be completed."""


def _fingerprint(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


@dataclass(frozen=True)
class SidecarBefore:
    """One sidecar's state before we touched it."""

    sidecar_path: Path
    existed: bool
    content: bytes | None
    after_fingerprint: str | None = None


@dataclass
class AppliedBatch:
    """One completed apply_picks call, in full, for reversal."""

    plan_id: str
    root: Path
    before: list[SidecarBefore]
    written: int
    errors: list[str]


def capture_before(sidecar_paths: list[Path]) -> list[SidecarBefore]:
    """Snapshot every sidecar we are about to touch. Call before writing."""
    snapshots: list[SidecarBefore] = []
    for path in sidecar_paths:
        try:
            content = path.read_bytes()
            snapshots.append(SidecarBefore(path, True, content))
        except FileNotFoundError:
            snapshots.append(SidecarBefore(path, False, None))
        except OSError as exc:
            raise JournalError(
                f"Could not read {path} before writing it, so this write "
                f"would not be reversible. Refusing to proceed: {exc}"
            ) from exc
    return snapshots


def seal(batch: AppliedBatch) -> AppliedBatch:
    """Record what each sidecar looks like after the write, for tamper checks."""
    sealed = [
        SidecarBefore(b.sidecar_path, b.existed, b.content, _fingerprint(b.sidecar_path))
        for b in batch.before
    ]
    batch.before = sealed
    return batch


def undo(batch: AppliedBatch) -> dict:
    """Reverse a batch. Skips sidecars that changed since we wrote them."""
    restored = 0
    deleted = 0
    skipped: list[str] = []
    failed: list[str] = []

    for snapshot in batch.before:
        path = snapshot.sidecar_path
        now = _fingerprint(path)
        if snapshot.after_fingerprint is not None and now != snapshot.after_fingerprint:
            # Something other than us touched it. Leave it alone and say so.
            skipped.append(path.name)
            continue
        try:
            if snapshot.existed and snapshot.content is not None:
                path.write_bytes(snapshot.content)
                restored += 1
            elif path.exists():
                path.unlink()
                deleted += 1
        except OSError as exc:
            failed.append(f"{path.name}: {exc}")

    return {
        "restored": restored,
        "deleted": deleted,
        "skipped_because_changed": skipped,
        "failed": failed,
    }


class JournalStore:
    """Holds the most recent applied batch. One deep, on purpose.

    Deeper undo history would invite an agent to walk backwards through a
    photographer's session on its own initiative. One step back is the
    accident recovery this needs; anything more is version control, which
    is not this tool's job.
    """

    def __init__(self) -> None:
        self._last: AppliedBatch | None = None

    def record(self, batch: AppliedBatch) -> None:
        self._last = seal(batch)

    def take(self) -> AppliedBatch:
        if self._last is None:
            raise JournalError(
                "Nothing to undo. This server has applied no plan since it "
                "started, and it does not carry undo history across restarts."
            )
        batch = self._last
        self._last = None
        return batch

    @property
    def has_undo(self) -> bool:
        return self._last is not None
