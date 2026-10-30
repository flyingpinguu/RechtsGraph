import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { parseArguments, runJudge } from "../legal_competence_v1/run_judge.mjs";

test("judge runner is dry by default and plans every blinded answer", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "legal-judge-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const judge = path.join(directory, "judge");
  await mkdir(judge, { recursive: true });
  await writeFile(path.join(judge, "preparation.json"), JSON.stringify({ judgeInputCount: 24 }));
  for (let index = 1; index <= 12; index += 1) {
    const taskId = `task_${String(index).padStart(2, "0")}`;
    for (const answerLabel of ["A", "B"]) {
      const inputDirectory = path.join(judge, "inputs", taskId);
      await mkdir(inputDirectory, { recursive: true });
      await writeFile(path.join(inputDirectory, `${answerLabel}.json`), JSON.stringify({
        taskId,
        answerLabel,
        reference: {
          criteria: [{ id: "accuracy" }],
          fatalErrors: [{ id: "invented_law" }],
        },
      }));
    }
  }

  const result = await runJudge({ results: directory, model: "test/judge", repeats: 3, execute: false });
  assert.equal(result.dryRun, true);
  assert.equal(result.totalJobs, 72);
  assert.equal(result.pendingJobs, 72);
  assert.match(result.note, /No model calls/);
  const targeted = await runJudge({
    results: directory,
    model: "test/judge",
    repeats: 3,
    execute: false,
    only: new Set(["task_01/A"]),
  });
  assert.equal(targeted.totalJobs, 3);
  const parsed = parseArguments([
    "--results", directory, "--model", "x", "--repeats", "3", "--only", "task_01/A,task_02/B",
  ]);
  assert.deepEqual([...parsed.only], ["task_01/A", "task_02/B"]);
  assert.throws(() => parseArguments(["--results", directory, "--model", "x", "--repeats", "2.5"]));
  assert.throws(() => parseArguments(["--results", directory, "--model", "x", "--only", "task_01/C"]), /--only/);
  assert.throws(() => parseArguments(["--model", "x"]), /Required/);
});


test("Codex judge options record real provider and effort without API credentials", async () => {
  const parsed = parseArguments(["--results", "/tmp/example", "--model", "gpt-6.1-sol", "--provider", "codex-default", "--effort", "low"]);
  assert.equal(parsed.provider, "codex-default");
  assert.equal(parsed.effort, "low");
  assert.throws(() => parseArguments(["--results", "/tmp/x", "--model", "x", "--provider", "other"]), /--provider/);
});

test("Codex cached raw evidence produces validated score envelopes and rejects changed effort", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "legal-codex-judge-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const judge = path.join(directory, "judge");
  await mkdir(path.join(judge, "inputs", "task"), { recursive: true });
  await writeFile(path.join(judge, "preparation.json"), JSON.stringify({ judgeInputCount: 2 }));
  for (const answerLabel of ["A", "B"]) {
    await writeFile(path.join(judge, "inputs", "task", `${answerLabel}.json`), JSON.stringify({
      taskId: "task", answerLabel, reference: { criteria: [{ id: "accuracy" }], fatalErrors: [] },
    }));
  }
  const options = { results: directory, provider: "codex-default", model: "gpt-6.1-sol", effort: "low", repeats: 1 };
  const plan = await runJudge({ ...options, execute: false });
  assert.equal(plan.judgeConfiguration.effort, "low");
  assert.equal(plan.judgeConfiguration.seed, null);
  await writeFile(path.join(directory, "judge_prompt.md"), "test rubric");
  for (const answerLabel of ["A", "B"]) {
    const rawDirectory = path.join(judge, "raw", "task", answerLabel);
    await mkdir(rawDirectory, { recursive: true });
    const score = { taskId: "task", answerLabel,
      criteria: [{ id: "accuracy", score: 3, reason: "Reason", answerEvidence: "Quote" }],
      fatalErrors: [], overallAssessment: "Test" };
    await writeFile(path.join(rawDirectory, "repeat-01.json"), JSON.stringify({
      judgeModel: options.model, judgeConfiguration: plan.judgeConfiguration,
      providerResponse: { id: "fake-turn", choices: [{ message: { content: JSON.stringify(score) } }], trace: [{ mock: true }] },
    }));
  }
  const result = await runJudge({ ...options, execute: true });
  assert.equal(result.completedJobs, 2);
  const saved = JSON.parse(await readFile(path.join(judge, "scores", "task", "A", "repeat-01.json"), "utf8"));
  assert.deepEqual(saved.judgeConfiguration, plan.judgeConfiguration);
  assert.equal(saved.score.criteria[0].score, 3);
  await assert.rejects(runJudge({ ...options, effort: "high", execute: false }), /generation settings changed/);
});
