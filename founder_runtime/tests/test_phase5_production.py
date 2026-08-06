"""RIG Memory OS v10 — Phase 5 production-wiring tests.

Scope (sealed Phase 4 packet's next_gate):
  - MemoryOSRuntime.from_env(): full production stack (PostgresWriter +
    PostgresCockpitStore + store-backed MemoryCockpit) from environment,
    fail-closed on missing secret or unreachable DB.
  - founder_runtime.reconcile: replay JSONL logs (canonical during a
    Postgres outage) into the idempotent Postgres sinks.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import psycopg

from founder_runtime.tests.test_phase4_wiring import Phase4TestCase


class TestFromEnv(Phase4TestCase):
    DB_NAME = "rig_test_phase5_fromenv"

    def _env(self):
        return {
            "RIG_MEMORY_OS_SECRET": "phase5-secret",
            "RIG_MEMORY_OS_DSN": self.dsn,
        }

    def test_from_env_constructs_full_stack(self):
        from founder_runtime.runtime import MemoryOSRuntime
        rt = MemoryOSRuntime.from_env(self._env())
        self.assertIsNotNone(rt.gateway._postgres_writer)
        self.assertIsNotNone(rt.intent._postgres_writer)
        self.assertIsNotNone(rt.cockpit._store)
        rt.close()
        rt.close()  # idempotent

    def test_from_env_fail_closed_without_secret(self):
        from founder_runtime.runtime import MemoryOSRuntime
        with self.assertRaises(ValueError):
            MemoryOSRuntime.from_env({"RIG_MEMORY_OS_DSN": self.dsn})

    def test_from_env_fail_closed_on_unreachable_db(self):
        from founder_runtime.runtime import MemoryOSRuntime
        with self.assertRaises(Exception):
            MemoryOSRuntime.from_env({
                "RIG_MEMORY_OS_SECRET": "x",
                "RIG_MEMORY_OS_DSN": "host=/tmp port=5432 dbname=rig_no_such_db_p5",
            })

    def test_from_env_bootstraps_cockpit_row(self):
        from founder_runtime.runtime import MemoryOSRuntime
        rt = MemoryOSRuntime.from_env(self._env())
        self.assertEqual(self.row_count(), 1)
        rt.close()

    def test_from_env_kill_propagates_to_fresh_process(self):
        """The from_env cockpit is a REAL multi-process control plane."""
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.runtime import MemoryOSRuntime
        rt = MemoryOSRuntime.from_env(self._env())
        other = MemoryCockpit(store=self.make_store(), store_read_ttl=0.0)
        self.assertFalse(other.is_killed())
        rt.kill(actor="phase5-test")
        self.assertTrue(other.is_killed())
        rt.close()


class TestReconcile(Phase4TestCase):
    DB_NAME = "rig_test_phase5_reconcile"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        from founder_runtime.checkpoint_writer import CheckpointWriter
        from founder_runtime.episode_builder import EpisodeBuilder, EventType
        from founder_runtime.intent_service import IntentService
        from founder_runtime.intent_service import PermissionClass

        # Canonical JSONL logs produced with NO Postgres writer (the
        # outage scenario reconcile exists for).
        self.events_path = tmp / "events.jsonl"
        b = EpisodeBuilder(storage_path=self.events_path)
        b.start_episode(run_id="r1", session_id="s1", actor="t")
        b.record(run_id="r1", session_id="s1", actor="t",
                 event_type=EventType.HEARTBEAT, action="hb")

        self.intents_path = tmp / "intents.jsonl"
        self.receipts_path = tmp / "receipts.jsonl"
        svc = IntentService(storage_path=self.receipts_path,
                            intents_path=self.intents_path)
        intent = svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="a", permission_class=PermissionClass.A1_PREPARE,
            idempotency_key="p5-reconcile",
        )
        svc.execute_intent(intent.intent_id)

        self.checkpoints_path = tmp / "checkpoints.jsonl"
        cw = CheckpointWriter("m1", storage_path=self.checkpoints_path)
        cw.write(0, "goal")

    def tearDown(self):
        self._tmp.cleanup()
        _ = self._psql_clean()

    def _psql_clean(self):
        from founder_runtime.tests.test_phase3_fixes import _psql
        return _psql(self.DB_NAME,
                     "DELETE FROM envelopes; DELETE FROM effect_receipts; "
                     "DELETE FROM checkpoints; DELETE FROM intents;")

    def _conn(self):
        conn = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(conn.close)
        return conn

    def test_reconcile_all_three_logs(self):
        from founder_runtime.reconcile import (
            reconcile_checkpoints, reconcile_effect_receipts,
            reconcile_events, reconcile_intents,
        )
        w = self.make_writer()
        conn = self._conn()
        # intents BEFORE receipts (foreign key).
        r0 = reconcile_intents(self.intents_path, w, conn)
        r1 = reconcile_events(self.events_path, w, conn)
        r2 = reconcile_effect_receipts(self.receipts_path, w, conn)
        r3 = reconcile_checkpoints(self.checkpoints_path, w, conn)
        self.assertEqual((r0.scanned, r0.written, r0.errors), (2, 1, 0))
        self.assertEqual((r1.scanned, r1.written, r1.errors), (2, 2, 0))
        self.assertEqual((r2.scanned, r2.written, r2.errors), (1, 1, 0))
        self.assertEqual((r3.scanned, r3.written, r3.errors), (1, 1, 0))
        self.assertEqual(w.envelope_count(), 2)
        self.assertEqual(w.checkpoint_count(), 1)

    def test_reconcile_is_idempotent(self):
        from founder_runtime.reconcile import reconcile_events
        w = self.make_writer()
        conn = self._conn()
        first = reconcile_events(self.events_path, w, conn)
        second = reconcile_events(self.events_path, w, conn)
        self.assertEqual(first.written, 2)
        self.assertEqual(second.written, 0)
        self.assertEqual(second.skipped_duplicate, 2)
        self.assertEqual(w.envelope_count(), 2)

    def test_reconcile_tolerates_corrupt_line(self):
        from founder_runtime.reconcile import reconcile_events
        with open(self.events_path, "a") as f:
            f.write("this is not json\n")
        w = self.make_writer()
        conn = self._conn()
        report = reconcile_events(self.events_path, w, conn)
        self.assertEqual(report.errors, 1)
        self.assertEqual(report.written, 2)  # valid rows still land
        self.assertFalse(report.ok)

    def test_reconcile_cli_smoke(self):
        from founder_runtime.reconcile import main
        rc = main([
            "--intents", str(self.intents_path),
            "--events", str(self.events_path),
            "--receipts", str(self.receipts_path),
            "--checkpoints", str(self.checkpoints_path),
            "--dsn", self.dsn,
        ])
        self.assertEqual(rc, 0)

    def test_intents_log_restart_recovery(self):
        """A fresh IntentService on the same intents.jsonl rebuilds the
        full in-memory state (create + latest status)."""
        from founder_runtime.intent_service import IntentService
        svc2 = IntentService(intents_path=self.intents_path)
        intents = svc2.all_intents()
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].status.value, "completed")
        # idempotency index recovered too — replaying the key returns the
        # same intent instead of creating a new one.
        again = svc2.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="a",
            permission_class=intents[0].permission_class,
            idempotency_key="p5-reconcile",
        )
        self.assertEqual(again.intent_id, intents[0].intent_id)
        self.assertEqual(len(svc2.all_intents()), 1)


if __name__ == "__main__":
    unittest.main()
