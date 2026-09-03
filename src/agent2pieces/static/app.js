"use strict";

const state = {
  csrfToken: "",
  settings: null,
  candidates: [],
  current: null,
  selectedIds: new Set(),
  duplicateCheck: null,
  confirmation: null,
  importJobId: null,
  messageTimer: null,
  candidateLoadGeneration: 0,
  scanPollGeneration: 0,
  importPollGeneration: 0,
  pendingMutation: null,
};

const supportedActions = [
  "save",
  "approve",
  "exclude",
  "group_create",
  "group_join",
  "group_leave",
  "set_representative",
  "override_findings",
];
const remoteRiskNotice = "Local ledger loss or replacement can hide an earlier remote write.";
const importAttentionStates = ["remote_duplicate", "ambiguous"];
const resumeResolutions = {
  recheck: {resolution: "recheck"},
  retry: {resolution: "retry"},
  skip: {resolution: "skip"},
};

function element(id) {
  return document.getElementById(id);
}

function clear(node) {
  node.replaceChildren();
}

function make(tag, className, content) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (content !== undefined && content !== null) {
    node.textContent = String(content);
  }
  return node;
}

function announce(message) {
  const node = element("live-message");
  node.textContent = message;
  node.classList.add("visible");
  if (state.messageTimer !== null) {
    window.clearTimeout(state.messageTimer);
  }
  state.messageTimer = window.setTimeout(function hideMessage() {
    node.classList.remove("visible");
  }, 4000);
}

function errorMessage(payload, fallback) {
  if (payload && payload.error && payload.error.message) {
    return payload.error.message;
  }
  return fallback;
}

async function api(path, options) {
  const requestOptions = options || {};
  const target = new URL(path, window.location.origin);
  const method = requestOptions.method || "GET";
  const headers = {"Content-Type": "application/json"};
  if (method !== "GET") {
    headers["X-CSRF-Token"] = state.csrfToken;
  }
  const response = await window.fetch(target, {
    method: method,
    headers: headers,
    body: requestOptions.body === undefined ? undefined : JSON.stringify(requestOptions.body),
    credentials: "same-origin",
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(errorMessage(payload, "The local request failed."));
  }
  return payload;
}

function invalidateConfirmation() {
  state.confirmation = null;
  element("apply-confirmation").textContent = "Candidate selection changed. Review again before Apply.";
  element("apply-confirm").disabled = true;
}

function invalidateDuplicateCheck() {
  state.duplicateCheck = null;
  clear(element("duplicate-warnings"));
}

function renderDuplicateWarnings() {
  const container = element("duplicate-warnings");
  clear(container);
  if (!state.duplicateCheck || (state.duplicateCheck.warnings || []).length === 0) {
    return;
  }
  container.append(make("strong", "", "Remote duplicate coverage warnings"));
  const list = make("ul", "");
  for (const warning of state.duplicateCheck.warnings) {
    list.append(make("li", "", warning));
  }
  container.append(list);
}

function candidateById(candidateId) {
  return state.candidates.find(function findCandidate(candidate) {
    return candidate.candidate_id === candidateId;
  });
}

function securityState(candidate) {
  const findings = candidate.safety.findings || [];
  if (findings.some(function hasBlock(item) { return item.state === "open" && item.severity === "block"; })) {
    return "blocked";
  }
  if (findings.some(function hasWarning(item) { return item.state === "open" && item.severity === "warn"; })) {
    return "warning";
  }
  if (findings.some(function hasOverride(item) { return item.state === "overridden"; })) {
    return "overridden";
  }
  return "clean";
}

function renderConnection() {
  const capabilities = state.settings.capabilities;
  element("connection-status").textContent = capabilities.status === "ready" ? "Pieces ready" : "Pieces blocked";
  element("create-memory-capability").textContent = capabilities.create_pieces_memory ? "create_pieces_memory available" : "create_pieces_memory missing";
  element("search-capability").textContent = capabilities.annotations_full_text_search ? "annotations_full_text_search available" : "Remote duplicate search unavailable";
  const source = state.settings.mcp_base_url_source === "cli" ? "CLI override" : "saved setting at startup";
  const restart = state.settings.mcp_base_url_restart_required ? " Saved endpoint changes apply after restart." : "";
  element("pieces-effective-endpoint").textContent = "Effective endpoint: " + state.settings.effective_mcp_base_url + " (" + source + ")." + restart;
}

function renderSettings() {
  const roots = element("source-root-list");
  clear(roots);
  for (const root of state.settings.source_roots) {
    const row = make("div", "source-root");
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.checked = root.enabled;
    enabled.setAttribute("aria-label", "Enable " + root.path);
    enabled.addEventListener("change", function rootEnabled() {
      root.enabled = enabled.checked;
      invalidateConfirmation();
    });
    const remove = make("button", "secondary", "Remove");
    remove.type = "button";
    remove.addEventListener("click", function removeRoot() {
      state.settings.source_roots = state.settings.source_roots.filter(function keepRoot(item) { return item !== root; });
      invalidateConfirmation();
      renderSettings();
    });
    row.append(enabled, make("strong", "", root.agent), make("span", "candidate-meta", root.path), remove);
    roots.append(row);
  }
  element("pieces-base-url").value = state.settings.mcp_base_url;
}

function addSourceRoot() {
  const path = element("new-root-path").value.trim();
  if (!path) {
    announce("Enter an absolute source root path.");
    return;
  }
  state.settings.source_roots.push({
    agent: element("new-root-agent").value,
    path: path,
    enabled: true,
    is_default: false,
  });
  element("new-root-path").value = "";
  invalidateConfirmation();
  renderSettings();
}

async function saveSettings() {
  const roots = state.settings.source_roots.map(function requestRoot(root) {
    return {agent: root.agent, path: root.path, enabled: root.enabled};
  });
  const mappings = state.settings.host_path_mappings.map(function requestMapping(mapping) {
    return {local_root: mapping.local_root, host_root: mapping.host_root, project: mapping.project};
  });
  state.settings = await api("/api/settings", {
    method: "PUT",
    body: {
      version: state.settings.version,
      source_roots: roots,
      host_path_mappings: mappings,
      mcp_base_url: element("pieces-base-url").value,
    },
  });
  state.csrfToken = state.settings.csrf_token;
  invalidateConfirmation();
  renderConnection();
  renderSettings();
  announce(state.settings.mcp_base_url_restart_required ? "Settings saved. Restart Agent2Pieces to use the saved Pieces endpoint." : "Settings saved.");
}

function renderProjectOptions(facets) {
  const select = element("project-filter");
  const currentValue = select.value;
  clear(select);
  const all = make("option", "", "All projects");
  all.value = "";
  select.append(all);
  for (const project of facets.projects || []) {
    const option = make("option", "", project.value + " (" + project.count + ")");
    option.value = project.value;
    select.append(option);
  }
  select.value = currentValue;
}

function renderCandidateList() {
  const list = element("candidate-list");
  clear(list);
  element("candidate-total").textContent = String(state.candidates.length);
  for (const candidate of state.candidates) {
    const row = make("div", "candidate-row");
    const selectLabel = make("label", "check-row");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selectedIds.has(candidate.candidate_id);
    checkbox.disabled = candidate.status !== "approved";
    checkbox.setAttribute("aria-label", "Select " + candidate.title + " for Apply");
    checkbox.addEventListener("change", function selectCandidate() {
      if (checkbox.checked) {
        state.selectedIds.add(candidate.candidate_id);
      } else {
        state.selectedIds.delete(candidate.candidate_id);
      }
      invalidateConfirmation();
    });
    selectLabel.append(checkbox, make("span", "candidate-meta", "Include in Apply"));
    const button = make("button", "candidate-card");
    button.type = "button";
    button.setAttribute("aria-label", candidate.title);
    button.setAttribute("aria-current", state.current && state.current.candidate_id === candidate.candidate_id ? "true" : "false");
    button.append(
      make("strong", "", candidate.title),
      make("span", "candidate-meta", candidate.source.agent + " | " + candidate.project_scope),
      make("span", "candidate-meta", candidate.status + " | " + candidate.duplicate.verdict + " | " + securityState(candidate))
    );
    button.addEventListener("click", function openCandidate() {
      state.current = candidate;
      renderCandidateList();
      renderCurrentCandidate();
    });
    row.append(selectLabel, button);
    list.append(row);
  }
}

function renderFindings(candidate) {
  const list = element("finding-list");
  clear(list);
  const findings = candidate.safety.findings || [];
  if (findings.length === 0) {
    list.append(make("p", "status-line", "No secret or PII findings."));
  }
  for (const finding of findings) {
    const row = make("label", "finding check-row");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = finding.finding_id;
    checkbox.disabled = finding.state !== "open";
    row.append(checkbox, make("span", "", finding.severity + ": " + finding.reason_code + " on line " + finding.line + " (" + finding.state + ")"));
    list.append(row);
  }
  element("finding-acknowledgement").checked = false;
  element("finding-reason").value = "";
}

function renderDiff(candidate) {
  const prior = candidate.prior_snapshot;
  const changed = candidate.changed_source;
  element("prior-body").textContent = prior ? prior.payload.markdown_body : "No prior approved snapshot.";
  element("current-body").textContent = candidate.markdown_body;
  const hunkList = element("diff-hunks");
  clear(hunkList);
  if (!changed || !changed.available || changed.hunks.length === 0) {
    hunkList.append(make("p", "status-line", "No changed-source hunks."));
    return;
  }
  for (const hunk of changed.hunks) {
    const block = make("pre", "diff-hunk");
    for (const line of hunk.lines) {
      const prefix = line.op === "insert" ? "+ " : line.op === "delete" ? "- " : "  ";
      const lineNode = make("span", "diff-line-" + line.op, prefix + line.text + "\n");
      block.append(lineNode);
    }
    hunkList.append(block);
  }
}

function renderDuplicateEvidence(candidate) {
  element("duplicate-verdict").textContent = candidate.duplicate.verdict;
  const hasGroup = Boolean(candidate.group.group_id);
  element("duplicate-group-status").textContent = hasGroup
    ? "Group status: " + candidate.group.status + " | version " + candidate.group.version
    : "No duplicate group.";
  element("approve-candidate").textContent = hasGroup ? "Approve group" : "Approve";
  element("exclude-candidate").textContent = hasGroup ? "Exclude group" : "Exclude";
  const list = element("duplicate-evidence-list");
  clear(list);
  const evidence = candidate.duplicate.evidence || [];
  if (evidence.length === 0) {
    list.append(make("p", "status-line", "No duplicate evidence for this candidate."));
  }
  for (const item of evidence) {
    const score = item.cosine === null || item.cosine === undefined ? "" : " cosine " + Number(item.cosine).toFixed(3);
    list.append(make("div", "evidence", item.classification + " | " + item.target_kind + " | " + item.target_key + score));
  }
  const representative = element("group-representative");
  clear(representative);
  const group = state.duplicateCheck && (state.duplicateCheck.suggested_groups || []).find(function matchesGroup(item) {
    return item.member_candidate_ids.includes(candidate.candidate_id);
  });
  const memberIds = group ? group.member_candidate_ids : [candidate.candidate_id];
  for (const candidateId of memberIds) {
    const member = candidateById(candidateId);
    const option = make("option", "", member ? member.title : candidateId);
    option.value = candidateId;
    representative.append(option);
  }
  const defaultId = candidate.group.representative_candidate_id
    || (group ? group.default_representative_candidate_id : null);
  if (defaultId) {
    representative.value = defaultId;
  }
}

function renderCurrentCandidate() {
  const candidate = state.current;
  if (!candidate) {
    return;
  }
  element("candidate-state").textContent = candidate.status + " | version " + candidate.version;
  element("candidate-title").value = candidate.title;
  element("candidate-body").value = candidate.markdown_body;
  element("candidate-links").value = (candidate.external_links || []).join("\n");
  element("candidate-project").value = candidate.project_scope;
  element("preview-title").textContent = candidate.title;
  element("preview-body").textContent = candidate.markdown_body;
  renderFindings(candidate);
  renderDiff(candidate);
  renderDuplicateEvidence(candidate);
}

function filtersQuery(page) {
  const query = new URLSearchParams();
  const sourceAgent = element("agent-filter").value;
  const project = element("project-filter").value;
  const duplicateVerdict = element("duplicate-filter").value;
  const security = element("security-filter").value;
  if (sourceAgent) { query.set("source_agent", sourceAgent); }
  if (project) { query.set("project", project); }
  if (duplicateVerdict) { query.set("duplicate_verdict", duplicateVerdict); }
  if (security) { query.set("security_state", security); }
  query.set("include_changed_source", "true");
  query.set("page_size", "10");
  query.set("page", String(page));
  return query.toString();
}

async function loadCandidates(preferredId) {
  state.candidateLoadGeneration += 1;
  const generation = state.candidateLoadGeneration;
  const firstPage = await api("/api/candidates?" + filtersQuery(1));
  const candidates = firstPage.items.slice();
  const pageCount = Math.ceil(firstPage.total / firstPage.page_size);
  for (let page = 2; page <= pageCount; page += 1) {
    const response = await api("/api/candidates?" + filtersQuery(page));
    if (generation !== state.candidateLoadGeneration) {
      return;
    }
    candidates.push(...response.items);
  }
  if (generation !== state.candidateLoadGeneration) {
    return;
  }
  state.candidates = candidates;
  const availableIds = new Set(state.candidates.map(function candidateId(item) { return item.candidate_id; }));
  for (const candidateId of Array.from(state.selectedIds)) {
    if (!availableIds.has(candidateId)) {
      state.selectedIds.delete(candidateId);
    }
  }
  renderProjectOptions(firstPage.facets);
  const nextId = preferredId || (state.current && state.current.candidate_id);
  state.current = candidateById(nextId) || state.candidates[0] || null;
  renderCandidateList();
  renderCurrentCandidate();
}

async function loadSettings() {
  state.settings = await api("/api/settings");
  state.csrfToken = state.settings.csrf_token;
  renderConnection();
  renderSettings();
}

async function mutateCandidate(action, extra) {
  const precedingMutation = state.pendingMutation;
  const operation = (async function applyMutation() {
    if (precedingMutation) {
      await precedingMutation;
    }
    if (!state.current) {
      return;
    }
    const candidateId = state.current.candidate_id;
    const body = Object.assign({version: state.current.version, action: action}, extra || {});
    const updated = await api("/api/candidates/" + candidateId, {method: "PATCH", body: body});
    invalidateConfirmation();
    invalidateDuplicateCheck();
    await loadCandidates(updated.candidate_id);
  }());
  state.pendingMutation = operation;
  try {
    await operation;
  } finally {
    if (state.pendingMutation === operation) {
      state.pendingMutation = null;
    }
  }
}

async function saveCandidate(event) {
  event.preventDefault();
  if (!state.current) {
    return;
  }
  await mutateCandidate("save", {
    title: element("candidate-title").value,
    markdown_body: element("candidate-body").value,
    external_links: element("candidate-links").value.split("\n").map(function trimLink(value) { return value.trim(); }).filter(Boolean),
    project_scope: element("candidate-project").value,
  });
  announce("Candidate edits saved.");
}

async function simpleCandidateAction(action) {
  if (state.pendingMutation) {
    await state.pendingMutation;
  }
  if (!state.current) {
    return;
  }
  const group = state.current.group;
  if (group.group_id) {
    await mutateCandidate(action, {
      target: "group",
      group_id: group.group_id,
      group_version: group.version,
    });
    announce("Group " + action + " completed.");
    return;
  }
  await mutateCandidate(action, {target: "candidate"});
  announce("Candidate " + action + " completed.");
}

async function overrideFindings() {
  if (!state.current) {
    return;
  }
  const findingIds = Array.from(element("finding-list").querySelectorAll("input:checked")).map(function findingId(input) { return input.value; });
  await mutateCandidate("override_findings", {
    finding_ids: findingIds,
    finding_acknowledged: element("finding-acknowledgement").checked,
    override_reason: element("finding-reason").value,
  });
  announce("Safety finding override recorded.");
}

async function checkDuplicates() {
  const candidateIds = state.candidates.map(function candidateId(candidate) { return candidate.candidate_id; });
  const candidateVersions = {};
  for (const candidate of state.candidates) {
    candidateVersions[candidate.candidate_id] = candidate.version;
  }
  state.duplicateCheck = await api("/api/duplicates/check-pieces", {
    method: "POST",
    body: {candidate_ids: candidateIds, candidate_versions: candidateVersions},
  });
  invalidateConfirmation();
  await loadCandidates(state.current && state.current.candidate_id);
  renderDuplicateWarnings();
  const warningCount = (state.duplicateCheck.warnings || []).length;
  announce("Duplicate check completed with " + state.duplicateCheck.coverage + " coverage and " + warningCount + " warnings.");
}

function suggestedGroupForCurrent() {
  if (!state.current || !state.duplicateCheck) {
    return null;
  }
  return (state.duplicateCheck.suggested_groups || []).find(function matches(item) {
    return item.member_candidate_ids.includes(state.current.candidate_id);
  }) || null;
}

async function createGroup() {
  const group = suggestedGroupForCurrent();
  if (!group || !state.duplicateCheck) {
    announce("Run duplicate checks before creating a group.");
    return;
  }
  await mutateCandidate("group_create", {
    duplicate_check_id: state.duplicateCheck.check_id,
    evidence_ids: group.evidence_ids,
  });
  announce("Duplicate group created.");
}

async function leaveGroup() {
  if (!state.current || !state.current.group.group_id) {
    return;
  }
  await mutateCandidate("group_leave", {
    target: "group",
    group_id: state.current.group.group_id,
    group_version: state.current.group.version,
  });
  announce("Candidate left the duplicate group.");
}

async function setRepresentative() {
  const candidateId = element("group-representative").value;
  if (state.pendingMutation) {
    await state.pendingMutation;
  }
  const candidate = candidateById(candidateId);
  if (!candidate || !candidate.group.group_id) {
    announce("Create or select a persisted group first.");
    return;
  }
  state.current = candidate;
  await mutateCandidate("set_representative", {
    target: "group",
    group_id: candidate.group.group_id,
    group_version: candidate.group.version,
  });
  announce("Group representative changed.");
}

async function pollScan(scanId, generation) {
  if (generation !== state.scanPollGeneration) {
    return;
  }
  const scan = await api("/api/scans/" + scanId);
  if (generation !== state.scanPollGeneration) {
    return;
  }
  const counts = scan.counts;
  element("scan-counts").textContent = scan.state + ": " + counts.accepted + " accepted, " + counts.excluded + " excluded, " + counts.quarantined + " quarantined, " + counts.errors + " errors";
  if (scan.state === "queued" || scan.state === "running") {
    window.setTimeout(function continueScan() {
      pollScan(scanId, generation).catch(handleError);
    }, 300);
    return;
  }
  invalidateConfirmation();
  await loadCandidates();
}

async function startScan() {
  const scan = await api("/api/scans", {
    method: "POST",
    body: {settings_version: state.settings.version, roots: []},
  });
  state.scanPollGeneration += 1;
  const generation = state.scanPollGeneration;
  element("scan-counts").textContent = "Scan queued.";
  await pollScan(scan.scan_id, generation);
}

function selectedApprovedCandidates() {
  return state.candidates.filter(function selected(candidate) {
    return state.selectedIds.has(candidate.candidate_id) && candidate.status === "approved";
  });
}

function refreshApplyConfirmState() {
  const requiresRiskAcknowledgement = !element("remote-risk-row").hidden;
  const riskAcknowledged = element("remote-risk-acknowledgement").checked;
  element("apply-confirm").disabled = !state.confirmation
    || state.confirmation.selected_write_count === 0
    || (requiresRiskAcknowledgement && !riskAcknowledged);
}

async function openApplyDialog() {
  const candidates = selectedApprovedCandidates();
  const payloadHashes = {};
  const candidateVersions = {};
  for (const candidate of candidates) {
    payloadHashes[candidate.candidate_id] = candidate.payload_hash;
    candidateVersions[candidate.candidate_id] = candidate.version;
  }
  if (candidates.length === 0) {
    state.confirmation = null;
    element("apply-endpoint").textContent = state.settings.capabilities.effective_endpoint;
    element("apply-count").textContent = "0";
    element("apply-context-hash").textContent = "Not created";
    clear(element("apply-confirmation"));
    element("apply-confirmation").append(make("p", "status-line", "Select at least one approved candidate before Apply."));
    element("apply-confirm").disabled = true;
    element("apply-dialog").showModal();
    return;
  }
  const preview = await api("/api/import-jobs", {
    method: "POST",
    body: {
      action: "preview",
      candidate_ids: candidates.map(function candidateId(candidate) { return candidate.candidate_id; }),
      candidate_versions: candidateVersions,
      payload_hashes: payloadHashes,
    },
  });
  state.confirmation = {
    candidate_ids: candidates.map(function candidateId(candidate) { return candidate.candidate_id; }),
    payload_hashes: payloadHashes,
    candidate_versions: candidateVersions,
    selected_write_count: preview.selected_write_count,
    pieces_endpoint: preview.pieces_endpoint,
    context_hash: preview.context_hash,
    items: preview.items,
  };
  element("apply-endpoint").textContent = state.confirmation.pieces_endpoint;
  element("apply-count").textContent = String(state.confirmation.selected_write_count);
  element("apply-context-hash").textContent = state.confirmation.context_hash;
  const confirmation = element("apply-confirmation");
  clear(confirmation);
  confirmation.append(make("p", "status-line", "The listed versions, payload hashes, and write context are frozen for this request."));
  const list = make("ul", "confirmation-list");
  for (const item of preview.items) {
    const project = item.project === null ? "none" : item.project;
    const files = item.files.length === 0 ? "none" : item.files.join(", ");
    list.append(make("li", "", item.title + " | " + item.payload_hash + " | project: " + project + " | files: " + files));
  }
  confirmation.append(list);
  element("remote-risk-row").hidden = state.settings.capabilities.annotations_full_text_search;
  element("remote-risk-row").title = remoteRiskNotice;
  element("remote-risk-acknowledgement").checked = false;
  refreshApplyConfirmState();
  element("apply-dialog").showModal();
}

async function submitImport() {
  if (!state.confirmation) {
    announce("Review Apply again because the confirmation is stale.");
    return;
  }
  const frozen = state.confirmation;
  const result = await api("/api/import-jobs", {
    method: "POST",
    body: {
      action: "start",
      candidate_ids: frozen.candidate_ids,
      candidate_versions: frozen.candidate_versions,
      payload_hashes: frozen.payload_hashes,
      confirmation: {
        confirmed: true,
        pieces_endpoint: frozen.pieces_endpoint,
        selected_write_count: frozen.selected_write_count,
        context_hash: frozen.context_hash,
      },
      acknowledge_remote_duplicate_risk: element("remote-risk-acknowledgement").checked,
    },
  });
  element("apply-dialog").close();
  state.importJobId = result.job_id;
  state.confirmation = null;
  state.importPollGeneration += 1;
  await pollImportJob(state.importPollGeneration);
}

function renderImportJob(job) {
  element("import-progress").textContent = job.state + (job.pause_reason ? " | " + job.pause_reason : "");
  const list = element("import-items");
  clear(list);
  for (const item of job.items) {
    const row = make("div", "import-item");
    const latest = item.attempt_telemetry.length ? item.attempt_telemetry[item.attempt_telemetry.length - 1] : null;
    const source = item.source ? item.source.agent + ":" + item.source.source_key : item.candidate_id;
    const telemetry = latest ? [latest.tool, latest.endpoint, latest.duration_ms === null ? "pending" : latest.duration_ms + " ms", latest.retry_state, latest.recovery_state].join(" | ") : item.attempts + " attempts";
    if (importAttentionStates.includes(item.state)) {
      row.setAttribute("data-needs-attention", "true");
    }
    row.append(
      make("span", "", String(item.ordinal)),
      make("span", "", source + " | " + item.state),
      make("span", "", item.error ? item.error + " | " + telemetry : item.pieces_memory_id || telemetry)
    );
    list.append(row);
  }
  const canResume = job.state === "paused";
  element("resume-recheck").disabled = !canResume;
  element("resume-retry").disabled = !canResume;
  element("resume-skip").disabled = !canResume;
}

async function pollImportJob(generation, resumeWaits) {
  if (!state.importJobId || generation !== state.importPollGeneration) {
    return;
  }
  const job = await api("/api/import-jobs/" + state.importJobId);
  if (generation !== state.importPollGeneration) {
    return;
  }
  renderImportJob(job);
  const waitsRemaining = resumeWaits || 0;
  if (job.state === "queued" || job.state === "running" || (job.state === "paused" && waitsRemaining > 0)) {
    window.setTimeout(function continueImport() {
      pollImportJob(generation, job.state === "paused" ? waitsRemaining - 1 : 0).catch(handleError);
    }, 400);
    return;
  }
  await loadCandidates(state.current && state.current.candidate_id);
}

async function resumeImport(resolution) {
  if (!state.importJobId) {
    return;
  }
  await api("/api/import-jobs", {
    method: "POST",
    body: {
      action: "resume",
      resume_job_id: state.importJobId,
      resolution: resolution,
      acknowledge_duplicate_write_risk: element("duplicate-write-risk-acknowledgement").checked,
    },
  });
  state.importPollGeneration += 1;
  await pollImportJob(state.importPollGeneration, 10);
}

function bind() {
  element("add-root").addEventListener("click", addSourceRoot);
  element("save-settings").addEventListener("click", function persistSettings() { saveSettings().catch(handleError); });
  element("candidate-form").addEventListener("submit", function handleSave(event) { saveCandidate(event).catch(handleError); });
  element("approve-candidate").addEventListener("click", function approve() { simpleCandidateAction("approve").catch(handleError); });
  element("exclude-candidate").addEventListener("click", function exclude() { simpleCandidateAction("exclude").catch(handleError); });
  element("override-findings").addEventListener("click", function override() { overrideFindings().catch(handleError); });
  element("duplicate-check").addEventListener("click", function check() { checkDuplicates().catch(handleError); });
  element("create-group").addEventListener("click", function groupCreate() { createGroup().catch(handleError); });
  element("leave-group").addEventListener("click", function groupLeave() { leaveGroup().catch(handleError); });
  element("set-representative").addEventListener("click", function representative() { setRepresentative().catch(handleError); });
  element("scan-button").addEventListener("click", function scan() { startScan().catch(handleError); });
  element("rescan-button").addEventListener("click", function rescan() { startScan().catch(handleError); });
  for (const id of ["agent-filter", "project-filter", "duplicate-filter", "security-filter"]) {
    element(id).addEventListener("change", function filterChanged() {
      invalidateConfirmation();
      loadCandidates().catch(handleError);
    });
  }
  for (const id of ["candidate-title", "candidate-body", "candidate-links", "candidate-project"]) {
    element(id).addEventListener("input", function editorChanged() {
      invalidateConfirmation();
      element("preview-title").textContent = element("candidate-title").value;
      element("preview-body").textContent = element("candidate-body").value;
    });
  }
  element("apply-button").addEventListener("click", function reviewApply() { openApplyDialog().catch(handleError); });
  element("apply-cancel").addEventListener("click", function cancelApply() { element("apply-dialog").close(); });
  element("remote-risk-acknowledgement").addEventListener("change", refreshApplyConfirmState);
  element("resume-recheck").addEventListener("click", function recheck() { resumeImport(resumeResolutions.recheck.resolution).catch(handleError); });
  element("resume-retry").addEventListener("click", function retry() { resumeImport(resumeResolutions.retry.resolution).catch(handleError); });
  element("resume-skip").addEventListener("click", function skip() { resumeImport(resumeResolutions.skip.resolution).catch(handleError); });
  document.getElementById("apply-confirm").addEventListener("click", submitImport);
}

function handleError(error) {
  announce(error instanceof Error ? error.message : "The operation failed.");
}

async function start() {
  if (supportedActions.length !== 8) {
    throw new Error("The browser action contract is incomplete.");
  }
  bind();
  await loadSettings();
  await loadCandidates();
}

start().catch(handleError);
