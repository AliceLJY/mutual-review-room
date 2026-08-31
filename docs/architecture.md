# Mutual Review Room Architecture

Mutual Review Room is an owner-led terminal review system. It is not a shared
chat. The user writes only in the native
owner pane; each reviewer has an isolated provider-native session, and the
right-side panes are read-only projections of durable, user-visible events.

## Three layers

### 1. Owner policy

`mutual_review_room.cli` creates a job, starts or resumes the selected native owner,
and gives that owner the job-specific dispatch contract. Round 1 uses one exact
task-envelope file for every reviewer. Rounds 2 and 3 are directed follow-ups;
the owner adjudicates each answer and chooses what finding, if any, to quote to
another reviewer. Reviewers never receive the owner conversation or another
reviewer's transcript implicitly.

Dispatch requires the job's `owner.token` file. Only its hash is stored in the
database. Reviewer subprocesses receive a small environment allowlist; review
control variables, token/key variables, and unrelated parent values are not
inherited. This is a trusted single-user boundary, not remote or multi-user
authentication.

### 2. Persistent-session runtime

`mutual_review_room.runtime` contains explicit adapters for Claude Code, Codex, and
Kimi. An adapter returns both the visible final answer and its provider-native
session ID. The first successful request binds that ID to one reviewer in one
job. Later requests pass the stored ID to the provider's native resume command;
an unexpected ID is rejected instead of silently rebinding the lane.

The adapter catalog and one room's selected reviewer set are independent. A
launcher selects the exact lanes for that job. The room has no fixed reviewer
count limit, although terminal height is the practical readability limit. The catalog
may grow without changing the ledger protocol, but a new adapter must provide
provider-native start/resume identity, visible-final parsing, cold-context and
tool isolation, Seatbelt coverage, and lane-local failure semantics. Unknown
names fail closed. Arbitrary command templates are not an adapter contract
because they cannot prove those properties.

Every job owns a private SQLite database. Its append-only event ledger records
job, request, reviewer, native session, round, parent request, direction, type,
status, content, and timestamp. Database triggers reject event updates and
deletes. Request and reviewer rows hold the current operational projection;
events remain the audit history.

The runtime uses one process lock per reviewer lane. Round 1 fan-out is
deliberately serial in the MVP: unavailable or failed lanes are recorded and
the command continues to later reviewers. The control process never resubmits
an interrupted request automatically because a provider may already have
answered before the local commit. Before serial fan-out begins, every eligible
lane receives a durable `request_queued` event with its queue position. At the
actual provider boundary it receives `provider_answering`, which explicitly
says that the final response appears after completion rather than pretending
to provide token streaming.

Control state and reviewer workspaces use separate trees. The default control
root is `~/.mutual-review-room/review-jobs`, while reviewer lanes live under
`~/.mutual-review-room/review-workspaces/<job>/<reviewer>`. On macOS, every
reviewer process is wrapped in a Seatbelt profile that denies reads and writes
to the complete control root, the current owner cwd, and every other reviewer
workspace registered in that state root when dispatch begins. Those paths are resolved before launch,
overlapping paths are rejected, and missing
`sandbox-exec`, or a binary that cannot actually apply a harmless profile,
makes the lane unavailable rather than silently weakening it.
For a Kimi reviewer, existing global Kimi `AGENTS.md` and `mcp.json` files are
also denied. This keeps the normal authenticated Kimi owner environment intact
while preventing a cold reviewer from reading those global context sources.
The reviewer may still use its own workspace: this is scoped isolation, not a
claim that every provider has a global read-only filesystem.

### 3. Observer projection

`mutual_review_room.room` renders one filtered projection per reviewer. It shows the
complete owner prompt, complete user-visible final answer, round, timestamp,
status, provider, model, and stable native session ID. Internal reasoning,
tool traffic, raw protocol chunks, and other reviewers' lanes are excluded.

Each room runs on a dedicated tmux socket/server, so server-level key settings
do not leak into unrelated user sessions. tmux creates a roughly 50/50
left-right layout. Reviewer lanes are stacked on
the right according to reviewer count. Their borders use durable reviewer IDs,
such as `reviewer kimi` and `reviewer codex`. Observer
pane input is disabled with tmux's `input-off` flag. Mouse support lets either
side enter per-pane scrollback with the wheel. Every surviving pane is created
after a 100,000-line history limit is applied. The keyboard equivalent is
`Ctrl-b [`, followed by `PgUp`, `PgDn`, or the arrow keys, with `q` returning to
the live view. Copy mode does not weaken observer `input-off`. Each observer
prints one complete ledger snapshot when it starts, then appends only newly
visible events instead of clearing and redrawing the screen. Codex owners use
`--no-alt-screen` so their native TUI also leaves usable terminal history. tmux
extended-key support is enabled with `extended-keys-format csi-u` for the native
owner TUI. No `send-keys`, pane capture, or terminal scraping participates in
dispatch or state attribution; those commands are permitted only for acceptance
evidence.

## Durable identity and request graph

The identity chain is:

```text
job
├── owner provider + native owner session
└── reviewer lane
    ├── provider + model + isolated cwd + native reviewer session
    └── request
        ├── round + parent request
        └── append-only prompt/status/final events
```

A new job inserts reviewers with empty native session IDs. There is no lookup
or fallback to an older job. Within a job, a round-2 or round-3 parent must
belong to the same reviewer; if omitted, it resolves to that reviewer's last
request. Cross-lane parents and provider session drift fail closed.

## Restart and recovery semantics

The database and provider-native IDs survive controller and observer restarts.
Recreating the tmux room starts fresh projections over the same ledger and
resumes the stored owner session. `mutual-review-room recover` changes orphaned
`running` requests to `interrupted` and records that transition. It reports
`replayed: false` and never feeds a summary into a new session as a substitute
for provider-native continuation. Recovery does not release the interrupted
round number: after round N is interrupted, the same lane may continue only at
N+1. An interrupted round 3 must converge with that uncertainty reported or
move to a new job.

When the owner reaches convergence or the three-round ceiling, `mutual-review-room
complete --verdict-file PATH` requires the non-empty final synthesis, changes
the job to `complete`, and stores that synthesis in an immutable
`job_completed` event. A completed job cannot accept new dispatches.

This leaves one intentional boundary: if a provider accepted a request but the
controller crashed before committing the answer, exact-once delivery cannot be
proven unless that provider offers a durable idempotency key or result lookup.
The MVP prefers an explicit interrupted state over a potentially duplicated
automatic retry.

## Provider boundaries

- **Codex reviewer:** provider-native start/resume, user configuration and
  project rules ignored, strict per-invocation configuration that disables
  shell/unified execution, delegation, apps, browser/computer use, image
  generation/reading, and skill search, plus a CLI read-only sandbox and
  Seatbelt control/owner/peer isolation. Unknown safety keys fail closed instead
  of falling back to a tool-enabled run. Only visible agent messages are stored.
- **Kimi reviewer:** provider-native start/resume and assistant messages only.
  The current CLI transports prompts in argv and has no provider-native
  all-in-one read-only switch. The first turn binds a self-contained custom
  agent whose body omits base-prompt and inherited-context placeholders, whose
  tool/subagent allowlists are empty, and whose denylist blocks dynamic tool
  disclosure plus all MCP tools. Resume restores the bound profile. It also
  runs with a fresh invocation-private empty skills directory and Seatbelt
  denial of global Kimi instructions/MCP configuration plus
  control/owner/peer paths. Kimi model overrides are rejected before job
  creation because the tested CLI has no matching adapter flag.
- **Claude reviewer:** provider-native session flags, no allowed tools, visible
  final result parsing, and the same Seatbelt wrapper before invocation.
- **Owner:** trusted native CLI session. The owner can write because it must
  create prompt files and call the local control CLI. A Codex owner uses
  `--approve-for-me`, which selects automatic review with the `workspace-write`
  sandbox. The current CLI rejects combining it with explicit `-s` or `-a`
  options, so the room does not duplicate them; it does not use
  `danger-full-access`. If the local shared app-server control socket exists,
  resume connects through it; this reattaches a loaded owner after tmux is
  rebuilt instead of opening a competing writer. Provider-specific owner
  limitations are documented in the README instead of being hidden behind a
  generic availability label. The adapter contract is tested against Claude
  Code 2.1.251, Codex CLI 0.151.0, Kimi CLI 0.39.1, and tmux 3.7b; drift in
  provider flags or event schemas requires a new compatibility check.

## Non-goals

The MVP does not add a browser dashboard, Electron application, Redis service,
long-running controller daemon, shared reviewer chat, hidden-chain-of-thought
display, automatic semantic voting, or cross-job memory.
