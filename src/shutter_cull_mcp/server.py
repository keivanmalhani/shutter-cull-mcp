"""MCP server: six tools over the cull engine, one of which can write.

Run it:
    shutter-cull-mcp --root ~/Pictures/2026-shoots

The tool docstrings are the agent's only instructions, so they say plainly
what each tool does, what it refuses, and what the caller must do first.
An agent that reads them should never be surprised by a refusal.
"""

from __future__ import annotations

import argparse
import sys

from mcp.server.fastmcp import FastMCP

from shutter_cull_mcp import __version__
from shutter_cull_mcp.engine import EngineError, EngineNotInstalled
from shutter_cull_mcp.journal import JournalError
from shutter_cull_mcp.paths import Allowlist, RootAccessError
from shutter_cull_mcp.plans import PlanError
from shutter_cull_mcp.service import CullService

mcp = FastMCP("shutter-cull-mcp")
_service: CullService | None = None


def service() -> CullService:
    if _service is None:  # pragma: no cover - guarded by main()
        raise RuntimeError("Server was not initialized with an allowlist.")
    return _service


def _fail(exc: Exception) -> str:
    """Errors come back as readable text, not tracebacks. Agents read these."""
    return f"Refused: {exc}"


@mcp.tool()
def propose_picks(root: str, picks_per_cluster: int = 1) -> str:
    """Analyze a shoot folder and return a cull plan. Writes nothing.

    Runs the full shutter-cull pipeline read-only: discovers frames, groups
    burst sequences, scores sharpness, eye-openness and aesthetics, and
    decides picks and rejects. Nothing is written to disk by this tool.

    Show the returned plan to the human before doing anything else. The
    plan carries a plan_id, which is the only way to apply it, and a
    confirm phrase, which apply_picks requires verbatim.

    Args:
        root: A folder inside the server's allowlist. Paths outside it are
            refused; the allowlist is fixed when the server starts and no
            tool can widen it.
        picks_per_cluster: How many top frames per burst become picks.
            Default 1. Frames in no burst are never picked.
    """
    try:
        plan = service().propose(root, picks_per_cluster=picks_per_cluster)
    except (RootAccessError, PlanError, EngineNotInstalled, EngineError) as exc:
        return _fail(exc)

    out = [plan.summary, ""]
    if plan.notes:
        out.extend(plan.notes)
        out.append("")
    out.append(f"plan_id: {plan.plan_id}")
    if plan.write_count:
        out.append(
            f'To apply, call apply_picks with that plan_id and confirm='
            f'"{plan.confirm_phrase}". Show this plan to the human first.'
        )
    else:
        out.append("There is nothing to apply.")
    return "\n".join(out)


@mcp.tool()
def apply_picks(plan_id: str, confirm: str) -> str:
    """Write the sidecars for a plan that was already proposed and shown.

    This is the only tool in this server that writes to disk, and it writes
    only .xmp sidecar files beside the originals. No original image file is
    ever opened for writing.

    It refuses unless all four hold:
      1. plan_id came from a propose_picks call on this running server.
      2. The plan has not expired.
      3. Nothing under the plan changed since it was proposed. If any frame
         was edited or moved, the plan is stale and nothing is written.
      4. confirm is exactly the phrase the plan printed, for example
         "apply 12 changes". A boolean will not do: quoting the count back
         is how this server knows the plan was actually read.

    There is no folder argument on purpose. A plan can only be applied to
    the folder it was proposed against.

    Args:
        plan_id: The id from propose_picks.
        confirm: The plan's exact confirm phrase.
    """
    try:
        result = service().apply(plan_id, confirm)
    except (PlanError, RootAccessError, EngineNotInstalled, EngineError) as exc:
        return _fail(exc)

    lines = [
        f"Applied plan {result['plan_id']} under {result['root']}.",
        f"{result['sidecars_written']} sidecars written: "
        f"{result['picks']} picks, {result['rejects']} rejects.",
        result["note"],
    ]
    if result["errors"]:
        lines.append("")
        lines.append("Errors on these frames, the rest were written:")
        lines.extend(f"  {e}" for e in result["errors"])
    return "\n".join(lines)


@mcp.tool()
def undo_last_apply() -> str:
    """Reverse the most recent apply_picks call.

    Every sidecar this server writes is snapshotted first, so undo puts
    each one back exactly as it was, deleting the ones that did not exist
    before. A sidecar that something else edited after the write is left
    alone and reported rather than overwritten.

    Undo is one step deep and does not survive a server restart.
    """
    try:
        result = service().undo_last()
    except JournalError as exc:
        return _fail(exc)

    lines = [
        f"Undid plan {result['plan_id']} under {result['root']}.",
        f"{result['restored']} sidecars restored, {result['deleted']} removed.",
    ]
    if result["skipped_because_changed"]:
        lines.append(
            "Left alone because something else edited them after the write: "
            + ", ".join(result["skipped_because_changed"])
        )
    if result["failed"]:
        lines.append("Failed to undo: " + "; ".join(result["failed"]))
    lines.append(result["note"])
    return "\n".join(lines)


@mcp.tool()
def explain_pick(plan_id: str, frame: str) -> str:
    """Explain why one frame in a plan got the decision it got.

    Use this when a human questions a call. A frame the engine left alone
    has no entry here, and that absence is the answer: it was neither a
    clear pick nor a clear reject.

    Args:
        plan_id: The plan the frame belongs to.
        frame: The file name, for example DSC01234.ARW.
    """
    try:
        result = service().explain(plan_id, frame)
    except PlanError as exc:
        return _fail(exc)
    return (
        f"{result['frame']}: {result['decision']} at {result['composite']}\n"
        f"{result['explanation']}\n"
        f"Sidecar that would carry it: {result['would_write']}"
    )


@mcp.tool()
def list_plans() -> str:
    """List the plans this server currently holds, newest first.

    Plans live in memory, expire after thirty minutes, and are dropped once
    applied. If a plan is missing from this list it cannot be applied.
    """
    plans = service().list_plans()
    if not plans:
        return "No live plans. Call propose_picks to create one."
    lines = ["Live plans, newest first:", ""]
    for p in plans:
        lines.append(
            f"  {p['plan_id']}  {p['writes']} writes of {p['considered']} "
            f"frames, {p['age_seconds']}s old, {p['root']}"
        )
    return "\n".join(lines)


@mcp.tool()
def server_status() -> str:
    """Report what this server may touch and what state it holds.

    Worth calling before anything else: it shows the allowlisted roots,
    which are the hard boundary on everything this server can do.
    """
    s = service().status()
    lines = [
        f"shutter-cull-mcp {__version__}",
        "Allowlisted roots, the hard boundary:",
    ]
    lines.extend(f"  {r}" for r in s["allowlisted_roots"])
    lines.append(f"Writes: {s['writes']}")
    lines.append(f"Live plans: {s['live_plans']}")
    lines.append(f"Undo available: {s['undo_available']}")
    lines.append(f"Engine installed: {s['engine_installed']}")
    if not s["engine_installed"]:
        lines.append(f"  {s['engine_note']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shutter-cull-mcp",
        description=(
            "MCP server for shutter-cull. Lets an agent propose and apply "
            "photo culls, behind a fixed root allowlist, plan tokens, "
            "staleness checks, and one-step undo."
        ),
    )
    parser.add_argument(
        "--root",
        action="append",
        required=True,
        metavar="DIR",
        help=(
            "A folder this server may touch. Repeatable. Required: the "
            "server refuses to start without an explicit allowlist."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    global _service
    args = build_parser().parse_args(argv)
    try:
        allowlist = Allowlist(args.root)
    except RootAccessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _service = CullService(allowlist)
    print(
        "shutter-cull-mcp serving. Roots: "
        + ", ".join(str(r) for r in allowlist.roots),
        file=sys.stderr,
    )
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
