import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import test from "node:test";
import { callCodexJudge, CODEX_JUDGE_ARGS } from "../legal_competence_v1/codex_judge.mjs";

function fakeServer({ tool = false, mcp = false } = {}) {
  const requests = [];
  function spawnProcess(binary, args, options) {
    assert.ok(args.includes("--stdio"));
    const child = new EventEmitter();
    child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
    child.kill = () => {};
    const emit = (message) => child.stdout.write(JSON.stringify(message)+"\n");
    child.stdin.on("data", (data) => {
      for (const line of String(data).trim().split("\n")) {
        const request = JSON.parse(line);
        requests.push(request);
        if (!request.id) continue;
        let result = {};
        if (request.method === "config/read") result = { config: { mcp_servers: mcp ? { unexpected: { enabled: true } } : {} } };
        if (request.method === "mcpServerStatus/list") result = { data: [] };
        if (request.method === "thread/start") result = { thread: { id: "thread-judge" } };
        if (request.method === "turn/start") result = { turn: { id: "turn-judge" } };
        queueMicrotask(() => {
          emit({ id: request.id, result });
          if (request.method === "turn/start") {
            emit({ method: "item/completed", params: { item: tool ? { id: "tool1", type: "webSearch" }
              : { id: "answer", type: "agentMessage", phase: "final_answer", text: '{"score":"fixture"}' } } });
            emit({ method: "turn/completed", params: { turn: { id: "turn-judge", status: "completed" } } });
          }
        });
      }
    });
    return child;
  }
  return { spawnProcess, requests };
}

test("Codex adapter isolates judge, disables tools and archives RPC trace", async () => {
  const fake = fakeServer();
  const result = await callCodexJudge({ model: "gpt-6.1-sol", effort: "low", judgePrompt: "rubric", input: { answerLabel: "A" }, timeoutMs: 1000, spawnProcess: fake.spawnProcess });
  assert.equal(result.error, undefined);
  assert.equal(result.choices[0].message.content, '{"score":"fixture"}');
  assert.ok(result.trace.length > 5);
  const thread = fake.requests.find((request) => request.method === "thread/start");
  assert.equal(thread.params.ephemeral, true);
  assert.equal(thread.params.sandbox, "read-only");
  const turn = fake.requests.find((request) => request.method === "turn/start");
  assert.equal(turn.params.effort, "low");
  assert.equal(turn.params.sandboxPolicy.networkAccess, false);
  assert.ok(CODEX_JUDGE_ARGS.includes('web_search="disabled"'));
});

for (const scenario of [{ tool: true }, { mcp: true }]) {
  test("Codex judge refuses forbidden tool availability or use " + JSON.stringify(scenario), async () => {
    const fake = fakeServer(scenario);
    const result = await callCodexJudge({ model: "test", judgePrompt: "rubric", input: {}, timeoutMs: 1000, spawnProcess: fake.spawnProcess });
    assert.ok(result.error);
    assert.deepEqual(result.choices, []);
    assert.ok(result.trace.length);
  });
}
