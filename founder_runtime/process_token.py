"""RIG Memory OS v10 — Process Token (Yellow #6 TOCTOU close).

Closes the TOCTOU race between `assert_active` and the actual
operation by issuing a monotonic process-local token at gate time
and re-checking it at operation time.

Pattern:
    token = assert_active_with_token(cockpit, op)
    try:
        # Operation body
        verify_or_abort(cockpit, token, op)
        # perform the operation
    finally:
        release_token(cockpit, token)

(assert_active_with_token / verify_or_abort live in
founder_runtime.cockpit_subscriber; they wrap issue_token / verify_token.)

The cockpit bumps its fence counter every state transition. Each
caller's token captures the pre-transition value. If the fence has
advanced past the token when `verify_token` is called, a kill/pause
landed during the operation — abort.

This is local-process concurrency control. For cross-process,
use PostgresCockpitStore + advisory locks (Yellow #3).
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ProcessToken:
    """A monotonic token captured at gate time.

    Captures the cockpit's pre-transition fence value; the caller
    passes it back to verify_token() before performing the operation.
    If the cockpit's fence has advanced past the token's value, the
    caller aborts.
    """
    operation: str
    fence_at_capture: int
    issued_at: float
    token_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class ProcessFence:
    """Local-process monotonic fence.

    MemoryCockpit composes one; bumping it on every state transition
    invalidates all in-flight tokens.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def capture(self) -> int:
        with self._lock:
            return self._value

    def bump(self) -> int:
        with self._lock:
            self._value += 1
            return self._value

    def current(self) -> int:
        with self._lock:
            return self._value


def issue_token(cockpit, operation: str) -> ProcessToken:
    """Issue a process token captured at the current fence value.

    Pair with verify_token() at the operation site to detect TOCTOU.
    """
    fence = getattr(cockpit, "_fence", None)
    if fence is None:
        # Cockpit doesn't have a fence (older instance); emit a no-op token.
        return ProcessToken(operation=operation, fence_at_capture=-1,
                            issued_at=time.time())
    return ProcessToken(
        operation=operation,
        fence_at_capture=fence.current(),
        issued_at=time.time(),
    )


def verify_token(cockpit, token: ProcessToken) -> bool:
    """Return True if the token is still valid (no kill landed).

    Returns False if the cockpit's fence has advanced past the token's
    captured value — meaning a state transition happened between the
    gate check and now, and the operation should abort.
    """
    if token.fence_at_capture < 0:
        return True  # sentinel for instances without a fence
    fence = getattr(cockpit, "_fence", None)
    if fence is None:
        return True
    return fence.current() == token.fence_at_capture


def release_token(cockpit, token: ProcessToken) -> None:
    """Release a token. No-op for our implementation; included for API symmetry."""
    return None