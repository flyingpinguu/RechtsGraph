import assert from "node:assert/strict";
import test from "node:test";

import {
  assertWebPolicy,
  evaluateWebPolicy,
  instructionsForWebPolicy,
  normalizeWebPolicy,
} from "../web_policy.mjs";

test("web policies normalize and add scoped instructions", () => {
  assert.equal(normalizeWebPolicy(undefined), "optional");
  assert.equal(normalizeWebPolicy(" REQUIRED "), "required");
  assert.throws(() => normalizeWebPolicy("sometimes"), /Unknown web policy/);
  assert.equal(instructionsForWebPolicy("base", "optional"), "base");
  assert.match(instructionsForWebPolicy("base", "required"), /requires live web evidence/);
  assert.match(instructionsForWebPolicy("base", "forbidden"), /Do not use live web search/);
});

test("required policy uses observed events rather than claims in answer text", () => {
  const claimed = evaluateWebPolicy({
    finalText: "I searched the web.",
    events: [],
  }, "required");
  assert.equal(claimed.compliant, false);
  assert.equal(claimed.reason, "web_search_not_used");

  const observed = evaluateWebPolicy({
    events: [{ message: { params: { item: {
      id: "web-1",
      type: "webSearch",
      results: [{ url: "https://example.test/source" }],
    } } } }],
  }, "required");
  assert.equal(observed.compliant, true);
  assert.equal(observed.callCount, 1);
  assert.equal(observed.sourceCount, 1);
  assert.deepEqual(observed.sourceUrls, ["https://example.test/source"]);
});

test("forbidden policy rejects observed web calls and required policy fails closed without a trace", () => {
  const run = { toolItems: [{ id: "web-1", type: "webSearch" }] };
  assert.equal(evaluateWebPolicy(run, "forbidden").reason, "web_search_used_while_forbidden");
  assert.throws(() => assertWebPolicy(run, "forbidden"), { code: "WEB_POLICY_VIOLATION" });
  assert.equal(evaluateWebPolicy({}, "required").reason, "web_trace_unavailable");
});

test("required policy rejects failed calls and calls without auditable sources", () => {
  const failed = evaluateWebPolicy({ toolItems: [{
    id: "web-failed", type: "webSearch", status: "failed", error: { message: "offline" },
  }] }, "required");
  assert.equal(failed.compliant, false);
  assert.equal(failed.reason, "web_search_failed");

  const empty = evaluateWebPolicy({ toolItems: [{
    id: "web-empty", type: "webSearch", status: "completed", results: [],
  }] }, "required");
  assert.equal(empty.compliant, false);
  assert.equal(empty.reason, "web_search_no_sources");
});
