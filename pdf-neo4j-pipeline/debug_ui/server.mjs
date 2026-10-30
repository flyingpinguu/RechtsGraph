import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath, pathToFileURL } from "node:url";

import { resolveAgentProfile, validateMcpInventory } from "./profiles.mjs";
import { configuredModelEntries, providerMetadata } from "./provider_config.mjs";
import {
  addEvent,
  createRun,
  nowIso,
  normalizeTurnStatus,
  serializeRun,
  summarizeRun,
  updateRunFromMessage,
} from "./run_model.mjs";
import { RunStore } from "./run_store.mjs";
import {
  instructionsForWebPolicy,
  normalizeWebPolicy,
} from "./web_policy.mjs";

export { createRun, serializeRun, updateRunFromMessage } from "./run_model.mjs";

const MODULE_DIR = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_DIR = path.resolve(MODULE_DIR, "..");
const PUBLIC_DIR = path.join(MODULE_DIR, "public");
const DEFAULT_RUN_DIRECTORY = path.join(PROJECT_DIR, "output", "debug_ui", "runs");
const DEFAULT_CODEX_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex";
const CODEX_BIN = process.env.RECHTSGRAPH_CODEX_BIN
  || (existsSync(DEFAULT_CODEX_BIN) ? DEFAULT_CODEX_BIN : "codex");
const HOST = "127.0.0.1";
const PORT = Number.parseInt(process.env.RECHTSGRAPH_DEBUG_PORT || "4317", 10);
const MAX_BODY_BYTES = 1_000_000;
const ACTIVE_STATUSES = new Set(["starting", "running", "interrupting"]);

const STATIC_FILES = new Map([
  ["/", ["index.html", "text/html; charset=utf-8"]],
  ["/index.html", ["index.html", "text/html; charset=utf-8"]],
  ["/app.js", ["app.js", "text/javascript; charset=utf-8"]],
  ["/styles.css", ["styles.css", "text/css; charset=utf-8"]],
]);

class CodexAppServer {
  constructor({ binary, cwd, profile, onRuntimeEvent }) {
    this.binary = binary;
    this.cwd = cwd;
    this.profile = profile;
    this.onRuntimeEvent = onRuntimeEvent;
    this.proc = null;
    this.pending = new Map();
    this.nextId = 1;
    this.ready = false;
    this.userAgent = null;
    this.models = [];
    this.mcpServers = [];
    this.providerCapabilities = {
      checked: false,
      webSearch: null,
      imageGeneration: null,
      namespaceTools: null,
      error: null,
    };
    this.stderrTail = [];
    this.messageListeners = new Set();
  }

  async start() {
    if (this.ready) return;
    this.proc = spawn(this.binary, [...this.profile.appServerArgs, "app-server", "--stdio"], {
      cwd: this.cwd,
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });

    const stdout = readline.createInterface({ input: this.proc.stdout });
    stdout.on("line", (line) => this.handleLine(line));
    this.proc.stderr.setEncoding("utf8");
    this.proc.stderr.on("data", (chunk) => {
      const text = String(chunk);
      this.stderrTail.push(text);
      if (this.stderrTail.length > 100) this.stderrTail.shift();
      this.onRuntimeEvent?.({ type: "stderr", at: nowIso(), text });
    });
    this.proc.on("error", (error) => this.handleExit(error));
    this.proc.on("exit", (code, signal) => {
      if (code !== 0 || signal) this.handleExit(new Error(`Codex App Server beendet: code=${code}, signal=${signal}`));
      this.ready = false;
    });

    const initialize = await this.request("initialize", {
      clientInfo: {
        name: "rechtsgraph_debug_ui",
        title: "RechtsGraph Debug UI",
        version: "0.2.0",
      },
      capabilities: {},
    }, 30_000);
    this.userAgent = initialize?.userAgent || null;
    this.notify("initialized", {});
    const modelResult = await this.request("model/list", { limit: 100, includeHidden: false }, 30_000);
    this.models = modelResult?.data || [];
    for (const entry of configuredModelEntries()) {
      if (!this.models.some((candidate) => candidate.id === entry.id || candidate.model === entry.id)) {
        this.models.push(entry);
      }
    }
    try {
      const capabilities = await this.request("modelProvider/capabilities/read", {}, 30_000);
      this.providerCapabilities = {
        checked: true,
        webSearch: capabilities?.webSearch === true,
        imageGeneration: capabilities?.imageGeneration === true,
        namespaceTools: capabilities?.namespaceTools === true,
        error: null,
      };
    } catch (error) {
      // Older app-server builds may not expose the capability method. Keep the
      // UI usable, but make the missing verification explicit to eval runners.
      this.providerCapabilities = {
        checked: false,
        webSearch: null,
        imageGeneration: null,
        namespaceTools: null,
        error: error.message,
      };
    }
    this.mcpServers = await this.listMcpServers();
    validateMcpInventory(this.profile.name, this.mcpServers);
    this.ready = true;
    this.onRuntimeEvent?.({ type: "ready", at: nowIso(), userAgent: this.userAgent });
  }

  handleLine(line) {
    let message;
    try {
      message = JSON.parse(line);
    } catch (error) {
      this.onRuntimeEvent?.({ type: "protocol_parse_error", at: nowIso(), line, error: error.message });
      return;
    }

    if (Object.hasOwn(message, "id") && !message.method) {
      const pending = this.pending.get(message.id);
      if (pending) {
        clearTimeout(pending.timer);
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message || JSON.stringify(message.error)));
        else pending.resolve(message.result);
      }
    } else if (Object.hasOwn(message, "id") && message.method) {
      this.handleServerRequest(message);
    }

    for (const listener of this.messageListeners) listener(message);
  }

  handleServerRequest(message) {
    const declining = new Map([
      ["item/commandExecution/requestApproval", { decision: "decline" }],
      ["item/fileChange/requestApproval", { decision: "decline" }],
      ["mcpServer/elicitation/request", { action: "decline", content: null }],
      ["item/tool/requestUserInput", { answers: {} }],
    ]);
    const result = declining.get(message.method);
    if (result) {
      this.write({ id: message.id, result });
      this.onRuntimeEvent?.({
        type: "server_request_auto_declined",
        at: nowIso(),
        method: message.method,
      });
      return;
    }
    this.write({
      id: message.id,
      error: { code: -32601, message: `Nicht unterstützte Serveranfrage: ${message.method}` },
    });
  }

  handleExit(error) {
    this.ready = false;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
    this.onRuntimeEvent?.({ type: "process_error", at: nowIso(), error: error.message });
  }

  write(message) {
    if (!this.proc?.stdin?.writable) throw new Error("Codex App Server ist nicht schreibbereit");
    this.proc.stdin.write(`${JSON.stringify(message)}\n`);
  }

  notify(method, params) {
    this.write({ method, params });
  }

  request(method, params = {}, timeoutMs = 120_000) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Timeout bei App-Server-Methode ${method}`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer, method });
      this.write({ method, id, params });
    });
  }

  onMessage(listener) {
    this.messageListeners.add(listener);
    return () => this.messageListeners.delete(listener);
  }

  async listMcpServers() {
    const servers = [];
    const effective = await this.request("config/read", { includeLayers: false }, 30_000);
    const configured = effective?.config?.mcp_servers;
    if (!configured || typeof configured !== "object") throw new Error("Effective MCP configuration unavailable");
    let cursor = null;
    do {
      const result = await this.request("mcpServerStatus/list", {
        cursor,
        detail: "toolsAndAuthOnly",
        limit: 100,
      }, 30_000);
      servers.push(...(result?.data || []).map((server) => ({
        name: server.name,
        pluginId: server.pluginId || null,
        runtimeStatus: server.runtimeStatus || null,
        toolCount: Object.keys(server.tools || {}).length,
        toolNames: Object.keys(server.tools || {}).sort(),
        enabled: configured[server.name]?.enabled !== false,
      })));
      cursor = result?.nextCursor || null;
    } while (cursor);
    for (const [name, config] of Object.entries(configured)) {
      if (!servers.some((server) => server.name === name)) {
        servers.push({ name, enabled: config.enabled !== false, toolCount: 0, toolNames: [], runtimeStatus: null });
      }
    }
    return servers;
  }

  async stop() {
    if (!this.proc) return;
    this.proc.kill("SIGTERM");
    this.proc = null;
    this.ready = false;
  }
}

function sendJson(res, status, value) {
  const body = JSON.stringify(value);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
  });
  res.end(body);
}

function sendSse(res, eventName, value) {
  res.write(`event: ${eventName}\n`);
  res.write(`data: ${JSON.stringify(value)}\n\n`);
}

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function readJsonBody(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new Error("Request body ist zu groß");
    chunks.push(chunk);
  }
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function normalizeModel(model, availableModels) {
  const requested = String(model || "gpt-5.6-sol");
  const available = availableModels.find((entry) => entry.id === requested || entry.model === requested);
  if (!available) throw new Error(`Modell ist in dieser Codex-Anmeldung nicht verfügbar: ${requested}`);
  return available.id || available.model;
}

function normalizeEffort(effort, model, availableModels) {
  const entry = availableModels.find((candidate) => candidate.id === model || candidate.model === model);
  const supported = (entry?.supportedReasoningEfforts || []).map((item) => item.reasoningEffort);
  const requested = String(effort || entry?.defaultReasoningEffort || "medium");
  if (supported.length && !supported.includes(requested)) {
    throw new Error(`Reasoning effort ${requested} wird von ${model} nicht unterstützt`);
  }
  return requested;
}

export async function createDebugServer({
  port = PORT,
  appServer: providedAppServer = null,
  startAppServer = true,
  profile: providedProfile = null,
  runDirectory = process.env.RECHTSGRAPH_RUN_DIRECTORY || DEFAULT_RUN_DIRECTORY,
  runStore: providedRunStore = null,
} = {}) {
  const profile = providedProfile || resolveAgentProfile();
  const runs = new Map();
  const loadedThreadIds = new Set();
  const continuationLocks = new Set();
  const hydratedConversationIds = new Set();
  const runtimeEvents = [];
  const recordRuntimeEvent = (event) => {
    runtimeEvents.push(event);
    if (runtimeEvents.length > 200) runtimeEvents.shift();
  };
  const runStore = providedRunStore || new RunStore(runDirectory, {
    onError(error) {
      recordRuntimeEvent({ type: "run_store_error", at: nowIso(), error: error.message });
    },
  });
  await runStore.init();
  for (const storedRun of await runStore.loadRecent(250)) runs.set(storedRun.id, storedRun);
  const appServer = providedAppServer || new CodexAppServer({
    binary: CODEX_BIN,
    cwd: PROJECT_DIR,
    profile,
    onRuntimeEvent(event) {
      recordRuntimeEvent(event);
      for (const run of runs.values()) {
        if (ACTIVE_STATUSES.has(run.status)) {
          const record = addEvent(run, { method: "debug/runtime", params: event }, "runtime");
          broadcastRun(run, record);
          runStore.schedule(run);
        }
      }
    },
  });

  async function ensureConversationLoaded(conversationId) {
    if (hydratedConversationIds.has(conversationId)) return;
    for (const storedRun of await runStore.loadConversation(conversationId)) {
      if (!runs.has(storedRun.id)) runs.set(storedRun.id, storedRun);
    }
    hydratedConversationIds.add(conversationId);
  }

  function conversationRuns(conversationId) {
    const candidates = [...runs.values()].filter((run) => run.conversationId === conversationId);
    if (!candidates.length) return [];
    const rootId = candidates[0].rootRunId;
    if (!rootId || candidates.some((run) => run.rootRunId !== rootId)) {
      throw new HttpError(409, "Die gespeicherte Unterhaltung hat widersprüchliche Root-IDs");
    }
    const byId = new Map(candidates.map((run) => [run.id, run]));
    const root = byId.get(rootId);
    if (!root || root.parentRunId) {
      throw new HttpError(409, "Die gespeicherte Unterhaltung ist unvollständig: Root-Turn fehlt");
    }
    const children = new Map();
    for (const run of candidates) {
      if (!run.parentRunId) continue;
      const siblings = children.get(run.parentRunId) || [];
      siblings.push(run);
      children.set(run.parentRunId, siblings);
    }
    const ordered = [root];
    const visited = new Set([root.id]);
    let current = root;
    while (children.has(current.id)) {
      const next = children.get(current.id);
      if (next.length !== 1) {
        throw new HttpError(409, "Die gespeicherte Unterhaltung enthält mehrere Antwortzweige");
      }
      current = next[0];
      if (visited.has(current.id)) throw new HttpError(409, "Die gespeicherte Unterhaltung enthält einen Zyklus");
      visited.add(current.id);
      ordered.push(current);
    }
    if (visited.size !== candidates.length) {
      throw new HttpError(409, "Die gespeicherte Unterhaltung enthält verwaiste Turns");
    }
    return ordered;
  }

  function serializeConversation(conversationId) {
    const turns = conversationRuns(conversationId);
    if (!turns.length) return null;
    const root = turns.find((run) => run.id === run.rootRunId);
    if (!root) throw new HttpError(409, "Die gespeicherte Unterhaltung ist unvollständig: Root-Turn fehlt");
    const active = turns.find((run) => ACTIVE_STATUSES.has(run.status)) || null;
    const latest = turns.at(-1);
    return {
      id: conversationId,
      rootRunId: root.id,
      activeRunId: active?.id || null,
      latestRunId: latest.id,
      threadId: root.threadId || latest.threadId,
      resumable: Boolean(root.threadId) && turns.every((run) => run.threadEphemeral === false),
      model: root.model,
      effort: root.effort,
      summary: root.summary,
      networkAccess: root.networkAccess,
      profile: root.profile,
      webPolicy: root.webPolicy,
      turns: turns.map((run, index) => ({
        index: index + 1,
        runId: run.id,
        parentRunId: run.parentRunId,
        turnId: run.turnId,
        prompt: run.prompt,
        finalText: run.finalText,
        commentaryText: run.commentaryText,
        status: run.status,
        startedAt: run.startedAt,
        completedAt: run.completedAt,
        error: run.error,
        usage: run.usage,
        turnUsage: run.turnUsage,
      })),
    };
  }

  function presentRun(run, includeEvents = false) {
    return {
      ...serializeRun(run, includeEvents),
      conversation: serializeConversation(run.conversationId),
    };
  }

  function broadcastRun(run, event) {
    const payload = { event, run: presentRun(run, false) };
    for (const client of run.clients) sendSse(client, "update", payload);
  }

  function recordProtocolMessage(run, message) {
    const event = updateRunFromMessage(run, message);
    broadcastRun(run, event);
    if (["turn/completed", "error"].includes(message?.method)) {
      runStore.flush(run).catch((error) => recordRuntimeEvent({
        type: "run_store_error", at: nowIso(), error: error.message,
      }));
    } else {
      runStore.schedule(run);
    }
  }

  async function getRun(runId) {
    const inMemory = runs.get(runId);
    if (inMemory) {
      await ensureConversationLoaded(inMemory.conversationId);
      return inMemory;
    }
    const stored = await runStore.load(runId);
    if (stored) {
      runs.set(stored.id, stored);
      await ensureConversationLoaded(stored.conversationId);
    }
    return stored;
  }

  appServer.onMessage?.((message) => {
    const threadId = message?.params?.threadId
      || message?.params?.thread?.id
      || message?.params?.turn?.threadId;
    const turnId = message?.params?.turnId || message?.params?.turn?.id;
    const exactTurn = turnId
      ? [...runs.values()].find((candidate) => candidate.turnId === turnId)
      : null;
    const activeThreadRuns = !exactTurn && threadId
      ? [...runs.values()].filter((candidate) => (
        candidate.threadId === threadId && ACTIVE_STATUSES.has(candidate.status)
      ))
      : [];
    const run = exactTurn || (activeThreadRuns.length === 1 ? activeThreadRuns[0] : null);
    if (run) recordProtocolMessage(run, message);
  });

  async function startRun(body) {
    const prompt = String(body.prompt || "").trim();
    if (!prompt) throw new Error("Bitte eine Frage eingeben");
    if (prompt.length > 100_000) throw new Error("Die Frage ist zu lang");
    if (!appServer.ready) throw new Error("Codex App Server ist noch nicht bereit");

    const model = normalizeModel(body.model, appServer.models);
    const effort = normalizeEffort(body.effort || profile.defaultEffort, model, appServer.models);
    const summary = ["none", "auto", "concise", "detailed"].includes(body.summary)
      ? body.summary
      : profile.defaultSummary;
    const networkAccess = body.networkAccess !== false;
    const webPolicy = normalizeWebPolicy(body.webPolicy, profile.defaultWebPolicy || "optional");
    if (webPolicy === "required" && !networkAccess) {
      throw new Error("webPolicy=required needs networkAccess=true");
    }
    const developerInstructions = instructionsForWebPolicy(profile.developerInstructions, webPolicy);
    const run = createRun({
      prompt,
      model,
      effort,
      summary,
      networkAccess,
      profile: profile.name,
      webPolicy,
      baseInstructions: profile.baseInstructions,
      developerInstructions,
    });
    runs.set(run.id, run);
    hydratedConversationIds.add(run.conversationId);
    runStore.schedule(run);

    try {
      const threadParams = {
        model,
        cwd: PROJECT_DIR,
        approvalPolicy: "never",
        sandbox: "read-only",
        personality: "pragmatic",
        ephemeral: false,
        serviceName: "rechtsgraph_debug_ui",
        developerInstructions,
      };
      if (profile.baseInstructions) threadParams.baseInstructions = profile.baseInstructions;
      const threadResult = await appServer.request("thread/start", threadParams, 180_000);
      run.threadId = threadResult?.thread?.id;
      run.threadEphemeral = threadResult?.thread?.ephemeral === true;
      addEvent(run, {
        method: "debug/threadCreated",
        params: {
          thread: threadResult?.thread,
          instructionSources: threadResult?.instructionSources || [],
        },
      }, "client");
      if (!run.threadId) throw new Error("App Server hat keine threadId geliefert");
      if (run.threadEphemeral) throw new Error("App Server hat nur einen ephemeren Thread angelegt");
      loadedThreadIds.add(run.threadId);
      runStore.schedule(run);

      const turnResult = await appServer.request("turn/start", {
        threadId: run.threadId,
        input: [{ type: "text", text: prompt }],
        cwd: PROJECT_DIR,
        approvalPolicy: "never",
        sandboxPolicy: { type: "readOnly", networkAccess },
        model,
        effort,
        summary,
        personality: "pragmatic",
      }, 180_000);
      run.turnId = turnResult?.turn?.id || run.turnId;
      run.status = normalizeTurnStatus(turnResult?.turn?.status);
      const event = addEvent(run, {
        method: "debug/turnAccepted",
        params: { turn: turnResult?.turn },
      }, "client");
      broadcastRun(run, event);
      runStore.schedule(run);
    } catch (error) {
      run.status = "failed";
      run.error = { message: error.message };
      run.completedAt = nowIso();
      const event = addEvent(run, { method: "debug/startFailed", params: run.error }, "runtime");
      broadcastRun(run, event);
      await runStore.flush(run);
    }
    return run;
  }

  function assertLockedReplyOptions(body, root) {
    for (const [field, expected] of [
      ["model", root.model],
      ["effort", root.effort],
      ["summary", root.summary],
      ["networkAccess", root.networkAccess],
      ["profile", root.profile],
      ["webPolicy", root.webPolicy],
    ]) {
      if (body[field] !== undefined && body[field] !== expected) {
        throw new HttpError(409, `Fortsetzungen verwenden unverändert ${field}=${expected}`);
      }
    }
  }

  async function resumeThread(root) {
    if (loadedThreadIds.has(root.threadId)) return;
    const resumeParams = {
      threadId: root.threadId,
      model: root.model,
      cwd: PROJECT_DIR,
      approvalPolicy: "never",
      sandbox: "read-only",
      personality: "pragmatic",
      serviceName: "rechtsgraph_debug_ui",
      developerInstructions: root.instructions?.developer,
    };
    if (root.instructions?.base) resumeParams.baseInstructions = root.instructions.base;
    try {
      const result = await appServer.request("thread/resume", resumeParams, 180_000);
      if (result?.thread?.id !== root.threadId) {
        throw new Error("App Server hat beim Fortsetzen eine andere threadId geliefert");
      }
      if (result?.thread?.ephemeral === true) {
        throw new Error("Der fortgesetzte Thread ist ephemer");
      }
      loadedThreadIds.add(root.threadId);
    } catch (error) {
      throw new HttpError(
        409,
        `Die gespeicherte Unterhaltung ist im Codex App Server nicht mehr verfügbar: ${error.message}`,
      );
    }
  }

  async function continueRun(parentRunId, body) {
    const prompt = String(body.prompt || "").trim();
    if (!prompt) throw new HttpError(400, "Bitte eine Antwort oder Folgefrage eingeben");
    if (prompt.length > 100_000) throw new HttpError(400, "Die Eingabe ist zu lang");
    if (!appServer.ready) throw new HttpError(503, "Codex App Server ist noch nicht bereit");

    const parent = await getRun(parentRunId);
    if (!parent) throw new HttpError(404, "Ausgangslauf nicht gefunden");
    const turns = conversationRuns(parent.conversationId);
    const root = turns.find((run) => run.id === run.rootRunId);
    if (!root) throw new HttpError(409, "Die gespeicherte Unterhaltung ist unvollständig: Root-Turn fehlt");
    const latest = turns.at(-1);
    if (turns.some((run) => ACTIVE_STATUSES.has(run.status))) {
      throw new HttpError(409, "In dieser Unterhaltung läuft bereits ein Turn");
    }
    if (continuationLocks.has(parent.conversationId)) {
      throw new HttpError(409, "In dieser Unterhaltung wird bereits ein Turn gestartet");
    }
    if (latest.id !== parent.id) {
      throw new HttpError(409, "Folgefragen sind nur auf den neuesten Turn möglich; Verzweigungen sind deaktiviert");
    }
    if (parent.status !== "completed") {
      throw new HttpError(409, "Nur ein abgeschlossener Turn kann fortgesetzt werden");
    }
    if (!root.threadId) throw new HttpError(409, "Die gespeicherte Unterhaltung enthält keine Codex threadId");
    if (root.threadEphemeral !== false || turns.some((run) => run.threadEphemeral !== false)) {
      throw new HttpError(
        409,
        "Diese ältere Unterhaltung wurde ephemer gespeichert und ist nach einem Neustart nicht fortsetzbar. Bitte eine neue Unterhaltung starten.",
      );
    }
    if (root.profile !== profile.name) {
      throw new HttpError(409, `Diese Unterhaltung ist an das Profil ${root.profile} gebunden; aktiv ist ${profile.name}`);
    }
    if (!appServer.models.some((entry) => entry.id === root.model || entry.model === root.model)) {
      throw new HttpError(409, `Das gespeicherte Modell ${root.model} ist in dieser Codex-Anmeldung nicht verfügbar`);
    }
    try {
      normalizeEffort(root.effort, root.model, appServer.models);
    } catch (error) {
      throw new HttpError(409, `Die gespeicherte Reasoning-Stufe ist nicht mehr verfügbar: ${error.message}`);
    }
    if (typeof root.instructions?.developer !== "string") {
      throw new HttpError(409, "Die gespeicherte Unterhaltung enthält keinen fortsetzbaren Instruktions-Snapshot");
    }
    assertLockedReplyOptions(body, root);
    continuationLocks.add(parent.conversationId);
    try {
      await resumeThread(root);

      const run = createRun({
        prompt,
        model: root.model,
        effort: root.effort,
        summary: root.summary,
        networkAccess: root.networkAccess,
        profile: root.profile,
        webPolicy: root.webPolicy,
        conversationId: root.conversationId,
        parentRunId: parent.id,
        rootRunId: root.id,
        threadId: root.threadId,
        threadEphemeral: false,
        usageBaseline: parent.usage?.total,
        baseInstructions: root.instructions?.base,
        developerInstructions: root.instructions?.developer,
      });
      runs.set(run.id, run);
      runStore.schedule(run);
      try {
        const turnResult = await appServer.request("turn/start", {
          threadId: run.threadId,
          input: [{ type: "text", text: prompt }],
          cwd: PROJECT_DIR,
          approvalPolicy: "never",
          sandboxPolicy: { type: "readOnly", networkAccess: run.networkAccess },
          model: run.model,
          effort: run.effort,
          summary: run.summary,
          personality: "pragmatic",
        }, 180_000);
        run.turnId = turnResult?.turn?.id || run.turnId;
        run.status = normalizeTurnStatus(turnResult?.turn?.status);
        const event = addEvent(run, {
          method: "debug/turnAccepted",
          params: { turn: turnResult?.turn, continuedFromRunId: parent.id },
        }, "client");
        broadcastRun(run, event);
        runStore.schedule(run);
      } catch (error) {
        run.status = "failed";
        run.error = { message: error.message };
        run.completedAt = nowIso();
        const event = addEvent(run, { method: "debug/startFailed", params: run.error }, "runtime");
        broadcastRun(run, event);
        await runStore.flush(run);
      }
      return run;
    } finally {
      continuationLocks.delete(parent.conversationId);
    }
  }

  const server = http.createServer(async (req, res) => {
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("Referrer-Policy", "no-referrer");
    res.setHeader(
      "Content-Security-Policy",
      "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'",
    );

    try {
      const url = new URL(req.url, `http://${HOST}`);
      if (req.method === "GET" && url.pathname === "/api/health") {
        sendJson(res, 200, {
          ok: appServer.ready,
          backend: "ready",
          appServer: appServer.ready ? "ready" : "not_ready",
          codexBinary: CODEX_BIN,
          codexUserAgent: appServer.userAgent,
          modelProvider: providerMetadata(),
          providerCapabilities: appServer.providerCapabilities,
          cwd: PROJECT_DIR,
          profile: profile.name,
          mcpInventory: appServer.mcpServers || [],
          persistedRuns: runs.size,
          runDirectory,
          runtimeEvents,
        });
        return;
      }

      if (req.method === "GET" && url.pathname === "/api/config") {
        sendJson(res, 200, {
          defaultModel: appServer.models.find((entry) => entry.id === "gpt-5.6-sol")?.id
            || appServer.models.find((entry) => entry.isDefault)?.id
            || appServer.models[0]?.id,
          models: appServer.models,
          cwd: PROJECT_DIR,
          sandbox: "read-only",
          approvalPolicy: "never",
          profile: { name: profile.name, label: profile.label },
          mcpInventory: appServer.mcpServers ?? null,
          appServerArgs: profile.appServerArgs,
          instructionHashes: {
            base: createHash("sha256").update(profile.baseInstructions || "").digest("hex"),
            developer: createHash("sha256").update(profile.developerInstructions || "").digest("hex"),
          },
          defaultEffort: profile.defaultEffort,
          defaultSummary: profile.defaultSummary,
          defaultWebPolicy: profile.defaultWebPolicy || "optional",
          runDirectory,
          appServerReady: appServer.ready,
          codexUserAgent: appServer.userAgent,
          modelProvider: providerMetadata(),
          providerCapabilities: appServer.providerCapabilities,
        });
        return;
      }

      if (req.method === "GET" && url.pathname === "/api/runs") {
        const requestedLimit = Number.parseInt(url.searchParams.get("limit") || "50", 10);
        const limit = Math.max(1, Math.min(Number.isFinite(requestedLimit) ? requestedLimit : 50, 250));
        const summaries = [...runs.values()]
          .sort((left, right) => Date.parse(right.startedAt) - Date.parse(left.startedAt))
          .slice(0, limit)
          .map(summarizeRun);
        sendJson(res, 200, { count: summaries.length, runs: summaries });
        return;
      }

      if (req.method === "POST" && url.pathname === "/api/runs") {
        const body = await readJsonBody(req);
        const run = await startRun(body);
        sendJson(res, run.status === "failed" ? 500 : 202, presentRun(run, true));
        return;
      }

      const replyMatch = url.pathname.match(/^\/api\/runs\/([^/]+)\/replies$/);
      if (req.method === "POST" && replyMatch) {
        const body = await readJsonBody(req);
        const run = await continueRun(replyMatch[1], body);
        sendJson(res, run.status === "failed" ? 500 : 202, presentRun(run, true));
        return;
      }

      const runMatch = url.pathname.match(/^\/api\/runs\/([^/]+)$/);
      if (req.method === "GET" && runMatch) {
        const run = await getRun(runMatch[1]);
        if (!run) return sendJson(res, 404, { error: "Lauf nicht gefunden" });
        sendJson(res, 200, presentRun(run, true));
        return;
      }

      const conversationMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)$/);
      if (req.method === "GET" && conversationMatch) {
        await ensureConversationLoaded(conversationMatch[1]);
        const conversation = serializeConversation(conversationMatch[1]);
        if (!conversation) return sendJson(res, 404, { error: "Unterhaltung nicht gefunden" });
        sendJson(res, 200, conversation);
        return;
      }

      const eventMatch = url.pathname.match(/^\/api\/runs\/([^/]+)\/events$/);
      if (req.method === "GET" && eventMatch) {
        const run = await getRun(eventMatch[1]);
        if (!run) return sendJson(res, 404, { error: "Lauf nicht gefunden" });
        res.writeHead(200, {
          "Content-Type": "text/event-stream; charset=utf-8",
          "Cache-Control": "no-cache, no-transform",
          Connection: "keep-alive",
        });
        res.write(": connected\n\n");
        sendSse(res, "snapshot", presentRun(run, true));
        run.clients.add(res);
        const heartbeat = setInterval(() => res.write(": heartbeat\n\n"), 15_000);
        req.on("close", () => {
          clearInterval(heartbeat);
          run.clients.delete(res);
        });
        return;
      }

      const interruptMatch = url.pathname.match(/^\/api\/runs\/([^/]+)\/interrupt$/);
      if (req.method === "POST" && interruptMatch) {
        const run = await getRun(interruptMatch[1]);
        if (!run) return sendJson(res, 404, { error: "Lauf nicht gefunden" });
        if (!run.threadId || !run.turnId || !["running", "starting"].includes(run.status)) {
          return sendJson(res, 409, { error: "Dieser Lauf kann nicht unterbrochen werden" });
        }
        run.status = "interrupting";
        await appServer.request("turn/interrupt", { threadId: run.threadId, turnId: run.turnId }, 30_000);
        const event = addEvent(run, { method: "debug/interruptRequested", params: {} }, "client");
        broadcastRun(run, event);
        runStore.schedule(run);
        sendJson(res, 202, presentRun(run, false));
        return;
      }

      const staticFile = STATIC_FILES.get(url.pathname);
      if (req.method === "GET" && staticFile) {
        const [filename, contentType] = staticFile;
        const body = await readFile(path.join(PUBLIC_DIR, filename));
        res.writeHead(200, {
          "Content-Type": contentType,
          "Content-Length": body.length,
          "Cache-Control": "no-cache",
        });
        res.end(body);
        return;
      }

      sendJson(res, 404, { error: "Nicht gefunden" });
    } catch (error) {
      sendJson(res, error.status || 400, { error: error.message });
    }
  });

  if (startAppServer) {
    try {
      await appServer.start();
    } catch (error) {
      await appServer.stop?.();
      throw error;
    }
  }
  await new Promise((resolve) => server.listen(port, HOST, resolve));
  const address = server.address();
  const actualPort = typeof address === "object" && address ? address.port : port;

  return {
    server,
    appServer,
    runs,
    profile,
    runStore,
    host: HOST,
    port: actualPort,
    async close() {
      for (const run of runs.values()) {
        for (const client of run.clients) client.end();
      }
      await new Promise((resolve) => server.close(resolve));
      const activeRuns = [...runs.values()].filter((run) => (
        ACTIVE_STATUSES.has(run.status)
      ));
      await runStore.close(activeRuns);
      await appServer.stop?.();
    },
  };
}

async function main() {
  const instance = await createDebugServer();
  process.stdout.write(`RechtsGraph Debug UI: http://${instance.host}:${instance.port}\n`);
  process.stdout.write(`Codex: ${CODEX_BIN}\n`);
  process.stdout.write(`Workspace: ${PROJECT_DIR}\n`);
  process.stdout.write(`Profile: ${instance.profile.name}\n`);
  process.stdout.write(`Run logs: ${instance.runStore.directory}\n`);

  const shutdown = async () => {
    await instance.close();
    process.exit(0);
  };
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (isMain) {
  main().catch((error) => {
    process.stderr.write(`${error.stack || error.message}\n`);
    process.exit(1);
  });
}
