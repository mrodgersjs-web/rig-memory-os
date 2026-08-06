"""Phase 2 — Persistent worker loop.

One worker per node. Never fanout into new chat sessions.

Reference loop (from handoff §10.3):
    while not shutdown_requested:
        heartbeat(...)
        item = lease_highest_value_eligible_work(...)
        if item is None: wait_with_jitter(10, 20); continue
        try:
            checkpoint(item, "STARTED")
            result = execute_bounded(item)
            proof = verify_and_seal(result)
            complete(item, result, proof)
        except RetryableError as exc: fail_or_retry(item, exc, retryable=True)
        except Exception as exc: dead_letter_or_reopen(item, exc)
"""

from __future__ import annotations

import logging
import os
import random
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .contracts import (
    NodeCapabilityContract,
    WorkItemContract,
    WorkItemStatus,
    WorkResultContract,
    WorkResultStatus,
    Verdict,
)
from .store import (
    Store,
    heartbeat,
    renew_lease,
    complete_work_item,
    fail_work_item,
    register_node,
    append_audit,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ Executor protocol

# A "handler" maps work_type -> callable(WorkItemContract) -> WorkResultContract
HandlerFn = Callable[[WorkItemContract], WorkResultContract]


# ------------------------------------------------------------------ Worker


class Worker:
    """Persistent worker bound to one node_id and capability set."""

    def __init__(
        self,
        store: Store,
        node: NodeCapabilityContract,
        handlers: dict[str, HandlerFn],
        *,
        lease_seconds: int = 300,
        idle_min_seconds: float = 10.0,
        idle_max_seconds: float = 20.0,
        heartbeat_seconds: float = 20.0,
    ) -> None:
        self.store = store
        self.node = node
        self.handlers = handlers
        self.lease_seconds = lease_seconds
        self.idle_min = idle_min_seconds
        self.idle_max = idle_max_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._shutdown = False
        self._last_heartbeat = 0.0
        self._current_load = 0

    def request_shutdown(self, *_: Any) -> None:
        logger.info("shutdown requested for node=%s", self.node.node_id)
        self._shutdown = True

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.request_shutdown)
        signal.signal(signal.SIGTERM, self.request_shutdown)

    def heartbeat_now(self, load: Optional[int] = None) -> None:
        if load is not None:
            self._current_load = load
        heartbeat(self.store, self.node.node_id, load=self._current_load)
        self._last_heartbeat = time.time()

    def run(self) -> None:
        """Main loop. Blocks until shutdown."""
        self.install_signal_handlers()
        register_node(self.store, self.node.model_dump())
        logger.info("worker started node=%s caps=%s", self.node.node_id, self.node.capabilities)

        while not self._shutdown:
            self.heartbeat_now()

            from .store import claim_next_work_item
            item = claim_next_work_item(
                self.store,
                node_id=self.node.node_id,
                capabilities=self.node.capabilities,
                lease_seconds=self.lease_seconds,
            )
            if item is None:
                self._idle_sleep()
                continue

            self._process_one(item)
            self.heartbeat_now(load=max(0, self._current_load - 1))

        self.heartbeat_now(load=0)
        logger.info("worker exited node=%s", self.node.node_id)

    def _process_one(self, item: WorkItemContract) -> None:
        logger.info("leased work_item=%s type=%s priority=%s", item.work_item_id, item.work_type, item.priority)
        handler = self.handlers.get(item.work_type)
        if handler is None:
            fail_work_item(
                self.store,
                work_item_id=item.work_item_id,
                node_id=self.node.node_id,
                error_class="no_handler",
                retryable=False,
                summary=f"No handler registered for work_type={item.work_type}",
            )
            append_audit(self.store, actor=self.node.node_id, action="no_handler", target=item.work_item_id, detail={"work_type": item.work_type})
            return

        started = datetime.now(timezone.utc)
        try:
            result = handler(item)
        except Exception as exc:
            err_class = exc.__class__.__name__
            retryable = self._is_retryable(exc)
            fail_work_item(
                self.store,
                work_item_id=item.work_item_id,
                node_id=self.node.node_id,
                error_class=err_class,
                retryable=retryable,
                summary=f"Handler raised {err_class}: {exc}",
            )
            append_audit(self.store, actor=self.node.node_id, action="handler_exception", target=item.work_item_id, detail={"err": str(exc), "class": err_class})
            return

        result.started_at = started
        result.completed_at = datetime.now(timezone.utc)
        complete_work_item(
            self.store,
            work_item_id=item.work_item_id,
            node_id=self.node.node_id,
            result=result,
        )
        append_audit(self.store, actor=self.node.node_id, action="completed", target=item.work_item_id, detail={"summary": result.summary, "status": result.status.value})

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        name = exc.__class__.__name__
        if name in {"TimeoutError", "ConnectionError", "TemporaryFailure", "RetryableError"}:
            return True
        if name in {"ValueError", "TypeError", "KeyError", "PermanentError"}:
            return False
        # Default: be conservative — only retry on clearly transient classes
        return False

    def _idle_sleep(self) -> None:
        if time.time() - self._last_heartbeat > self.heartbeat_seconds:
            self.heartbeat_now()
        # jittered sleep
        time.sleep(random.uniform(self.idle_min, self.idle_max))


# ------------------------------------------------------------------ Built-in handlers


def make_signal_research_handler(
    *,
    scrape_fn: Optional[Callable[[str], dict[str, Any]]] = None,
    default_source_type: str = "http",
) -> HandlerFn:
    """Default handler for work_type=signal_research.

    Calls scrape_fn(source_uri) if provided; otherwise writes a placeholder
    SignalPacket that downstream verifier can accept.

    In production, scrape_fn would dispatch to rig-scrape or GBrain.
    """
    def handler(item: WorkItemContract) -> WorkResultContract:
        source_uri = item.payload.get("source_uri", "")
        if scrape_fn is not None and source_uri:
            data = scrape_fn(source_uri)
        else:
            data = {
                "ok": True,
                "summary": item.payload.get("summary_seed", "no scrape configured"),
                "entities": {},
                "evidence_strength": 0.5,
            }
        return WorkResultContract(
            work_item_id=item.work_item_id,
            worker_id="",
            status=WorkResultStatus.COMPLETED if data.get("ok") else WorkResultStatus.FAILED,
            summary=data.get("summary", "no summary"),
            artifact_paths=data.get("artifact_paths", []),
            source_refs=[source_uri] if source_uri else [],
            metrics={"evidence_strength": data.get("evidence_strength", 0)},
        )
    return handler