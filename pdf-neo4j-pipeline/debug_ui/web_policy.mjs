export const WEB_POLICIES = Object.freeze(["optional", "required", "forbidden"]);

export function normalizeWebPolicy(value, fallback = "optional") {
  const normalizedFallback = String(fallback || "optional").trim().toLowerCase();
  if (!WEB_POLICIES.includes(normalizedFallback)) {
    throw new Error(`Invalid default web policy: ${fallback}`);
  }
  if (value === undefined || value === null || String(value).trim() === "") {
    return normalizedFallback;
  }
  const normalized = String(value).trim().toLowerCase();
  if (!WEB_POLICIES.includes(normalized)) {
    throw new Error(`Unknown web policy: ${value}. Expected ${WEB_POLICIES.join(", ")}`);
  }
  return normalized;
}

export function webPolicyInstruction(policy) {
  const normalized = normalizeWebPolicy(policy);
  if (normalized === "required") {
    return [
      "This turn requires live web evidence.",
      "Before the final answer, call the native live web-search tool and use at least one returned source.",
      "Do not treat prior knowledge, a guessed URL, or merely saying that you searched as web evidence.",
      "If live search is unavailable or fails, state that limitation instead of pretending that a search occurred.",
    ].join(" ");
  }
  if (normalized === "forbidden") {
    return "Do not use live web search in this turn. Answer only from the other evidence explicitly available to you.";
  }
  return null;
}

export function instructionsForWebPolicy(baseInstructions, policy) {
  const policyText = webPolicyInstruction(policy);
  return [baseInstructions, policyText].filter(Boolean).join(" ").trim();
}

function eventItem(event) {
  return event?.message?.params?.item || event?.params?.item || null;
}

function collectWebItems(run = {}) {
  const hasToolItems = Array.isArray(run.toolItems);
  const hasItems = Array.isArray(run.items);
  const hasEvents = Array.isArray(run.events);
  if (!hasToolItems && !hasItems && !hasEvents) return { traceAvailable: false, items: [] };

  const byId = new Map();
  let anonymousIndex = 0;
  const add = (item) => {
    if (item?.type !== "webSearch") return;
    const key = item.id || `anonymous-${anonymousIndex++}`;
    byId.set(key, { ...(byId.get(key) || {}), ...item });
  };
  for (const event of run.events || []) add(eventItem(event));
  for (const item of run.items || []) add(item);
  for (const item of run.toolItems || []) add(item);
  return { traceAvailable: true, items: [...byId.values()] };
}

function webSourceUrls(item) {
  const candidates = [
    ...(Array.isArray(item?.results) ? item.results : []),
    ...(Array.isArray(item?.action?.sources) ? item.action.sources : []),
  ];
  return candidates
    .map((entry) => typeof entry === "string" ? entry : entry?.url)
    .filter((url) => typeof url === "string" && /^https?:\/\//i.test(url));
}

export function evaluateWebPolicy(run = {}, policy = run.webPolicy || "optional") {
  const normalized = normalizeWebPolicy(policy);
  const collected = collectWebItems(run);
  const urls = [...new Set(collected.items.flatMap(webSourceUrls))];
  const failedCalls = collected.items.filter((item) => (
    ["failed", "error", "cancelled", "canceled"].includes(String(item?.status || "").toLowerCase())
    || Boolean(item?.error)
  )).length;
  const evidenceCallCount = collected.items.filter((item) => webSourceUrls(item).length > 0).length;

  let compliant = true;
  let reason = null;
  if (normalized === "required") {
    if (!collected.traceAvailable) {
      compliant = false;
      reason = "web_trace_unavailable";
    } else if (collected.items.length === 0) {
      compliant = false;
      reason = "web_search_not_used";
    } else if (failedCalls === collected.items.length) {
      compliant = false;
      reason = "web_search_failed";
    } else if (urls.length === 0) {
      compliant = false;
      reason = "web_search_no_sources";
    }
  } else if (normalized === "forbidden" && collected.items.length > 0) {
    compliant = false;
    reason = "web_search_used_while_forbidden";
  }
  if (normalized === "required"
      && ["starting", "running", "inprogress", "interrupting"].includes(String(run.status || "").toLowerCase())
      && compliant === false) {
    compliant = null;
    reason = "pending";
  }

  return {
    policy: normalized,
    compliant,
    reason,
    traceAvailable: collected.traceAvailable,
    callCount: collected.items.length,
    evidenceCallCount,
    failedCallCount: failedCalls,
    sourceCount: urls.length,
    sourceUrls: urls,
  };
}

export function assertWebPolicy(run, policy = run?.webPolicy || "optional") {
  const assessment = evaluateWebPolicy(run, policy);
  if (assessment.compliant === false) {
    const error = new Error(`Web policy ${assessment.policy} violated: ${assessment.reason}`);
    error.code = "WEB_POLICY_VIOLATION";
    error.assessment = assessment;
    throw error;
  }
  return assessment;
}
