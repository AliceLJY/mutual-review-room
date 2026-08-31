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

Each job has an owner token whose hash is stored in the database. The native
launcher injects the token into the trusted owner process without putting it in
the owner prompt or command arguments. Owner control commands authenticate
signed mailbox envelopes; the broker verifies them against the private token
file. Reviewer subprocesses receive a small environment allowlist, so review
control variables, token/key variables, and unrelated parent values are not
inherited. This is a trusted single-user boundary, not remote or multi-user
authentication.

### 2. Persistent-session runtime

Room creation starts a hidden job-local broker window before the interactive
owner. This process inherits the native launcher environment rather than the
Codex owner's Seatbelt sandbox. The owner writes a bounded, HMAC-signed JSON
request to a private ordinary-file inbox and waits for a signed result. The
broker atomically claims the file, commits through the existing ledger, and
invokes the provider. It keeps a heartbeat while provider calls block. A
claimed request left by a broker crash is reported as abandoned and is never
automatically replayed. Ordinary files are intentional: the Codex sandbox
rejects control connections to the outer tmux socket, and the design does not
open a network or Unix-socket service.

The owner's writable grant is one directory: the mailbox inbox. Everything the
owner *writes* has to fit inside it. The lock that orders enqueues is the one
thing that must not: `flock` binds to an inode, so whoever can write the lock's
directory can unlink or replace the entry and leave two submitters holding
locks on two different inodes under the same name.

The lock therefore stays at `<job>/broker/queue.lock`, outside the grant, and
the two halves of using it are separated. `prepare_mailbox` creates it once,
before any sandboxed process starts, and enforces its mode. A submitter then
opens that existing file read-only and takes `flock` on the descriptor —
`flock` needs an open descriptor, not a writable one, so this works from a
sandbox that denies every write to the control root. A missing or non-private
lock fails closed instead of being re-created by the submitter.

The rejected alternative was widening the grant to the whole mailbox, which
would also have handed the owner the broker's outbox, processing claims, and
heartbeat.

Commands split by whether they write. `status` is a pure read and runs directly
against the job database, so it neither needs the broker nor queues behind a
long provider call; the control root's permissions are checked but only
rewritten when they are actually wrong, because an unconditional metadata write
fails inside the owner sandbox even when it would change nothing. `recover`
transitions request state, so it goes through the broker when one is serving
the job and falls back to a direct transition only when there is no broker —
and therefore no sandbox — to respect. Owner authority is accepted from either
the private token file or the token the native launcher injects, since the
launcher removes the file variable from the owner environment.

`mutual_review_room.runtime` contains explicit adapters for Claude Code, Codex,
and Kimi. An adapter returns both the visible final answer and its
provider-native session ID. The first successful request binds that ID to one
reviewer in one job. Later requests pass the stored ID to the provider's native
resume command; an unexpected ID is rejected instead of silently rebinding the
lane.

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

The broker uses one process lock per job, so only one controller can claim
that job's signed requests. Round 1 fan-out is deliberately serial in the MVP:
unavailable or failed lanes are recorded and the broker continues to later
reviewers. The control path never resubmits
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
do not leak into unrelated user sessions. A hidden window owns the broker; it
is not part of the visible layout or an owner command channel. tmux creates a roughly 50/50
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

The database, mailbox, and provider-native IDs survive owner and observer
restarts. Closing the terminal normally detaches from the dedicated tmux
server, so its broker and in-flight request continue. Recreating the tmux room
starts fresh projections over the same ledger and resumes the stored owner
session. `mutual-review-room recover` changes orphaned
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
  project rules ignored, per-invocation configuration that *requests* disabling
  shell/unified execution, delegation, apps, browser/computer use, image
  generation/reading, and skill search, plus a CLI read-only sandbox and
  Seatbelt control/owner/peer isolation. Only visible agent messages are stored.

  Requesting an override is not the same as obtaining it. `--strict-config`
  only rejects keys the CLI does not recognise, so a key that is accepted and
  then ignored produces no error and no exit code for the failure classifier to
  catch. The adapter therefore replays the same overrides through
  `codex features list` and reads the effective state back.
  `provider_capabilities()` reports that measurement — `tool_access` is `none`
  only when every requested override is confirmed applied,
  `sandboxed-residual` when the CLI keeps one enabled, and `unverified` when
  the state cannot be read at all. The result is stored in each reviewer's
  capability row when the job is created, so a room records the boundary it
  actually had rather than the one it asked for.

  **Measured on Codex CLI 0.151.0:** eight of the nine requested overrides
  apply; `features.unified_exec=false` is accepted and silently ignored, and
  `codex features --disable unified_exec` does not disable it either. The
  remaining boundary for that surface is the CLI read-only sandbox plus the
  Seatbelt profile, not the absence of the tool. This is reported rather than
  failed closed: failing closed would disable Codex reviewers entirely on the
  tested CLI, and the sandbox boundary is still enforced.
- **Kimi reviewer:** provider-native start/resume and assistant messages only.
  The current CLI transports prompts in argv and has no provider-native
  all-in-one read-only switch. The first turn binds a self-contained custom
  agent whose body omits base-prompt and inherited-context placeholders, whose
  tool/subagent allowlists are empty, and whose denylist blocks dynamic tool
  disclosure plus all MCP tools. Resume restores the bound profile. It also
  runs with a fresh invocation-private empty skills directory and Seatbelt
  denial of global Kimi instructions/MCP configuration plus
  control/owner/peer paths. Kimi model overrides are rejected before job
  creation. Kimi CLI 0.39.1 does have a `-m/--model` flag; the adapter
  deliberately does not pass it, so a per-lane model could be neither applied
  nor verified. The room rejects the request up front instead of accepting a
  model it would silently ignore.
- **Claude reviewer:** provider-native session flags, no allowed tools, visible
  final result parsing, and the same Seatbelt wrapper before invocation.
- **Owner:** trusted native CLI session. The owner creates prompt files and
  writes only signed control requests; it does not invoke reviewer providers.
  A Codex owner receives the broker inbox as an additional writable directory
  and uses `--approve-for-me`, which selects automatic review with the
  `workspace-write` sandbox. It also sets
  `projects.<owner-cwd>.trust_level="trusted"` as a per-invocation config
  override, so a newly created room does not stop at Codex's project-trust
  screen or mutate the user's global config. The current CLI rejects combining
  `--approve-for-me` with explicit `-s` or `-a` options, so the room does not
  duplicate them; it does not use `danger-full-access`. tmux owns the live
  interactive CLI process. Rebuilding a room invokes the provider's native
  resume command with the stored session ID, restoring the same owner
  conversation without depending on a shared app-server process.
  Provider-specific owner limitations are documented in the README instead of
  being hidden behind a generic availability label. The adapter contract is
  tested against Claude Code 2.1.251, Codex CLI 0.151.0, Kimi CLI 0.39.1, and
  tmux 3.7b; drift in provider flags or event schemas requires a new
  compatibility check.

## Non-goals

The MVP does not add a browser dashboard, Electron application, Redis or
network service, shared reviewer chat, hidden-chain-of-thought display,
automatic semantic voting, or cross-job memory.
