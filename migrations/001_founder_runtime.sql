-- 001_founder_runtime.sql
-- Founder Runtime durable state (Phase 1)
-- SQLite-compatible; Postgres-portable (TIMESTAMP -> TIMESTAMPTZ, JSON -> JSONB).

PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

-- ---------- nodes ----------
CREATE TABLE IF NOT EXISTS nodes (
    node_id           TEXT PRIMARY KEY,
    hostname          TEXT NOT NULL,
    status            TEXT NOT NULL,                  -- ONLINE | DRAINING | OFFLINE_UNVERIFIED | OFFLINE
    capabilities      TEXT NOT NULL DEFAULT '{}',     -- JSON: capability tags, model_routes, concurrency
    max_concurrency   INTEGER NOT NULL DEFAULT 2,
    current_load      INTEGER NOT NULL DEFAULT 0,
    last_heartbeat    TEXT,
    lan_address       TEXT,
    tailnet_address   TEXT,
    worker_version    TEXT,
    health_details    TEXT                             -- JSON
);

-- ---------- opportunities ----------
CREATE TABLE IF NOT EXISTS opportunities (
    opportunity_id           TEXT PRIMARY KEY,
    title                    TEXT NOT NULL,
    vertical                 TEXT,
    company_id               TEXT,
    stage                    TEXT NOT NULL,            -- SIGNAL | CANDIDATE | VALIDATING | QUALIFIED | EXPERIMENT_READY | EXPERIMENTING | SELL_READY | BUILD_READY | WON | LOST | PARKED | KILLED
    direction_fit            REAL,
    pain_evidence            REAL,
    urgency_evidence         REAL,
    buyer_access             REAL,
    proof_advantage          REAL,
    speed_to_test            REAL,
    delivery_burden          REAL,
    recurrence_potential     REAL,
    ip_reuse_potential       REAL,
    confidence               REAL,
    priority                 REAL,
    owner                    TEXT,
    next_action              TEXT,
    next_action_due_at       TEXT,
    evidence                 TEXT DEFAULT '[]',        -- JSON array of evidence refs
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_opportunities_stage      ON opportunities(stage);
CREATE INDEX IF NOT EXISTS ix_opportunities_priority   ON opportunities(priority DESC);

-- ---------- signals ----------
CREATE TABLE IF NOT EXISTS signals (
    signal_id            TEXT PRIMARY KEY,
    source_uri           TEXT NOT NULL,
    source_type          TEXT NOT NULL,                -- rss | http | scrape | manual | inbound_email | crm | kb
    observed_at          TEXT NOT NULL,
    content_hash         TEXT NOT NULL,
    summary              TEXT NOT NULL,
    entities             TEXT,                         -- JSON
    opportunity_id       TEXT,
    freshness_until      TEXT,
    evidence_strength    REAL,
    dedupe_key           TEXT UNIQUE,
    created_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_signals_observed ON signals(observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_signals_opportunity ON signals(opportunity_id);

-- ---------- work_items ----------
CREATE TABLE IF NOT EXISTS work_items (
    work_item_id           TEXT PRIMARY KEY,
    opportunity_id        TEXT,
    work_type              TEXT NOT NULL,                -- signal_research | offer_draft | audit_build | landing_build | experiment_design | ...
    objective              TEXT NOT NULL,
    payload                TEXT NOT NULL DEFAULT '{}',   -- JSON
    required_capabilities  TEXT NOT NULL DEFAULT '[]',   -- JSON
    status                 TEXT NOT NULL,                -- READY | LEASED | STARTED | COMPLETED | FAILED | DEAD_LETTERED | REOPENED
    priority               INTEGER NOT NULL DEFAULT 50,
    idempotency_key        TEXT NOT NULL UNIQUE,
    approval_lane          TEXT NOT NULL,                -- autonomous_local | mike_approval
    max_attempts           INTEGER NOT NULL DEFAULT 2,
    attempt_count          INTEGER NOT NULL DEFAULT 0,
    available_at           TEXT NOT NULL,
    lease_owner            TEXT,
    lease_expires_at       TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_work_items_status        ON work_items(status, priority DESC);
CREATE INDEX IF NOT EXISTS ix_work_items_available     ON work_items(available_at);
CREATE INDEX IF NOT EXISTS ix_work_items_opportunity   ON work_items(opportunity_id);

-- ---------- work_results ----------
CREATE TABLE IF NOT EXISTS work_results (
    result_id         TEXT PRIMARY KEY,
    work_item_id      TEXT NOT NULL,
    worker_id         TEXT NOT NULL,
    status            TEXT NOT NULL,                     -- COMPLETED | FAILED | DEAD_LETTERED
    summary           TEXT NOT NULL,
    artifact_paths    TEXT,                              -- JSON
    source_refs       TEXT,                              -- JSON
    metrics           TEXT,                              -- JSON
    proofpacket_path  TEXT,
    started_at        TEXT,
    completed_at      TEXT,
    error_class       TEXT,
    retryable         INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_work_results_work_item ON work_results(work_item_id);
CREATE INDEX IF NOT EXISTS ix_work_results_worker    ON work_results(worker_id);

-- ---------- experiments ----------
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id     TEXT PRIMARY KEY,
    opportunity_id    TEXT NOT NULL,
    hypothesis        TEXT NOT NULL,
    test_design       TEXT NOT NULL DEFAULT '{}',
    success_criteria  TEXT NOT NULL DEFAULT '{}',
    failure_criteria  TEXT NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL,                    -- DRAFT | RUNNING | CONCLUDED | CANCELLED
    owner             TEXT,
    started_at        TEXT,
    ended_at          TEXT,
    result            TEXT,                             -- JSON
    decision          TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_experiments_opportunity ON experiments(opportunity_id);

-- ---------- decisions ----------
CREATE TABLE IF NOT EXISTS decisions (
    decision_id         TEXT PRIMARY KEY,
    opportunity_id      TEXT,
    decision_type       TEXT NOT NULL,
    question            TEXT NOT NULL,
    options             TEXT NOT NULL DEFAULT '[]',
    recommendation      TEXT,
    evidence_refs       TEXT DEFAULT '[]',
    status              TEXT NOT NULL,                   -- PROPOSED | APPROVED | REJECTED | SUPERSEDED
    approval_required   INTEGER NOT NULL DEFAULT 0,
    approved_by         TEXT,
    decided_at          TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_decisions_opportunity ON decisions(opportunity_id);

-- ---------- approval_requests ----------
CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id            TEXT PRIMARY KEY,
    action_type            TEXT NOT NULL,                -- send_email | post | spend | pricing | commit | deploy | dns | credential | destructive | export
    target                 TEXT NOT NULL,
    exact_content_or_diff  TEXT NOT NULL DEFAULT '{}',   -- JSON
    business_reason        TEXT NOT NULL,
    rollback_plan          TEXT,
    status                 TEXT NOT NULL,                -- PENDING | APPROVED | REJECTED | EXPIRED
    requested_at           TEXT NOT NULL,
    resolved_at            TEXT,
    resolved_by            TEXT
);

CREATE INDEX IF NOT EXISTS ix_approval_requests_status ON approval_requests(status);

-- ---------- proof_packets (index for the queue) ----------
CREATE TABLE IF NOT EXISTS proof_packets (
    proof_id         TEXT PRIMARY KEY,
    work_item_id     TEXT,
    opportunity_id   TEXT,
    result_id        TEXT,
    verifier_node    TEXT NOT NULL,
    verifier_model   TEXT NOT NULL,
    verdict          TEXT NOT NULL,                       -- PASS | FAIL | REOPEN
    evidence_hash    TEXT NOT NULL,
    packet_path      TEXT NOT NULL,
    sealed_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_proof_packets_work_item ON proof_packets(work_item_id);

-- ---------- append-only audit ----------
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id      TEXT PRIMARY KEY,
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    target        TEXT,
    detail        TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_audit_log_created ON audit_log(created_at DESC);