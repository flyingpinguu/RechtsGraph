import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { createDebugServer, createRun, updateRunFromMessage } from "../server.mjs";
import { RunStore } from "../run_store.mjs";

const MODEL = {
  id: "gpt-5.6-sol",
  displayName: "GPT-5.6-Sol",
  defaultReasoningEffort: "low",
  supportedReasoningEfforts: [{ reasoningEffort: "low" }],
};

function profile(version) {
  return {
    name: "lean",
    label: "Lean",
    defaultEffort: "low",
    defaultSummary: "none",
    appServerArgs: [],
    baseInstructions: `base-${version}`,
    developerInstructions: `developer-${version}: ask a clarification question when needed`,
  };
}

function mockAppServer({ startAt = 0 } = {}) {
  let listener = null;
  let turnNumber = startAt;
  const requests = [];
  const server = {
    ready: true,
    userAgent: "mock-codex",
    models: [MODEL],
    mcpServers: [],
    requests,
    onMessage(callback) { listener = callback; },
    async request(method, params) {
      requests.push({ method, params });
      if (method === "thread/start") {
        return { thread: { id: "thread-dialogue", ephemeral: false }, instructionSources: [] };
      }
      if (method === "thread/resume") {
        return { thread: { id: params.threadId, ephemeral: false }, instructionSources: [] };
      }
      if (method === "turn/start") {
        turnNumber += 1;
        const turnId = `turn-${turnNumber}`;
        const first = turnNumber === 1;
        setTimeout(() => {
          const totalTokens = first ? 100 : 160;
          const lastTokens = first ? 100 : 60;
          // This event deliberately omits turnId. The unique active run for the
          // thread must receive it, never a completed earlier turn.
          listener({
            method: "item/agentMessage/delta",
            params: {
              threadId: "thread-dialogue",
              itemId: `answer-${turnNumber}`,
              phase: "final_answer",
              delta: first ? "Welche Gerichtsbarkeit meinst du?" : "Für Deutschland gilt: ",
            },
          });
          listener({
            method: "thread/tokenUsage/updated",
            params: {
              threadId: "thread-dialogue",
              turnId,
              tokenUsage: {
                last: { inputTokens: lastTokens - 10, cachedInputTokens: 10, outputTokens: 10, reasoningOutputTokens: 2, totalTokens: lastTokens },
                total: { inputTokens: totalTokens - 10, cachedInputTokens: 20, outputTokens: 10, reasoningOutputTokens: 2, totalTokens },
                modelContextWindow: 1_050_000,
              },
            },
          });
          listener({
            method: "item/completed",
            params: {
              threadId: "thread-dialogue",
              turnId,
              item: {
                id: `answer-${turnNumber}`,
                type: "agentMessage",
                phase: "final_answer",
                text: first ? "Welche Gerichtsbarkeit meinst du?" : "Für Deutschland gilt: Antwort mit Kontext.",
              },
            },
          });
          listener({
            method: "turn/completed",
            params: { threadId: "thread-dialogue", turn: { id: turnId, status: "completed", error: null } },
          });
        }, 5);
        return { turn: { id: turnId, status: "inProgress", items: [] } };
      }
      throw new Error(`Unexpected method: ${method}`);
    },
    async stop() {},
  };
  return server;
}

async function waitForCompleted(baseUrl, runId) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    const response = await fetch(`${baseUrl}/api/runs/${runId}`);
    const run = await response.json();
    if (run.status === "completed") return run;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error(`run ${runId} did not complete`);
}

test("continues a persisted native thread after restart and returns a readable transcript", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "rechtsgraph-dialogue-"));
  const firstAppServer = mockAppServer();
  const firstInstance = await createDebugServer({
    port: 0,
    appServer: firstAppServer,
    startAppServer: false,
    runDirectory: directory,
    profile: profile("v1"),
  });
  const firstBaseUrl = `http://${firstInstance.host}:${firstInstance.port}`;

  const startedResponse = await fetch(`${firstBaseUrl}/api/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: "Welche Regeln gelten?", model: MODEL.id, effort: "low" }),
  });
  assert.equal(startedResponse.status, 202);
  const started = await startedResponse.json();
  const first = await waitForCompleted(firstBaseUrl, started.id);
  assert.equal(first.finalText, "Welche Gerichtsbarkeit meinst du?");
  assert.equal(first.conversation.turns.length, 1);
  assert.equal(first.threadEphemeral, false);
  await firstInstance.close();

  const secondAppServer = mockAppServer({ startAt: 1 });
  const secondInstance = await createDebugServer({
    port: 0,
    appServer: secondAppServer,
    startAppServer: false,
    runDirectory: directory,
    // The live profile changed, but resume must use the persisted v1 snapshot.
    profile: profile("v2"),
  });
  t.after(async () => {
    await secondInstance.close();
    await rm(directory, { recursive: true, force: true });
  });
  const secondBaseUrl = `http://${secondInstance.host}:${secondInstance.port}`;

  const changedModelResponse = await fetch(`${secondBaseUrl}/api/runs/${first.id}/replies`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: "Deutschland.", model: "gpt-other" }),
  });
  assert.equal(changedModelResponse.status, 409);
  assert.match((await changedModelResponse.json()).error, /unverändert model/);
  assert.equal(secondAppServer.requests.length, 0);

  const replyResponse = await fetch(`${secondBaseUrl}/api/runs/${first.id}/replies`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: "Deutschland." }),
  });
  assert.equal(replyResponse.status, 202);
  const reply = await replyResponse.json();
  assert.equal(reply.parentRunId, first.id);
  assert.equal(reply.conversationId, first.conversationId);
  assert.equal(reply.model, first.model);
  assert.equal(reply.effort, first.effort);

  const resume = secondAppServer.requests.find((request) => request.method === "thread/resume");
  assert.equal(resume.params.threadId, "thread-dialogue");
  assert.equal(resume.params.baseInstructions, "base-v1");
  assert.match(resume.params.developerInstructions, /^developer-v1:/);

  const completed = await waitForCompleted(secondBaseUrl, reply.id);
  assert.equal(completed.finalText, "Für Deutschland gilt: Antwort mit Kontext.");
  assert.equal(completed.turnUsage.totalTokens, 60);
  assert.equal(completed.usage.total.totalTokens, 160);
  assert.deepEqual(completed.conversation.turns.map((turn) => turn.prompt), [
    "Welche Regeln gelten?",
    "Deutschland.",
  ]);
  assert.deepEqual(completed.conversation.turns.map((turn) => turn.finalText), [
    "Welche Gerichtsbarkeit meinst du?",
    "Für Deutschland gilt: Antwort mit Kontext.",
  ]);

  const conversationResponse = await fetch(`${secondBaseUrl}/api/conversations/${completed.conversationId}`);
  assert.equal(conversationResponse.status, 200);
  assert.equal((await conversationResponse.json()).turns.length, 2);

  const branchResponse = await fetch(`${secondBaseUrl}/api/runs/${first.id}/replies`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: "Alternative Abzweigung" }),
  });
  assert.equal(branchResponse.status, 409);
  assert.match((await branchResponse.json()).error, /neuesten Turn/);
});

test("rejects legacy ephemeral conversations and locked-option changes", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "rechtsgraph-dialogue-guards-"));
  const store = new RunStore(directory);
  const legacy = createRun({
    prompt: "Alt",
    model: MODEL.id,
    effort: "low",
    summary: "none",
    networkAccess: true,
    profile: "lean",
    threadId: "thread-old",
    threadEphemeral: true,
  });
  updateRunFromMessage(legacy, {
    method: "turn/completed",
    params: { turn: { id: "turn-old", status: "completed" } },
  });
  await store.flush(legacy);

  const appServer = mockAppServer();
  const instance = await createDebugServer({
    port: 0,
    appServer,
    startAppServer: false,
    runDirectory: directory,
    profile: profile("v1"),
  });
  t.after(async () => {
    await instance.close();
    await rm(directory, { recursive: true, force: true });
  });
  const baseUrl = `http://${instance.host}:${instance.port}`;

  const response = await fetch(`${baseUrl}/api/runs/${legacy.id}/replies`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: "Weiter" }),
  });
  assert.equal(response.status, 409);
  assert.match((await response.json()).error, /ephemer/);
  assert.equal(appServer.requests.length, 0);
});

test("hydrates the complete parent chain when the root is outside the recent-run window", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "rechtsgraph-dialogue-long-"));
  const store = new RunStore(directory);
  const root = createRun({
    prompt: "Turn 1",
    model: MODEL.id,
    effort: "low",
    summary: "none",
    networkAccess: true,
    profile: "lean",
    threadId: "thread-long",
    threadEphemeral: false,
    baseInstructions: "base-v1",
    developerInstructions: "developer-v1",
  });
  updateRunFromMessage(root, {
    method: "turn/completed",
    params: { turn: { id: "turn-long-1", status: "completed" } },
  });
  await store.flush(root);
  let parent = root;
  for (let index = 2; index <= 252; index += 1) {
    const run = createRun({
      prompt: `Turn ${index}`,
      model: root.model,
      effort: root.effort,
      summary: root.summary,
      networkAccess: root.networkAccess,
      profile: root.profile,
      conversationId: root.conversationId,
      parentRunId: parent.id,
      rootRunId: root.id,
      threadId: root.threadId,
      threadEphemeral: false,
      baseInstructions: "base-v1",
      developerInstructions: "developer-v1",
    });
    updateRunFromMessage(run, {
      method: "turn/completed",
      params: { turn: { id: `turn-long-${index}`, status: "completed" } },
    });
    await store.flush(run);
    parent = run;
  }

  const recent = await store.loadRecent(250);
  assert.equal(recent.some((run) => run.id === root.id), false);

  const instance = await createDebugServer({
    port: 0,
    appServer: mockAppServer(),
    startAppServer: false,
    runDirectory: directory,
    profile: profile("v1"),
  });
  t.after(async () => {
    await instance.close();
    await rm(directory, { recursive: true, force: true });
  });
  const response = await fetch(`http://${instance.host}:${instance.port}/api/runs/${parent.id}`);
  assert.equal(response.status, 200);
  const restored = await response.json();
  assert.equal(restored.conversation.rootRunId, root.id);
  assert.equal(restored.conversation.latestRunId, parent.id);
  assert.equal(restored.conversation.turns.length, 252);
  assert.equal(restored.conversation.turns[0].prompt, "Turn 1");
  assert.equal(restored.conversation.turns.at(-1).prompt, "Turn 252");
});
