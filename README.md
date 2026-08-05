# shutter-cull-mcp

[![CI](https://github.com/keivanmalhani/shutter-cull-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/keivanmalhani/shutter-cull-mcp/actions/workflows/ci.yml)
![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)

English | [Espanol](README.es.md)

An MCP server that lets an AI agent cull your photo shoot, and cannot run away with it.

It wraps [shutter-cull](https://github.com/keivanmalhani/shutter-cull), which scores frames for sharpness, eye-openness and aesthetics and writes picks as XMP sidecars Lightroom reads natively. The engine was already safe to run. Handing it to an agent is a different problem, and this repo is the answer to that problem, not a thin RPC wrapper around a CLI.

## The problem this solves

[shutter-mcp](https://github.com/keivanmalhani/shutter-mcp) is read-only, and its entire security story is that sentence. Adding writes to it would have cost the guarantee. So writes live here instead, behind their own envelope.

An agent that can write to a photo library is a different risk class from one that can read it. It can be prompt-injected by a filename. It can hallucinate a folder. It can call a tool it was told about but never showed anyone. And a `confirm: true` argument is no defense at all, because a model that will call the tool will also pass `true`.

So in this server, writing is not a tool an agent calls. It is a plan an agent must produce, show, and then quote back.

## Five gates

```text
  propose_picks ......... read-only. Returns a plan and a plan_id.
        |
        |  the agent shows the plan to the human
        v
  apply_picks(plan_id, confirm) .... refuses unless ALL of:
        |
        |   1  plan_id was issued by THIS running process
        |   2  the plan has not expired
        |   3  nothing under it changed since it was proposed
        |   4  confirm is the exact phrase "apply N changes"
        |   5  every frame still resolves inside an allowlisted root
        v
  sidecars written .... and journaled, so undo_last_apply reverses them
```

**1. The plan_id cannot be forged.** It is a sha256 over the root and over every frame's path, decision, size and mtime. An agent cannot construct one, guess one, or carry one over from another session. A restarted server honors none.

**2. Plans expire.** Thirty minutes, in memory only, never written to disk.

**3. Staleness is re-checked at write time.** Between a human approving a plan and the write landing, the library can move. If any frame was edited, replaced or deleted in that window, the apply is refused and names the files. Nothing partial is written.

**4. The confirmation is not a boolean.** It must be the literal string the plan printed, `apply 12 changes`, with the count matching. Quoting the blast radius back is cheap evidence that the plan was actually read. Reflexive confirmation is a real agent failure mode and a boolean invites it.

**5. The root allowlist is fixed at startup.** The server is launched with `--root` and refuses to start without one. No tool widens it. Paths are resolved through symlinks before the containment check, so a link that sits inside the allowlist and points outside is rejected on where it lands. Containment is checked again immediately before writing, because a symlink can be swapped in between.

And `apply_picks` takes no folder argument. A plan can only ever be applied to the folder it was proposed against, because there is nowhere to put a different one.

## Everything is reversible

Every sidecar is snapshotted before it is touched. `undo_last_apply` restores each one exactly, and deletes the ones that did not exist before. A sidecar that something else edited after the write is left alone and reported by name rather than silently overwritten.

Two honest limits: undo is one step deep, and it does not survive a server restart. Persisting the journal would mean this server writing files it was never asked to write, which is a worse trade than losing undo across restarts.

## What it never does

- Never opens an original image file for writing. Only `.xmp` sidecars, beside the originals, which is the same non-destructive posture Lightroom itself uses for RAW metadata.
- Never touches a path outside the allowlist.
- Never writes without a plan that a human was shown.
- Never makes a network call, except shutter-cull's own opt-in, checksum-verified model download.

## Install

```bash
pip install git+https://github.com/keivanmalhani/shutter-cull-mcp.git
```

The engine is a separate install, because the safety envelope is useful to read and audit on its own:

```bash
pip install git+https://github.com/keivanmalhani/shutter-cull.git
```

```bash
brew install exiftool
```

`server_status` reports honestly when the engine is missing rather than failing mysteriously later.

## Run

```bash
shutter-cull-mcp --root ~/Pictures/2026-shoots
```

Repeat `--root` for more folders. In an MCP client config:

```json
{
  "mcpServers": {
    "shutter-cull": {
      "command": "shutter-cull-mcp",
      "args": ["--root", "/Users/you/Pictures/2026-shoots"]
    }
  }
}
```

## Tools

| Tool | Writes | What it does |
| --- | --- | --- |
| `propose_picks` | no | Runs the full pipeline read-only and returns a plan plus a `plan_id`. |
| `apply_picks` | **yes** | Writes the sidecars for an already-proposed plan, behind all five gates. |
| `undo_last_apply` | yes | Reverses the last apply exactly. |
| `explain_pick` | no | Why one frame got its decision. A frame the engine left alone has no entry, and that absence is the answer. |
| `list_plans` | no | Live plans, newest first. If a plan is not listed it cannot be applied. |
| `server_status` | no | The allowlisted roots, which are the hard boundary on everything here. |

## A session

```text
propose_picks(root="~/Pictures/canyon-2026")

  Cull plan for /Users/keivan/Pictures/canyon-2026
  412 frames analyzed.
  38 picks, 61 rejects, 313 left alone.
  ...
  plan_id: 8e2806e0ed2bfa8930c05349089e586a
  To apply, call apply_picks with that plan_id and confirm="apply 99 changes".

apply_picks(plan_id="8e28...", confirm="true")
  Refused: Confirmation did not match. To apply this plan the confirm
  argument must be exactly "apply 99 changes". This is deliberately not a
  boolean: quoting the change count back is evidence the plan was read.

apply_picks(plan_id="8e28...", confirm="apply 99 changes")
  Applied. 99 sidecars written: 38 picks, 61 rejects.
  Only .xmp sidecars were written. No original image file was opened for
  writing. Call undo_last_apply to reverse this.
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

51 tests, no engine required: the suite runs against a fake engine on purpose. What this server is made of is the safety envelope, and the envelope should be testable, fast, and auditable without the CV stack present. The tests are written as attacks: forging a token, replaying one, confirming reflexively, racing the human's approval, escaping the allowlist by symlink. The adapter is separately verified against the real engine end to end, including a full apply and undo through exiftool.

## Family

[shutter-cull](https://github.com/keivanmalhani/shutter-cull) is the engine. [shutter-mcp](https://github.com/keivanmalhani/shutter-mcp) is its read-only sibling, kept read-only on purpose. [shutter-select](https://github.com/keivanmalhani/shutter-select) does the same job for video.

## License

MIT, see [LICENSE](LICENSE).
