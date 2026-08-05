"""Adversarial tests for the write gate.

Each of these is a way an agent could plausibly get a write it should not
have: forging a token, replaying one, confirming reflexively, applying to
a folder it never proposed against, or racing the human's approval. They
are written as attacks, not as features, because that is how the gate was
designed.
"""

from __future__ import annotations

import time

import pytest

from shutter_cull_mcp.paths import RootAccessError
from shutter_cull_mcp.plans import PlanError, PlanStore


def sidecars(shoot):
    return sorted(p.name for p in shoot.glob("*.xmp"))


# ---------------------------------------------------------------- proposal


def test_propose_writes_absolutely_nothing(service, shoot, engine):
    before = sorted(p.name for p in shoot.iterdir())
    plan = service.propose(str(shoot))
    assert plan.write_count == 4  # 2 picks, 2 rejects, 2 left alone
    assert sorted(p.name for p in shoot.iterdir()) == before
    assert engine.write_calls == []


def test_plan_covers_only_confident_calls(service, shoot):
    plan = service.propose(str(shoot))
    names = {w.frame_path.name: w.decision for w in plan.writes}
    assert names == {
        "DSC0001.ARW": "pick", "DSC0004.ARW": "pick",
        "DSC0002.ARW": "reject", "DSC0006.JPG": "reject",
    }
    assert "DSC0003.ARW" not in names  # left alone stays off disk too


def test_plan_id_is_content_addressed(service, shoot):
    first = service.propose(str(shoot))
    second = service.propose(str(shoot))
    assert first.plan_id == second.plan_id  # same world, same plan

    (shoot / "DSC0001.ARW").write_bytes(b"edited in lightroom")
    third = service.propose(str(shoot))
    assert third.plan_id != first.plan_id  # world changed, plan changed


def test_propose_refuses_folders_outside_the_allowlist(service, tmp_path):
    outside = tmp_path / "someone_elses_shoot"
    outside.mkdir()
    with pytest.raises(RootAccessError):
        service.propose(str(outside))


def test_propose_rejects_absurd_picks_per_cluster(service, shoot):
    with pytest.raises(PlanError):
        service.propose(str(shoot), picks_per_cluster=99)
    with pytest.raises(PlanError):
        service.propose(str(shoot), picks_per_cluster=-1)


# ------------------------------------------------------------------- apply


def test_the_happy_path_writes_sidecars(service, shoot):
    plan = service.propose(str(shoot))
    result = service.apply(plan.plan_id, plan.confirm_phrase)
    assert result["sidecars_written"] == 4
    assert result["picks"] == 2 and result["rejects"] == 2
    assert sidecars(shoot) == [
        "DSC0001.xmp", "DSC0002.xmp", "DSC0004.xmp", "DSC0006.xmp"
    ]


def test_a_forged_plan_id_is_refused(service, shoot):
    service.propose(str(shoot))
    with pytest.raises(PlanError) as exc:
        service.apply("a" * 32, "apply 4 changes")
    assert "No such plan" in str(exc.value)
    assert sidecars(shoot) == []


def test_a_reflexive_confirmation_is_refused(service, shoot):
    plan = service.propose(str(shoot))
    for bad in ("true", "yes", "confirm", "apply", "apply 99 changes", ""):
        with pytest.raises(PlanError) as exc:
            service.apply(plan.plan_id, bad)
        assert "Confirmation did not match" in str(exc.value)
    assert sidecars(shoot) == []


def test_the_confirm_phrase_names_the_blast_radius(service, shoot):
    plan = service.propose(str(shoot))
    assert plan.confirm_phrase == "apply 4 changes"


def test_confirmation_is_case_insensitive_but_not_shape_insensitive(service, shoot):
    plan = service.propose(str(shoot))
    service.apply(plan.plan_id, "APPLY 4 CHANGES")  # a human retyping is fine
    assert len(sidecars(shoot)) == 4


def test_a_plan_cannot_be_applied_twice(service, shoot):
    plan = service.propose(str(shoot))
    service.apply(plan.plan_id, plan.confirm_phrase)
    with pytest.raises(PlanError):
        service.apply(plan.plan_id, plan.confirm_phrase)


def test_a_plan_goes_stale_when_a_frame_is_edited(service, shoot):
    plan = service.propose(str(shoot))
    time.sleep(0.01)
    (shoot / "DSC0001.ARW").write_bytes(b"the photographer edited this after approving")
    with pytest.raises(PlanError) as exc:
        service.apply(plan.plan_id, plan.confirm_phrase)
    assert "stale" in str(exc.value)
    assert "DSC0001.ARW" in str(exc.value)
    assert sidecars(shoot) == []  # nothing partial


def test_a_plan_goes_stale_when_a_frame_disappears(service, shoot):
    plan = service.propose(str(shoot))
    (shoot / "DSC0004.ARW").unlink()
    with pytest.raises(PlanError) as exc:
        service.apply(plan.plan_id, plan.confirm_phrase)
    assert "gone" in str(exc.value)
    assert sidecars(shoot) == []


def test_an_expired_plan_is_refused(shoot, engine):
    from shutter_cull_mcp.paths import Allowlist
    from shutter_cull_mcp.service import CullService

    svc = CullService(Allowlist([str(shoot)]), engine=engine,
                      plans=PlanStore(ttl_seconds=0.0))
    plan = svc.propose(str(shoot))
    time.sleep(0.01)
    with pytest.raises(PlanError) as exc:
        svc.apply(plan.plan_id, plan.confirm_phrase)
    assert "expired" in str(exc.value)
    assert sidecars(shoot) == []


def test_apply_takes_no_folder_argument(service):
    # Structural, not behavioral: an agent cannot aim apply at a folder,
    # because there is no parameter to aim it with.
    import inspect

    params = set(inspect.signature(service.apply).parameters)
    assert params == {"plan_id", "confirm"}


def test_write_errors_are_reported_not_swallowed(service, shoot, engine):
    engine.write_errors = {"DSC0002.ARW": "exiftool exited 1"}
    plan = service.propose(str(shoot))
    result = service.apply(plan.plan_id, plan.confirm_phrase)
    assert result["sidecars_written"] == 3
    assert any("DSC0002" in e for e in result["errors"])


# -------------------------------------------------------------------- undo


def test_undo_removes_sidecars_that_did_not_exist_before(service, shoot):
    plan = service.propose(str(shoot))
    service.apply(plan.plan_id, plan.confirm_phrase)
    assert len(sidecars(shoot)) == 4

    result = service.undo_last()
    assert result["deleted"] == 4
    assert sidecars(shoot) == []


def test_undo_restores_a_sidecar_that_existed_before(service, shoot):
    prior = shoot / "DSC0001.xmp"
    prior.write_bytes(b"<x:xmpmeta>the photographer's own rating</x:xmpmeta>")

    plan = service.propose(str(shoot))
    service.apply(plan.plan_id, plan.confirm_phrase)
    assert prior.read_bytes() != b"<x:xmpmeta>the photographer's own rating</x:xmpmeta>"

    service.undo_last()
    assert prior.read_bytes() == b"<x:xmpmeta>the photographer's own rating</x:xmpmeta>"


def test_undo_leaves_alone_what_someone_else_changed(service, shoot):
    plan = service.propose(str(shoot))
    service.apply(plan.plan_id, plan.confirm_phrase)

    # Lightroom, or the photographer, edits one sidecar after the fact.
    (shoot / "DSC0001.xmp").write_bytes(b"<x:xmpmeta>edited in lightroom</x:xmpmeta>")

    result = service.undo_last()
    assert result["skipped_because_changed"] == ["DSC0001.xmp"]
    assert (shoot / "DSC0001.xmp").read_bytes() == b"<x:xmpmeta>edited in lightroom</x:xmpmeta>"
    assert result["deleted"] == 3


def test_undo_is_one_step_deep(service, shoot):
    plan = service.propose(str(shoot))
    service.apply(plan.plan_id, plan.confirm_phrase)
    service.undo_last()
    from shutter_cull_mcp.journal import JournalError

    with pytest.raises(JournalError):
        service.undo_last()


def test_undo_before_any_apply_says_so(service):
    from shutter_cull_mcp.journal import JournalError

    with pytest.raises(JournalError) as exc:
        service.undo_last()
    assert "Nothing to undo" in str(exc.value)
