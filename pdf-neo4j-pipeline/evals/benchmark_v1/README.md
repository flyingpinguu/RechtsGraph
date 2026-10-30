# RechtsGraph compact benchmark v1

This is the small, repeatable development benchmark. It tests whether the same
guided agent performs better with RechtsGraph than with live web research alone.

## Scope

- 3 previously reviewed legal cases
- 3 deterministic corpus/relation tasks
- 1 no-research email control
- 2 primary profiles, 14 runs total

The legal cases are known development cases, not held-out evidence. Their fixed
rubrics make regressions comparable. The corpus tasks and email control are
scored automatically. End-to-end, retrieval and no-harm results remain separate.

## Safety and comparability

- No command makes model calls unless `--execute` is present.
- Every task starts a fresh Codex thread.
- Profile order alternates by task.
- The runner verifies effective profiles, instructions, tool inventories and
  live web configuration before it starts.
- Fixture, rubric, model, effort and effective server configurations are hashed
  or archived with the results.
- The OpenRouter key is read only from `OPENROUTER_API_KEY` and is never written
  into the benchmark definition or result manifest.
- Do not change `fixture.json` or `rubric.json` between compared versions. Make
  a new benchmark version if a task or gold answer changes.

## Validate without model calls

From `pdf-neo4j-pipeline`:

```sh
.venv-mcp/bin/python evals/benchmark_v1/benchmark.py validate
.venv-mcp/bin/python evals/benchmark_v1/benchmark.py verify-gold
node evals/run_benchmark_v1.mjs
```

`verify-gold` checks the three deterministic corpus answers against the current
retrieval index and Neo4j database. Stop if it fails: the corpus changed and the
gold snapshot must be reviewed before another comparable run.

It also verifies the SHA-256 hashes of all three local ZJS source PDFs listed in
`gold_sources.json`. These PDFs are scoring references only and are never passed
to the answering agent.

## Estimate cost without model calls

```sh
.venv-mcp/bin/python evals/benchmark_v1/benchmark.py estimate-cost
```

The estimate uses the observed token and web-call load of the previous six
Sol legal A/B runs and adds lower/likely/upper scenarios for the eight shorter
retrieval/control runs. Actual OpenRouter telemetry remains authoritative.

Snapshot from 2026-09-15:

| Model | Lower | Likely | Upper |
| --- | ---: | ---: | ---: |
| `z-ai/glm-5.3-flash` | $0.22 | $0.25 | $0.32 |
| `openai/gpt-5.6-luna` | $0.48 | $0.55 | $0.70 |

The estimate includes model tokens and assumed search charges. Search accounted
for most of the estimated cost in the historical trace, so the first real run
must compare this estimate with OpenRouter's activity telemetry.

## Run through OpenRouter

Set the key locally, not in this repository or a command-line argument:

```sh
export OPENROUTER_API_KEY="..."
```

Primary GLM run:

```sh
node evals/run_benchmark_v1.mjs --execute --managed \
  --provider openrouter --model z-ai/glm-5.3-flash --effort low
```

Alternative Luna run:

```sh
node evals/run_benchmark_v1.mjs --execute --managed \
  --provider openrouter --model openai/gpt-5.6-luna --effort low
```

The managed mode starts isolated `lean` and `web_only` Debug UI/App Server
processes on free local ports, runs all tasks sequentially, saves full traces,
and stops the child processes. Before the first paid benchmark, use a tiny
fixture to confirm that the selected model supports the Responses API, MCP tool
calls and Codex web search through OpenRouter.

## Blind review and scoring

After a completed run:

```sh
.venv-mcp/bin/python evals/benchmark_v1/benchmark.py prepare-review \
  evals/results/benchmark-v1-...
```

Give only `review/blind/` to the reviewer. Do not give them
`review/answer_key.json`, the manifest, profile names or tool traces. Fill every
manual criterion in `review/blind/scorecard.json` with 0, 1, 2 or 3 and a short
reason. Also enter `reviewer` and `reviewedAt`; the latter uses `YYYY-MM-DD`.
Automatic scores are recomputed rather than trusted from the edited scorecard.

Then run:

```sh
.venv-mcp/bin/python evals/benchmark_v1/benchmark.py score \
  evals/results/benchmark-v1-...
```

This creates `benchmark-report.json` and `benchmark-report.md` with separate
panel scores, major errors, tokens and tool counts.

## Optional ablations

To isolate graph edges from local full-text access, run only after the primary
comparison:

```sh
node evals/run_benchmark_v1.mjs --execute --managed --comparison graph \
  --provider openrouter --model z-ai/glm-5.3-flash --effort low
```

This compares `lean` with `local_text_only`; the latter still has local document
and structural-unit retrieval but cannot call `explore_references`.

The separate instruction comparison is:

```sh
node evals/run_benchmark_v1.mjs --execute --managed --comparison instructions \
  --provider openrouter --model z-ai/glm-5.3-flash --effort low
```

Neither optional comparison belongs in every development iteration.
