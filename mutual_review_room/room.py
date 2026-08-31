"""Read-only mutual-review projection and tmux room layout.

The durable review state is the source of truth.  This module deliberately does
not use terminal input, pane capture, or ``tmux send-keys`` as a message bus.
Observer panes only render the user-visible projection of one reviewer lane.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, TextIO


_MISSING = object()
_PROMPT_TYPES = {
    "owner_prompt",
    "prompt",
    "request",
    "review_request",
    "followup",
    "follow_up",
    "request_started",
}
_FINAL_TYPES = {
    "reviewer_final",
    "final",
    "answer",
    "response",
    "final_response",
    "response_completed",
}
_STATUS_TYPES = {
    "status",
    "reviewer_status",
    "failed",
    "failure",
    "error",
    "cancelled",
    "canceled",
    "timeout",
    "request_failed",
    "request_interrupted",
    "reviewer_unavailable",
    "request_queued",
    "provider_answering",
    "job_completed",
    "job_failed",
}
_INTERNAL_TYPES = {
    "reasoning",
    "thinking",
    "provider_raw",
    "raw",
    "tool",
    "tool_call",
    "tool_output",
    "stderr",
    "stdout",
    "delta",
}


def _read(record: Any, *names: str, default: Any = None) -> Any:
    """Read a field from either a mapping or a small state-model object."""

    for name in names:
        if isinstance(record, Mapping):
            value = record.get(name, _MISSING)
        else:
            value = getattr(record, name, _MISSING)
        if value is not _MISSING and value is not None:
            return value
    return default


def _normal(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _display(value: Any, fallback: str = "unknown") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def _local_timestamp(value: Any) -> str:
    """Return a stable local-time representation without changing source state."""

    if value is None or value == "":
        return "unknown"
    parsed: Optional[datetime] = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            parsed = None
    elif isinstance(value, str):
        candidate = value.strip()
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            parsed = None
    if parsed is None:
        return _display(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().isoformat(timespec="seconds")


def _event_id(event: Any) -> Any:
    # The durable state cursor is the monotonic SQLite sequence.  UUID-like
    # event IDs are identity, not ordering cursors.
    return _read(event, "sequence", "seq", "id", "event_id", "eventId", default=0)


def _event_sort_key(event: Any) -> tuple[Any, ...]:
    identifier = _event_id(event)
    try:
        return (0, int(identifier))
    except (TypeError, ValueError):
        return (
            1,
            str(_read(event, "created_at", "timestamp", "time", default="")),
            str(identifier),
        )


def _events_for(store: Any, reviewer_id: str, *, after_id: Any = 0) -> list[Any]:
    """Call the state API and defensively keep this reviewer lane isolated."""

    events = list(store.list_events(reviewer_id=reviewer_id, after_id=after_id))
    isolated = []
    target = str(reviewer_id)
    for event in events:
        event_reviewer = _read(event, "reviewer_id", "reviewerId", "lane_id", "laneId")
        if event_reviewer is not None and str(event_reviewer) != target:
            continue
        isolated.append(event)
    return sorted(isolated, key=_event_sort_key)


def _event_is_user_visible(event: Any) -> bool:
    if bool(_read(event, "internal", "is_internal", default=False)):
        return False
    visible = _read(event, "user_visible", "userVisible", "visible")
    if visible is False or visible == 0:
        return False
    visibility = _normal(_read(event, "visibility", default=""))
    return visibility not in {"internal", "private", "provider", "debug", "raw"}


def _event_kind(event: Any) -> str:
    return _normal(_read(event, "type", "event_type", "eventType", "kind"))


def _event_role(event: Any) -> str:
    return _normal(_read(event, "role", "actor", "source", "from_role", "fromRole"))


def _is_prompt(event: Any) -> bool:
    kind = _event_kind(event)
    role = _event_role(event)
    if kind in _PROMPT_TYPES:
        return True
    return kind in {"message", "user_message"} and role in {"owner", "user"}


def _is_final(event: Any) -> bool:
    kind = _event_kind(event)
    role = _event_role(event)
    if kind in _FINAL_TYPES:
        return True
    final_flag = bool(_read(event, "final", "is_final", "isFinal", default=False))
    return kind in {"message", "assistant_message"} and role in {"reviewer", "assistant"} and final_flag


def _is_status(event: Any) -> bool:
    return _event_kind(event) in _STATUS_TYPES


def _event_text(event: Any, category: str) -> str:
    # Field allowlists are intentional: never fall back to repr(event), payload,
    # provider output, reasoning, or tool data.
    if category == "prompt":
        names = ("prompt", "content", "text", "message", "user_visible_text")
    elif category == "final":
        names = ("final_text", "answer", "content", "text", "message", "user_visible_text")
    else:
        names = (
            "failure_message",
            "failure",
            "message",
            "error",
            "status_message",
            "content",
        )
    value = _read(event, *names, default="")
    if isinstance(value, str):
        return value
    # State should persist visible text as text.  Refuse to serialize arbitrary
    # objects because they may contain raw provider or tool payloads.
    return ""


def _event_round(event: Any, reviewer: Any, job: Any) -> str:
    fallback_round = _read(
        reviewer,
        "round",
        "current_round",
        default=_read(job, "round", "current_round"),
    )
    return _display(
        _read(
            event,
            "round",
            "round_number",
            "roundNumber",
            default=fallback_round,
        )
    )


def _event_timestamp(event: Any) -> str:
    return _local_timestamp(_read(event, "created_at", "timestamp", "time", "updated_at"))


def _latest_timestamp(
    job: Any,
    reviewer: Any,
    events: Sequence[Any],
) -> Any:
    if events:
        value = _read(events[-1], "updated_at", "created_at", "timestamp", "time")
        if value is not None:
            return value
    return _read(reviewer, "updated_at", "created_at", default=_read(job, "updated_at", "created_at"))


def _get_reviewer(store: Any, reviewer_id: str) -> Any:
    """Small compatibility shim for state implementations with list-only lookup."""

    getter = getattr(store, "get_reviewer", None)
    if getter is not None:
        reviewer = getter(reviewer_id)
        if reviewer is not None:
            return reviewer
    for reviewer in store.list_reviewers():
        candidate = _read(reviewer, "reviewer_id", "id")
        if candidate is not None and str(candidate) == str(reviewer_id):
            return reviewer
    raise KeyError(f"unknown reviewer: {reviewer_id}")


def _visible_event_lines(
    events: Sequence[Any],
    reviewer: Any,
    job: Any,
) -> tuple[list[str], int]:
    """Render only the durable event fields exposed in the observer projection."""

    lines: list[str] = []
    visible_count = 0
    for event in events:
        kind = _event_kind(event)
        if kind in _INTERNAL_TYPES or not _event_is_user_visible(event):
            continue
        if _is_prompt(event):
            category = "prompt"
            label = "OWNER → REVIEWER"
        elif _is_final(event):
            category = "final"
            label = "REVIEWER → OWNER · FINAL"
        elif _is_status(event):
            category = "status"
            status = _display(_read(event, "status", default=kind))
            label = f"STATUS · {status}"
        else:
            continue
        text = _event_text(event, category)
        if not text and category != "status":
            continue
        visible_count += 1
        lines.extend(
            [
                "",
                f"[{_event_timestamp(event)} · R{_event_round(event, reviewer, job)}] {label}",
                text if text else "(no user-visible detail)",
            ]
        )
    return lines, visible_count


def render_reviewer(
    store: Any,
    reviewer_id: str,
    *,
    events: Optional[Sequence[Any]] = None,
) -> str:
    """Render one complete, deterministic, user-visible reviewer transcript."""

    job = store.get_job()
    reviewer = _get_reviewer(store, reviewer_id)
    snapshot = (
        _events_for(store, reviewer_id, after_id=0)
        if events is None
        else list(events)
    )

    job_id = _read(job, "job_id", "id")
    stable_reviewer_id = _read(reviewer, "reviewer_id", "id", default=reviewer_id)
    provider = _read(reviewer, "provider", "provider_name")
    model = _read(reviewer, "model", "model_name")
    session_id = _read(
        reviewer,
        "native_session_id",
        "nativeSessionId",
        "provider_session_id",
        "providerSessionId",
        "session_id",
    )
    reviewer_status = _read(reviewer, "status", default="unknown")
    if snapshot and _event_kind(snapshot[-1]) == "request_queued" and reviewer_status == "ready":
        reviewer_status = "queued (provider not started)"
    job_status = _read(job, "status", default="unknown")
    round_number = _read(
        reviewer,
        "round",
        "current_round",
        default=_read(job, "round", "current_round"),
    )

    lines = [
        "MUTUAL REVIEW · OBSERVER (READ ONLY)",
        f"job: {_display(job_id)}",
        f"reviewer: {_display(stable_reviewer_id)}",
        f"provider: {_display(provider)}",
        f"model: {_display(model)}",
        f"native session: {_display(session_id, fallback='not established')}",
        f"job status: {_display(job_status)}",
        f"reviewer status: {_display(reviewer_status)}",
        f"round: {_display(round_number)}",
        f"updated (local): {_local_timestamp(_latest_timestamp(job, reviewer, snapshot))}",
        "scroll: mouse wheel",
        "",
        "VISIBLE TRANSCRIPT",
    ]

    event_lines, visible_count = _visible_event_lines(snapshot, reviewer, job)
    lines.extend(event_lines)

    if visible_count == 0:
        lines.extend(["", "(no user-visible events yet)"])
    return "\n".join(lines).rstrip() + "\n"


def _render_reviewer_update(
    store: Any,
    reviewer_id: str,
    events: Sequence[Any],
) -> str:
    """Append new visible events without clearing or redrawing scrollback."""

    job = store.get_job()
    reviewer = _get_reviewer(store, reviewer_id)
    event_lines, visible_count = _visible_event_lines(events, reviewer, job)
    if visible_count == 0:
        return ""
    latest = _latest_timestamp(job, reviewer, events)
    lines = [
        "",
        (
            "LIVE UPDATE · "
            f"job {_display(_read(job, 'status', default='unknown'))} · "
            f"reviewer {_display(_read(reviewer, 'status', default='unknown'))} · "
            f"{_local_timestamp(latest)}"
        ),
    ]
    lines.extend(event_lines)
    return "\n".join(lines).rstrip() + "\n"


class _ReviewStateView:
    """Bind ReviewState's explicit job_id API for one observer process."""

    def __init__(self, root: os.PathLike[str] | str, job_id: str) -> None:
        from .state import ReviewState

        self._state = ReviewState(root)
        self._job_id = job_id

    def get_job(self) -> Any:
        return self._state.get_job(self._job_id)

    def list_reviewers(self) -> list[Any]:
        return self._state.list_reviewers(self._job_id)

    def get_reviewer(self, reviewer_id: str) -> Any:
        return self._state.get_reviewer(self._job_id, reviewer_id)

    def list_events(self, *, reviewer_id: str, after_id: Any = 0) -> list[Any]:
        return self._state.list_events(
            self._job_id,
            reviewer_id=reviewer_id,
            include_global=True,
            after=int(after_id or 0),
        )


def _open_store(root: os.PathLike[str] | str, job_id: str) -> Any:
    return _ReviewStateView(root, job_id)


def _cursor_from(events: Sequence[Any], fallback: Any = 0) -> Any:
    if not events:
        return fallback
    return _event_id(events[-1])


def observe_reviewer(
    root: os.PathLike[str] | str,
    job_id: str,
    reviewer_id: str,
    *,
    once: bool = False,
    interval: float = 0.5,
    output: Optional[TextIO] = None,
    store_factory: Optional[Callable[[os.PathLike[str] | str, str], Any]] = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Project one reviewer lane; this loop intentionally never reads stdin."""

    if interval <= 0:
        raise ValueError("interval must be positive")
    stream = output if output is not None else sys.stdout
    factory = store_factory or _open_store
    store = factory(root, job_id)

    full_events = _events_for(store, reviewer_id, after_id=0)
    text = render_reviewer(store, reviewer_id, events=full_events)
    if once:
        stream.write(text)
        stream.flush()
        return

    stream.write(text)
    stream.flush()
    cursor = _cursor_from(full_events)
    while True:
        changed = _events_for(store, reviewer_id, after_id=cursor)
        if not changed:
            sleeper(interval)
            continue
        cursor = _cursor_from(changed, fallback=cursor)
        update = _render_reviewer_update(store, reviewer_id, changed)
        if update:
            stream.write(update)
            stream.flush()


class TmuxRoomError(RuntimeError):
    """Base error for tmux room operations."""


class TmuxUnavailableError(TmuxRoomError):
    """Raised when tmux is not installed."""


class RoomExistsError(TmuxRoomError):
    """Raised when creation would replace a room without explicit consent."""


@dataclass(frozen=True)
class PaneDescription:
    pane_id: str
    title: str
    input_off: bool
    active: bool
    command: str


Runner = Callable[..., Any]


def session_name_for_job(job_id: str) -> str:
    """Derive a stable, collision-resistant tmux session name from a job ID."""

    raw = str(job_id).strip()
    if not raw:
        raise ValueError("job_id must not be empty")
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-") or "job"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"review-{slug[:28]}-{digest}"


def _validated_argv(argv: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)) or not argv:
        raise ValueError(f"{label} must be a non-empty argv sequence")
    result = tuple(str(part) for part in argv)
    if any(not part or "\x00" in part for part in result):
        raise ValueError(f"{label} contains an empty or NUL argument")
    return result


class TmuxRoom:
    """Create and inspect the terminal projection for one isolated review job."""

    def __init__(
        self,
        job_id: str,
        *,
        runner: Optional[Runner] = None,
        tmux_bin: Optional[str] = None,
    ) -> None:
        self.job_id = str(job_id)
        self.session_name = session_name_for_job(self.job_id)
        socket_digest = hashlib.sha256(self.job_id.encode("utf-8")).hexdigest()[:16]
        self.socket_name = f"mrr-{socket_digest}"
        self._runner = runner or subprocess.run
        if tmux_bin is not None:
            self.tmux_bin = tmux_bin
        elif runner is not None:
            # A fake/injected runner should not depend on the host installation.
            self.tmux_bin = "tmux"
        else:
            resolved = shutil.which("tmux")
            if resolved is None:
                raise TmuxUnavailableError("tmux is required to create a review room")
            self.tmux_bin = resolved

    def _run(
        self,
        args: Sequence[str],
        *,
        allow_missing: bool = False,
        interactive: bool = False,
    ) -> Any:
        # A room gets its own tmux server.  This keeps server-level keyboard
        # options and the deterministic config from changing the user's other
        # tmux sessions.
        argv = [self.tmux_bin, "-L", self.socket_name, "-f", "/dev/null", *args]
        try:
            kwargs = {"check": False, "text": True}
            if not interactive:
                kwargs["capture_output"] = True
            result = self._runner(argv, **kwargs)
        except FileNotFoundError as exc:
            raise TmuxUnavailableError(f"tmux executable not found: {self.tmux_bin}") from exc
        returncode = int(getattr(result, "returncode", 0))
        if returncode and not allow_missing:
            detail = str(getattr(result, "stderr", "")).strip()
            suffix = f": {detail}" if detail else ""
            command = str(args[0]) if args else "command"
            raise TmuxRoomError(f"tmux {command} failed ({returncode}){suffix}")
        return result

    def exists(self) -> bool:
        result = self._run(["has-session", "-t", f"={self.session_name}"], allow_missing=True)
        returncode = int(getattr(result, "returncode", 0))
        if returncode == 0:
            return True
        if returncode == 1:
            return False
        detail = str(getattr(result, "stderr", "")).strip()
        raise TmuxRoomError(f"could not inspect tmux session ({returncode}): {detail}")

    @staticmethod
    def _pane_id(result: Any, fallback: str) -> str:
        stdout = str(getattr(result, "stdout", ""))
        first = next((line.strip() for line in stdout.splitlines() if line.strip()), "")
        return first or fallback

    def create(
        self,
        owner_argv: Sequence[str],
        observer_argvs: Sequence[Sequence[str]],
        *,
        broker_argv: Optional[Sequence[str]] = None,
        observer_titles: Optional[Sequence[str]] = None,
        attach: bool = False,
        replace: bool = False,
    ) -> dict[str, Any]:
        owner = _validated_argv(owner_argv, "owner_argv")
        broker = (
            _validated_argv(broker_argv, "broker_argv")
            if broker_argv is not None
            else None
        )
        observers = [
            _validated_argv(argv, f"observer_argvs[{index}]")
            for index, argv in enumerate(observer_argvs)
        ]
        if not observers:
            raise ValueError("a review room requires at least one observer pane")
        if observer_titles is None:
            titles = [f"reviewer {index}" for index in range(1, len(observers) + 1)]
        else:
            titles = [str(title).strip() for title in observer_titles]
            if len(titles) != len(observers):
                raise ValueError("observer_titles must match observer_argvs")
            if any(not title or "\x00" in title for title in titles):
                raise ValueError("observer_titles contains an empty or NUL title")

        already_exists = self.exists()
        if already_exists and not replace:
            raise RoomExistsError(
                f"review room already exists: {self.session_name}; pass replace=True to rebuild it"
            )
        if already_exists:
            self._run(["kill-session", "-t", f"={self.session_name}"])

        # Keep the session alive while its layout is being built.  A resumed
        # owner can exit immediately when its native session is unavailable or
        # another startup check fails.  If the owner were the only initial
        # pane, tmux would destroy the session before the observer panes and
        # diagnostic surface existed.
        placeholder = [sys.executable, "-c", "import time; time.sleep(86400)"]
        created = self._run(
            [
                "new-session",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-s",
                self.session_name,
                "-n",
                "review-room",
                shlex.join(placeholder),
            ]
        )
        initial_pane = self._pane_id(created, f"={self.session_name}")
        try:
            # Kimi's interactive TUI submits multiline prompts with CSI-u key
            # sequences.  These are server options, but the server is dedicated
            # to this room, so no unrelated tmux session is changed.
            self._run(["set-option", "-g", "extended-keys", "on"])
            self._run(["set-option", "-g", "extended-keys-format", "csi-u"])
            self._run(["set-option", "-t", self.session_name, "mouse", "on"])
            self._run(["set-option", "-t", self.session_name, "pane-border-status", "top"])
            self._run(
                [
                    "set-option",
                    "-t",
                    self.session_name,
                    "pane-border-format",
                    " #{@mrr_title} ",
                ]
            )
            self._run(["set-option", "-t", self.session_name, "status-right-length", "72"])
            self._run(
                [
                    "set-option",
                    "-t",
                    self.session_name,
                    "status-right",
                    "scroll: mouse wheel",
                ]
            )

            # tmux allocates pane history when a pane is created.  The initial
            # placeholder predates this option, so replace it with a new pane
            # after raising the limit; every surviving pane then keeps the same
            # 100,000-line mouse-scroll history.
            self._run(
                [
                    "set-option",
                    "-w",
                    "-t",
                    initial_pane,
                    "history-limit",
                    "100000",
                ]
            )
            replacement = self._run(
                [
                    "split-window",
                    "-h",
                    "-P",
                    "-F",
                    "#{pane_id}",
                    "-t",
                    initial_pane,
                    shlex.join(placeholder),
                ]
            )
            owner_pane = self._pane_id(replacement, f"={self.session_name}")
            self._run(["kill-pane", "-t", initial_pane])
            self._run(["set-option", "-p", "-t", owner_pane, "@mrr_title", "owner"])
            self._run(
                ["set-option", "-p", "-t", owner_pane, "allow-set-title", "off"]
            )
            self._run(["select-pane", "-t", owner_pane, "-T", "owner"])

            observer_panes: list[str] = []
            split_target = owner_pane
            for index, observer in enumerate(observers):
                orientation = "-h" if index == 0 else "-v"
                created_pane = self._run(
                    [
                        "split-window",
                        orientation,
                        "-P",
                        "-F",
                        "#{pane_id}",
                        "-t",
                        split_target,
                        shlex.join(observer),
                    ]
                )
                pane_id = self._pane_id(created_pane, f"={self.session_name}")
                observer_panes.append(pane_id)
                split_target = pane_id
                # Secure each observer immediately.  If a later split cannot
                # fit, no partially built pane is briefly left writable.
                self._run(
                    ["set-option", "-p", "-t", pane_id, "@mrr_title", titles[index]]
                )
                self._run(
                    ["set-option", "-p", "-t", pane_id, "allow-set-title", "off"]
                )
                # tmux 3.7b accepts -d and -T together but only applies the
                # input flag.  Keep these as two calls, then restore the owner
                # selection after layout construction below.
                self._run(["select-pane", "-d", "-t", pane_id])
                self._run(["select-pane", "-t", pane_id, "-T", titles[index]])

            # Select the owner before applying main-vertical so it remains the
            # main pane, then set it to half the available width.
            self._run(["select-pane", "-t", owner_pane])
            self._run(["select-layout", "-t", owner_pane, "main-vertical"])
            self._run(["resize-pane", "-t", owner_pane, "-x", "50%"])
            self._run(["select-pane", "-t", owner_pane])

            # The broker must inherit the native launcher environment, not the
            # Codex owner's Seatbelt sandbox.  Keep it in a hidden window and
            # exchange only signed files; the owner cannot use the tmux socket
            # as a command channel from inside its sandbox.
            if broker is not None:
                self._run(
                    [
                        "new-window",
                        "-d",
                        "-t",
                        self.session_name,
                        "-n",
                        "broker",
                        shlex.join(broker),
                    ]
                )
                self._run(
                    [
                        "select-window",
                        "-t",
                        f"{self.session_name}:review-room",
                    ]
                )
            self._run(["respawn-pane", "-k", "-t", owner_pane, shlex.join(owner)])
            description = self.describe()
        except BaseException:
            # The exact session did not exist before this call (or was
            # explicitly replaced), so killing it cannot affect another room.
            self._run(
                ["kill-session", "-t", f"={self.session_name}"],
                allow_missing=True,
            )
            raise
        if attach:
            self._run(
                ["attach-session", "-t", f"={self.session_name}"],
                interactive=True,
            )
        return description

    def list_panes(self) -> list[PaneDescription]:
        if not self.exists():
            return []
        fields = "#{pane_id}\t#{@mrr_title}\t#{pane_input_off}\t#{pane_active}\t#{pane_current_command}"
        result = self._run(
            [
                "list-panes",
                "-t",
                f"{self.session_name}:review-room",
                "-F",
                fields,
            ]
        )
        panes = []
        for line in str(getattr(result, "stdout", "")).splitlines():
            parts = line.split("\t", 4)
            if len(parts) != 5:
                continue
            pane_id, title, input_off, active, command = parts
            panes.append(
                PaneDescription(
                    pane_id=pane_id,
                    title=title,
                    input_off=input_off in {"1", "on", "true"},
                    active=active in {"1", "on", "true"},
                    command=command,
                )
            )
        return panes

    def describe(self) -> dict[str, Any]:
        exists = self.exists()
        panes = self.list_panes() if exists else []
        return {
            "job_id": self.job_id,
            "session_name": self.session_name,
            "exists": exists,
            "panes": panes,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mutual-review terminal room utilities")
    subparsers = parser.add_subparsers(dest="command", required=True)
    observe = subparsers.add_parser("observe", help="render one read-only reviewer lane")
    observe.add_argument("--root", type=Path, required=True)
    observe.add_argument("--job-id", required=True)
    observe.add_argument("--reviewer-id", required=True)
    observe.add_argument("--once", action="store_true")
    observe.add_argument("--interval", type=float, default=0.5)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "observe":
        observe_reviewer(
            args.root,
            args.job_id,
            args.reviewer_id,
            once=args.once,
            interval=args.interval,
        )
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
