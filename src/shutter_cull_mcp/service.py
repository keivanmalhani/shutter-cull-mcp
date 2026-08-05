"""The service layer: every rule lives here, the MCP tools are a thin skin.

Keeping the logic out of the tool handlers means the safety properties can
be tested directly, without an MCP client, and means the same rules would
hold if this were ever exposed over a different protocol. The handlers in
server.py do argument shuffling and formatting; they make no decisions.
"""

from __future__ import annotations

import time
from pathlib import Path

from shutter_cull_mcp.engine import CullEngine, EngineNotInstalled, ScoredFrame
from shutter_cull_mcp.journal import AppliedBatch, JournalStore, capture_before, undo
from shutter_cull_mcp.paths import Allowlist, RootAccessError
from shutter_cull_mcp.plans import (
    Plan,
    PlanError,
    PlannedWrite,
    PlanStore,
    compute_plan_id,
    verify_confirmation,
    verify_not_stale,
)

MAX_PICKS_PER_CLUSTER = 10


class CullService:
    """Holds the allowlist, the plan store, the journal, and the engine."""

    def __init__(
        self,
        allowlist: Allowlist,
        engine: CullEngine | None = None,
        plans: PlanStore | None = None,
        journal: JournalStore | None = None,
    ) -> None:
        self.allowlist = allowlist
        self.engine = engine or CullEngine()
        self.plans = plans or PlanStore()
        self.journal = journal or JournalStore()

    # ---------------------------------------------------------------- propose

    def propose(
        self,
        root: str,
        *,
        picks_per_cluster: int = 1,
        allow_download: bool = False,
    ) -> Plan:
        """Run the pipeline read-only and register a plan. Touches no disk."""
        if not 0 <= picks_per_cluster <= MAX_PICKS_PER_CLUSTER:
            raise PlanError(
                f"picks_per_cluster must be between 0 and {MAX_PICKS_PER_CLUSTER}."
            )
        resolved = self.allowlist.resolve_within(root)
        if not resolved.is_dir():
            raise RootAccessError(f"Not a folder: {root}")

        scored = self.engine.analyze(
            resolved,
            picks_per_cluster=picks_per_cluster,
            allow_download=allow_download,
        )

        writes: list[PlannedWrite] = []
        for frame in scored:
            if frame.decision not in ("pick", "reject"):
                continue  # "none" is left alone, on disk as well as in memory
            # Re-check containment per frame: a symlinked file inside an
            # allowlisted folder can still point outside it.
            if not self.allowlist.contains(frame.path.resolve()):
                continue
            try:
                stat = frame.path.stat()
            except OSError:
                continue
            writes.append(
                PlannedWrite(
                    frame_path=frame.path,
                    sidecar_path=self.engine.sidecar_for(frame.path),
                    decision=frame.decision,
                    composite=frame.composite,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                )
            )

        notes = self._notes(scored, writes)
        plan = Plan(
            plan_id=compute_plan_id(resolved, writes),
            root=resolved,
            writes=writes,
            considered=len(scored),
            created_at=time.time(),
            summary=render_plan(resolved, scored, writes),
            notes=notes,
        )
        self.plans.put(plan)
        return plan

    @staticmethod
    def _notes(scored: list[ScoredFrame], writes: list[PlannedWrite]) -> list[str]:
        notes: list[str] = []
        if not scored:
            notes.append("No frames were found under this folder.")
        elif not writes:
            notes.append(
                "The engine reached no confident call on any frame, so there "
                "is nothing to write. That is a normal outcome, not a failure."
            )
        untouched = len(scored) - len(writes)
        if untouched > 0:
            notes.append(
                f"{untouched} frames are deliberately left alone. shutter-cull "
                "marks the clear picks and the clear rejects and leaves the "
                "ambiguous middle to the photographer."
            )
        return notes

    # ------------------------------------------------------------------ apply

    def apply(self, plan_id: str, confirm: str) -> dict:
        """Apply a live, fresh, correctly confirmed plan. The only writer."""
        plan = self.plans.get(plan_id)
        verify_confirmation(plan, confirm)
        verify_not_stale(plan)

        # Fourth gate: containment, re-checked against the live filesystem
        # immediately before writing, in case a symlink moved since propose.
        for write in plan.writes:
            try:
                live = write.frame_path.resolve(strict=True)
            except OSError as exc:
                raise PlanError(
                    f"{write.frame_path.name} could not be resolved at write "
                    f"time: {exc}. Nothing was written."
                ) from exc
            if not self.allowlist.contains(live):
                raise PlanError(
                    f"{write.frame_path.name} now resolves outside the "
                    "allowlisted roots. Nothing was written."
                )

        before = capture_before([w.sidecar_path for w in plan.writes])
        outcomes = self.engine.write_sidecars(
            [(w.frame_path, w.decision) for w in plan.writes]
        )
        errors = [f"{path.name}: {err}" for path, err in outcomes if err]
        written = sum(1 for _, err in outcomes if not err)

        self.journal.record(
            AppliedBatch(
                plan_id=plan.plan_id,
                root=plan.root,
                before=before,
                written=written,
                errors=errors,
            )
        )
        # A plan is single-use. Re-applying the same id would be a silent
        # no-op at best and a surprise at worst.
        self.plans.drop(plan.plan_id)

        picks = sum(1 for w in plan.writes if w.decision == "pick")
        return {
            "applied": True,
            "plan_id": plan.plan_id,
            "root": str(plan.root),
            "sidecars_written": written,
            "picks": picks,
            "rejects": len(plan.writes) - picks,
            "errors": errors,
            "undo_available": True,
            "note": (
                "Only .xmp sidecars were written. No original image file was "
                "opened for writing. Call undo_last_apply to reverse this."
            ),
        }

    # ------------------------------------------------------------------- undo

    def undo_last(self) -> dict:
        batch = self.journal.take()
        result = undo(batch)
        result.update({
            "plan_id": batch.plan_id,
            "root": str(batch.root),
            "note": (
                "Sidecars are back to their state before that apply. Any file "
                "listed under skipped_because_changed was edited by something "
                "else after the write, so it was left as-is rather than "
                "overwritten."
            ),
        })
        return result

    # ---------------------------------------------------------------- explain

    def explain(self, plan_id: str, frame_name: str) -> dict:
        plan = self.plans.get(plan_id)
        needle = (frame_name or "").strip().lower()
        matches = [
            w for w in plan.writes
            if w.frame_path.name.lower() == needle
            or str(w.frame_path).lower().endswith(needle)
        ]
        if not matches:
            known = ", ".join(sorted(w.frame_path.name for w in plan.writes)[:10])
            raise PlanError(
                f"No frame named '{frame_name}' carries a decision in this "
                f"plan. Frames with decisions: {known or 'none'}. A frame the "
                "engine left alone will not appear here, which is itself the "
                "answer: it was neither a clear pick nor a clear reject."
            )
        write = matches[0]
        verdict = "a 5 star rating and a Green label" if write.decision == "pick" \
            else "a Red label, no star change"
        return {
            "frame": write.frame_path.name,
            "decision": write.decision,
            "composite": round(write.composite, 4),
            "would_write": str(write.sidecar_path),
            "tags": verdict,
            "explanation": (
                f"{write.frame_path.name} scored {write.composite:.2f} on the "
                "weighted composite of sharpness, eye-openness and the "
                "aesthetic heuristic, each percentile-ranked within this scan "
                f"rather than against a fixed threshold. It is marked "
                f"{write.decision}, which writes {verdict} into a sidecar "
                "beside the original."
            ),
        }

    # ------------------------------------------------------------------ plans

    def list_plans(self) -> list[dict]:
        return [
            {
                "plan_id": p.plan_id,
                "root": str(p.root),
                "writes": p.write_count,
                "considered": p.considered,
                "age_seconds": int(time.time() - p.created_at),
                "confirm_phrase": p.confirm_phrase,
            }
            for p in self.plans.list_live()
        ]

    def status(self) -> dict:
        engine_ready = True
        engine_note = "shutter-cull is importable."
        try:
            self.engine.analyze  # attribute access only, no import
            import importlib.util

            if importlib.util.find_spec("shutter_cull") is None:
                engine_ready = False
                engine_note = EngineNotInstalled.__doc__ or "shutter-cull not installed."
        except Exception:  # pragma: no cover - defensive
            engine_ready = False
            engine_note = "shutter-cull could not be checked."
        return {
            "allowlisted_roots": [str(r) for r in self.allowlist.roots],
            "live_plans": len(self.plans.list_live()),
            "undo_available": self.journal.has_undo,
            "engine_installed": engine_ready,
            "engine_note": engine_note,
            "writes": "XMP sidecars only, never the original image bytes",
        }


def render_plan(root: Path, scored: list[ScoredFrame], writes: list[PlannedWrite]) -> str:
    """The human-readable plan. This is what an agent should show, verbatim."""
    picks = [w for w in writes if w.decision == "pick"]
    rejects = [w for w in writes if w.decision == "reject"]
    untouched = len(scored) - len(writes)

    lines = [f"Cull plan for {root}", "=" * 56, ""]
    lines.append(f"{len(scored)} frames analyzed.")
    lines.append(
        f"{len(picks)} picks, {len(rejects)} rejects, {untouched} left alone."
    )
    lines.append("")
    if picks:
        lines.append("PICK  5 stars plus a Green label:")
        for w in sorted(picks, key=lambda x: -x.composite):
            lines.append(f"  {w.frame_path.name:<34} score {w.composite:.2f}")
        lines.append("")
    if rejects:
        lines.append("REJECT  Red label, stars untouched:")
        for w in sorted(rejects, key=lambda x: x.composite):
            lines.append(f"  {w.frame_path.name:<34} score {w.composite:.2f}")
        lines.append("")
    lines.append(
        "Every change is a .xmp sidecar written beside the original. An "
        "original image file is never opened for writing."
    )
    return "\n".join(lines)
