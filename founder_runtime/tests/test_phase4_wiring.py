"""RIG Memory OS v10 — Phase 4 wiring tests.

Scope (sealed Phase 3 packet's next_gate):
  - Yellow #1 remainder: the 5 PostgresWriter sinks now have production
    callers — usage_receipts (MemoryGateway), intents + effect_receipts
    (IntentService), envelopes (EpisodeBuilder), checkpoints
    (CheckpointWriter).
  - set_budget produces an audit entry (was the highest-value F4 gap).
  - update_intent_status stamps completed_at only on terminal states.

Every sink is best-effort: a Postgres failure never blocks the subsystem,
but is recorded in persistence_failures() — never silently lost.
Scratch databases per class (full deploy.py SCHEMA_SQL, extracted from
source so the harness cannot drift from the real DDL). The live
rig_memory_os_phase1 database is never touched.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock

from founder_runtime.tests.test_phase3_fixes import (
    PostgresTestCase, _psql,
)

# Full deployment schema, extracted from deploy.py at class-setup time so
# this harness tracks the real DDL instead of a copy.
_DEPLOY_PY = Path(__file__).resolve().parents[2] / "deploy.py"


def _deploy_schema() -> str:
    text = _DEPLOY_PY.read_text()
    m = re.search(r'SCHEMA_SQL = """(.*?)"""', text, re.S)
    sv = re.search(r'SCHEMA_VERSION_SQL = """(.*?)"""', text, re.S)
    v = re.search(r'^SCHEMA_VERSION = "([^"]+)"', text, re.M)
    if m is None or sv is None or v is None:
        raise RuntimeError("deploy.py SCHEMA_SQL / SCHEMA_VERSION_SQL / "
                           "SCHEMA_VERSION not found")
    # PostgresWriter.ensure_schema() refuses a DB with no version row
    # (Phase 1 fix #13), so the harness must seed it like deploy.py does.
    seed = (
        "INSERT INTO schema_version(schema_id, description, set_id) VALUES "
        f"('{v.group(1)}', 'test harness seed', 'phase1') "
        "ON CONFLICT (schema_id) DO NOTHING;"
    )
    return sv.group(1) + m.group(1) + seed


class Phase4TestCase(PostgresTestCase):
    """PostgresTestCase, but with the full 9-table deployment schema."""

    @classmethod
    def setUpClass(cls):
        if not getattr(cls, "_schema", None):
            cls._schema = _deploy_schema()
        # Reuse the parent's create/drop machinery but with full DDL.
        from founder_runtime.tests.test_phase3_fixes import _postgres_available
        if not _postgres_available():
            raise unittest.SkipTest("postgres not reachable at /tmp:5432")
        cls._drop()
        r = _psql("template1", f"CREATE DATABASE {cls.DB_NAME};")
        if r.returncode != 0:
            raise unittest.SkipTest(f"cannot create {cls.DB_NAME}: {r.stderr}")
        r = _psql(cls.DB_NAME, cls._schema)
        if r.returncode != 0:
            cls._drop()
            raise unittest.SkipTest(f"cannot apply schema: {r.stderr}")
        cls.dsn = (
            f"host=/tmp port=5432 dbname={cls.DB_NAME}"
        )


def _context(secret: bytes):
    from founder_runtime.memory_gateway import SensitivityCeiling, sign_context
    return sign_context(
        secret,
        operator_id="op", tenant_id="t", client_id="c", project_id="p",
        mission_id="m", agent_principal="builder", agent_instance="i1",
        harness_version="h1", adapter_version="a1", node="n1",
        purpose="phase4-test",
        sensitivity_ceiling=SensitivityCeiling.INTERNAL,
        run_id="r1", session_id="s1", trace_id="tr1", policy_version="v1",
    )


class TestGatewayReceiptSink(Phase4TestCase):
    DB_NAME = "rig_test_phase4_gateway"

    def tearDown(self):
        _psql(self.DB_NAME, "DELETE FROM usage_receipts;")

    def test_invoke_writes_usage_receipt_row(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.memory_gateway import MemoryGateway
        w = self.make_writer()
        secret = b"phase4-gateway-secret"
        g = MemoryGateway(shared_secret=secret, cockpit=MemoryCockpit(),
                          postgres_writer=w)
        result = g.invoke(_context(secret), "memory.heartbeat", {})
        self.assertTrue(result.accepted)
        self.assertEqual(w.receipt_count(), 1)

    def test_rejected_invoke_writes_nothing(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.memory_gateway import MemoryGateway
        w = self.make_writer()
        secret = b"phase4-gateway-secret"
        g = MemoryGateway(shared_secret=secret, cockpit=MemoryCockpit(),
                          postgres_writer=w)
        bad = _context(b"wrong-secret")
        result = g.invoke(bad, "memory.heartbeat", {})
        self.assertFalse(result.accepted)
        self.assertEqual(w.receipt_count(), 0)

    def test_sink_failure_visible_not_blocking(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.memory_gateway import MemoryGateway
        w = mock.Mock()
        w.write_usage_receipt.side_effect = RuntimeError("pg down")
        secret = b"phase4-gateway-secret"
        g = MemoryGateway(shared_secret=secret, cockpit=MemoryCockpit(),
                          postgres_writer=w)
        result = g.invoke(_context(secret), "memory.heartbeat", {})
        self.assertTrue(result.accepted)  # gateway never blocks on sink
        failures = g.persistence_failures()
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["sink"], "usage_receipts")

    def test_no_writer_is_byte_identical_legacy(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.memory_gateway import MemoryGateway
        secret = b"phase4-gateway-secret"
        g = MemoryGateway(shared_secret=secret, cockpit=MemoryCockpit())
        result = g.invoke(_context(secret), "memory.heartbeat", {})
        self.assertTrue(result.accepted)
        self.assertEqual(g.persistence_failures(), [])


class TestIntentSinks(Phase4TestCase):
    DB_NAME = "rig_test_phase4_intents"

    def tearDown(self):
        _psql(self.DB_NAME,
              "DELETE FROM effect_receipts; DELETE FROM intents;")

    def _service(self, w=None, **kw):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.intent_service import IntentService
        return IntentService(cockpit=MemoryCockpit(), postgres_writer=w, **kw)

    def _create(self, svc, key="k1"):
        from founder_runtime.intent_service import PermissionClass
        return svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="do-thing", permission_class=PermissionClass.A1_PREPARE,
            idempotency_key=key,
        )

    def test_create_intent_writes_row(self):
        w = self.make_writer()
        svc = self._service(w)
        intent = self._create(svc)
        r = _psql(self.DB_NAME,
                  f"SELECT status FROM intents WHERE intent_id = "
                  f"'{intent.intent_id}';")
        self.assertIn("pending", r.stdout)

    def test_idempotent_replay_no_duplicate_row(self):
        w = self.make_writer()
        svc = self._service(w)
        i1 = self._create(svc, key="dup")
        i2 = self._create(svc, key="dup")
        self.assertEqual(i1.intent_id, i2.intent_id)
        r = _psql(self.DB_NAME, "SELECT COUNT(*) FROM intents;")
        self.assertEqual(int(r.stdout.split("\n")[2].strip()), 1)

    def test_execute_writes_effect_receipt_and_status(self):
        w = self.make_writer()
        svc = self._service(w)
        intent = self._create(svc)
        result = svc.execute_intent(intent.intent_id)
        self.assertTrue(result.executed)
        r = _psql(self.DB_NAME,
                  f"SELECT status, completed_at IS NOT NULL FROM intents "
                  f"WHERE intent_id = '{intent.intent_id}';")
        self.assertIn("completed", r.stdout)
        self.assertIn("t", r.stdout)  # completed_at stamped
        r = _psql(self.DB_NAME,
                  f"SELECT COUNT(*) FROM effect_receipts WHERE "
                  f"intent_id = '{intent.intent_id}';")
        self.assertEqual(int(r.stdout.split("\n")[2].strip()), 1)

    def test_cancel_writes_terminal_status(self):
        w = self.make_writer()
        svc = self._service(w)
        intent = self._create(svc)
        svc.cancel_intent(intent.intent_id, reason="no longer needed")
        r = _psql(self.DB_NAME,
                  f"SELECT status, completed_at IS NOT NULL FROM intents "
                  f"WHERE intent_id = '{intent.intent_id}';")
        self.assertIn("cancelled", r.stdout)
        self.assertIn("t", r.stdout)

    def test_repend_does_not_stamp_completed_at(self):
        """A FAILED intent re-admitted by the retry policy must not carry
        a completion timestamp in Postgres."""
        w = self.make_writer()
        svc = self._service(w)
        intent = self._create(svc)

        def boom(_):
            raise RuntimeError("executor exploded")

        result = svc.execute_intent(intent.intent_id, executor_fn=boom)
        self.assertFalse(result.executed)
        # next_admission retry re-pended the intent.
        r = _psql(self.DB_NAME,
                  f"SELECT status, completed_at IS NULL FROM intents "
                  f"WHERE intent_id = '{intent.intent_id}';")
        self.assertIn("pending", r.stdout)
        self.assertIn("t", r.stdout)  # completed_at IS NULL -> true

    def test_sink_failure_visible_not_blocking(self):
        w = mock.Mock()
        w.write_intent.side_effect = RuntimeError("pg down")
        svc = self._service(w)
        intent = self._create(svc)
        self.assertIsNotNone(intent)
        self.assertEqual(len(svc.persistence_failures()), 1)
        self.assertEqual(svc.persistence_failures()[0]["sink"], "intents")

    def test_repend_clears_in_memory_completed_at(self):
        """F1: re-pend is not a completion; in-memory completed_at must be
        cleared so it cannot diverge from Postgres (NULL via CASE)."""
        w = self.make_writer()
        svc = self._service(w)
        intent = self._create(svc)

        def boom(_):
            raise RuntimeError("x")

        svc.execute_intent(intent.intent_id, executor_fn=boom)
        self.assertIsNone(svc.get_intent(intent.intent_id).completed_at)
        self.assertEqual(svc.get_intent(intent.intent_id).status.value, "pending")

    def test_expire_overdue_sinks_terminal_status(self):
        """F2: expire_overdue is a terminal transition and must reach
        Postgres like every other transition."""
        import time as _time
        w = self.make_writer()
        svc = self._service(w)
        intent = self._create(svc)
        intent.expires_at = _time.time() - 1  # already overdue
        svc.expire_overdue()
        r = _psql(self.DB_NAME,
                  f"SELECT status, completed_at IS NOT NULL FROM intents "
                  f"WHERE intent_id = '{intent.intent_id}';")
        self.assertIn("expired", r.stdout)
        self.assertIn("t", r.stdout)


class TestEpisodeEnvelopeSink(Phase4TestCase):
    DB_NAME = "rig_test_phase4_episodes"

    def tearDown(self):
        _psql(self.DB_NAME, "DELETE FROM envelopes;")

    def test_record_writes_envelope_row(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.episode_builder import EpisodeBuilder, EventType
        w = self.make_writer()
        b = EpisodeBuilder(cockpit=MemoryCockpit(), postgres_writer=w)
        b.start_episode(run_id="r1", session_id="s1", actor="t")
        b.record(run_id="r1", session_id="s1", actor="t",
                 event_type=EventType.HEARTBEAT, action="hb")
        # session.started + heartbeat
        self.assertEqual(w.envelope_count(), 2)
        r = _psql(self.DB_NAME,
                  "SELECT event_type FROM envelopes ORDER BY sequence;")
        self.assertIn("session.started", r.stdout)
        self.assertIn("heartbeat", r.stdout)

    def test_idempotent_replay_single_row(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.episode_builder import EpisodeBuilder, EventType
        w = self.make_writer()
        b = EpisodeBuilder(cockpit=MemoryCockpit(), postgres_writer=w)
        b.start_episode(run_id="r1", session_id="s1", actor="t")
        for _ in range(2):
            b.record(run_id="r1", session_id="s1", actor="t",
                     event_type=EventType.HEARTBEAT, action="hb",
                     idempotency_key="same-key")
        # 1 session.started + 1 heartbeat (replay deduped)
        self.assertEqual(w.envelope_count(), 2)

    def test_killed_cockpit_blocks_pg_write(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import ControlBlocked
        from founder_runtime.episode_builder import EpisodeBuilder, EventType
        w = self.make_writer()
        c = MemoryCockpit()
        b = EpisodeBuilder(cockpit=c, postgres_writer=w)
        b.start_episode(run_id="r1", session_id="s1", actor="t")
        n = w.envelope_count()
        c.engage_kill_switch()
        with self.assertRaises(ControlBlocked):
            b.record(run_id="r1", session_id="s1", actor="t",
                     event_type=EventType.HEARTBEAT, action="hb")
        self.assertEqual(w.envelope_count(), n)  # no partial Postgres write

    def test_sink_failure_visible_not_blocking(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.episode_builder import EpisodeBuilder
        w = mock.Mock()
        w.write_envelope.side_effect = RuntimeError("pg down")
        b = EpisodeBuilder(cockpit=MemoryCockpit(), postgres_writer=w)
        b.start_episode(run_id="r1", session_id="s1", actor="t")
        self.assertEqual(b.total_event_count(), 1)
        self.assertEqual(len(b.persistence_failures()), 1)
        self.assertEqual(b.persistence_failures()[0]["sink"], "envelopes")


class TestCheckpointSink(Phase4TestCase):
    DB_NAME = "rig_test_phase4_checkpoints"

    def tearDown(self):
        _psql(self.DB_NAME, "DELETE FROM checkpoints;")

    def test_write_persists_checkpoint_row(self):
        from founder_runtime.checkpoint_writer import CheckpointWriter
        w = self.make_writer()
        cw = CheckpointWriter("m1", postgres_writer=w)
        result = cw.write(0, "goal", next_action="step-1")
        self.assertTrue(result.accepted)
        self.assertEqual(w.checkpoint_count(), 1)
        r = _psql(self.DB_NAME,
                  "SELECT active_goal, fencing_token FROM checkpoints;")
        self.assertIn("goal", r.stdout)

    def test_second_write_two_rows(self):
        from founder_runtime.checkpoint_writer import CheckpointWriter
        w = self.make_writer()
        cw = CheckpointWriter("m1", postgres_writer=w)
        cw.write(0, "goal")
        cw.write(cw.fencing_token, "goal-2")
        self.assertEqual(w.checkpoint_count(), 2)

    def test_sink_failure_visible_not_blocking(self):
        from founder_runtime.checkpoint_writer import CheckpointWriter
        w = mock.Mock()
        w.write_checkpoint.side_effect = RuntimeError("pg down")
        cw = CheckpointWriter("m1", postgres_writer=w)
        result = cw.write(0, "goal")
        self.assertTrue(result.accepted)  # in-memory write still lands
        self.assertEqual(len(cw.persistence_failures()), 1)
        self.assertEqual(cw.persistence_failures()[0]["sink"], "checkpoints")


class TestSetBudgetAudit(Phase4TestCase):
    """set_budget was the highest-value unaudited control action."""

    DB_NAME = "rig_test_phase4_budget"

    def tearDown(self):
        _psql(self.DB_NAME,
              "DELETE FROM cockpit_log; DELETE FROM audit_log; "
              "DELETE FROM cockpit_state;")

    def test_set_budget_writes_audit_and_log_rows(self):
        from founder_runtime.cockpit import MemoryCockpit
        w = self.make_writer()
        store = self.make_store(audit_writer=w)
        c = MemoryCockpit(store=store)
        c.set_budget(0.5, actor="operator-x")
        actions = [e["action"] for e in c.audit()]
        self.assertIn("set_budget", actions)
        # store wrote cockpit_log + audit via its writer: exactly one each.
        self.assertEqual(w.audit_count(), 1)
        log = store.read_log(limit=5)
        self.assertTrue(any(r["action"] == "set_budget" for r in log))

    def test_set_budget_audit_fallback_without_store(self):
        from founder_runtime.cockpit import MemoryCockpit
        w = self.make_writer()
        c = MemoryCockpit(postgres_writer=w)
        c.set_budget(0.25, actor="operator-y")
        self.assertEqual(w.audit_count(), 1)
        entry = w.latest_audit_entries(limit=1)[0]
        self.assertEqual(entry["action"], "set_budget")
        self.assertEqual(entry["actor"], "operator-y")

    def test_set_budget_persists_through_audit_path(self):
        """The store upsert inside _record_audit must carry the new budget
        (no separate store.set_budget call exists anymore)."""
        from founder_runtime.cockpit import MemoryCockpit
        store = self.make_store()
        c = MemoryCockpit(store=store)
        c.set_budget(0.25)
        self.assertAlmostEqual(store.read_state()[1], 0.25, places=5)

    def test_set_budget_still_bumps_fence(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.process_token import issue_token, verify_token
        c = MemoryCockpit()
        token = issue_token(c, "op")
        c.set_budget(0.5)
        self.assertFalse(verify_token(c, token))

    def test_set_budget_cannot_clobber_remote_kill(self):
        """A process with a stale local state that sets its budget must NOT
        resurrect the state — the budget write never touches the state
        column (write_budget, not write_state)."""
        from founder_runtime.cockpit import MemoryCockpit
        a = MemoryCockpit(store=self.make_store(), store_read_ttl=30.0)
        b = MemoryCockpit(store=self.make_store(), store_read_ttl=0.0)
        b.engage_kill_switch(actor="b")
        # A is inside its TTL window: locally still ACTIVE (stale).
        self.assertFalse(a.is_killed())
        a.set_budget(0.5, actor="a")
        # The remote kill survives A's budget write.
        state, budget = self.make_store().read_state()
        self.assertEqual(state, "killed")
        self.assertAlmostEqual(budget, 0.5, places=5)


class TestRuntimeWiring(unittest.TestCase):

    def test_runtime_passes_writer_to_gateway_and_intents(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.runtime import MemoryOSRuntime
        w = mock.Mock()
        r = MemoryOSRuntime(cockpit=MemoryCockpit(),
                            gateway_secret=b"test-secret",
                            postgres_writer=w)
        self.assertIs(r.gateway._postgres_writer, w)
        self.assertIs(r.intent._postgres_writer, w)

    def test_runtime_default_unchanged(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.runtime import MemoryOSRuntime
        r = MemoryOSRuntime(cockpit=MemoryCockpit(),
                            gateway_secret=b"test-secret")
        self.assertIsNone(r.gateway._postgres_writer)
        self.assertIsNone(r.intent._postgres_writer)


if __name__ == "__main__":
    unittest.main()
