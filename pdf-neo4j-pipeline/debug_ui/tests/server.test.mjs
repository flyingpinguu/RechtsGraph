import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { createDebugServer, createRun, serializeRun, updateRunFromMessage } from "../server.mjs";
import { AGENT_PROFILES, NEUTRAL_LEGAL_RESEARCH_BASE_INSTRUCTIONS, resolveAgentProfile } from "../profiles.mjs";
import { RunStore } from "../run_store.mjs";

test("web-only profile shares neutral research instructions and disables configured MCP servers", () => {
  const webOnly = resolveAgentProfile("web_only");
  assert.equal(webOnly.baseInstructions, NEUTRAL_LEGAL_RESEARCH_BASE_INSTRUCTIONS);
  assert.equal(AGENT_PROFILES.lean.baseInstructions, webOnly.baseInstructions);
  assert.match(AGENT_PROFILES.lean.developerInstructions, /web research may be used whenever it helps/);
  assert.match(AGENT_PROFILES.lean.developerInstructions, /decisive issues/);
  assert.match(AGENT_PROFILES.local_first.developerInstructions, /do not collect every conceivable norm up front/);
  assert.match(webOnly.developerInstructions, /Do not use RechtsGraph, MCP tools/);
  assert.ok(webOnly.appServerArgs.includes("mcp_servers.rechtsgraph.enabled=false"));
  assert.ok(webOnly.appServerArgs.includes("mcp_servers.node_repl.enabled=false"));
  assert.ok(webOnly.appServerArgs.includes("mcp_servers.computer-use.enabled=false"));
  assert.ok(webOnly.appServerArgs.includes("web_search=\"live\""));
});

test("aggregates MCP tools, token usage, and completion state", () => {
  const run = createRun({
    prompt: "Test",
    model: "gpt-5.6-sol",
    effort: "medium",
    summary: "concise",
    networkAccess: true,
  });

  updateRunFromMessage(run, {
    method: "turn/started",
    params: { turn: { id: "turn-1", status: "inProgress" } },
  });
  updateRunFromMessage(run, {
    method: "item/started",
    params: {
      item: {
        id: "tool-1",
        type: "mcpToolCall",
        server: "rechtsgraph",
        tool: "find_documents",
        status: "inProgress",
        arguments: { query: "ErsatzbaustoffV" },
      },
    },
  });
  updateRunFromMessage(run, {
    method: "item/completed",
    params: {
      item: {
        id: "tool-1",
        type: "mcpToolCall",
        server: "rechtsgraph",
        tool: "find_documents",
        status: "completed",
        result: { content: [{ type: "text", text: "found" }] },
      },
    },
  });
  updateRunFromMessage(run, {
    method: "thread/tokenUsage/updated",
    params: {
      threadId: "thread-1",
      turnId: "turn-1",
      tokenUsage: {
        last: { inputTokens: 100, cachedInputTokens: 40, outputTokens: 20, reasoningOutputTokens: 8, totalTokens: 120 },
        total: { inputTokens: 100, cachedInputTokens: 40, outputTokens: 20, reasoningOutputTokens: 8, totalTokens: 120 },
        modelContextWindow: 1_050_000,
      },
    },
  });
  updateRunFromMessage(run, {
    method: "item/completed",
    params: { item: { id: "answer-1", type: "agentMessage", phase: "final_answer", text: "Antwort" } },
  });
  updateRunFromMessage(run, {
    method: "turn/completed",
    params: { turn: { id: "turn-1", status: "completed", error: null } },
  });

  const serialized = serializeRun(run, true);
  assert.equal(serialized.status, "completed");
  assert.equal(serialized.toolCallCount, 1);
  assert.equal(serialized.toolItems[0].tool, "find_documents");
  assert.equal(serialized.toolItems[0].status, "completed");
  assert.equal(serialized.usage.last.inputTokens, 100);
  assert.equal(serialized.modelRounds, 1);
  assert.equal(serialized.diagnostics.contentChars, 5);
  assert.equal(serialized.finalText, "Antwort");
  assert.equal(serialized.events.length, 6);
});

test("diagnoses content-only MCP JSON errors after compact result serialization", () => {
  const run = createRun({
    prompt: "Test",
    model: "gpt-5.6-sol",
    effort: "low",
    summary: "none",
    networkAccess: true,
  });
  updateRunFromMessage(run, {
    method: "item/completed",
    params: { item: {
      id: "content-only-error",
      type: "mcpToolCall",
      server: "rechtsgraph",
      tool: "get_structural_unit",
      status: "completed",
      result: { content: [{ type: "text", text: '{"result":{"ok":false,"error":"missing unit"}}' }] },
    } },
  });
  const serialized = serializeRun(run);
  const warning = serialized.diagnostics.warnings.find((entry) => entry.code === "failed_mcp_result");
  assert.equal(warning.severity, "error");
  assert.equal(warning.detail, "missing unit");
});

test("streams agent and reasoning deltas", () => {
  const run = createRun({ prompt: "Test", model: "gpt-5.6-sol", effort: "low", summary: "concise", networkAccess: false });
  updateRunFromMessage(run, {
    method: "item/agentMessage/delta",
    params: { itemId: "answer", phase: "final_answer", delta: "Hallo " },
  });
  updateRunFromMessage(run, {
    method: "item/agentMessage/delta",
    params: { itemId: "answer", phase: "final_answer", delta: "Welt" },
  });
  updateRunFromMessage(run, {
    method: "item/reasoning/summaryTextDelta",
    params: { itemId: "reason", summaryIndex: 0, delta: "Suche im Korpus" },
  });

  const serialized = serializeRun(run);
  assert.equal(serialized.finalText, "Hallo Welt");
  assert.equal(serialized.reasoningItems[0].summary[0], "Suche im Korpus");
  assert.ok(serialized.firstAgentDeltaAt);
});

test("serves a complete mocked agent run through the HTTP API", async (t) => {
  let listener = null;
  let threadStartParams = null;
  const runDirectory = await mkdtemp(path.join(os.tmpdir(), "rechtsgraph-debug-test-"));
  const mockAppServer = {
    ready: true,
    userAgent: "mock-codex",
    models: [{
      id: "gpt-5.6-sol",
      displayName: "GPT-5.6-Sol",
      defaultReasoningEffort: "low",
      supportedReasoningEfforts: [{ reasoningEffort: "low" }],
    }],
    onMessage(callback) { listener = callback; },
    async request(method, params) {
      if (method === "thread/start") {
        threadStartParams = params;
        return { thread: { id: "thread-http" }, instructionSources: [] };
      }
      if (method === "turn/start") {
        setTimeout(() => {
          listener({
            method: "item/started",
            params: {
              threadId: "thread-http",
              turnId: "turn-http",
              item: { id: "mcp-http", type: "mcpToolCall", server: "rechtsgraph", tool: "find_documents", status: "inProgress", arguments: { query: "Test" } },
            },
          });
          listener({
            method: "item/completed",
            params: {
              threadId: "thread-http",
              turnId: "turn-http",
              item: { id: "mcp-http", type: "mcpToolCall", server: "rechtsgraph", tool: "find_documents", status: "completed", result: { ok: true } },
            },
          });
          listener({
            method: "thread/tokenUsage/updated",
            params: {
              threadId: "thread-http",
              turnId: "turn-http",
              tokenUsage: {
                last: { inputTokens: 12, cachedInputTokens: 4, outputTokens: 3, reasoningOutputTokens: 1, totalTokens: 15 },
                total: { inputTokens: 12, cachedInputTokens: 4, outputTokens: 3, reasoningOutputTokens: 1, totalTokens: 15 },
                modelContextWindow: 1_050_000,
              },
            },
          });
          listener({
            method: "item/completed",
            params: {
              threadId: "thread-http",
              turnId: "turn-http",
              item: { id: "answer-http", type: "agentMessage", phase: "final_answer", text: "HTTP funktioniert" },
            },
          });
          listener({
            method: "turn/completed",
            params: { threadId: "thread-http", turn: { id: "turn-http", status: "completed", error: null } },
          });
        }, 5);
        return { turn: { id: "turn-http", status: "inProgress", items: [] } };
      }
      throw new Error(`Unexpected method: ${method}`);
    },
    async stop() {},
  };

  const instance = await createDebugServer({
    port: 0,
    appServer: mockAppServer,
    startAppServer: false,
    runDirectory,
  });
  t.after(async () => {
    await instance.close();
    await rm(runDirectory, { recursive: true, force: true });
  });
  const baseUrl = `http://${instance.host}:${instance.port}`;

  const startResponse = await fetch(`${baseUrl}/api/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: "Test", model: "gpt-5.6-sol", effort: "low",
      networkAccess: false, webPolicy: "forbidden" }),
  });
  assert.equal(startResponse.status, 202);
  const started = await startResponse.json();
  assert.equal(started.status, "running");
  assert.equal(started.profile, "lean");
  assert.equal(started.summary, "none");
  assert.equal(started.webPolicy, "forbidden");
  assert.match(threadStartParams.baseInstructions, /read-only legal research agent/);
  assert.match(threadStartParams.developerInstructions, /clarification question/);
  assert.match(threadStartParams.developerInstructions, /Do not use live web search/);
  assert.equal(threadStartParams.ephemeral, false);

  await new Promise((resolve) => setTimeout(resolve, 20));
  const runResponse = await fetch(`${baseUrl}/api/runs/${started.id}`);
  const completed = await runResponse.json();
  assert.equal(completed.status, "completed");
  assert.equal(completed.toolCallCount, 1);
  assert.equal(completed.usage.last.totalTokens, 15);
  assert.equal(completed.finalText, "HTTP funktioniert");
  const historyResponse = await fetch(`${baseUrl}/api/runs`);
  const history = await historyResponse.json();
  assert.equal(history.count, 1);
  assert.equal(history.runs[0].id, started.id);
});

test("persists and restores complete run telemetry", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "rechtsgraph-run-store-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const store = new RunStore(directory, { debounceMs: 1 });
  const run = createRun({
    prompt: "Persistenz",
    model: "gpt-5.6-sol",
    effort: "low",
    summary: "none",
    networkAccess: false,
    profile: "lean",
  });
  updateRunFromMessage(run, {
    method: "thread/tokenUsage/updated",
    params: {
      turnId: "turn-persisted",
      tokenUsage: {
        last: { inputTokens: 20, cachedInputTokens: 10, outputTokens: 2, totalTokens: 22 },
        total: { inputTokens: 20, cachedInputTokens: 10, outputTokens: 2, totalTokens: 22 },
        modelContextWindow: 1000,
      },
    },
  });
  updateRunFromMessage(run, {
    method: "item/completed",
    params: { item: { id: "answer", type: "agentMessage", phase: "final_answer", text: "Gespeichert" } },
  });
  updateRunFromMessage(run, {
    method: "turn/completed",
    params: { turn: { id: "turn-persisted", status: "completed" } },
  });

  await store.flush(run);
  const diskPayload = JSON.parse(await readFile(store.filePath(run.id), "utf8"));
  const restored = await new RunStore(directory).load(run.id);
  const serialized = serializeRun(restored, true);

  assert.equal(serialized.finalText, "Gespeichert");
  assert.equal(serialized.status, "completed");
  assert.equal(serialized.modelRounds, 1);
  assert.equal(serialized.events.length, 3);
  assert.equal(diskPayload.storageVersion, 3);
  assert.equal(diskPayload.items, undefined);
  assert.equal(diskPayload.events.length, 3);
});
