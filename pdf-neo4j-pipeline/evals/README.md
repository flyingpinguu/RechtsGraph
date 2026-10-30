# Paired legal-research evaluation

## Primary product benchmark: legal competence

The current development benchmark is documented in
[`legal_competence_v1/README.md`](legal_competence_v1/README.md). It tests the
quality of completed legal work, not tool use. Twelve assignments cover case
analysis, current-law research and advice, contract/document review, drafting,
client communication, and urgent triage. The primary `lean` versus `web_only`
comparison uses 24 fresh sessions with the same model, legal-research guide,
web access, effort, and task prompts. RechtsGraph access is the only intended
difference. The default `smoke` suite uses three fixed single-turn tasks
(case analysis, memo correction, demand letter): **six sessions** with a
180-second candidate wait per session. The explicit `quick` suite retains the
older five-task workflow; `full` is for milestones. Repeated candidate runs use
the same three-task `stability` subset. Prompts and rubrics are unchanged;
scoring remains blind and separate from execution diagnostics.

```sh
# No model calls:
.venv-mcp/bin/python evals/legal_competence_v1/benchmark.py validate
node evals/run_legal_competence_v1.mjs
node evals/run_legal_competence_v1.mjs --suite quick
```

`run_legal_competence_v1.mjs` remains a dry run unless `--execute` is supplied.
Its managed mode starts two isolated Codex App Server processes and supports a
custom OpenRouter provider; the legal candidate runner reads the API key from
the environment or ignored project `.env` (environment wins). The
API key is never accepted as a CLI argument or stored in the benchmark files.
Answers are graded separately and blind against task-specific reference briefs,
weighted criteria, accepted alternatives, and fatal-error rules. Token use,
latency, browsing, and MCP calls are retained only as diagnostics and cannot
increase or reduce the legal score.

The older [`benchmark_v1/`](benchmark_v1/) suite is retained as a technical
retrieval/integration smoke test. It is not the product benchmark and its exact
lookups and no-tool-call checks must not be described as legal competence.

## Claude Code collection

`run_claude_legal_competence.py` runs the same current legal-competence fixture
through the logged-in Claude Code CLI. It compares only `lean` and `web_only`,
archives the frozen benchmark definition and produces the same answer layout
consumed by `legal_competence_v1/benchmark.py prepare-judge`. It is a dry run
unless `--execute` is supplied.

```sh
.venv-mcp/bin/python evals/run_claude_legal_competence.py \
  --suite smoke --model claude-opus-5 --effort high

.venv-mcp/bin/python evals/run_claude_legal_competence.py \
  --execute --suite smoke --model claude-opus-5 --effort high \
  --output-dir evals/results/claude-legal-competence-smoke
```

Completed attempts are resumable; incomplete attempts are never silently
repeated. Legal scoring remains a separate blinded step.

## Paired runner

### Local-evidence routing diagnostic

`--comparison local_sources` compares `local_first` (left) with `web_only`
(right). Both must share the same general guide. The diagnostic profile asks
the agent to retrieve and use relevant local statutory passages, while still
allowing web research and clarification. It provides no case-specific provision
IDs or solutions. A status check or unrelated call does not satisfy the routing
instruction. This is a tools-plus-routing comparison, not a pure graph ablation.

`local_sources_diagnostic_2026.json` contains the unchanged pilot_02/03 prompts.
They are known regression cases, not held-out evaluation data. Usage metrics
report actual deduplicated web/MCP/evidence/status calls and observable errors;
`evidence-requested` does not assert that evidence was successfully applied.
Zero usage remains a valid outcome, distinct from an unavailable trace.

```sh
# Separate servers: start:local-first on 4320, start:web-only on 4321.
node evals/run_paired_eval.mjs --execute --comparison local_sources \
  --fixture-path evals/local_sources_diagnostic_2026.json \
  --left-url http://127.0.0.1:4320 --right-url http://127.0.0.1:4321 \
  --model gpt-5.6-sol --effort low --timeout-ms 600000
```

This harness compares two already running Debug UI servers, normally `lean` and `web_only`. The default command prints an offline plan. It needs no running server and makes no model calls. Model generation requires the explicit --execute flag.

```sh
cd pdf-neo4j-pipeline/debug_ui
npm run eval:paired

# Run only when both servers are ready; this starts 24 agent runs:
npm run eval:paired -- --execute --left-url http://127.0.0.1:4317 --right-url http://127.0.0.1:4318
```

Both `/api/config` responses are checked before any run begins. The default comparison uses `lean` and `web_only`. The runner checks effective MCP availability, instructions appropriate to the selected comparison, the Codex App Server's provider-capability response for live web search, live web configuration, and model availability. Saved event traces reject unexpected local tools or MCP use on the web-only side. Every fixture case creates a fresh run on each server. Cases execute sequentially and alternate first position: left then right for case 1, right then left for case 2, and so on.

Each fixture case may set `"webPolicy": "optional"`, `"required"`, or
`"forbidden"`; omitted policies remain optional for compatibility. A required
run is a technical failure unless its trace contains a successful web-search
call with at least one HTTP source. Failed or empty searches do not satisfy the
policy. The policy is frozen in the plan, request, attempt, and summary.

Provider capability metadata still does not prove that a particular model will
actually use the tool. Run the separate smoke check before an expensive new
provider/model evaluation. It is dry by default and makes a model call only
with `--execute`:

```sh
cd pdf-neo4j-pipeline/debug_ui
npm run eval:web-smoke -- --url http://127.0.0.1:4318 --model MODEL
npm run eval:web-smoke -- --execute --url http://127.0.0.1:4318 --model MODEL \
  --output ../evals/results/web-smoke-MODEL.json
```

The live smoke first checks the provider capability RPC and then requires an
actual `webSearch` event with an HTTP source. This is intentionally a separate,
explicit preflight so ordinary tests and dry plans never incur provider cost.

Results are written to `evals/results/paired-*`. `manifest.json` records the validated server configuration, plan, run IDs, poll trace, and state. `raw/<case>/<side>/start-response.json` and `final-response.json` retain raw API evidence, including the persisted run events returned by the server. `summary.json` is a compact index.

The bundled fixture is explicitly **unreviewed**. It contains no asserted gold answer, no correctness label, and no legal advice. The loader also accepts later `reviewed` fixtures but never derives scores from either status. A human reviewer should score paired answers without knowing the side where practical.

| Dimension | 0 | 1 | 2 |
| --- | --- | --- | --- |
| Legal relevance | misses the question | partially relevant | identifies the central legal issues |
| Factual/date discipline | changes or ignores facts/date | notes some limits | applies stated facts and dates, marks uncertainty |
| Authority and citation | no usable support | some support but weak linkage | cites appropriate authority and ties it to the conclusion |
| Calibration | overstates | mixed | distinguishes initial orientation from unresolved facts |
| Answer quality | unusable | usable with repair | concise, structured, decision-useful |

Record the reviewer, review date, and any source checks beside scores. The harness reports elapsed runtime separately from token-usage fields: `totalTokens`, cached input tokens, and uncached input tokens (`inputTokens - cachedInputTokens`), plus output and reasoning tokens. These are telemetry fields only. They are not billing, price, or cost estimates.

Use `--execute --left-url URL --right-url URL --resume-directory /absolute/path/to/paired-*` after an interrupted process. Resume skips finished cases, polls already started run IDs, and starts only previously unattempted cases. Changed instructions/configuration or fixtures are rejected. An exclusive `.runner-lock` directory prevents concurrent execution into the same output. If the process was forcibly killed, verify it has stopped and inspect remote runs before manually removing that stale lock. On a polling timeout it sends one best-effort interrupt to the known run ID, saves that raw response, and stops; a later resume may only poll the same ID. If the original POST timed out or the process stopped after its persisted start intent but before a run ID was recorded, the harness refuses to POST again because it cannot prove the first start did not succeed. Inspect the server history, then begin a new evaluation directory if needed.

Start the two servers in separate terminals (the first may already be running):

```sh
cd pdf-neo4j-pipeline/debug_ui
npm start
```

```sh
cd pdf-neo4j-pipeline/debug_ui
RECHTSGRAPH_DEBUG_PORT=4318 RECHTSGRAPH_RUN_DIRECTORY=../output/debug_ui/web_only npm run start:web-only
```

`web_only` disables the configured standalone MCP servers and irrelevant plugins.
Startup reads the actual App Server configuration and tool inventory. A disabled
server is accepted only when its effective `enabled` value is false and it has
zero tools. Unexpected enabled servers block startup rather than silently
contaminating a comparison. `lean` requires available RechtsGraph tools and
rejects other active MCP servers. Newly installed MCP servers may therefore
require an explicit profile update. `/api/config` records the inventory,
configuration arguments, and instruction hashes.

The real no-generation startup check is recorded in
`../reports/profile_smoke_2026-09-10.json`: the web-only side had zero enabled
MCP tools; lean had eight RechtsGraph tools. This verifies startup configuration,
not model behavior or legal quality. No live paired QA run has been performed.

If polling times out, interruption is best-effort; verify the remote run has
stopped if that request fails. If a POST response is lost before its run ID is
known, inspect the server history: the runner cannot safely retry that start.
Use a small fixture with `--fixture-path FILE` for an initial live pilot.


## Three profiles, three different questions

| Profile | Legal research guide | Sources |
|---|---|---|
| `web_baseline` | No; only minimal operational instructions | Web |
| `web_only` | Yes | Web |
| `lean` | Yes | Web and RechtsGraph |

`--comparison tools` (default) compares `lean` with `web_only` and requires the
same base instructions. `--comparison instructions` compares `web_only` with
`web_baseline`, requiring identical tool configuration and different base
instructions. `--comparison combined` compares `lean` with `web_baseline`; its
result measures the combined change and cannot isolate the graph/tool benefit.
The URL on the left/right must serve the corresponding profile.

The baseline is not a literally instruction-free model or the unchanged ChatGPT
product. It has minimal local runtime restrictions but no project-specific
legal research guide. Start it on a separate port with:

```sh
RECHTSGRAPH_DEBUG_PORT=4319 RECHTSGRAPH_RUN_DIRECTORY=../output/debug_ui/baseline npm run start:baseline
```

The general guide is [LEGAL_RESEARCH_GUIDE.md](../debug_ui/LEGAL_RESEARCH_GUIDE.md).
It allows clarification questions and flexible source choice in both guided
profiles. The paired fixture runner still tests one user turn per case; it does
not yet simulate answers to clarification questions. A clarification must not
be scored as a wrong legal answer. Multi-turn evaluation needs a separately
specified fact sheet/user-response protocol.

Published solutions remain scoring-only material. A future test prompt may ask
both sides not to search for the exact exercise or its solution; this is a test
condition, not part of the general guide. Such an instruction cannot exclude
pretraining knowledge or accidental web exposure. Record any solution access.
