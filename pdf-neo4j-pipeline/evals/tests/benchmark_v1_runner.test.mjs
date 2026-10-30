import assert from "node:assert/strict";
import test from "node:test";

import { parseArguments, runBenchmark } from "../run_benchmark_v1.mjs";

test("compact benchmark defaults to an offline fourteen-run GLM plan", async () => {
  const plan = await runBenchmark({});

  assert.equal(plan.dryRun, true);
  assert.equal(plan.plannedAgentRuns, 14);
  assert.equal(plan.provider, "openrouter");
  assert.equal(plan.model, "z-ai/glm-5.3-flash");
  assert.deepEqual(plan.plan.slice(0, 2).map((item) => item.profile), ["lean", "web_only"]);
});

test("benchmark CLI validates run mode and managed defaults", () => {
  const options = parseArguments(["--execute", "--model", "openai/gpt-5.6-luna"]);
  assert.equal(options.execute, true);
  assert.equal(options.managed, true);
  assert.equal(options.model, "openai/gpt-5.6-luna");
  assert.throws(() => parseArguments(["--execute", "--dry-run"]), /either/);
  assert.throws(() => parseArguments(["--comparison", "combined"]), /tools, graph, or instructions/);
});
