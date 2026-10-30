const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  config: null,
  run: null,
  source: null,
  visibleEvents: [],
  runHistory: [],
  toolFilter: "all",
  clock: null,
};

const formatNumber = new Intl.NumberFormat("de-DE");

function setRuntime(status, label, version = "") {
  const dot = $("#runtime-dot");
  dot.className = `status-dot status-${status}`;
  $("#runtime-label").textContent = label;
  $("#runtime-version").textContent = version || "—";
}

function setFormMessage(message, error = false) {
  const element = $("#form-message");
  element.textContent = message;
  element.classList.toggle("error", error);
}

function safeJson(value) {
  if (value === undefined || value === null) return "—";
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

function truncate(value, max = 120) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function setSelectOptions(select, options, value) {
  select.replaceChildren(...options.map(({ value: optionValue, label }) => {
    const option = document.createElement("option");
    option.value = optionValue;
    option.textContent = label;
    return option;
  }));
  if (value && options.some((option) => option.value === value)) select.value = value;
}

function selectedModel() {
  return state.config?.models?.find((entry) => entry.id === $("#model").value || entry.model === $("#model").value);
}

function refreshEfforts() {
  const model = selectedModel();
  const efforts = (model?.supportedReasoningEfforts || []).map((entry) => ({
    value: entry.reasoningEffort,
    label: entry.reasoningEffort,
  }));
  const fallback = ["low", "medium", "high", "xhigh", "max"].map((value) => ({ value, label: value }));
  const preferred = state.config?.defaultEffort || model?.defaultReasoningEffort || "medium";
  setSelectOptions($("#effort"), efforts.length ? efforts : fallback, preferred);
}

function relativeTime(value) {
  const milliseconds = Date.now() - Date.parse(value);
  if (!Number.isFinite(milliseconds)) return "—";
  const minutes = Math.round(milliseconds / 60_000);
  if (minutes < 1) return "gerade eben";
  if (minutes < 60) return `vor ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `vor ${hours} h`;
  return new Intl.DateTimeFormat("de-DE", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function renderHistory() {
  const list = $("#history-list");
  if (!state.runHistory.length) {
    list.className = "history-list empty-state";
    list.textContent = "Noch keine gespeicherten Läufe.";
    return;
  }
  const conversations = new Map();
  for (const run of state.runHistory) {
    const id = run.conversationId || run.id;
    const entry = conversations.get(id) || { runs: [] };
    entry.runs.push(run);
    conversations.set(id, entry);
  }
  list.className = "history-list";
  list.replaceChildren(...[...conversations.values()].map(({ runs }) => {
    const parentIds = new Set(runs.map((run) => run.parentRunId).filter(Boolean));
    const leaves = runs.filter((run) => !parentIds.has(run.id));
    const run = leaves.length === 1 ? leaves[0] : runs[0];
    const turns = runs.length;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `history-entry${state.run?.id === run.id ? " active" : ""}`;
    button.addEventListener("click", () => restoreRun(run.id));
    const heading = document.createElement("strong");
    heading.textContent = truncate(run.prompt, 92) || "Leerer Prompt";
    const meta = document.createElement("small");
    meta.textContent = [
      relativeTime(run.startedAt),
      run.status,
      `${formatNumber.format(run.totalTokens || 0)} tok`,
      `${run.modelRounds || 0} R`,
      `${turns} geladene${turns === 1 ? "r" : ""} Turn${turns === 1 ? "" : "s"}`,
    ].join(" · ");
    button.append(heading, meta);
    return button;
  }));
}

function renderTranscript(conversation) {
  const transcript = $("#transcript");
  const turns = conversation?.turns || [];
  if (!turns.length) {
    transcript.className = "transcript empty-state";
    transcript.textContent = "Noch keine Unterhaltung gestartet.";
    return;
  }
  transcript.className = "transcript";
  const fragment = document.createDocumentFragment();
  for (const turn of turns) {
    const wrapper = document.createElement("article");
    wrapper.className = "transcript-turn";
    const user = document.createElement("div");
    user.className = "message user";
    const userMeta = document.createElement("small");
    userMeta.textContent = `Du · Turn ${turn.index}`;
    const userText = document.createElement("span");
    userText.textContent = turn.prompt;
    user.append(userMeta, userText);
    wrapper.append(user);
    if (turn.commentaryText) {
      const commentary = document.createElement("div");
      commentary.className = "message agent-commentary";
      const meta = document.createElement("small");
      meta.textContent = "Agent · Zwischenstand";
      const text = document.createElement("span");
      text.textContent = turn.commentaryText;
      commentary.append(meta, text);
      wrapper.append(commentary);
    }
    if (turn.finalText || turn.status === "completed") {
      const agent = document.createElement("div");
      agent.className = "message agent";
      const meta = document.createElement("small");
      meta.textContent = `Agent · ${turn.status}`;
      const text = document.createElement("span");
      text.textContent = turn.finalText || "(keine Textantwort)";
      agent.append(meta, text);
      wrapper.append(agent);
    } else if (turn.error?.message) {
      const failure = document.createElement("div");
      failure.className = "message error";
      failure.textContent = `Fehler: ${turn.error.message}`;
      wrapper.append(failure);
    }
    fragment.append(wrapper);
  }
  transcript.replaceChildren(fragment);
  transcript.scrollTop = transcript.scrollHeight;
}

async function loadRunHistory() {
  try {
    const response = await fetch("/api/runs?limit=50");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Run-Historie nicht verfügbar");
    state.runHistory = payload.runs || [];
    renderHistory();
  } catch (error) {
    const list = $("#history-list");
    list.className = "history-list empty-state";
    list.textContent = error.message;
  }
}

async function loadConfig() {
  try {
    const [healthResponse, configResponse] = await Promise.all([fetch("/api/health"), fetch("/api/config")]);
    const health = await healthResponse.json();
    const config = await configResponse.json();
    if (!health.ok || !config.appServerReady) throw new Error("App Server ist nicht bereit");
    state.config = config;
    const options = config.models.map((model) => ({
      value: model.id || model.model,
      label: `${model.displayName || model.id}${model.id === "gpt-5.6-sol" ? " · Sol" : ""}`,
    }));
    setSelectOptions($("#model"), options, config.defaultModel);
    refreshEfforts();
    $("#summary").value = config.defaultSummary || "none";
    $("#web-policy").value = config.defaultWebPolicy || "optional";
    $("#runtime-profile").textContent = config.profile?.label || config.profile?.name || "—";
    $("#runtime-profile").title = config.providerCapabilities?.webSearch === true
      ? "Provider-Websuche vom Codex App Server bestätigt"
      : "Provider-Websuche nicht bestätigt";
    setRuntime("ready", "App Server bereit", health.codexUserAgent || "Codex");
    $("#run-button").disabled = false;
    await loadRunHistory();
    const existingRunId = new URLSearchParams(window.location.search).get("run");
    if (existingRunId) await restoreRun(existingRunId);
  } catch (error) {
    setRuntime("error", "App Server nicht erreichbar");
    setFormMessage(error.message, true);
    $("#run-button").disabled = true;
  }
}

async function restoreRun(runId) {
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
    const run = await response.json();
    if (!response.ok) throw new Error(run.error || "Lauf nicht gefunden");
    if (run.conversation?.activeRunId && run.conversation.activeRunId !== run.id) {
      await restoreRun(run.conversation.activeRunId);
      return;
    }
    state.visibleEvents = run.events || [];
    updateRunView(run);
    renderHistory();
    renderEvents();
    $("#download-events").disabled = !state.visibleEvents.length;
    if (["starting", "running", "inProgress", "interrupting"].includes(run.status)) connectEvents(run.id);
  } catch (error) {
    setFormMessage(`Gespeicherter Lauf: ${error.message}`, true);
  }
}

function tokenValue(usage, key) {
  return formatNumber.format(Number(usage?.[key] || 0));
}

function updateClock() {
  if (!state.run) return;
  const start = Date.parse(state.run.startedAt);
  const end = state.run.completedAt ? Date.parse(state.run.completedAt) : Date.now();
  $("#elapsed").textContent = `${(Math.max(0, end - start) / 1000).toFixed(2)} s`;
}

function durationSeconds(start, end) {
  if (!start || !end) return "—";
  return `${((Date.parse(end) - Date.parse(start)) / 1000).toFixed(2)} s`;
}

function updateRunView(run) {
  state.run = run;
  $("#run-status").textContent = run.status;
  $("#run-status").className = `run-status ${run.status}`;
  $("#thread-id").textContent = run.threadId || "—";
  $("#thread-id").title = run.threadId || "";
  $("#turn-id").textContent = run.turnId || "—";
  $("#turn-id").title = run.turnId || "";
  $("#item-count").textContent = formatNumber.format(run.itemCount || 0);
  $("#tool-count").textContent = formatNumber.format(run.toolCallCount || 0);
  $("#first-token").textContent = durationSeconds(run.startedAt, run.firstAgentDeltaAt);
  $("#run-profile").textContent = run.profile || "legacy";
  const webPolicy = run.webPolicyEvaluation || { policy: run.webPolicy || "optional", compliant: true };
  $("#run-web-policy").textContent = webPolicy.compliant === false
    ? `${webPolicy.policy} · ${webPolicy.reason}`
    : webPolicy.compliant === null ? `${webPolicy.policy} · pending` : webPolicy.policy;
  $("#model-rounds").textContent = formatNumber.format(run.modelRounds || 0);
  updateClock();

  const last = run.usage?.last || {};
  const total = run.usage?.total || {};
  const turn = run.turnUsage || {};
  $("#tokens-input").textContent = tokenValue(last, "inputTokens");
  $("#tokens-cached").textContent = tokenValue(last, "cachedInputTokens");
  $("#tokens-output").textContent = tokenValue(last, "outputTokens");
  $("#tokens-reasoning").textContent = tokenValue(last, "reasoningOutputTokens");
  $("#tokens-total").textContent = tokenValue(last, "totalTokens");
  $("#tokens-input-total").textContent = `Dieser Turn ${tokenValue(turn, "inputTokens")} · Unterhaltung ${tokenValue(total, "inputTokens")}`;
  $("#tokens-cached-total").textContent = `Dieser Turn ${tokenValue(turn, "cachedInputTokens")} · Unterhaltung ${tokenValue(total, "cachedInputTokens")}`;
  $("#tokens-output-total").textContent = `Dieser Turn ${tokenValue(turn, "outputTokens")} · Unterhaltung ${tokenValue(total, "outputTokens")}`;
  $("#tokens-reasoning-total").textContent = `Dieser Turn ${tokenValue(turn, "reasoningOutputTokens")} · Unterhaltung ${tokenValue(total, "reasoningOutputTokens")}`;
  $("#tokens-grand-total").textContent = `Dieser Turn ${tokenValue(turn, "totalTokens")} · Unterhaltung ${tokenValue(total, "totalTokens")}`;
  const cacheRatio = Number(run.diagnostics?.cacheRatio || 0);
  $("#cache-ratio").textContent = `${(cacheRatio * 100).toFixed(1)} %`;
  const contextWindow = Number(run.usage?.modelContextWindow || 0);
  const used = Number(last.totalTokens || 0);
  const percent = contextWindow ? Math.min(100, (used / contextWindow) * 100) : 0;
  $("#context-value").textContent = contextWindow
    ? `${formatNumber.format(used)} / ${formatNumber.format(contextWindow)} · ${percent.toFixed(2)} %`
    : "Kontext —";
  $("#context-meter-fill").style.width = `${percent}%`;

  renderTranscript(run.conversation);
  $("#copy-answer").disabled = !(run.conversation?.turns || []).some((item) => item.finalText);

  const active = ["starting", "running", "inProgress", "interrupting"].includes(run.status)
    || Boolean(run.conversation?.activeRunId);
  const unavailableLegacyConversation = run.conversation?.resumable === false;
  const latestTurn = run.conversation?.turns?.at(-1);
  const continuationBlocked = Boolean(latestTurn && latestTurn.status !== "completed");
  $("#run-button").disabled = active || unavailableLegacyConversation || continuationBlocked || !state.config?.appServerReady;
  $("#run-button").textContent = run.conversation?.turns?.length ? "Antwort senden" : "Unterhaltung starten";
  $("#prompt-label").textContent = run.conversation?.turns?.length ? "Antwort oder Folgefrage" : "Frage";
  $("#conversation-heading").textContent = run.conversation?.turns?.length ? "Unterhaltung fortsetzen" : "Neue Unterhaltung";
  for (const selector of ["#model", "#effort", "#summary", "#web-policy", "#network-access"]) $(selector).disabled = Boolean(run.conversation?.turns?.length);
  $("#stop-button").disabled = !["starting", "running"].includes(run.status);
  setFormMessage(
    run.error?.message
      || (unavailableLegacyConversation
        ? "Diese ältere ephemere Unterhaltung kann nicht fortgesetzt werden. Bitte eine neue Unterhaltung starten."
        : active ? "Agent läuft …" : run.status === "completed" ? "Turn abgeschlossen · Antwort oder Folgefrage möglich" : run.status),
    Boolean(run.error) || unavailableLegacyConversation,
  );
  renderTools(run.toolItems || []);
  renderReasoning(run.reasoningItems || []);
  renderRounds(run.rounds || []);
  renderDiagnostics(run.diagnostics || {});
  renderHistory();
}

function toolName(item) {
  if (item.type === "mcpToolCall") return `${item.server || "mcp"}.${item.tool || "tool"}`;
  if (item.type === "commandExecution") return truncate(item.command || "command", 90);
  if (item.type === "webSearch") return `web_search: ${truncate(item.query || "", 70)}`;
  if (item.type === "fileChange") return "file_change";
  return item.tool || item.type;
}

function toolSubtitle(item) {
  const resultChars = Number(item._debug?.resultChars || 0);
  const size = resultChars ? ` · ${formatNumber.format(resultChars)} chars` : "";
  if (item.type === "mcpToolCall") return `MCP · ${item.server || "unbekannt"}${size}`;
  if (item.type === "commandExecution") return `Shell · ${truncate(item.cwd || "", 60)}`;
  return `${item.type}${size}`;
}

function toolResult(item) {
  if (item.error) return item.error;
  if (item.result) return item.result;
  if (item.aggregatedOutput) return item.aggregatedOutput;
  if (item.changes) return item.changes;
  if (item.contentItems) return item.contentItems;
  return item.status === "inProgress" ? "läuft …" : "—";
}

function renderTools(items) {
  const list = $("#tool-list");
  const filtered = state.toolFilter === "all" ? items : items.filter((item) => item.type === state.toolFilter);
  if (!filtered.length) {
    list.className = "tool-list empty-state";
    list.textContent = items.length ? "Keine Tool-Calls für diesen Filter." : "Noch keine Tool-Calls.";
    return;
  }
  list.className = "tool-list";
  const fragment = document.createDocumentFragment();
  for (const item of filtered) {
    const card = $("#tool-template").content.firstElementChild.cloneNode(true);
    card.dataset.type = item.type;
    card.querySelector(".tool-main strong").textContent = toolName(item);
    card.querySelector(".tool-main small").textContent = toolSubtitle(item);
    card.querySelector(".tool-duration").textContent = item._debug?.durationMs != null ? `${item._debug.durationMs} ms` : "—";
    const status = item.status || (item._debug?.completedAt ? "completed" : "inProgress");
    const statusElement = card.querySelector(".tool-status");
    statusElement.textContent = status;
    statusElement.classList.add(status);
    card.querySelector(".tool-arguments").textContent = safeJson(item.arguments ?? item.command ?? item.query ?? item.changes);
    card.querySelector(".tool-result").textContent = safeJson(toolResult(item));
    fragment.append(card);
  }
  list.replaceChildren(fragment);
}

function reasoningText(item) {
  if (Array.isArray(item.summary)) return item.summary.filter(Boolean).join("\n\n");
  return safeJson(item.summary || item.content || "");
}

function renderReasoning(items) {
  const list = $("#reasoning-list");
  const useful = items.filter((item) => reasoningText(item).trim());
  if (!useful.length) {
    list.className = "reasoning-list empty-state";
    list.textContent = "Noch keine Reasoning-Events.";
    return;
  }
  list.className = "reasoning-list";
  list.replaceChildren(...useful.map((item, index) => {
    const entry = document.createElement("article");
    entry.className = "reasoning-entry";
    const meta = document.createElement("small");
    meta.textContent = `summary ${index + 1} · ${item.id || "ohne id"}`;
    const text = document.createElement("p");
    text.textContent = reasoningText(item);
    entry.append(meta, text);
    return entry;
  }));
}

function renderRounds(rounds) {
  const list = $("#round-list");
  if (!rounds.length) {
    list.className = "round-list empty-state";
    list.textContent = "Noch keine Modellrunden.";
    return;
  }
  list.className = "round-list";
  list.replaceChildren(...rounds.map((round) => {
    const entry = document.createElement("article");
    entry.className = "round-entry";
    const number = document.createElement("strong");
    number.textContent = `R${round.number}`;
    const usage = document.createElement("span");
    usage.textContent = `${formatNumber.format(round.usage?.totalTokens || 0)} tok`;
    const cache = document.createElement("span");
    cache.textContent = `${formatNumber.format(round.usage?.cachedInputTokens || 0)} cached`;
    const actions = document.createElement("small");
    const parts = [
      ...(round.tools || []),
      ...(round.messagePhases || []).map((phase) => `message:${phase}`),
    ];
    if (round.reasoningOnly) parts.push("Reasoning ohne abgeschlossene Aktion");
    actions.textContent = parts.join(" · ") || "keine abgeschlossene Aktion im Eventfenster";
    entry.append(number, usage, cache, actions);
    return entry;
  }));
}

function renderDiagnostics(diagnostics) {
  const warnings = diagnostics.warnings || [];
  $("#diagnostic-count").textContent = `${warnings.length} Hinweis${warnings.length === 1 ? "" : "e"}`;
  const summary = $("#diagnostic-summary");
  const stats = [
    ["Runden", diagnostics.modelRounds || 0],
    ["Toolresultate", `${formatNumber.format(diagnostics.resultChars || 0)} chars`],
    ["Content", `${formatNumber.format(diagnostics.contentChars || 0)} chars`],
    ["Structured", `${formatNumber.format(diagnostics.structuredChars || 0)} chars`],
  ];
  summary.replaceChildren(...stats.map(([label, value]) => {
    const item = document.createElement("span");
    item.innerHTML = `<small>${label}</small><strong>${value}</strong>`;
    return item;
  }));
  const list = $("#diagnostic-list");
  if (!warnings.length) {
    list.className = "diagnostic-list empty-state";
    list.textContent = state.run?.status === "completed" ? "Keine automatischen Bloat-Hinweise." : "Diagnose läuft.";
    return;
  }
  list.className = "diagnostic-list";
  list.replaceChildren(...warnings.map((warning) => {
    const entry = document.createElement("article");
    entry.className = `diagnostic-entry ${warning.severity || "info"}`;
    const title = document.createElement("strong");
    title.textContent = warning.title;
    const detail = document.createElement("p");
    detail.textContent = warning.detail;
    entry.append(title, detail);
    return entry;
  }));
}

function eventMatches(event, filter) {
  if (!filter) return true;
  return safeJson(event).toLowerCase().includes(filter.toLowerCase());
}

function renderEvents() {
  const stream = $("#event-stream");
  const filter = $("#event-filter").value.trim();
  const events = state.visibleEvents.filter((event) => eventMatches(event, filter));
  if (!events.length) {
    stream.innerHTML = '<div class="empty-state">Keine passenden Events.</div>';
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const event of events) {
    const row = document.createElement("div");
    row.className = `event-row ${event.direction || "server"}`;
    const seq = document.createElement("span");
    seq.className = "event-seq";
    seq.textContent = `#${event.seq}`;
    const method = document.createElement("span");
    method.className = "event-method";
    method.textContent = event.message?.method || (event.message?.id != null ? `response:${event.message.id}` : "event");
    method.title = method.textContent;
    const json = document.createElement("span");
    json.className = "event-json";
    json.textContent = safeJson(event.message?.params ?? event.message?.result ?? event.message?.error ?? event.message);
    row.append(seq, method, json);
    fragment.append(row);
  }
  stream.replaceChildren(fragment);
  if ($("#auto-scroll").checked) stream.scrollTop = stream.scrollHeight;
}

function addEvent(event) {
  if (!event || state.visibleEvents.some((existing) => existing.seq === event.seq)) return;
  state.visibleEvents.push(event);
  renderEvents();
}

function connectEvents(runId) {
  state.source?.close();
  const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
  state.source = source;
  source.addEventListener("snapshot", (event) => {
    const run = JSON.parse(event.data);
    state.visibleEvents = run.events || [];
    updateRunView(run);
    renderEvents();
    $("#download-events").disabled = !state.visibleEvents.length;
  });
  source.addEventListener("update", (event) => {
    const payload = JSON.parse(event.data);
    addEvent(payload.event);
    updateRunView(payload.run);
    $("#download-events").disabled = !state.visibleEvents.length;
    if (["completed", "failed", "interrupted"].includes(payload.run.status)) source.close();
    if (["completed", "failed", "interrupted"].includes(payload.run.status)) loadRunHistory();
  });
  source.onerror = () => {
    if (state.run && ["starting", "running", "interrupting"].includes(state.run.status)) {
      setRuntime("waiting", "Event stream verbindet neu", state.config?.codexUserAgent);
    }
  };
  source.onopen = () => setRuntime("ready", "App Server bereit", state.config?.codexUserAgent);
}

async function startRun(event) {
  event.preventDefault();
  setFormMessage("Starte Sol und RechtsGraph MCP …");
  $("#run-button").disabled = true;
  state.visibleEvents = [];
  renderEvents();
  try {
    const latestRunId = state.run?.conversation?.latestRunId;
    const isReply = Boolean(latestRunId);
    const response = await fetch(isReply ? `/api/runs/${encodeURIComponent(latestRunId)}/replies` : "/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(isReply ? { prompt: $("#prompt").value } : {
          prompt: $("#prompt").value,
          model: $("#model").value,
          effort: $("#effort").value,
          summary: $("#summary").value,
          webPolicy: $("#web-policy").value,
          networkAccess: $("#network-access").checked,
        }),
    });
    const run = await response.json();
    if (!response.ok) throw new Error(run.error?.message || run.error || "Lauf konnte nicht gestartet werden");
    updateRunView(run);
    $("#prompt").value = "";
    loadRunHistory();
    window.history.replaceState(null, "", `?run=${encodeURIComponent(run.id)}`);
    connectEvents(run.id);
  } catch (error) {
    setFormMessage(error.message, true);
    $("#run-button").disabled = false;
  }
}

function newConversation() {
  state.source?.close();
  state.source = null;
  state.run = null;
  state.visibleEvents = [];
  $("#prompt").value = "";
  $("#prompt-label").textContent = "Frage";
  $("#conversation-heading").textContent = "Neue Unterhaltung";
  $("#run-button").textContent = "Unterhaltung starten";
  $("#run-button").disabled = !state.config?.appServerReady;
  $("#stop-button").disabled = true;
  for (const selector of ["#model", "#effort", "#summary", "#web-policy", "#network-access"]) $(selector).disabled = false;
  setFormMessage("");
  renderTranscript(null);
  renderEvents();
  renderHistory();
  window.history.replaceState(null, "", window.location.pathname);
}

async function stopRun() {
  if (!state.run) return;
  $("#stop-button").disabled = true;
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(state.run.id)}/interrupt`, { method: "POST" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "Unterbrechen fehlgeschlagen");
    updateRunView(body);
  } catch (error) {
    setFormMessage(error.message, true);
  }
}

function downloadEvents() {
  if (!state.visibleEvents.length) return;
  const blob = new Blob([JSON.stringify({ run: state.run, events: state.visibleEvents }, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `rechtsgraph-run-${state.run?.id || "events"}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
}

$("#model").addEventListener("change", refreshEfforts);
$("#run-form").addEventListener("submit", startRun);
$("#stop-button").addEventListener("click", stopRun);
$("#copy-answer").addEventListener("click", () => {
  const text = (state.run?.conversation?.turns || [])
    .flatMap((turn) => [`Du: ${turn.prompt}`, turn.finalText ? `Agent: ${turn.finalText}` : null])
    .filter(Boolean)
    .join("\n\n");
  navigator.clipboard.writeText(text);
});
$("#new-conversation").addEventListener("click", newConversation);
$("#event-filter").addEventListener("input", renderEvents);
$("#download-events").addEventListener("click", downloadEvents);
$("#refresh-history").addEventListener("click", loadRunHistory);
$("#clear-events").addEventListener("click", () => { state.visibleEvents = []; renderEvents(); });
for (const button of $$("[data-tool-filter]")) {
  button.addEventListener("click", () => {
    state.toolFilter = button.dataset.toolFilter;
    for (const candidate of $$("[data-tool-filter]")) candidate.classList.toggle("active", candidate === button);
    renderTools(state.run?.toolItems || []);
  });
}

state.clock = setInterval(updateClock, 100);
loadConfig();
