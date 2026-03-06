"""OrchestratorLock — prevents concurrent breadforge runs on the same repo."""

from __future__ import annotations

import fcntl
from pathlib import Path


class LockError(Exception):
    pass


class OrchestratorLock:
    """File-based lock using fcntl.flock.

    Acquires an exclusive non-blocking lock on
    ``~/.breadforge/locks/{owner}-{repo}.lock``.  If the lock is already held
    by another process, prints a message and raises ``SystemExit(1)``.

    Usage::

        with OrchestratorLock(owner="bread-wood", repo="breadforge"):
            ...
    """

    def __init__(self, owner: str, repo: str) -> None:
        self._owner = owner
        self._repo = repo
        self._lock_path = Path.home() / ".breadforge" / "locks" / f"{owner}-{repo}.lock"
        self._lock_file = None

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def repo(self) -> str:
        return self._repo

    def __enter__(self) -> OrchestratorLock:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(self._lock_path, "w")  # noqa: SIM115
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            self._lock_file = None
            print(f"another breadforge run is active for {self._repo}")
            raise SystemExit(1) from None
        return self

    def __exit__(self, *args: object) -> None:
        if self._lock_file is not None:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None
