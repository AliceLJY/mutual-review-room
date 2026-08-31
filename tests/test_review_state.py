import sqlite3
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from mutual_review_room.state import (
    DuplicateRequestError,
    NotFoundError,
    ReviewState,
    StateConflictError,
    ValidationError,
)


class ReviewStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "review-state"
        self.state = ReviewState(self.root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def create_job(self, *, reviewers: int = 2, job_id: str = "job_test"):
        specs = [
            {
                "reviewer_id": f"reviewer_{index}",
                "provider": "codex" if index == 1 else "kimi",
                "model": "default",
                "cwd": "/tmp",
                "capability": {"streaming": True, "slot": index},
            }
            for index in range(1, reviewers + 1)
        ]
        return self.state.create_job(
            owner_provider="claude",
            owner_model="sonnet",
            owner_cwd="/tmp",
            reviewers=specs,
            job_id=job_id,
            max_rounds=3,
        )

    def bind_reviewers(self, job_id: str, reviewers: int = 2) -> None:
        for index in range(1, reviewers + 1):
            self.state.set_reviewer_session(
                job_id, f"reviewer_{index}", f"native-session-{index}"
            )

    def test_new_job_has_private_files_and_empty_native_sessions(self) -> None:
        created = self.create_job()

        self.assertIsNone(created.job.owner_session_id)
        self.assertTrue(all(item.native_session_id is None for item in created.reviewers))
        self.assertEqual(
            stat.S_IMODE(self.root.stat().st_mode),
            0o700,
        )
        job_directory = self.root / created.job.job_id
        self.assertEqual(stat.S_IMODE(job_directory.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((job_directory / "state.sqlite3").stat().st_mode), 0o600
        )
        self.assertEqual(
            stat.S_IMODE((job_directory / "owner.token").stat().st_mode), 0o600
        )

    def test_owner_token_is_hashed_verified_and_never_recorded(self) -> None:
        created = self.create_job()

        self.assertTrue(
            self.state.verify_owner_token(created.job.job_id, created.owner_token)
        )
        self.assertFalse(self.state.verify_owner_token(created.job.job_id, "wrong"))
        with sqlite3.connect(self.root / "job_test" / "state.sqlite3") as connection:
            stored_hash = connection.execute("SELECT token_hash FROM jobs").fetchone()[0]
        self.assertNotEqual(stored_hash, created.owner_token)
        self.assertFalse(
            any(created.owner_token in event.content for event in self.state.list_events("job_test"))
        )
        with self.assertRaises(ValidationError):
            self.state.append_event(
                "job_test",
                direction="system",
                event_type="unsafe",
                status="ready",
                content=f"leak {created.owner_token}",
            )

    def test_events_are_monotonic_and_sqlite_triggers_make_them_append_only(self) -> None:
        self.create_job()
        self.state.append_event(
            "job_test",
            direction="owner_to_room",
            event_type="note",
            status="ready",
            content="one",
        )
        self.state.append_event(
            "job_test",
            direction="owner_to_room",
            event_type="note",
            status="ready",
            content="two",
        )
        events = self.state.list_events("job_test")
        self.assertEqual(
            [event.sequence for event in events],
            sorted({event.sequence for event in events}),
        )
        after_first = self.state.list_events("job_test", after=events[0].sequence)
        self.assertEqual(after_first, events[1:])

        db_path = self.root / "job_test" / "state.sqlite3"
        connection = sqlite3.connect(db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE events SET content = 'changed'")
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM events")
        finally:
            connection.close()

    def test_duplicate_request_id_and_reviewer_round_are_rejected(self) -> None:
        self.create_job()
        self.bind_reviewers("job_test")
        self.state.begin_request(
            "job_test",
            "reviewer_1",
            round=1,
            prompt="cold review",
            request_id="request_one",
        )

        with self.assertRaises(DuplicateRequestError):
            self.state.begin_request(
                "job_test",
                "reviewer_2",
                round=1,
                prompt="different reviewer but duplicate ID",
                request_id="request_one",
            )
        with self.assertRaises(DuplicateRequestError):
            self.state.begin_request(
                "job_test",
                "reviewer_1",
                round=1,
                prompt="duplicate round",
                request_id="request_other",
            )
        self.assertEqual(len(self.state.list_requests("job_test")), 1)

    def test_follow_up_auto_parents_and_rejects_cross_reviewer_parent(self) -> None:
        self.create_job()
        self.bind_reviewers("job_test")
        first = self.state.begin_request(
            "job_test",
            "reviewer_1",
            round=1,
            prompt="round one",
            request_id="r1_one",
        )
        self.state.complete_request("job_test", first.request_id, "answer one")
        follow_up = self.state.begin_request(
            "job_test",
            "reviewer_1",
            round=2,
            prompt="round two",
            request_id="r2_one",
        )
        self.assertEqual(follow_up.parent_request_id, first.request_id)
        self.assertEqual(follow_up.session_id, first.session_id)

        other = self.state.begin_request(
            "job_test",
            "reviewer_2",
            round=1,
            prompt="other round one",
            request_id="r1_two",
        )
        self.state.complete_request("job_test", other.request_id, "other answer")
        with self.assertRaises(StateConflictError):
            self.state.begin_request(
                "job_test",
                "reviewer_2",
                round=2,
                prompt="bad parent",
                request_id="r2_two",
                parent_request_id=first.request_id,
            )

    def test_event_request_ownership_is_derived_and_mismatch_is_rejected(self) -> None:
        self.create_job()
        self.bind_reviewers("job_test")
        request = self.state.begin_request(
            "job_test",
            "reviewer_1",
            round=1,
            prompt="question",
            request_id="request_one",
        )
        event = self.state.append_event(
            "job_test",
            request_id=request.request_id,
            direction="reviewer_to_owner",
            event_type="output_delta",
            status="running",
            content="partial",
        )
        self.assertEqual(event.reviewer_id, request.reviewer_id)
        self.assertEqual(event.session_id, request.session_id)
        self.assertEqual(event.round, request.round)

        with self.assertRaises(StateConflictError):
            self.state.append_event(
                "job_test",
                request_id=request.request_id,
                reviewer_id="reviewer_2",
                direction="reviewer_to_owner",
                event_type="output_delta",
                status="running",
                content="misattributed",
            )

    def test_jobs_are_isolated_and_a_second_job_has_no_inherited_session(self) -> None:
        self.create_job(job_id="job_one")
        self.bind_reviewers("job_one")
        self.state.set_owner_session("job_one", "owner-native-one")
        self.state.begin_request(
            "job_one",
            "reviewer_1",
            round=1,
            prompt="job one",
            request_id="job_one_request",
        )

        second = self.create_job(job_id="job_two")
        self.assertIsNone(second.job.owner_session_id)
        self.assertTrue(all(item.native_session_id is None for item in second.reviewers))
        self.assertEqual(self.state.list_requests("job_two"), [])
        with self.assertRaises(NotFoundError):
            self.state.get_request("job_two", "job_one_request")
        self.assertNotEqual(
            self.root / "job_one" / "state.sqlite3",
            self.root / "job_two" / "state.sqlite3",
        )

    def test_completed_job_rejects_new_dispatch(self) -> None:
        self.create_job(reviewers=1)
        self.bind_reviewers("job_test", reviewers=1)
        self.state.complete_job("job_test", "Final synthesis.")

        with self.assertRaisesRegex(StateConflictError, "job is already complete"):
            self.state.begin_request(
                "job_test",
                "reviewer_1",
                round=1,
                prompt="must not reopen a terminal job",
                request_id="request_after_completion",
            )

        self.assertEqual(self.state.list_requests("job_test"), [])
        self.assertEqual(
            len(self.state.list_events("job_test", event_type="job_completed")), 1
        )
        self.assertEqual(
            self.state.list_events("job_test", event_type="job_completed")[0].content,
            "Final synthesis.",
        )

    def test_complete_rejects_running_request_in_the_terminal_transaction(self) -> None:
        self.create_job(reviewers=1)
        self.bind_reviewers("job_test", reviewers=1)
        request = self.state.begin_request(
            "job_test",
            "reviewer_1",
            round=1,
            prompt="still in flight",
            request_id="running_request",
        )

        with self.assertRaisesRegex(StateConflictError, "running request"):
            self.state.complete_job("job_test", "Final synthesis.")

        self.assertEqual(self.state.get_job("job_test").status, "running")
        self.assertEqual(
            self.state.get_request("job_test", request.request_id).status, "running"
        )
        self.assertEqual(
            self.state.list_events("job_test", event_type="job_completed"), []
        )

    def test_repeated_complete_is_idempotent_and_terminal_status_cannot_reverse(self) -> None:
        self.create_job(reviewers=1)

        first = self.state.complete_job("job_test", "Final synthesis.")
        second = self.state.complete_job("job_test", "Final synthesis.")

        self.assertEqual(first.status, "complete")
        self.assertEqual(second.status, "complete")
        self.assertEqual(
            len(self.state.list_events("job_test", event_type="job_completed")), 1
        )
        with self.assertRaisesRegex(StateConflictError, "already terminal"):
            self.state.fail_job("job_test", "must not reverse completion")
        self.assertEqual(self.state.get_job("job_test").status, "complete")
        self.assertEqual(
            self.state.list_events("job_test", event_type="job_failed"), []
        )

    def test_terminal_replay_rejects_a_different_verdict_or_error(self) -> None:
        self.create_job(reviewers=1, job_id="completed_job")
        self.state.complete_job("completed_job", "Original synthesis.")

        with self.assertRaisesRegex(StateConflictError, "different decision payload"):
            self.state.complete_job("completed_job", "Rewritten synthesis.")

        completion_events = self.state.list_events(
            "completed_job", event_type="job_completed"
        )
        self.assertEqual(
            [event.content for event in completion_events], ["Original synthesis."]
        )

        self.create_job(reviewers=1, job_id="failed_job")
        first = self.state.fail_job("failed_job", "Original failure.")
        replay = self.state.fail_job("failed_job", "Original failure.")
        self.assertEqual(first.status, "failed")
        self.assertEqual(replay.status, "failed")

        with self.assertRaisesRegex(StateConflictError, "different decision payload"):
            self.state.fail_job("failed_job", "Different failure.")

        failure_events = self.state.list_events("failed_job", event_type="job_failed")
        self.assertEqual(
            [event.content for event in failure_events], ["Original failure."]
        )

    def test_complete_requires_a_nonempty_final_verdict(self) -> None:
        self.create_job(reviewers=1)

        with self.assertRaisesRegex(ValidationError, "verdict must not be empty"):
            self.state.complete_job("job_test", "  ")

        self.assertEqual("ready", self.state.get_job("job_test").status)
        self.assertEqual(
            [], self.state.list_events("job_test", event_type="job_completed")
        )

    def test_request_settlement_cannot_append_after_job_completed(self) -> None:
        self.create_job(reviewers=1)
        self.bind_reviewers("job_test", reviewers=1)
        request = self.state.begin_request(
            "job_test",
            "reviewer_1",
            round=1,
            prompt="legacy in-flight request",
            request_id="legacy_running_request",
        )
        self.state.append_event(
            "job_test",
            direction="system",
            event_type="job_completed",
            status="complete",
            content="",
        )
        with sqlite3.connect(self.root / "job_test" / "state.sqlite3") as connection:
            connection.execute(
                "UPDATE jobs SET status = 'complete' WHERE job_id = ?",
                ("job_test",),
            )
        completion_sequence = self.state.list_events(
            "job_test", event_type="job_completed"
        )[-1].sequence

        with self.assertRaisesRegex(StateConflictError, "job is already complete"):
            self.state.complete_request("job_test", request.request_id, "late response")

        self.assertEqual(
            self.state.list_events("job_test", after=completion_sequence), []
        )
        self.assertEqual(
            self.state.get_request("job_test", request.request_id).status, "running"
        )

    def test_failed_terminal_job_cannot_be_completed_or_reopened(self) -> None:
        self.create_job(reviewers=1)
        self.state.fail_job("job_test", "owner unavailable")

        with self.assertRaisesRegex(StateConflictError, "already terminal"):
            self.state.complete_job("job_test", "Final synthesis.")
        with self.assertRaisesRegex(StateConflictError, "job is already failed"):
            self.state.begin_request(
                "job_test",
                "reviewer_1",
                round=1,
                prompt="must not reopen",
                request_id="request_after_failure",
            )

        self.assertEqual(self.state.get_job("job_test").status, "failed")
        self.assertEqual(
            len(self.state.list_events("job_test", event_type="job_failed")), 1
        )
        self.assertEqual(
            self.state.list_events("job_test", event_type="job_completed"), []
        )

    def test_fail_job_rejects_a_running_request_before_terminal_event(self) -> None:
        self.create_job(reviewers=1)
        self.bind_reviewers("job_test", reviewers=1)
        request = self.state.begin_request(
            "job_test",
            "reviewer_1",
            round=1,
            prompt="still running",
            request_id="request_before_failure",
        )

        with self.assertRaisesRegex(StateConflictError, "running request"):
            self.state.fail_job("job_test", "owner failed")

        self.assertEqual(self.state.get_job("job_test").status, "running")
        self.assertEqual(
            self.state.get_request("job_test", request.request_id).status, "running"
        )
        self.assertEqual(
            self.state.list_events("job_test", event_type="job_failed"), []
        )

    def test_one_unavailable_reviewer_does_not_change_the_other(self) -> None:
        self.create_job()
        self.bind_reviewers("job_test")
        first = self.state.begin_request(
            "job_test",
            "reviewer_1",
            round=1,
            prompt="first",
            request_id="request_one",
        )
        second = self.state.begin_request(
            "job_test",
            "reviewer_2",
            round=1,
            prompt="second",
            request_id="request_two",
        )

        unavailable = self.state.mark_unavailable(
            "job_test", "reviewer_1", "provider quota exhausted"
        )
        completed = self.state.complete_request(
            "job_test", second.request_id, "still completed"
        )
        self.assertEqual(unavailable.status, "unavailable")
        self.assertEqual(
            self.state.get_request("job_test", first.request_id).status,
            "unavailable",
        )
        self.assertEqual(completed.status, "completed")
        self.assertEqual(
            self.state.get_reviewer("job_test", "reviewer_2").status, "ready"
        )
        self.assertEqual(self.state.get_job("job_test").status, "running")

    def test_recovery_marks_running_requests_interrupted_without_replay(self) -> None:
        self.create_job()
        self.bind_reviewers("job_test")
        request = self.state.begin_request(
            "job_test",
            "reviewer_1",
            round=1,
            prompt="in flight",
            request_id="request_one",
        )
        count_before = len(self.state.list_requests("job_test"))

        recovered = self.state.recover_job("job_test")
        self.assertEqual([item.request_id for item in recovered], [request.request_id])
        self.assertEqual(recovered[0].status, "interrupted")
        self.assertEqual(
            self.state.get_reviewer("job_test", "reviewer_1").status,
            "interrupted",
        )
        self.assertEqual(self.state.get_job("job_test").status, "interrupted")
        self.assertEqual(len(self.state.list_requests("job_test")), count_before)
        self.assertEqual(self.state.recover_job("job_test"), [])
        self.assertEqual(len(self.state.list_requests("job_test")), count_before)
        interruption_events = self.state.list_events(
            "job_test", event_type="request_interrupted"
        )
        self.assertEqual(len(interruption_events), 1)

    def test_crash_then_dispatch_is_rejected_until_recovery(self) -> None:
        self.create_job(reviewers=1)
        self.bind_reviewers("job_test", reviewers=1)
        first = self.state.begin_request(
            "job_test",
            "reviewer_1",
            round=1,
            prompt="in flight before process crash",
            request_id="request_one",
        )

        restarted_state = ReviewState(self.root)
        with self.assertRaisesRegex(
            StateConflictError, "already has running request request_one"
        ):
            restarted_state.begin_request(
                "job_test",
                "reviewer_1",
                round=2,
                prompt="must not overtake the unresolved request",
                request_id="request_two",
            )
        self.assertEqual(
            [request.request_id for request in restarted_state.list_requests("job_test")],
            [first.request_id],
        )

        restarted_state.recover_job("job_test")
        follow_up = restarted_state.begin_request(
            "job_test",
            "reviewer_1",
            round=2,
            prompt="safe after explicit recovery",
            request_id="request_two",
        )
        self.assertEqual(follow_up.parent_request_id, first.request_id)
        self.assertEqual(follow_up.status, "running")

    def test_late_session_binding_targets_exact_running_request(self) -> None:
        self.create_job()
        first = self.state.begin_request(
            "job_test",
            "reviewer_1",
            round=1,
            prompt="late-bound invocation",
            request_id="request_one",
        )
        other = self.state.begin_request(
            "job_test",
            "reviewer_2",
            round=1,
            prompt="other reviewer invocation",
            request_id="request_two",
        )

        with self.assertRaisesRegex(
            StateConflictError, "different reviewer"
        ):
            self.state.set_reviewer_session(
                "job_test",
                "reviewer_1",
                "native-one",
                request_id=other.request_id,
            )
        self.assertIsNone(
            self.state.get_reviewer("job_test", "reviewer_1").native_session_id
        )
        self.assertIsNone(self.state.get_request("job_test", first.request_id).session_id)
        self.assertIsNone(self.state.get_request("job_test", other.request_id).session_id)

        self.state.set_reviewer_session(
            "job_test",
            "reviewer_1",
            "native-one",
            request_id=first.request_id,
        )
        self.assertEqual(
            self.state.get_reviewer("job_test", "reviewer_1").native_session_id,
            "native-one",
        )
        self.assertEqual(
            self.state.get_request("job_test", first.request_id).session_id,
            "native-one",
        )
        self.assertIsNone(self.state.get_request("job_test", other.request_id).session_id)
        self.state.set_reviewer_session(
            "job_test",
            "reviewer_1",
            "native-one",
            request_id=first.request_id,
        )
        binding_events = self.state.list_events(
            "job_test", event_type="reviewer_session_bound"
        )
        self.assertEqual(binding_events[-1].request_id, first.request_id)

    def test_strict_validation_and_session_binding(self) -> None:
        with self.assertRaises(ValidationError):
            self.state.create_job(
                owner_provider="Claude Code",
                owner_model="sonnet",
                owner_cwd="/tmp",
                reviewers=[
                    {
                        "reviewer_id": "reviewer_1",
                        "provider": "codex",
                        "model": "default",
                        "cwd": "/tmp",
                    }
                ],
            )
        with self.assertRaises(ValidationError):
            self.state.create_job(
                owner_provider="claude",
                owner_model="sonnet",
                owner_cwd="/tmp",
                reviewers=[],
            )

        self.create_job()
        self.state.set_reviewer_session("job_test", "reviewer_1", "native-one")
        self.state.set_reviewer_session("job_test", "reviewer_1", "native-one")
        with self.assertRaises(StateConflictError):
            self.state.set_reviewer_session("job_test", "reviewer_1", "native-two")
        self.state.set_owner_session("job_test", "owner-one")
        with self.assertRaises(StateConflictError):
            self.state.set_owner_session("job_test", "owner-two")

    def test_reading_a_job_never_rewrites_correct_modes(self):
        """A sandboxed owner may read the control root but not write to it."""

        self.create_job()
        job_dir = self.root / "job_test"
        database = job_dir / "state.sqlite3"

        def refuse(*args, **kwargs):
            raise PermissionError(1, "Operation not permitted")

        with unittest.mock.patch("mutual_review_room.state.os.chmod", refuse):
            reopened = ReviewState(self.root)
            self.assertEqual("ready", reopened.get_job("job_test").status)
            self.assertEqual(2, len(reopened.list_reviewers("job_test")))
            self.assertTrue(reopened.list_events("job_test"))

        self.assertEqual(0o700, stat.S_IMODE(job_dir.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(database.stat().st_mode))

    def test_a_wrong_mode_is_still_corrected_on_open(self):
        self.create_job()
        job_dir = self.root / "job_test"
        database = job_dir / "state.sqlite3"
        job_dir.chmod(0o755)
        database.chmod(0o644)
        self.root.chmod(0o755)

        ReviewState(self.root).get_job("job_test")

        self.assertEqual(0o700, stat.S_IMODE(self.root.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(job_dir.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(database.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
