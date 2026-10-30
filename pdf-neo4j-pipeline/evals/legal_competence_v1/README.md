# RechtsGraph legal-competence benchmark v1

This is the primary product benchmark for RechtsGraph. It asks one question:

> Does the same web-enabled legal assistant produce better real-world legal
> work when it can additionally use RechtsGraph?

The benchmark does **not** score tool calls, graph navigation, retrieval counts,
or whether an answer used local rather than web evidence.

## Core suite

- 12 fixed legal assignments
- 5 capability families
- 2 profiles: `lean` (web + RechtsGraph) and `web_only`
- 24 fresh candidate runs per complete comparison
- 26–28 model turns because the triage task is a real two- or three-turn dialogue
- all answers graded blind and pointwise against task-specific reference briefs

The four case-analysis tasks include two civil/commercial cases, one public/EU
case, and one criminal-law case. The other tasks require a regulatory memo, a
data-breach assessment, employment-contract review and redrafting, NDA
comparison, correction of a flawed retention memo, a client email, a demand
letter, and first-response triage after an employment dismissal.

| ID | Work product | Main coverage |
| --- | --- | --- |
| `case_01_representation` | claim-by-claim opinion | BGB representation and receipt |
| `case_02_factoring` | structured opinion | assignment, set-off, HGB/BGB |
| `case_03_freizuegigkeit` | court-oriented opinion | administrative procedure and EU freedoms |
| `case_04_criminal_image` | criminal-law opinion | offense mapping and statutory interpretation |
| `research_01_ai_act_recruiting` | implementation memo | current AI Act, GDPR, works council |
| `research_02_gdpr_breach` | incident decision memo | breach thresholds, deadlines, containment |
| `document_01_employment_contract` | review table and redrafts | five clauses including a clean control clause |
| `document_02_nda_comparison` | negotiation memo and clause | trade secrets and B2B standard terms |
| `document_03_retention_memo_audit` | corrected executive note | erasure, retention periods, backups |
| `draft_01_digital_product_email` | client email | digital-product consumer remedies |
| `draft_02_laptop_demand` | sendable demand letter | consumer-sales cure and evidence |
| `triage_01_employment_dismissal` | deterministic intake dialogue | question selection, adaptation, deadline safety |

All three document-review tasks contain deliberately unobjectionable text. The
rubric therefore measures false alarms explicitly instead of rewarding the
longest possible defect list. The two drafting tasks also define minimum
elements that a genuinely usable deliverable must contain.

## Small repeatable development check (default)

The ordinary command now selects `smoke`: **three fixed, single-turn tasks,
six candidate sessions** instead of 24. No new questions, shortened prompts,
or altered gold answers are generated between runs:

| ID | Purpose | Existing answer limit |
| --- | --- | --- |
| `case_01_representation` | Civil-law analysis and supporting provisions | 900 words |
| `document_03_retention_memo_audit` | Correct a flawed memo without false alarms | 700 words |
| `draft_02_laptop_demand` | Produce a usable demand letter | 450 words |

Each task is answered by `lean` and `web_only` using the same model, effort,
prompt, and timeout. Order alternates by task. The compact suites default to
180 seconds of polling per candidate session; `--timeout-ms` explicitly
overrides it for both profiles. This bounds candidate waiting, not provider
billing: startup, submission and requests add overhead, and timeout cancellation
is best effort. There is no hard dollar/token cap. Existing word limits are
instructions, not a provider output-token ceiling.

```sh
# Offline plan; no key or running services needed:
node evals/run_legal_competence_v1.mjs

# Collect one small paired check (explicit paid execution):
node evals/run_legal_competence_v1.mjs --execute --managed \
  --provider openrouter --model YOUR_FIXED_MODEL --effort low
```

Then use `prepare-judge`, the existing blind judge, and `aggregate` below to
produce the same JSON/Markdown reports. Keep the candidate and judge model IDs
fixed across iterations; record the actual IDs rather than using a moving
provider alias. A collection run alone is not a quality score.

The plan exposes task IDs, timeout and candidate wait budget. Live results keep
raw answers, selected IDs, archived definitions, environment/model settings and
the timeout; completed runs are resumed only with compatible settings. Use a
new result directory for each independent check. Existing results are untouched.

This subset is a regression signal for three products. It omits broad research
and multi-turn triage, is not held out, and cannot establish general legal
competence. Fixture hashes differ from older runs because suite membership was
updated; existing strict cross-run comparison gates continue to apply.

Other fixed suites:

- `full`: all twelve tasks; 24 candidate sessions. Use for milestones.
- `quick`: the original five-task subset, retained explicitly for older workflows.
- `stability`: the same three tasks as `smoke`; use for repeated candidate runs.
- `protected`: three tasks excluded from ordinary iteration. Their task
  text and rubric are still versioned, but should not be used to tune every change.

The four published/reference-solution cases are marked `known_solution` rather
than being presented as held out.

## What is primary and what is diagnostic

Primary outcome metrics:

- macro-average legal-work score per profile;
- score by capability family;
- paired score difference per assignment;
- win/tie/loss count;
- fatal legal-error rate.

Every candidate answer receives its own absolute rubric score from 0 to 100:
each criterion is rated from 0 to 4, multiplied by its fixed weight, and then
subject to the published fatal-error cap. The profile score is the unweighted
mean of those task scores. Blind A/B pairing only hides the harness identity
from the judge; it does not make the score relative to the other answer. The
same absolute task, family, and profile scores can therefore be compared across
models and later harness versions when the comparability gates below pass.

Token use, latency, web searches, and RechtsGraph calls are diagnostic and are
reported separately. They never add points to an answer.

## Validate without model calls

From `pdf-neo4j-pipeline`:

```sh
.venv-mcp/bin/python evals/legal_competence_v1/benchmark.py validate
node evals/run_legal_competence_v1.mjs
node evals/run_legal_competence_v1.mjs --suite quick
```

Both commands are offline. The runner is a dry run unless `--execute` is
explicitly supplied.

## Run the candidate comparison

The candidate runner reads `OPENROUTER_API_KEY` from the environment or, if unset,
from the ignored project `.env` (environment wins; no shell expansion). The
separate judge runner still requires the environment variable:

```sh
export OPENROUTER_API_KEY="..."
node evals/run_legal_competence_v1.mjs --execute --managed --suite smoke \
  --provider openrouter --model z-ai/glm-5.3-flash --effort low
```

The default is `--suite smoke`; use `--suite full` for a milestone. Every assignment starts in a fresh Codex
thread. The dialogue continues only that task's thread, uses a fixed hidden fact
map, and permits at most two clarification rounds; no second user model is
involved. Profile order alternates by task.

The result archives the complete benchmark definition, decisive source
passages, effective profile configurations, model settings, git state, hashes
of the harness instructions/code, corpus build metadata, database identity, and
raw traces. Live comparison runs should be started close together because web
search results can still change.

## Measure candidate stability

Candidate repetitions answer a different question from repeated judging. To
test whether one harness repeatedly produces better answers on the same compact fixed
subset:

```sh
node evals/run_legal_competence_v1.mjs --execute --managed \
  --suite stability --candidate-repeats 2 \
  --provider openrouter --model z-ai/glm-5.3-flash --effort low
```

This produces independent `candidate-repeat-01/`, `candidate-repeat-02/`, …
result folders. Prepare, judge, and aggregate each folder once, then run:

```sh
.venv-mcp/bin/python evals/legal_competence_v1/benchmark.py \
  aggregate-repetitions evals/results/legal-competence-stability-...
```

## Prepare blind judge inputs

```sh
.venv-mcp/bin/python evals/legal_competence_v1/benchmark.py prepare-judge \
  evals/results/legal-competence-v1-...
```

This creates `judge/inputs/` and a separate `judge/answer_key.json`. Judge inputs
contain the user assignment, candidate answer, reference brief, scoring
criteria, accepted alternatives, and fatal-error rules. They contain no profile
name or tool telemetry.

## Freeze a partial run

An interrupted milestone run can be converted into a standalone evaluation
without modifying its raw source directory. This copies only the first five
complete `lean`/`web_only` pairs, freezes their definitions and exact raw
answers, and records a SHA-256 inventory:

```sh
.venv-mcp/bin/python evals/legal_competence_v1/benchmark.py snapshot \
  evals/results/SOURCE-FULL-RUN evals/results/TARGET-PAIRED5-SNAPSHOT \
  --task-count 5

.venv-mcp/bin/python evals/legal_competence_v1/benchmark.py verify-snapshot \
  evals/results/TARGET-PAIRED5-SNAPSHOT
```

Snapshot targets must be new directories. If candidate evidence needs to
change, create a new named snapshot rather than editing an existing one.

## Run an independent LLM judge

Dry-run plan:

```sh
node evals/legal_competence_v1/run_judge.mjs \
  --results evals/results/legal-competence-v1-... \
  --model YOUR_JUDGE_MODEL
```

Paid execution:

```sh
node evals/legal_competence_v1/run_judge.mjs \
  --results evals/results/legal-competence-v1-... \
  --model YOUR_JUDGE_MODEL --execute
```

The judge should be stronger than the candidate model and fixed across
iterations. The script uses `OPENROUTER_API_KEY`, temperature 0, a fixed seed,
strict criterion IDs, and resumable per-answer files. One judge is the
economical default. With one judge, a fatal-error result is explicitly one
model's decision—not a majority. Additional judge repeats should normally be
targeted at a disputed answer or severe-error finding:

```sh
node evals/legal_competence_v1/run_judge.mjs \
  --results evals/results/legal-competence-v1-... \
  --model YOUR_JUDGE_MODEL --repeats 3 \
  --only triage_01_employment_dismissal/A --execute
```

Answers may therefore have different judge-repeat counts; aggregation records
the applicable single-decision or strict-majority rule. Raw provider responses
are stored before parsing, so an invalid judge answer remains auditable and is
not silently replaced by another paid call.

## Blind judge through the logged-in Codex account

The judge also supports `codex-default`, preserving OpenRouter credit:

```sh
node evals/legal_competence_v1/run_judge.mjs \
  --results evals/results/YOUR-RUN --provider codex-default \
  --model gpt-6.1-sol --effort low --execute
```

Omit `--execute` for the offline plan. Each blinded answer gets its own ephemeral
App Server thread in a temporary directory. Web search, shell, MCP, apps,
plugins, memories and collaboration are disabled; effective MCP configuration
and emitted item types are checked. The judge receives only its rubric and
blinded input. It cannot research additional sources. Full RPC traces and raw
final text are saved before score validation. Failed or malformed raw output
is retained and is never silently replaced by another model call.

The score envelope records the actual provider/model/effort. Temperature and
seed are not forced by this backend and are recorded as null. Resume refuses
changed provider or effort. The OpenRouter backend remains the default and
retains temperature 0 and the fixed seed policy. A same-model judge is a blind
development assessment, not independent legal expert validation. Keep that
limitation with the report. No raw trace/tool metrics award scoring points.

## Aggregate results

```sh
.venv-mcp/bin/python evals/legal_competence_v1/benchmark.py aggregate \
  evals/results/legal-competence-v1-...
```

The command validates every judge record, applies task-specific fatal-error
caps, macro-averages tasks, unblinds profiles only after scoring, and writes
`legal-competence-report.json` and `legal-competence-report.md`.

## Compare models and future runs

After judging and aggregating each run with the same fixed judge setup:

```sh
.venv-mcp/bin/python evals/legal_competence_v1/benchmark.py compare-runs \
  evals/results/comparison-OUTPUT \
  evals/results/FIRST-RUN evals/results/SECOND-RUN
```

The comparison exports absolute macro scores per model/profile, task scores,
family scores, fatal-error rates, judge model/repeats, benchmark and prompt
hashes, and differences. Numeric cross-run differences are produced only if
all of these gates match exactly:

- benchmark version;
- ordered task selection;
- fixture SHA-256;
- rubric SHA-256;
- judge-prompt SHA-256;
- judge provider/model/generation settings, repeat count per task/profile, and
  fatal-decision rule.

Runs that fail a gate remain visible as separate descriptive absolute results,
but are marked `incompatible_descriptive_only` and are never silently pooled.
Token consumption, latency, and tool calls remain diagnostic and never affect
the legal score.

## Calibration rule

An LLM judge is not a legal expert merely because it emits a number. Human legal
review is not a blocker for using the suite as a development signal. Before a
score is treated as externally credible, however, at least six blinded answers
spanning all five capability families should be independently reviewed by a
legally trained person. Record criterion-level agreement and revise the rubric
or judge prompt only by publishing a new benchmark version. Until then, scores
are development signals, not validated measurements of lawyer-equivalent
quality.
