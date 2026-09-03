# Requirements: Agent2Pieces v1

## Status and authority

This specification records the Agent2Pieces v1 behavior and public implementation contract.

## Problem statement

Useful agent memories are spread across Codex, Claude, and Hermes files. Importing them by hand loses provenance, makes duplicate detection inconsistent, and makes uncertain writes dangerous. Agent2Pieces v1 provides a local review queue that discovers curated source material, normalizes it without changing the source files, compares it with local and Pieces memories, and writes only the entries the user approves.

## Goals

- Run as a Python 3.12 localhost web application launched by the `agent2pieces` CLI.
- Discover supported Codex, Claude, and Hermes memory units from default and user-configured roots.
- Preserve source provenance and deterministic hashes in a local SQLite ledger.
- Quarantine invalid inputs without moving, renaming, truncating, or rewriting them.
- Classify exact, likely, and possible duplicates with fixed, inspectable rules.
- Let the user review, edit, group, exclude, and approve candidates before any Pieces write.
- Connect through the official MCP Python SDK v2, discover live tools, and require `create_pieces_memory` before imports are enabled.
- Write approved memories sequentially and stop on an ambiguous timeout.
- Ship native standalone artifacts with bundled static assets and no runtime CDN dependency.

## Non-goals

- Importing raw chat transcripts, prompt logs, user profiles, file watchers, or schedules.
- Running an LLM to summarize, normalize, rank, or deduplicate candidates.
- Updating or deleting existing Pieces memories.
- Syncing changes back to Codex, Claude, or Hermes inputs.
- Remote hosting, multi-user access, authentication, or non-loopback binding.
- Background monitoring after a scan completes.
- Importing an entry without an explicit user approval recorded in the ledger.

## Source discovery and normalization

- WHEN a scan uses the Codex source adapter, THE SYSTEM SHALL inspect every regular file whose basename ends with case-sensitive `.md` under `${CODEX_HOME:-~/.codex}/memories/rollout_summaries` and configured Codex roots, SHALL record every case-sensitive `.jsonl` file as `excluded_raw`, and SHALL ignore every other regular file.
- WHEN a scan uses the Claude source adapter, THE SYSTEM SHALL inspect every regular file whose basename ends with case-sensitive `.md` under `${CLAUDE_CONFIG_DIR:-~/.claude}/projects/*/memory` and configured Claude roots, SHALL record exact basename `MEMORY.md` as `excluded_index`, SHALL record a file with flat front matter scalar `type` equal to `user` after ASCII case folding as `excluded_user`, and SHALL ignore every other regular file.
- WHEN a scan uses the Hermes source adapter, THE SYSTEM SHALL inspect `MEMORY.md` files under `${HERMES_HOME:-~/.hermes}/memories` and configured Hermes roots, splitting entries on the literal U+00A7 delimiter.
- WHEN a configured root is scanned, THE SYSTEM SHALL use read-only file handles and SHALL NOT change source bytes, names, mtime, mode, or locations; access time is outside the portable preservation contract.
- WHEN a path is a symlink, THE SYSTEM SHALL resolve it before reading and SHALL accept it only when the resolved regular file remains inside the configured root.
- WHEN a directory symlink, broken symlink, special file, or path escaping its configured root is encountered, THE SYSTEM SHALL skip it and record an inspectable quarantine reason.
- WHEN a source unit is empty after metadata removal, cannot be decoded as UTF-8, cannot be parsed according to its adapter, or exceeds 65,536 UTF-8 bytes, THE SYSTEM SHALL quarantine that unit and continue scanning other units.
- WHEN repeated Hermes delimiters create whitespace-only gaps, THE SYSTEM SHALL ignore those gaps; if the file has no non-empty entry, THE SYSTEM SHALL quarantine the file as empty.
- WHEN a supported source unit is accepted, THE SYSTEM SHALL produce these fields: `source_agent`, `source_key`, `source_path`, `project_scope`, `source_updated_at`, `title`, `markdown_body`, `external_links`, `source_hash`, `payload_hash`, and `import_id`.
- WHEN `source_hash` is computed, THE SYSTEM SHALL remove supported source metadata, normalize Unicode to NFKC, normalize line endings to LF, trim trailing ASCII spaces and tabs from every line, collapse three or more consecutive blank lines to exactly two, remove leading and trailing blank lines, and preserve every other character and line boundary.
- WHEN an accepted source unit is normalized, THE SYSTEM SHALL compute an internal `candidate_input_hash` from its canonical pre-edit title, Markdown body, validated HTTP(S) links in canonical sorted order, and project scope; THE SYSTEM SHALL exclude audit-only timestamps and ignored metadata from that hash and SHALL NOT add it to the normalized candidate API fields.
- WHEN a scan observes a title-only, project-only, external-link-only, or other candidate-driving change, THE SYSTEM SHALL create a successor source revision even if `source_hash` is unchanged.
- WHEN a scan observes only mtime, `updated_at`, CRLF/LF, Unicode-equivalent, or defined harmless Markdown whitespace changes, THE SYSTEM SHALL reuse its source revision and candidate, record the new observation, and SHALL NOT change candidate version, approval, `payload_hash`, or `import_id`.
- WHEN a source unit changes, THE SYSTEM SHALL retain the old scan and import audit records while updating or versioning the current review candidate as defined in `architecture.md`.
- WHEN a source unit matches a deterministic secret signature, THE SYSTEM SHALL create a blocking safety finding with a safe reason code and SHALL leave its approval and override controls unchecked.
- WHEN a source unit matches a deterministic email, phone, US Social Security number, or Luhn-valid payment-card signature, THE SYSTEM SHALL create a PII warning finding and SHALL leave its approval and override controls unchecked.
- WHEN a user edits candidate content, THE SYSTEM SHALL rescan all safety findings; removed findings SHALL close automatically, while remaining findings SHALL block approval until the user records an explicit finding override and reason.
- WHEN safety findings are displayed or logged, THE SYSTEM SHALL expose only reason codes, line numbers, finding state, and non-secret labels; THE SYSTEM SHALL NOT persist or log matched secret values.

## Review and duplicate detection

- WHEN a candidate is first discovered, THE SYSTEM SHALL place it in a pending review state and SHALL NOT write it to Pieces.
- WHEN the user edits a candidate title, body, links, or project scope, THE SYSTEM SHALL increment the candidate version and retain original normalized values; only title, Markdown body, and validated HTTP(S) external links participate in `payload_hash` and visible `import_id`.
- WHEN approved user content is canonicalized, THE SYSTEM SHALL exclude `source_agent`, root identity, source key, source path, project scope, `source_updated_at`, source hash, generated provenance, and marker text from `payload_hash`.
- WHEN two candidates from any agents or roots have identical canonical approved user content, THE SYSTEM SHALL give them the same 26-character lowercase base32 Import ID and the same visible marker.
- WHEN two compared payload hashes are equal, THE SYSTEM SHALL classify the pair as an exact duplicate.
- WHEN local body cosine similarity is at least 0.92 or 5-token body-shingle Jaccard similarity is at least 0.80, THE SYSTEM SHALL classify the pair as a likely duplicate.
- WHEN local body cosine similarity is at least 0.78 and below 0.92, and title-token Jaccard similarity is at least 0.50, THE SYSTEM SHALL classify the pair as a possible duplicate unless a stronger rule applies.
- WHEN no exact, likely, or possible rule matches, THE SYSTEM SHALL classify the pair as distinct.
- WHEN the Pieces server exposes `annotations_full_text_search`, THE SYSTEM SHALL use it to retrieve comparison material and SHALL apply the same local scoring rules to the returned text.
- WHEN `annotations_full_text_search` is absent, THE SYSTEM SHALL still run local ledger comparisons and SHALL clearly label Pieces duplicate coverage as unavailable.
- WHEN a duplicate result is shown, THE SYSTEM SHALL expose the matched item, classification, scores, rule, and evidence source so the user can inspect the decision.
- WHEN a duplicate check produces local candidate-to-candidate evidence, THE SYSTEM SHALL expose evidence-backed suggested groups and a deterministic default representative using the lexicographic score in `architecture.md`.
- WHEN a group is created from duplicate evidence, THE SYSTEM SHALL verify the evidence and candidate versions, include the connected evidence component, and select the deterministic default representative independent of user click order.
- WHEN the user groups candidates, THE SYSTEM SHALL keep each source candidate and its provenance visible and SHALL support explicit `approve`, `exclude`, and `set_representative` group actions through `PATCH /api/candidates/{candidate_id}`.
- WHEN an approved group's representative changes, THE SYSTEM SHALL keep the group approved, mark only the new representative approved for import, and return the old representative to pending unless it has its own explicit exclusion.
- WHEN the user excludes a candidate or group, THE SYSTEM SHALL retain the decision in SQLite and omit it from import jobs.
- WHEN concurrent edits target an old candidate or settings version, THE SYSTEM SHALL reject the stale write with a conflict response rather than overwrite the newer state.

## Pieces connection and imports

- WHEN Agent2Pieces connects to Pieces, THE SYSTEM SHALL try the configured `/model_context_protocol/2025-03-26/mcp` endpoint first and SHALL fall back to `/model_context_protocol/2024-11-05/sse` only when the preferred transport cannot complete MCP initialization.
- WHEN MCP initialization completes, THE SYSTEM SHALL call tool discovery dynamically and SHALL require `create_pieces_memory` before enabling imports.
- IF `create_pieces_memory` is absent, THEN THE SYSTEM SHALL keep scanning and review available while health and settings show imports as blocked.
- WHEN a user starts an import job, THE SYSTEM SHALL freeze candidate versions and approved user content assigned to that job; generated provenance and supported optional tool fields SHALL be rendered from that frozen record only at dispatch.
- WHILE an import job is running, THE SYSTEM SHALL invoke one `create_pieces_memory` call at a time in the approved order.
- WHEN `annotations_full_text_search` exists, THE SYSTEM SHALL run an exact visible-marker preflight before every write and SHALL NOT dispatch when the marker is already present or absence cannot be proven within the bounded search contract.
- WHEN remote marker search is unavailable, THE SYSTEM SHALL show the at-least-once duplicate risk and SHALL require a visible per-job acknowledgement before dispatch; a new, missing, replaced, or restored ledger never suppresses this acknowledgement.
- WHEN invoking `create_pieces_memory`, THE SYSTEM SHALL map the candidate title to `summary_description` and SHALL map the detailed Markdown body plus dispatch-time provenance and visible marker to `summary`.
- WHEN the discovered optional schema supports them, THE SYSTEM SHALL send `connected_client` as `Agent2Pieces` and validated HTTP(S) `externalLinks`; it SHALL send `project` and `files` only from an explicit user-configured host-visible path mapping.
- IF the live write schema has any unknown required field, THEN THE SYSTEM SHALL block imports instead of inventing a value.
- WHEN a write returns success, THE SYSTEM SHALL record the returned memory identifier when present, the attempt timestamps, and the imported candidate version.
- WHEN validation fails before call initiation, THE SYSTEM SHALL mark that item failed and pause the job without attempting later items.
- IMMEDIATELY BEFORE entering the SDK tool call, THE SYSTEM SHALL durably set `dispatch_started_at` and attempt state `ambiguous`; every failure after that commit SHALL remain ambiguous unless the SDK gives explicit proof that no request bytes were sent.
- WHEN a call-initiated write times out, is cancelled, disconnects, or returns a malformed response, THE SYSTEM SHALL search for the exact import marker when `annotations_full_text_search` is available.
- IF the marker search finds exactly one matching Pieces memory, THEN THE SYSTEM SHALL record the item as imported and continue sequentially.
- IF the marker is absent, search is unavailable, or search returns an ambiguous result, THEN THE SYSTEM SHALL mark the attempt `ambiguous`, pause the job, and SHALL NOT retry automatically.
- WHEN the user resumes a paused job, THE SYSTEM SHALL require an explicit `recheck`, `retry`, or `skip` action, SHALL resume the same job ID in place with unchanged frozen items, and SHALL append audit attempts.
- WHEN a current-job write succeeds or one current-job ambiguous write is proven by marker recovery, THE SYSTEM SHALL mark its candidate imported.
- WHEN an item is skipped, fails before initiation, or remains ambiguous, THE SYSTEM SHALL leave its candidate and group approved; only a proven current-job success changes approval to imported.
- WHEN an already imported `import_id` is selected again, THE SYSTEM SHALL block a second write. A skipped, failed, remote-duplicate, or unresolved ambiguous item does not become imported.

## Web and CLI behavior

- WHEN the user runs `agent2pieces serve`, THE SYSTEM SHALL bind loopback port 0, report the OS-selected port and full local URL, initialize the ledger, serve bundled assets, and open that URL after health readiness.
- WHEN the user runs `agent2pieces serve --port PORT`, THE SYSTEM SHALL bind that loopback port exactly and exit nonzero without fallback when it is occupied.
- WHEN the user runs `agent2pieces serve --pieces-url URL`, THE SYSTEM SHALL use the validated URL as a process-only Pieces base URL override without mutating saved settings.
- WHEN the user runs `agent2pieces serve --no-open`, THE SYSTEM SHALL suppress browser launch without changing server behavior.
- WHEN the user repeats `--source-root AGENT=PATH` on `agent2pieces serve` or `agent2pieces scan`, THE SYSTEM SHALL add each absolute root to that command's configured roots using the same confinement validation, accept only exact agents `codex`, `claude`, and `hermes`, and SHALL NOT mutate saved settings.
- WHEN the user runs `agent2pieces scan`, THE SYSTEM SHALL execute a synchronous scan against saved settings plus command-scoped roots and print a machine-readable per-agent disposition summary.
- WHEN the user runs top-level `agent2pieces --health-check`, THE SYSTEM SHALL migrate a disposable SQLite database, verify packaged migrations and static assets, emit one bounded JSON line, perform no source scan, network request, browser launch, or socket operation, and exit 0 only when every check passes.
- WHEN CLI syntax or a supplied source root is invalid or unreadable, THE SYSTEM SHALL exit 2; WHEN a scan, serve, or standalone health operation fails after valid inputs, THE SYSTEM SHALL exit 1.
- WHEN the browser opens the application, THE SYSTEM SHALL provide project and security-state filters, escaped editing, evidence-backed groups, a side-by-side changed-source diff, settings, and sequential import progress.
- WHEN the user selects Apply, THE SYSTEM SHALL show the exact effective Pieces endpoint and selected write count, require explicit confirmation, and include matching confirmation fields in the existing import-job request.
- WHEN static assets are served, THE SYSTEM SHALL load scripts and styles from the packaged application only.
- WHEN candidate content is displayed, THE SYSTEM SHALL treat source and edited content as untrusted text and SHALL escape it without executing embedded HTML, script, event handlers, or remote resources.

## Security and privacy

- WHILE the server is running, THE SYSTEM SHALL bind only to `127.0.0.1`, `::1`, or a resolved loopback `localhost` address.
- WHEN an HTTP request has an unapproved `Host`, cross-origin `Origin` or `Referer`, or cross-site fetch metadata, THE SYSTEM SHALL reject it.
- WHEN a request can change state, THE SYSTEM SHALL require JSON content type, a matching same-origin request, and the current CSRF token.
- WHEN HTTP responses include candidate data, THE SYSTEM SHALL set a restrictive Content Security Policy, disable MIME sniffing, deny framing, and avoid permissive CORS headers.
- WHEN logging scanner, API, or MCP activity, THE SYSTEM SHALL omit full memory bodies and secrets; identifiers, paths, byte counts, hashes, states, and bounded error text are allowed.

## Public and internal routes

- WHEN a client calls `GET /health`, THE SYSTEM SHALL return packaged service readiness without exposing candidate bodies or secret settings.
- WHEN the bundled browser client needs application data, THE SYSTEM SHALL use only the nine approved `/api` routes documented in `architecture.md`.
- WHEN a request targets any unapproved `/api` path or method, THE SYSTEM SHALL return 404 or 405 and SHALL NOT provide a hidden alternate mutation path.
- WHERE document and asset requests are needed for the browser UI, THE SYSTEM SHALL serve only `/` and fingerprinted bundled asset paths; these are static resources, not additional JSON APIs.

## Automated acceptance checks

- A fixture scan proves the exact per-adapter inclusion, exclusion, ignore, symlink, and quarantine rules without changing fixture bytes, mtime, or mode.
- A normalization test proves the 11 required fields, internal candidate-input hash, source-hash boundaries for NFKC, CRLF/LF, trailing spaces/tabs, blank-line collapse, preserved interior whitespace, content-only payload hash keys, stable 26-character Import ID, cross-agent marker equality, observation-only changes, update versioning, and scan idempotency. It separately proves title-only, project-only, and external-link-only successors and `updated_at`-only and mtime-only observations.
- A safety matrix proves every secret-block and PII-warning reason code, default unchecked controls, edit-to-clear behavior, explicit override audit, and absence of matched values from logs and API errors.
- A duplicate matrix proves every threshold boundary, rule precedence, short-body behavior, optional Pieces search behavior, and inspectable evidence.
- API tests prove the exact method/path surface, optimistic conflicts, pagination bounds, CSRF, same-origin checks, Host checks, escaped content, and no CORS opt-in.
- Browser tests using Playwright prove scan polling, project/security filtering, editing, safe side-by-side diff, deterministic and overridden representatives, Apply confirmation endpoint/count, required confirmation rejection, and import progress.
- MCP contract tests prove preferred transport, fallback, dynamic tool discovery, live write field mapping, optional field allowlisting, unknown-required-field blocking, strict sequential calls, remote preflight before every write, durable call-initiation ambiguity, annotation grouping, truncation behavior, in-place resume, and no automatic retry.
- CLI tests prove the exact `serve`, `scan`, and top-level `--health-check` surface, repeatable command-scoped roots for both commands, root validation and non-persistence, OS-selected port reporting, occupied explicit-port failure, process-only Pieces override, browser-open suppression, bounded health JSON, and exit codes 0, 1, and 2.
- Packaging tests prove a clean native artifact starts without Python, passes standalone `--health-check` without source, network, browser, or socket activity, serves bundled assets, answers `GET /health`, and completes a fixture scan through CLI root overrides.
- A tag matching `v*` SHALL build versioned native archives on Linux, macOS, and Windows, run native smoke tests, emit SHA-256 files, upload workflow artifacts, and publish all archives and checksums to one GitHub Release only after every build succeeds.
- A manual workflow dispatch SHALL run the same dry build and artifact upload without publishing a GitHub Release.

## Manual acceptance checks

1. Start the packaged artifact on a machine with no project checkout, open the printed localhost URL, and confirm no CDN or remote asset request occurs.
2. Configure one read-only fixture root for each agent plus an escaping symlink. Run a scan and compare source bytes, mtime, and mode before and after.
3. Review a candidate containing HTML and script text. Confirm the UI shows text and executes nothing.
4. Filter by project and security state, inspect a side-by-side changed-source diff, create a suggested duplicate group, confirm its scored default representative is stable, override it, approve the group, and exclude it again.
5. Connect to a Pieces instance with `create_pieces_memory` and optional `annotations_full_text_search`. Confirm the UI shows the discovered capabilities.
6. Select two approved entries, confirm the Apply dialog shows the exact effective Pieces endpoint and count 2, then import and confirm each remote marker preflight precedes its write, calls occur in order, the visible marker and dispatch-time provenance are in `summary`, supported optional fields follow the allowlist, and source files remain unchanged.
7. Force an ambiguous timeout. Confirm durable ambiguity exists before SDK entry, marker lookup groups annotations by parent memory, the same job pauses when success cannot be proven, candidates remain approved, and no automatic retry occurs.
8. Use a fresh ledger while remote search is unavailable. Confirm import remains disabled until the user checks the visible duplicate-risk acknowledgement.
9. Restart the application with the same ledger. Confirm scans, edits, findings, groups, duplicate evidence, frozen job state, appended attempts, and imported identifiers remain visible.

## Dependencies and constraints

- Runtime: Python 3.12.
- Web: FastAPI and Uvicorn.
- MCP: official MCP Python SDK major version 2.
- Persistence: Python SQLite with explicit migrations.
- Packaging: PyInstaller native standalone artifacts.
- Browser acceptance: Playwright for Python with Chromium installed only in development and release-build environments.
- Browser assets: bundled HTML, CSS, and JavaScript with no Node runtime requirement in the shipped product.
- Network scope: localhost HTTP plus the user-configured Pieces MCP endpoint.

## Out of scope for v1

Potential follow-up work includes remote deployment, multiple users, source watchers, scheduled scans, LLM-assisted merge suggestions, raw transcript ingestion, and Pieces update or delete operations. None of these belong in the v1 task graph.
