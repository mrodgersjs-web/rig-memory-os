"""RIG Memory OS v10 — Episode Builder (S2 Capture, task 3.3) — Phase 1.

Phase 1 fixes per Opus 5 cross-family review (FAIL verdict):
- Events persisted to append-only JSONL log (atomic per-event write)
- Caller-supplied event idempotency keys with real dedup index
- get_episode() returns defensive copies
- Append-only: events immutable after record()
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from founder_runtime.cockpit_subscriber import (
    OperationKind, assert_active, assert_active_with_token, verify_or_abort,
)

if TYPE_CHECKING:
    from founder_runtime.cockpit import MemoryCockpit
    from founder_runtime.postgres_writer import PostgresWriter


class EventType(str, Enum):
    SESSION_STARTED = "session.started"
    PROMPT_RECEIVED = "prompt.received"
    RESPONSE_COMPLETED = "response.completed"
    TOOL_CALLED = "tool.called"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    FILE_CHANGED = "file.changed"
    ARTIFACT_CREATED = "artifact.created"
    CHECKPOINT_CREATED = "checkpoint.created"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    CORRECTION_RECEIVED = "correction.received"
    CONTEXT_SERVED = "context.served"
    CONTEXT_INJECTED = "context.injected"
    MEMORY_PROPOSED = "memory.proposed"
    MISSION_COMPLETED = "mission.completed"
    MISSION_ABORTED = "mission.aborted"
    OUTCOME_RECORDED = "outcome.recorded"
    SESSION_ENDED = "session.ended"
    HEARTBEAT = "heartbeat"


@dataclass(frozen=True)
class EpisodeEvent:
    """Phase 1: append-only — frozen dataclass, immutable after creation."""

    run_id: str
    event_id: str
    sequence: int
    actor: str
    event_type: EventType
    occurred_at: float
    action: str
    state_before_ref: Optional[str] = None
    result_ref: Optional[str] = None
    state_after_ref: Optional[str] = None
    input_refs: tuple[str, ...] = ()
    output_refs: tuple[str, ...] = ()
    decision: Optional[str] = None
    error: Optional[str] = None
    correction: Optional[str] = None
    approval_ref: Optional[str] = None
    outcome: Optional[str] = None
    provenance: str = ""
    sensitivity: str = "internal"
    metadata: dict = field(default_factory=dict)
    idempotency_key: str = ""  # NEW: caller-supplied dedup key


@dataclass
class Episode:
    run_id: str
    session_id: str
    started_at: float
    ended_at: Optional[float] = None
    events: list[EpisodeEvent] = field(default_factory=list)
    closed: bool = False
    final_outcome: Optional[str] = None
    metadata: dict = field(default_factory=dict)


class EpisodeBuilder:
    """Append-only L2 episodic memory builder — Phase 1: durable + idempotent."""

    def __init__(
        self,
        storage_path: Optional[Path] = None,
        cockpit: Optional["MemoryCockpit"] = None,
        postgres_writer: Optional["PostgresWriter"] = None,
    ) -> None:
        self._open_episodes: dict[str, Episode] = {}
        self._closed_episodes: list[Episode] = []
        self._runs: dict[str, list[str]] = {}
        # Per-run sequence tracker
        self._event_sequences: dict[str, list[int]] = {}
        # Phase 1: real idempotency index (caller-supplied keys)
        self._idempotency_index: dict[str, str] = {}  # key -> event_id
        # Phase 3 fix (F6): optional cockpit gate. Default None keeps every
        # existing construction site unchanged (ungated), matching the
        # cockpit_subscriber convention used across the other subsystems.
        self._cockpit = cockpit
        # Phase 4: optional Postgres sink for envelopes. Best-effort with
        # visible failure (never blocks the builder, never silently lost).
        self._postgres_writer = postgres_writer
        self._persistence_failures: list[dict] = []
        # Phase 1: persistence
        self._storage_path = storage_path
        self._load_persisted_events()

    def persistence_failures(self) -> list[dict]:
        """Phase 4: Postgres sink failures, in order. Empty when healthy."""
        return list(self._persistence_failures)

    def _sink(self, sink: str, fn) -> None:
        """Best-effort Postgres write-through with visible failure."""
        if self._postgres_writer is None:
            return
        try:
            fn(self._postgres_writer)
        except Exception as exc:
            self._persistence_failures.append({
                "sink": sink,
                "error": f"{type(exc).__name__}: {exc}",
                "timestamp": time.time(),
            })

    def _load_persisted_events(self) -> None:
        if self._storage_path is None or not self._storage_path.exists():
            return
        try:
            with open(self._storage_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("idempotency_key"):
                        self._idempotency_index[data["idempotency_key"]] = data["event_id"]
                    seq_sequences = self._event_sequences.setdefault(
                        data["run_id"], []
                    )
                    seq_sequences.append(data["sequence"])
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    def _persist_event(self, event: EpisodeEvent) -> None:
        if self._storage_path is None:
            return
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "run_id": event.run_id,
            "event_id": event.event_id,
            "sequence": event.sequence,
            "actor": event.actor,
            "event_type": event.event_type.value,
            "occurred_at": event.occurred_at,
            "action": event.action,
            "input_refs": list(event.input_refs),
            "output_refs": list(event.output_refs),
            "provenance": event.provenance,
            "sensitivity": event.sensitivity,
            "idempotency_key": event.idempotency_key,
        }
        with open(self._storage_path, "a") as f:
            f.write(json.dumps(data) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def start_episode(
        self,
        run_id: str,
        session_id: str,
        actor: str,
        metadata: Optional[dict] = None,
    ) -> Episode:
        # Phase 3 fix (F6): gate before ANY mutation. start_episode writes
        # _open_episodes/_runs/_event_sequences before it calls record(), so
        # gating only inside record() would leave an orphaned open episode
        # behind on refusal.
        assert_active(self._cockpit, "episode.start", kind=OperationKind.WRITE)
        if session_id in self._open_episodes:
            return self._open_episodes[session_id]

        episode = Episode(
            run_id=run_id,
            session_id=session_id,
            started_at=time.time(),
            metadata=metadata or {},
        )
        self._open_episodes[session_id] = episode
        self._runs.setdefault(run_id, []).append(session_id)
        self._event_sequences.setdefault(run_id, [])

        self.record(
            run_id=run_id, session_id=session_id, actor=actor,
            event_type=EventType.SESSION_STARTED,
            action=f"session.started for run {run_id}",
            provenance=f"agent_principal={actor}",
            metadata=metadata or {},
        )
        return episode

    def record(
        self,
        run_id: str,
        session_id: str,
        actor: str,
        event_type: EventType,
        action: str,
        state_before_ref: Optional[str] = None,
        result_ref: Optional[str] = None,
        state_after_ref: Optional[str] = None,
        input_refs: Optional[list[str]] = None,
        output_refs: Optional[list[str]] = None,
        decision: Optional[str] = None,
        error: Optional[str] = None,
        correction: Optional[str] = None,
        approval_ref: Optional[str] = None,
        outcome: Optional[str] = None,
        provenance: str = "",
        sensitivity: str = "internal",
        metadata: Optional[dict] = None,
        idempotency_key: str = "",
    ) -> EpisodeEvent:
        """Phase 1: idempotent on caller-supplied key; appends durably."""
        # Phase 3 fix (F6): cockpit gate + fence capture. Control-plane
        # refusal outranks argument validation.
        token = assert_active_with_token(
            self._cockpit, "episode.record", kind=OperationKind.WRITE,
        )
        episode = self._open_episodes.get(session_id)
        if episode is None:
            raise KeyError(
                f"no open episode for session_id={session_id!r}; "
                f"call start_episode() first"
            )
        if episode.closed:
            raise RuntimeError(
                f"episode for session_id={session_id!r} is closed"
            )

        # Phase 1: real idempotency on caller-supplied key
        if idempotency_key and idempotency_key in self._idempotency_index:
            existing_event_id = self._idempotency_index[idempotency_key]
            # Return defensive copy of existing event
            for existing in episode.events:
                if existing.event_id == existing_event_id:
                    return replace(existing)

        seq_sequences = self._event_sequences[run_id]
        sequence = len(seq_sequences) + 1

        event = EpisodeEvent(
            run_id=run_id,
            event_id=str(uuid.uuid4()),
            sequence=sequence,
            actor=actor,
            event_type=event_type,
            occurred_at=time.time(),
            action=action,
            state_before_ref=state_before_ref,
            result_ref=result_ref,
            state_after_ref=state_after_ref,
            input_refs=tuple(input_refs or []),
            output_refs=tuple(output_refs or []),
            decision=decision,
            error=error,
            correction=correction,
            approval_ref=approval_ref,
            outcome=outcome,
            provenance=provenance,
            sensitivity=sensitivity,
            metadata=metadata or {},
            idempotency_key=idempotency_key,
        )
        # Phase 3 fix (F6): a kill that landed while we were building the
        # event aborts it. Nothing has mutated yet: events, the sequence
        # list, the idempotency index and the JSONL log are all untouched.
        verify_or_abort(self._cockpit, token, "episode.record")
        episode.events.append(event)
        seq_sequences.append(sequence)
        if idempotency_key:
            self._idempotency_index[idempotency_key] = event.event_id
        self._persist_event(event)
        # Phase 4: durable sink (idempotent on event_id).
        self._sink("envelopes", lambda w: w.write_envelope(
            envelope_id=event.event_id,
            run_id=event.run_id,
            sequence=event.sequence,
            actor=event.actor,
            event_type=event.event_type.value,
            action=event.action,
            occurred_at=event.occurred_at,
            state_before_ref=event.state_before_ref,
            state_after_ref=event.state_after_ref,
            decision=event.decision,
            error=event.error,
            correction=event.correction,
            approval_ref=event.approval_ref,
            outcome=event.outcome,
            provenance=event.provenance,
            sensitivity=event.sensitivity,
            content={
                "input_refs": list(event.input_refs),
                "output_refs": list(event.output_refs),
            },
            metadata={**event.metadata,
                      "idempotency_key": event.idempotency_key},
        ))
        return event

    def close_episode(
        self,
        session_id: str,
        actor: str,
        final_outcome: Optional[str] = None,
    ) -> Episode:
        episode = self._open_episodes.get(session_id)
        if episode is None:
            raise KeyError(f"no open episode for session_id={session_id!r}")
        if episode.closed:
            return episode
        self.record(
            run_id=episode.run_id, session_id=session_id, actor=actor,
            event_type=EventType.SESSION_ENDED,
            action=f"session.ended for run {episode.run_id}",
            outcome=final_outcome,
            provenance=f"agent_principal={actor}",
        )
        episode.ended_at = time.time()
        episode.closed = True
        episode.final_outcome = final_outcome
        self._closed_episodes.append(episode)
        del self._open_episodes[session_id]
        return episode

    def abort_mission(
        self,
        run_id: str,
        session_id: str,
        actor: str,
        reason: str,
    ) -> Episode:
        episode = self._open_episodes.get(session_id)
        if episode is None:
            raise KeyError(f"no open episode for session_id={session_id!r}")
        self.record(
            run_id=run_id, session_id=session_id, actor=actor,
            event_type=EventType.MISSION_ABORTED,
            action=f"mission.aborted for run {run_id}",
            error=reason,
            provenance=f"agent_principal={actor}",
        )
        return self.close_episode(
            session_id=session_id, actor=actor,
            final_outcome=f"ABORTED: {reason}",
        )

    def get_episode(self, session_id: str) -> Optional[Episode]:
        """Phase 1: returns defensive copy of events."""
        ep = None
        if session_id in self._open_episodes:
            ep = self._open_episodes[session_id]
        else:
            for closed in self._closed_episodes:
                if closed.session_id == session_id:
                    ep = closed
                    break
        if ep is None:
            return None
        # Defensive copy of events list
        return replace(
            ep,
            events=list(ep.events),
            metadata=dict(ep.metadata),
        )

    def reconstruct_run(self, run_id: str) -> list[EpisodeEvent]:
        """Reconstruct ordered events for a run."""
        all_events: list[EpisodeEvent] = []
        for session_id in self._runs.get(run_id, []):
            episode = self.get_episode(session_id)
            if episode is not None:
                all_events.extend(episode.events)
        all_events.sort(key=lambda e: e.sequence)
        return all_events

    def all_open_episodes(self) -> list[Episode]:
        return list(self._open_episodes.values())

    def all_closed_episodes(self) -> list[Episode]:
        return list(self._closed_episodes)

    def total_event_count(self) -> int:
        n = sum(len(ep.events) for ep in self._closed_episodes)
        n += sum(len(ep.events) for ep in self._open_episodes.values())
        return n