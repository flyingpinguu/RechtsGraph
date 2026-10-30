#!/usr/bin/env node
import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { callCodexJudge } from "./codex_judge.mjs";

const BENCHMARK_DIR = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_TIMEOUT_MS = 300_000;
const JUDGE_SEED_BASE = 9132026;

function judgeConfiguration(model, provider = "openrouter", effort = "low") {
  if (provider === "codex-default") return {
    provider, model, effort, responseFormat: "json_in_final_text",
    tools: "disabled_and_trace_checked", webSearch: "disabled",
    networkAccess: false, ephemeral: true, temperature: null, seed: null,
  };
  return {
    provider: "openrouter",
    model,
    temperature: 0,
    seedBase: JUDGE_SEED_BASE,
    seedPolicy: "seedBase + repeat",
    responseFormat: "json_object",
  };
}

export function parseArguments(argv) {
  const options = { repeats: 1, timeoutMs: DEFAULT_TIMEOUT_MS, execute: false, provider: "openrouter", effort: "low" };
  const valueOptions = new Set(["results", "model", "repeats", "timeout-ms", "only", "provider", "effort"]);
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--execute") { options.execute = true; continue; }
    if (token === "--dry-run") { options.execute = false; continue; }
    if (token === "--help") { options.help = true; continue; }
    if (!token.startsWith("--") || !valueOptions.has(token.slice(2))) {
      throw new Error(`Unknown argument: ${token}`);
    }
    const value = argv[index + 1];
    if (value === undefined || value.startsWith("--")) throw new Error(`Missing value for ${token}`);
    const key = token.slice(2).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    options[key] = value;
    index += 1;
  }
  if (!["openrouter", "codex-default"].includes(options.provider)) throw new Error("--provider must be openrouter or codex-default");
  if (!["low", "medium", "high", "xhigh"].includes(options.effort)) throw new Error("Invalid --effort");
  options.repeats = Number(options.repeats);
  options.timeoutMs = Number(options.timeoutMs);
  if (!Number.isInteger(options.repeats) || options.repeats < 1 || options.repeats > 5) {
    throw new Error("--repeats must be an integer from 1 to 5");
  }
  if (!Number.isFinite(options.timeoutMs) || options.timeoutMs < 1_000 || options.timeoutMs > 600_000) {
    throw new Error("--timeout-ms must be from 1000 to 600000");
  }
  if (!options.help && (!options.results || !options.model)) {
    throw new Error("Required: --results DIRECTORY --model MODEL");
  }
  if (options.only) {
    options.only = new Set(String(options.only).split(",").map((entry) => entry.trim()).filter(Boolean));
    if ([...options.only].some((entry) => !/^[a-z0-9_-]+\/[AB]$/i.test(entry))) {
      throw new Error("--only must be a comma-separated list such as task_id/A,task_id/B");
    }
  }
  return options;
}

function scorePath(results, taskId, answerLabel, repeat) {
  return path.join(results, "judge", "scores", taskId, answerLabel, `repeat-${String(repeat).padStart(2, "0")}.json`);
}

function rawPath(results, taskId, answerLabel, repeat) {
  return path.join(results, "judge", "raw", taskId, answerLabel, `repeat-${String(repeat).padStart(2, "0")}.json`);
}

function validateScore(score, input) {
  if (score?.taskId !== input.taskId || score?.answerLabel !== input.answerLabel) {
    throw new Error(`Judge returned the wrong identity for ${input.taskId}/${input.answerLabel}`);
  }
  const expectedCriteria = input.reference.criteria.map((entry) => entry.id);
  const observedCriteria = score.criteria?.map((entry) => entry.id);
  if (JSON.stringify(expectedCriteria) !== JSON.stringify(observedCriteria)) {
    throw new Error(`Judge returned the wrong criterion IDs/order for ${input.taskId}/${input.answerLabel}`);
  }
  for (const criterion of score.criteria) {
    if (!Number.isInteger(criterion.score) || criterion.score < 0 || criterion.score > 4) {
      throw new Error(`Invalid criterion score for ${input.taskId}/${criterion.id}`);
    }
    if (!String(criterion.reason || "").trim() || !String(criterion.answerEvidence || "").trim()) {
      throw new Error(`Judge omitted reason/evidence for ${input.taskId}/${criterion.id}`);
    }
  }
  const expectedFatal = input.reference.fatalErrors.map((entry) => entry.id);
  const observedFatal = score.fatalErrors?.map((entry) => entry.id);
  if (JSON.stringify(expectedFatal) !== JSON.stringify(observedFatal)) {
    throw new Error(`Judge returned the wrong fatal-error IDs/order for ${input.taskId}/${input.answerLabel}`);
  }
  for (const fatal of score.fatalErrors) {
    if (typeof fatal.present !== "boolean" || !String(fatal.reason || "").trim()
        || !String(fatal.answerEvidence || "").trim()) {
      throw new Error(`Invalid fatal-error decision for ${input.taskId}/${fatal.id}`);
    }
  }
  if (!String(score.overallAssessment || "").trim()) {
    throw new Error(`Judge omitted overallAssessment for ${input.taskId}/${input.answerLabel}`);
  }
  return score;
}

function parseJsonResponse(content) {
  if (Array.isArray(content)) {
    content = content.map((part) => part?.text || "").join("");
  }
  const text = String(content || "").trim()
    .replace(/^```(?:json)?\s*/i, "")
    .replace(/\s*```$/, "");
  return JSON.parse(text);
}

async function existsAndValid(file, input, judgeModel, configuration) {
  try {
    const payload = JSON.parse(await readFile(file, "utf8"));
    if (payload.judgeModel && payload.judgeModel !== judgeModel) {
      throw new Error(`score uses ${payload.judgeModel}, not ${judgeModel}`);
    }
    if (payload.judgeConfiguration && JSON.stringify(payload.judgeConfiguration) !== JSON.stringify(configuration)) {
      throw new Error("judge provider or generation settings changed");
    }
    if (!payload.judgeConfiguration && configuration.provider !== "openrouter") {
      throw new Error("existing judge provider was not recorded");
    }
    validateScore(payload.score || payload, input);
    return true;
  } catch (error) {
    if (error.code === "ENOENT") return false;
    throw new Error(`Existing judge output is invalid; refusing to overwrite ${file}: ${error.message}`);
  }
}

export async function buildPlan(options) {
  const results = path.resolve(options.results);
  const preparation = JSON.parse(await readFile(path.join(results, "judge", "preparation.json"), "utf8"));
  if (!Number.isInteger(preparation.judgeInputCount) || preparation.judgeInputCount < 2 || preparation.judgeInputCount % 2 !== 0) {
    throw new Error("Prepared judge input count must be a positive pair count");
  }
  const jobs = [];
  for (const taskDirectory of await readdir(path.join(results, "judge", "inputs"), { withFileTypes: true })) {
    if (!taskDirectory.isDirectory()) continue;
    for (const label of ["A", "B"]) {
      const inputPath = path.join(results, "judge", "inputs", taskDirectory.name, `${label}.json`);
      const input = JSON.parse(await readFile(inputPath, "utf8"));
      if (options.only && !options.only.has(`${input.taskId}/${input.answerLabel}`)) continue;
      for (let repeat = 1; repeat <= options.repeats; repeat += 1) {
        const outputPath = scorePath(results, input.taskId, input.answerLabel, repeat);
        jobs.push({ inputPath, outputPath, rawPath: rawPath(results, input.taskId, input.answerLabel, repeat), repeat, input });
      }
    }
  }
  jobs.sort((left, right) => `${left.input.taskId}:${left.input.answerLabel}:${left.repeat}`.localeCompare(`${right.input.taskId}:${right.input.answerLabel}:${right.repeat}`));
  return { results, jobs };
}

async function callJudge({ model, judgePrompt, input, timeoutMs, repeat }) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch("https://openrouter.ai/api/v1/chat/completions", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${process.env.OPENROUTER_API_KEY}`,
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/christinck/RechtsGraph",
        "X-Title": "RechtsGraph legal-competence benchmark",
      },
      body: JSON.stringify({
        model,
        temperature: 0,
        seed: JUDGE_SEED_BASE + repeat,
        response_format: { type: "json_object" },
        messages: [
          { role: "system", content: judgePrompt },
          { role: "user", content: JSON.stringify(input) },
        ],
      }),
      signal: controller.signal,
    });
    const text = await response.text();
    let payload;
    try { payload = JSON.parse(text); } catch { payload = null; }
    if (!response.ok || !payload) {
      throw new Error(`OpenRouter judge request failed (${response.status}): ${text.slice(0, 1_000)}`);
    }
    return payload;
  } finally {
    clearTimeout(timer);
  }
}

export async function runJudge(options) {
  options = { provider: "openrouter", effort: "low", repeats: 1, timeoutMs: DEFAULT_TIMEOUT_MS, ...options };
  const configuration = judgeConfiguration(options.model, options.provider, options.effort);
  const { results, jobs } = await buildPlan(options);
  const pending = [];
  for (const job of jobs) {
    if (!(await existsAndValid(job.outputPath, job.input, options.model, configuration))) pending.push(job);
  }
  if (!options.execute) {
    return {
      dryRun: true,
      results,
      model: options.model,
      judgeConfiguration: configuration,
      repeats: options.repeats,
      totalJobs: jobs.length,
      completedJobs: jobs.length - pending.length,
      pendingJobs: pending.length,
      note: "No model calls were made. Codex uses the logged-in account; OpenRouter uses API credit. No automatic retry.",
    };
  }
  if (options.provider === "openrouter" && !process.env.OPENROUTER_API_KEY) throw new Error("OPENROUTER_API_KEY is not set");
  let judgePrompt;
  try {
    judgePrompt = await readFile(path.join(results, "benchmark-definition", "judge_prompt.md"), "utf8");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
    judgePrompt = await readFile(path.join(BENCHMARK_DIR, "judge_prompt.md"), "utf8");
  }
  for (const [index, job] of pending.entries()) {
    process.stderr.write(`[${index + 1}/${pending.length}] judging ${job.input.taskId}/${job.input.answerLabel} repeat ${job.repeat}\n`);
    let providerResponse;
    try {
      const raw = JSON.parse(await readFile(job.rawPath, "utf8"));
      if (raw.judgeModel !== options.model) {
        throw new Error(`Existing raw judge response uses ${raw.judgeModel}, not ${options.model}: ${job.rawPath}`);
      }
      if ((raw.judgeConfiguration && JSON.stringify(raw.judgeConfiguration) !== JSON.stringify(configuration))
          || (!raw.judgeConfiguration && configuration.provider !== "openrouter")) {
        throw new Error("Existing raw judge provider/settings differ; refusing another model call.");
      }
      providerResponse = raw.providerResponse;
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      const adapter = options.provider === "codex-default" ? callCodexJudge : callJudge;
      providerResponse = await adapter({
        effort: options.effort,
        model: options.model,
        judgePrompt,
        input: job.input,
        timeoutMs: options.timeoutMs,
        repeat: job.repeat,
      });
      await mkdir(path.dirname(job.rawPath), { recursive: true });
      await writeFile(job.rawPath, `${JSON.stringify({
        schemaVersion: 1,
        judgeModel: options.model,
        judgeConfiguration: configuration,
        receivedAt: new Date().toISOString(),
        providerResponse,
      }, null, 2)}\n`, { encoding: "utf8", flag: "wx" });
    }
    if (providerResponse.error) throw new Error(providerResponse.error);
    const score = validateScore(
      parseJsonResponse(providerResponse.choices?.[0]?.message?.content),
      job.input,
    );
    const envelope = {
      schemaVersion: 1,
      judgeModel: options.model,
      judgeConfiguration: configuration,
      completedAt: new Date().toISOString(),
      repeat: job.repeat,
      responseId: providerResponse.id || null,
      usage: providerResponse.usage || null,
      score,
    };
    await mkdir(path.dirname(job.outputPath), { recursive: true });
    await writeFile(job.outputPath, `${JSON.stringify(envelope, null, 2)}\n`, { encoding: "utf8", flag: "wx" });
  }
  return { dryRun: false, results, model: options.model, completedJobs: pending.length, totalJobs: jobs.length };
}

async function main() {
  try {
    const options = parseArguments(process.argv.slice(2));
    if (options.help) {
      process.stdout.write([
        "Prepare judge inputs first, then run this command.",
        "",
        "Dry run: node run_judge.mjs --results DIR --model MODEL",
        "Execute: node run_judge.mjs --results DIR --model MODEL --execute",
        "Optional: --provider codex-default --effort low (logged-in Codex account; no API key)",
        "Optional: --repeats 3 --timeout-ms 300000",
        "Targeted recheck: --repeats 3 --only task_id/A,task_id/B",
        "OPENROUTER_API_KEY is read only from the environment.",
        "",
      ].join("\n"));
      return;
    }
    const result = await runJudge(options);
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) main();
