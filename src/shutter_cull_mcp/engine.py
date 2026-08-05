"""Adapter over the shutter-cull engine. The only place that imports it.

Two reasons this boundary exists rather than calling shutter_cull directly
from the server:

- The engine is heavy: OpenCV, ONNX Runtime, rawpy. The safety envelope
  this server is actually made of, the allowlist, the plan tokens, the
  journal, should be testable without any of that installed, and it is:
  the whole test suite runs against a fake engine.
- shutter-cull is a separate package with its own release cadence. Pinning
  the coupling to one small adapter means a change over there is a change
  to one file here, not a change scattered through the tool handlers.

The import is lazy so that a server started without the engine installed
still answers, and fails with an instruction rather than a traceback the
first time someone asks it to actually analyze something.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class EngineNotInstalled(Exception):
    """shutter-cull is not importable in this environment."""


class EngineError(Exception):
    """The engine ran and failed."""


@dataclass(frozen=True)
class ScoredFrame:
    """One frame's decision, flattened out of the engine's own result type."""

    path: Path
    decision: str  # "pick", "reject", or "none"
    composite: float
    blur_percentile: float
    eyes_percentile: float
    aesthetic_percentile: float


INSTALL_HINT = (
    "shutter-cull is not installed in this environment. This server is the "
    "agent interface for that engine, not a copy of it. Install it with:\n"
    "  pip install git+https://github.com/keivanmalhani/shutter-cull.git\n"
    "and make sure exiftool is on PATH for writes."
)


class CullEngine:
    """The real engine. Imports shutter_cull on first use."""

    def analyze(
        self,
        root: Path,
        *,
        picks_per_cluster: int = 1,
        allow_download: bool = False,
        weights: dict[str, float] | None = None,
    ) -> list[ScoredFrame]:
        try:
            from shutter_cull.cluster import cluster_bursts
            from shutter_cull.ingest import discover_frames
            from shutter_cull.scoring.composite import (
                DEFAULT_WEIGHTS,
                composite_scan,
                score_frames,
            )
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise EngineNotInstalled(INSTALL_HINT) from exc

        try:
            frames = discover_frames(root)["frames"]
            if not frames:
                return []
            clusters = cluster_bursts(frames)
            scored = score_frames(frames, allow_download=allow_download)
            results = composite_scan(
                scored,
                clusters,
                weights=weights or dict(DEFAULT_WEIGHTS),
                picks_per_cluster=picks_per_cluster,
            )
        except Exception as exc:  # pragma: no cover - engine internals
            raise EngineError(f"shutter-cull failed on {root}: {exc}") from exc

        return [
            ScoredFrame(
                path=Path(r.frame.path),
                decision=r.decision,
                composite=float(r.composite),
                blur_percentile=float(r.blur_percentile),
                eyes_percentile=float(r.eyes_percentile),
                aesthetic_percentile=float(r.aesthetic_percentile),
            )
            for r in results
        ]

    def write_sidecars(self, decisions: list[tuple[Path, str]]) -> list[tuple[Path, str | None]]:
        """Write one XMP sidecar per decision through shutter-cull's writeback.

        Returns (sidecar_path, error_or_None) per action. Writes go through
        the engine's exiftool wrapper rather than a hand-rolled XMP writer,
        which is the same reasoning shutter-cull used to pick exiftool in
        the first place.
        """
        try:
            from shutter_cull.writeback import (
                PICK_LABEL,
                PICK_RATING,
                REJECT_LABEL,
                WriteAction,
                sidecar_path,
                write_all,
            )
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise EngineNotInstalled(INSTALL_HINT) from exc

        actions = []
        for frame_path, decision in decisions:
            if decision == "pick":
                tags = {"Rating": PICK_RATING, "Label": PICK_LABEL}
            elif decision == "reject":
                tags = {"Label": REJECT_LABEL}
            else:
                continue
            actions.append(
                WriteAction(
                    frame_path=frame_path,
                    sidecar_path=sidecar_path(frame_path),
                    decision=decision,
                    tags=tags,
                )
            )
        try:
            outcomes = write_all(actions)
        except Exception as exc:
            raise EngineError(f"Sidecar write failed: {exc}") from exc
        return [(a.sidecar_path, err) for a, err in outcomes]

    def sidecar_for(self, frame_path: Path) -> Path:
        """Where a frame's sidecar goes. Pure path math, no engine import needed."""
        return frame_path.with_suffix(".xmp")
