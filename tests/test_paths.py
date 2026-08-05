from __future__ import annotations

import pytest

from shutter_cull_mcp.paths import Allowlist, RootAccessError


def test_server_refuses_to_exist_without_an_allowlist():
    with pytest.raises(RootAccessError):
        Allowlist([])


def test_rejects_missing_empty_and_file_roots(tmp_path):
    with pytest.raises(RootAccessError):
        Allowlist([str(tmp_path / "nope")])
    with pytest.raises(RootAccessError):
        Allowlist(["  "])
    f = tmp_path / "a.txt"
    f.write_text("x")
    with pytest.raises(RootAccessError):
        Allowlist([str(f)])


def test_paths_inside_a_root_resolve(shoot):
    allow = Allowlist([str(shoot)])
    assert allow.resolve_within(str(shoot)) == shoot.resolve()
    assert allow.resolve_within(str(shoot / "DSC0001.ARW")).name == "DSC0001.ARW"


def test_paths_outside_every_root_are_refused(tmp_path, shoot):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    allow = Allowlist([str(shoot)])
    with pytest.raises(RootAccessError) as exc:
        allow.resolve_within(str(outside))
    assert "outside every allowlisted root" in str(exc.value)


def test_dot_dot_traversal_cannot_escape(shoot):
    allow = Allowlist([str(shoot)])
    with pytest.raises(RootAccessError):
        allow.resolve_within(str(shoot / ".." ))


def test_a_symlink_is_judged_on_where_it_lands(tmp_path, shoot):
    # The classic escape: a link that sits inside the allowlist and points out.
    secret = tmp_path / "private"
    secret.mkdir()
    (secret / "client.ARW").write_bytes(b"not yours")
    link = shoot / "shortcut"
    link.symlink_to(secret)

    allow = Allowlist([str(shoot)])
    with pytest.raises(RootAccessError):
        allow.resolve_within(str(link / "client.ARW"))


def test_a_symlinked_root_resolves_to_its_target(tmp_path):
    real = tmp_path / "real_shoot"
    real.mkdir()
    link = tmp_path / "link_shoot"
    link.symlink_to(real)
    allow = Allowlist([str(link)])
    assert allow.roots == (real.resolve(),)


def test_sibling_prefix_is_not_containment(tmp_path):
    # /shoot must not be treated as containing /shoot-backup
    (tmp_path / "shoot").mkdir()
    sibling = tmp_path / "shoot-backup"
    sibling.mkdir()
    allow = Allowlist([str(tmp_path / "shoot")])
    with pytest.raises(RootAccessError):
        allow.resolve_within(str(sibling))


def test_multiple_roots_all_work(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    allow = Allowlist([str(a), str(b)])
    assert allow.resolve_within(str(a)) == a.resolve()
    assert allow.resolve_within(str(b)) == b.resolve()
    assert len(allow.roots) == 2
