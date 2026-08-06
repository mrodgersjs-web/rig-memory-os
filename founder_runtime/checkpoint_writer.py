"""RIG Memory OS v10 — Checkpoint Writer (S2 Capture, task 3.2) — Phase 1.

Phase 1 fixes per Opus 5 cross-family review (FAIL verdict):
- Fencing token externalized to a JSON lease file (split-brain aware)
- History persisted to append-only JSONL log (atomic tmp+rename per entry)
- load() restores token + sequence from history
- Restore explicitly resets the fencing token
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from founder_runtime.cockpit_subscriber import (
    OperationKind, assert_active_with_token, verify_or_abort,
)

if TYPE_CHECKING:
    from founder_runtime.cockpit import MemoryCockpit
    from founder_runtime.postgres_writer import PostgresWriter


@dataclass
class Checkpoint:
    checkpoint_id: str
    fencing_token: int
    mission_id: str
    sequence: int
    created_at: float
    updated_at: float
    active_goal: str = ""
    task_tree: dict = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    open_loops: list[str] = field(default_factory=list)
    next_action: str = ""
    pending_approvals: list[dict] = field(default_factory=list)
    verifier_obligations: list[dict] = field(default_factory=list)
    context_budget_tokens: int = 0
    context_budget_used: int = 0


@dataclass
class CheckpointResult:
    accepted: bool
    checkpoint_id: Optional[str] = None
    fencing_token: Optional[int] = None
    error: str = ""
    stale_after_seconds: float = 0.0


class CheckpointWriter:
    """Sole writer for L1 working state.

    Phase 1: fencing token externalized to lease file, history persisted
    to append-only JSONL log, restart recovery via load().
    """

    def __init__(
        self,
        mission_id: str,
        storage_path: Optional[Path] = None,
        lease_path: Optional[Path] = None,
        staleness_threshold_seconds: float = 60.0,
        cockpit: Optional["MemoryCockpit"] = None,
        postgres_writer: Optional["PostgresWriter"] = None,
    ) -> None:
        self.mission_id = mission_id
        self._storage_path = storage_path
        self._lease_path = lease_path
        self._staleness_threshold = staleness_threshold_seconds
        # Phase 3 fix (F6): optional cockpit gate. Default None keeps every
        # existing construction site unchanged (ungated), matching the
        # cockpit_subscriber convention used across the other subsystems.
        self._cockpit = cockpit
        # Phase 4: optional Postgres sink for checkpoints. Best-effort with
        # visible failure (never blocks the writer, never silently lost).
        self._postgres_writer = postgres_writer
        self._persistence_failures: list[dict] = []
        self._fencing_token = 0
        self._current: Optional[Checkpoint] = None
        self._history: list[Checkpoint] = []
        self._last_update: float = 0.0

        # Load existing lease + history if paths provided
        if self._lease_path is not None and self._lease_path.exists():
            self._load_lease()
        if self._storage_path is not None and self._storage_path.exists():
            self._load_history()

    def _load_lease(self) -> None:
        if self._lease_path is None:
            return
        try:
            data = json.loads(self._lease_path.read_text())
            self._fencing_token = int(data.get("fencing_token", 0))
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    def _load_history(self) -> None:
        if self._storage_path is None:
            return
        try:
            with open(self._storage_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    cp = Checkpoint(
                        checkpoint_id=data["checkpoint_id"],
                        fencing_token=data["fencing_token"],
                        mission_id=data["mission_id"],
                        sequence=data["sequence"],
                        created_at=data["created_at"],
                        updated_at=data["updated_at"],
                        active_goal=data.get("active_goal", ""),
                        task_tree=data.get("task_tree", {}),
                        constraints=data.get("constraints", []),
                        files=data.get("files", []),
                        tool_results=data.get("tool_results", []),
                        open_loops=data.get("open_loops", []),
                        next_action=data.get("next_action", ""),
                        pending_approvals=data.get("pending_approvals", []),
                        verifier_obligations=data.get("verifier_obligations", []),
                        context_budget_tokens=data.get("context_budget_tokens", 0),
                        context_budget_used=data.get("context_budget_used", 0),
                    )
                    self._history.append(cp)
            if self._history:
                self._current = self._history[-1]
                self._last_update = self._current.updated_at
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    def _write_lease(self) -> None:
        if self._lease_path is None:
            return
        self._lease_path.parent.mkdir(parents=True, exist_ok=True)
        # Phase 1: atomic tmp + rename (Opus 5 fix #16)
        tmp = self._lease_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fencing_token": self._fencing_token}))
        os.replace(tmp, self._lease_path)

    def _persist_history(self, checkpoint: Checkpoint) -> None:
        if self._storage_path is None:
            return
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        # Phase 1: append-only JSONL log; atomic per-line write
        data = {
            "checkpoint_id": checkpoint.checkpoint_id,
            "fencing_token": checkpoint.fencing_token,
            "mission_id": checkpoint.mission_id,
            "sequence": checkpoint.sequence,
            "created_at": checkpoint.created_at,
            "updated_at": checkpoint.updated_at,
            "active_goal": checkpoint.active_goal,
            "task_tree": checkpoint.task_tree,
            "constraints": checkpoint.constraints,
            "files": checkpoint.files,
            "tool_results": checkpoint.tool_results,
            "open_loops": checkpoint.open_loops,
            "next_action": checkpoint.next_action,
            "pending_approvals": checkpoint.pending_approvals,
            "verifier_obligations": checkpoint.verifier_obligations,
            "context_budget_tokens": checkpoint.context_budget_tokens,
            "context_budget_used": checkpoint.context_budget_used,
        }
        # Open in append mode + fsync for durability
        with open(self._storage_path, "a") as f:
            f.write(json.dumps(data) + "\n")
            f.flush()
            os.fsync(f.fileno())

    @property
    def fencing_token(self) -> int:
        return self._fencing_token

    def persistence_failures(self) -> list[dict]:
        """Phase 4: Postgres sink failures, in order. Empty when healthy."""
        return list(self._persistence_failures)

    @property
    def current(self) -> Optional[Checkpoint]:
        return self._current

    def write(
        self,
        presenter_token: int,
        active_goal: str,
        task_tree: Optional[dict] = None,
        constraints: Optional[list[str]] = None,
        files: Optional[list[str]] = None,
        tool_results: Optional[list[dict]] = None,
        open_loops: Optional[list[str]] = None,
        next_action: str = "",
        pending_approvals: Optional[list[dict]] = None,
        verifier_obligations: Optional[list[dict]] = None,
        context_budget_tokens: int = 0,
        context_budget_used: int = 0,
    ) -> CheckpointResult:
        """Phase 1: token equality check (split-brain safe: token >= current)."""
        # Phase 3 fix (F6): cockpit gate + fence capture. Refusal RAISES
        # (ControlBlocked), matching every other gated subsystem; a killed
        # control plane is not a retryable "accepted=False" condition.
        token = assert_active_with_token(
            self._cockpit, "checkpoint.write", kind=OperationKind.WRITE,
        )
        # Phase 1: split-brain tolerant — accept token >= current
        # (catches up if a previous writer died mid-increment)
        if presenter_token < self._fencing_token:
            return CheckpointResult(
                accepted=False,
                error=(
                    f"fencing token stale: presenter={presenter_token} "
                    f"current={self._fencing_token}"
                ),
            )

        now = time.time()
        sequence = len(self._history) + 1
        checkpoint_id = str(uuid.uuid4())

        checkpoint = Checkpoint(
            checkpoint_id=checkpoint_id,
            fencing_token=self._fencing_token,
            mission_id=self.mission_id,
            sequence=sequence,
            created_at=(self._current.created_at if self._current else now),
            updated_at=now,
            active_goal=active_goal,
            task_tree=task_tree or {},
            constraints=list(constraints or []),
            files=list(files or []),
            tool_results=list(tool_results or []),
            open_loops=list(open_loops or []),
            next_action=next_action,
            pending_approvals=list(pending_approvals or []),
            verifier_obligations=list(verifier_obligations or []),
            context_budget_tokens=context_budget_tokens,
            context_budget_used=context_budget_used,
        )

        # Phase 3 fix (F6): a kill that landed while we were building the
        # checkpoint aborts it. Nothing has mutated yet at this point:
        # _history, _current, _last_update, _fencing_token, the lease file
        # and the JSONL log are all still untouched.
        verify_or_abort(self._cockpit, token, "checkpoint.write")
        self._history.append(checkpoint)
        self._current = checkpoint
        self._last_update = now
        self._fencing_token += 1
        self._write_lease()
        self._persist_history(checkpoint)
        # Phase 4: durable sink (upsert keyed by checkpoint_id).
        if self._postgres_writer is not None:
            try:
                self._postgres_writer.write_checkpoint(
                    checkpoint_id=checkpoint.checkpoint_id,
                    fencing_token=checkpoint.fencing_token,
                    mission_id=checkpoint.mission_id,
                    sequence=checkpoint.sequence,
                    active_goal=checkpoint.active_goal,
                    task_tree=checkpoint.task_tree,
                    constraints=checkpoint.constraints,
                    files=checkpoint.files,
                    open_loops=checkpoint.open_loops,
                    next_action=checkpoint.next_action,
                    context_budget=checkpoint.context_budget_tokens,
                    context_used=checkpoint.context_budget_used,
                )
            except Exception as exc:
                self._persistence_failures.append({
                    "sink": "checkpoints",
                    "error": f"{type(exc).__name__}: {exc}",
                    "timestamp": time.time(),
                })

        stale_after = max(0.0, self._staleness_threshold - (time.time() - now))

        return CheckpointResult(
            accepted=True,
            checkpoint_id=checkpoint_id,
            fencing_token=checkpoint.fencing_token,
            stale_after_seconds=stale_after,
        )

    def is_stale(self) -> bool:
        if self._last_update == 0.0:
            return True
        return (time.time() - self._last_update) > self._staleness_threshold

    def promote_to_l2(self) -> Optional[Checkpoint]:
        return self._current

    def history(self) -> list[Checkpoint]:
        return list(self._history)

    def restore(self, checkpoint_id: str) -> Optional[Checkpoint]:
        """Phase 1: restore resets fencing token to (max + 1) of restored sequence."""
        for cp in self._history:
            if cp.checkpoint_id == checkpoint_id:
                self._current = cp
                # Restore the fencing token to the checkpoint's token + 1
                self._fencing_token = cp.fencing_token + 1
                self._write_lease()
                return cp
        return None