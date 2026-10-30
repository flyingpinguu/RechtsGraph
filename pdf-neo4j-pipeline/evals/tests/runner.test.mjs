import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { runMetrics, runPairedEvaluation, validateToolTrace, parseArguments } from "../run_paired_eval.mjs";
import { validateMcpInventory, AGENT_PROFILES } from "../../debug_ui/profiles.mjs";

test("empty and truncated retrieval are distinct from missing payload and transport errors", () => {
  const item = (id, tool, result) => ({ id, type: "mcpToolCall", server: "rechtsgraph", tool,
    status: "completed", result: { structuredContent: { result } } });
  const metrics = runMetrics({ toolItems: [
    item("empty", "get_structural_unit", { ok: true, passage_count: 0, passages: [], truncated: false }),
    item("cut", "get_structural_unit", { ok: true, passage_count: 1, passages: [{text: "partial"}], truncated: true }),
    item("unknown", "get_structural_unit", { ok: true }),
    item("status", "get_service_status", { ok: true, passage_count: 0, passages: [] }),
    item("failed", "get_structural_unit", { ok: false, passage_count: 0, passages: [] }),
  ] });
  assert.equal(metrics.rechtsgraphEmptyEvidenceCallCount, 1);
  assert.equal(metrics.rechtsgraphTruncatedEvidenceCallCount, 1);
  assert.equal(metrics.rechtsgraphToolErrorCount, 1);
  assert.equal(runMetrics({}).rechtsgraphEmptyEvidenceCallCount, null);
});

async function startFakeServer(profile, observedStarts, {
  dropStart = false,
  neverComplete = false,
  inventory,
  webSearch = false,
  providerWebSearch = true,
} = {}) {
  const runs = new Map();
  let nextId = 1;
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://127.0.0.1");
    response.setHeader("Content-Type", "application/json");
    if (request.method === "GET" && url.pathname === "/api/config") {
      response.end(JSON.stringify({ profile: { name: profile }, appServerReady: true,
        mcpInventory: inventory || (profile === "lean" ? [{ name: "rechtsgraph", toolCount: 8 }] : []),
        instructionHashes: { base: profile === "web_baseline" ? "baseline-base" : "same-base" }, models: [{ id: "gpt-5.6-sol" }],
        appServerArgs: ['web_search="live"'],
        providerCapabilities: { checked: true, webSearch: providerWebSearch, imageGeneration: false, namespaceTools: false },
      }));
      return;
    }
    if (request.method === "POST" && url.pathname === "/api/runs") {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      const id = `${profile}-${nextId++}`;
      runs.set(id, body);
      observedStarts.push({ profile, prompt: body.prompt, id });
      if (dropStart) { response.destroy(); return; }
      response.statusCode = 202;
      response.end(JSON.stringify({ id, status: "running" }));
      return;
    }
    if (request.method === "POST" && url.pathname.endsWith("/interrupt")) {
      observedStarts.push({ interrupt: true });
      response.end(JSON.stringify({ status: "interrupting" }));
      return;
    }
    const match = url.pathname.match(/^\/api\/runs\/([^/]+)$/);
    if (request.method === "GET" && match && runs.has(match[1])) {
      const now = new Date().toISOString();
      const input = runs.get(match[1]);
      const webItem = webSearch ? {
        id: `web-${match[1]}`,
        type: "webSearch",
        status: "completed",
        query: "EUR-Lex Regulation 2024/1689",
        results: [{ type: "text_result", url: "https://eur-lex.europa.eu/eli/reg/2024/1689/oj" }],
      } : null;
      response.end(JSON.stringify({
        id: match[1], status: neverComplete ? "running" : "completed", startedAt: now, completedAt: now, firstAgentDeltaAt: now,
        webPolicy: input.webPolicy || "optional",
        toolCallCount: webItem ? 1 : 0,
        usage: { total: { inputTokens: 20, cachedInputTokens: 7, outputTokens: 4, reasoningOutputTokens: 2, totalTokens: 24 } },
        toolItems: webItem ? [webItem] : [],
        events: webItem ? [{ seq: 1, message: { method: "item/completed", params: { item: webItem } } }] : [],
      }));
      return;
    }
    response.statusCode = 404;
    response.end(JSON.stringify({ error: "not found" }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  return {
    url: `http://127.0.0.1:${address.port}`,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

test("paired runner validates profiles, alternates sequential starts, and saves raw evidence", async (t) => {
  const observedStarts = [];
  const left = await startFakeServer("lean", observedStarts);
  const right = await startFakeServer("web_only", observedStarts);
  const outputDirectory = await mkdtemp(path.join(os.tmpdir(), "paired-eval-"));
  t.after(async () => {
    await Promise.all([left.close(), right.close()]);
    await rm(outputDirectory, { recursive: true, force: true });
  });

  const result = await runPairedEvaluation({
    execute: true,
    leftUrl: left.url,
    rightUrl: right.url,
    outputDirectory,
    timeoutMs: 1_000,
    pollMs: 100,
  });

  assert.equal(observedStarts.length, 24);
  assert.equal(observedStarts[0].profile, "lean");
  assert.equal(observedStarts[1].profile, "web_only");
  assert.equal(observedStarts[2].profile, "web_only");
  assert.equal(observedStarts[3].profile, "lean");
  assert.equal(result.summary.attempts.length, 24);
  assert.equal(result.summary.attempts[0].metrics.uncachedInputTokens, 13);
  const raw = JSON.parse(await readFile(path.join(outputDirectory, "raw", "bgb-823-dog", "left", "final-response.json"), "utf8"));
  assert.equal(raw.body.status, "completed");
  const manifest = JSON.parse(await readFile(path.join(outputDirectory, "manifest.json"), "utf8"));
  assert.equal(manifest.attempts["bgb-823-dog:left"].state, "finished");
  await runPairedEvaluation({ execute: true, leftUrl: left.url, rightUrl: right.url, resumeDirectory: outputDirectory, timeoutMs: 1_000, pollMs: 100 });
  assert.equal(observedStarts.length, 24, "resume must not repeat completed model calls");
  await assert.rejects(runPairedEvaluation({ execute: true, leftUrl: left.url, rightUrl: right.url, outputDirectory }), /already contains/);
  await mkdir(path.join(outputDirectory, ".runner-lock"));
  await assert.rejects(runPairedEvaluation({ execute: true, leftUrl: left.url, rightUrl: right.url, resumeDirectory: outputDirectory }), { code: "EEXIST" });
  assert.equal(observedStarts.length, 24);
});

test("runtime and cached/uncached usage metrics remain separate", () => {
  const metrics = runMetrics({
    status: "completed",
    startedAt: "2026-01-01T00:00:00.000Z",
    firstAgentDeltaAt: "2026-01-01T00:00:00.250Z",
    completedAt: "2026-01-01T00:00:01.000Z",
    usage: { total: { totalTokens: 100, inputTokens: 70, cachedInputTokens: 20, outputTokens: 30 } },
  });
  assert.equal(metrics.durationMs, 1_000);
  assert.equal(metrics.timeToFirstAgentDeltaMs, 250);
  assert.equal(metrics.cachedInputTokens, 20);
  assert.equal(metrics.uncachedInputTokens, 50);
  assert.equal(metrics.totalTokens, 100);
  assert.equal(runMetrics({ usage: { total: { inputTokens: null } } }).inputTokens, null);
  assert.equal(runMetrics({}).totalTokens, null);
});

test("tool metrics use final trace items and deduplicate repeated events by item ID", () => {
  const metrics = runMetrics({
    status: "completed",
    toolCallCount: 99,
    events: [
      { message: { method: "item/started", params: { item: { id: "web-1", type: "webSearch" } } } },
      { message: { method: "item/completed", params: { item: { id: "web-1", type: "webSearch" } } } },
      { message: { method: "item/started", params: { item: { id: "status-1", type: "mcpToolCall", server: "rechtsgraph", tool: "get_service_status" } } } },
      { message: { method: "item/completed", params: { item: { id: "status-1", type: "mcpToolCall", server: "rechtsgraph", tool: "get_service_status", status: "completed" } } } },
      { message: { method: "item/started", params: { item: { id: "evidence-1", type: "mcpToolCall", server: "rechtsgraph", tool: "get_structural_unit" } } } },
      { message: { method: "item/completed", params: { item: { id: "evidence-1", type: "mcpToolCall", server: "rechtsgraph", tool: "get_structural_unit", status: "completed" } } } },
    ],
    toolItems: [
      { id: "web-1", type: "webSearch" },
      { id: "status-1", type: "mcpToolCall", server: "rechtsgraph", tool: "get_service_status", status: "completed" },
      { id: "evidence-1", type: "mcpToolCall", server: "rechtsgraph", tool: "get_structural_unit", status: "completed" },
    ],
  });

  assert.equal(metrics.toolTraceAvailable, true);
  assert.equal(metrics.toolCallCount, 3, "generic aggregate must not replace trace-derived calls");
  assert.equal(metrics.webToolCallCount, 1);
  assert.equal(metrics.rechtsgraphToolCallCount, 2);
  assert.equal(metrics.rechtsgraphStatusCallCount, 1);
  assert.equal(metrics.rechtsgraphEvidenceCallCount, 1);
  assert.equal(metrics.rechtsgraphUsageClassification, "evidence-requested");
  assert.equal(metrics.terminalFailure, false);
});

test("tool metrics distinguish observed zero, unavailable trace, status-only use, and MCP errors", () => {
  const observedZero = runMetrics({ status: "completed", toolCallCount: 8, toolItems: [], events: [] });
  assert.equal(observedZero.toolTraceAvailable, true);
  assert.equal(observedZero.toolCallCount, 0);
  assert.equal(observedZero.webToolCallCount, 0);
  assert.equal(observedZero.rechtsgraphToolCallCount, 0);
  assert.equal(observedZero.rechtsgraphUsageClassification, "not-used");

  const unavailable = runMetrics({ status: "completed", toolCallCount: 8 });
  assert.equal(unavailable.toolTraceAvailable, false);
  assert.equal(unavailable.toolCallCount, null);
  assert.equal(unavailable.webToolCallCount, null);
  assert.equal(unavailable.rechtsgraphUsageClassification, "trace-unavailable");

  const incomplete = runMetrics({ events: [{ message: { params: { item: {
    type: "mcpToolCall", server: "rechtsgraph", tool: "get_structural_unit",
  } } } }] });
  assert.equal(incomplete.toolTraceAvailable, true);
  assert.equal(incomplete.toolTraceHasUnidentifiedItems, true);
  assert.equal(incomplete.toolCallCount, null);
  assert.equal(incomplete.rechtsgraphUsageClassification, "trace-incomplete");

  const statusOnly = runMetrics({ events: [], toolItems: [{
    id: "status-ok", type: "mcpToolCall", server: "rechtsgraph", tool: "get_service_status", status: "completed",
  }] });
  assert.equal(statusOnly.rechtsgraphUsageClassification, "status-only");
  assert.equal(statusOnly.rechtsgraphEvidenceCallCount, 0);

  const failed = runMetrics({
    status: "failed",
    events: [],
    toolItems: [{
      id: "status-error", type: "mcpToolCall", server: "rechtsgraph", tool: "get_service_status",
      status: "completed", result: { structuredContent: { result: { ok: false, error: "offline" } } },
    }, {
      id: "text-error", type: "mcpToolCall", server: "rechtsgraph", tool: "search_corpus",
      status: "completed", result: { content: [{ type: "text", text: '{"ok":false,"error":"bad query"}' }] },
    }],
  });
  assert.equal(failed.rechtsgraphUsageClassification, "evidence-requested");
  assert.equal(failed.rechtsgraphEvidenceCallCount, 1);
  assert.equal(failed.toolErrorCount, 2);
  assert.equal(failed.rechtsgraphToolErrorCount, 2);
  assert.equal(failed.terminalFailure, true);
});

test("default is an offline plan and flags are validated", async () => {
  const plan = await runPairedEvaluation({ leftUrl: "invalid", rightUrl: "invalid" });
  assert.equal(plan.dryRun, true);
  assert.equal(plan.plannedAgentRuns, 24);
  assert.equal(plan.plan[0].profile, "lean");
  assert.equal(plan.plan[2].profile, "web_only");
  assert.throws(() => parseArguments(["--unknown", "x"]));
  assert.throws(() => parseArguments(["--execute"]));
  assert.throws(() => parseArguments(["--dry-run", "--execute"]));
  const baseline = await runPairedEvaluation({ comparison: "combined" });
  assert.equal(baseline.plan[1].profile, "web_baseline");
  const instructions = await runPairedEvaluation({ comparison: "instructions" });
  assert.deepEqual(instructions.plan.slice(0, 2).map((entry) => entry.profile), ["web_only", "web_baseline"]);
  const localSources = await runPairedEvaluation({ comparison: "local_sources" });
  assert.deepEqual(localSources.plan.slice(0, 2).map((entry) => entry.profile), ["local_first", "web_only"]);
  const graph = await runPairedEvaluation({ comparison: "graph" });
  assert.deepEqual(graph.plan.slice(0, 2).map((entry) => entry.profile), ["lean", "local_text_only"]);
  await assert.rejects(runPairedEvaluation({ comparison: "invalid" }));
});

test("paired profiles share instructions and reject unexpected tools", () => {
  assert.equal(AGENT_PROFILES.lean.baseInstructions, AGENT_PROFILES.web_only.baseInstructions);
  assert.notEqual(AGENT_PROFILES.web_only.baseInstructions, AGENT_PROFILES.web_baseline.baseInstructions);
  assert.deepEqual(AGENT_PROFILES.web_only.appServerArgs, AGENT_PROFILES.web_baseline.appServerArgs);
  assert.throws(() => validateMcpInventory("web_only", [{ name: "rechtsgraph", toolCount: 8 }]));
  assert.throws(() => validateMcpInventory("lean", []));
  assert.throws(() => validateMcpInventory("web_only", null));
  assert.doesNotThrow(() => validateMcpInventory("web_only", [{ name: "rechtsgraph", enabled: false, toolCount: 0 }]));
  assert.throws(() => validateMcpInventory("web_only", [{ name: "rechtsgraph", enabled: false, toolCount: 8 }]));
  assert.throws(() => validateMcpInventory("web_only", [{ name: "unknown", toolCount: 0 }]));
  assert.throws(() => validateToolTrace({ events: [{ message: { params: { item: { type: "mcpToolCall", server: "rechtsgraph" } } } }] }, "web_only"));
  assert.throws(() => validateToolTrace({ events: [{ message: { params: { item: { type: "commandExecution" } } } }] }, "lean"));
  assert.doesNotThrow(() => validateToolTrace({ events: [{ message: { params: { item: { type: "mcpToolCall", server: "rechtsgraph" } } } }] }, "local_first"));
  assert.throws(() => validateToolTrace({ events: [{ message: { params: { item: { type: "mcpToolCall", server: "rechtsgraph", tool: "explore_references" } } } }] }, "local_text_only"));
  assert.doesNotThrow(() => validateToolTrace({ events: [{ message: { params: { item: { type: "mcpToolCall", server: "rechtsgraph", tool: "get_structural_unit" } } } }] }, "local_text_only"));
  assert.throws(() => validateToolTrace({ events: [], toolItems: [] }, "web_only", "required"), { code: "WEB_POLICY_VIOLATION" });
  assert.doesNotThrow(() => validateToolTrace({
    events: [{ message: { params: { item: { id: "web", type: "webSearch",
      results: [{ url: "https://example.test/source" }] } } } }],
  }, "web_only", "required"));
});

test("required web policy is persisted and turns a completed answer without web into a technical failure", async (t) => {
  const starts = [];
  const left = await startFakeServer("lean", starts);
  const right = await startFakeServer("web_only", starts);
  const directory = await mkdtemp(path.join(os.tmpdir(), "paired-web-required-"));
  const fixturePath = path.join(directory, "fixture.json");
  await writeFile(fixturePath, `${JSON.stringify({ cases: [{
    id: "requires-web", prompt: "Prüfe den aktuellen Rechtsstand.", referenceStatus: "test", webPolicy: "required",
  }] })}\n`);
  t.after(async () => { await Promise.all([left.close(), right.close()]); await rm(directory, { recursive: true, force: true }); });

  await assert.rejects(runPairedEvaluation({ execute: true, leftUrl: left.url, rightUrl: right.url,
    fixturePath, outputDirectory: directory, timeoutMs: 1_000, pollMs: 100 }), /Web policy required violated/);
  const manifest = JSON.parse(await readFile(path.join(directory, "manifest.json"), "utf8"));
  const attempt = manifest.attempts["requires-web:left"];
  assert.equal(attempt.webPolicy, "required");
  assert.equal(attempt.state, "policy_violation");
  assert.equal(attempt.final.status, "failed");
  assert.equal(attempt.final.upstreamStatus, "completed");
  assert.equal(attempt.webPolicyEvaluation.reason, "web_search_not_used");
});

test("required web policy accepts an observed web event", async (t) => {
  const starts = [];
  const left = await startFakeServer("lean", starts, { webSearch: true });
  const right = await startFakeServer("web_only", starts, { webSearch: true });
  const parent = await mkdtemp(path.join(os.tmpdir(), "paired-web-observed-"));
  const fixturePath = path.join(parent, "fixture.json");
  const outputDirectory = path.join(parent, "result");
  await writeFile(fixturePath, `${JSON.stringify({ cases: [{
    id: "requires-web", prompt: "Prüfe den aktuellen Rechtsstand.", referenceStatus: "test", webPolicy: "required",
  }] })}\n`);
  t.after(async () => { await Promise.all([left.close(), right.close()]); await rm(parent, { recursive: true, force: true }); });

  const result = await runPairedEvaluation({ execute: true, leftUrl: left.url, rightUrl: right.url,
    fixturePath, outputDirectory, timeoutMs: 1_000, pollMs: 100 });
  assert.equal(result.summary.attempts.length, 2);
  assert(result.summary.attempts.every((entry) => entry.metrics.webPolicyCompliant === true));
  assert(result.summary.attempts.every((entry) => entry.metrics.webEvidenceSourceCount === 1));
});

test("instruction comparison accepts distinct guides with identical web capabilities", async (t) => {
  const starts = [];
  const left = await startFakeServer("web_only", starts);
  const right = await startFakeServer("web_baseline", starts);
  const directory = await mkdtemp(path.join(os.tmpdir(), "paired-instructions-"));
  t.after(async () => { await Promise.all([left.close(), right.close()]); await rm(directory, { recursive: true, force: true }); });
  const result = await runPairedEvaluation({ execute: true, comparison: "instructions",
    leftUrl: left.url, rightUrl: right.url, outputDirectory: directory });
  assert.equal(result.manifest.comparison, "instructions");
  assert.equal(starts.length, 24);
  assert.equal(starts[0].profile, "web_only");
  assert.equal(starts[1].profile, "web_baseline");
});

test("uncertain POST is persisted and resume never repeats it", async (t) => {
  const starts = [];
  const left = await startFakeServer("lean", starts, { dropStart: true });
  const right = await startFakeServer("web_only", starts);
  const directory = await mkdtemp(path.join(os.tmpdir(), "paired-uncertain-"));
  t.after(async () => { await Promise.all([left.close(), right.close()]); await rm(directory, { recursive: true, force: true }); });
  const options = { execute: true, leftUrl: left.url, rightUrl: right.url, outputDirectory: directory };
  await assert.rejects(runPairedEvaluation(options), /Start unklar/);
  await assert.rejects(runPairedEvaluation({ ...options, resumeDirectory: directory }), /kein weiterer POST/);
  assert.equal(starts.length, 1);
});

test("timeout interrupts the known run and retains its ID", async (t) => {
  const starts = [];
  const left = await startFakeServer("lean", starts, { neverComplete: true });
  const right = await startFakeServer("web_only", starts);
  const directory = await mkdtemp(path.join(os.tmpdir(), "paired-timeout-"));
  t.after(async () => { await Promise.all([left.close(), right.close()]); await rm(directory, { recursive: true, force: true }); });
  await assert.rejects(runPairedEvaluation({ execute: true, leftUrl: left.url, rightUrl: right.url,
    outputDirectory: directory, timeoutMs: 1000, pollMs: 100 }), /Polling-Timeout/);
  assert.equal(starts.filter((entry) => entry.interrupt).length, 1);
  const manifest = JSON.parse(await readFile(path.join(directory, "manifest.json"), "utf8"));
  assert.equal(manifest.attempts["bgb-823-dog:left"].remoteRunId, "lean-1");
});
