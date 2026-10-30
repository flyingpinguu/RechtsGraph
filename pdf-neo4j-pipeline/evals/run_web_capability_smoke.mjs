#!/usr/bin/env node
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { assertWebPolicy } from "../debug_ui/web_policy.mjs";

const DEFAULT_PROMPT = [
  "This is a web-capability smoke test.",
  "Use live web search to find the official EUR-Lex page for Regulation (EU) 2024/1689.",
  "Return only the official page URL.",
].join(" ");
const TERMINAL = new Set(["completed", "failed", "interrupted", "cancelled"]);

async function fetchJson(url, { method = "GET", body, timeoutMs = 15_000 } = {}) {
  const response = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  });
  const text = await response.text();
  let payload = null;
  try { payload = JSON.parse(text); } catch { /* report non-JSON response below */ }
  if (!response.ok || !payload) {
    throw new Error(`${method} ${url} failed (${response.status}): ${text.slice(0, 500)}`);
  }
  return payload;
}

async function pollRun(url, runId, { timeoutMs, pollMs }) {
  const deadline = Date.now() + timeoutMs;
  while (true) {
    const run = await fetchJson(`${url}/api/runs/${encodeURIComponent(runId)}`, {
      timeoutMs: Math.min(15_000, timeoutMs),
    });
    if (TERMINAL.has(run.status)) return run;
    if (Date.now() >= deadline) throw new Error(`Web smoke timed out after ${timeoutMs} ms`);
    await new Promise((resolve) => setTimeout(resolve, pollMs));
  }
}

function normalizeUrl(value) {
  const parsed = new URL(String(value || ""));
  if (!/^https?:$/.test(parsed.protocol)) throw new Error("--url must use http or https");
  return parsed.href.replace(/\/$/, "");
}

export async function runWebCapabilitySmoke(options = {}) {
  const url = normalizeUrl(options.url);
  const model = String(options.model || "").trim();
  if (!model) throw new Error("--model is required");
  const normalized = {
    url,
    model,
    effort: String(options.effort || "low"),
    prompt: String(options.prompt || DEFAULT_PROMPT),
    timeoutMs: Number(options.timeoutMs || 120_000),
    pollMs: Number(options.pollMs || 1_000),
  };
  if (options.execute !== true) return { dryRun: true, ...normalized, webPolicy: "required" };

  const config = await fetchJson(`${url}/api/config`, { timeoutMs: 15_000 });
  if (config.appServerReady !== true) throw new Error("Debug UI App Server is not ready");
  if (config.providerCapabilities?.checked !== true) {
    throw new Error("Model-provider web capability is unverified; refusing a paid smoke run");
  }
  if (config.providerCapabilities.webSearch !== true) {
    throw new Error("Model provider reports webSearch=false; the Codex App Server cannot expose native live web search for this provider");
  }
  if (!config.models?.some((entry) => entry.id === model || entry.model === model)) {
    throw new Error(`Requested model unavailable: ${model}`);
  }

  const started = await fetchJson(`${url}/api/runs`, {
    method: "POST",
    timeoutMs: 180_000,
    body: {
      prompt: normalized.prompt,
      model,
      effort: normalized.effort,
      summary: "none",
      networkAccess: true,
      webPolicy: "required",
    },
  });
  const run = await pollRun(url, started.id, normalized);
  if (run.status !== "completed") throw new Error(`Web smoke run ended with status ${run.status}`);
  const assessment = assertWebPolicy(run, "required");
  if (assessment.sourceCount === 0) {
    const error = new Error("Web search was called but returned no auditable HTTP source");
    error.code = "WEB_SMOKE_NO_SOURCES";
    throw error;
  }
  const result = {
    schemaVersion: 1,
    checkedAt: new Date().toISOString(),
    url,
    profile: config.profile?.name || null,
    provider: config.modelProvider || null,
    providerCapabilities: config.providerCapabilities,
    model,
    effort: normalized.effort,
    runId: run.id,
    status: run.status,
    assessment,
  };
  if (options.output) {
    const destination = path.resolve(options.output);
    await mkdir(path.dirname(destination), { recursive: true });
    await writeFile(destination, `${JSON.stringify(result, null, 2)}\n`, "utf8");
  }
  return result;
}

export function parseArguments(argv) {
  const options = {};
  const values = new Set(["url", "model", "effort", "prompt", "timeout-ms", "poll-ms", "output"]);
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--execute") { options.execute = true; continue; }
    if (token === "--dry-run") { options.execute = false; continue; }
    if (token === "--help") { options.help = true; continue; }
    if (!token.startsWith("--") || !values.has(token.slice(2))) throw new Error(`Unknown argument: ${token}`);
    const value = argv[index + 1];
    if (value === undefined || value.startsWith("--")) throw new Error(`Missing value for ${token}`);
    const key = token.slice(2).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    options[key] = value;
    index += 1;
  }
  return options;
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  if (options.help) {
    process.stdout.write([
      "Dry by default: verifies nothing and makes no model call.",
      "Live check: --execute --url http://127.0.0.1:4318 --model MODEL [--effort low] [--output FILE]",
      "The live check first verifies the provider capability RPC, then requires an observed webSearch event with an HTTP source.",
      "",
    ].join("\n"));
    return;
  }
  const result = await runWebCapabilitySmoke(options);
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
