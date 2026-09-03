CREATE TABLE duplicate_evidence_candidates (
    evidence_id TEXT NOT NULL REFERENCES duplicate_evidence(evidence_id),
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    candidate_version INTEGER NOT NULL,
    target_key TEXT NOT NULL,
    target_title TEXT,
    PRIMARY KEY (evidence_id, candidate_id, candidate_version)
);

CREATE INDEX idx_duplicate_evidence_candidates_candidate
ON duplicate_evidence_candidates(candidate_id, candidate_version, evidence_id);
