"""Lock manager implementing table-level **strict two-phase locking** (strict 2PL).

* Shared (S) locks for reading, exclusive (X) locks for writing.
* S is compatible with S; X is compatible with nothing.
* A transaction holding S may *upgrade* to X when it is the only holder.
* Locks are held until COMMIT/ROLLBACK (the "strict" part), which prevents
  other transactions from seeing uncommitted data (no dirty reads) and makes
  every schedule conflict-serializable.
* **Deadlock detection**: before a transaction waits, the manager checks the
  wait-for graph. If waiting would close a cycle, the requester gets a
  :class:`DeadlockError` instead of hanging forever. A timeout is the last
  line of defence.

Granularity is the whole table. That is coarse — two transactions updating
different rows of one table serialize — but simple, correct, and it also
prevents phantoms. Row-level locking is listed as future work.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from pydb.errors import DeadlockError, LockTimeoutError


class LockMode(Enum):
    SHARED = "S"
    EXCLUSIVE = "X"


@dataclass
class _LockState:
    holders: dict[int, LockMode] = field(default_factory=dict)
    waiting: dict[int, LockMode] = field(default_factory=dict)


class LockManager:
    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout
        self._locks: dict[str, _LockState] = {}
        self._held: dict[int, set[str]] = {}
        self._waits_for: dict[int, str] = {}  # txn -> resource it is blocked on
        self._cond = threading.Condition()
        self.deadlocks = 0
        self.waits = 0

    # ------------------------------------------------------------------ public API
    def acquire(self, txn_id: int, resource: str, mode: LockMode) -> None:
        with self._cond:
            state = self._locks.setdefault(resource, _LockState())
            deadline = time.monotonic() + self.timeout
            waited = False
            while not self._grantable(state, txn_id, mode):
                if self._would_deadlock(txn_id, resource, mode):
                    self.deadlocks += 1
                    raise DeadlockError(
                        f"deadlock detected: T{txn_id} waiting for {mode.value} lock on {resource!r}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop_waiting(txn_id, state)
                    raise LockTimeoutError(
                        f"T{txn_id} timed out waiting for {mode.value} lock on {resource!r}"
                    )
                if not waited:
                    self.waits += 1
                    waited = True
                state.waiting[txn_id] = mode
                self._waits_for[txn_id] = resource
                self._cond.wait(min(remaining, 0.05))
            self._stop_waiting(txn_id, state)
            current = state.holders.get(txn_id)
            if current is not LockMode.EXCLUSIVE:
                state.holders[txn_id] = mode if current is None else (
                    LockMode.EXCLUSIVE if mode is LockMode.EXCLUSIVE else current
                )
            self._held.setdefault(txn_id, set()).add(resource)

    def release_all(self, txn_id: int) -> None:
        with self._cond:
            for resource in self._held.pop(txn_id, set()):
                state = self._locks.get(resource)
                if state:
                    state.holders.pop(txn_id, None)
                    if not state.holders and not state.waiting:
                        del self._locks[resource]
            self._waits_for.pop(txn_id, None)
            self._cond.notify_all()

    def holders(self, resource: str) -> dict[int, LockMode]:
        with self._cond:
            state = self._locks.get(resource)
            return dict(state.holders) if state else {}

    def held_by(self, txn_id: int) -> dict[str, LockMode]:
        with self._cond:
            return {r: self._locks[r].holders[txn_id] for r in self._held.get(txn_id, ()) if r in self._locks}

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _blockers(state: _LockState, txn_id: int, mode: LockMode) -> set[int]:
        """Transactions that prevent ``txn_id`` from getting ``mode`` right now.

        Besides incompatible holders, a *new* shared request also queues behind
        a transaction already waiting for an exclusive lock. Without this rule a
        stream of readers could starve a writer forever.
        """
        blockers = {t for t, m in state.holders.items()
                    if t != txn_id and (mode is LockMode.EXCLUSIVE or m is LockMode.EXCLUSIVE)}
        if mode is LockMode.SHARED and txn_id not in state.holders:
            blockers |= {t for t, m in state.waiting.items() if t != txn_id and m is LockMode.EXCLUSIVE}
        return blockers

    def _grantable(self, state: _LockState, txn_id: int, mode: LockMode) -> bool:
        return not self._blockers(state, txn_id, mode)

    def _stop_waiting(self, txn_id: int, state: _LockState) -> None:
        state.waiting.pop(txn_id, None)
        self._waits_for.pop(txn_id, None)

    def _would_deadlock(self, txn_id: int, resource: str, mode: LockMode) -> bool:
        """DFS over the wait-for graph: would waiting on ``resource`` close a cycle?"""
        seen: set[int] = set()
        stack = list(self._blockers(self._locks[resource], txn_id, mode))
        while stack:
            t = stack.pop()
            if t == txn_id:
                return True
            if t in seen:
                continue
            seen.add(t)
            blocked_on = self._waits_for.get(t)
            if blocked_on is not None and blocked_on in self._locks:
                state = self._locks[blocked_on]
                stack.extend(self._blockers(state, t, state.waiting.get(t, LockMode.EXCLUSIVE)))
        return False
