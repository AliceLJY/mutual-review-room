import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from mutual_review_room import runtime as review_runtime


CLAUDE_SESSION = "12345678-1234-5678-1234-567812345678"
CODEX_SESSION = "87654321-4321-8765-4321-876543218765"
KIMI_SESSION = "session_7f9ffb58-ddde-453a-8c60-54859d0bf83d"


class FakeProcess:
    def __init__(self, *, stdout="", stderr="", returncode=0, times_out=False):
        self.stdout_value = stdout
        self.stderr_value = stderr
        self.returncode = returncode
        self.times_out = times_out
        self.killed = False
        self.inputs = []
        self.timeouts = []

    def communicate(self, input=None, timeout=None):
        self.inputs.append(input)
        self.timeouts.append(timeout)
        if self.times_out and not self.killed:
            raise subprocess.TimeoutExpired("provider", timeout)
        return self.stdout_value, self.stderr_value

    def kill(self):
        self.killed = True


def codex_output(session_id=CODEX_SESSION, messages=("answer",)):
    events = [
        {"type": "thread.started", "thread_id": session_id},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "private"}},
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "aggregated_output": "secret command"},
        },
    ]
    events.extend(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": message},
        }
        for message in messages
    )
    return "\n".join(json.dumps(event) for event in events)


def kimi_output(session_id=KIMI_SESSION, messages=("answer",)):
    events = [{"role": "meta", "type": "system.version", "version": "test"}]
    events.extend({"role": "assistant", "content": message} for message in messages)
    events.extend(
        [
            {"role": "tool", "content": "hidden tool output"},
            {
                "role": "meta",
                "type": "session.resume_hint",
                "session_id": session_id,
            },
        ]
    )
    return "\n".join(json.dumps(event) for event in events)


class ReviewRuntimeTests(unittest.TestCase):
    def test_capabilities_are_builtin_role_neutral_and_availability_is_discovered(self):
        with patch.object(
            review_runtime.shutil,
            "which",
            side_effect=lambda name: f"/bin/{name}" if name == "claude" else None,
        ):
            capabilities = review_runtime.provider_capabilities()

        self.assertEqual({"claude", "codex", "kimi"}, set(capabilities))
        self.assertTrue(capabilities["claude"]["builtin"])
        self.assertTrue(capabilities["claude"]["available"])
        self.assertFalse(capabilities["codex"]["available"])
        self.assertFalse(capabilities["kimi"]["available"])
        self.assertEqual("stdin", capabilities["codex"]["prompt_transport"])
        self.assertEqual("argv", capabilities["kimi"]["prompt_transport"])
        self.assertFalse(capabilities["kimi"]["model_override"])
        self.assertTrue(capabilities["kimi"]["read_only"])
        self.assertEqual("none", capabilities["kimi"]["tool_access"])
        self.assertIn("requires --prompt argv", capabilities["kimi"]["note"])
        self.assertEqual(
            "unavailable", capabilities["codex"]["filesystem_isolation"]
        )
        self.assertNotIn("owner", repr(capabilities).lower())
        self.assertNotIn("reviewer", repr(capabilities).lower())

    def test_claude_start_uses_stdin_safe_flags_and_strips_control_environment(self):
        process = FakeProcess(
            stdout=json.dumps({"session_id": CLAUDE_SESSION, "result": "visible answer"})
        )
        with (
            patch.object(review_runtime.shutil, "which", return_value="/bin/claude"),
            patch.object(review_runtime.uuid, "uuid4", return_value=uuid.UUID(CLAUDE_SESSION)),
            patch.object(review_runtime.subprocess, "Popen", return_value=process) as popen,
            patch.dict(
                review_runtime.os.environ,
                {
                    "PATH": "/bin",
                    "HOME": "/Users/tester",
                    "CLAUDE_CONFIG_DIR": "/Users/tester/.claude-test",
                    "LANG": "en_US.UTF-8",
                    "HTTPS_PROXY": "http://proxy.invalid:8080",
                    "SSL_CERT_FILE": "/tmp/test-ca.pem",
                    "OPENAI_API_KEY": "removed",
                    "GITHUB_TOKEN": "removed",
                    "PROVIDER_AUTH": "removed",
                    "UNRELATED_VALUE": "removed",
                    "MRR_OWNER_TOKEN": "removed",
                    "MRR_OWNER_TOKEN_FILE": "removed",
                    "MRR_JOB_ID": "removed",
                    "MRR_CUSTOM_CONTROL": "removed",
                },
                clear=True,
            ),
        ):
            result = review_runtime.invoke(
                "claude", "cold-read prompt", cwd="/work/project", timeout=17
            )

        argv = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertEqual(
            [
                "/bin/claude",
                "--session-id",
                CLAUDE_SESSION,
                "--print",
                "--output-format",
                "json",
                "--safe-mode",
                "--strict-mcp-config",
                "--permission-mode",
                "plan",
                "--tools",
                "",
            ],
            argv,
        )
        self.assertNotIn("cold-read prompt", argv)
        self.assertEqual(["cold-read prompt"], process.inputs)
        self.assertEqual([17], process.timeouts)
        self.assertEqual("/work/project", kwargs["cwd"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(
            {
                "CLAUDE_CONFIG_DIR": "/Users/tester/.claude-test",
                "HOME": "/Users/tester",
                "HTTPS_PROXY": "http://proxy.invalid:8080",
                "LANG": "en_US.UTF-8",
                "PATH": "/bin",
                "SSL_CERT_FILE": "/tmp/test-ca.pem",
            },
            kwargs["env"],
        )
        self.assertNotIn("PROVIDER_AUTH", kwargs["env"])
        self.assertFalse(
            any("TOKEN" in key or "KEY" in key for key in kwargs["env"])
        )
        self.assertFalse(any(key.startswith("MRR_") for key in kwargs["env"]))
        self.assertEqual(CLAUDE_SESSION, result.session_id)
        self.assertEqual(("visible answer",), result.visible_chunks)
        self.assertEqual("start", result.command_kind)

    def test_claude_resume_keeps_native_session_and_accepts_optional_model(self):
        first = FakeProcess(
            stdout=json.dumps({"session_id": CLAUDE_SESSION, "result": "first"})
        )
        follow_up = FakeProcess(
            stdout=json.dumps({"session_id": CLAUDE_SESSION, "result": "second"})
        )
        with (
            patch.object(review_runtime.shutil, "which", return_value="/bin/claude"),
            patch.object(review_runtime.uuid, "uuid4", return_value=uuid.UUID(CLAUDE_SESSION)),
            patch.object(
                review_runtime.subprocess, "Popen", side_effect=[first, follow_up]
            ) as popen,
        ):
            initial = review_runtime.invoke("claude", "one", cwd="/work")
            resumed = review_runtime.invoke(
                "claude",
                "two",
                cwd="/work",
                session_id=initial.session_id,
                model="claude-test",
            )

        resume_argv = popen.call_args_list[1].args[0]
        self.assertEqual("--resume", resume_argv[1])
        self.assertEqual(CLAUDE_SESSION, resume_argv[2])
        self.assertEqual(["--model", "claude-test"], resume_argv[-2:])
        self.assertEqual(CLAUDE_SESSION, resumed.session_id)
        self.assertEqual("resume", resumed.command_kind)
        self.assertEqual(["two"], follow_up.inputs)

    def test_claude_rejects_session_drift(self):
        process = FakeProcess(
            stdout=json.dumps({"session_id": CODEX_SESSION, "result": "answer"})
        )
        with (
            patch.object(review_runtime.shutil, "which", return_value="/bin/claude"),
            patch.object(review_runtime.subprocess, "Popen", return_value=process),
        ):
            with self.assertRaises(review_runtime.ProviderFailed) as raised:
                review_runtime.invoke(
                    "claude", "follow up", cwd="/work", session_id=CLAUDE_SESSION
                )

        self.assertEqual("failed", raised.exception.category)
        self.assertNotIn(CODEX_SESSION, str(raised.exception))

    def test_codex_start_uses_read_only_json_and_only_exposes_agent_messages(self):
        process = FakeProcess(stdout=codex_output(messages=("part one", "part two")))
        with (
            patch.object(review_runtime.shutil, "which", return_value="/bin/codex"),
            patch.object(review_runtime.subprocess, "Popen", return_value=process) as popen,
        ):
            result = review_runtime.invoke(
                "codex", "review this", cwd="/work/project", model="gpt-test"
            )

        argv = popen.call_args.args[0]
        self.assertEqual(
            [
                "/bin/codex",
                "exec",
                "--json",
                "--color",
                "never",
                "--sandbox",
                "read-only",
                "--strict-config",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "-c",
                "features.shell_tool=false",
                "-c",
                "features.unified_exec=false",
                "-c",
                "features.multi_agent=false",
                "-c",
                "features.view_image=false",
                "-c",
                "features.apps=false",
                "-c",
                "features.browser_use=false",
                "-c",
                "features.computer_use=false",
                "-c",
                "features.image_generation=false",
                "-c",
                "features.skill_search=false",
                "-c",
                "shell_environment_policy.inherit=none",
                "--model",
                "gpt-test",
                "-C",
                "/work/project",
                "-",
            ],
            argv,
        )
        self.assertNotIn("review this", argv)
        self.assertEqual(["review this"], process.inputs)
        self.assertEqual(CODEX_SESSION, result.session_id)
        self.assertEqual(("part one", "part two"), result.visible_chunks)
        self.assertEqual("part one\npart two", result.response)
        self.assertNotIn("private", result.response)
        self.assertNotIn("secret command", result.response)

    def test_codex_resume_uses_same_native_thread_and_read_only_safety_flags(self):
        first = FakeProcess(stdout=codex_output(messages=("first",)))
        follow_up = FakeProcess(stdout=codex_output(messages=("follow-up",)))
        with (
            patch.object(review_runtime.shutil, "which", return_value="/bin/codex"),
            patch.object(
                review_runtime.subprocess, "Popen", side_effect=[first, follow_up]
            ) as popen,
        ):
            initial = review_runtime.invoke("codex", "one", cwd="/work")
            resumed = review_runtime.invoke(
                "codex",
                "two",
                cwd="/work",
                session_id=initial.session_id,
                model="gpt-test",
            )

        self.assertEqual(
            [
                "/bin/codex",
                "exec",
                "--json",
                "--color",
                "never",
                "--sandbox",
                "read-only",
                "--strict-config",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "-c",
                "features.shell_tool=false",
                "-c",
                "features.unified_exec=false",
                "-c",
                "features.multi_agent=false",
                "-c",
                "features.view_image=false",
                "-c",
                "features.apps=false",
                "-c",
                "features.browser_use=false",
                "-c",
                "features.computer_use=false",
                "-c",
                "features.image_generation=false",
                "-c",
                "features.skill_search=false",
                "-c",
                "shell_environment_policy.inherit=none",
                "--model",
                "gpt-test",
                "resume",
                CODEX_SESSION,
                "-",
            ],
            popen.call_args_list[1].args[0],
        )
        self.assertEqual("/work", popen.call_args_list[1].kwargs["cwd"])
        self.assertEqual(CODEX_SESSION, resumed.session_id)
        self.assertEqual("resume", resumed.command_kind)

    def test_codex_owner_bootstrap_uses_final_trust_and_bounded_writable_root(self):
        process = FakeProcess(stdout=codex_output(messages=("owner ready",)))
        with tempfile.TemporaryDirectory(prefix="review-owner-") as directory:
            root = Path(directory)
            workspace = root / "Owner Project"
            inbox = root / "broker" / "inbox"
            workspace.mkdir()
            inbox.mkdir(parents=True)
            with (
                patch.object(review_runtime.shutil, "which", return_value="/bin/codex"),
                patch.object(
                    review_runtime.subprocess, "Popen", return_value=process
                ) as popen,
            ):
                result = review_runtime.invoke(
                    "codex",
                    "load owner contract",
                    cwd=workspace,
                    isolated_reviewer=False,
                    writable_dirs=[inbox],
                )

        argv = popen.call_args.args[0]
        self.assertIn("--approve-for-me", argv)
        self.assertEqual(
            f'projects."{workspace.resolve()}".trust_level="trusted"',
            argv[argv.index("-c") + 1],
        )
        self.assertEqual(str(inbox.resolve()), argv[argv.index("--add-dir") + 1])
        self.assertNotIn("--ignore-user-config", argv)
        self.assertNotIn("features.shell_tool=false", argv)
        self.assertNotIn("danger-full-access", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(CODEX_SESSION, result.session_id)

    def test_codex_rejects_thread_drift(self):
        process = FakeProcess(stdout=codex_output(session_id=CLAUDE_SESSION))
        with (
            patch.object(review_runtime.shutil, "which", return_value="/bin/codex"),
            patch.object(review_runtime.subprocess, "Popen", return_value=process),
        ):
            with self.assertRaises(review_runtime.ProviderFailed) as raised:
                review_runtime.invoke(
                    "codex", "follow up", cwd="/work", session_id=CODEX_SESSION
                )

        self.assertEqual("failed", raised.exception.category)
        self.assertNotIn(CLAUDE_SESSION, str(raised.exception))

    def test_kimi_start_uses_current_prompt_argv_and_returns_native_session(self):
        process = FakeProcess(stdout=kimi_output(messages=("visible one", "visible two")))
        with (
            patch.object(review_runtime.shutil, "which", return_value="/bin/kimi"),
            patch.object(
                review_runtime,
                "_kimi_reviewer_profile",
                return_value="/isolated/reviewer.md",
            ),
            patch.object(review_runtime, "_kimi_global_context_paths", return_value=()),
            patch.object(review_runtime.subprocess, "Popen", return_value=process) as popen,
        ):
            result = review_runtime.invoke("kimi", "private prompt", cwd="/isolated")

        argv = popen.call_args.args[0]
        skills_dir = Path(argv[argv.index("--skills-dir") + 1])
        self.assertEqual(argv[:2], ["/bin/kimi", "--agent-file"])
        self.assertEqual(argv[2], "/isolated/reviewer.md")
        self.assertEqual(
            argv[3:7],
            ["--prompt", "private prompt", "--output-format", "stream-json"],
        )
        self.assertIn("mutual-review-room-empty-skills-", skills_dir.name)
        self.assertFalse(skills_dir.exists())
        self.assertEqual([""], process.inputs)
        self.assertEqual(result.session_id, KIMI_SESSION)
        self.assertEqual(result.response, "visible one\nvisible two")
        self.assertNotIn("hidden tool output", result.response)

    def test_kimi_reviewer_profile_is_self_contained_and_toolless(self):
        with tempfile.TemporaryDirectory(prefix="review-kimi-profile-") as directory:
            path = Path(review_runtime._kimi_reviewer_profile(directory))
            profile = path.read_text(encoding="utf-8")

        self.assertIn("tools: []", profile)
        self.assertIn("subagents: []", profile)
        self.assertIn("select_tools", profile)
        self.assertIn('"mcp__*"', profile)
        self.assertNotIn("${base_prompt}", profile)
        self.assertNotIn("${agents_md}", profile)
        self.assertNotIn("${skills}", profile)

    def test_kimi_reviewer_profile_reuses_only_the_exact_trusted_content(self):
        with tempfile.TemporaryDirectory(prefix="review-kimi-profile-") as directory:
            first = review_runtime._kimi_reviewer_profile(directory)
            second = review_runtime._kimi_reviewer_profile(directory)
            self.assertEqual(first, second)

        with tempfile.TemporaryDirectory(prefix="review-kimi-profile-") as directory:
            path = Path(directory) / ".mutual-review-room-kimi-reviewer.md"
            path.write_text("untrusted replacement", encoding="utf-8")
            with self.assertRaises(review_runtime.ProviderFailed) as raised:
                review_runtime._kimi_reviewer_profile(directory)
            self.assertEqual("untrusted replacement", path.read_text(encoding="utf-8"))

        self.assertIn("does not match", str(raised.exception))

    def test_kimi_skills_directories_are_private_empty_unique_and_cleaned(self):
        with review_runtime._private_empty_skills_dir() as first_directory:
            first = Path(first_directory)
            self.assertTrue(first.is_dir())
            self.assertEqual(0o700, first.stat().st_mode & 0o777)
            self.assertEqual([], list(first.iterdir()))
            with review_runtime._private_empty_skills_dir() as second_directory:
                second = Path(second_directory)
                self.assertNotEqual(first, second)
                self.assertTrue(second.is_dir())
                self.assertEqual(0o700, second.stat().st_mode & 0o777)
                self.assertEqual([], list(second.iterdir()))
            self.assertFalse(second.exists())
        self.assertFalse(first.exists())

    def test_kimi_trusted_owner_bootstrap_does_not_bind_reviewer_profile(self):
        process = FakeProcess(stdout=kimi_output(messages=("owner ready",)))
        with (
            patch.object(review_runtime.shutil, "which", return_value="/bin/kimi"),
            patch.object(review_runtime, "_kimi_reviewer_profile") as profile,
            patch.object(review_runtime.subprocess, "Popen", return_value=process) as popen,
        ):
            result = review_runtime.invoke(
                "kimi",
                "load owner contract",
                cwd="/trusted-owner",
                isolated_reviewer=False,
            )

        profile.assert_not_called()
        self.assertNotIn("--agent-file", popen.call_args.args[0])
        self.assertEqual(KIMI_SESSION, result.session_id)

    def test_kimi_global_instruction_and_mcp_files_are_added_to_deny_set(self):
        with tempfile.TemporaryDirectory(prefix="review-kimi-home-") as directory:
            root = Path(directory)
            kimi_home = root / "kimi-home"
            generic_home = root / ".agents"
            kimi_home.mkdir()
            generic_home.mkdir()
            agents = kimi_home / "AGENTS.md"
            mcp = kimi_home / "mcp.json"
            generic = generic_home / "AGENTS.md"
            agents.write_text("instructions", encoding="utf-8")
            mcp.write_text("{}", encoding="utf-8")
            generic.write_text("generic", encoding="utf-8")
            with (
                patch.object(review_runtime.Path, "home", return_value=root),
                patch.dict(
                    review_runtime.os.environ,
                    {"KIMI_CODE_HOME": str(kimi_home)},
                    clear=False,
                ),
            ):
                paths = review_runtime._kimi_global_context_paths()

        self.assertEqual(
            {str(agents.resolve()), str(mcp.resolve()), str(generic.resolve())},
            set(paths),
        )

    def test_kimi_resume_keeps_session_and_rejects_drift(self):
        resumed = FakeProcess(stdout=kimi_output(messages=("follow-up",)))
        drifted = FakeProcess(stdout=kimi_output(session_id="session_other"))
        with (
            patch.object(review_runtime.shutil, "which", return_value="/bin/kimi"),
            patch.object(review_runtime, "_kimi_global_context_paths", return_value=()),
            patch.object(
                review_runtime.subprocess,
                "Popen",
                side_effect=[resumed, drifted],
            ) as popen,
        ):
            result = review_runtime.invoke(
                "kimi", "follow", cwd="/isolated", session_id=KIMI_SESSION
            )
            with self.assertRaises(review_runtime.ProviderFailed):
                review_runtime.invoke(
                    "kimi", "drift", cwd="/isolated", session_id=KIMI_SESSION
                )

        self.assertIn("--session", popen.call_args_list[0].args[0])
        self.assertIn(KIMI_SESSION, popen.call_args_list[0].args[0])
        resume_argv = popen.call_args_list[0].args[0]
        self.assertEqual(
            [
                "/bin/kimi",
                "--session",
                KIMI_SESSION,
                "--prompt",
                "follow",
                "--output-format",
                "stream-json",
                "--skills-dir",
            ],
            resume_argv[:-1],
        )
        first_skills_dir = Path(resume_argv[-1])
        second_skills_dir = Path(popen.call_args_list[1].args[0][-1])
        self.assertNotEqual(first_skills_dir, second_skills_dir)
        self.assertFalse(first_skills_dir.exists())
        self.assertFalse(second_skills_dir.exists())
        self.assertEqual(result.session_id, KIMI_SESSION)

    def test_kimi_model_override_is_rejected_before_spawning(self):
        with (
            patch.object(review_runtime.shutil, "which", return_value="/bin/kimi"),
            patch.object(review_runtime.subprocess, "Popen") as popen,
        ):
            with self.assertRaises(review_runtime.ProviderFailed) as raised:
                review_runtime.invoke(
                    "kimi", "prompt", cwd="/isolated", model="unsupported"
                )

        self.assertEqual("failed", raised.exception.category)
        self.assertIn("unsupported", str(raised.exception))
        popen.assert_not_called()

    def test_seatbelt_wraps_provider_with_explicit_read_write_denials(self):
        with tempfile.TemporaryDirectory(prefix="review-seatbelt-") as directory:
            first = Path(directory) / "control"
            second = Path(directory) / "peer"
            first.mkdir()
            second.mkdir()
            with (
                patch.object(
                    review_runtime.shutil,
                    "which",
                    side_effect=lambda name: "/usr/bin/sandbox-exec"
                    if name == "sandbox-exec"
                    else None,
                ),
                patch.object(review_runtime, "_probe_sandbox_exec", return_value=True),
            ):
                command = review_runtime._seatbelt_command(
                    ["/bin/provider", "--flag"], [first, second, first]
                )

        self.assertEqual(command[0], "/usr/bin/sandbox-exec")
        self.assertEqual(command.count("-D"), 2)
        profile = command[command.index("-p") + 1]
        self.assertIn("deny file-read*", profile)
        self.assertIn("deny file-write*", profile)
        self.assertEqual(command[-2:], ["/bin/provider", "--flag"])

    def test_seatbelt_cross_lane_read_and_write_are_denied(self):
        if not review_runtime.reviewer_isolation_available():
            self.skipTest("macOS sandbox-exec is unavailable")

        with tempfile.TemporaryDirectory(prefix="review-seatbelt-live-") as directory:
            root = Path(directory)
            own_lane = root / "own"
            peer_lane = root / "peer"
            own_lane.mkdir()
            peer_lane.mkdir()
            own_canary = own_lane / "canary.txt"
            peer_canary = peer_lane / "canary.txt"
            own_canary.write_text("own-lane-readable", encoding="utf-8")
            peer_canary.write_text("peer-lane-blocked", encoding="utf-8")

            allowed = subprocess.run(
                review_runtime._seatbelt_command(["/bin/cat", str(own_canary)], [peer_lane]),
                cwd=own_lane,
                capture_output=True,
                text=True,
                check=False,
            )
            denied_read = subprocess.run(
                review_runtime._seatbelt_command(["/bin/cat", str(peer_canary)], [peer_lane]),
                cwd=own_lane,
                capture_output=True,
                text=True,
                check=False,
            )
            denied_write = subprocess.run(
                review_runtime._seatbelt_command(
                    ["/usr/bin/touch", str(peer_lane / "created.txt")], [peer_lane]
                ),
                cwd=own_lane,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(0, allowed.returncode)
        self.assertEqual("own-lane-readable", allowed.stdout)
        self.assertNotEqual(0, denied_read.returncode)
        self.assertEqual("", denied_read.stdout)
        self.assertNotEqual(0, denied_write.returncode)

    def test_requested_isolation_fails_closed_without_sandbox_exec(self):
        with tempfile.TemporaryDirectory(prefix="review-seatbelt-") as directory:
            with patch.object(review_runtime.shutil, "which", return_value=None):
                with self.assertRaises(review_runtime.ProviderUnavailable) as raised:
                    review_runtime._seatbelt_command(["/bin/provider"], [directory])

        self.assertEqual("isolation", raised.exception.category)
        self.assertNotIn(directory, str(raised.exception))

    def test_requested_isolation_fails_closed_when_profile_cannot_be_applied(self):
        with (
            tempfile.TemporaryDirectory(prefix="review-seatbelt-") as directory,
            patch.object(
                review_runtime.shutil,
                "which",
                return_value="/usr/bin/sandbox-exec",
            ),
            patch.object(review_runtime, "_probe_sandbox_exec", return_value=False),
        ):
            with self.assertRaises(review_runtime.ProviderUnavailable) as raised:
                review_runtime._seatbelt_command(["/bin/provider"], [directory])

        self.assertEqual("isolation", raised.exception.category)
        self.assertEqual(
            "reviewer filesystem isolation is unavailable",
            str(raised.exception),
        )

    def test_reviewer_isolation_availability_is_discovered_separately(self):
        with (
            patch.object(
                review_runtime.shutil,
                "which",
                side_effect=lambda name: "/usr/bin/sandbox-exec"
                if name == "sandbox-exec"
                else None,
            ),
            patch.object(review_runtime, "_probe_sandbox_exec", return_value=True),
        ):
            self.assertTrue(review_runtime.reviewer_isolation_available())
            self.assertEqual(
                "macos-seatbelt",
                review_runtime.provider_capabilities()["codex"][
                    "filesystem_isolation"
                ],
            )

    def test_reviewer_isolation_probe_fails_closed_and_is_cached(self):
        review_runtime._probe_sandbox_exec.cache_clear()
        failed = subprocess.CompletedProcess(
            ["/usr/bin/sandbox-exec"], returncode=71
        )
        with patch.object(review_runtime.subprocess, "run", return_value=failed) as run:
            self.assertFalse(
                review_runtime._probe_sandbox_exec("/usr/bin/sandbox-exec")
            )
            self.assertFalse(
                review_runtime._probe_sandbox_exec("/usr/bin/sandbox-exec")
            )
        run.assert_called_once()
        review_runtime._probe_sandbox_exec.cache_clear()

    def test_reviewer_isolation_probe_accepts_only_a_successful_profile(self):
        review_runtime._probe_sandbox_exec.cache_clear()
        succeeded = subprocess.CompletedProcess(
            ["/usr/bin/sandbox-exec"], returncode=0
        )
        with patch.object(review_runtime.subprocess, "run", return_value=succeeded):
            self.assertTrue(
                review_runtime._probe_sandbox_exec("/usr/bin/sandbox-exec")
            )
        review_runtime._probe_sandbox_exec.cache_clear()

    def test_timeout_is_sanitized_and_process_is_killed(self):
        process = FakeProcess(times_out=True)
        with (
            patch.object(review_runtime.shutil, "which", return_value="/bin/codex"),
            patch.object(review_runtime.subprocess, "Popen", return_value=process),
        ):
            with self.assertRaises(review_runtime.ProviderFailed) as raised:
                review_runtime.invoke("codex", "prompt", cwd="/work", timeout=0.5)

        self.assertEqual("timeout", raised.exception.category)
        self.assertTrue(process.killed)
        self.assertEqual("provider invocation timed out", str(raised.exception))

    def test_nonzero_failures_are_classified_without_echoing_diagnostics(self):
        cases = (
            ("API key invalid secret-value", "auth", review_runtime.ProviderUnavailable),
            (
                "please log in to continue secret-value",
                "auth",
                review_runtime.ProviderUnavailable,
            ),
            ("rate limit quota secret-value", "quota", review_runtime.ProviderUnavailable),
            (
                "unknown configuration field tools.secret-value",
                "isolation",
                review_runtime.ProviderUnavailable,
            ),
            (
                "log information unavailable secret-value",
                "failed",
                review_runtime.ProviderFailed,
            ),
            ("unexpected secret-value", "failed", review_runtime.ProviderFailed),
        )
        for diagnostic, category, error_type in cases:
            with self.subTest(category=category):
                process = FakeProcess(returncode=1, stderr=diagnostic)
                with (
                    patch.object(review_runtime.shutil, "which", return_value="/bin/codex"),
                    patch.object(review_runtime.subprocess, "Popen", return_value=process),
                ):
                    with self.assertRaises(error_type) as raised:
                        review_runtime.invoke("codex", "prompt", cwd="/work")
                self.assertEqual(category, raised.exception.category)
                self.assertNotIn("secret-value", str(raised.exception))

    def test_missing_and_unknown_providers_are_unavailable_without_spawning(self):
        with (
            patch.object(review_runtime.shutil, "which", return_value=None),
            patch.object(review_runtime.subprocess, "Popen") as popen,
        ):
            with self.assertRaises(review_runtime.ProviderUnavailable) as missing:
                review_runtime.invoke("claude", "prompt", cwd="/work")
            with self.assertRaises(review_runtime.ProviderUnavailable) as unknown:
                review_runtime.invoke("other", "prompt", cwd="/work")

        self.assertEqual("missing", missing.exception.category)
        self.assertEqual("missing", unknown.exception.category)
        popen.assert_not_called()


def codex_features_listing(overrides):
    """Render a `codex features list` table honouring only some overrides."""

    lines = []
    for name in review_runtime._CODEX_DISABLED_FEATURES:
        state = overrides.get(name, False)
        lines.append(f"{name:<40} stable             {str(state).lower()}")
    return "\n".join(lines) + "\n"


class CodexFeatureReadbackTests(unittest.TestCase):
    """The requested feature state is not evidence; the read-back state is."""

    def setUp(self):
        review_runtime._codex_feature_readback.cache_clear()
        self.addCleanup(review_runtime._codex_feature_readback.cache_clear)

    def _readback(self, **run_kwargs):
        with (
            patch.object(
                review_runtime.shutil,
                "which",
                side_effect=lambda name: "/bin/codex" if name == "codex" else None,
            ),
            patch.object(review_runtime.subprocess, "run", **run_kwargs) as run,
        ):
            return review_runtime.codex_feature_readback(), run

    def test_every_disabled_feature_is_read_back_with_the_same_overrides(self):
        listing = subprocess.CompletedProcess(
            ["codex"], returncode=0, stdout=codex_features_listing({}), stderr=""
        )
        readback, run = self._readback(return_value=listing)

        self.assertTrue(readback["verified"])
        self.assertEqual((), readback["still_enabled"])
        self.assertEqual((), readback["unreported"])
        argv = run.call_args.args[0]
        for name in review_runtime._CODEX_DISABLED_FEATURES:
            self.assertIn(f"features.{name}=false", argv)
        self.assertEqual("list", argv[-1])

    def test_a_silently_ignored_override_is_reported_not_hidden(self):
        listing = subprocess.CompletedProcess(
            ["codex"],
            returncode=0,
            stdout=codex_features_listing({"unified_exec": True}),
            stderr="",
        )
        readback, _ = self._readback(return_value=listing)

        self.assertTrue(readback["verified"])
        self.assertEqual(("unified_exec",), readback["still_enabled"])

    def test_a_feature_absent_from_the_listing_counts_as_unverified(self):
        rows = [
            line
            for line in codex_features_listing({}).splitlines()
            if not line.startswith("skill_search")
        ]
        listing = subprocess.CompletedProcess(
            ["codex"], returncode=0, stdout="\n".join(rows), stderr=""
        )
        readback, _ = self._readback(return_value=listing)

        self.assertEqual(("skill_search",), readback["unreported"])

    def test_an_unusable_readback_is_unverified_rather_than_clean(self):
        for kwargs in (
            {"return_value": subprocess.CompletedProcess(["codex"], returncode=2)},
            {"return_value": subprocess.CompletedProcess(["codex"], 0, stdout="", stderr="")},
            {"side_effect": OSError("boom")},
            {"side_effect": subprocess.TimeoutExpired("codex", 15)},
        ):
            with self.subTest(kwargs=sorted(kwargs)):
                review_runtime._codex_feature_readback.cache_clear()
                readback, _ = self._readback(**kwargs)
                self.assertFalse(readback["verified"])
                self.assertIn("reason", readback)

    def test_capabilities_report_the_measured_state_not_the_request(self):
        cases = {
            "none": codex_features_listing({}),
            "sandboxed-residual": codex_features_listing({"unified_exec": True}),
        }
        for expected, stdout in cases.items():
            with self.subTest(tool_access=expected):
                review_runtime._codex_feature_readback.cache_clear()
                listing = subprocess.CompletedProcess(
                    ["codex"], returncode=0, stdout=stdout, stderr=""
                )
                with (
                    patch.object(
                        review_runtime.shutil, "which", return_value="/bin/codex"
                    ),
                    patch.object(review_runtime.subprocess, "run", return_value=listing),
                    patch.object(
                        review_runtime, "_probe_sandbox_exec", return_value=True
                    ),
                ):
                    codex = review_runtime.provider_capabilities()["codex"]
                self.assertEqual(expected, codex["tool_access"])
                self.assertTrue(codex["feature_state_verified"])
        self.assertIn("unified_exec", codex["features_still_enabled"])
        self.assertIn("unified_exec", codex["tool_isolation"])

    def test_capabilities_stay_role_neutral_in_every_readback_outcome(self):
        listings = (
            subprocess.CompletedProcess(
                ["codex"], returncode=0, stdout=codex_features_listing({}), stderr=""
            ),
            subprocess.CompletedProcess(
                ["codex"],
                returncode=0,
                stdout=codex_features_listing({"unified_exec": True}),
                stderr="",
            ),
            subprocess.CompletedProcess(["codex"], returncode=2, stdout="", stderr=""),
        )
        for listing in listings:
            with self.subTest(returncode=listing.returncode):
                review_runtime._codex_feature_readback.cache_clear()
                with (
                    patch.object(
                        review_runtime.shutil, "which", return_value="/bin/codex"
                    ),
                    patch.object(review_runtime.subprocess, "run", return_value=listing),
                ):
                    rendered = repr(review_runtime.provider_capabilities()).lower()
                self.assertNotIn("owner", rendered)
                self.assertNotIn("reviewer", rendered)

    def test_the_command_disables_exactly_the_features_that_are_read_back(self):
        command = review_runtime._codex_command(
            "/bin/codex", "/work", session_id=None, model=None
        )
        requested = {
            value.split("=", 1)[0].removeprefix("features.")
            for value in command
            if value.startswith("features.") and value.endswith("=false")
        }
        self.assertEqual(set(review_runtime._CODEX_DISABLED_FEATURES), requested)


if __name__ == "__main__":
    unittest.main()
