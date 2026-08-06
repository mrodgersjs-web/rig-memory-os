"""RIG Memory OS v10 — Cockpit Subscriber (S7) — Phase 1.

Phase 1 fix per minimax + Opus 5 cross-family reviews:
- subsystems (MemoryGateway, RetrievalEngine, IntentService,
  Skill/OfferFoundry) query the cockpit's kill/pause state BEFORE
  executing via assert_active()
- Phase 1 update (Opus 5 #3): reads vs writes distinction. PAUSE
  stops writes but allows reads. KILLED stops everything.
- Phase 1 update (Opus 5 #6): consume_budget() is called on real paths
  so the budget branch in assert_active is reachable.

Per Opus 5 + minimax reviews, the kill switch is AUTHORITATIVE.
"""

from __future__ import annotations

import time
from typing import Optional, TYPE_CHECKING

from founder_runtime.process_token import (
    ProcessToken, issue_token, release_token, verify_token,
)

if TYPE_CHECKING:
    from founder_runtime.cockpit import MemoryCockpit


class ControlBlocked(RuntimeError):
    """Raised when a subsystem operation is blocked by the cockpit."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# Per Opus 5 #3: distinguish read vs write semantics
class OperationKind:
    """Tag for operations so PAUSE can selectively allow reads."""
    READ = "read"
    WRITE = "write"


def assert_active(
    cockpit: Optional["MemoryCockpit"],
    operation: str,
    kind: str = OperationKind.WRITE,
) -> None:
    """Subsystem gate: refuse operation if cockpit is gated or budget exhausted.

    Opus 5 #3 semantics:
    - KILLED → refuses BOTH reads and writes.
    - PAUSED → refuses writes only (reads allowed).
    - budget <= 0 → refuses BOTH (budget is non-renewable for the session).

    Raises ControlBlocked if the operation should not proceed.
    """
    if cockpit is None:
        return
    if cockpit.is_killed():
        raise ControlBlocked(
            f"{operation} refused: kill switch engaged"
        )
    if cockpit.is_paused() and kind == OperationKind.WRITE:
        raise ControlBlocked(
            f"{operation} refused: paused (writes blocked)"
        )
    # Budget enforcement: refuse if budget is 0
    if cockpit.budget <= 0:
        raise ControlBlocked(
            f"{operation} refused: budget exhausted"
        )


def consume_budget(
    cockpit: Optional["MemoryCockpit"], cost: float,
) -> bool:
    """Attempt to consume `cost` units of cockpit budget.

    Returns True if consumed, False if budget exhausted.
    Subsystems call this on every state-changing operation (Opus 5 #6).
    """
    if cockpit is None:
        return True
    return cockpit.decrement_budget(cost)


def assert_read_allowed(
    cockpit: Optional["MemoryCockpit"], operation: str,
) -> None:
    """Convenience: assert a read is allowed under current control state.

    Reads are only blocked when KILLED or budget==0; PAUSE allows reads.
    """
    assert_active(cockpit, operation, kind=OperationKind.READ)


def assert_active_with_token(
    cockpit: Optional["MemoryCockpit"],
    operation: str,
    kind: str = OperationKind.WRITE,
) -> Optional[ProcessToken]:
    """Gate the operation AND capture the control fence in one call.

    Identical refusal semantics to assert_active(); additionally returns a
    ProcessToken snapshotting the cockpit's fence. Hand that token to
    verify_or_abort() immediately before the mutation lands so a kill/pause
    arriving in between aborts the operation instead of writing through a
    closed control plane.

    The token is issued BEFORE the gate (Phase 3 convergence decision): a
    kill landing after a successful gate but before token capture would
    otherwise produce a token that verifies clean against the post-kill
    fence. Issue-first means such a kill either trips the gate or
    invalidates the already-issued token.

    Returns None when `cockpit` is None (ungated caller); verify_or_abort()
    accepts None and is then a no-op.
    """
    if cockpit is None:
        return None
    token = issue_token(cockpit, operation)
    try:
        assert_active(cockpit, operation, kind=kind)
    except Exception:
        release_token(cockpit, token)
        raise
    return token


def verify_or_abort(
    cockpit: Optional["MemoryCockpit"],
    token: Optional[ProcessToken],
    operation: str,
) -> None:
    """Raise ControlBlocked if control state changed since the gate.

    Closes the TOCTOU window opened by assert_active(): between the gate and
    the mutation another thread — or, via the store read-through in
    MemoryCockpit._refresh_from_store, another PROCESS — may have engaged
    kill/pause or zeroed the budget.

    Residual window: the few bytecodes between this check and the mutation
    itself are not atomic with it. Making them atomic requires transactional
    mutation, which is out of scope for the single-host pilot. This narrows
    the window from "the whole operation" to "one statement"; it does not
    eliminate it.
    """
    if cockpit is None or token is None:
        return
    if not verify_token(cockpit, token):
        raise ControlBlocked(f"{operation} aborted: killed mid-operation")