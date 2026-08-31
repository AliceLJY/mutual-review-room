"""Provider-native, read-only review invocations.

This module deliberately has a small built-in provider surface: Claude Code,
Codex CLI, and Kimi CLI.  It does not assign a provider to an owner or reviewer
role; the caller chooses the provider for each role and persists the returned
native session id.
"""

from __future__ import annotations

import json
import os
import re
import resource
import shutil
import stat
import subprocess
import tempfile
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Sequence


_BUILTIN_PROVIDERS = ("claude", "codex", "kimi")
_CONTROL_ENV_PREFIXES = ("MRR_",)
_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "ALL_PROXY",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "CURL_CA_BUNDLE",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "KIMI_CODE_HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "NODE_EXTRA_CA_CERTS",
        "NO_PROXY",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "TZ",
        "USER",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
# Every execution/delegation/media surface an isolated Codex reviewer asks the
# CLI to turn off.  It is also the exact list that is read back, so a request
# can never silently drift away from what is verified.
_CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "multi_agent",
    "view_image",
    "apps",
    "browser_use",
    "computer_use",
    "image_generation",
    "skill_search",
)
_CODEX_FEATURE_STATES = {"true": True, "false": False}
_SEATBELT_PROFILE_HEADER = "(version 1)\n(allow default)"
_PROVIDER_SAFETY_UNSUPPORTED = "provider reviewer safety controls are unsupported"
_KIMI_REVIEWER_PROFILE = """---
name: mutual-review-room-cold-reviewer
description: Isolated cold reviewer for an owner-led mutual review
tools: []
subagents: []
disallowedTools:
  - select_tools
  - "mcp__*"
---
You are one independent reviewer in an owner-led mutual-review room.

The current user message is the complete task envelope for this turn. On a
follow-up, you may also use earlier messages from this same reviewer session.
Do not seek or apply user-level or project-level instructions, memories,
skills, MCP servers, subagents, or tools. Do not modify files or external
state. Return your complete review as the final assistant message.
"""


@dataclass(frozen=True)
class ProviderResult:
    """One provider answer plus the provider's stable native session id."""

    session_id: str
    response: str
    visible_chunks: tuple[str, ...]
    command_kind: str


class _ProviderError(RuntimeError):
    """Base class whose public details never contain provider stderr."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category
        self.message = message


class ProviderUnavailable(_ProviderError):
    """The selected provider cannot currently accept an invocation."""


class ProviderFailed(_ProviderError):
    """The provider was invoked but did not produce a trustworthy answer."""


def provider_available(provider: str) -> bool:
    """Return whether a supported provider executable is discoverable."""

    normalized = provider.strip().lower() if isinstance(provider, str) else ""
    return normalized in _BUILTIN_PROVIDERS and shutil.which(normalized) is not None


def reviewer_isolation_available() -> bool:
    """Return whether the fail-closed reviewer filesystem boundary exists."""

    executable = shutil.which("sandbox-exec")
    return executable is not None and _probe_sandbox_exec(executable)


@lru_cache(maxsize=4)
def _probe_sandbox_exec(executable: str) -> bool:
    """Verify that Seatbelt can apply a harmless profile, not just that it exists."""

    try:
        completed = subprocess.run(
            [executable, "-p", _SEATBELT_PROFILE_HEADER, "/usr/bin/true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def codex_feature_readback() -> dict[str, Any]:
    """Report which reviewer feature overrides the Codex CLI actually applies.

    Requesting ``features.<name>=false`` is not proof that the surface is off:
    Codex CLI 0.151.0 accepts ``features.unified_exec=false`` without an error
    and still reports the feature as enabled.  Nothing in the invocation path
    fails, so a text-matching failure classifier cannot notice it.  The same
    overrides are therefore replayed through ``codex features list`` and the
    effective state is read back.

    ``still_enabled`` lists features the CLI reports as on despite the
    override.  ``unreported`` lists requested features absent from the listing,
    which is treated as unverified rather than as success.
    """

    executable = shutil.which("codex")
    if executable is None:
        return {"verified": False, "reason": "provider executable is unavailable"}
    return _codex_feature_readback(executable)


@lru_cache(maxsize=4)
def _codex_feature_readback(executable: str) -> dict[str, Any]:
    argv = [executable, "features"]
    for name in _CODEX_DISABLED_FEATURES:
        argv.extend(["-c", f"features.{name}=false"])
    argv.append("list")
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env=_child_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"verified": False, "reason": "feature readback did not complete"}
    if completed.returncode != 0:
        return {"verified": False, "reason": "feature readback was rejected"}

    observed: dict[str, bool] = {}
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        state = _CODEX_FEATURE_STATES.get(fields[-1])
        if state is not None and fields[0] not in observed:
            observed[fields[0]] = state
    if not observed:
        return {"verified": False, "reason": "feature readback was unreadable"}

    still_enabled = tuple(
        name
        for name in _CODEX_DISABLED_FEATURES
        if observed.get(name) is True
    )
    unreported = tuple(
        name for name in _CODEX_DISABLED_FEATURES if name not in observed
    )
    return {
        "verified": True,
        "still_enabled": still_enabled,
        "unreported": unreported,
    }


def _codex_tool_isolation() -> dict[str, Any]:
    """Describe the Codex reviewer tool boundary as measured, not as requested."""

    readback = codex_feature_readback()
    summary: dict[str, Any] = {
        "requested_disabled_features": list(_CODEX_DISABLED_FEATURES),
        "feature_state_verified": bool(readback.get("verified")),
    }
    if not readback.get("verified"):
        summary["tool_access"] = "unverified"
        summary["tool_isolation"] = (
            "read-only CLI sandbox plus Seatbelt path isolation; "
            "the effective feature state could not be read back "
            f"({readback.get('reason', 'unknown reason')})"
        )
        return summary

    still_enabled = list(readback.get("still_enabled", ()))
    unreported = list(readback.get("unreported", ()))
    summary["features_still_enabled"] = still_enabled
    summary["features_unreported"] = unreported
    if not still_enabled and not unreported:
        summary["tool_access"] = "none"
        summary["tool_isolation"] = (
            "every requested feature override is confirmed disabled, plus a "
            "read-only CLI sandbox and Seatbelt path isolation"
        )
        return summary

    summary["tool_access"] = "sandboxed-residual"
    residual = ", ".join(still_enabled + unreported)
    summary["tool_isolation"] = (
        "the CLI does not apply every requested feature override "
        f"({residual}); the remaining boundary is the read-only CLI sandbox "
        "and Seatbelt path isolation"
    )
    return summary


def provider_capabilities() -> dict[str, dict[str, Any]]:
    """Describe the intentionally small set of built-in provider adapters."""

    isolation = "macos-seatbelt" if reviewer_isolation_available() else "unavailable"
    return {
        "claude": {
            "builtin": True,
            "available": provider_available("claude"),
            "provider_native_sessions": True,
            "read_only": True,
            "visible_output": "result",
            "prompt_transport": "stdin",
            "model_override": True,
            "filesystem_isolation": isolation,
        },
        "codex": {
            "builtin": True,
            "available": provider_available("codex"),
            "provider_native_sessions": True,
            "read_only": True,
            "visible_output": "agent_message",
            "prompt_transport": "stdin",
            "model_override": True,
            "filesystem_isolation": isolation,
            **_codex_tool_isolation(),
        },
        "kimi": {
            "builtin": True,
            "available": provider_available("kimi"),
            "provider_native_sessions": True,
            "read_only": True,
            "visible_output": "assistant_message",
            "prompt_transport": "argv",
            "model_override": False,
            "filesystem_isolation": isolation,
            "context_isolation": "bound custom agent without base prompt",
            "tool_access": "none",
            "note": (
                "the current Kimi CLI requires --prompt argv transport; "
                "a no-tools custom agent is bound on the first turn and restored on resume"
            ),
        },
    }


def invoke(
    provider: str,
    prompt: str,
    *,
    cwd: str | os.PathLike[str],
    session_id: str | None = None,
    model: str | None = None,
    timeout: float = 300.0,
    deny_paths: Sequence[str | os.PathLike[str]] = (),
    isolated_reviewer: bool = True,
    writable_dirs: Sequence[str | os.PathLike[str]] = (),
) -> ProviderResult:
    """Invoke a built-in provider without exposing control data to the child.

    Claude and Codex receive ``prompt`` only through stdin.  The current Kimi
    CLI requires ``--prompt`` argv transport; this limitation is surfaced by
    :func:`provider_capabilities`.  Its reviewer path binds a custom agent whose
    prompt omits inherited instructions and whose tool allowlist is empty.  A
    continuation must report exactly the same provider-native session id or the
    result is rejected.  Codex disables every available execution, delegation,
    image-read, and web-search surface through strict config overrides before
    using its read-only sandbox.  ``isolated_reviewer=False`` is reserved for
    the trusted interactive-owner bootstrap.
    """

    normalized = provider.strip().lower() if isinstance(provider, str) else ""
    if normalized not in _BUILTIN_PROVIDERS:
        raise ProviderUnavailable("missing", "provider is not built in")
    if not isinstance(prompt, str):
        raise TypeError("prompt must be a string")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if session_id is not None:
        _validate_session_id(session_id)
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("model must be a non-empty string")
    if normalized == "kimi" and model is not None:
        raise ProviderFailed("failed", "provider model override is unsupported")

    executable = shutil.which(normalized)
    if executable is None:
        raise ProviderUnavailable("missing", "provider executable is unavailable")

    working_dir = os.fspath(Path(cwd).expanduser())
    owner_writable_dirs = tuple(
        os.fspath(Path(path).expanduser().resolve()) for path in writable_dirs
    )
    if owner_writable_dirs and (isolated_reviewer or normalized != "codex"):
        raise ValueError("additional writable directories are only valid for a Codex owner")
    for path in owner_writable_dirs:
        candidate = Path(path)
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError("additional writable directory is unavailable")
    with ExitStack() as resources:
        if normalized == "claude":
            expected_session_id = session_id or str(uuid.uuid4())
            argv = _claude_command(
                executable,
                expected_session_id,
                resume=session_id is not None,
                model=model,
            )
        elif normalized == "codex":
            expected_session_id = session_id
            argv = _codex_command(
                executable,
                working_dir,
                session_id=session_id,
                model=model,
                isolated_reviewer=isolated_reviewer,
                writable_dirs=owner_writable_dirs,
            )
        else:
            expected_session_id = session_id
            agent_file = None
            if isolated_reviewer and session_id is None:
                agent_file = _kimi_reviewer_profile(working_dir)
            skills_dir = resources.enter_context(_private_empty_skills_dir())
            argv = _kimi_command(
                executable,
                prompt=prompt,
                session_id=session_id,
                skills_dir=skills_dir,
                agent_file=agent_file,
            )

        stdin_payload = "" if normalized == "kimi" else prompt
        effective_deny_paths = list(deny_paths)
        if normalized == "kimi" and isolated_reviewer:
            effective_deny_paths.extend(_kimi_global_context_paths())
        stdout = _run(
            argv,
            prompt=stdin_payload,
            cwd=working_dir,
            timeout=timeout,
            deny_paths=effective_deny_paths,
        )
    if normalized == "claude":
        return _parse_claude(
            stdout,
            expected_session_id=expected_session_id,
            command_kind="resume" if session_id is not None else "start",
        )
    if normalized == "codex":
        return _parse_codex(
            stdout,
            expected_session_id=expected_session_id,
            command_kind="resume" if session_id is not None else "start",
        )
    return _parse_kimi(
        stdout,
        expected_session_id=expected_session_id,
        command_kind="resume" if session_id is not None else "start",
    )


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise ProviderFailed("failed", "provider session identity is invalid")


def _claude_command(
    executable: str,
    session_id: str,
    *,
    resume: bool,
    model: str | None,
) -> list[str]:
    command = [executable, "--resume" if resume else "--session-id", session_id]
    command.extend(
        [
            "--print",
            "--output-format",
            "json",
            "--safe-mode",
            "--strict-mcp-config",
            # No --permission-mode plan here: read-only is already enforced by
            # --tools "" plus the Seatbelt profile, while plan mode makes a
            # --print reviewer answer with "I'll write a plan and request
            # approval" instead of findings (verified 2026-09-01, see README).
            "--tools",
            "",
        ]
    )
    if model is not None:
        command.extend(["--model", model])
    return command


def _codex_command(
    executable: str,
    cwd: str,
    *,
    session_id: str | None,
    model: str | None,
    isolated_reviewer: bool = True,
    writable_dirs: Sequence[str] = (),
) -> list[str]:
    command = [executable, "exec", "--json", "--color", "never"]
    if isolated_reviewer:
        command.extend(
            [
                "--sandbox",
                "read-only",
                "--strict-config",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
            ]
        )
        for name in _CODEX_DISABLED_FEATURES:
            command.extend(["-c", f"features.{name}=false"])
        command.extend(["-c", "shell_environment_policy.inherit=none"])
    else:
        trusted_cwd = os.fspath(Path(cwd).resolve())
        project_trust = (
            f"projects.{json.dumps(trusted_cwd, ensure_ascii=False)}"
            '.trust_level="trusted"'
        )
        command.extend(
            [
                "--approve-for-me",
                "--skip-git-repo-check",
                "-c",
                project_trust,
            ]
        )
        for path in writable_dirs:
            command.extend(["--add-dir", path])
    if model is not None:
        command.extend(["--model", model])

    if session_id is None:
        command.extend(["-C", cwd, "-"])
        return command

    command.extend(["resume", session_id, "-"])
    return command


def _kimi_command(
    executable: str,
    *,
    prompt: str,
    session_id: str | None,
    skills_dir: str,
    agent_file: str | None,
) -> list[str]:
    command = [executable]
    if session_id is not None:
        command.extend(["--session", session_id])
    elif agent_file is not None:
        command.extend(["--agent-file", agent_file])
    command.extend(
        [
            "--prompt",
            prompt,
            "--output-format",
            "stream-json",
            "--skills-dir",
            skills_dir,
        ]
    )
    return command


def _kimi_reviewer_profile(cwd: str | os.PathLike[str]) -> str:
    """Create the immutable-input profile used to bind a new Kimi lane."""

    root = Path(cwd).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ProviderFailed("failed", "reviewer workspace is unavailable")
    path = root / ".mutual-review-room-kimi-reviewer.md"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o400)
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise ProviderFailed(
                "failed", "isolated reviewer profile is unavailable"
            ) from None
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            raise ProviderFailed(
                "failed", "isolated reviewer profile is unavailable"
            ) from None
        if existing != _KIMI_REVIEWER_PROFILE:
            raise ProviderFailed(
                "failed", "isolated reviewer profile does not match the trusted profile"
            )
    except OSError:
        raise ProviderFailed(
            "failed", "isolated reviewer profile could not be created"
        ) from None
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(_KIMI_REVIEWER_PROFILE)
    os.chmod(path, 0o400)
    return os.fspath(path)


def _kimi_global_context_paths() -> tuple[str, ...]:
    """Return existing global instruction/MCP files denied to cold Kimi lanes."""

    user_home = Path.home()
    configured_home = os.environ.get("KIMI_CODE_HOME")
    kimi_home = (
        Path(configured_home).expanduser()
        if configured_home
        else user_home / ".kimi-code"
    )
    candidates = (
        kimi_home / "AGENTS.md",
        kimi_home / "mcp.json",
        user_home / ".agents" / "AGENTS.md",
    )
    resolved: list[str] = []
    for candidate in candidates:
        try:
            path = candidate.resolve(strict=True)
        except OSError:
            continue
        if path.is_file() and os.fspath(path) not in resolved:
            resolved.append(os.fspath(path))
    return tuple(resolved)


@contextmanager
def _private_empty_skills_dir() -> Iterator[str]:
    """Yield one invocation-private, verified-empty skills directory."""

    with tempfile.TemporaryDirectory(prefix="mutual-review-room-empty-skills-") as directory:
        path = Path(directory)
        os.chmod(path, 0o700)
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ProviderFailed("failed", "isolated skills directory is unavailable")
        if any(path.iterdir()):
            raise ProviderFailed("failed", "isolated skills directory is not empty")
        yield os.fspath(path)


def _child_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key in _CHILD_ENV_ALLOWLIST
        and not key.startswith(_CONTROL_ENV_PREFIXES)
    }


def _seatbelt_command(
    argv: Sequence[str], deny_paths: Sequence[str | os.PathLike[str]]
) -> list[str]:
    """Deny reviewer reads and writes to explicit control/peer paths on macOS."""

    if not deny_paths:
        return list(argv)
    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec is None or not _probe_sandbox_exec(sandbox_exec):
        raise ProviderUnavailable(
            "isolation", "reviewer filesystem isolation is unavailable"
        )

    resolved: list[str] = []
    for raw_path in deny_paths:
        try:
            path = Path(raw_path).expanduser().resolve(strict=True)
        except OSError:
            raise ProviderFailed(
                "failed", "reviewer isolation path is unavailable"
            ) from None
        if not path.is_absolute():
            raise ProviderFailed("failed", "reviewer isolation path is invalid")
        value = os.fspath(path)
        if value not in resolved:
            resolved.append(value)

    profile_lines = [_SEATBELT_PROFILE_HEADER]
    command = [sandbox_exec]
    for index, path in enumerate(resolved):
        parameter = f"MRR_DENY_{index}"
        command.extend(["-D", f"{parameter}={path}"])
        profile_lines.extend(
            [
                f'(deny file-read* (subpath (param "{parameter}")))',
                f'(deny file-write* (subpath (param "{parameter}")))',
            ]
        )
    command.extend(["-p", "\n".join(profile_lines), *argv])
    return command


def _run(
    argv: list[str],
    *,
    prompt: str,
    cwd: str,
    timeout: float,
    deny_paths: Sequence[str | os.PathLike[str]] = (),
) -> str:
    isolated_argv = _seatbelt_command(argv, deny_paths)
    original_limit: tuple[int, int] | None = None
    try:
        soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        target_limit = max(soft_limit, 4096)
        if hard_limit != resource.RLIM_INFINITY:
            target_limit = min(target_limit, hard_limit)
        if target_limit > soft_limit:
            original_limit = (soft_limit, hard_limit)
            resource.setrlimit(resource.RLIMIT_NOFILE, (target_limit, hard_limit))
        process = subprocess.Popen(
            isolated_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=_child_environment(),
            shell=False,
        )
    except (FileNotFoundError, PermissionError):
        raise ProviderUnavailable(
            "missing", "provider executable is unavailable"
        ) from None
    except OSError:
        raise ProviderFailed("failed", "provider invocation failed") from None
    finally:
        if original_limit is not None:
            resource.setrlimit(resource.RLIMIT_NOFILE, original_limit)

    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.communicate(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            pass
        raise ProviderFailed("timeout", "provider invocation timed out") from None

    if process.returncode != 0:
        category = _classify_failure(stdout, stderr)
        message = {
            "missing": "provider executable is unavailable",
            "auth": "provider authentication is unavailable",
            "quota": "provider quota is unavailable",
            "isolation": _PROVIDER_SAFETY_UNSUPPORTED,
            "failed": "provider invocation failed",
        }[category]
        error_type = (
            ProviderUnavailable
            if category in {"missing", "auth", "quota", "isolation"}
            else ProviderFailed
        )
        raise error_type(category, message)
    return stdout


def _classify_failure(stdout: str | None, stderr: str | None) -> str:
    diagnostic = f"{stdout or ''}\n{stderr or ''}".lower()
    if any(
        marker in diagnostic
        for marker in (
            "unknown configuration field",
            "unknown feature",
            "unrecognized configuration",
        )
    ):
        return "isolation"
    if any(
        marker in diagnostic
        for marker in (
            "quota",
            "rate limit",
            "rate_limit",
            "too many requests",
            "usage limit",
            "credit balance",
        )
    ):
        return "quota"
    if any(
        marker in diagnostic
        for marker in (
            "authentication",
            "unauthorized",
            "not authenticated",
            "login required",
            "api key",
            "credential",
            "forbidden",
            "http 401",
            "http 403",
        )
    ) or re.search(r"\blog in\b", diagnostic):
        return "auth"
    if any(
        marker in diagnostic
        for marker in (
            "command not found",
            "executable not found",
            "no such file or directory",
            "enoent",
        )
    ):
        return "missing"
    return "failed"


def _parse_claude(
    stdout: str,
    *,
    expected_session_id: str,
    command_kind: str,
) -> ProviderResult:
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        raise ProviderFailed("failed", "provider returned invalid structured output") from None
    if not isinstance(payload, dict):
        raise ProviderFailed("failed", "provider returned invalid structured output")

    observed_session_id = payload.get("session_id")
    response = payload.get("result")
    if observed_session_id != expected_session_id:
        raise ProviderFailed("failed", "provider session identity changed")
    if not isinstance(response, str) or not response:
        raise ProviderFailed("failed", "provider returned no visible answer")
    return ProviderResult(
        session_id=observed_session_id,
        response=response,
        visible_chunks=(response,),
        command_kind=command_kind,
    )


def _parse_codex(
    stdout: str,
    *,
    expected_session_id: str | None,
    command_kind: str,
) -> ProviderResult:
    observed_session_ids: list[str] = []
    visible_chunks: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise ProviderFailed(
                "failed", "provider returned invalid structured output"
            ) from None
        if not isinstance(event, dict):
            raise ProviderFailed("failed", "provider returned invalid structured output")
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str):
                observed_session_ids.append(thread_id)
        elif event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    visible_chunks.append(text)

    if not observed_session_ids or len(set(observed_session_ids)) != 1:
        raise ProviderFailed("failed", "provider session identity is missing or changed")
    observed_session_id = observed_session_ids[0]
    if expected_session_id is not None and observed_session_id != expected_session_id:
        raise ProviderFailed("failed", "provider session identity changed")
    if not visible_chunks:
        raise ProviderFailed("failed", "provider returned no visible answer")
    return ProviderResult(
        session_id=observed_session_id,
        response="\n".join(visible_chunks),
        visible_chunks=tuple(visible_chunks),
        command_kind=command_kind,
    )


def _parse_kimi(
    stdout: str,
    *,
    expected_session_id: str | None,
    command_kind: str,
) -> ProviderResult:
    observed_session_ids: list[str] = []
    visible_chunks: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise ProviderFailed(
                "failed", "provider returned invalid structured output"
            ) from None
        if not isinstance(event, dict):
            raise ProviderFailed("failed", "provider returned invalid structured output")
        if event.get("role") == "assistant":
            content = event.get("content")
            if isinstance(content, str) and content:
                visible_chunks.append(content)
        elif event.get("role") == "meta" and event.get("type") == "session.resume_hint":
            session_id = event.get("session_id")
            if isinstance(session_id, str):
                observed_session_ids.append(session_id)

    if not observed_session_ids or len(set(observed_session_ids)) != 1:
        raise ProviderFailed("failed", "provider session identity is missing or changed")
    observed_session_id = observed_session_ids[0]
    if expected_session_id is not None and observed_session_id != expected_session_id:
        raise ProviderFailed("failed", "provider session identity changed")
    if not visible_chunks:
        raise ProviderFailed("failed", "provider returned no visible answer")
    return ProviderResult(
        session_id=observed_session_id,
        response="\n".join(visible_chunks),
        visible_chunks=tuple(visible_chunks),
        command_kind=command_kind,
    )


__all__ = [
    "ProviderFailed",
    "ProviderResult",
    "ProviderUnavailable",
    "invoke",
    "provider_available",
    "provider_capabilities",
]
