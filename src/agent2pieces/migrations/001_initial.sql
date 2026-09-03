CREATE TABLE settings (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    version INTEGER NOT NULL,
    ledger_instance_id TEXT NOT NULL,
    mcp_base_url TEXT NOT NULL,
    codex_enabled INTEGER NOT NULL CHECK (codex_enabled IN (0, 1)),
    claude_enabled INTEGER NOT NULL CHECK (claude_enabled IN (0, 1)),
    hermes_enabled INTEGER NOT NULL CHECK (hermes_enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE source_roots (
    root_id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    lexical_path TEXT NOT NULL,
    resolved_path TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    is_default INTEGER NOT NULL CHECK (is_default IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE (agent, resolved_path)
);

CREATE TABLE host_path_mappings (
    mapping_id TEXT PRIMARY KEY,
    local_root TEXT NOT NULL UNIQUE,
    host_root TEXT NOT NULL,
    project TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE scan_runs (
    scan_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    settings_version INTEGER NOT NULL,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    accepted_count INTEGER NOT NULL DEFAULT 0,
    excluded_count INTEGER NOT NULL DEFAULT 0,
    quarantine_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    error_detail TEXT
);

CREATE TABLE source_revisions (
    revision_id TEXT PRIMARY KEY,
    predecessor_revision_id TEXT REFERENCES source_revisions(revision_id),
    agent TEXT NOT NULL,
    root_id TEXT NOT NULL REFERENCES source_roots(root_id),
    source_key TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    candidate_input_hash TEXT NOT NULL,
    first_observed_at TEXT NOT NULL,
    UNIQUE (agent, root_id, source_key, source_hash, candidate_input_hash)
);

CREATE TABLE source_observations (
    observation_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES source_revisions(revision_id),
    scan_id TEXT NOT NULL REFERENCES scan_runs(scan_id),
    source_updated_at TEXT NOT NULL,
    file_mtime_ns INTEGER NOT NULL,
    raw_byte_count INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE (revision_id, scan_id)
);

CREATE TABLE source_dispositions (
    disposition_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES scan_runs(scan_id),
    agent TEXT NOT NULL,
    root_id TEXT NOT NULL REFERENCES source_roots(root_id),
    source_path TEXT NOT NULL,
    source_key TEXT,
    disposition TEXT NOT NULL,
    byte_count INTEGER NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE candidates (
    candidate_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL UNIQUE REFERENCES source_revisions(revision_id),
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    original_payload_json TEXT NOT NULL,
    current_payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    import_id TEXT NOT NULL,
    group_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    superseded_at TEXT
);

CREATE TABLE candidate_payload_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    candidate_version INTEGER NOT NULL,
    source_revision_id TEXT NOT NULL REFERENCES source_revisions(revision_id),
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (candidate_id, candidate_version, state)
);

CREATE TABLE safety_findings (
    finding_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    candidate_version INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    severity TEXT NOT NULL,
    line_number INTEGER,
    state TEXT NOT NULL,
    override_reason TEXT,
    override_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE duplicate_checks (
    check_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    candidate_version INTEGER NOT NULL,
    coverage TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    verdict TEXT,
    error_detail TEXT
);

CREATE TABLE duplicate_evidence (
    evidence_id TEXT PRIMARY KEY,
    check_id TEXT NOT NULL REFERENCES duplicate_checks(check_id),
    target_kind TEXT NOT NULL,
    target_key TEXT NOT NULL,
    target_title TEXT,
    target_excerpt TEXT,
    payload_hash TEXT,
    cosine REAL,
    body_shingle_jaccard REAL,
    title_jaccard REAL,
    classification TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    remote_rank INTEGER
);

CREATE TABLE review_groups (
    group_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    representative_candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_from_check_id TEXT REFERENCES duplicate_checks(check_id),
    representative_overridden INTEGER NOT NULL CHECK (representative_overridden IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE review_group_evidence (
    group_id TEXT NOT NULL REFERENCES review_groups(group_id),
    evidence_id TEXT NOT NULL REFERENCES duplicate_evidence(evidence_id),
    PRIMARY KEY (group_id, evidence_id)
);

CREATE TABLE import_jobs (
    job_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    current_ordinal INTEGER NOT NULL,
    remote_search_available INTEGER NOT NULL CHECK (remote_search_available IN (0, 1)),
    duplicate_risk_acknowledged_at TEXT,
    duplicate_risk_ack_text_version TEXT,
    pause_reason TEXT,
    error_detail TEXT
);

CREATE TABLE import_items (
    item_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES import_jobs(job_id),
    ordinal INTEGER NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    candidate_version INTEGER NOT NULL,
    frozen_payload_json TEXT NOT NULL,
    import_id TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    pieces_memory_id TEXT,
    completed_at TEXT,
    error_detail TEXT,
    UNIQUE (job_id, ordinal),
    UNIQUE (job_id, candidate_id)
);

CREATE TABLE import_attempts (
    attempt_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES import_items(item_id),
    attempt_number INTEGER NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    marker TEXT NOT NULL,
    started_at TEXT NOT NULL,
    dispatch_started_at TEXT,
    finished_at TEXT,
    marker_outcome TEXT,
    parent_memory_id TEXT,
    response_id TEXT,
    error_detail TEXT,
    UNIQUE (item_id, attempt_number)
);

CREATE INDEX idx_source_revisions_source ON source_revisions(agent, root_id, source_key);
CREATE INDEX idx_candidates_status ON candidates(status);
CREATE INDEX idx_safety_findings_candidate ON safety_findings(candidate_id, candidate_version);
CREATE INDEX idx_duplicate_evidence_check ON duplicate_evidence(check_id);
CREATE INDEX idx_import_items_job ON import_items(job_id, ordinal);
CREATE INDEX idx_import_attempts_item ON import_attempts(item_id, attempt_number);
