import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { loadProjectOpenRouterKey, runLegalCompetenceBenchmark } from "../run_legal_competence_v1.mjs";

test("legal-competence benchmark is an offline 6-run single-turn outcome comparison by default", async () => {
  const plan = await runLegalCompetenceBenchmark({});

  assert.equal(plan.dryRun, true);
  assert.equal(plan.plannedAgentRuns, 6);
  assert.deepEqual(plan.plannedModelTurns, { minimum: 6, maximum: 6 });
  assert.equal(plan.suite, "smoke");
  assert.equal(plan.timeoutMs, 180_000);
  assert.equal(plan.maximumCandidateWaitMs, 1_080_000);
  assert.equal(plan.benchmarkVersion, "legal-competence-v1");
  assert.equal(plan.plan.filter((entry) => entry.profile === "lean").length, 3);
  assert.equal(plan.plan.filter((entry) => entry.profile === "web_only").length, 3);
  assert.deepEqual(plan.plan.slice(0, 4).map((entry) => entry.profile), [
    "lean", "web_only", "web_only", "lean",
  ]);
  assert.ok(plan.plan.every((entry) => !entry.caseId.startsWith("retrieval_")));
  await assert.rejects(
    runLegalCompetenceBenchmark({ comparison: "graph" }),
    /only the primary tools comparison/,
  );

  const full = await runLegalCompetenceBenchmark({ suite: "full" });
  assert.equal(full.plannedAgentRuns, 24);
  assert.deepEqual(full.plannedModelTurns, { minimum: 26, maximum: 28 });
  assert.equal(full.timeoutMs, 600_000);
  const custom = await runLegalCompetenceBenchmark({ timeoutMs: 60_000 });
  assert.equal(custom.timeoutMs, 60_000);
  for (const caseId of plan.selectedCaseIds) {
    const pair = plan.plan.filter((entry) => entry.caseId === caseId);
    assert.equal(pair[0].prompt, pair[1].prompt);
    assert.equal(pair[0].dialogue, false);
  }

  const quick = await runLegalCompetenceBenchmark({ suite: "quick" });
  assert.equal(quick.plannedAgentRuns, 10);
  assert.deepEqual(quick.plannedModelTurns, { minimum: 12, maximum: 14 });
  assert.equal(quick.selectedCaseIds.length, 5);

  const stability = await runLegalCompetenceBenchmark({ candidateRepeats: 2 });
  assert.equal(stability.suite, "stability");
  assert.equal(stability.plannedAgentRuns, 12);
  assert.deepEqual(stability.plannedModelTurns, { minimum: 12, maximum: 12 });
  assert.equal(new Set(stability.plan.map((entry) => entry.candidateRepeat)).size, 2);
});

async function requestJson(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
}

async function startFakeServer(profile) {
  const runs = new Map();
  let nextId = 1;
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://127.0.0.1");
    response.setHeader("Content-Type", "application/json");
    if (request.method === "GET" && url.pathname === "/api/config") {
      response.end(JSON.stringify({
        profile: { name: profile },
        appServerReady: true,
        mcpInventory: profile === "lean" ? [{ name: "rechtsgraph", toolCount: 8 }] : [],
        instructionHashes: { base: "same-legal-guide", developer: `${profile}-developer` },
        models: [{ id: "test-model" }],
        appServerArgs: ['web_search="live"'],
        providerCapabilities: { checked: true, webSearch: true, imageGeneration: false, namespaceTools: false },
      }));
      return;
    }
    if (request.method === "POST" && url.pathname === "/api/runs") {
      const body = await requestJson(request);
      const id = `${profile}-${nextId++}`;
      const dialogue = body.prompt.includes("mehrstufiges Mandatsaufnahme-Gespräch");
      runs.set(id, {
        id,
        prompt: body.prompt,
        parentRunId: null,
        finalText: dialogue
          ? "RÜCKFRAGEN\n1. Trägt das Original eine Unterschrift und wann war es im Briefkasten?\n2. Seit wann arbeiten Sie dort und wie viele Beschäftigte gibt es?\n3. Was steht zu Art, Termin und Grund der Kündigung im Schreiben?\n4. Gibt es einen Betriebsrat, besonderen Schutz, Tarifvertrag oder abweichende Vertragsregeln?"
          : `Synthetic ${profile} legal answer`,
      });
      response.statusCode = 202;
      response.end(JSON.stringify({ id, status: "running" }));
      return;
    }
    const replyMatch = url.pathname.match(/^\/api\/runs\/([^/]+)\/replies$/);
    if (request.method === "POST" && replyMatch && runs.has(replyMatch[1])) {
      const body = await requestJson(request);
      const parent = runs.get(replyMatch[1]);
      const id = `${profile}-${nextId++}`;
      const firstContinuation = parent.parentRunId === null;
      runs.set(id, {
        id,
        prompt: body.prompt,
        parentRunId: parent.id,
        finalText: profile === "web_only" && firstContinuation
          ? "LETZTE RÜCKFRAGEN\nGilt laut Arbeitsvertrag oder Tarifvertrag eine abweichende Kündigungsfrist?"
          : "EINSCHÄTZUNG\nDie unterschriebene Papierkündigung erfüllt voraussichtlich § 623 BGB. Die Dreiwochenfrist ist vorsorglich bis 6. Oktober 2026 zu sichern; die betriebsbedingte Rechtfertigung und Betriebsratsanhörung sind offen.",
      });
      response.statusCode = 202;
      response.end(JSON.stringify({ id, status: "running" }));
      return;
    }
    const match = url.pathname.match(/^\/api\/runs\/([^/]+)$/);
    if (request.method === "GET" && match && runs.has(match[1])) {
      const run = runs.get(match[1]);
      const chain = [];
      let cursor = run;
      while (cursor) {
        chain.unshift(cursor);
        cursor = cursor.parentRunId ? runs.get(cursor.parentRunId) : null;
      }
      const now = new Date().toISOString();
      response.end(JSON.stringify({
        id: run.id,
        status: "completed",
        startedAt: now,
        completedAt: now,
        firstAgentDeltaAt: now,
        finalText: run.finalText,
        usage: { total: { totalTokens: 10, inputTokens: 7, cachedInputTokens: 2, outputTokens: 3 } },
        events: [],
        toolItems: [],
        conversation: {
          id: `${profile}-conversation`,
          turns: chain.map((entry) => ({ id: entry.id, prompt: entry.prompt, finalText: entry.finalText })),
        },
      }));
      return;
    }
    response.statusCode = 404;
    response.end(JSON.stringify({ error: "not found" }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return {
    url: `http://127.0.0.1:${server.address().port}`,
    runCount: () => runs.size,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

test("live orchestration freezes and validates the legal benchmark before synthetic runs", async (t) => {
  const left = await startFakeServer("lean");
  const right = await startFakeServer("web_only");
  const outputDirectory = await mkdtemp(path.join(os.tmpdir(), "legal-competence-run-"));
  t.after(async () => {
    await Promise.all([left.close(), right.close()]);
    await rm(outputDirectory, { recursive: true, force: true });
  });

  const result = await runLegalCompetenceBenchmark({
    execute: true,
    suite: "full",
    leftUrl: left.url,
    rightUrl: right.url,
    outputDirectory,
    provider: "codex-default",
    model: "test-model",
    timeoutMs: 1_000,
    pollMs: 100,
  });

  await assert.rejects(runLegalCompetenceBenchmark({
    execute: true, suite: "full", resumeDirectory: outputDirectory,
    leftUrl: left.url, rightUrl: right.url, provider: "codex-default",
    model: "test-model", timeoutMs: 2_000, pollMs: 100,
  }), /Resume-Mismatch bei timeoutMs/);
  assert.equal(result.summary.attempts.length, 24);
  assert.equal(left.runCount(), 13);
  assert.equal(right.runCount(), 14);
  const validation = JSON.parse(await readFile(path.join(outputDirectory, "benchmark-validation.json"), "utf8"));
  assert.equal(validation.ok, true);
  assert.equal(validation.taskCount, 12);
  const frozen = JSON.parse(await readFile(path.join(outputDirectory, "benchmark-definition.json"), "utf8"));
  assert.equal(frozen.benchmarkVersion, "legal-competence-v1");
  assert.ok(frozen.files["rubric.json"]);
  assert.ok(frozen.files["judge_prompt.md"]);
  assert.ok(frozen.files["source_passages.json"]);
  const leanDialogue = JSON.parse(await readFile(path.join(
    outputDirectory, "raw", "triage_01_employment_dismissal", "left", "dialogue-transcript.json",
  ), "utf8"));
  const webDialogue = JSON.parse(await readFile(path.join(
    outputDirectory, "raw", "triage_01_employment_dismissal", "right", "dialogue-transcript.json",
  ), "utf8"));
  assert.equal(leanDialogue.turns.length, 2);
  assert.equal(webDialogue.turns.length, 3);
  assert.equal(leanDialogue.answeredFactIds.length, 7);
  assert.equal(webDialogue.answeredFactIds.length, 7);
  assert.match(leanDialogue.turns.at(-1).assistant, /^EINSCHÄTZUNG/);
  assert.match(webDialogue.turns.at(-1).assistant, /^EINSCHÄTZUNG/);
  assert.ok((await readFile(path.join(outputDirectory, "environment-snapshot.json"), "utf8")).includes('"test-model"'));
});

test("candidate repetitions execute as isolated stability-suite comparisons", async (t) => {
  const left = await startFakeServer("lean");
  const right = await startFakeServer("web_only");
  const outputDirectory = await mkdtemp(path.join(os.tmpdir(), "legal-stability-run-"));
  t.after(async () => {
    await Promise.all([left.close(), right.close()]);
    await rm(outputDirectory, { recursive: true, force: true });
  });

  const result = await runLegalCompetenceBenchmark({
    execute: true,
    candidateRepeats: 2,
    leftUrl: left.url,
    rightUrl: right.url,
    outputDirectory,
    provider: "codex-default",
    model: "test-model",
    timeoutMs: 1_000,
    pollMs: 100,
  });

  assert.equal(result.candidateRepeats, 2);
  assert.equal(result.repetitions.length, 2);
  assert.ok((await readFile(path.join(outputDirectory, "candidate-repetition-manifest.json"), "utf8")).includes('"stability"'));
  for (const repeat of [1, 2]) {
    const manifest = JSON.parse(await readFile(path.join(
      outputDirectory, `candidate-repeat-${String(repeat).padStart(2, "0")}`, "manifest.json",
    ), "utf8"));
    assert.equal(manifest.selectedCaseIds.length, 3);
    assert.equal(Object.keys(manifest.attempts).length, 6);
  }
});

test("local key loader reads only the API key and preserves the environment", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "legal-local-key-"));
  const previous = process.env.OPENROUTER_API_KEY;
  const envPath = path.join(directory, ".env");
  try {
    await writeFile(envPath, 'OTHER_SECRET=do-not-load\nOPENROUTER_API_KEY="local-test-key"\n');
    delete process.env.OPENROUTER_API_KEY;
    await loadProjectOpenRouterKey(envPath);
    assert.equal(process.env.OPENROUTER_API_KEY, "local-test-key");
    assert.equal(process.env.OTHER_SECRET, undefined);
    process.env.OPENROUTER_API_KEY = "existing-test-key";
    await loadProjectOpenRouterKey(envPath);
    assert.equal(process.env.OPENROUTER_API_KEY, "existing-test-key");
  } finally {
    if (previous === undefined) delete process.env.OPENROUTER_API_KEY;
    else process.env.OPENROUTER_API_KEY = previous;
    await rm(directory, { recursive: true, force: true });
  }
});


test("actual CLI defaults to the compact timeout and honors explicit override", () => {
  const runner = fileURLToPath(new URL("../run_legal_competence_v1.mjs", import.meta.url));
  const plan = JSON.parse(execFileSync(process.execPath, [runner], { encoding: "utf8" }));
  assert.equal(plan.timeoutMs, 180_000);
  const explicit = JSON.parse(execFileSync(process.execPath, [runner, "--timeout-ms", "42000"], { encoding: "utf8" }));
  assert.equal(explicit.timeoutMs, 42_000);
});
