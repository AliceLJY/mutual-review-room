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

| Component | Tested version |
| --- | --- |
| Claude Code | 2.1.251 |
| Codex CLI | 0.151.0 |
| Kimi CLI | 0.39.1 |
| tmux | 3.7b |

Not every provider role has been exercised against a live account. What the
0.1.0 acceptance run actually covered, on macOS 27 with those versions:

| Role | State | Basis |
| --- | --- | --- |
| Kimi owner | verified | two-round room, native session resumed |
| Codex owner | verified | room launched, dispatch issued from inside the owner sandbox |
| Kimi reviewer | verified | two rounds, stable native session |
| Codex reviewer | verified | two rounds, stable native session |
| Claude owner | verified | full round 1: launch, contract load, dispatch issued, both reviewers answered |
| Claude reviewer | **unverified** | no live round yet |

The Claude adapter is exercised by the automated tests. During the 0.1.0
acceptance run the account was rate-limited (HTTP 429), so neither Claude role
was verified; on 2026-09-01 the Claude owner was retested on 0.1.1 through a
full round 1: room launch, owner contract load and interactive takeover, a
`dispatch-all` issued by the owner, and independent answers from both the codex
and kimi reviewers returning to the owner — all succeeded, with no rate
limiting. The one rough edge is that the contract-load turn is slow: two
measured runs took 4m24s and 2m29s (Codex takes seconds for the same step), and
the first logged one `model_refusal_fallback`. Final adjudication (`complete`)
and the Claude reviewer have not run live yet, so verify those two yourself.

## Install

```bash
uv tool install git+https://github.com/AliceLJY/mutual-review-room
```

That puts `mutual-review-room` on your `PATH` in an isolated environment.
`pipx install git+https://github.com/AliceLJY/mutual-review-room` does the same
thing if you prefer pipx. To move to a newer version later, re-run the same
command with `--force`.

From a clone instead:

```bash
git clone https://github.com/AliceLJY/mutual-review-room.git
cd mutual-review-room
python3 -m pip install .          # inside a virtualenv
./scripts/mutual-review-room      # or just run it in place, no install
```

A bare `pip install .` against a Homebrew or system Python is refused by
[PEP 668](https://peps.python.org/pep-0668/); use a virtualenv, or one of the
tool installers above, rather than `--break-system-packages`.

**This is deliberately not on PyPI.** A one-command install already works
straight from git, so publishing would buy a shorter name and nothing else,
while a PyPI name is claimed permanently on first upload. That is a poor trade
for something this young: macOS-only, alpha, and tied to the exact flags and
event schemas of three provider CLIs that can change under it. Worth
reconsidering once it has real usage behind it and someone other than the
author wants to install it.

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

A Codex owner starts with automatic approval inside its normal
`workspace-write` sandbox. The launcher marks only the exact owner working
directory as trusted for that invocation and gives it the private broker inbox
as one additional writable directory. It does not edit global Codex config or
stop at the project-trust question. Binding the owner's native session makes
one real provider call first, so the owner pane can take up to a minute to
appear.

Running from a clone rather than an install is supported: the owner is given a
fully qualified `python3 -m mutual_review_room.cli --root ...` command, not a
bare `mutual-review-room`, so it works whether or not the console script is on
`PATH`.

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
Kimi CLI 0.39.1 does have a `-m/--model` flag; the adapter does not pass it, so
the room refuses the option instead of accepting a model it would ignore.

## Reviewer tool isolation is measured, not assumed

`mutual-review-room providers` reports what each adapter's isolation actually
achieved, not what it requested. For Codex this matters: the room asks the CLI
to disable nine execution and media surfaces, and on Codex CLI 0.151.0 eight of
them apply while `features.unified_exec=false` is accepted and silently
ignored. `--strict-config` does not catch it, because the key is recognised —
just not honoured.

So `tool_access` is reported as `sandboxed-residual` rather than `none`, naming
the surface that stayed on, and the measurement is stored in the job's reviewer
row. Codex reviewers remain bounded by the CLI read-only sandbox and the
Seatbelt profile; that boundary is what is doing the work for that surface, and
the room says so instead of claiming the tool is gone.

## What persists

Jobs and append-only SQLite ledgers live under
`~/.mutual-review-room/review-jobs`. Isolated reviewer workspaces live under
`~/.mutual-review-room/review-workspaces/<job>/<reviewer>`.

The ledger records the exact prompt, visible final answer, reviewer identity,
round, parent request, provider-native session ID, status, and timestamp.
Rebuilding a tmux room restores projections over the same ledger and resumes
the stored native sessions. Every room uses a dedicated tmux server, so its
mouse, history, and key settings do not change unrelated tmux sessions.

A hidden job-local broker starts with the room. The sandboxed owner writes a
signed request to a private file inbox; the broker invokes reviewers outside
the owner's sandbox and applies each reviewer's own Seatbelt boundary. This
avoids nested macOS sandboxes and does not require an approval prompt for every
reviewer turn. The broker is not a network service, and reviewer processes
cannot read its control directory.

Useful recovery commands:

```bash
mutual-review-room status --job JOB_ID
mutual-review-room room --job JOB_ID --replace
mutual-review-room recover --job JOB_ID --token-file TOKEN_FILE
```

`status` is a pure read and needs no token. `recover` needs owner authority:
inside the owner pane the injected token is enough, so `--token-file` is only
needed from another shell.

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

Dispatch uses the signed file inbox and durable ledger, not tmux `send-keys`,
pane scraping, sockets, or a shared chat. See
[the architecture document](docs/architecture.md) for the identity,
isolation, recovery, and exact-once boundaries.

## Advanced tmux shortcuts

Mouse-wheel scrolling is the normal interface. tmux users may also enter copy
mode with `Ctrl-b [` and return to the live view with `q`.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q mutual_review_room
python3 -m pip install '.[dev]'   # ruff is not a runtime dependency
ruff check .
```

The lint rule set is pinned in `pyproject.toml`. Ruff's default selection
widened in 0.16, so leaving it unpinned made the same command pass on one ruff
version and report dozens of style findings on another.

MIT licensed.
