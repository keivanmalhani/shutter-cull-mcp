from __future__ import annotations

import pytest

from shutter_cull_mcp.paths import Allowlist
from shutter_cull_mcp.plans import PlanError, PlanStore
from shutter_cull_mcp.service import CullService


def test_plan_summary_reads_like_something_you_can_show_a_human(service, shoot):
    plan = service.propose(str(shoot))
    text = plan.summary
    assert "6 frames analyzed" in text
    assert "2 picks, 2 rejects, 2 left alone" in text
    assert "DSC0001.ARW" in text
    assert "sidecar" in text.lower()
    assert "never" in text.lower()  # the no-originals promise is stated


def test_notes_explain_the_frames_left_alone(service, shoot):
    plan = service.propose(str(shoot))
    assert any("left alone" in n for n in plan.notes)


def test_an_empty_folder_produces_an_empty_plan_not_an_error(tmp_path, engine):
    empty = tmp_path / "empty"
    empty.mkdir()
    svc = CullService(Allowlist([str(empty)]), engine=engine)
    plan = svc.propose(str(empty))
    assert plan.write_count == 0
    assert any("No frames" in n for n in plan.notes)


def test_a_shoot_with_no_confident_calls_says_so(shoot, engine):
    engine.decisions = {}  # everything is "none"
    svc = CullService(Allowlist([str(shoot)]), engine=engine)
    plan = svc.propose(str(shoot))
    assert plan.write_count == 0
    assert any("no confident call" in n for n in plan.notes)


def test_explain_answers_for_a_decided_frame(service, shoot):
    plan = service.propose(str(shoot))
    out = service.explain(plan.plan_id, "DSC0001.ARW")
    assert out["decision"] == "pick"
    assert out["composite"] == pytest.approx(0.93)
    assert "percentile" in out["explanation"]
    assert out["would_write"].endswith("DSC0001.xmp")


def test_explain_treats_absence_as_the_answer(service, shoot):
    plan = service.propose(str(shoot))
    with pytest.raises(PlanError) as exc:
        service.explain(plan.plan_id, "DSC0003.ARW")  # left alone
    assert "neither a clear pick nor a clear reject" in str(exc.value)


def test_list_plans_and_expiry(shoot, engine):
    svc = CullService(Allowlist([str(shoot)]), engine=engine)
    svc.propose(str(shoot))
    assert len(svc.list_plans()) == 1
    entry = svc.list_plans()[0]
    assert entry["writes"] == 4
    assert entry["confirm_phrase"] == "apply 4 changes"

    svc2 = CullService(Allowlist([str(shoot)]), engine=engine,
                       plans=PlanStore(ttl_seconds=0.0))
    svc2.propose(str(shoot))
    assert svc2.list_plans() == []


def test_plan_store_is_capped(shoot, engine, tmp_path):
    store = PlanStore(max_plans=2)
    svc = CullService(Allowlist([str(shoot)]), engine=engine, plans=store)
    # Three distinct worlds, so three distinct plan ids.
    for i in range(3):
        (shoot / "DSC0001.ARW").write_bytes(b"variant" + bytes([i]))
        svc.propose(str(shoot))
    assert len(svc.list_plans()) <= 2


def test_status_reports_the_boundary(service, shoot):
    s = service.status()
    assert s["allowlisted_roots"] == [str(shoot.resolve())]
    assert "sidecars only" in s["writes"]
    assert s["undo_available"] is False


def test_status_after_apply_offers_undo(service, shoot):
    plan = service.propose(str(shoot))
    service.apply(plan.plan_id, plan.confirm_phrase)
    assert service.status()["undo_available"] is True


def test_a_symlinked_frame_pointing_outside_is_dropped_from_the_plan(
    tmp_path, engine
):
    shoot = tmp_path / "shoot"
    shoot.mkdir()
    (shoot / "DSC0001.ARW").write_bytes(b"real frame")
    outside = tmp_path / "private"
    outside.mkdir()
    target = outside / "client.ARW"
    target.write_bytes(b"someone else's work")
    (shoot / "DSC0009.ARW").symlink_to(target)

    engine.decisions = {"DSC0001.ARW": "pick", "DSC0009.ARW": "pick"}
    svc = CullService(Allowlist([str(shoot)]), engine=engine)
    plan = svc.propose(str(shoot))

    names = {w.frame_path.name for w in plan.writes}
    assert names == {"DSC0001.ARW"}  # the escaping link never enters the plan
