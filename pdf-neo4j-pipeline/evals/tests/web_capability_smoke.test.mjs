import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import { runWebCapabilitySmoke } from "../run_web_capability_smoke.mjs";

async function fakeSmokeServer({ providerWebSearch = true, includeSource = true } = {}) {
  let posted = null;
  const server = http.createServer(async (request, response) => {
    response.setHeader("Content-Type", "application/json");
    if (request.method === "GET" && request.url === "/api/config") {
      response.end(JSON.stringify({
        appServerReady: true,
        profile: { name: "web_only" },
        modelProvider: { id: "test" },
        providerCapabilities: { checked: true, webSearch: providerWebSearch },
        models: [{ id: "test-model" }],
      }));
      return;
    }
    if (request.method === "POST" && request.url === "/api/runs") {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      posted = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      response.statusCode = 202;
      response.end(JSON.stringify({ id: "smoke-run", status: "running" }));
      return;
    }
    if (request.method === "GET" && request.url === "/api/runs/smoke-run") {
      const item = {
        id: "web-1",
        type: "webSearch",
        status: "completed",
        results: includeSource ? [{ url: "https://eur-lex.europa.eu/eli/reg/2024/1689/oj" }] : [],
      };
      response.end(JSON.stringify({
        id: "smoke-run",
        status: "completed",
        webPolicy: posted?.webPolicy,
        events: [{ message: { method: "item/completed", params: { item } } }],
        toolItems: [item],
      }));
      return;
    }
    response.statusCode = 404;
    response.end(JSON.stringify({ error: "not found" }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return {
    url: `http://127.0.0.1:${server.address().port}`,
    posted: () => posted,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

test("web capability smoke is dry by default", async () => {
  const result = await runWebCapabilitySmoke({ url: "http://127.0.0.1:1", model: "test-model" });
  assert.equal(result.dryRun, true);
  assert.equal(result.webPolicy, "required");
});

test("web capability smoke refuses a provider that reports no native web support without starting a model", async (t) => {
  const server = await fakeSmokeServer({ providerWebSearch: false });
  t.after(server.close);
  await assert.rejects(runWebCapabilitySmoke({ execute: true, url: server.url, model: "test-model" }), /webSearch=false/);
  assert.equal(server.posted(), null);
});

test("web capability smoke verifies an actual web event and HTTP source", async (t) => {
  const server = await fakeSmokeServer();
  t.after(server.close);
  const result = await runWebCapabilitySmoke({
    execute: true, url: server.url, model: "test-model", pollMs: 1, timeoutMs: 1_000,
  });
  assert.equal(server.posted().webPolicy, "required");
  assert.equal(result.assessment.compliant, true);
  assert.equal(result.assessment.sourceCount, 1);
});

test("web capability smoke rejects a nominal call with no source", async (t) => {
  const server = await fakeSmokeServer({ includeSource: false });
  t.after(server.close);
  await assert.rejects(runWebCapabilitySmoke({
    execute: true, url: server.url, model: "test-model", pollMs: 1, timeoutMs: 1_000,
  }), /web_search_no_sources|no auditable HTTP source/);
});
