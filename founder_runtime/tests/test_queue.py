"""Phase 1 tests — the durability spine.

Run with: pytest founder_runtime/tests/test_queue.py -v
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from founder_runtime.store import (
    Store,
    init_db,
    register_node,
    heartbeat,
    list_nodes,
    enqueue_work_item,
    claim_next_work_item,
    renew_lease,
    complete_work_item,
    fail_work_item,
    recover_expired_leases,
    queue_metrics,
    upsert_opportunity,
    list_opportunities,
    mark_offline_stale_nodes,
)
from founder_runtime.contracts import (
    NodeCapabilityContract,
    WorkItemContract,
    WorkResultContract,
    OpportunityContract,
    WorkResultStatus,
    OpportunityStage,
)


MIGRATION = Path(__file__).parent.parent.parent / "migrations" / "001_founder_runtime.sql"


@pytest.fixture
def tmp_store():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "state.db"
        s = Store(path)
        init_db(s, MIGRATION)
        yield s
        s.close()


def _node(node_id="rig-36gb", caps=None, **kw):
    return NodeCapabilityContract(
        node_id=node_id,
        hostname="RIG-36GB-Mac-Studio.local",
        capabilities=caps or ["signal_research", "web_scrape"],
        max_concurrency=kw.get("max_concurrency", 2),
        lan_address="100.91.39.12",
        tailnet_address="100.91.39.12",
    )


def _item(work_type="signal_research", caps=None, **kw):
    return WorkItemContract(
        work_type=work_type,
        objective="Find a market signal about a vertical.",
        required_capabilities=caps or ["signal_research"],
        priority=kw.get("priority", 50),
        idempotency_key=kw.get("idem", f"idem-{work_type}-{kw.get('priority', 50)}-{uuid.uuid4()}"),
        **{k: v for k, v in kw.items() if k not in {"priority", "idem"}},
    )


# ---------- Node registry ----------


def test_register_and_heartbeat(tmp_store):
    register_node(tmp_store, _node().model_dump())
    nodes = list_nodes(tmp_store)
    assert len(nodes) == 1
    assert nodes[0]["node_id"] == "rig-36gb"
    assert "signal_research" in nodes[0]["capabilities"]
    heartbeat(tmp_store, "rig-36gb", load=1)
    n = list_nodes(tmp_store)[0]
    assert n["current_load"] == 1
    assert n["last_heartbeat"] is not None


def test_register_idempotent(tmp_store):
    register_node(tmp_store, _node().model_dump())
    register_node(tmp_store, _node(max_concurrency=4).model_dump())
    nodes = list_nodes(tmp_store)
    assert len(nodes) == 1
    assert nodes[0]["max_concurrency"] == 4


def test_stale_nodes_marked_offline(tmp_store):
    register_node(tmp_store, _node().model_dump())
    with tmp_store.tx() as conn:
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        conn.execute("UPDATE nodes SET last_heartbeat = ? WHERE node_id = ?", (old, "rig-36gb"))
    marked = mark_offline_stale_nodes(tmp_store, stale_after_seconds=120)
    assert marked == 1
    n = list_nodes(tmp_store)[0]
    assert n["status"] == "OFFLINE_UNVERIFIED"


# ---------- Idempotency ----------


def test_enqueue_idempotency_key_dedups(tmp_store):
    enqueue_work_item(tmp_store, _item(idem="fixed-key"))
    enqueue_work_item(tmp_store, _item(idem="fixed-key"))
    enqueue_work_item(tmp_store, _item(idem="different"))
    assert queue_metrics(tmp_store)["READY"] == 2


# ---------- Atomic leasing ----------


def test_two_workers_cannot_lease_same_item(tmp_store):
    enqueue_work_item(tmp_store, _item(idem="once"))
    a = claim_next_work_item(tmp_store, node_id="a", capabilities=["signal_research"], lease_seconds=60)
    b = claim_next_work_item(tmp_store, node_id="b", capabilities=["signal_research"], lease_seconds=60)
    assert a is not None and a.lease_owner == "a"
    assert b is None
    assert queue_metrics(tmp_store)["LEASED"] == 1


def test_capability_gate_defers_item(tmp_store):
    enqueue_work_item(tmp_store, _item(idem="needs-vector", caps=["vector_search"]))
    out = claim_next_work_item(tmp_store, node_id="a", capabilities=["signal_research"], lease_seconds=60)
    assert out is None
    assert queue_metrics(tmp_store)["READY"] == 1
    out2 = claim_next_work_item(tmp_store, node_id="b", capabilities=["signal_research", "vector_search"], lease_seconds=60)
    assert out2 is not None


def test_priority_ordering(tmp_store):
    enqueue_work_item(tmp_store, _item(idem="low", priority=10))
    enqueue_work_item(tmp_store, _item(idem="high", priority=90))
    enqueue_work_item(tmp_store, _item(idem="mid", priority=50))
    got = claim_next_work_item(tmp_store, node_id="a", capabilities=["signal_research"], lease_seconds=60)
    assert got is not None and got.idempotency_key == "high"


def test_lease_renewal(tmp_store):
    enqueue_work_item(tmp_store, _item(idem="long"))
    got = claim_next_work_item(tmp_store, node_id="a", capabilities=["signal_research"], lease_seconds=10)
    assert got is not None
    assert renew_lease(tmp_store, got.work_item_id, "a", lease_seconds=600)
    assert not renew_lease(tmp_store, got.work_item_id, "b", lease_seconds=600)


# ---------- Expiry recovery ----------


def test_expired_leases_recover(tmp_store):
    enqueue_work_item(tmp_store, _item(idem="stale"))
    got = claim_next_work_item(tmp_store, node_id="a", capabilities=["signal_research"], lease_seconds=1)
    assert got is not None
    with tmp_store.tx() as conn:
        conn.execute("UPDATE work_items SET lease_expires_at = ? WHERE work_item_id = ?",
                     ("2000-01-01T00:00:00+00:00", got.work_item_id))
    recovered = recover_expired_leases(tmp_store)
    assert recovered == 1
    assert queue_metrics(tmp_store)["REOPENED"] == 1


# ---------- Completion + retry ----------


def test_complete_persists_result(tmp_store):
    enqueue_work_item(tmp_store, _item(idem="done"))
    got = claim_next_work_item(tmp_store, node_id="a", capabilities=["signal_research"], lease_seconds=60)
    assert got is not None
    result = WorkResultContract(
        work_item_id=got.work_item_id,
        worker_id="a",
        status=WorkResultStatus.COMPLETED,
        summary="Found a signal",
        artifact_paths=["/tmp/sig.json"],
        source_refs=["https://example.com/source"],
    )
    complete_work_item(tmp_store, work_item_id=got.work_item_id, node_id="a", result=result)
    metrics = queue_metrics(tmp_store)
    assert metrics.get("COMPLETED") == 1
    assert metrics.get("LEASED", 0) == 0


def test_fail_retryable_below_max_reopens(tmp_store):
    enqueue_work_item(tmp_store, _item(idem="retry-me"))
    a = claim_next_work_item(tmp_store, node_id="a", capabilities=["signal_research"], lease_seconds=60)
    assert a is not None
    out = fail_work_item(tmp_store, work_item_id=a.work_item_id, node_id="a",
                         error_class="network_timeout", retryable=True, summary="flake")
    assert out == "REOPENED"
    assert queue_metrics(tmp_store)["REOPENED"] == 1


def test_fail_at_max_attempts_dead_letters(tmp_store):
    # max_attempts=1 → lease (attempt=1) + fail = dead-letter
    enqueue_work_item(tmp_store, _item(idem="forever", max_attempts=1))
    a = claim_next_work_item(tmp_store, node_id="a", capabilities=["signal_research"], lease_seconds=60)
    assert a is not None
    out = fail_work_item(tmp_store, work_item_id=a.work_item_id, node_id="a",
                         error_class="permanent", retryable=True, summary="bad")
    assert out == "DEAD_LETTERED"
    assert queue_metrics(tmp_store)["DEAD_LETTERED"] == 1


# ---------- Opportunity ----------


def test_opportunity_upsert_and_filter(tmp_store):
    opp = OpportunityContract(
        title="Install governed audit at mid-market CPA",
        vertical="cpa",
        stage=OpportunityStage.QUALIFIED,
        direction_fit=9.0,
        pain_evidence=8.0,
        urgency_evidence=6.0,
        buyer_access=7.0,
        proof_advantage=8.0,
        speed_to_test=7.0,
        delivery_burden=4.0,
        recurrence_potential=9.0,
        ip_reuse_potential=7.0,
        confidence=6.0,
    )
    upsert_opportunity(tmp_store, opp)
    rows = list_opportunities(tmp_store, stage=OpportunityStage.QUALIFIED)
    assert len(rows) == 1
    assert rows[0]["title"].startswith("Install governed")


def test_opportunity_composite_score():
    opp = OpportunityContract(
        title="Test",
        stage=OpportunityStage.VALIDATING,
        direction_fit=10, pain_evidence=10, urgency_evidence=10, buyer_access=10,
        proof_advantage=10, speed_to_test=10, delivery_burden=2,
        recurrence_potential=10, ip_reuse_potential=10, confidence=10,
    )
    s = opp.composite()
    assert s > 25.0
    opp.delivery_burden = 10
    assert opp.composite() < s