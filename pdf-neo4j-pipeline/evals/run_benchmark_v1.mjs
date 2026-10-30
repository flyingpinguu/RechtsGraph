#!/usr/bin/env node
import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { createWriteStream, existsSync } from "node:fs";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { runPairedEvaluation } from "./run_paired_eval.mjs";

const EVALS_DIR = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_DIR = path.dirname(EVALS_DIR);
const DEBUG_UI_DIR = path.join(PROJECT_DIR, "debug_ui");
const BENCHMARK_DIR = path.join(EVALS_DIR, "benchmark_v1");
const DEFAULT_FIXTURE = path.join(BENCHMARK_DIR, "fixture.json");
const PROFILE_PAIRS = Object.freeze({
  tools: ["lean", "web_only"],
  graph: ["lean", "local_text_only"],
  instructions: ["web_only", "web_baseline"],
});

function isoForPath() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}

function parseArguments(argv) {
  const options = {
    comparison: "tools",
    provider: "openrouter",
    model: "z-ai/glm-5.3-flash",
    effort: "low",
    timeoutMs: 600_000,
  };
  let explicitRunMode = null;
  const valueOptions = new Set([
    "comparison", "provider", "model", "effort", "fixture-path",
    "output-directory", "resume-directory", "left-url", "right-url",
    "timeout-ms", "poll-ms", "request-timeout-ms", "start-timeout-ms",
    "suite", "candidate-repeats",
  ]);
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--execute" || token === "--dry-run") {
      const requested = token === "--execute";
      if (explicitRunMode !== null && explicitRunMode !== requested) {
        throw new Error("Use either --execute or --dry-run");
      }
      explicitRunMode = requested;
      options.execute = requested;
      continue;
    }
    if (token === "--managed") { options.managed = true; continue; }
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
  if (!PROFILE_PAIRS[options.comparison]) {
    throw new Error("--comparison must be tools, graph, or instructions");
  }
  if (!['openrouter', 'codex-default'].includes(options.provider)) {
    throw new Error("--provider must be openrouter or codex-default");
  }
  for (const key of ["timeoutMs", "pollMs", "requestTimeoutMs", "startTimeoutMs"]) {
    if (options[key] !== undefined) {
      options[key] = Number(options[key]);
      if (!Number.isFinite(options[key]) || options[key] <= 0) throw new Error(`Invalid --${key}`);
    }
  }
  if (options.candidateRepeats !== undefined) {
    options.candidateRepeats = Number(options.candidateRepeats);
    if (!Number.isInteger(options.candidateRepeats) || options.candidateRepeats < 1 || options.candidateRepeats > 5) {
      throw new Error("Invalid --candidate-repeats; expected an integer from 1 to 5");
    }
  }
  if (options.execute && !options.managed && (!options.leftUrl || !options.rightUrl)) {
    options.managed = true;
  }
  return options;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close((error) => error ? reject(error) : resolve(port));
    });
  });
}

async function waitUntilReady(url, child, timeoutMs = 90_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError = "server not ready";
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`Managed server exited with code ${child.exitCode}`);
    try {
      const response = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(2_000) });
      const payload = await response.json();
      if (response.ok && payload.ok === true) return payload;
      lastError = JSON.stringify(payload);
    } catch (error) {
      lastError = error.message;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`Managed server did not become ready: ${lastError}`);
}

async function startManagedServer({ profile, port, model, provider, runtimeDirectory }) {
  await mkdir(runtimeDirectory, { recursive: true });
  const stdout = createWriteStream(path.join(runtimeDirectory, `${profile}.stdout.log`), { flags: "a" });
  const stderr = createWriteStream(path.join(runtimeDirectory, `${profile}.stderr.log`), { flags: "a" });
  const env = {
    ...process.env,
    RECHTSGRAPH_HARNESS_PROFILE: profile,
    RECHTSGRAPH_DEBUG_PORT: String(port),
    RECHTSGRAPH_RUN_DIRECTORY: path.join(runtimeDirectory, `${profile}-runs`),
  };
  if (provider === "openrouter") {
    if (!env.OPENROUTER_API_KEY) {
      throw new Error("OPENROUTER_API_KEY is not set in the environment");
    }
    env.RECHTSGRAPH_MODEL_PROVIDER = "openrouter";
    env.RECHTSGRAPH_CUSTOM_MODEL = model;
  } else {
    delete env.RECHTSGRAPH_MODEL_PROVIDER;
    delete env.RECHTSGRAPH_CUSTOM_MODEL;
  }
  const child = spawn(process.execPath, [path.join(DEBUG_UI_DIR, "server.mjs")], {
    cwd: DEBUG_UI_DIR,
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.pipe(stdout);
  child.stderr.pipe(stderr);
  const url = `http://127.0.0.1:${port}`;
  try {
    await waitUntilReady(url, child);
  } catch (error) {
    child.kill("SIGTERM");
    throw error;
  }
  return {
    profile,
    url,
    child,
    close() {
      stdout.end();
      stderr.end();
      if (child.exitCode === null) child.kill("SIGTERM");
    },
  };
}

async function archiveDefinition(directory, benchmark) {
  const configDirectory = path.join(directory, "benchmark-definition");
  await mkdir(configDirectory, { recursive: true });
  const hashes = {};
  for (const filename of benchmark.definitionFiles) {
    const source = await readFile(path.join(benchmark.directory, filename));
    const destination = path.join(configDirectory, filename);
    try {
      const existing = await readFile(destination);
      if (!existing.equals(source)) throw new Error(`Frozen benchmark definition changed: ${filename}`);
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      await writeFile(destination, source, { flag: "wx" });
    }
    hashes[filename] = createHash("sha256").update(source).digest("hex");
  }
  const metadataPath = path.join(directory, "benchmark-definition.json");
  const metadata = `${JSON.stringify({ schemaVersion: 1, benchmarkVersion: benchmark.version, files: hashes }, null, 2)}\n`;
  try {
    const existing = await readFile(metadataPath, "utf8");
    if (existing !== metadata) throw new Error("Frozen benchmark metadata changed");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
    await writeFile(metadataPath, metadata, { flag: "wx" });
  }
}

function commandOutput(command, args, cwd = PROJECT_DIR) {
  const completed = spawnSync(command, args, { cwd, encoding: "utf8", env: process.env, maxBuffer: 10 * 1024 * 1024 });
  return completed.status === 0
    ? { ok: true, stdout: completed.stdout.trim() }
    : { ok: false, status: completed.status, error: (completed.stderr || completed.stdout || "command failed").trim() };
}

async function artifactSnapshot(file, { hash = true } = {}) {
  const resolved = path.resolve(file);
  try {
    const details = await stat(resolved);
    const snapshot = {
      path: resolved,
      sizeBytes: details.size,
      modifiedAt: details.mtime.toISOString(),
    };
    if (hash) snapshot.sha256 = createHash("sha256").update(await readFile(resolved)).digest("hex");
    return snapshot;
  } catch (error) {
    return { path: resolved, missing: true, error: error.message };
  }
}

async function snapshotEnvironment(directory, normalized) {
  const destination = path.join(directory, "environment-snapshot.json");
  if (existsSync(destination)) return;
  const bundledPython = path.join(PROJECT_DIR, ".venv-mcp", "bin", "python");
  const python = process.env.RECHTSGRAPH_PYTHON || (existsSync(bundledPython) ? bundledPython : "python3");
  const serviceStatus = commandOutput(python, [
    "-c",
    "import json, sqlite3; from rechtsgraph_mcp.server import _index_path, _external_register_path, _neo4j; p=_index_path(); c=sqlite3.connect('file:'+str(p.resolve())+'?mode=ro', uri=True); m=dict(c.execute(\"SELECT key,value FROM metadata WHERE key IN ('schema_version','build_stats','external_register_path')\")); c.close(); print(json.dumps({'retrieval': {'path': str(p), **m}, 'external_register': {'path': str(_external_register_path())}, 'neo4j': _neo4j().status()}, ensure_ascii=False, sort_keys=True))",
  ]);
  let parsedServiceStatus = serviceStatus;
  if (serviceStatus.ok) {
    try { parsedServiceStatus = JSON.parse(serviceStatus.stdout); } catch { /* retain auditable raw output */ }
  }
  const trackedArtifacts = [
    path.join(DEBUG_UI_DIR, "profiles.mjs"),
    path.join(DEBUG_UI_DIR, "LEGAL_RESEARCH_GUIDE.md"),
    path.join(DEBUG_UI_DIR, "web_policy.mjs"),
    path.join(DEBUG_UI_DIR, "server.mjs"),
    path.join(EVALS_DIR, "run_paired_eval.mjs"),
    path.join(PROJECT_DIR, "rechtsgraph_mcp", "server.py"),
    path.join(PROJECT_DIR, "rechtsgraph_mcp", "index.py"),
    path.join(PROJECT_DIR, "rechtsgraph_mcp", "AGENT_GUIDE.md"),
    path.join(PROJECT_DIR, "output", "gii_xml_full_chunked", "manifest.json"),
    path.join(PROJECT_DIR, "output", "eurlex_formex_rl", "content_graph_manifest.json"),
  ];
  const largeCorpusArtifacts = [
    path.join(PROJECT_DIR, "output", "retrieval", "rechtsgraph.sqlite"),
    path.join(path.dirname(PROJECT_DIR), "external_documents", "register.sqlite"),
  ];
  const payload = {
    schemaVersion: 1,
    capturedAt: new Date().toISOString(),
    execution: {
      provider: normalized.provider,
      model: normalized.model,
      effort: normalized.effort,
      comparison: normalized.comparison,
      selectedCaseIds: normalized.caseIds,
      timeoutMs: normalized.timeoutMs,
    },
    git: {
      head: commandOutput("git", ["rev-parse", "HEAD"], path.dirname(PROJECT_DIR)),
      status: commandOutput("git", ["status", "--porcelain=v1"], path.dirname(PROJECT_DIR)),
    },
    runtime: {
      node: process.version,
      python: commandOutput(python, ["--version"]),
    },
    serviceStatus: parsedServiceStatus,
    neo4jConfiguration: {
      url: process.env.RECHTSGRAPH_NEO4J_URL || "http://localhost:7474",
      database: process.env.RECHTSGRAPH_NEO4J_DATABASE || "gii-xml-chunked",
      authenticationConfigured: Boolean(process.env.RECHTSGRAPH_NEO4J_USERNAME || process.env.RECHTSGRAPH_NEO4J_PASSWORD),
    },
    harnessArtifacts: await Promise.all(trackedArtifacts.map((file) => artifactSnapshot(file))),
    corpusArtifacts: await Promise.all(largeCorpusArtifacts.map((file) => artifactSnapshot(file, { hash: false }))),
    note: "Large SQLite corpora are identified by path, byte size, mtime, schema/build status and document counts; they are not re-hashed for every run.",
  };
  await writeFile(destination, `${JSON.stringify(payload, null, 2)}\n`, { flag: "wx" });
}

async function resumePorts(directory) {
  const manifest = JSON.parse(await readFile(path.join(directory, "manifest.json"), "utf8"));
  const left = new URL(manifest.left?.url);
  const right = new URL(manifest.right?.url);
  return {
    leftPort: Number(left.port),
    rightPort: Number(right.port),
    leftProfile: manifest.left?.profile,
    rightProfile: manifest.right?.profile,
  };
}

function verifyDefinition(benchmark) {
  const bundledPython = path.join(PROJECT_DIR, ".venv-mcp", "bin", "python");
  const python = process.env.RECHTSGRAPH_PYTHON || (existsSync(bundledPython) ? bundledPython : "python3");
  const completed = spawnSync(
    python,
    [path.join(benchmark.directory, "benchmark.py"), benchmark.preflightCommand],
    { cwd: PROJECT_DIR, encoding: "utf8", env: process.env, maxBuffer: 10 * 1024 * 1024 },
  );
  if (completed.status !== 0) {
    throw new Error(`Benchmark definition verification failed: ${(completed.stderr || completed.stdout).trim()}`);
  }
  const payload = JSON.parse(completed.stdout);
  if (payload.ok !== true) throw new Error("Benchmark definition verification failed");
  return payload;
}

export async function runBenchmark(options = {}) {
  const benchmark = {
    directory: path.resolve(options.benchmarkDirectory || BENCHMARK_DIR),
    version: options.benchmarkVersion ?? 1,
    outputPrefix: options.outputPrefix || "benchmark-v1",
    preflightCommand: options.preflightCommand || "verify-gold",
    verificationFilename: options.verificationFilename || "corpus-gold-verification.json",
    definitionFiles: options.definitionFiles || [
      "fixture.json", "rubric.json", "gold_sources.json", "pricing.json", "README.md", "benchmark.py",
    ],
  };
  const normalized = {
    comparison: options.comparison || "tools",
    provider: options.provider || "openrouter",
    model: options.model || "z-ai/glm-5.3-flash",
    effort: options.effort || "low",
    fixturePath: path.resolve(options.fixturePath || path.join(benchmark.directory, "fixture.json")),
    timeoutMs: Number(options.timeoutMs || 600_000),
    pollMs: Number(options.pollMs || 2_000),
    requestTimeoutMs: Number(options.requestTimeoutMs || 15_000),
    startTimeoutMs: Number(options.startTimeoutMs || 180_000),
    execute: options.execute === true,
    managed: options.managed === true,
    caseIds: options.caseIds,
  };
  if (!PROFILE_PAIRS[normalized.comparison]) throw new Error("Unknown comparison");
  if (!normalized.execute) {
    const plan = await runPairedEvaluation({
      fixturePath: normalized.fixturePath,
      comparison: normalized.comparison,
      model: normalized.model,
      effort: normalized.effort,
      caseIds: normalized.caseIds,
    });
    return { ...plan, provider: normalized.provider, benchmarkVersion: benchmark.version };
  }

  const outputDirectory = path.resolve(
    options.resumeDirectory
      || options.outputDirectory
      || path.join(EVALS_DIR, "results", `${benchmark.outputPrefix}-${isoForPath()}`),
  );
  const servers = [];
  let leftUrl = options.leftUrl;
  let rightUrl = options.rightUrl;
  try {
    await archiveDefinition(outputDirectory, benchmark);
    const definitionVerification = verifyDefinition(benchmark);
    await writeFile(
      path.join(outputDirectory, benchmark.verificationFilename),
      `${JSON.stringify(definitionVerification, null, 2)}\n`,
    );
    await snapshotEnvironment(outputDirectory, normalized);
    if (normalized.managed) {
      const runtimeDirectory = path.join(outputDirectory, "runtime");
      let [leftProfile, rightProfile] = PROFILE_PAIRS[normalized.comparison];
      let leftPort;
      let rightPort;
      if (options.resumeDirectory) {
        const resume = await resumePorts(outputDirectory);
        ({ leftPort, rightPort } = resume);
        if (resume.leftProfile !== leftProfile || resume.rightProfile !== rightProfile) {
          throw new Error("Resume profile pair does not match --comparison");
        }
      } else {
        leftPort = await freePort();
        rightPort = await freePort();
        while (rightPort === leftPort) rightPort = await freePort();
      }
      const left = await startManagedServer({
        profile: leftProfile, port: leftPort, model: normalized.model,
        provider: normalized.provider, runtimeDirectory,
      });
      servers.push(left);
      const right = await startManagedServer({
        profile: rightProfile, port: rightPort, model: normalized.model,
        provider: normalized.provider, runtimeDirectory,
      });
      servers.push(right);
      leftUrl = left.url;
      rightUrl = right.url;
    }
    if (!leftUrl || !rightUrl) throw new Error("Two server URLs are required without --managed");
    const result = await runPairedEvaluation({
      execute: true,
      comparison: normalized.comparison,
      fixturePath: normalized.fixturePath,
      outputDirectory: options.resumeDirectory ? undefined : outputDirectory,
      resumeDirectory: options.resumeDirectory ? outputDirectory : undefined,
      leftUrl,
      rightUrl,
      model: normalized.model,
      effort: normalized.effort,
      timeoutMs: normalized.timeoutMs,
      pollMs: normalized.pollMs,
      requestTimeoutMs: normalized.requestTimeoutMs,
      startTimeoutMs: normalized.startTimeoutMs,
      caseIds: normalized.caseIds,
    });
    return { ...result, provider: normalized.provider, benchmarkVersion: benchmark.version };
  } finally {
    for (const server of servers.reverse()) server.close();
  }
}

async function main() {
  try {
    const options = parseArguments(process.argv.slice(2));
    if (options.help) {
      process.stdout.write([
        "RechtsGraph compact benchmark v1 (dry-run unless --execute is present).",
        "",
        "Default live run: --execute --managed --provider openrouter --model z-ai/glm-5.3-flash",
        "Comparisons: tools (primary), graph (optional ablation), instructions (optional).",
        "Set OPENROUTER_API_KEY in the environment; never pass it as an argument.",
        "",
      ].join("\n"));
      return;
    }
    if (options.suite || options.candidateRepeats) {
      throw new Error("--suite and --candidate-repeats are available only in run_legal_competence_v1.mjs");
    }
    const result = await runBenchmark(options);
    process.stdout.write(
      result.dryRun
        ? `${JSON.stringify(result, null, 2)}\n`
        : `Benchmark saved: ${result.directory}\nPrepare blind review with: .venv-mcp/bin/python evals/benchmark_v1/benchmark.py prepare-review ${result.directory}\n`,
    );
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) main();

export { parseArguments };
