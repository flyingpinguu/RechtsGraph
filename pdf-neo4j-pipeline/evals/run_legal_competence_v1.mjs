#!/usr/bin/env node
import path from "node:path";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";

import { parseArguments, runBenchmark } from "./run_benchmark_v1.mjs";

const EVALS_DIR = path.dirname(fileURLToPath(import.meta.url));
const BENCHMARK_DIR = path.join(EVALS_DIR, "legal_competence_v1");

const BENCHMARK_OPTIONS = Object.freeze({
  benchmarkDirectory: BENCHMARK_DIR,
  benchmarkVersion: "legal-competence-v1",
  outputPrefix: "legal-competence-v1",
  preflightCommand: "validate",
  verificationFilename: "benchmark-validation.json",
  definitionFiles: [
    "fixture.json",
    "rubric.json",
    "judge_prompt.md",
    "RESEARCH_BASIS.md",
    "README.md",
    "benchmark.py",
    "source_passages.json",
  ],
});

export async function loadProjectOpenRouterKey(envPath = path.join(path.dirname(EVALS_DIR), ".env")) {
  if (process.env.OPENROUTER_API_KEY) return;
  let contents;
  try {
    contents = await readFile(envPath, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return;
    throw new Error("Unable to read local project .env");
  }
  for (const line of contents.split(/\r?\n/)) {
    const match = line.match(/^\s*OPENROUTER_API_KEY\s*=\s*(.*?)\s*$/);
    if (!match) continue;
    let value = match[1];
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    if (value) process.env.OPENROUTER_API_KEY = value;
    return;
  }
}

function isoForPath() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}

async function suiteSelection(options) {
  const fixture = JSON.parse(await readFile(path.join(BENCHMARK_DIR, "fixture.json"), "utf8"));
  const candidateRepeats = Number(options.candidateRepeats || 1);
  const suite = options.suite || (candidateRepeats > 1 ? "stability" : "smoke");
  const caseIds = fixture.suites?.[suite];
  if (!Array.isArray(caseIds) || caseIds.length === 0) {
    throw new Error(`Unknown --suite ${suite}; expected one of ${Object.keys(fixture.suites || {}).join(", ")}`);
  }
  if (!Number.isInteger(candidateRepeats) || candidateRepeats < 1 || candidateRepeats > 5) {
    throw new Error("--candidate-repeats must be an integer from 1 to 5");
  }
  if (candidateRepeats > 1 && suite !== "stability") {
    throw new Error("Candidate repetitions are deliberately restricted to --suite stability");
  }
  return { suite, caseIds, candidateRepeats };
}

export async function runLegalCompetenceBenchmark(options = {}) {
  if (options.comparison && options.comparison !== "tools") {
    throw new Error("The legal-competence benchmark supports only the primary tools comparison (lean vs web_only)");
  }
  if (options.execute === true && (options.provider || "openrouter") === "openrouter") {
    await loadProjectOpenRouterKey();
  }
  const selection = await suiteSelection(options);
  const timeoutMs = options.timeoutMs ?? (["smoke", "stability"].includes(selection.suite) ? 180_000 : 600_000);
  const common = {
    ...options,
    ...BENCHMARK_OPTIONS,
    timeoutMs,
    caseIds: selection.caseIds,
    outputPrefix: `${BENCHMARK_OPTIONS.outputPrefix}-${selection.suite}`,
  };
  if (selection.candidateRepeats === 1) {
    const result = await runBenchmark(common);
    return { ...result, suite: selection.suite, candidateRepeats: 1, timeoutMs,
      maximumCandidateWaitMs: timeoutMs * selection.caseIds.length * 2 };
  }

  if (options.execute !== true) {
    const single = await runBenchmark({ ...common, execute: false });
    return {
      ...single,
      suite: selection.suite,
      candidateRepeats: selection.candidateRepeats,
      timeoutMs,
      maximumCandidateWaitMs: timeoutMs * selection.caseIds.length * 2 * selection.candidateRepeats,
      plannedAgentRuns: single.plannedAgentRuns * selection.candidateRepeats,
      plannedModelTurns: {
        minimum: single.plannedModelTurns.minimum * selection.candidateRepeats,
        maximum: single.plannedModelTurns.maximum * selection.candidateRepeats,
      },
      plan: Array.from({ length: selection.candidateRepeats }, (_, index) => single.plan.map((entry) => ({
        ...entry,
        candidateRepeat: index + 1,
      }))).flat(),
    };
  }

  const groupDirectory = path.resolve(
    options.resumeDirectory
      || options.outputDirectory
      || path.join(EVALS_DIR, "results", `legal-competence-stability-${isoForPath()}`),
  );
  await mkdir(groupDirectory, { recursive: true });
  const groupManifestPath = path.join(groupDirectory, "candidate-repetition-manifest.json");
  const expectedGroup = {
    schemaVersion: 1,
    suite: selection.suite,
    candidateRepeats: selection.candidateRepeats,
    selectedCaseIds: selection.caseIds,
    provider: options.provider || "openrouter",
    model: options.model || "z-ai/glm-5.3-flash",
    effort: options.effort || "low",
    timeoutMs,
  };
  if (existsSync(groupManifestPath)) {
    const existing = JSON.parse(await readFile(groupManifestPath, "utf8"));
    for (const [key, value] of Object.entries(expectedGroup)) {
      if (JSON.stringify(existing[key]) !== JSON.stringify(value)) {
        throw new Error(`Candidate-repeat resume mismatch at ${key}`);
      }
    }
  } else {
    await writeFile(groupManifestPath, `${JSON.stringify({ ...expectedGroup, createdAt: new Date().toISOString() }, null, 2)}\n`, { flag: "wx" });
  }

  const repetitions = [];
  for (let repeat = 1; repeat <= selection.candidateRepeats; repeat += 1) {
    const repeatDirectory = path.join(groupDirectory, `candidate-repeat-${String(repeat).padStart(2, "0")}`);
    const resumable = existsSync(path.join(repeatDirectory, "manifest.json"));
    const result = await runBenchmark({
      ...common,
      outputDirectory: resumable ? undefined : repeatDirectory,
      resumeDirectory: resumable ? repeatDirectory : undefined,
    });
    repetitions.push({ repeat, directory: result.directory, summary: result.summary });
  }
  return {
    dryRun: false,
    directory: groupDirectory,
    suite: selection.suite,
    candidateRepeats: selection.candidateRepeats,
    selectedCaseIds: selection.caseIds,
    repetitions,
  };
}

async function main() {
  try {
    const argv = process.argv.slice(2);
    const options = parseArguments(argv);
    // The shared parser retains the old full-suite timeout for other runners.
    if (!argv.includes("--timeout-ms")) delete options.timeoutMs;
    if (options.help) {
      process.stdout.write([
        "RechtsGraph legal-competence benchmark v1 (dry-run unless --execute is present).",
        "",
        "Primary comparison: the same guided, web-enabled assistant with RechtsGraph (lean) versus without it (web_only).",
        "Default: --suite smoke (3 single-turn tasks, 6 fresh sessions, 180s wait per session).",
        "Milestone: --suite full (12 tasks, 24 sessions).",
        "Legacy: --suite quick (5 tasks). Stability: the same 3 smoke tasks, --candidate-repeats 2.",
        "Default live run: --execute --managed --provider openrouter --model z-ai/glm-5.3-flash",
        "OPENROUTER_API_KEY: environment or ignored project .env; never pass it as an argument.",
        "",
      ].join("\n"));
      return;
    }
    const result = await runLegalCompetenceBenchmark(options);
    if (result.dryRun) {
      process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    } else if (result.candidateRepeats > 1) {
      process.stdout.write([
        `Candidate repetitions saved: ${result.directory}`,
        ...result.repetitions.map((entry) => `Prepare repeat ${entry.repeat}: .venv-mcp/bin/python evals/legal_competence_v1/benchmark.py prepare-judge ${entry.directory}`),
        "After judging and aggregating each repeat, aggregate stability with: .venv-mcp/bin/python evals/legal_competence_v1/benchmark.py aggregate-repetitions <group-directory>",
        "",
      ].join("\n"));
    } else {
      process.stdout.write(`Benchmark saved: ${result.directory}\nPrepare blind judging with: .venv-mcp/bin/python evals/legal_competence_v1/benchmark.py prepare-judge ${result.directory}\n`);
    }
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) main();
