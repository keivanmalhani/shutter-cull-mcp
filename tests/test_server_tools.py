"""The MCP tool skin: right wiring, and refusals that read as instructions.

An agent only ever sees these strings, so a refusal that does not say what
to do instead is a bug in the interface, not just unfriendly copy.
"""

from __future__ import annotations

import pytest

from shutter_cull_mcp import server
from shutter_cull_mcp.paths import Allowlist
from shutter_cull_mcp.service import CullService


@pytest.fixture(autouse=True)
def wire(shoot, engine, monkeypatch):
    svc = CullService(Allowlist([str(shoot)]), engine=engine)
    monkeypatch.setattr(server, "_service", svc)
    return svc


def call(tool, **kwargs):
    """FastMCP wraps handlers; reach the underlying function."""
    fn = getattr(tool, "fn", tool)
    return fn(**kwargs)


def test_every_tool_is_registered():
    names = {
        "propose_picks", "apply_picks", "undo_last_apply",
        "explain_pick", "list_plans", "server_status",
    }
    for name in names:
        assert hasattr(server, name)


def test_propose_returns_a_plan_with_its_id_and_confirm_phrase(shoot):
    out = call(server.propose_picks, root=str(shoot))
    assert "Cull plan for" in out
    assert "plan_id:" in out
    assert 'confirm="apply 4 changes"' in out


def test_full_round_trip_through_the_tools(shoot):
    proposed = call(server.propose_picks, root=str(shoot))
    plan_id = [l for l in proposed.splitlines() if l.startswith("plan_id:")][0].split()[1]

    applied = call(server.apply_picks, plan_id=plan_id, confirm="apply 4 changes")
    assert "4 sidecars written" in applied
    assert "No original image file" in applied
    assert len(list(shoot.glob("*.xmp"))) == 4

    undone = call(server.undo_last_apply)
    assert "4 removed" in undone
    assert list(shoot.glob("*.xmp")) == []


def test_refusals_are_readable_and_actionable(shoot, tmp_path):
    outside = tmp_path / "not_yours"
    outside.mkdir()

    denied = call(server.propose_picks, root=str(outside))
    assert denied.startswith("Refused:")
    assert "allowlisted root" in denied

    forged = call(server.apply_picks, plan_id="deadbeef" * 4, confirm="apply 4 changes")
    assert forged.startswith("Refused:")
    assert "propose_picks" in forged  # tells the agent what to do instead

    nothing = call(server.undo_last_apply)
    assert nothing.startswith("Refused:")
    assert "Nothing to undo" in nothing


def test_wrong_confirmation_explains_the_rule(shoot):
    proposed = call(server.propose_picks, root=str(shoot))
    plan_id = [l for l in proposed.splitlines() if l.startswith("plan_id:")][0].split()[1]
    out = call(server.apply_picks, plan_id=plan_id, confirm="true")
    assert "deliberately not a boolean" in out
    assert list(shoot.glob("*.xmp")) == []


def test_list_plans_reflects_state(shoot):
    assert "No live plans" in call(server.list_plans)
    call(server.propose_picks, root=str(shoot))
    listed = call(server.list_plans)
    assert "4 writes of 6 frames" in listed


def test_status_leads_with_the_boundary(shoot):
    out = call(server.server_status)
    assert "Allowlisted roots" in out
    assert str(shoot.resolve()) in out
    assert "sidecars only" in out


def test_explain_through_the_tool(shoot):
    proposed = call(server.propose_picks, root=str(shoot))
    plan_id = [l for l in proposed.splitlines() if l.startswith("plan_id:")][0].split()[1]
    out = call(server.explain_pick, plan_id=plan_id, frame="DSC0001.ARW")
    assert "pick" in out
    assert "percentile" in out


def test_the_parser_demands_a_root():
    parser = server.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    args = parser.parse_args(["--root", "/tmp", "--root", "/var"])
    assert args.root == ["/tmp", "/var"]


def test_main_refuses_a_bad_root(capsys, tmp_path):
    code = server.main(["--root", str(tmp_path / "missing")])
    assert code == 1
    assert "error:" in capsys.readouterr().err
