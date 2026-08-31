"""Owner-only control CLI for mutual-review terminal rooms."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import resource
import shlex
import shutil
import sys
import uuid
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .broker import (
    BrokerError,
    BrokerResponse,
    BrokerUnavailable,
    MailboxSecurityError,
    mailbox_paths,
    prepare_mailbox,
    serve_forever,
    submit as submit_broker_request,
    wait as wait_for_broker,
)
from .room import TmuxRoom, TmuxRoomError, observe_reviewer
from .runtime import (
    ProviderFailed,
    ProviderUnavailable,
    invoke,
    provider_available,
    provider_capabilities,
    reviewer_isolation_available,
)
from .state import (
    DEFAULT_REVIEW_ROOT,
    ReviewState,
    ReviewStateError,
    ReviewerSpec,
    StateConflictError,
    ValidationError,
)


class CliError(RuntimeError):
    pass


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _print_json(value: Any) -> None:
    print(json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True))


def _root(args: argparse.Namespace) -> Path:
    configured = args.root or os.environ.get("MRR_ROOT") or DEFAULT_REVIEW_ROOT
    return Path(configured).expanduser().resolve()


def _job_id(args: argparse.Namespace) -> str:
    value = getattr(args, "job", None) or os.environ.get("MRR_JOB_ID")
    if not value:
        raise CliError("job ID is required (use --job or MRR_JOB_ID)")
    return value


def _model(value: str) -> str | None:
    return None if value == "default" else value


def _read_prompt(args: argparse.Namespace) -> str:
    prompt_file = getattr(args, "prompt_file", None)
    if prompt_file:
        path = Path(prompt_file).expanduser()
        if path.is_symlink() or not path.is_file():
            raise CliError("prompt file must be a regular, non-symlink file")
        if path.stat().st_size > 1_000_000:
            raise CliError("prompt file exceeds 1,000,000 bytes")
        prompt = path.read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            raise CliError("provide --prompt-file or pipe the task envelope on stdin")
        prompt = sys.stdin.read(1_000_001)
        if len(prompt) > 1_000_000:
            raise CliError("prompt exceeds 1,000,000 characters")
    if not prompt.strip():
        raise CliError("task envelope must not be empty")
    return prompt


def _read_verdict(args: argparse.Namespace) -> str:
    path = Path(args.verdict_file).expanduser()
    if path.is_symlink() or not path.is_file():
        raise CliError("verdict file must be a regular, non-symlink file")
    if path.stat().st_size > 1_000_000:
        raise CliError("verdict file exceeds 1,000,000 bytes")
    verdict = path.read_text(encoding="utf-8")
    if not verdict.strip():
        raise CliError("final verdict must not be empty")
    return verdict


def _authorize(state: ReviewState, job_id: str, token_file: str | None) -> None:
    """Accept the same owner authority the broker clients accept.

    The native owner launcher removes ``MRR_OWNER_TOKEN_FILE`` and injects
    ``MRR_OWNER_TOKEN`` instead, so a file-only check can never succeed inside
    the owner pane.
    """

    token = _client_token(state.root, job_id, token_file)
    if not state.verify_owner_token(job_id, token):
        raise CliError("owner control token is invalid")


def _client_token(root: Path, job_id: str, supplied: str | None) -> str:
    """Read owner authority without making the sandboxed client open SQLite."""

    environment_token = os.environ.get("MRR_OWNER_TOKEN")
    if supplied is None and environment_token:
        token = environment_token.strip()
        if token:
            return token
    configured = supplied or os.environ.get("MRR_OWNER_TOKEN_FILE")
    if not configured:
        raise CliError("owner control token is not configured")
    expected = (root / job_id / "owner.token").resolve()
    candidate = Path(configured).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise CliError("owner control token file is unavailable")
    if candidate.resolve() != expected:
        raise CliError("owner control token file does not belong to this job")
    token = candidate.read_text(encoding="utf-8").strip()
    if not token:
        raise CliError("owner control token is invalid")
    return token


@contextmanager
def _reviewer_lock(state: ReviewState, job_id: str, reviewer_id: str):
    lock_dir = state.root / job_id / "locks"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(lock_dir, 0o700)
    path = lock_dir / f"{reviewer_id}.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _reviewer_deny_paths(
    state: ReviewState, job_id: str, reviewer_id: str
) -> tuple[str, ...]:
    """Return control, owner, and cross-job lane paths a reviewer must not read."""

    job = state.get_job(job_id)
    reviewer = state.get_reviewer(job_id, reviewer_id)
    own_cwd = Path(reviewer.cwd).resolve(strict=True)
    denied = [
        state.root.resolve(strict=True),
        Path(job.owner_cwd).resolve(strict=True),
    ]
    for candidate_job in state.list_jobs():
        for item in state.list_reviewers(candidate_job.job_id):
            candidate = Path(item.cwd)
            try:
                path = candidate.resolve(strict=True)
            except FileNotFoundError:
                # Historical reviewer workspaces are disposable.  A plain
                # missing path protects no readable data and must not disable
                # every future dispatch after the user cleans it up.  Broken
                # symlinks remain a fail-closed condition because their target
                # identity is ambiguous.
                if candidate.is_symlink():
                    raise CliError("historical reviewer workspace is unavailable") from None
                continue
            except OSError:
                raise CliError("historical reviewer workspace is unavailable") from None
            if path != own_cwd:
                denied.append(path)
    unique: list[str] = []
    for path in denied:
        if path == own_cwd or path in own_cwd.parents:
            raise CliError("reviewer workspace overlaps a protected path")
        value = os.fspath(path)
        if value not in unique:
            unique.append(value)
    return tuple(unique)


def dispatch_one(
    state: ReviewState,
    job_id: str,
    reviewer_id: str,
    *,
    round_no: int,
    prompt: str,
    parent_request_id: str | None = None,
    request_id: str | None = None,
    timeout: float = 300.0,
    invoke_fn: Callable[..., Any] = invoke,
) -> dict[str, Any]:
    """Run one attributable request under a per-reviewer process lock."""

    with _reviewer_lock(state, job_id, reviewer_id):
        reviewer = state.get_reviewer(job_id, reviewer_id)
        if reviewer.status == "unavailable":
            return {
                "job_id": job_id,
                "reviewer_id": reviewer_id,
                "status": "skipped",
                "reason": "reviewer is unavailable",
                "session_id": reviewer.native_session_id,
            }
        # Finish every local path/capability preflight before a durable request
        # enters the running state.  A missing historical workspace must not
        # strand an otherwise unstarted request.
        deny_paths = _reviewer_deny_paths(state, job_id, reviewer_id)
        request = state.begin_request(
            job_id,
            reviewer_id,
            round=round_no,
            prompt=prompt,
            request_id=request_id,
            parent_request_id=parent_request_id,
        )
        try:
            state.append_event(
                job_id,
                request_id=request.request_id,
                direction="system",
                event_type="provider_answering",
                status="running",
                content=(
                    "Provider is answering. The complete final response will appear "
                    "after completion; this pane is not a token stream."
                ),
            )
            result = invoke_fn(
                reviewer.provider,
                prompt,
                cwd=reviewer.cwd,
                session_id=reviewer.native_session_id,
                model=_model(reviewer.model),
                timeout=timeout,
                deny_paths=deny_paths,
            )
            state.set_reviewer_session(
                job_id,
                reviewer_id,
                result.session_id,
                request_id=request.request_id,
            )
            completed = state.complete_request(job_id, request.request_id, result.response)
        except ProviderUnavailable as error:
            state.mark_unavailable(
                job_id,
                reviewer_id,
                error.message,
                request_id=request.request_id,
            )
            return {
                "job_id": job_id,
                "request_id": request.request_id,
                "reviewer_id": reviewer_id,
                "round": round_no,
                "status": "unavailable",
                "reason": error.category,
                "session_id": reviewer.native_session_id,
            }
        except ProviderFailed as error:
            state.fail_request(job_id, request.request_id, error.message)
            return {
                "job_id": job_id,
                "request_id": request.request_id,
                "reviewer_id": reviewer_id,
                "round": round_no,
                "status": "failed",
                "reason": error.category,
                "session_id": reviewer.native_session_id,
            }
        except ReviewStateError:
            # The provider may have answered with a different native identity;
            # preserve the durable request failure without trusting the answer.
            try:
                state.fail_request(
                    job_id,
                    request.request_id,
                    "provider session identity could not be bound",
                )
            except ReviewStateError:
                pass
            raise
        except BaseException:
            # Once begin_request commits, every exit path must settle it.  Keep
            # the public ledger message generic so local exception details and
            # provider stderr are never persisted accidentally.
            try:
                state.fail_request(
                    job_id,
                    request.request_id,
                    "controller stopped before a provider response was committed",
                )
            except ReviewStateError:
                pass
            raise
        return {
            "job_id": job_id,
            "request_id": completed.request_id,
            "parent_request_id": completed.parent_request_id,
            "reviewer_id": reviewer_id,
            "round": completed.round,
            "status": completed.status,
            "session_id": result.session_id,
            "response": result.response,
        }


def _round_request_id(job_id: str, reviewer_id: str, round_no: int) -> str:
    material = f"{job_id}\0{reviewer_id}\0{round_no}".encode("utf-8")
    return f"req_{hashlib.sha256(material).hexdigest()[:32]}"


def _dispatch_all_local(
    state: ReviewState,
    job_id: str,
    *,
    round_no: int,
    prompt: str,
    timeout: float,
) -> dict[str, Any]:
    """Run one serial fan-out inside the native broker process."""

    job = state.get_job(job_id)
    if job.status in {"complete", "failed"}:
        raise StateConflictError(f"job is already {job.status}")
    if not 1 <= round_no <= min(job.max_rounds, 3):
        raise ValidationError(
            f"round {round_no} exceeds job maximum {job.max_rounds}"
        )
    reviewers = state.list_reviewers(job_id)
    queued = [
        reviewer
        for reviewer in reviewers
        if reviewer.status not in {"unavailable", "running"}
        and reviewer.current_round + 1 == round_no
    ]
    for position, reviewer in enumerate(queued, start=1):
        existing = state.list_events(
            job_id,
            reviewer_id=reviewer.reviewer_id,
            event_type="request_queued",
        )
        if any(event.round == round_no for event in existing):
            continue
        state.append_event(
            job_id,
            reviewer_id=reviewer.reviewer_id,
            session_id=reviewer.native_session_id,
            round=round_no,
            direction="system",
            event_type="request_queued",
            status="queued",
            content=(
                f"Queued for serial dispatch ({position}/{len(queued)}); "
                "the provider has not started."
            ),
        )
    # Only eligible lanes are dispatched, but an ineligible lane is still
    # reported so the owner can see why it did not take part in this round.
    eligible = {reviewer.reviewer_id for reviewer in queued}
    results: list[dict[str, Any]] = [
        {
            "job_id": job_id,
            "reviewer_id": reviewer.reviewer_id,
            "round": round_no,
            "status": "skipped",
            "reason": (
                f"lane is {reviewer.status} at round {reviewer.current_round}; "
                f"round {round_no} is not its next round"
            ),
        }
        for reviewer in reviewers
        if reviewer.reviewer_id not in eligible
    ]
    for reviewer in queued:
        try:
            results.append(
                dispatch_one(
                    state,
                    job_id,
                    reviewer.reviewer_id,
                    round_no=round_no,
                    prompt=prompt,
                    timeout=timeout,
                    request_id=_round_request_id(
                        job_id, reviewer.reviewer_id, round_no
                    ),
                )
            )
        except ReviewStateError as error:
            results.append(
                {
                    "job_id": job_id,
                    "reviewer_id": reviewer.reviewer_id,
                    "round": round_no,
                    "status": "failed",
                    "reason": str(error),
                }
            )
    return {"job_id": job_id, "results": results}


def _parse_assignment(value: str, label: str) -> tuple[str, str]:
    if "=" not in value:
        raise CliError(f"{label} must use ID=VALUE syntax")
    key, item = value.split("=", 1)
    if not key or not item:
        raise CliError(f"{label} must use non-empty ID=VALUE syntax")
    return key, item


def _parse_reviewer(value: str) -> tuple[str, str]:
    """Parse the common PROVIDER form and the advanced ID=PROVIDER form."""

    candidate = value.strip()
    if not candidate:
        raise CliError("--reviewer must name a provider")
    if "=" in candidate:
        reviewer_id, provider = _parse_assignment(candidate, "--reviewer")
        reviewer_id = reviewer_id.strip()
        provider = provider.strip().lower()
        if reviewer_id.lower() == provider:
            raise CliError(f"use --reviewer {provider} instead of {candidate}")
        return reviewer_id, provider
    provider = candidate.lower()
    return provider, provider


def _new_job_id() -> str:
    return f"job_{uuid.uuid4().hex}"


def _create_job(args: argparse.Namespace) -> tuple[ReviewState, Any]:
    root = _root(args)
    owner_provider = args.owner.strip().lower()
    if not provider_available(owner_provider):
        raise CliError(f"owner provider is unavailable: {owner_provider}")
    assignments = [_parse_reviewer(value) for value in args.reviewer]
    if not assignments:
        raise CliError("select at least one reviewer for a terminal room")
    reviewer_ids = [reviewer_id for reviewer_id, _ in assignments]
    if len({reviewer_id.casefold() for reviewer_id in reviewer_ids}) != len(reviewer_ids):
        raise CliError(
            "reviewer IDs must be unique even when letter case differs; "
            "use ID=PROVIDER for repeated providers"
        )
    models = dict(_parse_assignment(value, "--reviewer-model") for value in args.reviewer_model)
    unknown_models = sorted(set(models).difference(reviewer_ids))
    if unknown_models:
        raise CliError(f"reviewer model has no matching reviewer: {', '.join(unknown_models)}")
    if owner_provider == "kimi" and _model(args.owner_model) is not None:
        raise CliError("Kimi owner model override is not supported")
    kimi_model_ids = [
        reviewer_id
        for reviewer_id, provider in assignments
        if provider == "kimi" and _model(models.get(reviewer_id, "default")) is not None
    ]
    if kimi_model_ids:
        raise CliError(
            "Kimi reviewer model override is not supported: "
            + ", ".join(kimi_model_ids)
        )
    capabilities = provider_capabilities()
    unknown_providers = sorted(
        {provider for _, provider in assignments}.difference(capabilities)
    )
    if unknown_providers:
        raise CliError(
            f"reviewer provider is not built in: {', '.join(unknown_providers)}"
        )

    state = ReviewState(root)
    job_id = args.job_id or _new_job_id()
    owner_cwd = Path(args.cwd).expanduser().resolve()
    if not owner_cwd.is_dir():
        raise CliError("owner cwd must be an existing directory")
    explicit_reviewer_base = (
        Path(args.reviewer_cwd).expanduser().resolve() if args.reviewer_cwd else None
    )
    if explicit_reviewer_base is not None and not explicit_reviewer_base.is_dir():
        raise CliError("reviewer cwd base must be an existing directory")
    reviewer_base = explicit_reviewer_base or (root.parent / "review-workspaces")
    reviewer_base = reviewer_base.resolve()
    control_root = root.resolve(strict=True)
    if (
        reviewer_base == control_root
        or reviewer_base in control_root.parents
        or control_root in reviewer_base.parents
    ):
        raise CliError("reviewer workspace base must be separate from review state")
    if (
        reviewer_base == owner_cwd
        or reviewer_base in owner_cwd.parents
        or owner_cwd in reviewer_base.parents
    ):
        raise CliError("reviewer workspace base must not contain the owner cwd")
    if explicit_reviewer_base is None:
        reviewer_base.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(reviewer_base, 0o700)

    specs = []
    for reviewer_id, provider in assignments:
        provider = provider.strip().lower()
        workspace = reviewer_base / job_id / reviewer_id
        specs.append(
            ReviewerSpec(
                reviewer_id=reviewer_id,
                provider=provider,
                model=models.get(reviewer_id, "default"),
                cwd=str(workspace),
                capability=capabilities.get(
                    provider,
                    {
                        "builtin": False,
                        "available": False,
                        "provider_native_sessions": False,
                    },
                ),
            )
        )
    created = state.create_job(
        owner_provider=owner_provider,
        owner_model=args.owner_model,
        owner_cwd=str(owner_cwd),
        reviewers=specs,
        job_id=job_id,
        max_rounds=3,
    )
    for reviewer in created.reviewers:
        workspace = Path(reviewer.cwd)
        if workspace.exists() and (workspace.is_symlink() or not workspace.is_dir()):
            raise CliError("reviewer workspace is unavailable")
        workspace.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(workspace, 0o700)
    for reviewer in created.reviewers:
        if not provider_available(reviewer.provider):
            state.mark_unavailable(
                job_id,
                reviewer.reviewer_id,
                "provider executable is unavailable",
            )
        elif not reviewer_isolation_available():
            state.mark_unavailable(
                job_id,
                reviewer.reviewer_id,
                "reviewer filesystem isolation is unavailable",
            )
    return state, created


def _module_command(root: Path, *parts: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "mutual_review_room.cli",
        "--root",
        str(root),
        *parts,
    ]


def _room_commands(
    state: ReviewState, job_id: str
) -> tuple[list[str], list[list[str]], list[str], list[str]]:
    owner = _module_command(state.root, "owner", "--job", job_id)
    broker = _module_command(state.root, "broker", "--job", job_id)
    reviewers = state.list_reviewers(job_id)
    observers = [
        _module_command(
            state.root,
            "observe",
            "--job",
            job_id,
            "--reviewer",
            reviewer.reviewer_id,
        )
        for reviewer in reviewers
    ]
    titles = [f"reviewer {reviewer.reviewer_id}" for reviewer in reviewers]
    return owner, observers, titles, broker


def _create_room(
    state: ReviewState,
    job_id: str,
    *,
    attach: bool,
    replace: bool,
) -> dict[str, Any]:
    prepare_mailbox(state.root, job_id)
    owner, observers, titles, broker = _room_commands(state, job_id)
    return TmuxRoom(job_id).create(
        owner,
        observers,
        broker_argv=broker,
        observer_titles=titles,
        attach=attach,
        replace=replace,
    )


def _owner_prompt(state: ReviewState, job_id: str) -> str:
    reviewers = ", ".join(item.reviewer_id for item in state.list_reviewers(job_id))
    command = f"{shlex.quote(sys.executable)} -m mutual_review_room.cli --root {shlex.quote(str(state.root))}"
    return (
        "You are the owner of a mutual-review terminal room. The human talks only to you in this "
        "left pane. The right panes are read-only projections; never ask the human to operate them. "
        f"The review job is {job_id}; selected reviewer IDs are: {reviewers}. "
        "Reviewers are isolated: they have no tools, no file access, and cannot read any path "
        "you name, including the material you are reviewing. Every prompt file you send must "
        "therefore carry the complete material inline. A prompt that points at a path gives "
        "that reviewer nothing to review. "
        "For round 1, inspect the brief, write one complete task envelope with Goal, Scope, "
        "Constraints, Done when, and Current materials, then send the exact same file to every "
        f"reviewer with: {command} dispatch-all --job {job_id} --round 1 --prompt-file PATH. "
        "For rounds 2 and 3, adjudicate each finding and send reviewer-specific follow-ups with "
        f"{command} dispatch --job {job_id} --reviewer REVIEWER_ID --round N --prompt-file PATH. "
        "Never share one reviewer's full answer with another; quote only the finding needed for "
        "a directed challenge. Stop after round 3 and report unresolved disagreement explicitly. "
        "After reporting either convergence or unresolved disagreement, write the complete final "
        "synthesis (including unresolved disagreements) to a verdict file and persist it with: "
        f"{command} complete --job {job_id} --verdict-file PATH. "
        "Do not reveal or copy the owner token."
    )


def _owner_environment(state: ReviewState, job_id: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("MRR_OWNER_TOKEN_FILE", None)
    token = (state.root / job_id / "owner.token").read_text(encoding="utf-8").strip()
    if not state.verify_owner_token(job_id, token):
        raise CliError("owner control token is invalid")
    environment.update(
        {
            "MRR_JOB_ID": job_id,
            "MRR_OWNER_TOKEN": token,
            "MRR_REVIEWER_COUNT": str(len(state.list_reviewers(job_id))),
            "MRR_ROOT": str(state.root),
        }
    )
    source_root = str(Path(__file__).resolve().parents[1])
    current = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source_root if not current else f"{source_root}{os.pathsep}{current}"
    return environment


def _raise_nofile_for_owner() -> None:
    """Give the long-lived native owner enough descriptors before exec."""

    soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    target_limit = max(soft_limit, 4096)
    if hard_limit != resource.RLIM_INFINITY:
        target_limit = min(target_limit, hard_limit)
    if target_limit > soft_limit:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target_limit, hard_limit))


def _codex_owner_argv(
    executable: str,
    owner_cwd: str,
    session_id: str,
    model: str | None,
    *,
    mailbox_inbox: str | None = None,
) -> list[str]:
    """Build the interactive owner command with bounded, automatic approvals."""

    project_trust = (
        f"projects.{json.dumps(owner_cwd, ensure_ascii=False)}.trust_level=\"trusted\""
    )
    argv = [executable, "--config", project_trust]
    argv.extend(
        [
            "resume",
            "--include-non-interactive",
            "-C",
            owner_cwd,
            "--approve-for-me",
            "--no-alt-screen",
        ]
    )
    if mailbox_inbox:
        argv.extend(["--add-dir", mailbox_inbox])
    if model:
        argv.extend(["--model", model])
    argv.append(session_id)
    return argv


def _exec_owner(state: ReviewState, job_id: str) -> None:
    job = state.get_job(job_id)
    executable = shutil.which(job.owner_provider)
    if executable is None:
        state.fail_job(job_id, "owner provider executable is unavailable")
        raise CliError("owner provider executable is unavailable")
    prompt = _owner_prompt(state, job_id)
    environment = _owner_environment(state, job_id)
    model = _model(job.owner_model)
    _raise_nofile_for_owner()

    if job.owner_provider == "claude":
        session_id = job.owner_session_id
        if session_id is None:
            result = invoke(
                "claude",
                prompt + " Reply only that the owner control contract is loaded.",
                cwd=job.owner_cwd,
                model=model,
                timeout=300,
                isolated_reviewer=False,
            )
            session_id = result.session_id
            state.set_owner_session(job_id, session_id)
        argv = [executable]
        argv.extend(["--resume", session_id])
        argv.extend(
            [
                "--name",
                f"mutual-review-{job_id[:12]}",
                "--append-system-prompt",
                prompt,
                "--strict-mcp-config",
                "--no-chrome",
            ]
        )
        if model:
            argv.extend(["--model", model])
        os.chdir(job.owner_cwd)
        os.execvpe(executable, argv, environment)

    if job.owner_provider == "codex":
        session_id = job.owner_session_id
        if session_id is None:
            result = invoke(
                "codex",
                prompt + " Reply only that the owner control contract is loaded.",
                cwd=job.owner_cwd,
                model=model,
                timeout=300,
                isolated_reviewer=False,
                writable_dirs=[mailbox_paths(state.root, job_id).inbox],
            )
            session_id = result.session_id
            state.set_owner_session(job_id, session_id)
        argv = _codex_owner_argv(
            executable,
            job.owner_cwd,
            session_id,
            model,
            mailbox_inbox=str(mailbox_paths(state.root, job_id).inbox),
        )
        os.chdir(job.owner_cwd)
        os.execvpe(executable, argv, environment)

    if job.owner_provider == "kimi":
        if model is not None:
            raise CliError("Kimi owner model override is not supported")
        session_id = job.owner_session_id
        if session_id is None:
            result = invoke(
                "kimi",
                prompt + " Reply only that the owner control contract is loaded.",
                cwd=job.owner_cwd,
                timeout=300,
                isolated_reviewer=False,
            )
            session_id = result.session_id
            state.set_owner_session(job_id, session_id)
        skills_dir = state.root / job_id / "owner-skills"
        if skills_dir.exists() and (skills_dir.is_symlink() or not skills_dir.is_dir()):
            raise CliError("isolated owner skills directory is unavailable")
        skills_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(skills_dir, 0o700)
        argv = [
            executable,
            "--session",
            session_id,
            "--skills-dir",
            str(skills_dir),
        ]
        os.chdir(job.owner_cwd)
        os.execvpe(executable, argv, environment)

    raise CliError(f"owner provider has no interactive adapter: {job.owner_provider}")


def _broker_payload(
    payload: Any,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("broker payload must be an object")
    optional = optional or set()
    missing = required.difference(payload)
    unknown = set(payload).difference(required | optional)
    if missing or unknown:
        raise ValidationError("broker payload fields do not match the action")
    return payload


def _broker_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > 1_000_000:
        raise ValidationError(f"{field} is invalid")
    if not allow_empty and not value.strip():
        raise ValidationError(f"{field} must not be empty")
    return value


def _broker_round(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 3:
        raise ValidationError("round must be an integer from 1 through 3")
    return value


def _broker_timeout(value: Any) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not 0 < float(value) <= 3600
    ):
        raise ValidationError("timeout must be between 0 and 3600 seconds")
    return float(value)


def _optional_broker_id(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _broker_text(value, field)


def _run_broker_action(
    state: ReviewState,
    job_id: str,
    action: str,
    payload: Mapping[str, Any],
    _control_request_id: str,
) -> Mapping[str, Any]:
    """Validate one signed command and execute it in the native broker."""

    try:
        if action == "dispatch":
            values = _broker_payload(
                dict(payload),
                required={"reviewer_id", "round", "prompt", "timeout"},
                optional={"parent_request_id", "request_id"},
            )
            reviewer_id = _broker_text(values["reviewer_id"], "reviewer_id")
            round_no = _broker_round(values["round"])
            result = dispatch_one(
                state,
                job_id,
                reviewer_id,
                round_no=round_no,
                prompt=_broker_text(values["prompt"], "prompt"),
                parent_request_id=_optional_broker_id(
                    values.get("parent_request_id"), "parent_request_id"
                ),
                request_id=(
                    _optional_broker_id(values.get("request_id"), "request_id")
                    or _round_request_id(job_id, reviewer_id, round_no)
                ),
                timeout=_broker_timeout(values["timeout"]),
            )
            return {
                "exit_code": 0 if result["status"] == "completed" else 3,
                "output": result,
            }

        if action == "dispatch_all":
            values = _broker_payload(
                dict(payload),
                required={"round", "prompt", "timeout"},
            )
            result = _dispatch_all_local(
                state,
                job_id,
                round_no=_broker_round(values["round"]),
                prompt=_broker_text(values["prompt"], "prompt"),
                timeout=_broker_timeout(values["timeout"]),
            )
            return {
                "exit_code": (
                    0
                    if any(item["status"] == "completed" for item in result["results"])
                    else 3
                ),
                "output": result,
            }

        if action == "complete":
            values = _broker_payload(dict(payload), required={"verdict"})
            job = state.complete_job(
                job_id,
                _broker_text(values["verdict"], "verdict"),
            )
            completion = state.list_events(job_id, event_type="job_completed")[-1]
            return {
                "exit_code": 0,
                "output": {
                    "job": _jsonable(job),
                    "completion_event": _jsonable(completion),
                },
            }

        if action == "recover":
            _broker_payload(dict(payload), required=set())
            recovered = state.recover_job(job_id)
            return {
                "exit_code": 0,
                "output": {
                    "job_id": job_id,
                    "interrupted": _jsonable(recovered),
                    "replayed": False,
                },
            }

        raise ValidationError(f"unsupported broker action: {action}")
    except (CliError, ReviewStateError, OSError, ValueError) as error:
        return {"exit_code": 2, "error": str(error)}


def _broker_wait_timeout(provider_timeout: float, *, fan_out: bool) -> float:
    if not fan_out:
        return provider_timeout + 30.0
    raw_count = os.environ.get("MRR_REVIEWER_COUNT", "16")
    try:
        reviewer_count = int(raw_count)
    except ValueError:
        reviewer_count = 16
    reviewer_count = min(max(reviewer_count, 1), 10_000)
    return min(provider_timeout * reviewer_count + 30.0, 86_400.0)


def _submit_control(
    args: argparse.Namespace,
    *,
    action: str,
    payload: Mapping[str, Any],
    wait_timeout: float,
) -> tuple[int, Mapping[str, Any] | None, str | None]:
    root = _root(args)
    job_id = _job_id(args)
    token = _client_token(root, job_id, getattr(args, "token_file", None))
    paths = mailbox_paths(root, job_id)
    control_id = submit_broker_request(paths, token, action, payload)
    response: BrokerResponse = wait_for_broker(
        paths,
        token,
        control_id,
        timeout=wait_timeout,
        stale_after=5.0,
        startup_grace=3.0,
    )
    if response.status != "ok" or response.result is None:
        message = (
            response.error.get("message", "broker request failed")
            if response.error
            else "broker request failed"
        )
        return 2, None, message
    exit_code = response.result.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise CliError("broker returned an invalid exit code")
    output = response.result.get("output")
    error = response.result.get("error")
    if output is not None and not isinstance(output, Mapping):
        raise CliError("broker returned an invalid output")
    if error is not None and not isinstance(error, str):
        raise CliError("broker returned an invalid error")
    return exit_code, output, error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Owner-led mutual-review terminal rooms")
    parser.add_argument("--root", type=Path, help="review state root")
    commands = parser.add_subparsers(dest="command", required=True)

    providers = commands.add_parser("providers", help="show built-in provider capabilities")
    providers.set_defaults(handler=_cmd_providers)

    launch = commands.add_parser("launch", help="create a job and launch its tmux room")
    launch.add_argument("--owner", required=True, help="owner provider: claude, codex, or kimi")
    launch.add_argument("--owner-model", default="default")
    launch.add_argument(
        "--reviewer",
        action="append",
        required=True,
        metavar="PROVIDER",
        help=(
            "selected reviewer provider; repeat as needed. "
            "Use ID=PROVIDER only for repeated providers"
        ),
    )
    launch.add_argument("--reviewer-model", action="append", default=[], metavar="ID=MODEL")
    launch.add_argument("--cwd", default=os.getcwd(), help="owner working directory")
    launch.add_argument(
        "--reviewer-cwd",
        help="private workspace base; each reviewer still receives a separate job/lane directory",
    )
    launch.add_argument("--job-id")
    launch.add_argument("--no-attach", action="store_true")
    launch.set_defaults(handler=_cmd_launch)

    room = commands.add_parser("room", help="open or explicitly rebuild an existing room")
    room.add_argument("--job")
    room.add_argument("--replace", action="store_true")
    room.add_argument("--no-attach", action="store_true")
    room.set_defaults(handler=_cmd_room)

    owner = commands.add_parser("owner", help=argparse.SUPPRESS)
    owner.add_argument("--job")
    owner.set_defaults(handler=_cmd_owner)

    broker = commands.add_parser("broker", help=argparse.SUPPRESS)
    broker.add_argument("--job")
    broker.set_defaults(handler=_cmd_broker)

    observe = commands.add_parser("observe", help="render one reviewer lane read-only")
    observe.add_argument("--job")
    observe.add_argument("--reviewer", required=True)
    observe.add_argument("--once", action="store_true")
    observe.add_argument("--interval", type=float, default=0.5)
    observe.set_defaults(handler=_cmd_observe)

    dispatch = commands.add_parser("dispatch", help="send one owner request")
    dispatch.add_argument("--job")
    dispatch.add_argument("--reviewer", required=True)
    dispatch.add_argument("--round", type=int, required=True, dest="round_no")
    dispatch.add_argument("--parent")
    dispatch.add_argument("--request-id")
    dispatch.add_argument("--prompt-file")
    dispatch.add_argument("--timeout", type=float, default=300.0)
    dispatch.add_argument("--token-file")
    dispatch.set_defaults(handler=_cmd_dispatch)

    dispatch_all = commands.add_parser("dispatch-all", help="send one identical owner request to all lanes")
    dispatch_all.add_argument("--job")
    dispatch_all.add_argument("--round", type=int, required=True, dest="round_no")
    dispatch_all.add_argument("--prompt-file")
    dispatch_all.add_argument("--timeout", type=float, default=300.0)
    dispatch_all.add_argument("--token-file")
    dispatch_all.set_defaults(handler=_cmd_dispatch_all)

    complete = commands.add_parser("complete", help="mark an adjudicated review job complete")
    complete.add_argument("--job")
    complete.add_argument("--verdict-file", required=True)
    complete.add_argument("--token-file")
    complete.set_defaults(handler=_cmd_complete)

    status = commands.add_parser("status", help="show durable job/session state")
    status.add_argument("--job")
    status.set_defaults(handler=_cmd_status)

    recover = commands.add_parser("recover", help="mark orphaned running requests interrupted")
    recover.add_argument("--job")
    recover.add_argument("--token-file")
    recover.set_defaults(handler=_cmd_recover)
    return parser


def _cmd_providers(args: argparse.Namespace) -> int:
    _print_json(provider_capabilities())
    return 0


def _cmd_launch(args: argparse.Namespace) -> int:
    state, created = _create_job(args)
    summary = {
        "job": created.job,
        "reviewers": state.list_reviewers(created.job.job_id),
        "token_file": created.token_path,
    }
    _print_json(summary)
    _create_room(
        state,
        created.job.job_id,
        attach=not args.no_attach,
        replace=False,
    )
    return 0


def _cmd_room(args: argparse.Namespace) -> int:
    state = ReviewState(_root(args))
    job_id = _job_id(args)
    state.get_job(job_id)
    description = _create_room(
        state,
        job_id,
        attach=not args.no_attach,
        replace=args.replace,
    )
    if args.no_attach:
        _print_json(description)
    return 0


def _cmd_owner(args: argparse.Namespace) -> int:
    _exec_owner(ReviewState(_root(args)), _job_id(args))
    return 0


def _cmd_broker(args: argparse.Namespace) -> int:
    state = ReviewState(_root(args))
    job_id = _job_id(args)
    state.get_job(job_id)
    token_path = state.root / job_id / "owner.token"
    if token_path.is_symlink() or not token_path.is_file():
        raise CliError("owner control token file is unavailable")
    token = token_path.read_text(encoding="utf-8").strip()
    if not state.verify_owner_token(job_id, token):
        raise CliError("owner control token is invalid")
    serve_forever(
        prepare_mailbox(state.root, job_id),
        token,
        lambda action, payload, request_id: _run_broker_action(
            state,
            job_id,
            action,
            payload,
            request_id,
        ),
    )
    return 0


def _cmd_observe(args: argparse.Namespace) -> int:
    observe_reviewer(
        _root(args),
        _job_id(args),
        args.reviewer,
        once=args.once,
        interval=args.interval,
    )
    return 0


def _cmd_dispatch(args: argparse.Namespace) -> int:
    exit_code, output, error = _submit_control(
        args,
        action="dispatch",
        payload={
            "reviewer_id": args.reviewer,
            "round": args.round_no,
            "prompt": _read_prompt(args),
            "parent_request_id": args.parent,
            "request_id": args.request_id,
            "timeout": args.timeout,
        },
        wait_timeout=_broker_wait_timeout(args.timeout, fan_out=False),
    )
    if output is not None:
        _print_json(output)
    if error:
        print(f"mutual-review-room: {error}", file=sys.stderr)
    return exit_code


def _cmd_dispatch_all(args: argparse.Namespace) -> int:
    exit_code, output, error = _submit_control(
        args,
        action="dispatch_all",
        payload={
            "round": args.round_no,
            "prompt": _read_prompt(args),
            "timeout": args.timeout,
        },
        wait_timeout=_broker_wait_timeout(args.timeout, fan_out=True),
    )
    if output is not None:
        _print_json(output)
    if error:
        print(f"mutual-review-room: {error}", file=sys.stderr)
    return exit_code


def _cmd_complete(args: argparse.Namespace) -> int:
    exit_code, output, error = _submit_control(
        args,
        action="complete",
        payload={"verdict": _read_verdict(args)},
        wait_timeout=30.0,
    )
    if output is not None:
        _print_json(output)
    if error:
        print(f"mutual-review-room: {error}", file=sys.stderr)
    return exit_code


def _cmd_status(args: argparse.Namespace) -> int:
    state = ReviewState(_root(args))
    job_id = _job_id(args)
    snapshot = state.open_job(job_id)
    _print_json(
        {
            "job": snapshot.job,
            "reviewers": snapshot.reviewers,
            "requests": snapshot.requests,
            "event_count": len(state.list_events(job_id, limit=10_000)),
            "tmux": TmuxRoom(job_id).describe(),
        }
    )
    return 0


def _cmd_recover(args: argparse.Namespace) -> int:
    """Recover through the live broker, falling back to a direct transition.

    Recovery writes to the job database, which a sandboxed owner cannot do
    itself.  When a broker is serving this job the request goes through it, so
    the transition also stays serialized behind the job lock.  Without a live
    broker there is no sandbox to respect and the owner recovers directly.
    """

    try:
        exit_code, output, error = _submit_control(
            args,
            action="recover",
            payload={},
            wait_timeout=30.0,
        )
    except (BrokerUnavailable, MailboxSecurityError, CliError):
        state = ReviewState(_root(args))
        job_id = _job_id(args)
        _authorize(state, job_id, args.token_file)
        recovered = state.recover_job(job_id)
        _print_json({"job_id": job_id, "interrupted": recovered, "replayed": False})
        return 0
    if output is not None:
        _print_json(output)
    if error:
        print(f"mutual-review-room: {error}", file=sys.stderr)
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        BrokerError,
        CliError,
        ReviewStateError,
        TmuxRoomError,
        OSError,
        ValueError,
    ) as error:
        print(f"mutual-review-room: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
