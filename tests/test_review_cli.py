import io
import json
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mutual_review_room import cli as review_cli, runtime as review_runtime
from mutual_review_room.broker import (
    _validated_object as _validated_broker_object,
    prepare_mailbox,
    serve_forever,
    serve_one,
    submit,
    wait,
)
from mutual_review_room.room import _ReviewStateView, render_reviewer
from mutual_review_room.runtime import ProviderResult, ProviderUnavailable
from mutual_review_room.state import ReviewState, ReviewerSpec, StateConflictError


class ReviewCliTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="review-cli-test-")
        self.root = Path(self.temp_dir.name) / "state"
        self.workspace = Path(self.temp_dir.name) / "workspace"
        self.workspace.mkdir()
        self.codex_workspace = Path(self.temp_dir.name) / "codex-workspace"
        self.claude_workspace = Path(self.temp_dir.name) / "claude-workspace"
        self.codex_workspace.mkdir()
        self.claude_workspace.mkdir()
        self.verdict_file = Path(self.temp_dir.name) / "verdict.md"
        self.verdict_file.write_text(
            "Final synthesis with one explicit disagreement.", encoding="utf-8"
        )
        self.state = ReviewState(self.root)
        self.created = self.state.create_job(
            owner_provider="claude",
            owner_model="default",
            owner_cwd=str(self.workspace),
            reviewers=[
                ReviewerSpec(
                    "codex-lane", "codex", "default", str(self.codex_workspace)
                ),
                ReviewerSpec(
                    "claude-lane", "claude", "default", str(self.claude_workspace)
                ),
            ],
            job_id="job_cli",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_late_native_session_binding_and_same_session_followup(self):
        calls = []

        def fake_invoke(provider, prompt, **kwargs):
            calls.append((provider, prompt, kwargs["session_id"]))
            session = kwargs["session_id"] or "codex-native-stable"
            return ProviderResult(session, f"answer:{prompt}", (f"answer:{prompt}",), "start")

        first = review_cli.dispatch_one(
            self.state,
            "job_cli",
            "codex-lane",
            round_no=1,
            prompt="same brief",
            invoke_fn=fake_invoke,
        )
        second = review_cli.dispatch_one(
            self.state,
            "job_cli",
            "codex-lane",
            round_no=2,
            prompt="codex-specific follow-up",
            invoke_fn=fake_invoke,
        )

        self.assertEqual(first["session_id"], "codex-native-stable")
        self.assertEqual(second["session_id"], "codex-native-stable")
        self.assertIsNone(calls[0][2])
        self.assertEqual(calls[1][2], "codex-native-stable")
        self.assertEqual(second["parent_request_id"], first["request_id"])
        request = self.state.get_request("job_cli", first["request_id"])
        self.assertEqual(request.session_id, "codex-native-stable")
        event_types = [
            event.event_type
            for event in self.state.list_events(
                "job_cli", reviewer_id="codex-lane", request_id=first["request_id"]
            )
        ]
        self.assertIn("provider_answering", event_types)

    def test_dispatch_preflight_failure_never_creates_a_running_request(self):
        with patch.object(
            review_cli,
            "_reviewer_deny_paths",
            side_effect=review_cli.CliError("historical workspace is unavailable"),
        ):
            with self.assertRaisesRegex(review_cli.CliError, "historical workspace"):
                review_cli.dispatch_one(
                    self.state,
                    "job_cli",
                    "codex-lane",
                    round_no=1,
                    prompt="cold brief",
                )

        self.assertEqual([], self.state.list_requests("job_cli"))

    def test_dispatch_unexpected_controller_error_settles_the_request(self):
        with self.assertRaisesRegex(OSError, "controller broke"):
            review_cli.dispatch_one(
                self.state,
                "job_cli",
                "codex-lane",
                round_no=1,
                prompt="cold brief",
                invoke_fn=lambda *args, **kwargs: (_ for _ in ()).throw(
                    OSError("controller broke")
                ),
            )

        requests = self.state.list_requests("job_cli")
        self.assertEqual(1, len(requests))
        self.assertEqual("failed", requests[0].status)
        self.assertEqual(
            "controller stopped before a provider response was committed",
            requests[0].error,
        )

    def test_dispatch_all_queues_every_ready_lane_before_first_provider_starts(self):
        calls = []
        reviewer_order = [
            reviewer.reviewer_id for reviewer in self.state.list_reviewers("job_cli")
        ]

        def fake_dispatch(state, job_id, reviewer_id, **kwargs):
            calls.append(reviewer_id)
            for expected in ("codex-lane", "claude-lane"):
                queued = state.list_events(
                    job_id, reviewer_id=expected, event_type="request_queued"
                )
                self.assertEqual(1, len(queued))
                self.assertIn("provider has not started", queued[0].content)
            return {
                "job_id": job_id,
                "reviewer_id": reviewer_id,
                "round": kwargs["round_no"],
                "status": "completed",
            }

        with patch.object(review_cli, "dispatch_one", side_effect=fake_dispatch):
            result = review_cli._dispatch_all_local(
                self.state,
                "job_cli",
                round_no=1,
                prompt="identical cold-read envelope",
                timeout=300.0,
            )

        self.assertEqual("job_cli", result["job_id"])
        self.assertEqual(reviewer_order, calls)
        codex_queued = self.state.list_events(
            "job_cli", reviewer_id="codex-lane", event_type="request_queued"
        )[0]
        claude_queued = self.state.list_events(
            "job_cli", reviewer_id="claude-lane", event_type="request_queued"
        )[0]
        positions = {
            reviewer_id: index
            for index, reviewer_id in enumerate(reviewer_order, start=1)
        }
        self.assertIn(f"({positions['codex-lane']}/2)", codex_queued.content)
        self.assertIn(f"({positions['claude-lane']}/2)", claude_queued.content)

    def test_dispatch_all_cannot_append_queue_events_after_completion(self):
        self.state.complete_job("job_cli", "Final synthesis.")
        before = len(self.state.list_events("job_cli", limit=10_000))

        with self.assertRaisesRegex(StateConflictError, "job is already complete"):
            review_cli._dispatch_all_local(
                self.state,
                "job_cli",
                round_no=1,
                prompt="must not queue",
                timeout=300.0,
            )

        self.assertEqual(before, len(self.state.list_events("job_cli", limit=10_000)))
        self.assertEqual(
            [], self.state.list_events("job_cli", event_type="request_queued")
        )

    def test_dispatch_denies_control_root_owner_and_all_job_lanes_but_not_own_workspace(self):
        seen = {}

        other_owner = Path(self.temp_dir.name) / "other-owner"
        other_lane = Path(self.temp_dir.name) / "other-lane"
        other_owner.mkdir()
        other_lane.mkdir()
        self.state.create_job(
            owner_provider="kimi",
            owner_model="default",
            owner_cwd=str(other_owner),
            reviewers=[
                ReviewerSpec("kimi-lane", "kimi", "default", str(other_lane))
            ],
            job_id="job_other",
        )

        def fake_invoke(provider, prompt, **kwargs):
            seen.update(kwargs)
            return ProviderResult("codex-native", "answer", ("answer",), "start")

        review_cli.dispatch_one(
            self.state,
            "job_cli",
            "codex-lane",
            round_no=1,
            prompt="cold brief",
            invoke_fn=fake_invoke,
        )

        denied = {Path(item) for item in seen["deny_paths"]}
        self.assertIn(self.root.resolve(), denied)
        self.assertIn(self.workspace.resolve(), denied)
        self.assertIn(self.claude_workspace.resolve(), denied)
        self.assertIn(other_lane.resolve(), denied)
        self.assertNotIn(self.codex_workspace.resolve(), denied)

    def test_deleted_historical_lane_does_not_block_a_new_dispatch(self):
        other_owner = Path(self.temp_dir.name) / "deleted-lane-owner"
        deleted_lane = Path(self.temp_dir.name) / "deleted-lane"
        other_owner.mkdir()
        deleted_lane.mkdir()
        self.state.create_job(
            owner_provider="kimi",
            owner_model="default",
            owner_cwd=str(other_owner),
            reviewers=[
                ReviewerSpec("old-lane", "kimi", "default", str(deleted_lane))
            ],
            job_id="job_deleted_lane",
        )
        deleted_lane.rmdir()
        seen = {}

        def fake_invoke(provider, prompt, **kwargs):
            seen["deny_paths"] = kwargs["deny_paths"]
            return ProviderResult(
                "codex-native-after-cleanup",
                "reviewed",
                ("reviewed",),
                "start",
            )

        result = review_cli.dispatch_one(
            self.state,
            "job_cli",
            "codex-lane",
            round_no=1,
            prompt="cold brief",
            invoke_fn=fake_invoke,
        )

        self.assertEqual("completed", result["status"])
        self.assertNotIn(str(deleted_lane), seen["deny_paths"])

    def test_broken_historical_lane_symlink_fails_before_request_creation(self):
        other_owner = Path(self.temp_dir.name) / "broken-lane-owner"
        broken_lane = Path(self.temp_dir.name) / "broken-lane"
        missing_target = Path(self.temp_dir.name) / "missing-lane-target"
        other_owner.mkdir()
        broken_lane.symlink_to(missing_target, target_is_directory=True)
        self.state.create_job(
            owner_provider="kimi",
            owner_model="default",
            owner_cwd=str(other_owner),
            reviewers=[
                ReviewerSpec("old-lane", "kimi", "default", str(broken_lane))
            ],
            job_id="job_broken_lane",
        )

        with self.assertRaisesRegex(
            review_cli.CliError, "historical reviewer workspace"
        ):
            review_cli.dispatch_one(
                self.state,
                "job_cli",
                "codex-lane",
                round_no=1,
                prompt="cold brief",
            )

        self.assertEqual([], self.state.list_requests("job_cli"))

    def test_seatbelt_denies_another_jobs_control_and_lane_canaries(self):
        if not review_runtime.reviewer_isolation_available():
            self.skipTest("macOS sandbox-exec is unavailable")

        other_owner = Path(self.temp_dir.name) / "other-owner"
        other_lane = Path(self.temp_dir.name) / "other-lane"
        other_owner.mkdir()
        other_lane.mkdir()
        self.state.create_job(
            owner_provider="kimi",
            owner_model="default",
            owner_cwd=str(other_owner),
            reviewers=[
                ReviewerSpec("kimi-lane", "kimi", "default", str(other_lane))
            ],
            job_id="job_other",
        )
        own_canary = self.codex_workspace / "own.txt"
        control_canary = self.root / "job_other" / "control.txt"
        peer_canary = other_lane / "peer.txt"
        own_canary.write_text("own-readable", encoding="utf-8")
        control_canary.write_text("control-blocked", encoding="utf-8")
        peer_canary.write_text("peer-blocked", encoding="utf-8")
        denied = review_cli._reviewer_deny_paths(
            self.state, "job_cli", "codex-lane"
        )

        allowed = subprocess.run(
            review_runtime._seatbelt_command(["/bin/cat", str(own_canary)], denied),
            cwd=self.codex_workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        denied_control = subprocess.run(
            review_runtime._seatbelt_command(
                ["/bin/cat", str(control_canary)], denied
            ),
            cwd=self.codex_workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        denied_peer = subprocess.run(
            review_runtime._seatbelt_command(["/bin/cat", str(peer_canary)], denied),
            cwd=self.codex_workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        control_write_target = self.root / "job_other" / "created.txt"
        peer_write_target = other_lane / "created.txt"
        denied_control_write = subprocess.run(
            review_runtime._seatbelt_command(
                ["/usr/bin/touch", str(control_write_target)], denied
            ),
            cwd=self.codex_workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        denied_peer_write = subprocess.run(
            review_runtime._seatbelt_command(
                ["/usr/bin/touch", str(peer_write_target)], denied
            ),
            cwd=self.codex_workspace,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, allowed.returncode, allowed.stderr)
        self.assertEqual("own-readable", allowed.stdout)
        self.assertNotEqual(0, denied_control.returncode)
        self.assertEqual("", denied_control.stdout)
        self.assertNotEqual(0, denied_peer.returncode)
        self.assertEqual("", denied_peer.stdout)
        self.assertNotEqual(0, denied_control_write.returncode)
        self.assertFalse(control_write_target.exists())
        self.assertNotEqual(0, denied_peer_write.returncode)
        self.assertFalse(peer_write_target.exists())

    def test_distinct_reviewer_followups_never_cross_parent_or_session(self):
        def fake_invoke(provider, prompt, **kwargs):
            session = kwargs["session_id"] or f"{provider}-native"
            return ProviderResult(session, f"{provider}:{prompt}", (prompt,), "start")

        first_a = review_cli.dispatch_one(
            self.state, "job_cli", "codex-lane", round_no=1, prompt="shared", invoke_fn=fake_invoke
        )
        first_b = review_cli.dispatch_one(
            self.state, "job_cli", "claude-lane", round_no=1, prompt="shared", invoke_fn=fake_invoke
        )
        second_a = review_cli.dispatch_one(
            self.state,
            "job_cli",
            "codex-lane",
            round_no=2,
            prompt="challenge A only",
            invoke_fn=fake_invoke,
        )
        second_b = review_cli.dispatch_one(
            self.state,
            "job_cli",
            "claude-lane",
            round_no=2,
            prompt="challenge B only",
            invoke_fn=fake_invoke,
        )

        self.assertEqual(second_a["parent_request_id"], first_a["request_id"])
        self.assertEqual(second_b["parent_request_id"], first_b["request_id"])
        self.assertNotEqual(second_a["session_id"], second_b["session_id"])
        a_events = self.state.list_events("job_cli", reviewer_id="codex-lane")
        b_events = self.state.list_events("job_cli", reviewer_id="claude-lane")
        self.assertIn("challenge A only", [event.content for event in a_events])
        self.assertNotIn("challenge B only", [event.content for event in a_events])
        self.assertIn("challenge B only", [event.content for event in b_events])

    def test_unavailable_reviewer_does_not_block_remaining_lane(self):
        def unavailable(*args, **kwargs):
            raise ProviderUnavailable("quota", "provider quota is unavailable")

        failed = review_cli.dispatch_one(
            self.state,
            "job_cli",
            "codex-lane",
            round_no=1,
            prompt="shared",
            invoke_fn=unavailable,
        )
        successful = review_cli.dispatch_one(
            self.state,
            "job_cli",
            "claude-lane",
            round_no=1,
            prompt="shared",
            invoke_fn=lambda *args, **kwargs: ProviderResult(
                "claude-native", "usable answer", ("usable answer",), "start"
            ),
        )
        self.assertEqual(failed["status"], "unavailable")
        self.assertEqual(successful["status"], "completed")
        self.assertEqual(self.state.get_reviewer("job_cli", "codex-lane").status, "unavailable")
        self.assertEqual(self.state.get_reviewer("job_cli", "claude-lane").status, "ready")

    def test_control_token_file_is_job_bound(self):
        review_cli._authorize(self.state, "job_cli", self.created.token_path)
        wrong = Path(self.temp_dir.name) / "wrong.token"
        wrong.write_text(self.created.owner_token, encoding="utf-8")
        with self.assertRaises(review_cli.CliError):
            review_cli._authorize(self.state, "job_cli", str(wrong))

    def test_authority_accepts_the_token_the_owner_launcher_injects(self):
        """The launcher removes MRR_OWNER_TOKEN_FILE and injects MRR_OWNER_TOKEN."""

        injected = review_cli._owner_environment(self.state, "job_cli")
        self.assertNotIn("MRR_OWNER_TOKEN_FILE", injected)
        self.assertEqual(self.created.owner_token, injected["MRR_OWNER_TOKEN"])

        with patch.dict(
            review_cli.os.environ,
            {"MRR_OWNER_TOKEN": injected["MRR_OWNER_TOKEN"]},
            clear=True,
        ):
            review_cli._authorize(self.state, "job_cli", None)
            with patch.dict(
                review_cli.os.environ, {"MRR_OWNER_TOKEN": "not-the-token"}
            ):
                with self.assertRaises(review_cli.CliError):
                    review_cli._authorize(self.state, "job_cli", None)

    def test_recover_is_available_as_a_signed_broker_action(self):
        request = self.state.begin_request(
            "job_cli", "codex-lane", round=1, prompt="brief"
        )
        self.assertEqual(
            "running", self.state.get_request("job_cli", request.request_id).status
        )

        result = review_cli._run_broker_action(
            self.state, "job_cli", "recover", {}, "control_1"
        )

        self.assertEqual(0, result["exit_code"])
        self.assertEqual(
            [request.request_id],
            [item["request_id"] for item in result["output"]["interrupted"]],
        )
        self.assertFalse(result["output"]["replayed"])
        self.assertEqual(
            "interrupted", self.state.get_request("job_cli", request.request_id).status
        )
        # The broker only accepts JSON-serialisable results.
        _validated_broker_object(result["output"], "result")

    def test_recover_rejects_an_unexpected_payload(self):
        result = review_cli._run_broker_action(
            self.state, "job_cli", "recover", {"round": 1}, "control_1"
        )
        self.assertEqual(2, result["exit_code"])

    def test_fan_out_skips_ineligible_lanes_and_says_so(self):
        self.state.mark_unavailable("job_cli", "claude-lane", "provider is missing")
        dispatched = []

        def fake_dispatch(state, job_id, reviewer_id, **kwargs):
            dispatched.append(reviewer_id)
            return {
                "job_id": job_id,
                "reviewer_id": reviewer_id,
                "round": kwargs["round_no"],
                "status": "completed",
            }

        with patch.object(review_cli, "dispatch_one", fake_dispatch):
            result = review_cli._dispatch_all_local(
                self.state, "job_cli", round_no=1, prompt="shared brief", timeout=5.0
            )

        self.assertEqual(["codex-lane"], dispatched)
        by_lane = {item["reviewer_id"]: item for item in result["results"]}
        self.assertEqual({"codex-lane", "claude-lane"}, set(by_lane))
        self.assertEqual("completed", by_lane["codex-lane"]["status"])
        self.assertEqual("skipped", by_lane["claude-lane"]["status"])
        self.assertIn("unavailable", by_lane["claude-lane"]["reason"])

    def test_owner_prompt_keeps_owner_authority_out_of_commands_and_text(self):
        prompt = review_cli._owner_prompt(self.state, "job_cli")

        self.assertNotIn("--token-file", prompt)
        self.assertNotIn(self.created.token_path, prompt)
        self.assertIn("complete --job job_cli", prompt)
        self.assertNotIn(self.created.owner_token, prompt)

    def test_reopening_a_live_room_joins_it_instead_of_rebuilding(self):
        """Getting back into a detached room must not restart the owner."""

        args = review_cli.build_parser().parse_args(
            ["--root", str(self.root), "room", "--job", "job_cli"]
        )
        with (
            patch.object(review_cli.TmuxRoom, "exists", return_value=True),
            patch.object(review_cli.TmuxRoom, "attach") as attach,
            patch.object(review_cli, "_create_room") as create,
        ):
            self.assertEqual(0, args.handler(args))

        attach.assert_called_once()
        create.assert_not_called()

    def test_replace_still_rebuilds_a_live_room(self):
        args = review_cli.build_parser().parse_args(
            ["--root", str(self.root), "room", "--job", "job_cli", "--replace"]
        )
        with (
            patch.object(review_cli.TmuxRoom, "exists", return_value=True),
            patch.object(review_cli.TmuxRoom, "attach") as attach,
            patch.object(review_cli, "_create_room", return_value={}) as create,
        ):
            self.assertEqual(0, args.handler(args))

        create.assert_called_once()
        self.assertTrue(create.call_args.kwargs["replace"])
        attach.assert_not_called()

    def test_reopening_a_room_that_is_not_running_builds_it(self):
        args = review_cli.build_parser().parse_args(
            ["--root", str(self.root), "room", "--job", "job_cli"]
        )
        with (
            patch.object(review_cli.TmuxRoom, "exists", return_value=False),
            patch.object(review_cli.TmuxRoom, "attach") as attach,
            patch.object(review_cli, "_create_room", return_value={}) as create,
        ):
            self.assertEqual(0, args.handler(args))

        create.assert_called_once()
        attach.assert_not_called()

    def test_owner_prompt_says_reviewers_cannot_read_the_paths_it_names(self):
        """A path-only envelope leaves an isolated reviewer nothing to review.

        Observed live: one lane refused to review and asked for the material to
        be pasted, because the envelope referenced a file it had no tools to
        open.
        """

        prompt = review_cli._owner_prompt(self.state, "job_cli")

        self.assertIn("no file access", prompt)
        self.assertIn("inline", prompt)

    def test_complete_parser_selects_owner_only_handler(self):
        args = review_cli.build_parser().parse_args(
            [
                "--root",
                str(self.root),
                "complete",
                "--job",
                "job_cli",
                "--verdict-file",
                str(self.verdict_file),
                "--token-file",
                self.created.token_path,
            ]
        )

        self.assertIs(args.handler, review_cli._cmd_complete)
        self.assertEqual(args.job, "job_cli")
        self.assertEqual(args.verdict_file, str(self.verdict_file))
        self.assertEqual(args.token_file, self.created.token_path)

    def test_complete_rejects_an_invalid_owner_token_without_mutating_state(self):
        initial_status = self.state.get_job("job_cli").status
        paths = prepare_mailbox(self.root, "job_cli")
        request_id = submit(
            paths,
            "invalid-token",
            "complete",
            {"verdict": "must not be committed"},
        )
        handled = []
        serve_one(
            paths,
            self.created.owner_token,
            lambda *items: handled.append(items) or {"unexpected": True},
        )
        response = wait(
            paths,
            self.created.owner_token,
            request_id,
            timeout=1.0,
        )

        self.assertEqual("error", response.status)
        self.assertEqual([], handled)
        self.assertEqual(self.state.get_job("job_cli").status, initial_status)
        self.assertEqual(
            self.state.list_events("job_cli", event_type="job_completed"), []
        )

    def test_complete_records_append_only_event_and_outputs_committed_state(self):
        result = review_cli._run_broker_action(
            self.state,
            "job_cli",
            "complete",
            {"verdict": self.verdict_file.read_text(encoding="utf-8")},
            "control_complete",
        )

        payload = result["output"]
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(payload["job"]["status"], "complete")
        self.assertEqual(payload["completion_event"]["event_type"], "job_completed")
        self.assertEqual(payload["completion_event"]["status"], "complete")
        self.assertEqual(
            payload["completion_event"]["content"],
            "Final synthesis with one explicit disagreement.",
        )
        events = self.state.list_events("job_cli", event_type="job_completed")
        self.assertEqual(len(events), 1)
        self.assertEqual(payload["completion_event"]["event_id"], events[0].event_id)

    def test_sandbox_client_round_trips_through_the_native_broker(self):
        paths = prepare_mailbox(self.root, "job_cli")
        stopped = threading.Event()
        broker = threading.Thread(
            target=serve_forever,
            args=(
                paths,
                self.created.owner_token,
                lambda action, payload, request_id: review_cli._run_broker_action(
                    self.state,
                    "job_cli",
                    action,
                    payload,
                    request_id,
                ),
            ),
            kwargs={
                "poll_interval": 0.01,
                "heartbeat_interval": 0.01,
                "stop_event": stopped,
            },
        )
        broker.start()
        args = review_cli.build_parser().parse_args(
            [
                "--root",
                str(self.root),
                "complete",
                "--job",
                "job_cli",
                "--verdict-file",
                str(self.verdict_file),
            ]
        )
        output = io.StringIO()
        try:
            with (
                patch.dict(
                    review_cli.os.environ,
                    {"MRR_OWNER_TOKEN": self.created.owner_token},
                    clear=False,
                ),
                redirect_stdout(output),
            ):
                result = args.handler(args)
        finally:
            stopped.set()
            broker.join(timeout=2.0)

        self.assertFalse(broker.is_alive())
        self.assertEqual(0, result)
        self.assertEqual("complete", json.loads(output.getvalue())["job"]["status"])

    def test_status_reports_job_complete_after_owner_completion(self):
        self.state.complete_job("job_cli", "Final synthesis.")
        args = review_cli.build_parser().parse_args(
            ["--root", str(self.root), "status", "--job", "job_cli"]
        )
        output = io.StringIO()

        with (
            patch.object(review_cli.TmuxRoom, "describe", return_value={"running": False}),
            redirect_stdout(output),
        ):
            result = args.handler(args)

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["job"]["status"], "complete")
        self.assertEqual(
            payload["event_count"], len(self.state.list_events("job_cli", limit=10_000))
        )

    def test_owner_exec_limit_is_raised_without_lowering_the_hard_limit(self):
        with (
            patch.object(review_cli.resource, "getrlimit", return_value=(256, 8192)),
            patch.object(review_cli.resource, "setrlimit") as setrlimit,
        ):
            review_cli._raise_nofile_for_owner()

        setrlimit.assert_called_once_with(review_cli.resource.RLIMIT_NOFILE, (4096, 8192))

    def test_owner_exec_limit_respects_a_lower_hard_limit(self):
        with (
            patch.object(review_cli.resource, "getrlimit", return_value=(256, 2048)),
            patch.object(review_cli.resource, "setrlimit") as setrlimit,
        ):
            review_cli._raise_nofile_for_owner()

        setrlimit.assert_called_once_with(review_cli.resource.RLIMIT_NOFILE, (2048, 2048))

    def test_codex_owner_keeps_sandbox_and_uses_auto_review_without_alt_screen(self):
        inbox = str(Path(self.temp_dir.name) / "broker" / "inbox")
        argv = review_cli._codex_owner_argv(
            "/opt/homebrew/bin/codex",
            str(self.workspace),
            "native-owner-session",
            "gpt-5",
            mailbox_inbox=inbox,
        )

        self.assertIn("--approve-for-me", argv)
        self.assertIn("--no-alt-screen", argv)
        self.assertEqual(
            f'projects."{self.workspace}".trust_level="trusted"',
            argv[argv.index("--config") + 1],
        )
        self.assertNotIn("-s", argv)
        self.assertNotIn("-a", argv)
        self.assertNotIn("danger-full-access", argv)
        self.assertNotIn("--yolo", argv)
        self.assertEqual(inbox, argv[argv.index("--add-dir") + 1])
        self.assertEqual("native-owner-session", argv[-1])

    def test_owner_environment_uses_an_ephemeral_token_not_a_file_command(self):
        environment = review_cli._owner_environment(self.state, "job_cli")

        self.assertEqual(self.created.owner_token, environment["MRR_OWNER_TOKEN"])
        self.assertEqual("2", environment["MRR_REVIEWER_COUNT"])
        self.assertNotIn("MRR_OWNER_TOKEN_FILE", environment)

    def test_claude_owner_binds_only_after_successful_bootstrap(self):
        result = ProviderResult(
            "claude-owner-native",
            "contract loaded",
            ("contract loaded",),
            "start",
        )
        with (
            patch.object(review_cli, "invoke", return_value=result) as invoke,
            patch.object(review_cli.shutil, "which", return_value="/usr/local/bin/claude"),
            patch.object(review_cli.os, "chdir"),
            patch.object(review_cli, "_raise_nofile_for_owner"),
            patch.object(
                review_cli.os,
                "execvpe",
                side_effect=RuntimeError("exec boundary"),
            ) as execvpe,
        ):
            with self.assertRaisesRegex(RuntimeError, "exec boundary"):
                review_cli._exec_owner(self.state, "job_cli")

        invoke.assert_called_once()
        self.assertEqual(
            "claude-owner-native",
            self.state.get_job("job_cli").owner_session_id,
        )
        argv = execvpe.call_args.args[1]
        self.assertEqual(
            ["/usr/local/bin/claude", "--resume", "claude-owner-native"],
            argv[:3],
        )

    def test_claude_owner_bootstrap_failure_does_not_bind_a_session(self):
        with (
            patch.object(
                review_cli,
                "invoke",
                side_effect=ProviderUnavailable("auth", "authentication unavailable"),
            ),
            patch.object(review_cli.shutil, "which", return_value="/usr/local/bin/claude"),
        ):
            with self.assertRaises(ProviderUnavailable):
                review_cli._exec_owner(self.state, "job_cli")

        self.assertIsNone(self.state.get_job("job_cli").owner_session_id)

    def test_projection_reads_real_state_and_hides_other_lane(self):
        self.state.set_reviewer_session("job_cli", "codex-lane", "codex-native")
        request = self.state.begin_request(
            "job_cli", "codex-lane", round=1, prompt="question for codex"
        )
        self.state.complete_request("job_cli", request.request_id, "codex answer")
        rendered = render_reviewer(_ReviewStateView(self.root, "job_cli"), "codex-lane")
        self.assertIn("question for codex", rendered)
        self.assertIn("codex answer", rendered)
        self.assertIn("native session: codex-native", rendered)
        self.assertNotIn("claude-lane", rendered)

    def test_projection_distinguishes_queued_from_provider_answering(self):
        self.state.append_event(
            "job_cli",
            reviewer_id="codex-lane",
            round=1,
            direction="system",
            event_type="request_queued",
            status="queued",
            content="Queued for serial dispatch (1/2); the provider has not started.",
        )
        queued = render_reviewer(_ReviewStateView(self.root, "job_cli"), "codex-lane")
        self.assertIn("reviewer status: queued (provider not started)", queued)
        self.assertIn("STATUS · queued", queued)
        self.assertIn("provider has not started", queued)

    def test_create_job_marks_unavailable_builtin_lane_but_keeps_room_shape(self):
        parser = review_cli.build_parser()
        args = parser.parse_args(
            [
                "--root",
                str(Path(self.temp_dir.name) / "created"),
                "launch",
                "--owner",
                "claude",
                "--reviewer",
                "a=codex",
                "--reviewer",
                "b=kimi",
                "--cwd",
                str(self.workspace),
                "--no-attach",
            ]
        )
        with (
            patch.object(
                review_cli,
                "provider_available",
                side_effect=lambda p: p in {"claude", "codex"},
            ),
            patch.object(review_cli, "reviewer_isolation_available", return_value=True),
        ):
            state, created = review_cli._create_job(args)
        self.assertEqual(len(state.list_reviewers(created.job.job_id)), 2)
        self.assertEqual(state.get_reviewer(created.job.job_id, "b").status, "unavailable")
        self.assertEqual(state.get_reviewer(created.job.job_id, "a").status, "ready")
        for reviewer in state.list_reviewers(created.job.job_id):
            reviewer_path = Path(reviewer.cwd).resolve()
            self.assertNotIn(state.root.resolve(), reviewer_path.parents)
            self.assertTrue(reviewer_path.is_dir())
        self.assertNotEqual(
            state.get_reviewer(created.job.job_id, "a").cwd,
            state.get_reviewer(created.job.job_id, "b").cwd,
        )

    def test_create_job_accepts_more_than_three_selected_reviewers(self):
        root = Path(self.temp_dir.name) / "too-many-reviewers"
        args = review_cli.build_parser().parse_args(
            [
                "--root",
                str(root),
                "launch",
                "--owner",
                "claude",
                "--reviewer",
                "a=codex",
                "--reviewer",
                "b=kimi",
                "--reviewer",
                "c=claude",
                "--reviewer",
                "d=codex",
                "--cwd",
                str(self.workspace),
                "--no-attach",
            ]
        )

        with (
            patch.object(review_cli, "provider_available", return_value=True),
            patch.object(review_cli, "reviewer_isolation_available", return_value=True),
        ):
            state, created = review_cli._create_job(args)

        self.assertEqual(4, len(state.list_reviewers(created.job.job_id)))

    def test_launch_parser_accepts_kimi_as_an_explicit_owner_choice(self):
        args = review_cli.build_parser().parse_args(
            [
                "launch",
                "--owner",
                "kimi",
                "--reviewer",
                "codex",
                "--cwd",
                str(self.workspace),
                "--no-attach",
            ]
        )

        self.assertEqual("kimi", args.owner)

    def test_reviewer_shorthand_uses_provider_as_lane_id(self):
        self.assertEqual(("kimi", "kimi"), review_cli._parse_reviewer("kimi"))
        self.assertEqual(("codex", "codex"), review_cli._parse_reviewer("CODEX"))

    def test_reviewer_alias_is_available_for_repeated_provider_sessions(self):
        self.assertEqual(
            ("kimi-a", "kimi"), review_cli._parse_reviewer("kimi-a=kimi")
        )

    def test_redundant_reviewer_assignment_has_actionable_correction(self):
        with self.assertRaisesRegex(review_cli.CliError, "use --reviewer kimi"):
            review_cli._parse_reviewer("kimi=kimi")

    def test_duplicate_shorthand_requires_unique_aliases(self):
        args = review_cli.build_parser().parse_args(
            [
                "--root",
                str(Path(self.temp_dir.name) / "duplicate-reviewers"),
                "launch",
                "--owner",
                "claude",
                "--reviewer",
                "kimi",
                "--reviewer",
                "kimi",
                "--cwd",
                str(self.workspace),
                "--no-attach",
            ]
        )
        with patch.object(review_cli, "provider_available", return_value=True):
            with self.assertRaisesRegex(review_cli.CliError, "use ID=PROVIDER"):
                review_cli._create_job(args)

    def test_reviewer_ids_that_only_differ_by_case_are_rejected(self):
        root = Path(self.temp_dir.name) / "case-collision"
        args = review_cli.build_parser().parse_args(
            [
                "--root",
                str(root),
                "launch",
                "--owner",
                "claude",
                "--reviewer",
                "KimiA=codex",
                "--reviewer",
                "kimia=kimi",
                "--cwd",
                str(self.workspace),
                "--no-attach",
            ]
        )

        with patch.object(review_cli, "provider_available", return_value=True):
            with self.assertRaisesRegex(review_cli.CliError, "letter case differs"):
                review_cli._create_job(args)

        self.assertFalse(root.exists())

    def test_kimi_model_overrides_fail_before_state_is_created(self):
        cases = (
            ["--owner", "kimi", "--owner-model", "custom", "--reviewer", "claude"],
            [
                "--owner",
                "claude",
                "--reviewer",
                "kimi",
                "--reviewer-model",
                "kimi=custom",
            ],
        )
        for index, provider_args in enumerate(cases):
            with self.subTest(provider_args=provider_args):
                root = Path(self.temp_dir.name) / f"kimi-model-{index}"
                args = review_cli.build_parser().parse_args(
                    [
                        "--root",
                        str(root),
                        "launch",
                        *provider_args,
                        "--cwd",
                        str(self.workspace),
                        "--no-attach",
                    ]
                )
                with patch.object(review_cli, "provider_available", return_value=True):
                    with self.assertRaisesRegex(
                        review_cli.CliError,
                        "Kimi .* model override is not supported",
                    ):
                        review_cli._create_job(args)
                self.assertFalse(root.exists())

    def test_rejected_explicit_reviewer_base_keeps_user_permissions(self):
        root = Path(self.temp_dir.name) / "permission-rejection"
        before = self.workspace.stat().st_mode & 0o777
        args = review_cli.build_parser().parse_args(
            [
                "--root",
                str(root),
                "launch",
                "--owner",
                "claude",
                "--reviewer",
                "kimi",
                "--cwd",
                str(self.workspace),
                "--reviewer-cwd",
                str(self.workspace),
                "--no-attach",
            ]
        )

        with patch.object(review_cli, "provider_available", return_value=True):
            with self.assertRaisesRegex(review_cli.CliError, "owner cwd"):
                review_cli._create_job(args)

        self.assertEqual(before, self.workspace.stat().st_mode & 0o777)

    def test_unknown_reviewer_provider_fails_before_creating_state(self):
        root = Path(self.temp_dir.name) / "unknown-provider"
        args = review_cli.build_parser().parse_args(
            [
                "--root",
                str(root),
                "launch",
                "--owner",
                "claude",
                "--reviewer",
                "unknown-provider",
                "--cwd",
                str(self.workspace),
                "--no-attach",
            ]
        )

        with patch.object(review_cli, "provider_available", return_value=True):
            with self.assertRaisesRegex(review_cli.CliError, "not built in"):
                review_cli._create_job(args)

        self.assertFalse(root.exists())

    def test_room_commands_name_bare_reviewer_panes_by_provider(self):
        bare_root = Path(self.temp_dir.name) / "bare-reviewers"
        args = review_cli.build_parser().parse_args(
            [
                "--root",
                str(bare_root),
                "launch",
                "--owner",
                "claude",
                "--reviewer",
                "kimi",
                "--reviewer",
                "codex",
                "--cwd",
                str(self.workspace),
                "--no-attach",
            ]
        )
        with (
            patch.object(review_cli, "provider_available", return_value=True),
            patch.object(review_cli, "reviewer_isolation_available", return_value=True),
        ):
            state, created = review_cli._create_job(args)

        _owner, _observers, titles, broker = review_cli._room_commands(
            state, created.job.job_id
        )
        self.assertEqual(["reviewer kimi", "reviewer codex"], titles)
        self.assertEqual("broker", broker[-3])
        self.assertEqual(created.job.job_id, broker[-1])


if __name__ == "__main__":
    unittest.main()
