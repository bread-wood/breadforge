"""Unit tests for OrchestratorLock."""

from __future__ import annotations

import fcntl
import multiprocessing
from pathlib import Path
from unittest.mock import patch

import pytest

from breadforge.graph.lock import LockError, OrchestratorLock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _try_acquire(lock_path: Path, result_queue: multiprocessing.Queue[bool]) -> None:
    """Worker: try to acquire the lock; put True if acquired, False if blocked."""
    try:
        with open(lock_path, "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result_queue.put(True)
                # Hold briefly so parent can observe
                import time

                time.sleep(0.2)
                fcntl.flock(f, fcntl.LOCK_UN)
            except BlockingIOError:
                result_queue.put(False)
    except Exception:
        result_queue.put(False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOrchestratorLockInit:
    def test_properties(self, tmp_path: Path) -> None:
        lock = OrchestratorLock(owner="acme", repo="widgets")
        assert lock.owner == "acme"
        assert lock.repo == "widgets"

    def test_lock_path_location(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock = OrchestratorLock(owner="org", repo="proj")
        assert lock._lock_path == tmp_path / ".breadforge" / "locks" / "org-proj.lock"

    def test_lock_file_initially_none(self) -> None:
        lock = OrchestratorLock(owner="x", repo="y")
        assert lock._lock_file is None


class TestOrchestratorLockAcquire:
    def test_enter_creates_lock_dir(self, tmp_path: Path) -> None:
        lock_dir = tmp_path / ".breadforge" / "locks"
        assert not lock_dir.exists()
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock = OrchestratorLock(owner="a", repo="b")
        with lock:
            assert lock_dir.is_dir()

    def test_enter_creates_lock_file(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock = OrchestratorLock(owner="a", repo="b")
        with lock:
            assert lock._lock_path.exists()

    def test_enter_returns_self(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock = OrchestratorLock(owner="a", repo="b")
        result = lock.__enter__()
        lock.__exit__(None, None, None)
        assert result is lock

    def test_lock_file_open_during_context(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock = OrchestratorLock(owner="a", repo="b")
        with lock:
            assert lock._lock_file is not None
            assert not lock._lock_file.closed

    def test_lock_file_closed_after_context(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock = OrchestratorLock(owner="a", repo="b")
        with lock:
            f = lock._lock_file
        assert f is not None
        assert f.closed
        assert lock._lock_file is None

    def test_lock_file_none_after_exit(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock = OrchestratorLock(owner="a", repo="b")
        with lock:
            pass
        assert lock._lock_file is None

    def test_reentrant_same_object_sequential(self, tmp_path: Path) -> None:
        """Same object can be re-entered after being released."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock = OrchestratorLock(owner="a", repo="b")
        with lock:
            pass
        with lock:  # should succeed — lock was released
            assert lock._lock_file is not None


class TestOrchestratorLockBlocking:
    def test_blocked_prints_message_and_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When another process holds the lock, prints message and raises SystemExit(1)."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock1 = OrchestratorLock(owner="org", repo="myrepo")
            lock2 = OrchestratorLock(owner="org", repo="myrepo")

        with lock1:
            with pytest.raises(SystemExit) as exc_info:
                lock2.__enter__()
            assert exc_info.value.code == 1
            captured = capsys.readouterr()
            assert "another breadforge run is active for myrepo" in captured.out

    def test_blocked_lock_file_is_closed(self, tmp_path: Path) -> None:
        """On blocking, the file opened for locking is closed (no leak)."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock1 = OrchestratorLock(owner="org", repo="myrepo")
            lock2 = OrchestratorLock(owner="org", repo="myrepo")

        with lock1:
            with pytest.raises(SystemExit):
                lock2.__enter__()
            assert lock2._lock_file is None

    def test_blocked_does_not_hold_lock(self, tmp_path: Path) -> None:
        """After a failed acquire, the file descriptor is released (no double-lock)."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock1 = OrchestratorLock(owner="org", repo="myrepo")
            lock2 = OrchestratorLock(owner="org", repo="myrepo")

        with lock1, pytest.raises(SystemExit):
            lock2.__enter__()
        # lock1 released — lock2 should now be acquirable
        with lock2:
            assert lock2._lock_file is not None


class TestOrchestratorLockRelease:
    def test_exclusive_lock_blocks_second_flock(self, tmp_path: Path) -> None:
        """While lock is held, a direct flock attempt on the same file should fail."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock = OrchestratorLock(owner="a", repo="b")

        with lock:  # noqa: SIM117
            # Try to grab the lock from within the same process (should block / fail)
            with open(lock._lock_path, "w") as f:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_lock_released_after_context_exit(self, tmp_path: Path) -> None:
        """After exiting the context, the same file can be locked again."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock = OrchestratorLock(owner="a", repo="b")

        with lock:
            pass

        # Should be able to acquire an exclusive lock after release
        with open(lock._lock_path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)

    def test_exit_with_exception_releases_lock(self, tmp_path: Path) -> None:
        """Lock is released even when an exception occurs inside the context."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock = OrchestratorLock(owner="a", repo="b")

        try:
            with lock:
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        assert lock._lock_file is None
        # Lock should be free
        with open(lock._lock_path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)


class TestLockError:
    def test_lock_error_is_exception(self) -> None:
        assert issubclass(LockError, Exception)

    def test_lock_error_can_be_raised(self) -> None:
        with pytest.raises(LockError):
            raise LockError("something went wrong")

    def test_lock_error_message(self) -> None:
        err = LockError("test message")
        assert str(err) == "test message"


class TestOrchestratorLockDifferentRepos:
    def test_different_repos_do_not_conflict(self, tmp_path: Path) -> None:
        """Locks for different repos use different files and don't interfere."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock_a = OrchestratorLock(owner="org", repo="repo-a")
            lock_b = OrchestratorLock(owner="org", repo="repo-b")

        with lock_a, lock_b:  # should not raise
            assert lock_b._lock_file is not None

    def test_different_owners_do_not_conflict(self, tmp_path: Path) -> None:
        """Locks for different owners don't interfere even with same repo name."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock_a = OrchestratorLock(owner="owner-a", repo="myrepo")
            lock_b = OrchestratorLock(owner="owner-b", repo="myrepo")

        with lock_a, lock_b:
            assert lock_b._lock_file is not None

    def test_lock_path_encodes_owner_and_repo(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            lock = OrchestratorLock(owner="my-org", repo="my-repo")
        assert lock._lock_path.name == "my-org-my-repo.lock"
