#!/usr/bin/env node
import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile, rm } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { validateMcpInventory } from "../debug_ui/profiles.mjs";
import {
  assertWebPolicy,
  evaluateWebPolicy,
  normalizeWebPolicy,
} from "../debug_ui/web_policy.mjs";

const EVALS_DIR = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_FIXTURE = path.join(EVALS_DIR, "legal_questions_unreviewed.json");
const TERMINAL_STATUSES = new Set(["completed", "failed", "interrupted", "cancelled"]);
const COMPARISONS = {
  tools: ["lean", "web_only"],
  local_sources: ["local_first", "web_only"],
  graph: ["lean", "local_text_only"],
  instructions: ["web_only", "web_baseline"],
  combined: ["lean", "web_baseline"],
};

const TOOL_ITEM_TYPES = new Set([
  "mcpToolCall", "dynamicToolCall", "commandExecution", "fileChange",
  "webSearch", "collabToolCall", "imageView", "browserToolCall",
]);
const RECHTSGRAPH_PROFILES = new Set(["lean", "local_first", "local_text_only"]);
const FAILED_TOOL_STATUSES = new Set(["failed", "error", "cancelled", "canceled"]);
const RETRIEVAL_RESULT_FIELDS = Object.freeze({
  get_structural_unit: { count: "passage_count", items: "passages" },
  get_context: { count: "count", items: "passages" },
  search_corpus: { count: "count", items: "results" },
});

function usageMetrics(run = {}) {
  const total = run.usage?.total || {};
  const asNumberOrNull = (value) => (typeof value === "number" && Number.isFinite(value) ? value : null);
  const inputTokens = asNumberOrNull(total.inputTokens);
  const cachedInputTokens = asNumberOrNull(total.cachedInputTokens);
  return {
    totalTokens: asNumberOrNull(total.totalTokens),
    inputTokens,
    cachedInputTokens,
    uncachedInputTokens: inputTokens === null || cachedInputTokens === null ? null : Math.max(0, inputTokens - cachedInputTokens),
    cacheWriteInputTokens: asNumberOrNull(total.cacheWriteInputTokens),
    outputTokens: asNumberOrNull(total.outputTokens),
    reasoningOutputTokens: asNumberOrNull(total.reasoningOutputTokens),
  };
}

function eventItem(event) {
  return event?.message?.params?.item || event?.params?.item || null;
}

function toolResultPayload(item) {
  const structured = item?.result?.structuredContent?.result || item?.result?.structuredContent;
  if (structured && typeof structured === "object") return structured;
  for (const part of item?.result?.content || []) {
    if (typeof part?.text !== "string") return false;
    try {
      const parsed = JSON.parse(part.text);
      const payload = parsed?.result || parsed;
      if (payload && typeof payload === "object") return payload;
    } catch {
      // A non-JSON text result does not expose structured telemetry.
    }
  }
  return null;
}

function observableToolError(item) {
  const payload = toolResultPayload(item);
  return FAILED_TOOL_STATUSES.has(String(item?.status || "").toLowerCase())
    || Boolean(item?.error)
    || item?.result?.isError === true
    || item?.result?.ok === false
    || payload?.ok === false;
}

function explicitEmptyEvidence(item) {
  const fields = RETRIEVAL_RESULT_FIELDS[item?.tool];
  const payload = fields ? toolResultPayload(item) : null;
  return payload?.ok === true
    && payload[fields.count] === 0
    && Array.isArray(payload[fields.items])
    && payload[fields.items].length === 0;
}

function explicitTruncatedEvidence(item) {
  return Boolean(RETRIEVAL_RESULT_FIELDS[item?.tool]) && toolResultPayload(item)?.truncated === true;
}

function toolUsageMetrics(run) {
  const hasToolItems = Array.isArray(run.toolItems);
  const hasEvents = Array.isArray(run.events);
  if (!hasToolItems && !hasEvents) {
    return {
      toolTraceAvailable: false,
      toolCallCount: null,
      webToolCallCount: null,
      rechtsgraphToolCallCount: null,
      rechtsgraphStatusCallCount: null,
      rechtsgraphEvidenceCallCount: null,
      rechtsgraphGraphCallCount: null,
      rechtsgraphTextCallCount: null,
      rechtsgraphEmptyEvidenceCallCount: null,
      rechtsgraphTruncatedEvidenceCallCount: null,
      toolErrorCount: null,
      rechtsgraphToolErrorCount: null,
      rechtsgraphUsageClassification: "trace-unavailable",
    };
  }

  // Started and completed events describe the same call. Merge by item ID,
  // then prefer final toolItems because they carry the complete result/status.
  const byId = new Map();
  let unidentifiedToolItemCount = 0;
  const add = (item) => {
    if (!TOOL_ITEM_TYPES.has(item?.type)) return;
    if (!item.id) {
      unidentifiedToolItemCount += 1;
      return;
    }
    byId.set(item.id, { ...(byId.get(item.id) || {}), ...item });
  };
  for (const event of run.events || []) add(eventItem(event));
  for (const item of run.toolItems || []) add(item);

  if (unidentifiedToolItemCount > 0) {
    return {
      toolTraceAvailable: true,
      toolTraceHasUnidentifiedItems: true,
      unidentifiedToolItemCount,
      toolCallCount: null,
      webToolCallCount: null,
      rechtsgraphToolCallCount: null,
      rechtsgraphStatusCallCount: null,
      rechtsgraphEvidenceCallCount: null,
      rechtsgraphGraphCallCount: null,
      rechtsgraphTextCallCount: null,
      rechtsgraphEmptyEvidenceCallCount: null,
      rechtsgraphTruncatedEvidenceCallCount: null,
      toolErrorCount: null,
      rechtsgraphToolErrorCount: null,
      rechtsgraphUsageClassification: "trace-incomplete",
    };
  }

  const items = [...byId.values()];
  const webItems = items.filter((item) => item.type === "webSearch");
  const rechtsgraphItems = items.filter((item) => item.type === "mcpToolCall" && item.server === "rechtsgraph");
  const statusItems = rechtsgraphItems.filter((item) => item.tool === "get_service_status");
  const evidenceItems = rechtsgraphItems.filter((item) => item.tool !== "get_service_status");
  const graphItems = rechtsgraphItems.filter((item) => item.tool === "explore_references");
  const textItems = evidenceItems.filter((item) => item.tool !== "explore_references");
  let classification = "not-used";
  if (evidenceItems.length > 0) classification = "evidence-requested";
  else if (statusItems.length > 0) classification = "status-only";
  return {
    toolTraceAvailable: true,
    toolTraceHasUnidentifiedItems: unidentifiedToolItemCount > 0,
    unidentifiedToolItemCount,
    toolCallCount: items.length,
    webToolCallCount: webItems.length,
    rechtsgraphToolCallCount: rechtsgraphItems.length,
    rechtsgraphStatusCallCount: statusItems.length,
    rechtsgraphEvidenceCallCount: evidenceItems.length,
    rechtsgraphGraphCallCount: graphItems.length,
    rechtsgraphTextCallCount: textItems.length,
    rechtsgraphEmptyEvidenceCallCount: evidenceItems.filter(explicitEmptyEvidence).length,
    rechtsgraphTruncatedEvidenceCallCount: evidenceItems.filter(explicitTruncatedEvidence).length,
    toolErrorCount: items.filter(observableToolError).length,
    rechtsgraphToolErrorCount: rechtsgraphItems.filter(observableToolError).length,
    rechtsgraphUsageClassification: classification,
  };
}

export function runMetrics(run = {}) {
  const started = Date.parse(run.startedAt || "");
  const completed = Date.parse(run.completedAt || "");
  const firstDelta = Date.parse(run.firstAgentDeltaAt || "");
  const webPolicy = evaluateWebPolicy(run, run.webPolicy || "optional");
  return {
    status: run.status || "unknown",
    terminalFailure: ["failed", "interrupted", "cancelled"].includes(run.status)
      ? true : run.status === "completed" ? false : null,
    durationMs: Number.isFinite(started) && Number.isFinite(completed) ? Math.max(0, completed - started) : null,
    timeToFirstAgentDeltaMs: Number.isFinite(started) && Number.isFinite(firstDelta) ? Math.max(0, firstDelta - started) : null,
    ...toolUsageMetrics(run),
    ...usageMetrics(run),
    webPolicy: webPolicy.policy,
    webPolicyCompliant: webPolicy.compliant,
    webPolicyViolation: webPolicy.reason,
    webEvidenceSourceCount: webPolicy.sourceCount,
  };
}

function isoForPath() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}

function fixtureDigest(fixture) {
  return createHash("sha256").update(JSON.stringify(fixture)).digest("hex");
}

function safeCaseId(value) {
  const id = String(value || "");
  if (!/^[a-z0-9][a-z0-9_-]{0,80}$/i.test(id)) throw new Error(`Ungültige Fall-ID: ${id}`);
  return id;
}

async function writeJson(file, value) {
  await mkdir(path.dirname(file), { recursive: true });
  const temporary = `${file}.${process.pid}.${randomUUID()}.tmp`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  await rename(temporary, file);
}

async function fetchJson(url, { method = "GET", body, timeoutMs }) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    const text = await response.text();
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
    }
    return {
      status: response.status,
      ok: response.ok,
      body: parsed,
      rawText: text,
      receivedAt: new Date().toISOString(),
    };
  } finally {
    clearTimeout(timer);
  }
}

function normalizeUrl(value, optionName) {
  let url;
  try {
    url = new URL(String(value));
  } catch {
    throw new Error(`${optionName} muss eine vollständige URL sein`);
  }
  if (!/^https?:$/.test(url.protocol)) throw new Error(`${optionName} muss http oder https verwenden`);
  return url.href.replace(/\/$/, "");
}

export async function loadFixture(file = DEFAULT_FIXTURE) {
  const fixture = JSON.parse(await readFile(file, "utf8"));
  if (!Array.isArray(fixture.cases) || fixture.cases.length === 0) throw new Error("Fixture benötigt mindestens einen Fall");
  const seen = new Set();
  for (const entry of fixture.cases) {
    const id = safeCaseId(entry.id);
    if (seen.has(id)) throw new Error(`Doppelte Fall-ID: ${id}`);
    seen.add(id);
    if (!String(entry.prompt || "").trim()) throw new Error(`Fall ${id} enthält keine Frage`);
    if (!String(entry.referenceStatus || "").trim()) {
      throw new Error(`Fall ${id} braucht einen referenceStatus`);
    }
    normalizeWebPolicy(entry.webPolicy, "optional");
  }
  return fixture;
}

export async function validateServer({ url, profile, timeoutMs }) {
  const response = await fetchJson(`${url}/api/config`, { timeoutMs });
  if (!response.ok || !response.body) throw new Error(`${url} /api/config antwortete nicht erfolgreich (${response.status})`);
  if (response.body.profile?.name !== profile) {
    throw new Error(`${url} meldet Profil ${response.body.profile?.name || "unbekannt"}; erwartet ${profile}`);
  }
  if (response.body.appServerReady !== true) throw new Error(`${url} App Server ist nicht bereit`);
  validateMcpInventory(profile, response.body.mcpInventory);
  if (response.body.providerCapabilities?.checked !== true) {
    throw new Error(`${url} konnte die Websuche-Fähigkeit des Modellproviders nicht verifizieren`);
  }
  if (response.body.providerCapabilities.webSearch !== true) {
    throw new Error(`${url} meldet für den Modellprovider webSearch=false; web_search=\"live\" allein genügt nicht`);
  }
  return response.body;
}

export function validateToolTrace(run, profile, webPolicy = run?.webPolicy || "optional") {
  if (!Array.isArray(run.events)) throw new Error("Run has no auditable event trace");
  const forbidden = new Set(["commandExecution", "fileChange", "dynamicToolCall", "collabToolCall", "imageView", "browserToolCall"]);
  for (const event of run.events) {
    const item = event.message?.params?.item;
    if (!item) continue;
    if (forbidden.has(item.type) || (item.type === "mcpToolCall" && (!RECHTSGRAPH_PROFILES.has(profile) || item.server !== "rechtsgraph"))) {
      throw new Error(`Contaminated ${profile} run: unexpected ${item.type} ${item.server || ""}`);
    }
    if (profile === "local_text_only" && item.type === "mcpToolCall" && item.tool === "explore_references") {
      throw new Error("Contaminated local_text_only run: explore_references was used");
    }
  }
  return assertWebPolicy(run, webPolicy);
}

function requestFor(caseEntry, options) {
  return {
    prompt: caseEntry.prompt,
    model: options.model,
    effort: options.effort,
    summary: "none",
    networkAccess: true,
    webPolicy: normalizeWebPolicy(caseEntry.webPolicy, "optional"),
  };
}

function attemptKey(caseId, side) {
  return `${caseId}:${side}`;
}

function attemptRawDirectory(directory, caseId, side) {
  return path.join(directory, "raw", safeCaseId(caseId), side);
}

function selectCases(fixture, requestedCaseIds) {
  if (!requestedCaseIds) return fixture.cases;
  if (!Array.isArray(requestedCaseIds) || requestedCaseIds.length === 0) {
    throw new Error("caseIds must be a non-empty array when supplied");
  }
  const requested = new Set(requestedCaseIds.map(safeCaseId));
  if (requested.size !== requestedCaseIds.length) throw new Error("caseIds must not contain duplicates");
  const selected = fixture.cases.filter((entry) => requested.has(entry.id));
  const missing = [...requested].filter((id) => !selected.some((entry) => entry.id === id));
  if (missing.length) throw new Error(`Unknown caseIds: ${missing.join(", ")}`);
  return selected;
}

function dialogueShouldContinue(text, dialogue) {
  const normalized = String(text || "").toLocaleLowerCase("de-DE");
  const finalHeading = String(dialogue.finalHeading || "EINSCHÄTZUNG").toLocaleLowerCase("de-DE");
  if (normalized.includes(finalHeading)) return false;
  return normalized.includes("?") || normalized.includes("rückfragen");
}

function dialogueReply(assistantText, dialogue, answeredFactIds, round) {
  const normalized = String(assistantText || "").toLocaleLowerCase("de-DE");
  const matched = [];
  for (const fact of dialogue.facts || []) {
    if (answeredFactIds.has(fact.id)) continue;
    if ((fact.match || []).some((pattern) => normalized.includes(String(pattern).toLocaleLowerCase("de-DE")))) {
      matched.push(fact);
    }
  }
  const lines = matched.map((fact) => `- ${fact.answer}`);
  if (lines.length === 0) {
    lines.push("- Zu den neu angesprochenen Punkten liegen mir keine weiteren Informationen vor.");
  }
  const unmatchedNote = "Falls eine Frage damit nicht beantwortet ist, ist mir die betreffende Information nicht bekannt.";
  const instruction = round < Number(dialogue.maxClarificationRounds || 1)
    ? `Gib jetzt entweder unter ${dialogue.finalHeading || "EINSCHÄTZUNG"} deine abschließende Antwort oder stelle unter ${dialogue.followUpHeading || "LETZTE RÜCKFRAGEN"} nur noch wirklich entscheidende offene Fragen.`
    : `Gib jetzt unter ${dialogue.finalHeading || "EINSCHÄTZUNG"} die abschließende Antwort. Es gibt keine weitere Rückfragerunde.`;
  return {
    matchedFactIds: matched.map((fact) => fact.id),
    prompt: `Hier sind die verfügbaren Antworten:\n${lines.join("\n")}\n\n${unmatchedNote}\n\n${instruction}`,
  };
}

async function completeDialogue({
  caseEntry, server, attempt, rootFinalResponse, directory, normalized, persist,
}) {
  const config = caseEntry.dialogue;
  const rawDirectory = attemptRawDirectory(directory, caseEntry.id, server.side);
  if (!attempt.dialogue) {
    attempt.dialogue = {
      mode: config.mode,
      maxClarificationRounds: Number(config.maxClarificationRounds || 1),
      answeredFactIds: [],
      turns: [],
    };
  }
  const state = attempt.dialogue;
  if (state.mode !== config.mode || state.maxClarificationRounds !== Number(config.maxClarificationRounds || 1)) {
    throw new Error(`Resume-Mismatch: dialogue configuration changed for ${caseEntry.id}`);
  }
  if (!state.turns[0]) {
    state.turns[0] = {
      turnNumber: 1,
      prompt: caseEntry.prompt,
      remoteRunId: attempt.remoteRunId,
      state: "finished",
      final: { status: rootFinalResponse.body?.status, metrics: runMetrics(rootFinalResponse.body) },
      polls: attempt.polls,
    };
  }
  await writeJson(path.join(rawDirectory, "turn-01-response.json"), rootFinalResponse);
  let parentResponse = rootFinalResponse;
  let parentRunId = attempt.remoteRunId;
  const answered = new Set(state.answeredFactIds || []);

  for (let round = 1; round <= state.maxClarificationRounds; round += 1) {
    if (parentResponse.body?.status !== "completed"
        || !dialogueShouldContinue(parentResponse.body?.finalText, config)) break;
    const turnIndex = round;
    let turn = state.turns[turnIndex];
    if (turn && !turn.remoteRunId) {
      throw new Error(`Sicheres Resume nicht möglich für ${caseEntry.id} Dialog-Turn ${turnIndex + 1}: kein remoteRunId`);
    }
    if (!turn) {
      const reply = dialogueReply(parentResponse.body?.finalText, config, answered, round);
      turn = {
        turnNumber: turnIndex + 1,
        prompt: reply.prompt,
        matchedFactIds: reply.matchedFactIds,
        parentRunId,
        state: "start_intent_recorded",
        polls: [],
      };
      state.turns[turnIndex] = turn;
      await persist();
      const start = await fetchJson(`${server.url}/api/runs/${encodeURIComponent(parentRunId)}/replies`, {
        method: "POST",
        body: { prompt: turn.prompt },
        timeoutMs: normalized.startTimeoutMs,
      });
      await writeJson(path.join(rawDirectory, `turn-${String(turnIndex + 1).padStart(2, "0")}-start-response.json`), start);
      if (start.status !== 202 || !start.body?.id) {
        turn.state = "uncertain_start";
        turn.error = { at: new Date().toISOString(), message: `Continuation start answered with ${start.status}` };
        await persist();
        throw new Error(`Dialog-Turn ${turnIndex + 1} startete nicht eindeutig (${start.status})`);
      }
      turn.remoteRunId = start.body.id;
      turn.state = "polling";
      for (const factId of turn.matchedFactIds) answered.add(factId);
      state.answeredFactIds = [...answered];
      await persist();
    } else {
      for (const factId of turn.matchedFactIds || []) answered.add(factId);
    }

    const response = await pollRun({
      server,
      remoteRunId: turn.remoteRunId,
      timeoutMs: normalized.timeoutMs,
      pollMs: normalized.pollMs,
      onPoll(poll) { turn.polls.push(poll); },
    });
    await writeJson(path.join(rawDirectory, `turn-${String(turnIndex + 1).padStart(2, "0")}-response.json`), response);
    validateToolTrace(response.body, server.profile, caseEntry.webPolicy);
    turn.state = "finished";
    turn.final = { status: response.body.status, metrics: runMetrics(response.body) };
    await persist();
    parentResponse = response;
    parentRunId = turn.remoteRunId;
  }

  const transcript = {
    schemaVersion: 1,
    taskId: caseEntry.id,
    mode: state.mode,
    maxClarificationRounds: state.maxClarificationRounds,
    answeredFactIds: [...answered],
    turns: state.turns.map((turn, index) => ({
      turnNumber: turn.turnNumber,
      user: turn.prompt,
      assistant: index === 0
        ? rootFinalResponse.body?.conversation?.turns?.[0]?.finalText || rootFinalResponse.body?.finalText || ""
        : parentResponse.body?.conversation?.turns?.find((entry) => entry.id === turn.remoteRunId)?.finalText
          || null,
      remoteRunId: turn.remoteRunId,
      matchedFactIds: turn.matchedFactIds || [],
    })),
  };
  // Conversation snapshots are authoritative and avoid associating the last
  // response with every continuation when a server exposes the full history.
  const conversationTurns = parentResponse.body?.conversation?.turns;
  if (Array.isArray(conversationTurns) && conversationTurns.length) {
    transcript.turns = conversationTurns.map((turn, index) => ({
      turnNumber: index + 1,
      user: turn.prompt || state.turns[index]?.prompt || "",
      assistant: turn.finalText || "",
      remoteRunId: turn.runId || turn.id || state.turns[index]?.remoteRunId || null,
      matchedFactIds: state.turns[index]?.matchedFactIds || [],
    }));
  } else {
    for (let index = 0; index < state.turns.length; index += 1) {
      const responsePath = path.join(rawDirectory, `turn-${String(index + 1).padStart(2, "0")}-response.json`);
      const response = JSON.parse(await readFile(responsePath, "utf8"));
      transcript.turns[index].assistant = response.body?.finalText || "";
    }
  }
  await writeJson(path.join(rawDirectory, "dialogue-transcript.json"), transcript);
  await writeJson(path.join(rawDirectory, "final-response.json"), parentResponse);
  state.completedAt = new Date().toISOString();
  state.finalTurnCount = transcript.turns.length;
  state.answeredFactIds = [...answered];
  return { finalResponse: parentResponse, transcript };
}

async function pollRun({ server, remoteRunId, timeoutMs, pollMs, onPoll }) {
  const deadline = Date.now() + timeoutMs;
  while (true) {
    const response = await fetchJson(`${server.url}/api/runs/${encodeURIComponent(remoteRunId)}`, { timeoutMs: Math.min(15_000, timeoutMs) });
    onPoll({ at: response.receivedAt, httpStatus: response.status, runStatus: response.body?.status || null });
    if (!response.ok || !response.body) throw new Error(`Abruf von Lauf ${remoteRunId} fehlgeschlagen (${response.status})`);
    if (TERMINAL_STATUSES.has(response.body.status)) return response;
    if (Date.now() >= deadline) {
      const error = new Error(`Polling-Timeout nach ${timeoutMs} ms für Lauf ${remoteRunId}`);
      error.code = "POLL_TIMEOUT";
      error.remoteRunId = remoteRunId;
      throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, pollMs));
  }
}

function makePlan(cases, left, right) {
  return cases.flatMap((caseEntry, index) => {
    const sides = index % 2 === 0 ? [left, right] : [right, left];
    return sides.map((server, position) => ({ caseEntry, server, position }));
  });
}

async function readManifest(directory) {
  return JSON.parse(await readFile(path.join(directory, "manifest.json"), "utf8"));
}

function assertResumeCompatible(manifest, options, fixture) {
  const expected = {
    fixtureSha256: fixtureDigest(fixture),
    timeoutMs: options.timeoutMs,
    model: options.model,
    effort: options.effort,
    selectedCaseIds: options.selectedCases.map((entry) => entry.id),
  };
  for (const [key, value] of Object.entries(expected)) {
    if (JSON.stringify(manifest[key]) !== JSON.stringify(value)) throw new Error(`Resume-Mismatch bei ${key}`);
  }
  for (const side of ["left", "right"]) {
    if (manifest[side]?.url !== options[side].url || manifest[side]?.profile !== options[side].profile) {
      throw new Error(`Resume-Mismatch bei ${side}`);
    }
  }
}

export async function runPairedEvaluation(options) {
  const fixture = await loadFixture(options.fixturePath || DEFAULT_FIXTURE);
  const selectedCases = selectCases(fixture, options.caseIds);
  const comparison = options.comparison || "tools";
  if (!COMPARISONS[comparison]) throw new Error("Comparison must be tools, local_sources, graph, instructions, or combined");
  const [leftProfile, rightProfile] = COMPARISONS[comparison];
  if (options.execute !== true) {
    const dialogueRuns = selectedCases.filter((entry) => entry.dialogue).length * 2;
    return { dryRun: true, comparison, model: options.model || "gpt-5.6-sol", effort: options.effort || "low",
      plannedAgentRuns: selectedCases.length * 2,
      plannedModelTurns: { minimum: selectedCases.length * 2 + dialogueRuns, maximum: selectedCases.length * 2 + dialogueRuns * 2 },
      selectedCaseIds: selectedCases.map((entry) => entry.id),
      plan: makePlan(selectedCases, { side: "left", profile: leftProfile }, { side: "right", profile: rightProfile })
        .map(({ caseEntry, server }) => ({ caseId: caseEntry.id, profile: server.profile,
          webPolicy: normalizeWebPolicy(caseEntry.webPolicy, "optional"), dialogue: Boolean(caseEntry.dialogue), prompt: caseEntry.prompt })) };
  }
  const left = { url: normalizeUrl(options.leftUrl, "--left-url"), profile: options.leftProfile || leftProfile, side: "left" };
  const right = { url: normalizeUrl(options.rightUrl, "--right-url"), profile: options.rightProfile || rightProfile, side: "right" };
  if (left.profile !== leftProfile || right.profile !== rightProfile) throw new Error(`Comparison ${comparison} requires ${leftProfile} and ${rightProfile}`);
  if (left.url === right.url) throw new Error("Die zwei Server-URLs müssen verschieden sein");
  const normalized = {
    ...options,
    left,
    right,
    model: options.model || "gpt-5.6-sol",
    effort: options.effort || "low",
    timeoutMs: Number(options.timeoutMs || 300_000),
    pollMs: Number(options.pollMs || 2_000),
    requestTimeoutMs: Number(options.requestTimeoutMs || 15_000),
    startTimeoutMs: Number(options.startTimeoutMs || 180_000),
    selectedCases,
  };
  if (!Number.isFinite(normalized.timeoutMs) || normalized.timeoutMs < 1_000) throw new Error("--timeout-ms muss mindestens 1000 sein");
  if (!Number.isFinite(normalized.pollMs) || normalized.pollMs < 100) throw new Error("--poll-ms muss mindestens 100 sein");
  if (normalized.timeoutMs > 600_000 || normalized.pollMs > normalized.timeoutMs) throw new Error("Timeout must be at most 600000 ms and polling interval must not exceed it");
  for (const key of ["requestTimeoutMs", "startTimeoutMs"]) {
    if (!Number.isFinite(normalized[key]) || normalized[key] < 100 || normalized[key] > 600_000) throw new Error(`Invalid ${key}`);
  }

  const [leftConfig, rightConfig] = await Promise.all([
    validateServer({ url: left.url, profile: left.profile, timeoutMs: normalized.requestTimeoutMs }),
    validateServer({ url: right.url, profile: right.profile, timeoutMs: normalized.requestTimeoutMs }),
  ]);
  if (!leftConfig.instructionHashes?.base || !rightConfig.instructionHashes?.base) throw new Error("Instruction hashes missing");
  if (["tools", "local_sources", "graph"].includes(comparison) && leftConfig.instructionHashes.base !== rightConfig.instructionHashes.base) {
    throw new Error("Paired profiles must share the same base instructions");
  }
  if (!["tools", "local_sources", "graph"].includes(comparison) && leftConfig.instructionHashes.base === rightConfig.instructionHashes.base) throw new Error("Baseline must omit the legal research guide");
  if (comparison === "instructions" && JSON.stringify(leftConfig.appServerArgs) !== JSON.stringify(rightConfig.appServerArgs)) throw new Error("Instruction comparison requires identical tool configuration");
  for (const config of [leftConfig, rightConfig]) {
    const model = config.models?.find((entry) => entry.id === normalized.model || entry.model === normalized.model);
    if (!model) throw new Error(`Requested model unavailable: ${normalized.model}`);
    if (!config.appServerArgs?.includes('web_search="live"')) throw new Error("Live web search not configured");
  }

  let directory = options.resumeDirectory ? path.resolve(options.resumeDirectory) : null;
  if (!directory) directory = path.resolve(options.outputDirectory || path.join(EVALS_DIR, "results", `paired-${isoForPath()}`));
  await mkdir(directory, { recursive: true });
  const lockDirectory = path.join(directory, ".runner-lock");
  await mkdir(lockDirectory);
  try {
    let manifest;
    if (options.resumeDirectory) {
      manifest = await readManifest(directory);
      assertResumeCompatible(manifest, normalized, fixture);
      if ((manifest.comparison || "tools") !== comparison) throw new Error("Resume-Mismatch: comparison changed");
      for (const [side, config] of [["left", leftConfig], ["right", rightConfig]]) {
        if (JSON.stringify(manifest[side]?.config?.instructionHashes) !== JSON.stringify(config.instructionHashes)
            || JSON.stringify(manifest[side]?.config?.appServerArgs) !== JSON.stringify(config.appServerArgs)) {
          throw new Error(`Resume-Mismatch: ${side} instructions or configuration changed`);
        }
      }
    } else {
      try {
        await readManifest(directory);
        throw new Error("Output already contains an evaluation; use --resume-directory");
      } catch (error) {
        if (error.code !== "ENOENT") throw error;
      }
      manifest = {
        comparison,
        schemaVersion: 1,
        evaluationId: randomUUID(),
        createdAt: new Date().toISOString(),
        fixturePath: path.resolve(options.fixturePath || DEFAULT_FIXTURE),
        fixtureSha256: fixtureDigest(fixture),
        referenceStatus: fixture.referenceStatus || "unreviewed",
        selectedCaseIds: selectedCases.map((entry) => entry.id),
        left: { url: left.url, profile: left.profile, config: leftConfig },
        right: { url: right.url, profile: right.profile, config: rightConfig },
        model: normalized.model,
        effort: normalized.effort,
        timeoutMs: normalized.timeoutMs,
        pollMs: normalized.pollMs,
        plan: makePlan(selectedCases, left, right).map(({ caseEntry, server, position }) => ({ caseId: caseEntry.id, side: server.side, profile: server.profile,
          webPolicy: normalizeWebPolicy(caseEntry.webPolicy, "optional"), position, dialogue: Boolean(caseEntry.dialogue) })),
        attempts: {},
      };
      await writeJson(path.join(directory, "manifest.json"), manifest);
    }

    const persist = () => writeJson(path.join(directory, "manifest.json"), manifest);
    for (const { caseEntry, server, position } of makePlan(selectedCases, left, right)) {
      const key = attemptKey(caseEntry.id, server.side);
      let attempt = manifest.attempts[key];
      if (attempt?.final?.status && TERMINAL_STATUSES.has(attempt.final.status)) continue;
      if (attempt && !attempt.remoteRunId) {
        throw new Error(`Sicheres Resume nicht möglich für ${key}: Startstatus ${attempt.state}; kein weiterer POST wird gesendet.`);
      }
      if (!attempt) {
        // Persist intent before POST.  A crash or timeout after this point is
        // intentionally treated as uncertain instead of risking a second model run.
        attempt = manifest.attempts[key] = {
          caseId: caseEntry.id,
          side: server.side,
          profile: server.profile,
          webPolicy: normalizeWebPolicy(caseEntry.webPolicy, "optional"),
          orderPosition: position,
          prompt: caseEntry.prompt,
          referenceStatus: caseEntry.referenceStatus,
          state: "start_intent_recorded",
          startedAt: new Date().toISOString(),
          polls: [],
        };
        await persist();
        try {
          const request = requestFor(caseEntry, normalized);
          const response = await fetchJson(`${server.url}/api/runs`, {
            method: "POST", body: request, timeoutMs: normalized.startTimeoutMs,
          });
          await writeJson(path.join(attemptRawDirectory(directory, caseEntry.id, server.side), "start-response.json"), response);
          if (response.status !== 202 || !response.body?.id) throw new Error(`Start antwortete mit ${response.status}`);
          attempt.remoteRunId = response.body.id;
          attempt.startResponse = { httpStatus: response.status, receivedAt: response.receivedAt, runStatus: response.body.status };
          attempt.state = "polling";
          await persist();
        } catch (error) {
          attempt.state = "uncertain_start";
          attempt.error = { at: new Date().toISOString(), message: error.message };
          await persist();
          throw new Error(`${key}: Start unklar; aus Sicherheitsgründen nicht erneut gestartet (${error.message})`);
        }
      }
      let observedFinalResponse = null;
      try {
        const finalResponse = observedFinalResponse = await pollRun({
          server,
          remoteRunId: attempt.remoteRunId,
          timeoutMs: normalized.timeoutMs,
          pollMs: normalized.pollMs,
          onPoll(poll) { attempt.polls.push(poll); },
        });
        await writeJson(path.join(attemptRawDirectory(directory, caseEntry.id, server.side), "final-response.json"), finalResponse);
        validateToolTrace(finalResponse.body, server.profile, caseEntry.webPolicy);
        let completedResponse = finalResponse;
        if (caseEntry.dialogue && finalResponse.body.status === "completed") {
          const completedDialogue = await completeDialogue({
            caseEntry, server, attempt, rootFinalResponse: finalResponse,
            directory, normalized, persist,
          });
          completedResponse = completedDialogue.finalResponse;
        }
        attempt.state = "finished";
        attempt.completedAt = new Date().toISOString();
        attempt.final = { id: completedResponse.body.id, status: completedResponse.body.status, metrics: runMetrics(completedResponse.body) };
        await persist();
      } catch (error) {
        if (error.code === "POLL_TIMEOUT") {
          const interruptedRunId = error.remoteRunId || attempt.remoteRunId;
          attempt.interruptAttempt = { at: new Date().toISOString(), remoteRunId: interruptedRunId };
          try {
            const interrupt = await fetchJson(`${server.url}/api/runs/${encodeURIComponent(interruptedRunId)}/interrupt`, {
              method: "POST", timeoutMs: normalized.requestTimeoutMs,
            });
            await writeJson(path.join(attemptRawDirectory(directory, caseEntry.id, server.side), "interrupt-response.json"), interrupt);
            attempt.interruptAttempt.httpStatus = interrupt.status;
            attempt.interruptAttempt.runStatus = interrupt.body?.status || null;
          } catch (interruptError) {
            attempt.interruptAttempt.error = interruptError.message;
          }
          attempt.state = "timeout_interrupt_requested";
        } else if (error.code === "WEB_POLICY_VIOLATION") {
          attempt.state = "policy_violation";
          attempt.webPolicyEvaluation = error.assessment || null;
          attempt.final = {
            id: observedFinalResponse?.body?.id || attempt.remoteRunId,
            status: "failed",
            upstreamStatus: observedFinalResponse?.body?.status || null,
            metrics: observedFinalResponse?.body
              ? runMetrics({ ...observedFinalResponse.body, webPolicy: attempt.webPolicy })
              : null,
          };
        } else {
          attempt.state = "polling_incomplete";
        }
        attempt.error = { at: new Date().toISOString(), message: error.message };
        await persist();
        throw new Error(`${key}: ${error.message}`);
      }
    }

    const summary = {
      comparison,
      evaluationId: manifest.evaluationId,
      referenceStatus: manifest.referenceStatus,
      selectedCaseIds: manifest.selectedCaseIds,
      metricsAreUsageAndRuntimeOnly: true,
      attempts: Object.values(manifest.attempts).map((attempt) => ({
        caseId: attempt.caseId,
        side: attempt.side,
        profile: attempt.profile,
        webPolicy: attempt.webPolicy,
        remoteRunId: attempt.remoteRunId,
        state: attempt.state,
        metrics: attempt.final?.metrics || null,
      })),
    };
    await writeJson(path.join(directory, "summary.json"), summary);
    return { directory, manifest, summary };
  } finally {
    await rm(lockDirectory, { recursive: true, force: true });
  }
}

export function parseArguments(argv) {
  const options = {};
  const allowed = new Set(["comparison", "left-url", "right-url", "left-profile", "right-profile", "fixture-path", "output-directory", "resume-directory", "model", "effort", "timeout-ms", "poll-ms", "request-timeout-ms", "start-timeout-ms"]);
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--execute") {
      if (options.dryRun) throw new Error("Use either --execute or --dry-run");
      options.execute = true; continue;
    }
    if (token === "--dry-run") {
      if (options.execute) throw new Error("Use either --execute or --dry-run");
      options.dryRun = true; continue;
    }
    if (token === "--help") { options.help = true; continue; }
    if (!token.startsWith("--")) throw new Error(`Unbekanntes Argument: ${token}`);
    if (!allowed.has(token.slice(2))) throw new Error(`Unbekanntes Argument: ${token}`);
    const key = token.slice(2).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    const value = argv[index + 1];
    if (value === undefined || value.startsWith("--")) throw new Error(`Wert fehlt für ${token}`);
    options[key] = value;
    index += 1;
  }
  if (options.execute && (!options.leftUrl || !options.rightUrl)) {
    throw new Error("Erforderlich: --left-url URL --right-url URL (bestehende, getrennte Server)");
  }
  return options;
}

async function main() {
  try {
    const options = parseArguments(process.argv.slice(2));
    if (options.help) {
      process.stdout.write("Default: offline plan, no model calls. Use --execute --left-url URL --right-url URL to run. Optional: --comparison tools|local_sources|graph|instructions|combined --fixture-path FILE --output-directory DIR --resume-directory DIR --model MODEL --effort low --timeout-ms MS.\n");
      return;
    }
    const result = await runPairedEvaluation(options);
    process.stdout.write(result.dryRun ? `${JSON.stringify(result, null, 2)}\n` : `Paired evaluation saved: ${result.directory}\n`);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) main();
