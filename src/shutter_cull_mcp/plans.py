"""Plan store: content-addressed proposals that gate every write.

The problem this solves
-----------------------
An agent that can write to a photo library is a different risk class from
one that can read it. The agent might be prompt-injected by a filename, it
might hallucinate a path, or it might apply a plan the human never saw. A
boolean confirm argument does not help: a model that will call a tool will
also pass confirm=true.

So writes here are not a tool an agent calls. They are a plan an agent
must first produce, show, and then quote back.

1. propose_picks runs the pipeline read-only and returns a plan plus a
   plan_id. The plan_id is a sha256 over the root, and over every frame's
   path, decision, size and mtime. It is content-addressed: it cannot be
   guessed, and it changes if anything about the plan or the underlying
   files changes.

2. apply_picks accepts only a plan_id this process issued. There is no
   path argument on apply. An agent cannot apply to a folder it never
   proposed against, because there is nowhere to put the folder name.

3. Before writing, the plan is re-verified against the filesystem. If any
   frame was edited, moved, or replaced since the proposal, the plan is
   stale and the write is refused with the specific files named. This
   closes the window between "the human approved this" and "the write
   happened".

4. The confirm argument is not a boolean. It must be the exact string
   "apply N changes" with N matching the plan's write count, so a caller
   has to have read the plan to construct it. Reflexive confirmation is
   a real failure mode with agents, and a literal that encodes the blast
   radius is cheap insurance against it.

Plans live in memory only, expire, and are capped. A restarted server
honors no old plans, which is the correct default for something that
writes to a stranger's photographs.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TTL_SECONDS = 30 * 60
MAX_PLANS = 32


class PlanError(Exception):
    """A plan is unknown, expired, stale, or its confirmation did not match."""


@dataclass(frozen=True)
class PlannedWrite:
    """One frame's decision plus the fingerprint it had when proposed."""

    frame_path: Path
    sidecar_path: Path
    decision: str  # "pick" or "reject"
    composite: float
    size: int
    mtime: float

    def fingerprint(self) -> str:
        return f"{self.frame_path}|{self.decision}|{self.size}|{self.mtime:.6f}"


@dataclass
class Plan:
    """A proposal: what would be written, and the world it was computed against."""

    plan_id: str
    root: Path
    writes: list[PlannedWrite]
    considered: int
    created_at: float
    summary: str
    notes: list[str] = field(default_factory=list)

    @property
    def write_count(self) -> int:
        return len(self.writes)

    @property
    def confirm_phrase(self) -> str:
        """The exact string apply_picks demands. Encodes the blast radius."""
        return f"apply {self.write_count} changes"

    def is_expired(self, now: float, ttl: float) -> bool:
        return (now - self.created_at) > ttl


def compute_plan_id(root: Path, writes: list[PlannedWrite]) -> str:
    """Content-address a plan over its root and every write's fingerprint.

    Sorted so two runs that reach the same conclusion about the same files
    produce the same id, and unsorted differences in walk order never make
    an identical plan look new.
    """
    digest = hashlib.sha256()
    digest.update(str(root).encode("utf-8"))
    for write in sorted(writes, key=lambda w: str(w.frame_path)):
        digest.update(b"\0")
        digest.update(write.fingerprint().encode("utf-8"))
    return digest.hexdigest()[:32]


def current_fingerprint(write: PlannedWrite) -> str | None:
    """Re-read a frame's size and mtime. None if it is gone."""
    try:
        stat = write.frame_path.stat()
    except OSError:
        return None
    return f"{write.frame_path}|{write.decision}|{stat.st_size}|{stat.st_mtime:.6f}"


class PlanStore:
    """In-memory, expiring, capped. Plans never touch disk by design."""

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS,
                 max_plans: int = MAX_PLANS) -> None:
        self._plans: dict[str, Plan] = {}
        self._ttl = ttl_seconds
        self._max = max_plans

    def put(self, plan: Plan) -> None:
        self._plans[plan.plan_id] = plan
        # Never evict the plan being stored, even under a degenerate TTL:
        # a caller that just proposed should always get a plan_id back that
        # exists, and then be told plainly that it expired.
        self._evict(protect=plan.plan_id)

    def get(self, plan_id: str, now: float | None = None) -> Plan:
        """Fetch a live plan. Raises PlanError for unknown or expired ids."""
        now = time.time() if now is None else now
        plan = self._plans.get((plan_id or "").strip())
        if plan is None:
            raise PlanError(
                "No such plan. A plan_id must come from a propose_picks call "
                "made against this running server. Ids cannot be constructed, "
                "reused across restarts, or borrowed from another session."
            )
        if plan.is_expired(now, self._ttl):
            self._plans.pop(plan.plan_id, None)
            raise PlanError(
                f"Plan {plan_id} has expired after "
                f"{int(self._ttl / 60)} minutes. Run propose_picks again and "
                "show the human the fresh plan before applying it."
            )
        return plan

    def list_live(self, now: float | None = None) -> list[Plan]:
        now = time.time() if now is None else now
        live = [p for p in self._plans.values() if not p.is_expired(now, self._ttl)]
        return sorted(live, key=lambda p: p.created_at, reverse=True)

    def drop(self, plan_id: str) -> None:
        self._plans.pop(plan_id, None)

    def _evict(self, protect: str | None = None) -> None:
        now = time.time()
        for plan_id, plan in list(self._plans.items()):
            if plan_id != protect and plan.is_expired(now, self._ttl):
                self._plans.pop(plan_id, None)
        if len(self._plans) > self._max:
            oldest = sorted(self._plans.values(), key=lambda p: p.created_at)
            for plan in oldest[: len(self._plans) - self._max]:
                if plan.plan_id != protect:
                    self._plans.pop(plan.plan_id, None)


def verify_confirmation(plan: Plan, confirm: str) -> None:
    """The confirm string must name the plan's own blast radius, exactly."""
    supplied = (confirm or "").strip().lower()
    expected = plan.confirm_phrase.lower()
    if supplied != expected:
        raise PlanError(
            f"Confirmation did not match. To apply this plan the confirm "
            f'argument must be exactly "{plan.confirm_phrase}". This is '
            "deliberately not a boolean: quoting the change count back is "
            "evidence the plan was actually read."
        )


def verify_not_stale(plan: Plan) -> None:
    """Refuse the write if the library moved under the plan."""
    changed: list[str] = []
    missing: list[str] = []
    for write in plan.writes:
        current = current_fingerprint(write)
        if current is None:
            missing.append(write.frame_path.name)
        elif current != write.fingerprint():
            changed.append(write.frame_path.name)
    if missing or changed:
        parts = []
        if missing:
            parts.append("gone: " + ", ".join(sorted(missing)[:8]))
        if changed:
            parts.append("modified: " + ", ".join(sorted(changed)[:8]))
        raise PlanError(
            "This plan is stale: the library changed after it was proposed ("
            + "; ".join(parts)
            + "). Nothing was written. Run propose_picks again so the human "
            "approves what is actually on disk now."
        )
