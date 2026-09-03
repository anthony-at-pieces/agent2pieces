# Architecture: Agent2Pieces v1

## Status

This document fixes the v1 implementation contract for a greenfield Python 3.12 application. `requirements.md` owns observable behavior. This file owns module boundaries, persisted data, algorithms, HTTP contracts, and failure handling.

## System shape

Agent2Pieces is one local process with four boundaries:

```text
read-only memory roots
        |
        v
scanner adapters -> normalization -> SQLite ledger -> review API -> bundled browser UI
                                             |
                                             v
                                  duplicate coordinator
                                   |               |
                              local scorer    Pieces search
                                             |
                                             v
                                    sequential importer
                                             |
                                             v
                                      Pieces MCP v2
```

Uvicorn hosts FastAPI and the static application on loopback. Scans and imports run as single-process background jobs. SQLite is the durable authority for review decisions and job recovery. There is no external queue, watcher, scheduler, or frontend build service at runtime.

## Planned files

| Path | Role |
|---|---|
| `pyproject.toml` | Python 3.12 package, locked runtime groups, lint, type, and test configuration |
| `README.md` | Packaged usage and CLI reference |
| `LICENSE` | License included in every release archive |
| `src/agent2pieces/__init__.py` | Package version |
| `src/agent2pieces/cli.py` | Exact `serve`, `scan`, and standalone health-check command surface |
| `src/agent2pieces/config.py` | Defaults, saved settings, path expansion, and loopback validation |
| `src/agent2pieces/models.py` | Typed domain records, enums, and request-independent validation |
| `src/agent2pieces/normalization.py` | Canonical candidate fields, tokenization, links, hashes, and import marker |
| `src/agent2pieces/safety.py` | Deterministic secret blocks, PII warnings, and safe finding metadata |
| `src/agent2pieces/scanners/base.py` | Read-only source adapter contract and confined traversal |
| `src/agent2pieces/scanners/codex.py` | Curated Codex rollout summary discovery |
| `src/agent2pieces/scanners/claude.py` | Claude project topic-file discovery and exclusions |
| `src/agent2pieces/scanners/hermes.py` | Hermes `MEMORY.md` U+00A7 entry parsing |
| `src/agent2pieces/ledger.py` | SQLite connection policy, transactions, repositories, and optimistic updates |
| `src/agent2pieces/migrations/001_initial.sql` | Initial ledger schema and indexes |
| `src/agent2pieces/dedupe.py` | Exact hashes, local token scores, classification, and evidence |
| `src/agent2pieces/mcp_client.py` | MCP v2 initialization, transport fallback, capability discovery, and tool calls |
| `src/agent2pieces/services.py` | Scan, duplicate-check, grouping, and import job orchestration |
| `src/agent2pieces/routes.py` | Approved JSON route handlers and request/response models |
| `src/agent2pieces/api.py` | FastAPI assembly, health endpoint, and packaged static-route wiring |
| `src/agent2pieces/security.py` | Host, same-origin, CSRF, response headers, and safe error middleware |
| `src/agent2pieces/static/index.html` | Packaged single-page review shell and CSRF bootstrap |
| `src/agent2pieces/static/app.js` | Browser state, API client, edits, groups, polling, and import controls |
| `src/agent2pieces/static/app.css` | Bundled layout and review styles |
| `agent2pieces.spec` | PyInstaller entrypoint and data-file manifest |
| `scripts/build_native.py` | Repeatable native build and checksum generation |
| `scripts/validate_release_workflow.py` | Static verification of CI matrix and artifact commands |
| `.github/workflows/release.yml` | Native build matrix and tag-gated GitHub Release publication |
| `tests/fixtures/sources/` | Read-only Codex, Claude, Hermes, malformed, and symlink fixtures |
| `tests/unit/` | Scanner, normalization, ledger, dedupe, and MCP unit tests |
| `tests/integration/` | API, security, services, import recovery, and CLI tests |
| `tests/browser/` | Playwright Chromium review-flow tests |
| `tests/acceptance/` | End-to-end fixture and packaged-binary acceptance tests |

## Source adapters

### Shared traversal contract

Each configured source has `{agent, root, enabled}`. `agent` is `codex`, `claude`, or `hermes`. Roots are expanded to absolute paths when settings are saved. A scan records the exact resolved root used.

Traversal uses `lstat` and does not descend through directory symlinks. For a file symlink, the adapter calls `resolve(strict=True)`, verifies that `os.path.commonpath([resolved_root, resolved_file]) == resolved_root`, and then verifies a regular file. Escapes, broken links, devices, sockets, and unreadable adapter-relevant paths become quarantine rows. Source handles are binary and read-only. Tests require source bytes, mtime, and mode to remain unchanged. Reading may update atime under the host filesystem, and v1 makes no guarantee about it. One bad source never aborts the other configured roots.

Adapters open inputs with binary reads, apply the 65,536-byte limit per candidate source unit, decode strict UTF-8, and never open a write handle. Codex and Claude treat one file as one source unit. Hermes reads a `MEMORY.md` container and applies the byte limit to each U+00A7-delimited entry so a valid multi-entry file can exceed 65,536 bytes.

### Codex adapter

- Default root: `${CODEX_HOME:-~/.codex}/memories/rollout_summaries`.
- Accepted files: every regular file, including a hidden file, whose basename ends with case-sensitive `.md`.
- Excluded files: every regular file whose basename ends with case-sensitive `.jsonl`; record one `excluded_raw` quarantine row and include it in discovered and quarantined counts.
- Ignored files: every other regular file; create no observation or quarantine row and do not include it in discovered counts. There is no filename or content heuristic for transcript, log, or temporary Markdown files.
- Title: first non-empty H1 with Markdown markers removed, else file stem.
- Body: Markdown after supported front matter, kept verbatim apart from newline normalization and outer blank lines.
- `project_scope`: valid front-matter `project` or `cwd`, else the nearest parent directory relative to the configured root.
- `source_key`: POSIX relative path from the configured root.

### Claude adapter

- Default root pattern: `${CLAUDE_CONFIG_DIR:-~/.claude}/projects/*/memory`.
- Accepted files: every regular file whose basename ends with case-sensitive `.md`, subject to the two exclusions below.
- Excluded index: exact case-sensitive basename `MEMORY.md`; record `excluded_index`.
- Excluded user memory: flat front matter scalar `type` whose trimmed value equals `user` after ASCII case folding; record `excluded_user`.
- Ignored files: every regular non-`.md` file; create no row and do not include it in discovered counts.
- Title: front-matter `title`, else first non-empty H1, else file stem.
- Body: Markdown after front matter, with outer blank lines removed.
- `project_scope`: front-matter `project`, else the project directory between `projects/` and `memory/`, else root-relative parent.
- `source_key`: POSIX relative path from the configured root.

Front matter parsing is deliberately narrow. It accepts a leading `---` block with flat scalar keys used above. Unterminated blocks, duplicate required keys, or invalid UTF-8 are malformed. Unsupported nested YAML is ignored unless it occupies a required key.

### Hermes adapter

- Default root: `${HERMES_HOME:-~/.hermes}/memories`.
- Accepted files: regular files with exact case-sensitive basename `MEMORY.md`.
- Ignored files: every other regular file; create no row and do not include it in discovered counts.
- Split: literal U+00A7 code point. Delimiter-only whitespace gaps are ignored.
- Title: first non-empty Markdown heading in the entry, else the first non-empty line truncated to 120 Unicode code points, else `Hermes memory <index>`.
- Body: the entry with outer blank lines removed.
- `project_scope`: root-relative parent directory, with `.` mapped to `hermes`.
- `source_key`: `<relative-memory-path>#section=<one-based-non-empty-index>`.

The raw entry, rather than the whole container, is the source unit for size checks and source canonicalization. An invalid container encoding quarantines the container because its delimiters cannot be read safely.

### Quarantine

Source disposition is a ledger record. Agent2Pieces does not move or copy source files. Each row records disposition `excluded|quarantined`, scan ID, adapter, configured root, source path, optional source key, observed byte count, safe reason code, bounded error detail, and time. Excluded reason codes are `excluded_raw`, `excluded_index`, and `excluded_user`. Quarantine reason codes are `empty`, `oversize`, `invalid_utf8`, `malformed`, `symlink_escape`, `broken_symlink`, `special_file`, and `unreadable`. Unsupported and ignored files produce no row. Per agent and in total, `discovered = accepted + excluded + quarantined`; ignored entries do not contribute. Counts are disjoint.

### Source-hash canonicalization

`source_hash` identifies meaningful source text, not file encoding trivia. The adapter first removes only its supported source metadata:

- Codex and Claude remove one valid leading flat front matter block, including its opening/closing delimiter lines.
- Hermes hashes one delimiter-separated entry; the U+00A7 delimiter and container text outside that entry are absent.
- No Markdown heading, link, comment, or body field is removed.

The metadata-stripped text then passes through these operations in order:

1. Apply Unicode NFKC to the entire string.
2. Replace CRLF and lone CR with LF.
3. Split on LF while retaining a final empty line, then remove only trailing ASCII space U+0020 and tab U+0009 characters from every line.
4. Define a blank line as an empty line after step 3. Replace each run of three or more consecutive blank lines with exactly two blank lines.
5. Remove all leading and trailing blank lines.
6. Preserve every other code point, interior space/tab, non-ASCII whitespace character, line order, and one-versus-two interior blank-line distinction.
7. Join the remaining lines with one LF and no terminal LF.

Encode the result as UTF-8 and compute lowercase SHA-256 hex. An empty result is quarantined as `empty`. `source_hash` remains the identity of meaningful metadata-stripped source text; it is necessary but not sufficient for source-revision reuse. Every scan appends a source observation with raw byte count, mtime, source-updated time, and observed time.

## Normalized candidate contract

The accepted adapter unit becomes this immutable normalized record before review edits:

| Field | Contract |
|---|---|
| `source_agent` | `codex`, `claude`, or `hermes` |
| `source_key` | Stable adapter key within one configured root |
| `source_path` | Absolute lexical input path for audit; never sent to Pieces |
| `project_scope` | Trimmed project label, maximum 512 code points |
| `source_updated_at` | Audit-only UTC RFC 3339 timestamp from flat front matter key `updated_at` when it is valid RFC 3339, else file mtime captured before read; Hermes always uses file mtime |
| `title` | Plain text, trimmed, internal whitespace collapsed, 1 to 240 code points |
| `markdown_body` | UTF-8 Markdown with CRLF/CR converted to LF and outer blank lines removed, 1 to 65,536 bytes |
| `external_links` | First-seen unique absolute URLs from Markdown links and bare URLs after `urllib.parse` validation requires `http` or `https`, non-empty host, no username/password, and no control characters |
| `source_hash` | Lowercase SHA-256 hex of metadata-stripped source text after the exact source canonicalization above |
| `payload_hash` | Lowercase SHA-256 hex of canonical payload JSON |
| `import_id` | Visible 26-character lowercase RFC 4648 base32 prefix derived only from `payload_hash` |

Canonical approved user content JSON has exactly the keys `external_links`, `markdown_body`, and `title`. Links remain in first-seen order after validation and deduplication. Serialization is `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")`; `payload_hash` is lowercase SHA-256 hex of those bytes. Source agent, configured root, source key, source path, project scope, source timestamps, source hash, safety findings, generated provenance, and marker text are excluded.

`candidate_input_hash` is an internal ledger field, not a twelfth normalized candidate output field and not part of any JSON API response. It is lowercase SHA-256 hex over the same exact JSON serialization with exactly `external_links`, `markdown_body`, `project_scope`, and `title`, captured before user edits. Its values are canonicalized as follows:

- `title` applies NFKC, removes leading and trailing code points for which Python `str.isspace()` is true, and replaces each remaining run of one or more such whitespace code points with one ASCII space.
- `markdown_body` is the normalized body after NFKC and the exact LF, trailing-space/tab, blank-run, and outer-blank operations used by source canonicalization. All other Markdown remains.
- `external_links` contains the validated, deduplicated HTTP(S) URLs sorted ascending by their UTF-8 byte sequences, independent of source order.
- `project_scope` applies NFKC and removes only leading and trailing code points for which Python `str.isspace()` is true; internal content remains unchanged.

Audit-only `source_updated_at`, file mtime, paths, source identity, and adapter metadata that does not supply one of these four values are excluded. User review edits never recompute `candidate_input_hash`; they affect candidate version and, where applicable, `payload_hash` as already defined.

The visible Import ID is the first 130 bits of the `payload_hash` digest encoded as exactly 26 lowercase RFC 4648 base32 characters without padding. The reference operation is:

```python
base64.b32encode(bytes.fromhex(payload_hash)).decode("ascii").lower()[:26]
```

This content-only identity is intentional. Exact approved content from different agents, roots, files, or timestamps shares one `payload_hash`, Import ID, and marker. Source provenance remains separately auditable in SQLite.

Review edits to title, body, or links change `payload_hash` and Import ID. A project-scope edit changes candidate version but not either hash. Provenance fields remain immutable. Revision lookup uses the full `(source_agent, root_id, source_key, source_hash, candidate_input_hash)` identity. If both hashes match, the scan reuses the revision and candidate and appends an observation. This makes mtime-only, `updated_at`-only, line-ending-only, NFKC-equivalent, and defined harmless-whitespace-only changes observation-only events with no candidate version, approval, payload hash, or Import ID change.

If either hash differs, the scanner inserts a successor source revision linked to its predecessor. A title-only, project-only, or external-link-only change therefore creates a successor even when metadata stripping leaves `source_hash` unchanged. An unimported candidate records its prior state as a payload snapshot, advances to the successor revision and its normalized values, increments version, and returns to pending review; an imported candidate remains immutable and receives a new pending successor candidate. No source revision or candidate audit snapshot is overwritten or deleted.

The visible marker is a standalone text line appended only at dispatch:

```text
Agent2Pieces Import ID: <import_id>
```

Immediately before dispatch, `summary` is rendered from the frozen approved body plus this standardized provenance block:

```text
---
Imported by Agent2Pieces
Source agent: <Codex|Claude|Hermes>
Agent2Pieces Import ID: <import_id>
```

If an explicit host-visible path mapping supplies a project label, add `Project: <mapped-project>` before the Import ID line. Never append source key, local path, or unmapped project scope. The entire generated block is excluded from `payload_hash` and local similarity text.

## Safety findings

Safety scanning runs on normalized body and title after discovery and after every edit. Matching is deterministic and never calls a model.

Blocking secret reason codes and signatures are:

- `secret_private_key`: `-----BEGIN ` followed on the same line by an optional key type and `PRIVATE KEY-----`.
- `secret_aws_access_key`: token boundary, `AKIA` or `ASIA`, then exactly 16 uppercase letters or digits.
- `secret_github_token`: token boundary followed by `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, or `github_pat_` and at least 20 ASCII letters, digits, or underscore characters.
- `secret_slack_token`: token boundary followed by `xoxb-`, `xoxp-`, `xoxa-`, `xoxr-`, or `xoxs-` and at least 20 ASCII letters, digits, or hyphens.
- `secret_openai_key`: token boundary followed by `sk-` and at least 20 ASCII letters, digits, underscores, or hyphens.
- `secret_assignment`: ASCII case-insensitive key name `api_key`, `apikey`, `access_token`, `client_secret`, or `password`, optional spaces, `:` or `=`, optional spaces, then a non-whitespace value of at least 8 characters.

PII warning codes are `pii_email` for an RFC-5322-shaped ASCII mailbox, `pii_phone` for 10 to 15 digits with common separators, `pii_us_ssn` for `NNN-NN-NNNN`, and `pii_payment_card` for 13 to 19 digits with spaces or hyphens that pass Luhn. These are warnings because deterministic patterns have false positives.

Each finding stores UUID4 ID, candidate version, reason code, severity `block` or `warn`, one-based line number, state `open|cleared|overridden`, override timestamp, and override reason. It does not store matched text, capture groups, or a value-derived digest. API and logs may emit the safe reason code and line only.

Approval controls default unchecked. Open findings block `approve` for a candidate or group. The user can edit content until a rescan clears them or send `override_findings` with every open finding ID, a checked acknowledgement, and a 10 to 500 character reason. An edit invalidates overrides and rescans. Group approval requires the representative to have no unhandled finding.

## Duplicate algorithm

### Text preparation

Local scoring is deterministic and has no model call.

1. Remove the Agent2Pieces marker, Markdown HTML blocks, link destinations, and punctuation from comparison text.
2. Apply Unicode NFKC, lowercase with `casefold`, and tokenize letter/number runs.
3. Body cosine uses raw unigram term-frequency vectors and the standard cosine formula. Two empty vectors score `0.0`.
4. Body shingle Jaccard uses sets of contiguous 5-token tuples. If either side has fewer than 5 tokens, the score is `0.0` unless payload hashes already matched.
5. Title Jaccard uses sets of normalized title tokens. If either title set is empty, the score is `0.0`.

### Candidate pool

Every pending or edited candidate is compared with:

- non-superseded candidates in the current scan and ledger;
- every imported payload retained in the ledger; and
- Pieces annotations returned by `annotations_full_text_search`, when available.

The general Pieces duplicate query uses the normalized title plus the first 2,000 body characters. It reads at most 50 unique annotation records across pages when the live schema exposes a cursor. Without a cursor it requests one page with limit 50. `has_more`, `next_cursor`, or a server truncation flag after the 50th unique annotation sets coverage `truncated`; absence cannot be inferred from that result set. Returned `annotation.text` supplies comparison text. Remote rank can be stored as evidence but cannot change the local classification.

### Exact marker search contract

Marker preflight and recovery query the entire line `Agent2Pieces Import ID: <id>`, where `<id>` matches `[a-z2-7]{26}`. A returned annotation matches only when `annotation.text` contains that exact standalone line after CRLF normalization. Substrings, HTML comments, case changes, and matches only in titles or metadata do not count.

For each matching annotation, parent memory identity is the first non-empty scalar found in this fixed order: `annotation.summary.id`, `annotation.summary.reference.id`, `annotation.summary_id`, `summary.id`, `summary_id`, `memory.id`, `memory_id`. Multiple matching annotations with the same parent ID represent one remote memory. `annotation.id` is evidence identity only and is never used as a parent memory ID.

Marker search reads no more than 50 unique annotations, paging when a live cursor is available. Outcomes are:

- `absent`: zero matching annotations, no missing parent IDs, and the server proves results are complete within the cap.
- `one_parent`: one distinct stable parent ID and no parentless matching annotation.
- `multiple_parents`: more than one distinct stable parent ID.
- `parent_unknown`: any matching annotation lacks a stable parent ID.
- `truncated`: the cap is reached while the server reports more results, returned annotation text is flagged truncated, or result completeness cannot be established.
- `search_error`: tool error or response-shape failure.

Before every write, only `absent` permits dispatch. `one_parent`, `multiple_parents`, and `parent_unknown` block the write as remote marker evidence and leave the candidate approved. `truncated` and `search_error` pause the job because absence is unproven. During ambiguous-write recovery, only `one_parent` proves that current-job write succeeded; every other outcome stays ambiguous. When the search tool is unavailable, the per-job risk acknowledgement is required instead of preflight.

### Classification order

The strongest matching rule wins:

1. `exact`: equal `payload_hash`, or an exact `import_id` marker match.
2. `likely`: body cosine `>= 0.92`, or body 5-token shingle Jaccard `>= 0.80`.
3. `possible`: `0.78 <= body cosine < 0.92` and title-token Jaccard `>= 0.50`.
4. `distinct`: no prior rule matches.

Scores are stored to six decimal places with the rule ID and evidence source. A candidate verdict is the strongest pair result. Ties prefer an imported local memory, then a Pieces result, then the lowest stable evidence ID. Duplicate checking never mutates a review decision.

### Evidence-backed groups and default representative

A duplicate check builds suggested groups only from current-version local candidate-to-candidate evidence classified `exact`, `likely`, or `possible`. Treat each evidence pair as an undirected edge. Each connected component with at least two candidates becomes one suggested group. Remote annotations and imported-ledger targets can affect a verdict but cannot become group members. The duplicate-check response returns the check ID, member candidate IDs, supporting evidence IDs, and computed default representative; it does not persist a review group.

`group_create` must supply that check ID and all supporting evidence IDs. The server rejects stale candidate versions, missing edges, remote targets, or a member set that differs from the connected component. It then persists the component as one draft group.

The default representative is the first candidate under this lexicographic ordering:

1. Metadata completeness count descending. Add one for each non-empty field among title, project scope, external links, and source-updated timestamp, for a total of 0 through 4.
2. Normalized `markdown_body` UTF-8 byte length descending.
3. Parsed `source_updated_at` instant descending. Missing or invalid timestamps use an oldest sentinel earlier than every valid timestamp.
4. Source-agent preference ascending: Codex, Claude Code, Hermes. Internal values map `codex`, `claude`, `hermes` in that order.
5. `source_key` ascending by Unicode code point after NFKC, with no case folding.
6. Canonical lowercase UUID4 candidate ID ascending.

The information criteria rank higher values first; the final three keys are stable ascending tie-breakers. Re-running a duplicate check with unchanged candidate versions produces the same order regardless of discovery or click order. `set_representative` is the explicit user override and does not change the score.

## SQLite ledger

SQLite runs in WAL mode with foreign keys enabled, a 5-second busy timeout, explicit transactions, and one migration lock during startup. Timestamps are UTC RFC 3339 strings. All ledger row IDs are canonical lowercase hyphenated UUID4 strings. `payload_hash`, `source_hash`, and `candidate_input_hash` are SHA-256 hex; `import_id` is the 26-character content identity defined above.

### Tables

- `schema_migrations(version PRIMARY KEY, applied_at)`.
- `settings(singleton_id CHECK singleton_id=1, version, ledger_instance_id, mcp_base_url, codex_enabled, claude_enabled, hermes_enabled, created_at, updated_at)`.
- `source_roots(root_id PRIMARY KEY, agent, lexical_path, resolved_path, enabled, is_default, created_at, UNIQUE(agent, resolved_path))`.
- `host_path_mappings(mapping_id PRIMARY KEY, local_root, host_root, project, created_at, UNIQUE(local_root))`.
- `scan_runs(scan_id PRIMARY KEY, state, requested_at, started_at, finished_at, settings_version, discovered_count, accepted_count, excluded_count, quarantine_count, error_count, error_detail)`.
- `source_revisions(revision_id PRIMARY KEY, predecessor_revision_id, agent, root_id, source_key, source_path, source_hash, candidate_input_hash, first_observed_at, UNIQUE(agent, root_id, source_key, source_hash, candidate_input_hash))`.
- `source_observations(observation_id PRIMARY KEY, revision_id, scan_id, source_updated_at, file_mtime_ns, raw_byte_count, observed_at, UNIQUE(revision_id, scan_id))`.
- `source_dispositions(disposition_id PRIMARY KEY, scan_id, agent, root_id, source_path, source_key, disposition, byte_count, reason, detail, created_at)`.
- `candidates(candidate_id PRIMARY KEY, revision_id, status, version, original_payload_json, current_payload_json, payload_hash, import_id, group_id, created_at, updated_at, superseded_at, UNIQUE(revision_id))`.
- `candidate_payload_snapshots(snapshot_id PRIMARY KEY, candidate_id, candidate_version, source_revision_id, state, payload_json, recorded_at, UNIQUE(candidate_id, candidate_version, state))`.
- `safety_findings(finding_id PRIMARY KEY, candidate_id, candidate_version, reason_code, severity, line_number, state, override_reason, override_at, created_at)`.
- `duplicate_checks(check_id PRIMARY KEY, candidate_id, candidate_version, coverage, started_at, finished_at, verdict, error_detail)`.
- `duplicate_evidence(evidence_id PRIMARY KEY, check_id, target_kind, target_key, target_title, target_excerpt, payload_hash, cosine, body_shingle_jaccard, title_jaccard, classification, rule_id, remote_rank)`.
- `duplicate_evidence_candidates(evidence_id, candidate_id, candidate_version, target_key, target_title, PRIMARY KEY(evidence_id, candidate_id, candidate_version))`.
- `review_groups(group_id PRIMARY KEY, title, representative_candidate_id, status, version, created_from_check_id, representative_overridden, created_at, updated_at)`.
- `review_group_evidence(group_id, evidence_id, PRIMARY KEY(group_id, evidence_id))`.
- `import_jobs(job_id PRIMARY KEY, state, requested_at, started_at, finished_at, current_ordinal, remote_search_available, duplicate_risk_acknowledged_at, duplicate_risk_ack_text_version, pause_reason, error_detail)`.
- `import_items(item_id PRIMARY KEY, job_id, ordinal, candidate_id, candidate_version, frozen_payload_json, import_id, state, attempt_count, pieces_memory_id, completed_at, error_detail, UNIQUE(job_id, ordinal), UNIQUE(job_id, candidate_id))`.
- `import_attempts(attempt_id PRIMARY KEY, item_id, attempt_number, kind, state, marker, started_at, dispatch_started_at, finished_at, marker_outcome, parent_memory_id, response_id, error_detail, UNIQUE(item_id, attempt_number))`.

Candidate status is `pending`, `approved`, `excluded`, `superseded`, or `imported`. Group status is `draft`, `approved`, `excluded`, or `imported`. Scan state is `queued`, `running`, `completed`, or `failed`. Import job state is `queued`, `running`, `paused`, `completed`, or `failed`. Import item state is `queued`, `preflight`, `remote_duplicate`, `imported`, `failed`, `ambiguous`, or `skipped`. Attempt kind is `marker_preflight`, `write`, or `marker_recheck`.

Resume mutates one paused `import_jobs` row in place. The job ID, item IDs, ordinals, frozen payloads, and Import IDs never change. Every resume action appends an `import_attempts` row; it never copies items or creates a successor job. `recheck` appends `marker_recheck`. `retry` appends a new `marker_preflight` and, if permitted, a new `write`. `skip` appends an audit attempt with state `skipped` and advances the ordinal.

Only a successful current-job SDK result or current-job ambiguous recovery with `one_parent` sets item and candidate to `imported`; it sets an approved group to imported only when the item is its representative. `remote_duplicate`, `skipped`, `failed`, and `ambiguous` leave candidate and group approval unchanged. A completed job can therefore contain imported, remote-duplicate, or skipped terminal items.

## MCP boundary

`mcp_client.py` uses the official `mcp` Python package with major version 2. The configured value is an origin/base URL; secrets in userinfo are rejected and never persisted. Connection behavior is fixed:

1. Initialize streamable HTTP at `<base>/model_context_protocol/2025-03-26/mcp`.
2. If initialization fails due to unsupported endpoint, HTTP status, or protocol negotiation before a session exists, initialize SSE at `<base>/model_context_protocol/2024-11-05/sse`.
3. Do not fall back after an authenticated session dispatches a tool call. That could duplicate a write.
4. Call `tools/list` after initialization. Record transport, server version, required-tool presence, optional-search presence, and checked time.
5. Block imports unless `create_pieces_memory` exists. Treat `annotations_full_text_search` as optional.

Tool schemas come from live discovery. The adapter requires `summary_description` and `summary`. It accepts these known optional fields only when the live schema supports them:

- `connected_client`: constant `Agent2Pieces`.
- `externalLinks`: candidate links after the HTTP(S) validation in the normalized contract.
- `project` and `files`: only when the candidate source path is inside a saved `host_path_mappings.local_root`. The longest matching local root wins. `files` contains the host root joined with the source-relative path using `/`; `project` is that mapping's non-empty project value. No mapping means both fields are omitted.

Any unknown live required field blocks imports and appears as a capability error. Agent2Pieces never guesses a value. Known optional fields absent from the live schema are omitted. The request is assembled only after a successful marker preflight or explicit no-search risk acknowledgement:

```json
{
  "summary_description": "<candidate title>",
  "summary": "<candidate markdown body>\n\n---\nImported by Agent2Pieces\nSource agent: Codex\nAgent2Pieces Import ID: abcdefghijklmnopqrstuvwxyz",
  "connected_client": "Agent2Pieces",
  "externalLinks": ["https://example.test"]
}
```

`project` and `files` are absent in this example because there is no explicit host-visible path mapping. Generated provenance is rendered at dispatch and is not part of the frozen approved content hash.

The importer owns one async mutex and processes ordinals in ascending order. When annotation search exists, every ordinal first appends a `marker_preflight` attempt and applies the exact marker outcome contract. `absent` proceeds. A remote marker result sets item `remote_duplicate`, leaves candidate approved, and advances without a write. `truncated` or `search_error` pauses. If search is unavailable, job creation requires `acknowledge_remote_duplicate_risk: true`; the UI must show that a missing, replaced, restored, or new ledger cannot prove remote absence. The acknowledgement is stored per job and never inferred from a prior job.

All request validation, live-schema validation, and argument rendering happen before call initiation. Immediately before entering `session.call_tool`, one transaction appends the `write` attempt with state `ambiguous` and non-null `dispatch_started_at`, then commits. The application calls the SDK only after that commit. A normal success changes the attempt and item to imported. Every exception, timeout, cancellation, disconnect, malformed response, or process exit after this boundary remains ambiguous unless the SDK returns explicit machine-readable proof that no request bytes were sent; only that proof permits `failed`.

An ambiguous attempt runs marker recovery when search exists. Only `one_parent` proves success and changes the current item and candidate to imported. `absent`, `multiple_parents`, `parent_unknown`, `truncated`, `search_error`, or unavailable search leaves the attempt and item ambiguous and pauses before the next ordinal. There is no timed or automatic retry.

## HTTP and UI boundary

All JSON uses UTF-8. API errors have `{ "error": { "code": "...", "message": "...", "details": {} } }`. Error messages are bounded and omit memory bodies. UUID path IDs that are malformed return 404. Missing objects return 404. Validation returns 422. Optimistic version conflicts return 409. Accepted background work returns 202.

`GET /` and fingerprinted `/assets/<name>.<digest>.<ext>` serve static packaged resources. They are not JSON API endpoints. `GET /health` is the only public service route. The internal route list below is exact.

### `GET /health`

Response `200` when the process, ledger, migrations, and packaged assets are ready; otherwise `503`:

```json
{
  "status": "ok",
  "version": "1.0.0",
  "ledger": "ok",
  "assets": "ok",
  "mcp": {
    "status": "ready|blocked|unchecked",
    "transport": "streamable-http|sse|null",
    "create_pieces_memory": true,
    "annotations_full_text_search": false
  }
}
```

### `POST /api/scans`

Request:

```json
{
  "settings_version": 3,
  "roots": [
    {"agent": "codex", "path": "/absolute/extra/root"}
  ]
}
```

`roots` is optional and adds one-scan roots without saving settings. Paths must be absolute. Response `202`: `{ "scan_id": "...", "state": "queued" }`. A running scan causes `409 scan_in_progress`.

### `GET /api/scans/{scan_id}`

Response `200`:

```json
{
  "scan_id": "...",
  "state": "queued|running|completed|failed",
  "requested_at": "...",
  "started_at": "...",
  "finished_at": null,
  "counts": {"discovered": 0, "accepted": 0, "excluded": 0, "quarantined": 0, "errors": 0},
  "by_agent": {"codex": {"discovered": 0, "accepted": 0, "excluded": 0, "quarantined": 0}},
  "disposition_by_reason": {"excluded_raw": 0, "oversize": 0},
  "error": null
}
```

### `GET /api/candidates`

Query parameters are optional: `scan_id`, `status`, `source_agent`, `project`, `security_state`, `group_id`, `duplicate_verdict`, `q`, `include_changed_source` (default false), `page` (default 1), and `page_size` (default 50, maximum 200). `project` is an exact case-sensitive match after NFKC and outer trimming. `security_state` is one of `clean|warning|blocked|overridden`; precedence is open blocking secret, open PII warning, current-version override with no open finding, then clean. Search `q` matches title, project scope, and source key only. When `include_changed_source=true`, `page_size` is capped at 10. Response `200`:

```json
{
  "items": [
    {
      "candidate_id": "...",
      "version": 2,
      "status": "pending",
      "source": {"agent": "codex", "key": "x.md", "path": "/...", "updated_at": "...", "hash": "..."},
      "project_scope": "project",
      "title": "Memory title",
      "markdown_body": "Body",
      "external_links": ["https://example.test"],
      "payload_hash": "...",
      "import_id": "abcdefghijklmnopqrstuvwxyz",
      "group": {"group_id": null, "version": null, "status": null, "representative_candidate_id": null},
      "safety": {
        "approval_checked": false,
        "findings": [{"finding_id": "...", "reason_code": "pii_email", "severity": "warn", "line": 4, "state": "open"}]
      },
      "prior_snapshot": {
        "state": "approved|imported",
        "candidate_version": 1,
        "recorded_at": "...",
        "payload": {"title": "Prior title", "markdown_body": "Prior body", "external_links": [], "project_scope": "project"}
      },
      "changed_source": {
        "available": true,
        "hunks": [{"old_start": 1, "old_count": 1, "new_start": 1, "new_count": 1, "lines": [{"op": "delete", "text": "old"}, {"op": "insert", "text": "new"}]}]
      },
      "duplicate": {"coverage": "local+pieces", "verdict": "possible", "evidence": []}
    }
  ],
  "page": 1,
  "page_size": 50,
  "total": 1,
  "facets": {"projects": [{"value": "project", "count": 1}], "security_states": {"clean": 0, "warning": 1, "blocked": 0, "overridden": 0}}
}
```

Evidence entries include `target_kind`, `target_key`, bounded `target_title` and `target_excerpt`, `classification`, `rule_id`, `cosine`, `body_shingle_jaccard`, `title_jaccard`, and optional `remote_rank`.

`prior_snapshot` is the newest immutable approved or imported snapshot for the same `(source_agent, root_id, source_key)` that predates the current revision/version; it is null when none exists. A snapshot is written on every approval and successful import before state changes. `changed_source` is null unless requested. The server computes it from prior and current LF-normalized Markdown with `difflib.SequenceMatcher(autojunk=False)`, emits three context lines around each opcode, and returns JSON line operations `context|delete|insert`, never HTML. A line containing a current or historical blocking-secret match is replaced in full with `[redacted:<reason_code>]`. The browser renders prior/current columns and operation text with `textContent`.

### `PATCH /api/candidates/{candidate_id}`

`version` and `action` are required. Edit fields are accepted only with `save`. Group actions also require `group_version` for optimistic concurrency.

```json
{
  "version": 2,
  "action": "save|approve|exclude|group_create|group_join|group_leave|set_representative|override_findings",
  "title": "Edited title",
  "markdown_body": "Edited body",
  "external_links": ["https://example.test"],
  "project_scope": "project",
  "target": "candidate|group",
  "group_id": null,
  "group_version": null,
  "duplicate_check_id": null,
  "evidence_ids": [],
  "finding_ids": [],
  "finding_acknowledged": false,
  "override_reason": null
}
```

Action semantics are exact:

- `save`: updates supplied title, body, links, or project scope; increments candidate version, invalidates finding overrides, and rescans safety.
- `approve`: with target `candidate`, requires no group and no open finding; with target `group`, requires membership, matching `group_id` and `group_version`, a representative, and no open representative finding. It sets the candidate or group approved and makes only the representative candidate approved.
- `exclude`: with target `candidate`, sets that candidate excluded. Excluding the representative returns an approved group to draft until another representative is selected. With target `group`, it sets the group excluded without erasing member-level decisions.
- `group_create`: requires a current duplicate check ID and the complete supporting evidence ID set. It creates the verified connected component and selects the scored default representative, which may differ from this endpoint's candidate ID.
- `group_join`: requires evidence linking this candidate to the named group's connected component. It recomputes the scored default only when `representative_overridden` is false.
- `group_leave`: removes this candidate. A representative cannot leave until another representative is selected or the group is excluded.
- `set_representative`: requires target `group`, membership, matching group version, and no open finding on this candidate. It selects this candidate. If the group is approved, this candidate becomes approved and the former representative returns to pending unless explicitly excluded.
- `override_findings`: requires every currently open finding ID, `finding_acknowledged: true`, and a 10 to 500 character reason. It marks those findings overridden without changing content or approval.

Response `200` is the updated candidate representation with group and finding state. Imported or superseded candidates are immutable and return 409. A stale candidate or group version returns 409 with no partial change.

### `POST /api/duplicates/check-pieces`

Request:

```json
{
  "candidate_ids": ["..."],
  "candidate_versions": {"<candidate-id>": 2}
}
```

An empty `candidate_ids` list means every pending or approved non-superseded candidate, capped at 500 per call. The operation performs local comparison in every case and optional Pieces retrieval when available. Response `200`:

```json
{
  "check_id": "...",
  "coverage": "local+pieces|local-only",
  "results": [{"candidate_id": "...", "candidate_version": 2, "verdict": "likely", "evidence": []}],
  "suggested_groups": [{"member_candidate_ids": ["...", "..."], "evidence_ids": ["..."], "default_representative_candidate_id": "..."}],
  "warnings": []
}
```

A stale requested candidate version returns 409 without storing partial results.

### `POST /api/import-jobs`

This endpoint accepts one of two tagged request shapes.

Start request:

```json
{
  "action": "start",
  "candidate_ids": ["..."],
  "candidate_versions": {"<candidate-id>": 2},
  "confirmation": {"confirmed": true, "pieces_endpoint": "http://127.0.0.1:39300/model_context_protocol/2025-03-26/mcp", "selected_write_count": 1},
  "acknowledge_remote_duplicate_risk": false
}
```

Every candidate must be approved, have no open finding, be non-superseded, not previously imported, and be either ungrouped or the representative of an approved group. The array order becomes the write order. `confirmation.confirmed` must be true, its endpoint must byte-for-byte match the effective initialized MCP endpoint exposed by settings, and its count must equal `candidate_ids.length`; mismatch returns 422 `apply_confirmation_mismatch` before a job is created. This count is the selected maximum write count before remote preflight. When annotation search is unavailable, `acknowledge_remote_duplicate_risk` must be true or the server returns 422 `remote_duplicate_risk_unacknowledged`. The UI text must state that local ledger loss or replacement can hide an earlier remote write.

Resume request:

```json
{
  "action": "resume",
  "resume_job_id": "...",
  "resolution": "recheck|retry|skip",
  "acknowledge_duplicate_write_risk": false
}
```

Resume always mutates `resume_job_id` in place. `recheck` appends a marker-recheck attempt and stays paused unless current-job success is proven. `retry` requires `acknowledge_duplicate_write_risk: true`, then appends preflight and write attempts while retaining frozen items. `skip` appends a skipped attempt and advances. Failed, skipped, or unresolved candidates remain approved. Response `202`: `{ "job_id": "<same resume_job_id>", "state": "queued|paused" }`. The ledger permits one running import job; another start or resume returns 409.

### `GET /api/import-jobs/{job_id}`

Response `200`:

```json
{
  "job_id": "...",
  "state": "queued|running|paused|completed|failed",
  "current_ordinal": 1,
  "pause_reason": null,
  "items": [
    {
      "ordinal": 1,
      "candidate_id": "...",
      "candidate_version": 2,
      "import_id": "abcdefghijklmnopqrstuvwxyz",
      "state": "queued|preflight|remote_duplicate|imported|failed|ambiguous|skipped",
      "attempts": 1,
      "pieces_memory_id": "...",
      "error": null
    }
  ]
}
```

### `GET /api/settings`

Response `200` includes the CSRF token needed by the bundled client:

```json
{
  "version": 3,
  "source_roots": [{"root_id": "...", "agent": "codex", "path": "/...", "enabled": true, "is_default": true}],
  "host_path_mappings": [{"mapping_id": "...", "local_root": "/local/project", "host_root": "C:/host/project", "project": "project-id"}],
  "mcp_base_url": "http://127.0.0.1:39300",
  "effective_mcp_base_url": "http://127.0.0.1:39300",
  "mcp_base_url_source": "saved|cli",
  "capabilities": {
    "status": "ready|blocked|unchecked",
    "transport": "streamable-http|sse|null",
    "effective_endpoint": "http://127.0.0.1:39300/model_context_protocol/2025-03-26/mcp",
    "create_pieces_memory": true,
    "annotations_full_text_search": false,
    "checked_at": "...",
    "error": null
  },
  "csrf_token": "<per-process random token>"
}
```

### `PUT /api/settings`

Request replaces saved settings and requires optimistic version:

```json
{
  "version": 3,
  "source_roots": [{"agent": "codex", "path": "/absolute/root", "enabled": true}],
  "host_path_mappings": [{"local_root": "/local/project", "host_root": "C:/host/project", "project": "project-id"}],
  "mcp_base_url": "http://127.0.0.1:39300"
}
```

The server validates roots, the Pieces URL HTTP(S) scheme and absence of URL userinfo, absolute local mapping roots, non-empty host roots, and non-empty project values before one transaction replaces settings. The Pieces host may be remote when explicitly configured; only the Agent2Pieces web listener is loopback-only. It then probes MCP capabilities without exposing credentials. A CLI Pieces URL remains the effective process value and is never persisted by this request. Response `200` is the same shape as `GET /api/settings` with an incremented version.

## Browser review flow

The bundled UI runs these observable flows through the fixed routes:

1. Start a scan with `POST /api/scans`, poll its existing status route, and refresh disposition counts and candidates.
2. Populate project and security-state filters from candidate facets and send both as `GET /api/candidates` query parameters.
3. Load `include_changed_source=true` for changed candidates and render prior/current columns plus server diff operations with `textContent`. Blocking-secret diff lines remain redacted.
4. Save edits through candidate PATCH, display refreshed findings and diff, create an evidence-backed group, show the scored default representative, and allow `set_representative` override.
5. Build the selected candidate list. Before any import POST, open a confirmation dialog that displays the exact `capabilities.effective_endpoint` string and selected candidate count. The confirm button stays disabled until both values are visible and the user checks confirmation.
6. Send those exact values in `confirmation` to `POST /api/import-jobs`, then poll the existing job route and show every ordinal state through completion or pause.

Closing or cancelling the dialog sends no import request. Any selection, candidate version, or effective endpoint change invalidates the prior confirmation and requires a new dialog confirmation.

## Security model

- Uvicorn binds to loopback only. CLI accepts `127.0.0.1`, `::1`, and `localhost` only after resolving every address as loopback.
- Host middleware accepts the actual listener host/port and normalized `localhost` or loopback equivalents. It rejects forwarding headers and DNS-rebinding hosts.
- CORS middleware is absent. State-changing requests require `application/json`, `X-CSRF-Token`, `Sec-Fetch-Site` of `same-origin` or `none`, and an `Origin` matching the listener origin. A missing Origin may use a same-origin Referer; both missing is rejected outside CLI-internal calls carrying the token.
- The server generates 32 random bytes per process, exposes the URL-safe token only in the initial page and `GET /api/settings`, and requires constant-time comparison. The token is never stored in SQLite or logs.
- Responses set `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and `Cache-Control: no-store` for JSON.
- Browser rendering uses `textContent`, form values, and DOM node creation. Candidate Markdown preview is escaped preformatted text in v1. No source HTML parser output is inserted with `innerHTML`.
- Logs never include full candidate bodies, MCP tool arguments, CSRF tokens, URL credentials, matched safety values, or finding capture groups. Safety logs use candidate ID, version, reason code, severity, line, and state only.

## CLI contract

```text
agent2pieces serve [--pieces-url URL] [--no-open] [--port PORT] [--source-root AGENT=PATH]...
agent2pieces scan [--source-root AGENT=PATH]...
agent2pieces --health-check
```

These are the complete v1 commands and option names. Unlisted commands and flags return CLI usage error exit 2.

`serve` always binds `127.0.0.1`. When `--port` is absent, it creates and listens on a socket with port `0`, reads the OS-selected port from `getsockname()`, and passes that already-bound socket to Uvicorn. This avoids a probe-then-bind race. When `--port` is present, its value must be 1 through 65535; bind failure, including an occupied port, is an operational exit 1 and never falls back to another port.

`--pieces-url` accepts an HTTP(S) Pieces base URL under the same URL validation as saved settings. It overrides the effective MCP base URL for this process and its child jobs only. It does not update SQLite or the settings version. `GET /api/settings` shows both the saved base URL and effective endpoint so Apply confirmation uses the running process value.

After Uvicorn is ready, `serve` prints one UTF-8 JSON line with `url`, selected `port`, PID, version, and ledger path. It then opens that exact URL in the default browser unless `--no-open` is present. Browser-open failure is a warning and does not stop the server.

`--source-root` is repeatable on both `serve` and `scan`. Each value splits at its first `=`; the left side must be exactly lowercase `codex`, `claude`, or `hermes`, and the right side must be a non-empty absolute path. The path is normalized and resolved with the same configured-root validation and symlink-confinement rules as a saved root, must resolve to a readable directory, and is coalesced when the same `(agent, resolved_path)` is repeated. Valid overrides are additive to enabled saved roots, exist only for that command or serving process, and never write SQLite or increment settings version. `serve` uses them for every scan requested during that process; `scan` uses them for its one synchronous scan.

`scan` uses saved source settings plus its command-scoped roots and the standard OS user-data ledger, runs synchronously, and prints one UTF-8 JSON object with total and per-agent `discovered`, `accepted`, `excluded`, and `quarantined` counts plus safe disposition reasons.

Top-level `--health-check` accepts no command and no other option. It creates a private temporary directory, opens and migrates a disposable SQLite database there, verifies the packaged migration resources and exact static assets `index.html`, `app.js`, and `app.css`, closes the database, and removes the temporary directory. It performs no source discovery or file reads outside packaged resources and the disposable directory, no MCP or other network request, no browser launch, and no socket create, bind, or listen. It writes exactly one compact UTF-8 JSON line no longer than 2,048 bytes with this shape:

```json
{"status":"ok","version":"1.0.0","checks":{"database":true,"migrations":true,"static_assets":true},"error_code":null}
```

`status` is exactly `ok` only when every check is true and otherwise `error`. `version` is the packaged application version. `checks` has exactly the three Boolean keys shown. `error_code` is null on success and otherwise the first applicable code from `database_failed`, `migrations_missing`, `migration_failed`, `static_assets_missing`, or `cleanup_failed`. On failure, completed check booleans retain their actual values; exception text and paths are omitted. Success exits 0. A valid `scan`, `serve`, or `--health-check` operational failure exits 1. CLI syntax, invalid option values, invalid saved configuration, and malformed, nonexistent, non-directory, or unreadable `--source-root` input exit 2 before scan or server startup.

The default ledger lives under the operating system user data directory, never inside a source root. Tests always supply a disposable path.

## Packaging and release workflow

PyInstaller builds one native executable per runner. Static assets and SQL migrations are explicit data files. Hidden imports required by FastAPI, Uvicorn, and MCP transports are named in `agent2pieces.spec`. Builds do not download browser assets.

`.github/workflows/release.yml` has two triggers:

- `push.tags: ["v*"]` performs a publishing build. The tag must match `vMAJOR.MINOR.PATCH` and equal the package version with the leading `v` removed or the workflow fails before building.
- `workflow_dispatch` performs the same build, smoke, checksum, and workflow-artifact upload as a dry run. It never creates or updates a GitHub Release.

The build matrix has Linux x86_64, Windows x86_64, and macOS arm64 native runners with Python 3.12. Each job installs the locked development environment and validates the release workflow. The Linux job also installs Playwright Chromium and runs lint, type checks, the fixture suite, and browser tests. Each native build job then:

1. creates a separate temporary environment containing the locked runtime and `build` dependency group, with default development groups disabled;
2. runs PyInstaller from that environment with dependency synchronization disabled;
3. inspects the executable's embedded Python modules, archive entries, and native libraries against `third_party/release_components.json`, rejecting forbidden or unmapped content before packaging;
4. generates `THIRD_PARTY_NOTICES.txt`, a conservative `THIRD_PARTY_COMPONENTS.json` inventory, and the referenced license files from the reviewed component manifest and installed distributions;
5. runs the built executable's top-level `--health-check`, starts it with `serve --no-open --source-root` fixture overrides, parses the selected-port JSON line, polls `GET /health`, loads `/`, runs `scan --source-root` against isolated fixtures, and stops the process;
6. creates a deterministic archive and checksum, validates archive contents, and uploads both as one workflow artifact.

Archive names are fixed:

- `agent2pieces-v<version>-linux-x86_64.tar.gz`
- `agent2pieces-v<version>-windows-x86_64.zip`
- `agent2pieces-v<version>-macos-arm64.tar.gz`

Each archive has one root directory named like the archive without `.tar.gz` or `.zip`. Files use this deterministic order:

1. `LICENSE` and `README.md`;
2. `THIRD_PARTY_NOTICES.txt` and `THIRD_PARTY_COMPONENTS.json`;
3. the referenced files under `third_party_licenses/`, sorted by filename;
4. `agent2pieces` or `agent2pieces.exe`.

`THIRD_PARTY_COMPONENTS.json` records the reviewed Python runtime dependency set and build components for the target platform, including applicable dependencies whose import modules PyInstaller may omit as unused. It also records artifact module roots, archive entries, binary entries, and detected native components. Each license-file entry carries its SHA-256 digest, and every listed license file is present in the archive.

Binary mode is 0755; document and license modes are 0644. Tar uid/gid are 0 with empty owner/group names and gzip mtime 0. ZIP timestamps are 1980-01-01T00:00:00 and external mode bits match the same modes. Each checksum file is `<archive-name>.sha256` and contains lowercase SHA-256 hex, two ASCII spaces, the archive basename, and LF. A rebuild from the same commit on the same runner produces the same archive bytes.

One `publish` job depends on every matrix job. For a valid tag only, it downloads all workflow artifacts, verifies all three checksums and archive manifests, creates the GitHub Release for that tag, and uploads the three archives plus three checksum files. A failed or cancelled matrix job prevents publication. The publish job is skipped for `workflow_dispatch`.

V1 does not cross-compile or build a container.

## Public acceptance evidence

Public release checks use synthetic source fixtures, isolated application state, and
a fake MCP server. Output from real agent stores, live endpoints, and local manual
acceptance runs is private operational evidence and is not tracked or distributed.

## Failure and recovery rules

- Scanner errors are isolated per source unit and become quarantine or bounded scan errors.
- A scan process crash changes any stale `running` scan to `failed` at next startup. It does not infer accepted candidates that lack committed rows.
- Candidate and settings changes use version checks inside one SQLite transaction.
- A user-requested duplicate review search failure stores `local-only` coverage and a warning; import marker preflight uses the stricter pause rules and never treats search failure as absence.
- Import recovery treats every unfinished attempt with non-null `dispatch_started_at` as ambiguous and applies the marker contract before any new write.
- A pre-initiation failure or ambiguous import outcome stops later ordinals. Only the approved in-place resume request can move the job.
- SQLite corruption, failed migration, missing packaged assets, or missing required MCP write capability is visible in health output and blocks writes.

## Performance limits

- One active scan and one active import job per process.
- Candidate list page size is capped at 200.
- Duplicate check request is capped at 500 candidates and 50 Pieces results per candidate.
- Local comparison may be O(n squared) within the bounded request. Exact-hash indexes and imported-payload indexes run first to avoid needless scoring.
- Body size is capped at 65,536 bytes before storage. API evidence excerpts are capped at 500 code points.
- SQLite writes use short transactions; MCP network waits never hold a database transaction.

## Architectural non-goals

- No React, Node runtime, CDN, external job queue, or container requirement.
- No vector database, embedding model, or LLM call for v1 duplicate scoring.
- No Pieces update/delete calls and no write batching or parallel write dispatch.
- No source-root write permission requirement.
- No API authentication scheme for remote access because remote access is rejected.
- No plugin system for additional source agents in v1.
