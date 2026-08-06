"""RIG Memory OS v10 — Memory Cockpit (S7) — Phase 1.

Phase 1 fixes per Opus 5 cross-family review (FAIL verdict):
- Controls are AUTHORITATIVE: subsystems use cockpit_subscriber.assert_active
  before executing (MemoryGateway, RetrievalEngine, IntentService,
  SkillFoundry, OfferFoundry all import and consult the cockpit)
- Kill/pause is a state machine: kill implies pause; release_kill_switch
  leaves pause engaged; release_pause only valid after kill is released
  (forbids killed-and-unpaused state)
- Budget enforcement: budget field is decremented by subsystems and
  ControlBlocked is raised if budget == 0
- Audit log of every control action (who, when, what)
- All 8 documented panels implemented (events, retrieval, gBrain,
  procedures, backup/restore, plus the existing L1-L8 health +
  predictions + intentions)
- Snapshot retention bounded to 50 entries
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

from founder_runtime.process_token import ProcessFence

if TYPE_CHECKING:
    from founder_runtime.postgres_cockpit import PostgresCockpitStore
    from founder_runtime.postgres_writer import PostgresWriter


class ControlState(str, Enum):
    """Phase 1: explicit state machine for kill/pause.

    Per Opus 5 #3: PAUSED stops writes only (reads allowed);
    KILLED stops everything. The semantics are enforced by
    cockpit_subscriber.assert_active(kind=READ|WRITE).
    """
    ACTIVE = "active"            # neither killed nor paused
    PAUSED = "paused"            # writes blocked, reads allowed (Opus 5 #3)
    KILLED = "killed"            # everything blocked; pause is implied


@dataclass
class CockpitPanel:
    name: str
    status: str  # ok | degraded | failed | no_data
    metrics: dict = field(default_factory=dict)
    last_updated: float = field(default_factory=time.time)


@dataclass
class CockpitSnapshot:
    captured_at: float = field(default_factory=time.time)
    panels: list[CockpitPanel] = field(default_factory=list)
    control_state: ControlState = ControlState.ACTIVE
    budget_remaining: float = 1.0
    audit: list[dict] = field(default_factory=list)


class MemoryCockpit:
    """Memory Cockpit — Phase 1: authoritative controls + state machine.

    Yellow #4: optional PostgresWriter persists every audit entry to
    the audit_log table (in addition to the in-memory deque).

    Phase 3 (F3) read-through contract: when a PostgresCockpitStore is
    wired, the store is AUTHORITATIVE and this object is a read-through
    cache with bounded staleness. A kill engaged by any other process
    becomes visible here within `store_read_ttl` seconds (default 0.25).
    This is a documented window, not instant propagation: LISTEN/NOTIFY is
    explicitly out of scope for the single-host pilot. Observing a remote
    transition bumps the local ProcessFence, so every in-flight process
    token in THIS process is invalidated by a kill from ANOTHER process.
    Local transitions are write-through, best-effort: a failed write-through
    is reverted by the next refresh.

    Phase 3 (F4) audit contract: exactly ONE audit_log row per state
    transition. When the wired store has an audit_writer, the store owns
    the insert (inside write_state). The cockpit's own postgres_writer is a
    fallback that fires only when the store did not write. cockpit_log
    (store) and audit_log (writer) are distinct intended tables. Wiring
    DIFFERENT writer objects to DIFFERENT databases in both slots is
    unsupported — under this contract only the store's database receives
    the row.
    """

    MAX_SNAPSHOTS = 50  # Phase 1: bounded snapshot retention

    def __init__(
        self,
        store: Optional["PostgresCockpitStore"] = None,
        postgres_writer: Optional["PostgresWriter"] = None,
        store_read_ttl: float = 0.25,
    ) -> None:
        # Phase 1 fix (Opus 5 #7): RLock around all state mutations so
        # concurrent subsystems can't see torn state during a kill/pause
        # transition. The codebase already runs concurrently elsewhere
        # (store.py, fleet_probe.py) so this isn't theoretical.
        self._lock = threading.RLock()
        # Yellow #6: monotonic fence bumped on every state transition.
        # Used by process_token.verify_token() to close the TOCTOU
        # window between assert_active and the actual operation.
        self._fence = ProcessFence()
        # Yellow #3: optional Postgres-backed store for multi-process
        # kill switch. When supplied, state transitions are written to
        # Postgres under pg_advisory_xact_lock so all processes sharing
        # the same store see the same state.
        self._store = store
        # Yellow #4: optional PostgresWriter for persisting every
        # audit entry to the audit_log table.
        self._postgres_writer = postgres_writer
        self._state: ControlState = ControlState.ACTIVE
        self._budget_remaining: float = 1.0
        # Phase 1: audit log of all control actions
        self._audit: deque[dict] = deque(maxlen=200)
        self._snapshots: deque[CockpitSnapshot] = deque(maxlen=self.MAX_SNAPSHOTS)
        self._layer_status: dict[str, str] = {}
        self._queue_lag: dict[str, float] = {}
        self._prediction_stats: dict = {}
        self._intent_stats: dict = {}
        self._events_episodes: dict = {}
        self._retrieval_stats: dict = {}
        self._gbrain_stats: dict = {}
        self._procedure_stats: dict = {}
        self._backup_stats: dict = {}
        # Phase 3 fix (F3): read-through cache bookkeeping. `_last_store_read`
        # is a time.monotonic() stamp; 0.0 means "never successfully read".
        self._store_read_ttl = max(0.0, float(store_read_ttl))
        self._last_store_read: float = 0.0
        # When store is supplied, hydrate from it (multi-process sync)
        if self._store is not None:
            self._hydrate_from_store()

    def _hydrate_from_store(self) -> None:
        """Read current state from the Postgres-backed store at init.

        Phase 3 fix (F4): also bootstraps the singleton row so a fresh
        deployment has somewhere for set_budget to land.
        """
        if self._store is None:
            return
        try:
            self._store.ensure_row()
            state_value, budget = self._store.read_state()
            self._state = ControlState(state_value)
            self._budget_remaining = budget
        except Exception:
            # Store not available (test environment); in-memory default.
            pass
        finally:
            # Phase 3 fix (F3): stamp even on failure so a dead store is
            # retried once per TTL instead of on every gate call.
            self._last_store_read = time.monotonic()

    def _refresh_from_store(self) -> None:
        """Phase 3 fix (F3): pull authoritative state when the TTL expired.

        Called from every control READ path. No-op when no store is wired,
        which is why the entire in-memory test suite is unaffected.

        Deliberately does NOT call _record_audit: that would write the
        observed state back to the store and echo forever. The observation
        is appended to the local audit deque directly so an operator can
        see when propagation happened.
        """
        if self._store is None:
            return
        with self._lock:
            now = time.monotonic()
            if self._last_store_read and (
                now - self._last_store_read
            ) < self._store_read_ttl:
                return
            try:
                state_value, budget = self._store.read_state()
            except Exception:
                # Fail SAFE: keep the last known state. A store outage must
                # never un-kill a killed cockpit. Back off one TTL.
                self._last_store_read = now
                return
            self._last_store_read = now
            new_state = ControlState(state_value)
            if new_state == self._state and budget == self._budget_remaining:
                return
            before = self._state
            self._state = new_state
            self._budget_remaining = budget
            # A transition we did not originate still invalidates every
            # in-flight process token here (F3 -> F6 linkage).
            self._fence.bump()
            self._audit.append({
                "actor": "store",
                "action": "observed_remote_state",
                "before": before.value,
                "after": new_state.value,
                "timestamp": time.time(),
            })

    def _record_audit(self, actor: str, action: str, before: ControlState, after: ControlState) -> None:
        with self._lock:
            self._audit.append({
                "actor": actor,
                "action": action,
                "before": before.value,
                "after": after.value,
                "timestamp": time.time(),
            })
            # Yellow #6: bump the monotonic fence on every state change
            # so in-flight process_tokens can detect the change.
            if before != after:
                self._fence.bump()
            # Phase 3 fix (F4): single canonical Postgres audit path.
            # Previously store.write_state() AND self._postgres_writer both
            # inserted into audit_log, so every transition landed two rows.
            # Now: when the store carries an audit_writer and the transition
            # write succeeds, the store owns the audit_log insert; the
            # cockpit's own writer is a FALLBACK (no store, store without an
            # audit_writer, or a store write that raised).
            store_wrote_audit = False
            # Phase 4: set_budget is a control-plane write with
            # before == after state — the guard must key on the ACTION,
            # not only the state transition, or budget writes never reach
            # the store. Budget writes route through write_budget (never
            # touches the state column), so a stale local state cannot
            # clobber a remote kill.
            if self._store is not None and (
                before != after or action == "set_budget"
            ):
                try:
                    if action == "set_budget":
                        self._store.write_budget(
                            actor=actor, state=self._state.value,
                            budget=self._budget_remaining,
                        )
                    else:
                        self._store.write_state(
                            actor=actor, action=action,
                            before=before.value, after=after.value,
                            budget=self._budget_remaining,
                        )
                    # We just wrote the authoritative value; no read is owed.
                    self._last_store_read = time.monotonic()
                    store_wrote_audit = (
                        getattr(self._store, "audit_writer", None) is not None
                    )
                except Exception:
                    # Best-effort: don't block the control plane on store.
                    # The audit row was NOT written, so the fallback below
                    # must cover it.
                    store_wrote_audit = False
            # Yellow #4: persist audit entry to audit_log table if writer
            if self._postgres_writer is not None and not store_wrote_audit:
                try:
                    self._postgres_writer.write_audit_entry(
                        actor=actor, action=action,
                        before_state=before.value,
                        after_state=after.value,
                    )
                except Exception as exc:
                    # Phase 3 re-review fix (cross-family finding
                    # SILENT_AUDIT_FAILURE): never BLOCK the control plane
                    # on an audit failure, but never lose it silently either
                    # — record the loss in the surviving in-memory deque.
                    self._audit.append({
                        "actor": "cockpit",
                        "action": "audit_write_failed",
                        "before": before.value,
                        "after": after.value,
                        "timestamp": time.time(),
                        "error": f"{type(exc).__name__}: {exc}",
                        "lost_entry": {"actor": actor, "action": action},
                    })

    def audit(self) -> list[dict]:
        """Phase 1 fix (Opus 5 #7): public accessor for the audit log.

        Callers (e.g. MemoryOSRuntime.status) must not reach into _audit.
        Returns a defensive copy.
        """
        with self._lock:
            return list(self._audit)

    def engage_pause(self, actor: str = "system") -> None:
        """Pause writes. Valid only if not KILLED."""
        with self._lock:
            if self._state == ControlState.KILLED:
                return
            before = self._state
            self._state = ControlState.PAUSED
            self._record_audit(actor, "engage_pause", before, self._state)

    def release_pause(self, actor: str = "system") -> None:
        """Release pause. Phase 1: requires kill to be released first."""
        with self._lock:
            before = self._state
            if self._state == ControlState.KILLED:
                return
            self._state = ControlState.ACTIVE
            self._record_audit(actor, "release_pause", before, self._state)

    def engage_kill_switch(self, actor: str = "system") -> None:
        """Kill: stops everything. KILLED state implies paused."""
        with self._lock:
            before = self._state
            self._state = ControlState.KILLED
            self._record_audit(actor, "engage_kill_switch", before, self._state)

    def release_kill_switch(self, actor: str = "system") -> None:
        """Phase 1: transition KILLED -> PAUSED (NOT directly to ACTIVE)."""
        with self._lock:
            before = self._state
            if self._state != ControlState.KILLED:
                return
            self._state = ControlState.PAUSED
            self._record_audit(actor, "release_kill_switch", before, self._state)

    def is_paused(self) -> bool:
        self._refresh_from_store()
        with self._lock:
            return self._state in {ControlState.PAUSED, ControlState.KILLED}

    def is_killed(self) -> bool:
        self._refresh_from_store()
        with self._lock:
            return self._state == ControlState.KILLED

    def is_active(self) -> bool:
        self._refresh_from_store()
        with self._lock:
            return self._state == ControlState.ACTIVE

    @property
    def state(self) -> ControlState:
        self._refresh_from_store()
        with self._lock:
            return self._state

    @property
    def budget(self) -> float:
        self._refresh_from_store()
        with self._lock:
            return self._budget_remaining

    @property
    def store_read_ttl(self) -> float:
        """Bounded staleness window for cross-process control state, seconds."""
        return self._store_read_ttl

    def set_budget(self, remaining: float, actor: str = "system") -> None:
        with self._lock:
            if remaining < 0:
                remaining = 0.0
            if remaining > 1:
                remaining = 1.0
            self._budget_remaining = remaining
            # Yellow #6: budget change bumps fence too (budget=0
            # transitions block all gates, so must invalidate tokens).
            self._fence.bump()
            # Phase 4: budget changes are control-plane actions — they now
            # produce an audit entry (in-memory deque + Postgres write-
            # through), closing the highest-value F4 follow-up gap. The
            # store upsert inside _record_audit carries the new budget, so
            # no separate store.set_budget call is needed.
            self._record_audit(actor, "set_budget", self._state, self._state)

    def decrement_budget(self, amount: float) -> bool:
        """Phase 1: budget enforcement. Returns False if exhausted.

        Phase 1 fix (Opus 5 #7): atomic under RLock so concurrent
        decrements can't over-draw.
        Yellow #3: when a store is wired, decrement goes through
        PostgresCockpitStore.adjust_budget (single SQL UPDATE with
        pg_advisory_xact_lock) so the result is visible across processes.
        """
        if self._store is not None:
            with self._lock:
                success, new_budget = self._store.adjust_budget(amount)
                self._budget_remaining = new_budget
                # The store just told us the authoritative value; no read owed.
                self._last_store_read = time.monotonic()
            return success
        with self._lock:
            if self._budget_remaining < amount:
                return False
            self._budget_remaining -= amount
            return True

    def set_layer_status(self, layer: str, status: str) -> None:
        self._layer_status[layer] = status

    def set_queue_lag(self, queue_name: str, lag_seconds: float) -> None:
        self._queue_lag[queue_name] = lag_seconds

    def set_prediction_stats(self, stats: dict) -> None:
        self._prediction_stats = stats

    def set_intent_stats(self, stats: dict) -> None:
        self._intent_stats = stats

    def set_events_episodes(self, stats: dict) -> None:
        """Phase 1: events / episodes panel data."""
        self._events_episodes = stats

    def set_retrieval_stats(self, stats: dict) -> None:
        """Phase 1: retrieval precision / latency panel data."""
        self._retrieval_stats = stats

    def set_gbrain_stats(self, stats: dict) -> None:
        """Phase 1: gBrain orphans / projection lag panel data."""
        self._gbrain_stats = stats

    def set_procedure_stats(self, stats: dict) -> None:
        """Phase 1: procedure candidates / reliability panel data."""
        self._procedure_stats = stats

    def set_backup_stats(self, stats: dict) -> None:
        """Phase 1: backup / WAL / restore-test panel data."""
        self._backup_stats = stats

    def snapshot(self) -> CockpitSnapshot:
        """Capture all 8 panels + state + audit."""
        self._refresh_from_store()
        panels: list[CockpitPanel] = []

        # Panel 1: L1-L8 health
        panels.append(CockpitPanel(
            name="L1-L8 health",
            status="ok" if all(s == "ok" for s in self._layer_status.values()) else "degraded",
            metrics={
                "layer_status": dict(self._layer_status),
                "queue_lag_seconds": dict(self._queue_lag),
            },
        ))

        # Panel 2: Predictions
        panels.append(CockpitPanel(
            name="Predictions",
            status="ok" if self._prediction_stats.get("brier", 1.0) < 0.5 else "degraded",
            metrics=dict(self._prediction_stats),
        ))

        # Panel 3: Intentions
        panels.append(CockpitPanel(
            name="Intentions",
            status="ok",
            metrics=dict(self._intent_stats),
        ))

        # Panel 4: Events / episodes (Phase 1 NEW)
        panels.append(CockpitPanel(
            name="Events / episodes",
            status="ok",
            metrics=dict(self._events_episodes),
        ))

        # Panel 5: Retrieval (Phase 1 NEW)
        panels.append(CockpitPanel(
            name="Retrieval",
            status="ok",
            metrics=dict(self._retrieval_stats),
        ))

        # Panel 6: gBrain (Phase 1 NEW; Phase 3 fix F5: status derived,
        # not hardcoded — an unpopulated panel must not read "ok")
        panels.append(CockpitPanel(
            name="gBrain",
            status=(self._gbrain_stats.get("status") or "no_data")
                   if self._gbrain_stats else "no_data",
            metrics=dict(self._gbrain_stats),
        ))

        # Panel 7: Procedures (Phase 1 NEW)
        panels.append(CockpitPanel(
            name="Procedures",
            status="ok",
            metrics=dict(self._procedure_stats),
        ))

        # Panel 8: Backup / restore (Phase 1 NEW)
        panels.append(CockpitPanel(
            name="Backup / restore",
            status="ok",
            metrics=dict(self._backup_stats),
        ))

        snap = CockpitSnapshot(
            panels=panels,
            control_state=self._state,
            budget_remaining=self._budget_remaining,
            audit=list(self._audit)[-10:],  # last 10 audit entries
        )
        self._snapshots.append(snap)
        return snap

    def last_snapshot(self) -> Optional[CockpitSnapshot]:
        return self._snapshots[-1] if self._snapshots else None

    def render_text(self) -> str:
        """Render cockpit as plain text."""
        snap = self.snapshot()
        lines = [
            "=" * 70,
            "RIG Memory OS Cockpit (Phase 1)",
            f"Captured at: {snap.captured_at}",
            f"State:       {snap.control_state.value}",
            f"Budget:      {snap.budget_remaining:.0%}",
            f"Audit log:   {len(self._audit)} entries (showing last {len(snap.audit)})",
            "=" * 70,
        ]
        for panel in snap.panels:
            lines.append(f"\n[{panel.name}] status={panel.status}")
            for k, v in panel.metrics.items():
                if isinstance(v, dict):
                    lines.append(f"  {k}:")
                    for k2, v2 in v.items():
                        lines.append(f"    {k2}: {v2}")
                else:
                    lines.append(f"  {k}: {v}")
        if snap.audit:
            lines.append("\n[Recent audit]")
            for entry in snap.audit:
                lines.append(
                    f"  {entry['action']}: {entry['before']} -> {entry['after']} "
                    f"by {entry['actor']} at {entry['timestamp']:.0f}"
                )
        lines.append("=" * 70)
        return "\n".join(lines)