import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mutual_review_room import broker


class ReviewBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="review-broker-")
        self.root = Path(self.temporary_directory.name) / "state"
        self.root.mkdir(mode=0o700)
        self.job_dir = self.root / "job_test"
        self.job_dir.mkdir(mode=0o700)
        self.paths = broker.prepare_mailbox(self.root, "job_test")
        self.token = "owner-token-for-broker-tests"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_private_layout_atomic_queue_and_file_modes(self) -> None:
        for directory in (
            self.paths.base,
            self.paths.inbox,
            self.paths.processing,
            self.paths.outbox,
        ):
            self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))

        real_rename = os.rename
        observed = []

        def checking_rename(source, target, **kwargs):
            observed.append((source, target))
            self.assertFalse((self.paths.inbox / target).exists())
            return real_rename(source, target, **kwargs)

        with patch.object(broker.os, "rename", side_effect=checking_rename):
            request_id = broker.submit(
                self.paths,
                self.token,
                "dispatch",
                {"prompt": "full prompt travels in the mailbox"},
                request_id="request_atomic",
            )

        request_path = self.paths.inbox / f"{request_id}.json"
        self.assertTrue(observed)
        self.assertEqual(0o600, stat.S_IMODE(request_path.stat().st_mode))
        self.assertEqual([], list(self.paths.inbox.glob(".*.tmp")))
        envelope = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertEqual("full prompt travels in the mailbox", envelope["payload"]["prompt"])

    def test_symlinked_mailbox_directory_and_lock_are_rejected(self) -> None:
        alternate = self.paths.base / "alternate-inbox"
        alternate.mkdir(mode=0o700)
        original = self.paths.base / "original-inbox"
        self.paths.inbox.rename(original)
        self.paths.inbox.symlink_to(alternate, target_is_directory=True)
        with self.assertRaises(broker.MailboxSecurityError):
            broker.submit(self.paths, self.token, "dispatch", {"prompt": "unsafe"})

        self.paths.inbox.unlink()
        original.rename(self.paths.inbox)
        self.paths.lock.symlink_to(self.paths.inbox / "not-a-lock")
        with self.assertRaises(broker.MailboxSecurityError):
            broker.serve_one(self.paths, self.token, lambda action, payload, request_id: {})

    def test_tampered_request_is_not_handled_and_tampered_response_is_rejected(self) -> None:
        calls = []
        request_id = broker.submit(
            self.paths,
            self.token,
            "dispatch",
            {"prompt": "original"},
            request_id="request_tampered",
        )
        request_path = self.paths.inbox / f"{request_id}.json"
        envelope = json.loads(request_path.read_text(encoding="utf-8"))
        envelope["payload"]["prompt"] = "changed"
        request_path.write_text(json.dumps(envelope), encoding="utf-8")
        os.chmod(request_path, 0o600)

        broker.serve_one(
            self.paths,
            self.token,
            lambda action, payload, claimed_id: calls.append(claimed_id) or {},
        )
        response = broker.wait(self.paths, self.token, request_id)
        self.assertEqual([], calls)
        self.assertEqual("error", response.status)
        self.assertEqual("invalid_request", response.error["code"])

        clean_id = broker.submit(
            self.paths,
            self.token,
            "dispatch",
            {"prompt": "clean"},
            request_id="request_response_tamper",
        )
        broker.serve_one(
            self.paths,
            self.token,
            lambda action, payload, claimed_id: {"answer": "trusted"},
        )
        response_path = self.paths.outbox / f"{clean_id}.json"
        response_envelope = json.loads(response_path.read_text(encoding="utf-8"))
        response_envelope["result"]["answer"] = "forged"
        response_path.write_text(json.dumps(response_envelope), encoding="utf-8")
        os.chmod(response_path, 0o600)
        with self.assertRaises(broker.SignatureError):
            broker.wait(self.paths, self.token, clean_id)

    def test_strict_schema_version_and_size_are_enforced(self) -> None:
        with self.assertRaises(broker.ProtocolError):
            broker.submit(
                self.paths,
                self.token,
                "dispatch",
                {"prompt": "x" * broker.MAX_REQUEST_BYTES},
            )

        request_id = broker.submit(
            self.paths,
            self.token,
            "dispatch",
            {"prompt": "schema"},
            request_id="request_schema",
        )
        request_path = self.paths.inbox / f"{request_id}.json"
        envelope = json.loads(request_path.read_text(encoding="utf-8"))
        body = {key: value for key, value in envelope.items() if key != "signature"}
        body["version"] = 2
        request_path.write_bytes(
            broker._encode_signed(body, self.token.encode(), broker.MAX_REQUEST_BYTES)
        )
        os.chmod(request_path, 0o600)
        broker.serve_one(
            self.paths,
            self.token,
            lambda action, payload, claimed_id: self.fail("handler must not run"),
        )
        response = broker.wait(self.paths, self.token, request_id)
        self.assertEqual("invalid_request", response.error["code"])

    def test_serve_one_processes_each_request_once(self) -> None:
        calls = []
        for request_id in ("request_a", "request_b"):
            broker.submit(
                self.paths,
                self.token,
                "dispatch",
                {"prompt": request_id},
                request_id=request_id,
            )

        handled = broker.serve_one(
            self.paths,
            self.token,
            lambda action, payload, request_id: calls.append(request_id)
            or {"answer": payload["prompt"]},
        )
        self.assertTrue(handled)
        self.assertEqual(["request_a"], calls)
        self.assertTrue((self.paths.inbox / "request_b.json").exists())
        self.assertTrue((self.paths.processing / "request_a.json").exists())
        self.assertEqual("ok", broker.wait(self.paths, self.token, "request_a").status)

        broker.serve_one(
            self.paths,
            self.token,
            lambda action, payload, request_id: calls.append(request_id) or {"answer": "b"},
        )
        self.assertEqual(["request_a", "request_b"], calls)
        self.assertFalse(broker.serve_one(self.paths, self.token, lambda *args: {}))

    def test_heartbeat_continues_during_handler_and_client_disconnect_does_not_cancel(self) -> None:
        request_id = broker.submit(
            self.paths,
            self.token,
            "dispatch",
            {"prompt": "slow"},
            request_id="request_slow",
        )
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def handler(action, payload, claimed_id):
            entered.set()
            release.wait(2)
            return {"answer": "completed after client left"}

        def run_broker():
            try:
                broker.serve_one(
                    self.paths,
                    self.token,
                    handler,
                    heartbeat_interval=0.01,
                )
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run_broker)
        thread.start()
        self.assertTrue(entered.wait(1))
        time.sleep(0.12)
        heartbeat = json.loads(self.paths.heartbeat.read_text(encoding="utf-8"))
        self.assertLess(time.time_ns() - heartbeat["updated_ns"], 80_000_000)
        # No client wait remains connected; the broker still finishes the work.
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        response = broker.wait(
            self.paths,
            self.token,
            request_id,
            stale_after=0.03,
            startup_grace=0,
        )
        self.assertEqual("completed after client left", response.result["answer"])

    def test_stale_heartbeat_fails_fast(self) -> None:
        broker.serve_one(self.paths, self.token, lambda *args: {})
        time.sleep(0.04)
        started = time.monotonic()
        with self.assertRaises(broker.StaleHeartbeatError):
            broker.wait(
                self.paths,
                self.token,
                "request_missing",
                timeout=2,
                stale_after=0.02,
                startup_grace=0,
            )
        self.assertLess(time.monotonic() - started, 0.3)

    def test_processing_entry_is_diagnosed_but_never_replayed(self) -> None:
        request_id = broker.submit(
            self.paths,
            self.token,
            "dispatch",
            {"prompt": "provider may already have been called"},
            request_id="request_orphaned",
        )
        request_path = self.paths.inbox / f"{request_id}.json"
        processing_path = self.paths.processing / request_path.name
        os.replace(request_path, processing_path)
        calls = []

        handled = broker.serve_one(
            self.paths,
            self.token,
            lambda action, payload, claimed_id: calls.append(claimed_id) or {},
        )
        self.assertFalse(handled)
        self.assertEqual([], calls)
        self.assertTrue(processing_path.exists())
        response = broker.wait(self.paths, self.token, request_id)
        self.assertEqual("abandoned", response.status)
        self.assertEqual("broker_interrupted", response.error["code"])

    def test_broker_lock_allows_only_one_server(self) -> None:
        stop = threading.Event()
        errors = []

        def run_server():
            try:
                broker.serve_forever(
                    self.paths,
                    self.token,
                    lambda *args: {},
                    poll_interval=0.01,
                    heartbeat_interval=0.01,
                    stop_event=stop,
                )
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run_server)
        thread.start()
        deadline = time.monotonic() + 1
        while not self.paths.heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        with self.assertRaises(broker.BrokerAlreadyRunning):
            broker.serve_one(self.paths, self.token, lambda *args: {})
        stop.set()
        thread.join(2)
        self.assertEqual([], errors)

    def test_submitting_writes_only_inside_the_inbox(self) -> None:
        """A sandboxed owner is given the inbox and nothing else to write."""

        self.assertEqual(self.paths.inbox, self.paths.queue_lock.parent)

        inbox_inode = self.paths.inbox.stat().st_ino
        writes: list[str] = []
        escaped: list[str] = []
        real_open = os.open

        def recording_open(path, flags, *args, **kwargs):
            directory_fd = kwargs.get("dir_fd")
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                writes.append(str(path))
                if directory_fd is None:
                    if Path(path).parent != self.paths.inbox:
                        escaped.append(str(path))
                elif os.fstat(directory_fd).st_ino != inbox_inode:
                    escaped.append(str(path))
            return real_open(path, flags, *args, **kwargs)

        with patch.object(broker.os, "open", recording_open):
            broker.submit(self.paths, self.token, "dispatch", {"round": 1})

        self.assertTrue(writes)
        self.assertEqual([], escaped, f"writes escaped the inbox: {escaped}")
        self.assertIn(self.paths.queue_lock.name, writes)

    def test_the_queue_lock_is_not_mistaken_for_a_queued_request(self) -> None:
        request_id = broker.submit(self.paths, self.token, "dispatch", {"round": 1})
        self.assertTrue(self.paths.queue_lock.exists())

        queued = broker._message_files(self.paths.inbox)

        self.assertEqual([f"{request_id}.json"], [path.name for path in queued])


if __name__ == "__main__":
    unittest.main()
