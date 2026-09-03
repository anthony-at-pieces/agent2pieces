ALTER TABLE import_jobs
ADD COLUMN pieces_endpoint TEXT NOT NULL DEFAULT 'unknown';

ALTER TABLE import_jobs
ADD COLUMN context_hash TEXT NOT NULL DEFAULT '';

ALTER TABLE import_items
ADD COLUMN frozen_write_arguments_json TEXT NOT NULL DEFAULT '{}';

ALTER TABLE import_attempts
ADD COLUMN pieces_endpoint TEXT NOT NULL DEFAULT 'unknown';

UPDATE import_items
SET state = 'failed', error_detail = 'migration_context_unavailable'
WHERE job_id IN (
    SELECT job_id FROM import_jobs WHERE state IN ('queued', 'running', 'paused')
)
AND state NOT IN ('imported', 'remote_duplicate', 'skipped');

UPDATE import_jobs
SET state = 'failed',
    finished_at = COALESCE(finished_at, requested_at),
    pause_reason = NULL,
    error_detail = 'migration_context_unavailable'
WHERE state IN ('queued', 'running', 'paused');
