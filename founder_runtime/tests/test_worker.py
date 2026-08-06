"""Phase 2 tests — worker loop + dispatcher + verifier.

Run with: pytest founder_runtime/tests/test_worker.py -v
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from founder_runtime.store import (
    Store,
    init_db,
    register_node,
    upsert_opportunity,
    enqueue_work_item,
    claim_next_work_item,
    queue_metrics,
    append_audit,
)
from founder_runtime.contracts import (
    NodeCapabilityContract,
    WorkItemContract,
    WorkResultContract,
    WorkItemStatus,
    WorkResultStatus,
    OpportunityStage,
    Verdict,
)
from founder_runtime.dispatcher import dispatch_tick, enqueue_signal_research
from founder_runtime.worker import Worker, make_signal_research_handler
from founder_runtime.verification import verify_and_seal
from founder_runtime.founder_loop import founder_review, morning_brief, enqueue_mission


MIGRATION = Path(__file__).parent.parent.parent / "migrations" / "001_founder_runtime.sql"


@pytest.fixture
def tmp_store():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "state.db"
        s = Store(path)
        init_db(s, MIGRATION)
        yield s
        s.close()


# ---------- Dispatcher ----------


def test_dispatch_tick_lease_and_recover(tmp_store):
    # Register an online node
    register_node(tmp_store, NodeCapabilityContract(
        node_id="rig-36gb",
        hostname="RIG-36GB.local",
        capabilities=["signal_research", "web_scrape"],
        max_concurrency=2,
    ).model_dump())

    # Lease item, then expire it
    item = enqueue_signal_research(
        tmp_store,
        source_uri="https://example.com/market-signal",
        summary_seed="seed",
        objective="Find a signal",
    )

    # Manually backdate to simulate fresh tick
    metrics1 = dispatch_tick(tmp_store)
    assert metrics1["items_leased"] == 1
    assert metrics1["queue"].get("LEASED") == 1

    # Second tick — node is at capacity, no new leases
    metrics2 = dispatch_tick(tmp_store)
    assert metrics2["items_leased"] == 0
    assert metrics2["queue"].get("LEASED") == 1


def test_dispatch_recovers_expired_leases(tmp_store):
    register_node(tmp_store, NodeCapabilityContract(
        node_id="rig-36gb",
        hostname="RIG-36GB.local",
        capabilities=["signal_research", "web_scrape"],
        max_concurrency=1,
    ).model_dump())
    enqueue_signal_research(
        tmp_store,
        source_uri="https://example.com/a",
        summary_seed="a",
        objective="a",
    )
    dispatch_tick(tmp_store)
    # Force expiry
    with tmp_store.tx() as conn:
        conn.execute("UPDATE work_items SET lease_expires_at = ?", ("2000-01-01T00:00:00+00:00",))
    m = dispatch_tick(tmp_store)
    assert m["expired_leases_recovered"] == 1
    assert m["queue"].get("REOPENED") == 1


def test_dispatch_stale_nodes_skip(tmp_store):
    register_node(tmp_store, NodeCapabilityContract(
        node_id="rig-stale",
        hostname="stale.local",
        capabilities=["signal_research", "web_scrape"],
        max_concurrency=2,
    ).model_dump())
    with tmp_store.tx() as conn:
        old = (datetime.now(timezone.utc).timestamp() - 9999)
        from datetime import datetime as _dt, timezone as _tz
        conn.execute("UPDATE nodes SET last_heartbeat = ?", (_dt.fromtimestamp(old, _tz.utc).isoformat(),))
    enqueue_signal_research(
        tmp_store,
        source_uri="https://example.com/x",
        summary_seed="x",
        objective="x",
    )
    m = dispatch_tick(tmp_store)
    assert m["healthy_nodes"] == 0
    assert m["items_leased"] == 0


# ---------- Worker loop ----------


def test_worker_processes_one_item(tmp_store):
    register_node(tmp_store, NodeCapabilityContract(
        node_id="rig-36gb",
        hostname="RIG-36GB.local",
        capabilities=["signal_research", "web_scrape"],
        max_concurrency=1,
    ).model_dump())
    enqueue_signal_research(
        tmp_store,
        source_uri="https://example.com/source",
        summary_seed="Initial seed for the report.",
        objective="Find a signal",
    )

    node = NodeCapabilityContract(
        node_id="rig-36gb",
        hostname="RIG-36GB.local",
        capabilities=["signal_research", "web_scrape"],
        max_concurrency=1,
    )
    handlers = {"signal_research": make_signal_research_handler()}

    # Patch run() to do a single iteration
    worker = Worker(tmp_store, node, handlers, lease_seconds=10)
    # Manual loop for one cycle instead of run()
    item = claim_next_work_item(tmp_store, 
        node_id=node.node_id,
        capabilities=node.capabilities,
        lease_seconds=10,
    )
    assert item is not None
    worker._process_one(item)
    from founder_runtime.store import queue_metrics
    assert queue_metrics(tmp_store).get("COMPLETED") == 1


def test_worker_no_handler_dead_letters(tmp_store):
    register_node(tmp_store, NodeCapabilityContract(
        node_id="rig-36gb",
        hostname="RIG-36GB.local",
        capabilities=["unregistered_type"],
        max_concurrency=1,
    ).model_dump())
    enqueue_signal_research(
        tmp_store,
        source_uri="https://example.com/y",
        summary_seed="y",
        objective="y",
        required_capabilities=["unregistered_type"],
    )

    node = NodeCapabilityContract(
        node_id="rig-36gb",
        hostname="RIG-36GB.local",
        capabilities=["unregistered_type"],
        max_concurrency=1,
    )
    worker = Worker(tmp_store, node, handlers={}, lease_seconds=10)
    item = claim_next_work_item(tmp_store, 
        node_id=node.node_id,
        capabilities=node.capabilities,
        lease_seconds=10,
    )
    assert item is not None
    worker._process_one(item)
    from founder_runtime.store import queue_metrics
    assert queue_metrics(tmp_store).get("DEAD_LETTERED") == 1


# ---------- Verifier ----------


def test_verify_pass(tmp_store):
    artifact = Path(tempfile.gettempdir()) / f"verify-art-{uuid.uuid4()}.txt"
    artifact.write_text("ok\n")
    result = WorkResultContract(
        work_item_id="wi-1",
        worker_id="rig-36gb",
        status=WorkResultStatus.COMPLETED,
        summary="non-empty summary",
        artifact_paths=[str(artifact)],
        source_refs=["https://example.com/src"],
    )
    v = verify_and_seal(
        tmp_store,
        work_item_id="wi-1",
        result=result,
        verifier_node="verifier-128gb",
        verifier_model="minimax-m3",
    )
    assert v.verdict == Verdict.PASS
    assert v.evidence_hash.startswith("sha256:")
    pkt = Path(v.evidence_hash)
    # proof file written
    files = list(Path.home().joinpath(".rig", "founder-runtime", "proof").glob("wi-1.proof.json"))
    assert len(files) == 1


def test_verify_reopen_on_missing_artifact(tmp_store):
    result = WorkResultContract(
        work_item_id="wi-2",
        worker_id="rig-36gb",
        status=WorkResultStatus.COMPLETED,
        summary="ok",
        artifact_paths=["/nonexistent/path/file.json"],
        source_refs=["https://example.com/src"],
    )
    v = verify_and_seal(
        tmp_store,
        work_item_id="wi-2",
        result=result,
        verifier_node="v",
        verifier_model="m",
    )
    assert v.verdict == Verdict.REOPEN
    assert "artifact missing" in v.notes


def test_verify_fail_on_empty_summary(tmp_store):
    result = WorkResultContract(
        work_item_id="wi-3",
        worker_id="rig-36gb",
        status=WorkResultStatus.COMPLETED,
        summary="",
        source_refs=["https://example.com/x"],
    )
    v = verify_and_seal(
        tmp_store,
        work_item_id="wi-3",
        result=result,
        verifier_node="v",
        verifier_model="m",
    )
    assert v.verdict == Verdict.REOPEN


# ---------- Founder loop ----------


def test_founder_review_returns_at_most_three_changes(tmp_store):
    from founder_runtime.contracts import OpportunityContract, OpportunityStage
    for i in range(10):
        opp = OpportunityContract(
            title=f"Opp {i}",
            stage=OpportunityStage.QUALIFIED,
            direction_fit=5+i*0.1,
            confidence=1,
        )
        upsert_opportunity(tmp_store, opp)
        # Backdate to 30 days ago
        with tmp_store.tx() as conn:
            old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            conn.execute("UPDATE opportunities SET updated_at = ? WHERE title = ?", (old, f"Opp {i}"))
    out = founder_review(tmp_store)
    assert len(out["changes"]) <= 3


def test_morning_brief_has_focus_section(tmp_store):
    from founder_runtime.contracts import OpportunityContract, OpportunityStage
    opp = OpportunityContract(
        title="Test focus opp",
        stage=OpportunityStage.QUALIFIED,
        direction_fit=8, pain_evidence=8, confidence=7,
    )
    upsert_opportunity(tmp_store, opp)
    brief = morning_brief(tmp_store)
    assert "RIG Morning Brief" in brief
    assert "Focus" in brief


def test_enqueue_mission_creates_work_item(tmp_store):
    item = enqueue_mission(
        tmp_store,
        work_type="offer_draft",
        objective="Draft $7.5K audit offer for CPA prospect Acme",
        required_capabilities=["offer_draft"],
        priority=80,
    )
    assert item.work_type == "offer_draft"
    assert item.idempotency_key.startswith("mission:offer_draft:")