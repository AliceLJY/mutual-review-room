"""Authenticated, job-local file mailbox for sandboxed owner clients.

The owner process and the outer controller deliberately communicate through
ordinary files.  A provider invocation is never tied to a client connection,
and a request moved to ``processing`` is never replayed after a broker restart.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_JSON_DEPTH = 24
MAX_COLLECTION_ITEMS = 10_000

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_FIELDS = {
    "version",
    "type",
    "request_id",
    "action",
    "payload",
    "created_ns",
    "signature",
}
_RESPONSE_FIELDS = {
    "version",
    "type",
    "request_id",
    "status",
    "result",
    "error",
    "completed_ns",
    "signature",
}
_HEARTBEAT_FIELDS = {
    "version",
    "type",
    "broker_id",
    "pid",
    "started_ns",
    "updated_ns",
    "signature",
}
_RESPONSE_STATUSES = {"ok", "error", "abandoned"}


class BrokerError(RuntimeError):
    """Base class for mailbox failures."""


class MailboxSecurityError(BrokerError):
    """A mailbox path or permission boundary is unsafe."""


class ProtocolError(BrokerError, ValueError):
    """A mailbox message violates the protocol."""


class SignatureError(ProtocolError):
    """A signed mailbox message failed authentication."""


class DuplicateMessageError(BrokerError):
    """A request ID already exists in this job mailbox."""


class BrokerAlreadyRunning(BrokerError):
    """Another process owns the job-local broker lock."""


class BrokerUnavailable(BrokerError):
    """No live broker heartbeat is available."""


class StaleHeartbeatError(BrokerUnavailable):
    """The broker heartbeat stopped advancing."""


class BrokerTimeout(BrokerError, TimeoutError):
    """A response did not arrive before the caller's deadline."""


class _DirectoryHandle:
    """Small owner for a directory descriptor (``os.fdopen`` rejects it)."""

    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor

    def fileno(self) -> int:
        if self.descriptor < 0:
            raise ValueError("directory handle is closed")
        return self.descriptor

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


@dataclass(frozen=True, slots=True)
class MailboxPaths:
    """Validated filesystem locations for one review job's broker."""

    job_dir: Path
    base: Path
    inbox: Path
    processing: Path
    outbox: Path
    lock: Path
    queue_lock: Path
    heartbeat: Path


@dataclass(frozen=True, slots=True)
class BrokerRequest:
    request_id: str
    action: str
    payload: Mapping[str, Any]
    created_ns: int


@dataclass(frozen=True, slots=True)
class BrokerResponse:
    request_id: str
    status: str
    result: Mapping[str, Any] | None
    error: Mapping[str, str] | None
    completed_ns: int


BrokerHandler = Callable[[str, Mapping[str, Any], str], Mapping[str, Any]]


def prepare_mailbox(
    state_root: str | os.PathLike[str], job_id: str
) -> MailboxPaths:
    """Create and validate a private mailbox beneath an existing job.

    The state root and job directory must already exist.  Every mailbox
    directory is opened without following symlinks and forced to mode 0700.
    """

    job_id = _validate_id(job_id, "job_id")
    root = _absolute_path(state_root)
    _open_private_dir(root, fix_mode=True).close()
    job_dir = root / job_id
    _open_private_dir(job_dir, fix_mode=True).close()
    base = job_dir / "broker"
    _ensure_private_dir(base)
    inbox = base / "inbox"
    processing = base / "processing"
    outbox = base / "outbox"
    for directory in (inbox, processing, outbox):
        _ensure_private_dir(directory)
    paths = _paths_for(root, job_id)
    _validate_paths(paths)
    return paths


def mailbox_paths(
    state_root: str | os.PathLike[str], job_id: str
) -> MailboxPaths:
    """Open an already prepared mailbox without changing parent metadata."""

    paths = _paths_for(_absolute_path(state_root), _validate_id(job_id, "job_id"))
    _validate_paths(paths)
    return paths


def _paths_for(root: Path, job_id: str) -> MailboxPaths:
    job_dir = root / job_id
    base = job_dir / "broker"
    inbox = base / "inbox"
    return MailboxPaths(
        job_dir=job_dir,
        base=base,
        inbox=inbox,
        processing=base / "processing",
        outbox=base / "outbox",
        lock=base / "broker.lock",
        # The enqueue lock lives beside the requests it orders so that a
        # sandboxed owner needs write access to the inbox and nothing else.
        # Widening the owner's writable set to the whole mailbox would also
        # hand it the broker's own outbox, processing claims, and heartbeat.
        queue_lock=inbox / "queue.lock",
        heartbeat=base / "heartbeat.json",
    )


def submit(
    paths: MailboxPaths,
    owner_token: str | bytes,
    action: str,
    payload: Mapping[str, Any],
    *,
    request_id: str | None = None,
) -> str:
    """Atomically enqueue one authenticated request and return its ID."""

    _validate_paths(paths)
    key = _token_bytes(owner_token)
    action = _validate_action(action)
    request_id = _validate_id(request_id or f"req_{uuid.uuid4().hex}", "request_id")
    payload_object = _validated_object(payload, "payload")
    _assert_token_absent(payload_object, key)
    body = {
        "version": PROTOCOL_VERSION,
        "type": "request",
        "request_id": request_id,
        "action": action,
        "payload": payload_object,
        "created_ns": time.time_ns(),
    }
    encoded = _encode_signed(body, key, MAX_REQUEST_BYTES)
    filename = _message_filename(request_id)
    with _file_lock(paths.inbox, paths.queue_lock.name, blocking=True):
        for directory in (paths.inbox, paths.processing, paths.outbox):
            if _entry_exists(directory, filename):
                raise DuplicateMessageError(f"request already exists: {request_id}")
        _atomic_write(paths.inbox, filename, encoded, replace=False)
    return request_id


def wait(
    paths: MailboxPaths,
    owner_token: str | bytes,
    request_id: str,
    *,
    timeout: float = 300.0,
    stale_after: float = 5.0,
    poll_interval: float = 0.05,
    startup_grace: float = 1.0,
) -> BrokerResponse:
    """Wait for a signed response while failing quickly on a stale broker."""

    _validate_paths(paths)
    key = _token_bytes(owner_token)
    request_id = _validate_id(request_id, "request_id")
    _validate_positive(timeout, "timeout")
    _validate_positive(stale_after, "stale_after")
    _validate_positive(poll_interval, "poll_interval")
    if not isinstance(startup_grace, (int, float)) or isinstance(startup_grace, bool):
        raise ValueError("startup_grace must be a non-negative number")
    if startup_grace < 0:
        raise ValueError("startup_grace must be a non-negative number")

    started = time.monotonic()
    response_name = _message_filename(request_id)
    while True:
        response_bytes = _read_optional_file(
            paths.outbox, response_name, MAX_RESPONSE_BYTES
        )
        if response_bytes is not None:
            response = _decode_response(response_bytes, key)
            if response.request_id != request_id:
                raise ProtocolError("response request_id does not match its filename")
            return response

        heartbeat_bytes = _read_optional_file(
            paths.base, paths.heartbeat.name, 16_384
        )
        elapsed = time.monotonic() - started
        if heartbeat_bytes is None:
            if elapsed >= startup_grace:
                raise BrokerUnavailable("broker heartbeat is unavailable")
        else:
            heartbeat = _decode_heartbeat(heartbeat_bytes, key)
            age_ns = time.time_ns() - heartbeat["updated_ns"]
            if age_ns < -5_000_000_000:
                raise ProtocolError("broker heartbeat is implausibly in the future")
            if age_ns > int(stale_after * 1_000_000_000):
                raise StaleHeartbeatError("broker heartbeat is stale")

        if elapsed >= timeout:
            raise BrokerTimeout(f"timed out waiting for response: {request_id}")
        time.sleep(min(poll_interval, max(0.0, timeout - elapsed)))


def serve_one(
    paths: MailboxPaths,
    owner_token: str | bytes,
    handler: BrokerHandler,
    *,
    heartbeat_interval: float = 0.5,
) -> bool:
    """Settle at most one queued request under the single-broker lock."""

    _validate_paths(paths)
    key = _token_bytes(owner_token)
    _validate_handler(handler)
    _validate_positive(heartbeat_interval, "heartbeat_interval")
    with _file_lock(paths.base, paths.lock.name, blocking=False):
        with _Heartbeat(paths, key, heartbeat_interval) as heartbeat:
            _reconcile_processing(paths, key)
            claimed = _claim_next(paths)
            if claimed is None:
                heartbeat.raise_if_failed()
                return False
            _process_claimed(paths, key, claimed, handler)
            heartbeat.raise_if_failed()
            return True


def serve_forever(
    paths: MailboxPaths,
    owner_token: str | bytes,
    handler: BrokerHandler,
    *,
    poll_interval: float = 0.1,
    heartbeat_interval: float = 0.5,
    stop_event: threading.Event | None = None,
) -> None:
    """Serve queued requests until ``stop_event`` is set.

    Heartbeats are written by a dedicated thread, so a blocking provider call
    does not make the broker appear dead.  Stopping a client wait has no effect
    on this loop or on an in-flight handler.
    """

    _validate_paths(paths)
    key = _token_bytes(owner_token)
    _validate_handler(handler)
    _validate_positive(poll_interval, "poll_interval")
    _validate_positive(heartbeat_interval, "heartbeat_interval")
    stop_event = stop_event or threading.Event()
    with _file_lock(paths.base, paths.lock.name, blocking=False):
        with _Heartbeat(paths, key, heartbeat_interval) as heartbeat:
            _reconcile_processing(paths, key)
            while not stop_event.is_set():
                claimed = _claim_next(paths)
                if claimed is None:
                    heartbeat.raise_if_failed()
                    stop_event.wait(poll_interval)
                    continue
                _process_claimed(paths, key, claimed, handler)
                heartbeat.raise_if_failed()


def _process_claimed(
    paths: MailboxPaths,
    key: bytes,
    claimed: Path,
    handler: BrokerHandler,
) -> None:
    filename_id = _id_from_filename(claimed.name)
    try:
        request = _read_request(claimed, key)
        if request.request_id != filename_id:
            raise ProtocolError("request_id does not match its filename")
    except (ProtocolError, MailboxSecurityError):
        _write_response(
            paths,
            key,
            filename_id,
            status="error",
            result=None,
            error={"code": "invalid_request", "message": "request authentication failed"},
        )
        return

    try:
        result = handler(request.action, request.payload, request.request_id)
        result_object = _validated_object(result, "handler result")
        _assert_token_absent(result_object, key)
    except Exception as error:  # provider/controller errors become durable replies
        _write_response(
            paths,
            key,
            request.request_id,
            status="error",
            result=None,
            error={
                "code": "handler_error",
                "message": f"handler raised {type(error).__name__}",
            },
        )
        return
    _write_response(
        paths,
        key,
        request.request_id,
        status="ok",
        result=result_object,
        error=None,
    )


def _reconcile_processing(paths: MailboxPaths, key: bytes) -> None:
    """Return diagnostics for orphaned claims without invoking their handlers."""

    for claimed in _message_files(paths.processing):
        request_id = _id_from_filename(claimed.name)
        if _entry_exists(paths.outbox, claimed.name):
            continue
        try:
            request = _read_request(claimed, key)
            if request.request_id != request_id:
                raise ProtocolError("request_id does not match its filename")
            error = {
                "code": "broker_interrupted",
                "message": "claimed request was not replayed after broker restart",
            }
        except (ProtocolError, MailboxSecurityError):
            error = {
                "code": "invalid_processing_entry",
                "message": "retained processing entry could not be authenticated",
            }
        _write_response(
            paths,
            key,
            request_id,
            status="abandoned",
            result=None,
            error=error,
        )


def _claim_next(paths: MailboxPaths) -> Path | None:
    queued = _message_files(paths.inbox)
    if not queued:
        return None
    source = queued[0]
    target = paths.processing / source.name
    if _entry_exists(paths.processing, source.name):
        raise DuplicateMessageError(f"processing entry already exists: {source.stem}")
    source_directory = _open_private_dir(paths.inbox)
    target_directory = _open_private_dir(paths.processing)
    try:
        os.replace(
            source.name,
            target.name,
            src_dir_fd=source_directory.fileno(),
            dst_dir_fd=target_directory.fileno(),
        )
        os.fsync(source_directory.fileno())
        os.fsync(target_directory.fileno())
    finally:
        source_directory.close()
        target_directory.close()
    return target


def _write_response(
    paths: MailboxPaths,
    key: bytes,
    request_id: str,
    *,
    status: str,
    result: Mapping[str, Any] | None,
    error: Mapping[str, str] | None,
) -> None:
    request_id = _validate_id(request_id, "request_id")
    if status not in _RESPONSE_STATUSES:
        raise ProtocolError("unsupported response status")
    if status == "ok":
        if result is None or error is not None:
            raise ProtocolError("successful responses require result and no error")
    elif result is not None or error is None:
        raise ProtocolError("non-success responses require error and no result")
    error_object = None if error is None else _validated_error(error)
    result_object = None if result is None else _validated_object(result, "result")
    body = {
        "version": PROTOCOL_VERSION,
        "type": "response",
        "request_id": request_id,
        "status": status,
        "result": result_object,
        "error": error_object,
        "completed_ns": time.time_ns(),
    }
    encoded = _encode_signed(body, key, MAX_RESPONSE_BYTES)
    _atomic_write(
        paths.outbox,
        _message_filename(request_id),
        encoded,
        replace=False,
    )


def _read_request(path: Path, key: bytes) -> BrokerRequest:
    raw = _read_regular_file(path, MAX_REQUEST_BYTES)
    envelope = _decode_signed(raw, key, _REQUEST_FIELDS, "request")
    if envelope["version"] != PROTOCOL_VERSION:
        raise ProtocolError("unsupported request protocol version")
    request_id = _validate_id(envelope["request_id"], "request_id")
    action = _validate_action(envelope["action"])
    created_ns = _validate_timestamp(envelope["created_ns"], "created_ns")
    payload = _validated_object(envelope["payload"], "payload")
    return BrokerRequest(request_id, action, payload, created_ns)


def _decode_response(raw: bytes, key: bytes) -> BrokerResponse:
    envelope = _decode_signed(raw, key, _RESPONSE_FIELDS, "response")
    if envelope["version"] != PROTOCOL_VERSION:
        raise ProtocolError("unsupported response protocol version")
    request_id = _validate_id(envelope["request_id"], "request_id")
    status = envelope["status"]
    if status not in _RESPONSE_STATUSES:
        raise ProtocolError("unsupported response status")
    completed_ns = _validate_timestamp(envelope["completed_ns"], "completed_ns")
    result = envelope["result"]
    error = envelope["error"]
    if status == "ok":
        if error is not None:
            raise ProtocolError("successful response contains an error")
        result = _validated_object(result, "result")
    else:
        if result is not None:
            raise ProtocolError("non-success response contains a result")
        error = _validated_error(error)
    return BrokerResponse(request_id, status, result, error, completed_ns)


def _decode_heartbeat(raw: bytes, key: bytes) -> dict[str, Any]:
    envelope = _decode_signed(raw, key, _HEARTBEAT_FIELDS, "heartbeat")
    if envelope["version"] != PROTOCOL_VERSION:
        raise ProtocolError("unsupported heartbeat protocol version")
    _validate_id(envelope["broker_id"], "broker_id")
    pid = envelope["pid"]
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ProtocolError("heartbeat pid must be a positive integer")
    started_ns = _validate_timestamp(envelope["started_ns"], "started_ns")
    updated_ns = _validate_timestamp(envelope["updated_ns"], "updated_ns")
    if updated_ns < started_ns:
        raise ProtocolError("heartbeat updated_ns precedes started_ns")
    return envelope


class _Heartbeat:
    def __init__(self, paths: MailboxPaths, key: bytes, interval: float) -> None:
        self.paths = paths
        self.key = key
        self.interval = interval
        self.broker_id = f"broker_{uuid.uuid4().hex}"
        self.started_ns = time.time_ns()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: BaseException | None = None

    def __enter__(self) -> _Heartbeat:
        self._write()
        self.thread = threading.Thread(
            target=self._run,
            name=f"mrr-heartbeat-{self.broker_id[-8:]}",
            daemon=True,
        )
        self.thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        if exc is None:
            self.raise_if_failed()

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self._write()
            except BaseException as error:
                self.error = error
                self.stop_event.set()
                return

    def _write(self) -> None:
        body = {
            "version": PROTOCOL_VERSION,
            "type": "heartbeat",
            "broker_id": self.broker_id,
            "pid": os.getpid(),
            "started_ns": self.started_ns,
            "updated_ns": time.time_ns(),
        }
        encoded = _encode_signed(body, self.key, 16_384)
        _atomic_write(
            self.paths.base,
            self.paths.heartbeat.name,
            encoded,
            replace=True,
        )

    def raise_if_failed(self) -> None:
        if self.error is not None:
            raise BrokerError("broker heartbeat writer failed") from self.error


def _encode_signed(body: Mapping[str, Any], key: bytes, maximum: int) -> bytes:
    body_object = _validated_object(body, "signed body")
    signature = hmac.new(key, _canonical_bytes(body_object), hashlib.sha256).hexdigest()
    envelope = dict(body_object)
    envelope["signature"] = signature
    encoded = _canonical_bytes(envelope) + b"\n"
    if len(encoded) > maximum:
        raise ProtocolError(f"message exceeds {maximum} bytes")
    return encoded


def _decode_signed(
    raw: bytes,
    key: bytes,
    expected_fields: set[str],
    expected_type: str,
) -> dict[str, Any]:
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except UnicodeDecodeError as error:
        raise ProtocolError("message is not UTF-8") from error
    except json.JSONDecodeError as error:
        raise ProtocolError("message is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise ProtocolError("message must be a JSON object")
    if set(decoded) != expected_fields:
        raise ProtocolError("message has missing or unknown fields")
    signature = decoded.get("signature")
    if not isinstance(signature, str) or not _SIGNATURE_RE.fullmatch(signature):
        raise SignatureError("message signature is malformed")
    body = dict(decoded)
    del body["signature"]
    expected = hmac.new(key, _canonical_bytes(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise SignatureError("message signature is invalid")
    if body.get("type") != expected_type:
        raise ProtocolError(f"expected {expected_type} message")
    _validate_json_value(body)
    return body


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ProtocolError("message contains unsupported JSON values") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ProtocolError(f"invalid JSON number: {value}")


def _validated_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{field} must be a JSON object")
    result = dict(value)
    _validate_json_value(result)
    return result


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ProtocolError("JSON nesting is too deep")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if not -(2**63) <= value < 2**63:
            raise ProtocolError("JSON integer is outside the signed 64-bit range")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError("JSON number must be finite")
        return
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise ProtocolError("JSON array has too many items")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise ProtocolError("JSON object has too many fields")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256 or "\x00" in key:
                raise ProtocolError("JSON object key is invalid")
            _validate_json_value(item, depth=depth + 1)
        return
    raise ProtocolError("message contains a non-JSON value")


def _validated_error(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"code", "message"}:
        raise ProtocolError("error must contain exactly code and message")
    code = value["code"]
    message = value["message"]
    if not isinstance(code, str) or not _ACTION_RE.fullmatch(code):
        raise ProtocolError("error code is invalid")
    if not isinstance(message, str) or not message or len(message) > 4096:
        raise ProtocolError("error message is invalid")
    return {"code": code, "message": message}


def _assert_token_absent(value: Mapping[str, Any], key: bytes) -> None:
    try:
        token = key.decode("utf-8")
    except UnicodeDecodeError:
        return
    if token and token in _canonical_bytes(value).decode("utf-8"):
        raise ProtocolError("mailbox payload must not contain the owner token")


def _validate_paths(paths: MailboxPaths) -> None:
    if not isinstance(paths, MailboxPaths):
        raise TypeError("paths must be MailboxPaths")
    if paths.base.parent != paths.job_dir:
        raise MailboxSecurityError("broker base is outside the job directory")
    expected = {
        paths.inbox: paths.base,
        paths.processing: paths.base,
        paths.outbox: paths.base,
        paths.lock: paths.base,
        paths.queue_lock: paths.inbox,
        paths.heartbeat: paths.base,
    }
    for path, parent in expected.items():
        if path.parent != parent:
            raise MailboxSecurityError("mailbox path escaped its expected directory")
    for directory in (
        paths.job_dir,
        paths.base,
        paths.inbox,
        paths.processing,
        paths.outbox,
    ):
        _open_private_dir(directory).close()


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    try:
        path = Path(value).expanduser()
    except TypeError as error:
        raise MailboxSecurityError("state root must be a filesystem path") from error
    return Path(os.path.abspath(os.fspath(path)))


def _ensure_private_dir(path: Path) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    _open_private_dir(path, fix_mode=True).close()


def _open_private_dir(path: Path, *, fix_mode: bool = False):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise MailboxSecurityError(f"mailbox directory is unavailable: {path}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise MailboxSecurityError(f"mailbox path is not a directory: {path}")
        if fix_mode:
            os.fchmod(descriptor, 0o700)
            info = os.fstat(descriptor)
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise MailboxSecurityError(f"mailbox directory is not private: {path}")
        return _DirectoryHandle(descriptor)
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _file_lock(
    directory: Path, name: str, *, blocking: bool
) -> Iterator[None]:
    directory_handle = _open_private_dir(directory)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            descriptor = os.open(
                name,
                flags,
                0o600,
                dir_fd=directory_handle.fileno(),
            )
        except OSError as error:
            raise MailboxSecurityError(f"mailbox lock is unsafe: {name}") from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise MailboxSecurityError(f"mailbox lock is not a private regular file: {name}")
            os.fchmod(descriptor, 0o600)
            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(descriptor, operation)
            except OSError as error:
                if not blocking and error.errno in {errno.EACCES, errno.EAGAIN}:
                    raise BrokerAlreadyRunning("another broker owns this job") from error
                raise
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    finally:
        directory_handle.close()


def _atomic_write(
    directory: Path,
    filename: str,
    content: bytes,
    *,
    replace: bool,
) -> None:
    if Path(filename).name != filename or not filename:
        raise MailboxSecurityError("mailbox filename is invalid")
    directory_handle = _open_private_dir(directory)
    temporary = f".{filename}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        if not replace and _entry_exists_at(directory_handle.fileno(), filename):
            raise DuplicateMessageError(f"mailbox entry already exists: {filename}")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            temporary,
            flags,
            0o600,
            dir_fd=directory_handle.fileno(),
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short mailbox write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if replace:
            os.replace(
                temporary,
                filename,
                src_dir_fd=directory_handle.fileno(),
                dst_dir_fd=directory_handle.fileno(),
            )
        else:
            if _entry_exists_at(directory_handle.fileno(), filename):
                raise DuplicateMessageError(f"mailbox entry already exists: {filename}")
            os.rename(
                temporary,
                filename,
                src_dir_fd=directory_handle.fileno(),
                dst_dir_fd=directory_handle.fileno(),
            )
        os.fsync(directory_handle.fileno())
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_handle.fileno())
        except FileNotFoundError:
            pass
        directory_handle.close()


def _message_files(directory: Path) -> list[Path]:
    handle = _open_private_dir(directory)
    handle.close()
    result: list[Path] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if not entry.name.endswith(".json"):
                continue
            _id_from_filename(entry.name)
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                raise MailboxSecurityError(f"mailbox entry is not a regular file: {entry.name}")
            info = entry.stat(follow_symlinks=False)
            if info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
                raise MailboxSecurityError(f"mailbox entry is not private: {entry.name}")
            result.append(Path(entry.path))
    return sorted(result, key=lambda path: path.name)


def _read_regular_file(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise MailboxSecurityError(f"mailbox file is unavailable: {path.name}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise MailboxSecurityError(f"mailbox file is not a regular private file: {path.name}")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise MailboxSecurityError(f"mailbox file is not mode 0600: {path.name}")
        if info.st_size > maximum:
            raise ProtocolError(f"mailbox file exceeds {maximum} bytes")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > maximum:
            raise ProtocolError(f"mailbox file exceeds {maximum} bytes")
        return content
    finally:
        os.close(descriptor)


def _read_optional_file(directory: Path, filename: str, maximum: int) -> bytes | None:
    path = directory / filename
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    return _read_regular_file(path, maximum)


def _entry_exists(directory: Path, filename: str) -> bool:
    handle = _open_private_dir(directory)
    try:
        return _entry_exists_at(handle.fileno(), filename)
    finally:
        handle.close()


def _entry_exists_at(directory_fd: int, filename: str) -> bool:
    try:
        info = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        raise MailboxSecurityError(f"mailbox entry is a symlink: {filename}")
    return True


def _token_bytes(owner_token: str | bytes) -> bytes:
    if isinstance(owner_token, str):
        token = owner_token.encode("utf-8")
    elif isinstance(owner_token, bytes):
        token = owner_token
    else:
        raise TypeError("owner_token must be str or bytes")
    if not token or len(token) > 4096 or b"\x00" in token:
        raise ProtocolError("owner token is invalid")
    return token


def _validate_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ProtocolError(f"{field} is invalid")
    return value


def _validate_action(value: Any) -> str:
    if not isinstance(value, str) or not _ACTION_RE.fullmatch(value):
        raise ProtocolError("action is invalid")
    return value


def _validate_timestamp(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProtocolError(f"{field} must be a non-negative integer")
    return value


def _validate_positive(value: Any, field: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field} must be a positive finite number")


def _validate_handler(handler: BrokerHandler) -> None:
    if not callable(handler):
        raise TypeError("handler must be callable")


def _message_filename(request_id: str) -> str:
    return f"{_validate_id(request_id, 'request_id')}.json"


def _id_from_filename(filename: str) -> str:
    if Path(filename).name != filename or not filename.endswith(".json"):
        raise ProtocolError("mailbox message filename is invalid")
    return _validate_id(filename[:-5], "request_id")


__all__ = [
    "BrokerAlreadyRunning",
    "BrokerError",
    "BrokerRequest",
    "BrokerResponse",
    "BrokerTimeout",
    "BrokerUnavailable",
    "DuplicateMessageError",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "MailboxPaths",
    "MailboxSecurityError",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "SignatureError",
    "StaleHeartbeatError",
    "prepare_mailbox",
    "mailbox_paths",
    "serve_forever",
    "serve_one",
    "submit",
    "wait",
]
