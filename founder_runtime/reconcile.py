"""RIG Memory OS v10 — Phase 5: sink reconciliation.

The Phase 4 sinks are best-effort: during a Postgres outage the JSONL
logs remain canonical and Postgres accumulates gaps. This tool replays
the JSONL logs into Postgres. Every writer is idempotent
(INSERT ... ON CONFLICT DO NOTHING), so replay is safe to run any number
of times.

Usage:
    python -m founder_runtime.reconcile \
        --events episodes.jsonl --receipts effect_receipts.jsonl \
        --checkpoints checkpoints.jsonl
    (DSN via RIG_MEMORY_OS_DSN or the PG host/port/db env pieces)

Exit code: 0 when every scanned record landed (or was already present),
1 if any record errored.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from founder_runtime.postgres_writer import PostgresWriter


@dataclass
class ReconcileReport:
    """Outcome for one reconciled file."""

    source: str
    scanned: int = 0
    written: int = 0
    skipped_duplicate: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.errors == 0


def _count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table};")
        return cur.fetchone()[0]


def _read_jsonl(path: Path, report: ReconcileReport):
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                report.errors += 1
                report.error_details.append(f"line {lineno}: {exc}")


def _finish(report: ReconcileReport, conn, table: str, before: int) -> ReconcileReport:
    after = _count(conn, table)
    report.written = after - before
    report.skipped_duplicate = report.scanned - report.written - report.errors
    return report


def reconcile_events(path: Path, writer: PostgresWriter, conn) -> ReconcileReport:
    """Replay an EpisodeBuilder JSONL log into `envelopes`."""
    report = ReconcileReport(source=str(path))
    before = _count(conn, "envelopes")
    for e in _read_jsonl(path, report):
        report.scanned += 1
        try:
            writer.write_envelope(
                envelope_id=e["event_id"],
                run_id=e["run_id"],
                sequence=e["sequence"],
                actor=e["actor"],
                event_type=e["event_type"],
                action=e["action"],
                occurred_at=e.get("occurred_at"),
                provenance=e.get("provenance"),
                sensitivity=e.get("sensitivity"),
                content={
                    "input_refs": e.get("input_refs", []),
                    "output_refs": e.get("output_refs", []),
                },
                metadata={"idempotency_key": e.get("idempotency_key", "")},
            )
        except (KeyError, TypeError) as exc:
            report.errors += 1
            report.error_details.append(f"event {e.get('event_id', '?')}: {exc}")
    return _finish(report, conn, "envelopes", before)


def reconcile_intents(path: Path, writer: PostgresWriter, conn) -> ReconcileReport:
    """Replay an IntentService intents JSONL log (create + status lines).

    MUST run before reconcile_effect_receipts — effect_receipts.intent_id
    has a foreign key to intents.
    """
    report = ReconcileReport(source=str(path))
    before = _count(conn, "intents")
    seen_ids: set[str] = set()  # track create lines for orphan detection
    for e in _read_jsonl(path, report):
        report.scanned += 1
        try:
            kind = e.get("kind")
            if kind == "create":
                writer.write_intent(
                    intent_id=e["intent_id"],
                    owner=e["owner"],
                    trigger_type=e["trigger_type"],
                    trigger_spec=e["trigger_spec"],
                    action=e.get("action", ""),
                    permission_class=e.get("permission_class", "A1_prepare"),
                    retry_policy=e.get("retry_policy", "next_admission"),
                    idempotency_key=e.get("idempotency_key"),
                    due_at=e.get("due_at"),
                    expires_at=e.get("expires_at"),
                    status=e.get("status", "pending"),
                )
                seen_ids.add(e["intent_id"])
            elif kind == "status":
                iid = e["intent_id"]
                if iid not in seen_ids:
                    report.errors += 1
                    report.error_details.append(
                        f"orphan status for {iid}: no create line found"
                    )
                    continue
                writer.update_intent_status(iid, e["status"])
            else:
                raise ValueError(f"unknown line kind: {kind!r}")
        except (KeyError, TypeError, ValueError) as exc:
            report.errors += 1
            report.error_details.append(f"intent {e.get('intent_id', '?')}: {exc}")
    return _finish(report, conn, "intents", before)


def reconcile_effect_receipts(path: Path, writer: PostgresWriter, conn) -> ReconcileReport:
    """Replay an IntentService effect-receipt JSONL log."""
    report = ReconcileReport(source=str(path))
    before = _count(conn, "effect_receipts")
    for e in _read_jsonl(path, report):
        report.scanned += 1
        try:
            writer.write_effect_receipt(
                receipt_id=e["receipt_id"],
                intent_id=e["intent_id"],
                approver_id=e.get("approver_id"),
                permission_class=e["permission_class"],
                result=e.get("result", ""),
            )
        except (KeyError, TypeError) as exc:
            report.errors += 1
            report.error_details.append(f"receipt {e.get('receipt_id', '?')}: {exc}")
    return _finish(report, conn, "effect_receipts", before)


def reconcile_checkpoints(path: Path, writer: PostgresWriter, conn) -> ReconcileReport:
    """Replay a CheckpointWriter JSONL history into `checkpoints`."""
    report = ReconcileReport(source=str(path))
    before = _count(conn, "checkpoints")
    for e in _read_jsonl(path, report):
        report.scanned += 1
        try:
            writer.write_checkpoint(
                checkpoint_id=e["checkpoint_id"],
                fencing_token=e["fencing_token"],
                mission_id=e["mission_id"],
                sequence=e["sequence"],
                active_goal=e.get("active_goal", ""),
                task_tree=e.get("task_tree"),
                constraints=e.get("constraints"),
                files=e.get("files"),
                open_loops=e.get("open_loops"),
                next_action=e.get("next_action", ""),
                context_budget=e.get("context_budget_tokens", 0),
                context_used=e.get("context_budget_used", 0),
            )
        except (KeyError, TypeError) as exc:
            report.errors += 1
            report.error_details.append(
                f"checkpoint {e.get('checkpoint_id', '?')}: {exc}"
            )
    return _finish(report, conn, "checkpoints", before)


def _dsn_from_env(env) -> str:
    dsn = env.get("RIG_MEMORY_OS_DSN")
    if dsn:
        return dsn
    host = env.get("RIG_MEMORY_OS_PG_HOST", "/tmp")
    port = env.get("RIG_MEMORY_OS_PG_PORT", "5432")
    db = env.get("RIG_MEMORY_OS_PG_DB", "rig_memory_os_phase1")
    return f"host={host} port={port} dbname={db}"


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="RIG Memory OS sink reconciliation")
    p.add_argument("--intents", type=Path, help="IntentService intents JSONL log")
    p.add_argument("--events", type=Path, help="EpisodeBuilder JSONL log")
    p.add_argument("--receipts", type=Path, help="IntentService effect receipts JSONL")
    p.add_argument("--checkpoints", type=Path, help="CheckpointWriter JSONL history")
    p.add_argument("--dsn", default=None, help="psycopg DSN (default: env)")
    args = p.parse_args(argv)

    if not (args.events or args.receipts or args.checkpoints or args.intents):
        p.error("at least one of --intents/--events/--receipts/--checkpoints is required")

    import psycopg

    dsn = args.dsn or _dsn_from_env(os.environ)
    writer = PostgresWriter(dsn=dsn)
    conn = psycopg.connect(dsn, autocommit=True)
    reports: list[ReconcileReport] = []
    try:
        # intents BEFORE receipts: effect_receipts.intent_id is a FK.
        if args.intents:
            reports.append(reconcile_intents(args.intents, writer, conn))
        if args.events:
            reports.append(reconcile_events(args.events, writer, conn))
        if args.receipts:
            reports.append(reconcile_effect_receipts(args.receipts, writer, conn))
        if args.checkpoints:
            reports.append(reconcile_checkpoints(args.checkpoints, writer, conn))
    finally:
        writer.close()
        conn.close()

    for r in reports:
        print(json.dumps({
            "source": r.source, "scanned": r.scanned, "written": r.written,
            "skipped_duplicate": r.skipped_duplicate, "errors": r.errors,
            "error_details": r.error_details,
        }))
    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
