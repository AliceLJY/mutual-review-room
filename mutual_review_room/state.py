"""Durable, append-only state for terminal mutual-review rooms.

Each review job owns a private directory and SQLite database.  The database is
the source of truth for orchestration; terminal panes are projections of its
events, never a transport channel.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_REVIEW_ROOT = Path.home() / ".mutual-review-room" / "review-jobs"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,127}$")
_JOB_STATUSES = {"ready", "running", "complete", "failed", "interrupted"}
_TERMINAL_JOB_STATUSES = {"complete", "failed"}
_REVIEWER_STATUSES = {
    "ready",
    "running",
    "complete",
    "failed",
    "unavailable",
    "interrupted",
}
_REQUEST_STATUSES = {
    "running",
    "completed",
    "failed",
    "unavailable",
    "interrupted",
}
_DIRECTIONS = {"owner_to_room", "owner_to_reviewer", "reviewer_to_owner", "system"}


class ReviewStateError(RuntimeError):
    """Base class for review-room state failures."""


class ValidationError(ReviewStateError, ValueError):
    """An input is invalid or unsafe."""


class NotFoundError(ReviewStateError, LookupError):
    """A requested job, reviewer, or request does not exist."""


class StateConflictError(ReviewStateError):
    """The requested transition conflicts with durable state."""


class DuplicateRequestError(StateConflictError):
    """A request ID or reviewer round was already recorded."""


@dataclass(frozen=True, slots=True)
class ReviewerSpec:
    reviewer_id: str
    provider: str
    model: str
    cwd: str
    capability: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ReviewJob:
    job_id: str
    owner_provider: str
    owner_model: str
    owner_cwd: str
    owner_session_id: str | None
    status: str
    max_rounds: int
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Reviewer:
    job_id: str
    reviewer_id: str
    provider: str
    model: str
    cwd: str
    native_session_id: str | None
    status: str
    current_round: int
    last_request_id: str | None
    capability: Mapping[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    request_id: str
    job_id: str
    reviewer_id: str
    session_id: str | None
    round: int
    parent_request_id: str | None
    status: str
    prompt: str
    created_at: str
    started_at: str
    completed_at: str | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReviewEvent:
    sequence: int
    event_id: str
    job_id: str
    request_id: str | None
    reviewer_id: str | None
    session_id: str | None
    round: int | None
    parent_request_id: str | None
    direction: str
    event_type: str
    status: str
    content: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CreatedJob:
    job: ReviewJob
    reviewers: tuple[Reviewer, ...]
    owner_token: str
    token_path: str


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    job: ReviewJob
    reviewers: tuple[Reviewer, ...]
    requests: tuple[ReviewRequest, ...]


class ReviewState:
    """SQLite-backed review-room ledger.

    ``root`` is injectable for tests and alternate installations.  Creating a
    ``ReviewState`` object never recovers or resubmits work; callers must invoke
    :meth:`recover_job` or :meth:`recover_all` explicitly after a restart.
    """

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        configured = root or os.environ.get("MRR_ROOT")
        self.root = Path(configured).expanduser() if configured else DEFAULT_REVIEW_ROOT
        self._ensure_private_dir(self.root)

    # -- public job API -------------------------------------------------

    def create_job(
        self,
        *,
        owner_provider: str,
        owner_model: str,
        owner_cwd: str | os.PathLike[str],
        reviewers: Sequence[ReviewerSpec | Mapping[str, Any]],
        job_id: str | None = None,
        max_rounds: int = 3,
    ) -> CreatedJob:
        job_id = _validate_id(job_id or _new_id("job"), "job_id")
        owner_provider = _validate_provider(owner_provider)
        owner_model = _validate_model(owner_model)
        owner_cwd = _validate_cwd(owner_cwd)
        if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or not 1 <= max_rounds <= 3:
            raise ValidationError("max_rounds must be an integer from 1 through 3")
        specs = tuple(_coerce_reviewer_spec(value) for value in reviewers)
        if not specs:
            raise ValidationError("a review job requires at least one reviewer")
        reviewer_ids = [spec.reviewer_id for spec in specs]
        if len(reviewer_ids) != len(set(reviewer_ids)):
            raise ValidationError("reviewer_id values must be unique within a job")

        job_dir = self._job_dir(job_id)
        if job_dir.exists():
            raise StateConflictError(f"job already exists: {job_id}")
        self._ensure_private_dir(job_dir)
        db_path = job_dir / "state.sqlite3"
        fd = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(db_path, 0o600)

        owner_token = secrets.token_urlsafe(32)
        token_path = job_dir / "owner.token"
        token_fd = os.open(token_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(token_fd, (owner_token + "\n").encode("utf-8"))
        finally:
            os.close(token_fd)
        os.chmod(token_path, 0o600)

        created_at = _utc_now()
        connection = self._connect(job_id, initialize=True)
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO jobs (
                        job_id, owner_provider, owner_model, owner_cwd,
                        owner_session_id, status, max_rounds, created_at, token_hash
                    ) VALUES (?, ?, ?, ?, NULL, 'ready', ?, ?, ?)
                    """,
                    (
                        job_id,
                        owner_provider,
                        owner_model,
                        owner_cwd,
                        max_rounds,
                        created_at,
                        _token_hash(owner_token),
                    ),
                )
                for position, spec in enumerate(specs):
                    connection.execute(
                        """
                        INSERT INTO reviewers (
                            job_id, reviewer_id, position, provider, model, cwd,
                            native_session_id, status, current_round,
                            last_request_id, capability, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'ready', 0, NULL, ?, ?)
                        """,
                        (
                            job_id,
                            spec.reviewer_id,
                            position,
                            spec.provider,
                            spec.model,
                            spec.cwd,
                            _json_object(spec.capability or {}),
                            created_at,
                        ),
                    )
                self._insert_event(
                    connection,
                    job_id=job_id,
                    request_id=None,
                    reviewer_id=None,
                    session_id=None,
                    round_no=None,
                    parent_request_id=None,
                    direction="system",
                    event_type="job_created",
                    status="ready",
                    content="Review job created.",
                    created_at=created_at,
                )
        except Exception:
            # Leave the private directory intact for forensic inspection.  A
            # half-created job is not discoverable because it has no jobs row.
            raise
        finally:
            connection.close()

        return CreatedJob(
            job=self.get_job(job_id),
            reviewers=tuple(self.list_reviewers(job_id)),
            owner_token=owner_token,
            token_path=str(token_path),
        )

    def open_job(self, job_id: str) -> JobSnapshot:
        job_id = _validate_id(job_id, "job_id")
        return JobSnapshot(
            job=self.get_job(job_id),
            reviewers=tuple(self.list_reviewers(job_id)),
            requests=tuple(self.list_requests(job_id)),
        )

    def list_jobs(self) -> list[ReviewJob]:
        jobs: list[ReviewJob] = []
        for child in sorted(self.root.iterdir(), key=lambda path: path.name):
            if child.is_symlink() or not child.is_dir() or not _ID_RE.fullmatch(child.name):
                continue
            db_path = child / "state.sqlite3"
            if not _is_regular_file(db_path):
                continue
            try:
                jobs.append(self.get_job(child.name))
            except (NotFoundError, sqlite3.DatabaseError):
                continue
        return sorted(jobs, key=lambda job: (job.created_at, job.job_id), reverse=True)

    def get_job(self, job_id: str) -> ReviewJob:
        job_id = _validate_id(job_id, "job_id")
        connection = self._connect(job_id)
        try:
            row = connection.execute(
                """
                SELECT job_id, owner_provider, owner_model, owner_cwd,
                       owner_session_id, status, max_rounds, created_at
                FROM jobs WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"job not found: {job_id}")
            return _job_from_row(row)
        finally:
            connection.close()

    def verify_owner_token(self, job_id: str, token: str) -> bool:
        job_id = _validate_id(job_id, "job_id")
        if not isinstance(token, str) or not token:
            return False
        connection = self._connect(job_id)
        try:
            row = connection.execute(
                "SELECT token_hash FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"job not found: {job_id}")
            return hmac.compare_digest(row["token_hash"], _token_hash(token))
        finally:
            connection.close()

    def set_owner_session(self, job_id: str, session_id: str) -> ReviewJob:
        job_id = _validate_id(job_id, "job_id")
        session_id = _validate_id(session_id, "session_id")
        connection = self._connect(job_id)
        try:
            with connection:
                row = connection.execute(
                    "SELECT owner_session_id FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"job not found: {job_id}")
                existing = row["owner_session_id"]
                if existing and existing != session_id:
                    raise StateConflictError("owner native session is immutable once bound")
                if not existing:
                    connection.execute(
                        "UPDATE jobs SET owner_session_id = ? WHERE job_id = ?",
                        (session_id, job_id),
                    )
                    self._insert_event(
                        connection,
                        job_id=job_id,
                        request_id=None,
                        reviewer_id=None,
                        session_id=session_id,
                        round_no=None,
                        parent_request_id=None,
                        direction="system",
                        event_type="owner_session_bound",
                        status="ready",
                        content="Owner native session bound.",
                    )
        finally:
            connection.close()
        return self.get_job(job_id)

    def complete_job(self, job_id: str, verdict: str) -> ReviewJob:
        verdict = _validate_text(
            verdict, "verdict", allow_empty=False, max_length=1_000_000
        )
        return self._set_job_status(job_id, "complete", "job_completed", verdict)

    def fail_job(self, job_id: str, error: str) -> ReviewJob:
        error = _validate_text(error, "error", allow_empty=False, max_length=16_384)
        return self._set_job_status(job_id, "failed", "job_failed", error)

    # -- reviewer API ---------------------------------------------------

    def list_reviewers(self, job_id: str) -> list[Reviewer]:
        job_id = _validate_id(job_id, "job_id")
        connection = self._connect(job_id)
        try:
            self._require_job(connection, job_id)
            rows = connection.execute(
                """
                SELECT job_id, reviewer_id, provider, model, cwd,
                       native_session_id, status, current_round,
                       last_request_id, capability, created_at
                FROM reviewers WHERE job_id = ? ORDER BY position
                """,
                (job_id,),
            ).fetchall()
            return [_reviewer_from_row(row) for row in rows]
        finally:
            connection.close()

    def get_reviewer(self, job_id: str, reviewer_id: str) -> Reviewer:
        job_id = _validate_id(job_id, "job_id")
        reviewer_id = _validate_id(reviewer_id, "reviewer_id")
        connection = self._connect(job_id)
        try:
            row = self._require_reviewer(connection, job_id, reviewer_id)
            return _reviewer_from_row(row)
        finally:
            connection.close()

    def set_reviewer_session(
        self,
        job_id: str,
        reviewer_id: str,
        session_id: str,
        *,
        request_id: str | None = None,
    ) -> Reviewer:
        """Bind an immutable native session to a reviewer and exact request.

        Providers may disclose their session identity only after an invocation
        starts.  In that late-binding case ``request_id`` is mandatory so a
        stale caller cannot backfill an unrelated in-flight request.
        """

        job_id = _validate_id(job_id, "job_id")
        reviewer_id = _validate_id(reviewer_id, "reviewer_id")
        session_id = _validate_id(session_id, "session_id")
        if request_id is not None:
            request_id = _validate_id(request_id, "request_id")
        connection = self._connect(job_id)
        try:
            with connection:
                row = self._require_reviewer(connection, job_id, reviewer_id)
                existing = row["native_session_id"]
                if existing and existing != session_id:
                    raise StateConflictError(
                        "reviewer native session is immutable once bound"
                    )
                request = None
                if request_id is not None:
                    request = self._require_request(connection, job_id, request_id)
                    if request["reviewer_id"] != reviewer_id:
                        raise StateConflictError(
                            "session-binding request belongs to a different reviewer"
                        )
                    if request["status"] != "running":
                        raise StateConflictError(
                            "session binding requires the intended request to be running"
                        )
                    if request["session_id"] not in {None, session_id}:
                        raise StateConflictError(
                            "request is already bound to a different native session"
                        )
                elif connection.execute(
                    """
                    SELECT 1 FROM requests
                    WHERE job_id = ? AND reviewer_id = ? AND status = 'running'
                    """,
                    (job_id, reviewer_id),
                ).fetchone():
                    raise StateConflictError(
                        "request_id is required when binding a running request"
                    )

                if request is not None:
                    cursor = connection.execute(
                        """
                        UPDATE requests SET session_id = ?
                        WHERE request_id = ? AND job_id = ? AND reviewer_id = ?
                          AND status = 'running'
                          AND (session_id IS NULL OR session_id = ?)
                        """,
                        (session_id, request_id, job_id, reviewer_id, session_id),
                    )
                    if cursor.rowcount != 1:
                        raise StateConflictError(
                            "native session was not bound to exactly one running request"
                        )

                if not existing:
                    connection.execute(
                        """
                        UPDATE reviewers SET native_session_id = ?
                        WHERE job_id = ? AND reviewer_id = ?
                        """,
                        (session_id, job_id, reviewer_id),
                    )
                    self._insert_event(
                        connection,
                        job_id=job_id,
                        request_id=request_id,
                        reviewer_id=reviewer_id,
                        session_id=session_id,
                        round_no=request["round_no"] if request is not None else None,
                        parent_request_id=(
                            request["parent_request_id"] if request is not None else None
                        ),
                        direction="system",
                        event_type="reviewer_session_bound",
                        status=row["status"],
                        content="Reviewer native session bound.",
                    )
        finally:
            connection.close()
        return self.get_reviewer(job_id, reviewer_id)

    # -- request API ----------------------------------------------------

    def begin_request(
        self,
        job_id: str,
        reviewer_id: str,
        *,
        round: int,
        prompt: str,
        request_id: str | None = None,
        parent_request_id: str | None = None,
    ) -> ReviewRequest:
        job_id = _validate_id(job_id, "job_id")
        reviewer_id = _validate_id(reviewer_id, "reviewer_id")
        request_id = _validate_id(request_id or _new_id("req"), "request_id")
        prompt = _validate_text(prompt, "prompt", allow_empty=False, max_length=1_000_000)
        if not isinstance(round, int) or isinstance(round, bool) or round < 1:
            raise ValidationError("round must be a positive integer")
        if parent_request_id is not None:
            parent_request_id = _validate_id(parent_request_id, "parent_request_id")

        connection = self._connect(job_id)
        try:
            with connection:
                # Serialize dispatch against terminal transitions.  Without an
                # immediate write reservation, a deferred reader could observe
                # a non-terminal job and insert a request after completion.
                connection.execute("BEGIN IMMEDIATE")
                job = self._require_job(connection, job_id)
                if job["status"] in _TERMINAL_JOB_STATUSES:
                    raise StateConflictError(f"job is already {job['status']}")
                if round > job["max_rounds"] or round > 3:
                    raise ValidationError(
                        f"round {round} exceeds job maximum {job['max_rounds']}"
                    )
                reviewer = self._require_reviewer(connection, job_id, reviewer_id)
                if reviewer["status"] == "unavailable":
                    raise StateConflictError("reviewer is unavailable")
                # Some providers (notably Codex) return the native session ID
                # only after the first invocation begins.  A first request may
                # therefore start unbound; set_reviewer_session() atomically
                # binds the returned ID before completion.  Follow-ups always
                # use the immutable stored ID.
                session_id = reviewer["native_session_id"]
                if connection.execute(
                    "SELECT 1 FROM requests WHERE request_id = ?", (request_id,)
                ).fetchone():
                    raise DuplicateRequestError(f"request already exists: {request_id}")
                if connection.execute(
                    """
                    SELECT 1 FROM requests
                    WHERE job_id = ? AND reviewer_id = ? AND round_no = ?
                    """,
                    (job_id, reviewer_id, round),
                ).fetchone():
                    raise DuplicateRequestError(
                        f"reviewer {reviewer_id} already has a request for round {round}"
                    )
                running = connection.execute(
                    """
                    SELECT request_id, round_no FROM requests
                    WHERE job_id = ? AND reviewer_id = ? AND status = 'running'
                    ORDER BY started_at, request_id
                    LIMIT 1
                    """,
                    (job_id, reviewer_id),
                ).fetchone()
                if running is not None:
                    raise StateConflictError(
                        f"reviewer {reviewer_id} already has running request "
                        f"{running['request_id']}; recover or reconcile it before dispatching again"
                    )
                expected_round = reviewer["current_round"] + 1
                if round != expected_round:
                    raise StateConflictError(
                        f"reviewer {reviewer_id} expects round {expected_round}, got {round}"
                    )

                if round == 1:
                    if parent_request_id is not None:
                        raise ValidationError("round 1 must not have a parent request")
                else:
                    parent_request_id = parent_request_id or reviewer["last_request_id"]
                    if parent_request_id is None:
                        raise StateConflictError(
                            "rounds after 1 require a prior request from the same reviewer"
                        )
                    parent = connection.execute(
                        """
                        SELECT reviewer_id, round_no FROM requests
                        WHERE request_id = ? AND job_id = ?
                        """,
                        (parent_request_id, job_id),
                    ).fetchone()
                    if parent is None:
                        raise NotFoundError(
                            f"parent request not found: {parent_request_id}"
                        )
                    if parent["reviewer_id"] != reviewer_id:
                        raise StateConflictError(
                            "parent request must belong to the same reviewer"
                        )
                    if parent["round_no"] >= round:
                        raise StateConflictError(
                            "parent request must precede the new round"
                        )

                now = _utc_now()
                connection.execute(
                    """
                    INSERT INTO requests (
                        request_id, job_id, reviewer_id, session_id, round_no,
                        parent_request_id, status, prompt, created_at, started_at,
                        completed_at, error
                    ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, NULL, NULL)
                    """,
                    (
                        request_id,
                        job_id,
                        reviewer_id,
                        session_id,
                        round,
                        parent_request_id,
                        prompt,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE reviewers
                    SET status = 'running', current_round = ?, last_request_id = ?
                    WHERE job_id = ? AND reviewer_id = ?
                    """,
                    (round, request_id, job_id, reviewer_id),
                )
                connection.execute(
                    "UPDATE jobs SET status = 'running' WHERE job_id = ?",
                    (job_id,),
                )
                self._insert_event(
                    connection,
                    job_id=job_id,
                    request_id=request_id,
                    reviewer_id=reviewer_id,
                    session_id=session_id,
                    round_no=round,
                    parent_request_id=parent_request_id,
                    direction="owner_to_reviewer",
                    event_type="request_started",
                    status="running",
                    content=prompt,
                    created_at=now,
                )
        except sqlite3.IntegrityError as error:
            raise DuplicateRequestError("request ID or reviewer round already exists") from error
        finally:
            connection.close()
        return self.get_request(job_id, request_id)

    def complete_request(
        self, job_id: str, request_id: str, content: str
    ) -> ReviewRequest:
        content = _validate_text(content, "content", allow_empty=True, max_length=2_000_000)
        return self._finish_request(
            job_id,
            request_id,
            request_status="completed",
            reviewer_status="ready",
            event_type="response_completed",
            direction="reviewer_to_owner",
            content=content,
            error=None,
        )

    def fail_request(
        self,
        job_id: str,
        request_id: str,
        error: str,
        *,
        content: str = "",
    ) -> ReviewRequest:
        error = _validate_text(error, "error", allow_empty=False, max_length=16_384)
        content = _validate_text(content, "content", allow_empty=True, max_length=2_000_000)
        return self._finish_request(
            job_id,
            request_id,
            request_status="failed",
            reviewer_status="failed",
            event_type="request_failed",
            direction="reviewer_to_owner",
            content=content or error,
            error=error,
        )

    def mark_unavailable(
        self,
        job_id: str,
        reviewer_id: str,
        error: str,
        *,
        request_id: str | None = None,
    ) -> Reviewer:
        job_id = _validate_id(job_id, "job_id")
        reviewer_id = _validate_id(reviewer_id, "reviewer_id")
        error = _validate_text(error, "error", allow_empty=False, max_length=16_384)
        if request_id is not None:
            request_id = _validate_id(request_id, "request_id")
        connection = self._connect(job_id)
        try:
            with connection:
                reviewer = self._require_reviewer(connection, job_id, reviewer_id)
                target_request_id = request_id or reviewer["last_request_id"]
                request = None
                if target_request_id:
                    request = self._require_request(
                        connection, job_id, target_request_id
                    )
                    if request["reviewer_id"] != reviewer_id:
                        raise StateConflictError(
                            "unavailable request belongs to a different reviewer"
                        )
                    if request["status"] == "running":
                        connection.execute(
                            """
                            UPDATE requests
                            SET status = 'unavailable', completed_at = ?, error = ?
                            WHERE request_id = ?
                            """,
                            (_utc_now(), error, target_request_id),
                        )
                connection.execute(
                    """
                    UPDATE reviewers SET status = 'unavailable'
                    WHERE job_id = ? AND reviewer_id = ?
                    """,
                    (job_id, reviewer_id),
                )
                self._insert_event(
                    connection,
                    job_id=job_id,
                    request_id=target_request_id,
                    reviewer_id=reviewer_id,
                    session_id=reviewer["native_session_id"],
                    round_no=request["round_no"] if request else None,
                    parent_request_id=(request["parent_request_id"] if request else None),
                    direction="system",
                    event_type="reviewer_unavailable",
                    status="unavailable",
                    content=error,
                )
        finally:
            connection.close()
        return self.get_reviewer(job_id, reviewer_id)

    def list_requests(
        self, job_id: str, *, reviewer_id: str | None = None
    ) -> list[ReviewRequest]:
        job_id = _validate_id(job_id, "job_id")
        if reviewer_id is not None:
            reviewer_id = _validate_id(reviewer_id, "reviewer_id")
        connection = self._connect(job_id)
        try:
            self._require_job(connection, job_id)
            if reviewer_id is None:
                rows = connection.execute(
                    """
                    SELECT * FROM requests WHERE job_id = ?
                    ORDER BY created_at, request_id
                    """,
                    (job_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM requests
                    WHERE job_id = ? AND reviewer_id = ?
                    ORDER BY round_no, created_at, request_id
                    """,
                    (job_id, reviewer_id),
                ).fetchall()
            return [_request_from_row(row) for row in rows]
        finally:
            connection.close()

    def get_request(self, job_id: str, request_id: str) -> ReviewRequest:
        job_id = _validate_id(job_id, "job_id")
        request_id = _validate_id(request_id, "request_id")
        connection = self._connect(job_id)
        try:
            return _request_from_row(
                self._require_request(connection, job_id, request_id)
            )
        finally:
            connection.close()

    # -- append-only event API -----------------------------------------

    def append_event(
        self,
        job_id: str,
        *,
        direction: str,
        event_type: str,
        status: str,
        content: str,
        request_id: str | None = None,
        reviewer_id: str | None = None,
        session_id: str | None = None,
        round: int | None = None,
        parent_request_id: str | None = None,
        event_id: str | None = None,
    ) -> ReviewEvent:
        job_id = _validate_id(job_id, "job_id")
        event_id = _validate_id(event_id or _new_id("evt"), "event_id")
        direction = _validate_choice(direction, "direction", _DIRECTIONS)
        event_type = _validate_id(event_type, "event_type")
        status = _validate_id(status, "status")
        content = _validate_text(content, "content", allow_empty=True, max_length=2_000_000)
        request_id = _validate_optional_id(request_id, "request_id")
        reviewer_id = _validate_optional_id(reviewer_id, "reviewer_id")
        session_id = _validate_optional_id(session_id, "session_id")
        parent_request_id = _validate_optional_id(
            parent_request_id, "parent_request_id"
        )
        if round is not None and (
            not isinstance(round, int) or isinstance(round, bool) or not 1 <= round <= 3
        ):
            raise ValidationError("round must be null or an integer from 1 through 3")

        connection = self._connect(job_id)
        try:
            with connection:
                self._require_job(connection, job_id)
                request = None
                if request_id is not None:
                    request = self._require_request(connection, job_id, request_id)
                    expected = {
                        "reviewer_id": request["reviewer_id"],
                        "session_id": request["session_id"],
                        "round": request["round_no"],
                        "parent_request_id": request["parent_request_id"],
                    }
                    supplied = {
                        "reviewer_id": reviewer_id,
                        "session_id": session_id,
                        "round": round,
                        "parent_request_id": parent_request_id,
                    }
                    for field, expected_value in expected.items():
                        supplied_value = supplied[field]
                        if supplied_value is not None and supplied_value != expected_value:
                            raise StateConflictError(
                                f"event {field} does not match request ownership"
                            )
                    reviewer_id = request["reviewer_id"]
                    session_id = request["session_id"]
                    round = request["round_no"]
                    parent_request_id = request["parent_request_id"]
                elif reviewer_id is not None:
                    reviewer = self._require_reviewer(
                        connection, job_id, reviewer_id
                    )
                    if session_id is not None and session_id != reviewer["native_session_id"]:
                        raise StateConflictError(
                            "event session does not match reviewer ownership"
                        )
                self._insert_event(
                    connection,
                    event_id=event_id,
                    job_id=job_id,
                    request_id=request_id,
                    reviewer_id=reviewer_id,
                    session_id=session_id,
                    round_no=round,
                    parent_request_id=parent_request_id,
                    direction=direction,
                    event_type=event_type,
                    status=status,
                    content=content,
                )
            row = connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            assert row is not None
            return _event_from_row(row)
        finally:
            connection.close()

    def list_events(
        self,
        job_id: str,
        *,
        after: int = 0,
        limit: int = 500,
        reviewer_id: str | None = None,
        include_global: bool = False,
        request_id: str | None = None,
        event_type: str | None = None,
    ) -> list[ReviewEvent]:
        job_id = _validate_id(job_id, "job_id")
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise ValidationError("after cursor must be a non-negative integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise ValidationError("limit must be an integer from 1 through 10000")
        if not isinstance(include_global, bool):
            raise ValidationError("include_global must be a boolean")
        reviewer_id = _validate_optional_id(reviewer_id, "reviewer_id")
        request_id = _validate_optional_id(request_id, "request_id")
        event_type = _validate_optional_id(event_type, "event_type")
        clauses = ["job_id = ?", "sequence > ?"]
        parameters: list[Any] = [job_id, after]
        if reviewer_id is not None:
            if include_global:
                clauses.append("(reviewer_id = ? OR reviewer_id IS NULL)")
            else:
                clauses.append("reviewer_id = ?")
            parameters.append(reviewer_id)
        for column, value in (("request_id", request_id), ("event_type", event_type)):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        parameters.append(limit)
        connection = self._connect(job_id)
        try:
            self._require_job(connection, job_id)
            rows = connection.execute(
                f"SELECT * FROM events WHERE {' AND '.join(clauses)} "
                "ORDER BY sequence LIMIT ?",
                parameters,
            ).fetchall()
            return [_event_from_row(row) for row in rows]
        finally:
            connection.close()

    # -- restart recovery ----------------------------------------------

    def recover_job(self, job_id: str) -> list[ReviewRequest]:
        """Mark in-flight work interrupted without creating replacement work."""

        job_id = _validate_id(job_id, "job_id")
        connection = self._connect(job_id)
        interrupted_ids: list[str] = []
        try:
            with connection:
                self._require_job(connection, job_id)
                rows = connection.execute(
                    """
                    SELECT * FROM requests
                    WHERE job_id = ? AND status = 'running'
                    ORDER BY created_at, request_id
                    """,
                    (job_id,),
                ).fetchall()
                now = _utc_now()
                for request in rows:
                    interrupted_ids.append(request["request_id"])
                    connection.execute(
                        """
                        UPDATE requests
                        SET status = 'interrupted', completed_at = ?,
                            error = 'interrupted by process restart'
                        WHERE request_id = ? AND status = 'running'
                        """,
                        (now, request["request_id"]),
                    )
                    connection.execute(
                        """
                        UPDATE reviewers SET status = 'interrupted'
                        WHERE job_id = ? AND reviewer_id = ? AND status = 'running'
                        """,
                        (job_id, request["reviewer_id"]),
                    )
                    self._insert_event(
                        connection,
                        job_id=job_id,
                        request_id=request["request_id"],
                        reviewer_id=request["reviewer_id"],
                        session_id=request["session_id"],
                        round_no=request["round_no"],
                        parent_request_id=request["parent_request_id"],
                        direction="system",
                        event_type="request_interrupted",
                        status="interrupted",
                        content="Request marked interrupted after process restart; no retry was created.",
                        created_at=now,
                    )
                if rows:
                    connection.execute(
                        """
                        UPDATE jobs SET status = 'interrupted'
                        WHERE job_id = ? AND status = 'running'
                        """,
                        (job_id,),
                    )
        finally:
            connection.close()
        return [self.get_request(job_id, request_id) for request_id in interrupted_ids]

    def recover_all(self) -> dict[str, list[ReviewRequest]]:
        recovered: dict[str, list[ReviewRequest]] = {}
        for job in self.list_jobs():
            interrupted = self.recover_job(job.job_id)
            if interrupted:
                recovered[job.job_id] = interrupted
        return recovered

    # -- internal helpers ----------------------------------------------

    def _set_job_status(
        self, job_id: str, status: str, event_type: str, content: str = ""
    ) -> ReviewJob:
        job_id = _validate_id(job_id, "job_id")
        status = _validate_choice(status, "job status", _JOB_STATUSES)
        connection = self._connect(job_id)
        should_write = True
        try:
            with connection:
                # Terminal decisions and the event that records them are one
                # serialized transaction.  This also makes repeated terminal
                # calls idempotent instead of appending duplicate decisions.
                connection.execute("BEGIN IMMEDIATE")
                job = self._require_job(connection, job_id)
                current_status = job["status"]
                if current_status in _TERMINAL_JOB_STATUSES:
                    if current_status != status:
                        raise StateConflictError(
                            f"job is already terminal with status {current_status}"
                        )
                    recorded = connection.execute(
                        """
                        SELECT content FROM events
                        WHERE job_id = ? AND reviewer_id IS NULL
                          AND direction = 'system' AND event_type = ? AND status = ?
                        ORDER BY sequence DESC
                        LIMIT 1
                        """,
                        (job_id, event_type, status),
                    ).fetchone()
                    if recorded is None or recorded["content"] != content:
                        raise StateConflictError(
                            "job is already terminal with a different decision payload"
                        )
                    should_write = False
                if should_write:
                    running = connection.execute(
                        """
                        SELECT request_id FROM requests
                        WHERE job_id = ? AND status = 'running'
                        ORDER BY started_at, request_id
                        LIMIT 1
                        """,
                        (job_id,),
                    ).fetchone()
                    if running is not None:
                        raise StateConflictError(
                            "job has a running request; finish or recover it before a terminal transition"
                        )
                if should_write:
                    connection.execute(
                        "UPDATE jobs SET status = ? WHERE job_id = ?", (status, job_id)
                    )
                    self._insert_event(
                        connection,
                        job_id=job_id,
                        request_id=None,
                        reviewer_id=None,
                        session_id=None,
                        round_no=None,
                        parent_request_id=None,
                        direction="system",
                        event_type=event_type,
                        status=status,
                        content=content,
                    )
        finally:
            connection.close()
        return self.get_job(job_id)

    def _finish_request(
        self,
        job_id: str,
        request_id: str,
        *,
        request_status: str,
        reviewer_status: str,
        event_type: str,
        direction: str,
        content: str,
        error: str | None,
    ) -> ReviewRequest:
        job_id = _validate_id(job_id, "job_id")
        request_id = _validate_id(request_id, "request_id")
        _validate_choice(request_status, "request status", _REQUEST_STATUSES)
        _validate_choice(reviewer_status, "reviewer status", _REVIEWER_STATUSES)
        connection = self._connect(job_id)
        try:
            with connection:
                # Serialize request settlement against complete_job().
                connection.execute("BEGIN IMMEDIATE")
                job = self._require_job(connection, job_id)
                if job["status"] in _TERMINAL_JOB_STATUSES:
                    raise StateConflictError(f"job is already {job['status']}")
                request = self._require_request(connection, job_id, request_id)
                if request["status"] != "running":
                    raise StateConflictError(
                        f"request {request_id} is already {request['status']}"
                    )
                now = _utc_now()
                connection.execute(
                    """
                    UPDATE requests
                    SET status = ?, completed_at = ?, error = ?
                    WHERE request_id = ?
                    """,
                    (request_status, now, error, request_id),
                )
                connection.execute(
                    """
                    UPDATE reviewers SET status = ?
                    WHERE job_id = ? AND reviewer_id = ?
                    """,
                    (reviewer_status, job_id, request["reviewer_id"]),
                )
                self._insert_event(
                    connection,
                    job_id=job_id,
                    request_id=request_id,
                    reviewer_id=request["reviewer_id"],
                    session_id=request["session_id"],
                    round_no=request["round_no"],
                    parent_request_id=request["parent_request_id"],
                    direction=direction,
                    event_type=event_type,
                    status=request_status,
                    content=content,
                    created_at=now,
                )
        finally:
            connection.close()
        return self.get_request(job_id, request_id)

    def _job_dir(self, job_id: str) -> Path:
        job_id = _validate_id(job_id, "job_id")
        return self.root / job_id

    def _connect(self, job_id: str, *, initialize: bool = False) -> sqlite3.Connection:
        job_dir = self._job_dir(job_id)
        db_path = job_dir / "state.sqlite3"
        if job_dir.is_symlink() or not job_dir.is_dir():
            raise NotFoundError(f"job not found: {job_id}")
        if not _is_regular_file(db_path):
            raise NotFoundError(f"job database not found: {job_id}")
        _enforce_mode(job_dir, 0o700)
        _enforce_mode(db_path, 0o600)
        connection = sqlite3.connect(db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if initialize:
            connection.executescript(_SCHEMA)
        return connection

    def _require_job(self, connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"job not found: {job_id}")
        return row

    def _require_reviewer(
        self, connection: sqlite3.Connection, job_id: str, reviewer_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM reviewers WHERE job_id = ? AND reviewer_id = ?
            """,
            (job_id, reviewer_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"reviewer not found: {reviewer_id}")
        return row

    def _require_request(
        self, connection: sqlite3.Connection, job_id: str, request_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM requests WHERE job_id = ? AND request_id = ?",
            (job_id, request_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"request not found: {request_id}")
        return row

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        request_id: str | None,
        reviewer_id: str | None,
        session_id: str | None,
        round_no: int | None,
        parent_request_id: str | None,
        direction: str,
        event_type: str,
        status: str,
        content: str,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> None:
        self._assert_token_absent(job_id, content)
        connection.execute(
            """
            INSERT INTO events (
                event_id, job_id, request_id, reviewer_id, session_id,
                round_no, parent_request_id, direction, event_type,
                status, content, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id or _new_id("evt"),
                job_id,
                request_id,
                reviewer_id,
                session_id,
                round_no,
                parent_request_id,
                direction,
                event_type,
                status,
                content,
                created_at or _utc_now(),
            ),
        )

    def _assert_token_absent(self, job_id: str, content: str) -> None:
        token_path = self._job_dir(job_id) / "owner.token"
        if not _is_regular_file(token_path):
            return
        token = token_path.read_text(encoding="utf-8").strip()
        if token and token in content:
            raise ValidationError("event content must not contain the owner token")

    @staticmethod
    def _ensure_private_dir(path: Path) -> None:
        if path.exists() and (path.is_symlink() or not path.is_dir()):
            raise ValidationError(f"private state path is not a real directory: {path}")
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        _enforce_mode(path, 0o700)


def _coerce_reviewer_spec(value: ReviewerSpec | Mapping[str, Any]) -> ReviewerSpec:
    if isinstance(value, ReviewerSpec):
        source: Mapping[str, Any] = asdict(value)
    elif isinstance(value, Mapping):
        source = value
    else:
        raise ValidationError("reviewers must be ReviewerSpec objects or mappings")
    required = {"reviewer_id", "provider", "model", "cwd"}
    missing = sorted(required.difference(source))
    unknown = sorted(set(source).difference(required | {"capability"}))
    if missing:
        raise ValidationError(f"reviewer is missing fields: {', '.join(missing)}")
    if unknown:
        raise ValidationError(f"reviewer has unknown fields: {', '.join(unknown)}")
    capability = source.get("capability") or {}
    if not isinstance(capability, Mapping):
        raise ValidationError("reviewer capability must be a JSON object")
    return ReviewerSpec(
        reviewer_id=_validate_id(source["reviewer_id"], "reviewer_id"),
        provider=_validate_provider(source["provider"]),
        model=_validate_model(source["model"]),
        cwd=_validate_cwd(source["cwd"]),
        capability=json.loads(_json_object(capability)),
    )


def _validate_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValidationError(
            f"{field} must match {_ID_RE.pattern} and be at most 128 characters"
        )
    return value


def _validate_optional_id(value: Any, field: str) -> str | None:
    return None if value is None else _validate_id(value, field)


def _validate_provider(value: Any) -> str:
    if not isinstance(value, str) or not _PROVIDER_RE.fullmatch(value):
        raise ValidationError(
            "provider must be lowercase and contain only letters, digits, '_' or '-'"
        )
    return value


def _validate_model(value: Any) -> str:
    if not isinstance(value, str) or not _MODEL_RE.fullmatch(value):
        raise ValidationError("model contains unsupported characters or is too long")
    return value


def _validate_cwd(value: str | os.PathLike[str]) -> str:
    try:
        path = Path(value).expanduser()
    except TypeError as error:
        raise ValidationError("cwd must be a filesystem path") from error
    if not path.is_absolute() or "\x00" in str(path):
        raise ValidationError("cwd must be an absolute path")
    return str(path)


def _validate_text(
    value: Any, field: str, *, allow_empty: bool, max_length: int
) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    if not allow_empty and not value.strip():
        raise ValidationError(f"{field} must not be empty")
    if "\x00" in value:
        raise ValidationError(f"{field} must not contain NUL")
    if len(value) > max_length:
        raise ValidationError(f"{field} exceeds {max_length} characters")
    return value


def _validate_choice(value: Any, field: str, choices: set[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValidationError(f"{field} must be one of: {', '.join(sorted(choices))}")
    return value


def _json_object(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValidationError("capability must contain JSON-serializable values") from error
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ValidationError("capability must encode a JSON object")
    return encoded


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_regular_file(path: Path) -> bool:
    try:
        return not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)
    except FileNotFoundError:
        return False


def _enforce_mode(path: Path, mode: int) -> None:
    """Require ``mode`` without writing when the mode is already correct.

    A read-only consumer such as ``status`` must be able to open the control
    root from a sandbox that denies writes to it.  An unconditional ``chmod``
    fails there even though it would not have changed anything, so the metadata
    write only happens when the mode actually differs.
    """

    if stat.S_IMODE(os.lstat(path).st_mode) != mode:
        os.chmod(path, mode)


def _job_from_row(row: sqlite3.Row) -> ReviewJob:
    return ReviewJob(
        job_id=row["job_id"],
        owner_provider=row["owner_provider"],
        owner_model=row["owner_model"],
        owner_cwd=row["owner_cwd"],
        owner_session_id=row["owner_session_id"],
        status=row["status"],
        max_rounds=row["max_rounds"],
        created_at=row["created_at"],
    )


def _reviewer_from_row(row: sqlite3.Row) -> Reviewer:
    return Reviewer(
        job_id=row["job_id"],
        reviewer_id=row["reviewer_id"],
        provider=row["provider"],
        model=row["model"],
        cwd=row["cwd"],
        native_session_id=row["native_session_id"],
        status=row["status"],
        current_round=row["current_round"],
        last_request_id=row["last_request_id"],
        capability=json.loads(row["capability"]),
        created_at=row["created_at"],
    )


def _request_from_row(row: sqlite3.Row) -> ReviewRequest:
    return ReviewRequest(
        request_id=row["request_id"],
        job_id=row["job_id"],
        reviewer_id=row["reviewer_id"],
        session_id=row["session_id"],
        round=row["round_no"],
        parent_request_id=row["parent_request_id"],
        status=row["status"],
        prompt=row["prompt"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        error=row["error"],
    )


def _event_from_row(row: sqlite3.Row) -> ReviewEvent:
    return ReviewEvent(
        sequence=row["sequence"],
        event_id=row["event_id"],
        job_id=row["job_id"],
        request_id=row["request_id"],
        reviewer_id=row["reviewer_id"],
        session_id=row["session_id"],
        round=row["round_no"],
        parent_request_id=row["parent_request_id"],
        direction=row["direction"],
        event_type=row["event_type"],
        status=row["status"],
        content=row["content"],
        created_at=row["created_at"],
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    owner_provider TEXT NOT NULL,
    owner_model TEXT NOT NULL,
    owner_cwd TEXT NOT NULL,
    owner_session_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('ready','running','complete','failed','interrupted')),
    max_rounds INTEGER NOT NULL CHECK (max_rounds BETWEEN 1 AND 3),
    created_at TEXT NOT NULL,
    token_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviewers (
    job_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    cwd TEXT NOT NULL,
    native_session_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('ready','running','complete','failed','unavailable','interrupted')
    ),
    current_round INTEGER NOT NULL DEFAULT 0 CHECK (current_round BETWEEN 0 AND 3),
    last_request_id TEXT,
    capability TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, reviewer_id),
    UNIQUE (job_id, position),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    session_id TEXT,
    round_no INTEGER NOT NULL CHECK (round_no BETWEEN 1 AND 3),
    parent_request_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('running','completed','failed','unavailable','interrupted')
    ),
    prompt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT,
    UNIQUE (job_id, reviewer_id, round_no),
    FOREIGN KEY (job_id, reviewer_id) REFERENCES reviewers(job_id, reviewer_id),
    FOREIGN KEY (parent_request_id) REFERENCES requests(request_id)
);

CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL,
    request_id TEXT,
    reviewer_id TEXT,
    session_id TEXT,
    round_no INTEGER CHECK (round_no IS NULL OR round_no BETWEEN 1 AND 3),
    parent_request_id TEXT,
    direction TEXT NOT NULL CHECK (
        direction IN ('owner_to_room','owner_to_reviewer','reviewer_to_owner','system')
    ),
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    FOREIGN KEY (request_id) REFERENCES requests(request_id),
    FOREIGN KEY (job_id, reviewer_id) REFERENCES reviewers(job_id, reviewer_id),
    FOREIGN KEY (parent_request_id) REFERENCES requests(request_id)
);

CREATE INDEX IF NOT EXISTS events_job_cursor
    ON events(job_id, sequence);
CREATE INDEX IF NOT EXISTS events_reviewer_cursor
    ON events(job_id, reviewer_id, sequence);
CREATE INDEX IF NOT EXISTS events_request_cursor
    ON events(job_id, request_id, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS requests_one_running_per_reviewer
    ON requests(job_id, reviewer_id) WHERE status = 'running';

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;
"""


__all__ = [
    "CreatedJob",
    "DuplicateRequestError",
    "JobSnapshot",
    "NotFoundError",
    "ReviewEvent",
    "ReviewJob",
    "ReviewRequest",
    "Reviewer",
    "ReviewerSpec",
    "ReviewState",
    "ReviewStateError",
    "StateConflictError",
    "ValidationError",
]
