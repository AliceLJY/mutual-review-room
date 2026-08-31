from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mutual_review_room.room import (
    RoomExistsError,
    TmuxRoom,
    observe_reviewer,
    render_reviewer,
    session_name_for_job,
)
from mutual_review_room.state import ReviewState, ReviewerSpec


class FakeStore:
    def __init__(self) -> None:
        self.job = {
            "job_id": "job-123",
            "status": "running",
            "current_round": 2,
            "updated_at": "2026-08-31T01:02:03Z",
        }
        self.reviewers = {
            "codex": {
                "reviewer_id": "codex",
                "provider": "codex",
                "model": "gpt-5",
                "native_session_id": "native-codex-stable",
                "status": "failed",
                "current_round": 2,
                "updated_at": "2026-08-31T01:04:00Z",
            },
            "claude": {
                "reviewer_id": "claude",
                "provider": "claude",
                "model": "sonnet",
                "native_session_id": "native-claude-stable",
                "status": "ready",
                "current_round": 1,
            },
        }
        self.events = [
            {
                "sequence": 1,
                "reviewer_id": "codex",
                "event_type": "request_started",
                "round": 1,
                "created_at": "2026-08-31T01:02:10Z",
                "content": "Read every line.\nReturn all findings.",
            },
            {
                "sequence": 2,
                "reviewer_id": "codex",
                "event_type": "reasoning",
                "content": "private chain of thought must never appear",
            },
            {
                "sequence": 3,
                "reviewer_id": "codex",
                "event_type": "response_completed",
                "round": 1,
                "created_at": "2026-08-31T01:03:00Z",
                "content": "Finding A\nFinding B",
            },
            {
                "sequence": 4,
                "reviewer_id": "claude",
                "event_type": "response_completed",
                "content": "another reviewer's private lane",
            },
            {
                "sequence": 5,
                "reviewer_id": "codex",
                "event_type": "request_started",
                "round": 2,
                "created_at": "2026-08-31T01:03:30Z",
                "content": "Recheck finding B against the updated file.",
            },
            {
                "sequence": 6,
                "reviewer_id": "codex",
                "event_type": "request_failed",
                "round": 2,
                "created_at": "2026-08-31T01:04:00Z",
                "status": "failed",
                "content": "provider session unavailable",
            },
        ]

    def get_job(self):
        return self.job

    def list_reviewers(self):
        return list(self.reviewers.values())

    def get_reviewer(self, reviewer_id):
        return self.reviewers[reviewer_id]

    def list_events(self, *, reviewer_id, after_id=0):
        # Ignore reviewer_id deliberately; projection must enforce isolation.
        return [event for event in self.events if event["sequence"] > int(after_id)]


class FakeRunner:
    def __init__(self) -> None:
        self.calls = []
        self.sessions = set()
        self.next_pane = 0

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        command = argv[5]
        if command == "has-session":
            session = argv[argv.index("-t") + 1].lstrip("=")
            return SimpleNamespace(
                returncode=0 if session in self.sessions else 1,
                stdout="",
                stderr="",
            )
        if command == "new-session":
            self.sessions.add(argv[argv.index("-s") + 1])
            pane = f"%{self.next_pane}"
            self.next_pane += 1
            return SimpleNamespace(returncode=0, stdout=pane + "\n", stderr="")
        if command == "split-window":
            pane = f"%{self.next_pane}"
            self.next_pane += 1
            return SimpleNamespace(returncode=0, stdout=pane + "\n", stderr="")
        if command == "kill-session":
            self.sessions.discard(argv[argv.index("-t") + 1].lstrip("="))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == "list-panes":
            return SimpleNamespace(
                returncode=0,
                stdout="%0\towner\t0\t1\tzsh\n%1\treviewer codex\t1\t0\tpython\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def commands(self, name):
        return [
            [argv[0], *argv[5:]]
            for argv, _ in self.calls
            if argv[5] == name
        ]


class ReviewRoomTests(unittest.TestCase):
    def test_projection_is_complete_attributable_and_isolated(self):
        rendered = render_reviewer(FakeStore(), "codex")
        for expected in (
            "job: job-123",
            "reviewer: codex",
            "provider: codex",
            "model: gpt-5",
            "native session: native-codex-stable",
            "Read every line.\nReturn all findings.",
            "Finding A\nFinding B",
            "Recheck finding B against the updated file.",
            "provider session unavailable",
            "OWNER → REVIEWER",
            "REVIEWER → OWNER · FINAL",
        ):
            self.assertIn(expected, rendered)
        for hidden in (
            "private chain of thought",
            "another reviewer's private lane",
        ):
            self.assertNotIn(hidden, rendered)

    def test_observe_once_is_deterministic(self):
        store = FakeStore()
        output = io.StringIO()
        observe_reviewer(
            "/state",
            "job-123",
            "codex",
            once=True,
            output=output,
            store_factory=lambda root, job_id: store,
        )
        self.assertEqual(output.getvalue(), render_reviewer(store, "codex"))
        self.assertNotIn("\x1b[2J", output.getvalue())

    def test_observe_initial_render_and_cursor_use_one_event_snapshot(self):
        class SnapshotStore(FakeStore):
            def __init__(self):
                super().__init__()
                self.reads = 0

            def list_events(self, *, reviewer_id, after_id=0):
                self.reads += 1
                if self.reads > 1:
                    self.events.append(
                        {
                            "sequence": 7,
                            "reviewer_id": "codex",
                            "event_type": "response_completed",
                            "content": "must not enter a second initial read",
                        }
                    )
                return super().list_events(
                    reviewer_id=reviewer_id,
                    after_id=after_id,
                )

        store = SnapshotStore()
        output = io.StringIO()
        observe_reviewer(
            "/state",
            "job-123",
            "codex",
            once=True,
            output=output,
            store_factory=lambda root, job_id: store,
        )

        self.assertEqual(1, store.reads)
        self.assertNotIn("must not enter a second initial read", output.getvalue())

    def test_live_observer_refreshes_for_job_global_completion_event(self):
        class StopObservation(Exception):
            pass

        with tempfile.TemporaryDirectory(prefix="review-room-live-") as temp_dir:
            root = Path(temp_dir) / "state"
            state = ReviewState(root)
            state.create_job(
                owner_provider="kimi",
                owner_model="default",
                owner_cwd=temp_dir,
                reviewers=[
                    ReviewerSpec(
                        "codex",
                        "codex",
                        "default",
                        temp_dir,
                    )
                ],
                job_id="job-live",
            )
            output = io.StringIO()
            sleeps = 0

            def complete_then_stop(interval):
                nonlocal sleeps
                sleeps += 1
                if sleeps == 1:
                    state.complete_job("job-live", "Final synthesis.")
                    return
                raise StopObservation

            with self.assertRaises(StopObservation):
                observe_reviewer(
                    root,
                    "job-live",
                    "codex",
                    interval=0.001,
                    output=output,
                    sleeper=complete_then_stop,
                )

            rendered_updates = output.getvalue()
            self.assertNotIn("\x1b[2J\x1b[H", rendered_updates)
            self.assertEqual(1, rendered_updates.count("MUTUAL REVIEW · OBSERVER"))
            self.assertIn("LIVE UPDATE · job complete", rendered_updates)
            self.assertIn("STATUS · complete", rendered_updates)

    def test_tmux_layout_accepts_an_open_ended_reviewer_selection(self):
        for reviewer_count in (1, 2, 4, 6):
            with self.subTest(reviewer_count=reviewer_count):
                runner = FakeRunner()
                room = TmuxRoom("job/layout", runner=runner, tmux_bin="tmux")
                observers = [["python3", "observer.py", str(i)] for i in range(reviewer_count)]
                titles = [f"reviewer provider-{i}" for i in range(reviewer_count)]
                description = room.create(
                    ["python3", "owner.py"],
                    observers,
                    observer_titles=titles,
                )
                # One split replaces the low-history bootstrap pane; the rest
                # are the selected reviewer panes.
                self.assertEqual(len(runner.commands("split-window")), reviewer_count + 1)
                self.assertEqual(runner.commands("select-layout")[-1][-1], "main-vertical")
                self.assertEqual(runner.commands("resize-pane")[-1][-2:], ["-x", "50%"])
                global_options = [
                    cmd for cmd in runner.commands("set-option") if "-p" not in cmd
                ]
                self.assertEqual(
                    global_options,
                    [
                        ["tmux", "set-option", "-g", "extended-keys", "on"],
                        [
                            "tmux",
                            "set-option",
                            "-g",
                            "extended-keys-format",
                            "csi-u",
                        ],
                        [
                            "tmux",
                            "set-option",
                            "-t",
                            room.session_name,
                            "mouse",
                            "on",
                        ],
                        [
                            "tmux",
                            "set-option",
                            "-t",
                            room.session_name,
                            "pane-border-status",
                            "top",
                        ],
                        [
                            "tmux",
                            "set-option",
                            "-t",
                            room.session_name,
                            "pane-border-format",
                            " #{@mrr_title} ",
                        ],
                        [
                            "tmux",
                            "set-option",
                            "-t",
                            room.session_name,
                            "status-right-length",
                            "72",
                        ],
                        [
                            "tmux",
                            "set-option",
                            "-t",
                            room.session_name,
                            "status-right",
                            "scroll: mouse wheel",
                        ],
                        [
                            "tmux",
                            "set-option",
                            "-w",
                            "-t",
                            "%0",
                            "history-limit",
                            "100000",
                        ],
                    ],
                )
                pane_options = [
                    cmd for cmd in runner.commands("set-option") if "-p" in cmd
                ]
                expected_pane_options = [
                    ["@mrr_title", "owner"],
                    ["allow-set-title", "off"],
                ]
                for title in titles:
                    expected_pane_options.extend(
                        [
                            ["@mrr_title", title],
                            ["allow-set-title", "off"],
                        ]
                    )
                self.assertEqual(
                    [cmd[-2:] for cmd in pane_options], expected_pane_options
                )
                command_names = [call[0][5] for call in runner.calls]
                self.assertLess(
                    command_names.index("new-session"),
                    command_names.index("set-option"),
                )
                self.assertNotIn("owner.py", runner.commands("new-session")[0][-1])
                self.assertIn("time.sleep", runner.commands("new-session")[0][-1])
                self.assertIn("time.sleep", runner.commands("split-window")[0][-1])
                self.assertEqual(len(runner.commands("respawn-pane")), 1)
                self.assertIn("owner.py", runner.commands("respawn-pane")[0][-1])
                disabled = [cmd for cmd in runner.commands("select-pane") if "-d" in cmd]
                self.assertEqual(len(disabled), reviewer_count)
                titled = [cmd for cmd in runner.commands("select-pane") if "-T" in cmd]
                self.assertEqual([cmd[-1] for cmd in titled], ["owner", *titles])
                self.assertTrue(description["exists"])
                self.assertFalse(
                    any(
                        call[0][5] in {"send-keys", "capture-pane"}
                        for call in runner.calls
                    )
                )
                self.assertFalse(any(kwargs.get("shell") for _, kwargs in runner.calls))

                for argv, _kwargs in runner.calls:
                    self.assertEqual(
                        ["-L", room.socket_name, "-f", "/dev/null"],
                        argv[1:5],
                    )

    def test_tmux_construction_failure_rolls_back_only_the_new_room(self):
        runner = FakeRunner()
        split_count = 0

        def fail_second_observer(argv, **kwargs):
            nonlocal split_count
            if argv[5] == "split-window":
                split_count += 1
                # replacement owner, first observer, then fail
                if split_count == 3:
                    runner.calls.append((list(argv), dict(kwargs)))
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="size or position no space",
                    )
            return runner(argv, **kwargs)

        room = TmuxRoom("rollback", runner=fail_second_observer, tmux_bin="tmux")
        with self.assertRaisesRegex(Exception, "size or position no space"):
            room.create(
                ["owner"],
                [["observer", "one"], ["observer", "two"]],
            )

        self.assertNotIn(room.session_name, runner.sessions)
        self.assertEqual(1, len(runner.commands("kill-session")))

    def test_projection_explains_mouse_scrollback_without_keyboard_chords(self):
        rendered = render_reviewer(FakeStore(), "codex")

        self.assertIn("scroll: mouse wheel", rendered)
        self.assertNotIn("Ctrl-b", rendered)
        self.assertNotIn("PgUp", rendered)

    def test_observer_titles_must_match_the_selected_reviewers(self):
        runner = FakeRunner()
        room = TmuxRoom("title-mismatch", runner=runner, tmux_bin="tmux")

        with self.assertRaisesRegex(ValueError, "observer_titles must match"):
            room.create(
                ["owner"],
                [["observer"], ["observer"]],
                observer_titles=["reviewer kimi"],
            )

    def test_session_names_are_safe_stable_and_unique(self):
        first = session_name_for_job("Case / One")
        self.assertEqual(first, session_name_for_job("Case / One"))
        self.assertNotEqual(first, session_name_for_job("Case---One"))
        self.assertTrue(first.startswith("review-"))
        self.assertLessEqual(set(first), set("abcdefghijklmnopqrstuvwxyz0123456789-"))

    def test_existing_room_requires_explicit_replace(self):
        runner = FakeRunner()
        room = TmuxRoom("same-job", runner=runner, tmux_bin="tmux")
        runner.sessions.add(room.session_name)
        with self.assertRaises(RoomExistsError):
            room.create(["owner"], [["observer"]])
        self.assertFalse(runner.commands("kill-session"))
        room.create(["owner"], [["observer"]], replace=True)
        self.assertEqual(len(runner.commands("kill-session")), 1)

    def test_describe_reports_input_disabled(self):
        runner = FakeRunner()
        room = TmuxRoom("described", runner=runner, tmux_bin="tmux")
        runner.sessions.add(room.session_name)
        description = room.describe()
        self.assertEqual(description["panes"][0].title, "owner")
        self.assertTrue(description["panes"][1].input_off)


if __name__ == "__main__":
    unittest.main()
