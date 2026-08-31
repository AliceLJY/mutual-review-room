# Mutual Review Room

[中文说明](README_CN.md)

Mutual Review Room puts one interactive owner and any selected reviewers in a
single tmux room. You talk only to the owner in the left pane. Reviewer answers
appear verbatim in read-only panes on the right, so you can watch the full
review process without relaying messages yourself.

```text
┌──────────────────────────┬──────────────────────────┐
│ owner                    │ reviewer kimi            │
│                          ├──────────────────────────┤
│ talk here                │ reviewer codex           │
└──────────────────────────┴──────────────────────────┘
```

Each reviewer uses a separate provider-native session. The first round can use
one identical task envelope, while later rounds can ask each reviewer a
different follow-up without losing that reviewer's earlier context.

## Requirements

- macOS with `sandbox-exec`
- Python 3.10 or newer
- tmux
- At least one authenticated provider CLI: `claude`, `codex`, or `kimi`

The Python runtime itself uses only the standard library. macOS Seatbelt is the
current fail-closed filesystem boundary for reviewer processes.

This is a version-sensitive alpha: provider CLIs change flags and event schemas.
The release is tested on both development Macs with the following versions:

| Component | Tested version |
| --- | --- |
| Claude Code | 2.1.251 |
| Codex CLI | 0.151.0 |
| Kimi CLI | 0.39.1 |
| tmux | 3.7b |

## Install

```bash
git clone https://github.com/AliceLJY/mutual-review-room.git
cd mutual-review-room
python3 -m pip install .
```

You can also run directly from a clone with `./scripts/mutual-review-room`.

## Quick start

Check which built-in providers are discoverable:

```bash
mutual-review-room providers
```

Open a room with a Codex owner and two reviewers:

```bash
mutual-review-room launch \
  --owner codex \
  --reviewer kimi \
  --reviewer codex \
  --cwd "$PWD"
```

Talk only in the left pane. The right panes are read-only. Point at any pane and
use the mouse wheel to read its retained scrollback. Each pane keeps up to
100,000 lines, rather than tmux's small default history.

Repeat `--reviewer` for every reviewer you want. There is no fixed lane-count
limit; terminal height is the practical limit. Built-in adapters currently
cover Claude Code, Codex CLI, and Kimi CLI.

To run two independent sessions from the same provider, give them unique aliases:

```bash
mutual-review-room launch \
  --owner codex \
  --reviewer kimi-a=kimi \
  --reviewer kimi-b=kimi
```

The ordinary form is `--reviewer kimi`; redundant forms such as `kimi=kimi`
are rejected with a correction.

Kimi currently uses its CLI-selected default model. `--owner-model` and
`--reviewer-model` overrides for Kimi are rejected before a job is created.

## What persists

Jobs and append-only SQLite ledgers live under
`~/.mutual-review-room/review-jobs`. Isolated reviewer workspaces live under
`~/.mutual-review-room/review-workspaces/<job>/<reviewer>`.

The ledger records the exact prompt, visible final answer, reviewer identity,
round, parent request, provider-native session ID, status, and timestamp.
Rebuilding a tmux room restores projections over the same ledger and resumes
the stored native sessions. Every room uses a dedicated tmux server, so its
mouse, history, and key settings do not change unrelated tmux sessions.

Useful recovery commands:

```bash
mutual-review-room status --job JOB_ID
mutual-review-room room --job JOB_ID --replace
mutual-review-room recover --job JOB_ID --token-file TOKEN_FILE
```

`recover` records orphaned work as interrupted; it never resends that prompt or
releases its round number. If round N is interrupted, continue at N+1. If round
3 is interrupted, converge with that uncertainty stated or start a new job.

## Review contract

- Round 1 sends the same self-contained task envelope to each selected reviewer.
- Later rounds may use reviewer-specific follow-ups.
- Reviewers cannot implicitly read the owner conversation or another lane.
- Reviewer panes display visible answers, not hidden reasoning or tool traffic.
- The owner must stop after round 3 and report unresolved disagreement honestly.
- A failed or unavailable lane does not erase successful lanes.

Dispatch uses the durable ledger, not tmux `send-keys`, pane scraping, or a
shared chat. See [the architecture document](docs/architecture.md) for the
identity, isolation, recovery, and exact-once boundaries.

## Advanced tmux shortcuts

Mouse-wheel scrolling is the normal interface. tmux users may also enter copy
mode with `Ctrl-b [` and return to the live view with `q`.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q mutual_review_room
ruff check .
```

MIT licensed.
