import { randomUUID } from "node:crypto";

import { evaluateWebPolicy, normalizeWebPolicy } from "./web_policy.mjs";

const TOOL_ITEM_TYPES = new Set([
  "mcpToolCall",
  "dynamicToolCall",
  "commandExecution",
  "fileChange",
  "webSearch",
  "collabToolCall",
  "imageView",
]);

export function nowIso() {
  return new Date().toISOString();
}

export function emptyUsage() {
  return {
    last: {
      inputTokens: 0,
      cachedInputTokens: 0,
      cacheWriteInputTokens: 0,
      outputTokens: 0,
      reasoningOutputTokens: 0,
      totalTokens: 0,
    },
    total: {
      inputTokens: 0,
      cachedInputTokens: 0,
      cacheWriteInputTokens: 0,
      outputTokens: 0,
      reasoningOutputTokens: 0,
      totalTokens: 0,
    },
    modelContextWindow: null,
  };
}

export function normalizeTurnStatus(status) {
  if (status === "inProgress") return "running";
  return status || "running";
}

export function createRun({
  prompt,
  model,
  effort,
  summary,
  networkAccess,
  profile = "lean",
  webPolicy = "optional",
  conversationId = null,
  parentRunId = null,
  rootRunId = null,
  threadId = null,
  threadEphemeral = false,
  usageBaseline = null,
  baseInstructions = null,
  developerInstructions = null,
}) {
  const id = randomUUID();
  return {
    id,
    conversationId: conversationId || id,
    parentRunId,
    rootRunId: rootRunId || id,
    prompt,
    model,
    effort,
    summary,
    networkAccess,
    profile,
    webPolicy: normalizeWebPolicy(webPolicy),
    instructions: {
      base: baseInstructions,
      developer: developerInstructions,
    },
    status: "starting",
    startedAt: nowIso(),
    completedAt: null,
    firstAgentDeltaAt: null,
    threadId,
    threadEphemeral,
    turnId: null,
    error: null,
    finalText: "",
    commentaryText: "",
    usage: emptyUsage(),
    usageBaseline: { ...emptyUsage().total, ...(usageBaseline || {}) },
    turnUsage: { ...emptyUsage().total },
    usageHistory: [],
    items: new Map(),
    events: [],
    clients: new Set(),
    sequence: 0,
  };
}

export function hydrateRun(payload) {
  if (!Array.isArray(payload.items) && Array.isArray(payload.events)) {
    const replayed = createRun({
      prompt: payload.prompt || "",
      model: payload.model || "",
      effort: payload.effort || "",
      summary: payload.summary || "none",
      networkAccess: payload.networkAccess !== false,
      profile: payload.profile || "legacy",
      webPolicy: payload.webPolicy || "optional",
    });
    replayed.id = payload.id || replayed.id;
    replayed.conversationId = payload.conversationId || replayed.id;
    replayed.parentRunId = payload.parentRunId || null;
    replayed.rootRunId = payload.rootRunId || replayed.id;
    // Version 2 logs were created with ephemeral: true and cannot be resumed
    // after the App Server process exits.
    replayed.threadEphemeral = payload.threadEphemeral ?? true;
    replayed.instructions = payload.instructions || { base: null, developer: null };
    replayed.usageBaseline = { ...emptyUsage().total, ...(payload.usageBaseline || {}) };
    replayed.turnUsage = { ...emptyUsage().total, ...(payload.turnUsage || {}) };
    replayed.startedAt = payload.startedAt || replayed.startedAt;
    for (const event of payload.events) {
      applyMessage(replayed, event.message, event.at, event.seq);
      replayed.events.push(event);
      replayed.sequence = Math.max(replayed.sequence, Number(event.seq || 0));
    }
    for (const field of [
      "status", "completedAt", "firstAgentDeltaAt", "threadId", "turnId",
      "error", "finalText", "commentaryText",
    ]) {
      if (payload[field] !== undefined) replayed[field] = payload[field];
    }
    return replayed;
  }
  const run = {
    ...createRun({
      prompt: payload.prompt || "",
      model: payload.model || "",
      effort: payload.effort || "",
      summary: payload.summary || "none",
      networkAccess: payload.networkAccess !== false,
      profile: payload.profile || "legacy",
      webPolicy: payload.webPolicy || "optional",
    }),
    ...payload,
    usage: { ...emptyUsage(), ...(payload.usage || {}) },
    usageHistory: Array.isArray(payload.usageHistory) ? payload.usageHistory : [],
    items: new Map((payload.items || []).map((item) => [item.id, item])),
    events: Array.isArray(payload.events) ? payload.events : [],
    clients: new Set(),
  };
  run.conversationId = payload.conversationId || run.id;
  run.parentRunId = payload.parentRunId || null;
  run.rootRunId = payload.rootRunId || run.id;
  run.threadEphemeral = payload.threadEphemeral ?? true;
  run.instructions = payload.instructions || { base: null, developer: null };
  run.usageBaseline = { ...emptyUsage().total, ...(payload.usageBaseline || {}) };
  run.turnUsage = { ...emptyUsage().total, ...(payload.turnUsage || {}) };
  run.sequence = Number(payload.sequence || run.events.at(-1)?.seq || 0);
  return run;
}

export function isToolItem(item) {
  return TOOL_ITEM_TYPES.has(item?.type);
}

export function addEvent(run, message, direction = "server", at = nowIso()) {
  const event = {
    seq: ++run.sequence,
    at,
    direction,
    message,
  };
  run.events.push(event);
  return event;
}

function itemWithDebug(existing, next, timestamp, completed) {
  const debug = existing?._debug || { startedAt: timestamp, completedAt: null, durationMs: null };
  if (completed) {
    debug.completedAt = timestamp;
    debug.durationMs = Math.max(0, Date.parse(timestamp) - Date.parse(debug.startedAt));
  }
  return { ...existing, ...next, _debug: debug };
}

function usageDifference(total, baseline) {
  const result = {};
  for (const field of [
    "inputTokens",
    "cachedInputTokens",
    "cacheWriteInputTokens",
    "outputTokens",
    "reasoningOutputTokens",
    "totalTokens",
  ]) {
    result[field] = Math.max(0, Number(total?.[field] || 0) - Number(baseline?.[field] || 0));
  }
  return result;
}

function applyMessage(run, message, timestamp, eventSeq) {
  const method = message?.method;
  const params = message?.params || {};

  if (method === "thread/started") {
    run.threadId = params.thread?.id || run.threadId;
  } else if (method === "turn/started") {
    run.turnId = params.turn?.id || run.turnId;
    run.status = "running";
  } else if (method === "thread/tokenUsage/updated") {
    run.usage = { ...emptyUsage(), ...params.tokenUsage };
    run.turnUsage = usageDifference(params.tokenUsage?.total, run.usageBaseline);
    run.turnId = params.turnId || run.turnId;
    run.usageHistory.push({
      at: timestamp,
      eventSeq,
      turnId: params.turnId || run.turnId,
      last: { ...(params.tokenUsage?.last || {}) },
      total: { ...(params.tokenUsage?.total || {}) },
      modelContextWindow: params.tokenUsage?.modelContextWindow || null,
    });
  } else if (method === "item/started" && params.item?.id) {
    const current = run.items.get(params.item.id);
    run.items.set(params.item.id, itemWithDebug(current, params.item, timestamp, false));
  } else if (method === "item/completed" && params.item?.id) {
    const current = run.items.get(params.item.id);
    const item = itemWithDebug(current, params.item, timestamp, true);
    run.items.set(params.item.id, item);
    if (item.type === "agentMessage") {
      if (item.phase === "final_answer" || item.phase === "final") run.finalText = item.text || run.finalText;
      else if (item.phase === "commentary") run.commentaryText = item.text || run.commentaryText;
      else run.finalText = item.text || run.finalText;
    }
  } else if (method === "item/agentMessage/delta") {
    if (!run.firstAgentDeltaAt) run.firstAgentDeltaAt = timestamp;
    const id = params.itemId || `agent-${run.turnId || "current"}`;
    const current = run.items.get(id) || {
      id,
      type: "agentMessage",
      phase: params.phase || null,
      text: "",
    };
    current.text = `${current.text || ""}${params.delta || ""}`;
    current.phase = params.phase || current.phase;
    run.items.set(id, itemWithDebug(current, current, timestamp, false));
    if (current.phase === "commentary") run.commentaryText = current.text;
    else run.finalText = current.text;
  } else if (method === "item/reasoning/summaryTextDelta") {
    const id = params.itemId || `reasoning-${run.turnId || "current"}`;
    const current = run.items.get(id) || { id, type: "reasoning", summary: [], content: [] };
    const summaryIndex = Number.isInteger(params.summaryIndex) ? params.summaryIndex : 0;
    if (!Array.isArray(current.summary)) current.summary = [];
    current.summary[summaryIndex] = `${current.summary[summaryIndex] || ""}${params.delta || ""}`;
    run.items.set(id, itemWithDebug(current, current, timestamp, false));
  } else if (method === "turn/completed") {
    run.turnId = params.turn?.id || run.turnId;
    run.status = normalizeTurnStatus(params.turn?.status || "completed");
    run.error = params.turn?.error || run.error;
    run.completedAt = timestamp;
  } else if (method === "error") {
    run.error = params.error || message.error || { message: "Unbekannter App-Server-Fehler" };
  }
}

export function updateRunFromMessage(run, message) {
  const timestamp = nowIso();
  const eventSeq = run.sequence + 1;
  applyMessage(run, message, timestamp, eventSeq);
  return addEvent(run, message, "server", timestamp);
}

function jsonChars(value) {
  if (value === undefined || value === null) return 0;
  try {
    return JSON.stringify(value).length;
  } catch {
    return String(value).length;
  }
}

function contentChars(content) {
  if (!Array.isArray(content)) return 0;
  return content.reduce((total, item) => (
    total + String(item?.text || item?.data || item?.resource?.text || "").length
  ), 0);
}

export function toolResultMetrics(item) {
  const result = item?.result;
  if (item?.type === "mcpToolCall" && result) {
    const text = contentChars(result.content);
    const structured = jsonChars(result.structuredContent);
    return {
      contentChars: text,
      structuredChars: structured,
      resultChars: text + structured,
    };
  }
  const value = result ?? item?.aggregatedOutput ?? item?.changes ?? item?.contentItems;
  return { contentChars: jsonChars(value), structuredChars: 0, resultChars: jsonChars(value) };
}

function serializeItem(item) {
  const serialized = JSON.parse(JSON.stringify(item));
  if (isToolItem(serialized)) {
    serialized._debug = { ...(serialized._debug || {}), ...toolResultMetrics(serialized) };
  }
  return serialized;
}

function itemName(item) {
  if (item.type === "mcpToolCall") return `${item.server || "mcp"}.${item.tool || "tool"}`;
  if (item.type === "webSearch") return "webSearch";
  return item.type;
}

export function buildModelRounds(run) {
  let previousSeq = 0;
  return (run.usageHistory || []).map((usage, index) => {
    const windowEvents = run.events.filter((event) => event.seq > previousSeq && event.seq <= usage.eventSeq);
    previousSeq = usage.eventSeq;
    const completedItems = windowEvents
      .filter((event) => event.message?.method === "item/completed")
      .map((event) => event.message?.params?.item)
      .filter(Boolean);
    const tools = completedItems.filter(isToolItem).map(itemName);
    const messagePhases = [...new Set(completedItems
      .filter((item) => item.type === "agentMessage")
      .map((item) => item.phase || "unspecified"))];
    const reasoningItems = completedItems.filter((item) => item.type === "reasoning").length;
    return {
      number: index + 1,
      at: usage.at,
      eventSeq: usage.eventSeq,
      usage: usage.last,
      cumulativeUsage: usage.total,
      tools,
      messagePhases,
      reasoningItems,
      reasoningOnly: tools.length === 0 && messagePhases.length === 0 && reasoningItems > 0,
    };
  });
}

function mcpPayload(item) {
  const structured = item?.result?.structuredContent?.result
    || item?.result?.structuredContent;
  if (structured && typeof structured === "object") return structured;
  for (const part of item?.result?.content || []) {
    if (typeof part?.text !== "string") continue;
    try {
      const parsed = JSON.parse(part.text);
      const payload = parsed?.result || parsed;
      if (payload && typeof payload === "object") return payload;
    } catch {
      // Plain-text MCP output has no structured diagnostic payload.
    }
  }
  return null;
}

export function diagnoseRun(run, items, rounds) {
  const toolItems = items.filter(isToolItem);
  const totals = toolItems.reduce((result, item) => {
    const metrics = toolResultMetrics(item);
    result.contentChars += metrics.contentChars;
    result.structuredChars += metrics.structuredChars;
    result.resultChars += metrics.resultChars;
    return result;
  }, { contentChars: 0, structuredChars: 0, resultChars: 0 });
  const totalInput = Number(run.usage?.total?.inputTokens || 0);
  const cachedInput = Number(run.usage?.total?.cachedInputTokens || 0);
  const totalTokens = Number(run.usage?.total?.totalTokens || 0);
  const outputTokens = Number(run.usage?.total?.outputTokens || 0);
  const warnings = [];
  const mcpTools = toolItems.filter((item) => item.type === "mcpToolCall");
  const toolNames = mcpTools.map((item) => item.tool);

  if (run.commentaryText) {
    warnings.push({
      code: "commentary_round",
      severity: "warning",
      title: "Fortschrittsnachricht im QA-Lauf",
      detail: "Commentary kann eine zusätzliche vollständige Modellrunde verursachen.",
    });
  }
  const reasoningOnlyRounds = rounds.filter((round) => round.reasoningOnly).length;
  if (reasoningOnlyRounds) {
    warnings.push({
      code: "reasoning_only_round",
      severity: "info",
      title: `${reasoningOnlyRounds} Reasoning-Runde${reasoningOnlyRounds === 1 ? "" : "n"} ohne abgeschlossene Aktion`,
      detail: "Reasoning ist bei Sol normal. Dieser Hinweis markiert nur Eventfenster, in denen danach weder ein Toolcall noch eine sichtbare Antwort abgeschlossen wurde.",
    });
  }
  if (toolNames.includes("search_corpus") && toolNames.includes("get_document_outline")) {
    warnings.push({
      code: "outline_after_search",
      severity: "warning",
      title: "Outline nach Korpussuche",
      detail: "Prüfen, ob die Suchtreffer bereits ausreichende Normstellen für get_context liefern.",
    });
  }
  const repeatedDocumentSearches = toolNames.filter((name) => name === "find_documents").length;
  if (repeatedDocumentSearches > 1) {
    warnings.push({
      code: "repeated_document_search",
      severity: "info",
      title: `${repeatedDocumentSearches} Dokumentensuchen`,
      detail: "Möglicher Alias- oder Rankingfehler; Suchargumente im Toolprotokoll vergleichen.",
    });
  }
  for (const item of toolItems) {
    const metrics = toolResultMetrics(item);
    if (metrics.resultChars > 20_000) {
      warnings.push({
        code: "large_tool_result",
        severity: "warning",
        title: `Großes Toolresultat: ${itemName(item)}`,
        detail: `${metrics.resultChars.toLocaleString("de-DE")} Zeichen in content und structuredContent.`,
      });
    }
    const payload = mcpPayload(item);
    if (payload?.ok === false) {
      warnings.push({
        code: "failed_mcp_result",
        severity: "error",
        title: `MCP-Fehler: ${itemName(item)}`,
        detail: payload.error || payload.error_type || "Tool meldete ok=false.",
      });
    }
  }

  return {
    modelRounds: rounds.length,
    cacheRatio: totalInput ? cachedInput / totalInput : 0,
    outputShare: totalTokens ? outputTokens / totalTokens : 0,
    ...totals,
    warnings,
  };
}

export function serializeRun(run, includeEvents = false) {
  const items = [...run.items.values()].map(serializeItem);
  const toolItems = items.filter(isToolItem);
  const reasoningItems = items.filter((item) => item.type === "reasoning");
  const rounds = buildModelRounds(run);
  const webPolicyEvaluation = evaluateWebPolicy({ ...run, items }, run.webPolicy);
  const diagnostics = diagnoseRun(run, items, rounds);
  if (webPolicyEvaluation.compliant === false) {
    diagnostics.warnings.push({
      code: "web_policy_violation",
      severity: "error",
      title: `Web-Policy verletzt: ${webPolicyEvaluation.policy}`,
      detail: webPolicyEvaluation.reason,
    });
  }
  const result = {
    id: run.id,
    conversationId: run.conversationId,
    parentRunId: run.parentRunId,
    rootRunId: run.rootRunId,
    prompt: run.prompt,
    model: run.model,
    effort: run.effort,
    summary: run.summary,
    networkAccess: run.networkAccess,
    profile: run.profile,
    webPolicy: run.webPolicy,
    webPolicyEvaluation,
    status: run.status,
    startedAt: run.startedAt,
    completedAt: run.completedAt,
    firstAgentDeltaAt: run.firstAgentDeltaAt,
    threadId: run.threadId,
    threadEphemeral: run.threadEphemeral,
    turnId: run.turnId,
    error: run.error,
    finalText: run.finalText,
    commentaryText: run.commentaryText,
    usage: run.usage,
    usageBaseline: run.usageBaseline,
    turnUsage: run.turnUsage,
    usageHistory: run.usageHistory,
    modelRounds: rounds.length,
    rounds,
    diagnostics,
    itemCount: items.length,
    toolCallCount: toolItems.length,
    items,
    toolItems,
    reasoningItems,
    eventCount: run.events.length,
    sequence: run.sequence,
  };
  if (includeEvents) result.events = run.events;
  return result;
}

export function summarizeRun(run) {
  const serialized = serializeRun(run, false);
  return {
    id: serialized.id,
    conversationId: serialized.conversationId,
    parentRunId: serialized.parentRunId,
    rootRunId: serialized.rootRunId,
    prompt: serialized.prompt,
    model: serialized.model,
    effort: serialized.effort,
    profile: serialized.profile,
    webPolicy: serialized.webPolicy,
    webPolicyCompliant: serialized.webPolicyEvaluation.compliant,
    status: serialized.status,
    startedAt: serialized.startedAt,
    completedAt: serialized.completedAt,
    totalTokens: Number(serialized.usage?.total?.totalTokens || 0),
    turnTokens: Number(serialized.turnUsage?.totalTokens || 0),
    cachedInputTokens: Number(serialized.usage?.total?.cachedInputTokens || 0),
    modelRounds: serialized.modelRounds,
    toolCallCount: serialized.toolCallCount,
    warningCount: serialized.diagnostics.warnings.filter(
      (item) => item.severity === "warning" || item.severity === "error",
    ).length,
  };
}
