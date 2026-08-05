"""Fixtures. The whole suite runs against a fake engine on purpose.

What this server is actually made of is the safety envelope, not the
computer vision. Testing the envelope against a fake engine means the
suite is fast, deterministic, needs neither OpenCV nor exiftool, and can
stage failures (a write error, a frame that vanishes mid-apply) that would
be awkward to provoke through the real pipeline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shutter_cull_mcp.engine import ScoredFrame
from shutter_cull_mcp.paths import Allowlist
from shutter_cull_mcp.service import CullService


class FakeEngine:
    """Stands in for shutter-cull. Records calls, can be told to misbehave."""

    def __init__(self) -> None:
        self.decisions: dict[str, str] = {}
        self.scores: dict[str, float] = {}
        self.analyze_calls: list[Path] = []
        self.write_calls: list[list[tuple[Path, str]]] = []
        self.write_errors: dict[str, str] = {}
        self.raise_on_write: Exception | None = None
        self.sidecar_body = b"<x:xmpmeta>written by fake engine</x:xmpmeta>"

    def analyze(self, root: Path, *, picks_per_cluster: int = 1,
                allow_download: bool = False, weights=None) -> list[ScoredFrame]:
        self.analyze_calls.append(root)
        out = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in (".arw", ".jpg", ".dng"):
                continue
            decision = self.decisions.get(path.name, "none")
            out.append(
                ScoredFrame(
                    path=path,
                    decision=decision,
                    composite=self.scores.get(path.name, 0.5),
                    blur_percentile=0.5,
                    eyes_percentile=0.5,
                    aesthetic_percentile=0.5,
                )
            )
        return out

    def write_sidecars(self, decisions: list[tuple[Path, str]]):
        if self.raise_on_write is not None:
            raise self.raise_on_write
        self.write_calls.append(list(decisions))
        outcomes = []
        for frame_path, _decision in decisions:
            sidecar = self.sidecar_for(frame_path)
            err = self.write_errors.get(frame_path.name)
            if err is None:
                sidecar.write_bytes(self.sidecar_body)
            outcomes.append((sidecar, err))
        return outcomes

    def sidecar_for(self, frame_path: Path) -> Path:
        return frame_path.with_suffix(".xmp")


def make_frames(root: Path, names: list[str]) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, name in enumerate(names):
        p = root / name
        p.write_bytes(b"fake raw bytes " + bytes([i % 256]) * 64)
        paths.append(p)
    return paths


@pytest.fixture()
def shoot(tmp_path) -> Path:
    """A shoot folder with six frames."""
    root = tmp_path / "shoot"
    make_frames(root, [
        "DSC0001.ARW", "DSC0002.ARW", "DSC0003.ARW",
        "DSC0004.ARW", "DSC0005.JPG", "DSC0006.JPG",
    ])
    return root


@pytest.fixture()
def engine() -> FakeEngine:
    fake = FakeEngine()
    fake.decisions = {
        "DSC0001.ARW": "pick",
        "DSC0002.ARW": "reject",
        "DSC0003.ARW": "none",
        "DSC0004.ARW": "pick",
        "DSC0005.JPG": "none",
        "DSC0006.JPG": "reject",
    }
    fake.scores = {
        "DSC0001.ARW": 0.93, "DSC0002.ARW": 0.06, "DSC0003.ARW": 0.51,
        "DSC0004.ARW": 0.88, "DSC0005.JPG": 0.44, "DSC0006.JPG": 0.09,
    }
    return fake


@pytest.fixture()
def service(shoot, engine) -> CullService:
    return CullService(Allowlist([str(shoot)]), engine=engine)
