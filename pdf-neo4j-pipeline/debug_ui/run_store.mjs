import { mkdir, readFile, readdir, rename, stat, writeFile } from "node:fs/promises";
import path from "node:path";

import { hydrateRun, serializeRun } from "./run_model.mjs";

function storageSnapshot(run) {
  const payload = serializeRun(run, true);
  payload.instructions = run.instructions;
  for (const field of [
    "items",
    "toolItems",
    "reasoningItems",
    "rounds",
    "diagnostics",
    "usageHistory",
  ]) delete payload[field];
  payload.storageVersion = 3;
  return payload;
}

export class RunStore {
  constructor(directory, { debounceMs = 250, onError = null } = {}) {
    this.directory = directory;
    this.debounceMs = debounceMs;
    this.onError = onError;
    this.timers = new Map();
    this.queues = new Map();
  }

  filePath(runId) {
    return path.join(this.directory, `${runId}.json`);
  }

  async init() {
    await mkdir(this.directory, { recursive: true });
  }

  schedule(run) {
    clearTimeout(this.timers.get(run.id));
    const timer = setTimeout(() => {
      this.timers.delete(run.id);
      this.persist(run).catch((error) => this.onError?.(error));
    }, this.debounceMs);
    timer.unref?.();
    this.timers.set(run.id, timer);
  }

  async persist(run) {
    await this.init();
    const previous = this.queues.get(run.id) || Promise.resolve();
    const operation = previous.catch(() => {}).then(async () => {
      const destination = this.filePath(run.id);
      const temporary = `${destination}.${process.pid}.tmp`;
      const payload = `${JSON.stringify(storageSnapshot(run), null, 2)}\n`;
      await writeFile(temporary, payload, "utf8");
      await rename(temporary, destination);
    });
    this.queues.set(run.id, operation);
    try {
      await operation;
    } finally {
      if (this.queues.get(run.id) === operation) this.queues.delete(run.id);
    }
  }

  async flush(run) {
    clearTimeout(this.timers.get(run.id));
    this.timers.delete(run.id);
    await this.persist(run);
  }

  async load(runId) {
    try {
      return hydrateRun(JSON.parse(await readFile(this.filePath(runId), "utf8")));
    } catch (error) {
      if (error.code === "ENOENT") return null;
      throw error;
    }
  }

  async loadRecent(limit = 100) {
    await this.init();
    const entries = await readdir(this.directory, { withFileTypes: true });
    const files = entries.filter((entry) => entry.isFile() && entry.name.endsWith(".json"));
    const ranked = await Promise.all(files.map(async (entry) => ({
      name: entry.name,
      modified: (await stat(path.join(this.directory, entry.name))).mtimeMs,
    })));
    ranked.sort((left, right) => right.modified - left.modified);
    const runs = [];
    for (const entry of ranked.slice(0, Math.max(1, limit))) {
      try {
        const payload = JSON.parse(await readFile(path.join(this.directory, entry.name), "utf8"));
        runs.push(hydrateRun(payload));
      } catch (error) {
        this.onError?.(new Error(`Run-Log konnte nicht gelesen werden (${entry.name}): ${error.message}`));
      }
    }
    return runs;
  }

  async loadConversation(conversationId) {
    await this.init();
    const entries = await readdir(this.directory, { withFileTypes: true });
    const runs = [];
    for (const entry of entries) {
      if (!entry.isFile() || !entry.name.endsWith(".json")) continue;
      try {
        const payload = JSON.parse(await readFile(path.join(this.directory, entry.name), "utf8"));
        const storedConversationId = payload.conversationId || payload.id;
        if (storedConversationId === conversationId) runs.push(hydrateRun(payload));
      } catch (error) {
        this.onError?.(new Error(`Run-Log konnte nicht gelesen werden (${entry.name}): ${error.message}`));
      }
    }
    return runs;
  }

  async close(runs = []) {
    for (const timer of this.timers.values()) clearTimeout(timer);
    this.timers.clear();
    await Promise.all(runs.map((run) => this.persist(run).catch((error) => this.onError?.(error))));
    await Promise.all([...this.queues.values()].map((operation) => operation.catch((error) => this.onError?.(error))));
  }
}
