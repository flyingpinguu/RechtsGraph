# RechtsGraph legal-competence benchmark

## Product question

The benchmark asks whether the same legal assistant produces more useful legal
work when it has RechtsGraph in addition to live web research. It does not ask
whether the assistant can call a tool, retrieve a known section, or traverse an
edge. Those are integration tests, not product outcomes.

Primary comparison:

| Profile | Legal-work instructions | Web | RechtsGraph |
| --- | --- | --- | --- |
| `web_only` | identical | yes | no |
| `lean` | identical | yes | yes |

Model, provider, reasoning effort, user assignment, web access, answer limit,
and fresh-session policy remain fixed. Profile order alternates by assignment.
The assistant may use the available sources however it thinks best; tool use is
never rewarded or required by the score.

## What legal competence means here

The suite measures five kinds of externally useful work:

| Capability | Tasks | What the user receives |
| --- | ---: | --- |
| Case analysis | 4 | civil/commercial, public/EU, and criminal opinions |
| Research and advice | 2 | current-law regulatory and incident memoranda |
| Document review | 3 | clause review/redraft, NDA comparison, memo audit |
| Drafting and communication | 2 | client email and enforceable demand letter |
| Triage and judgment | 1 | deterministic dialogue with deadline-safe, fact-responsive advice |

This is intentionally a mixed work-product suite. A legal assistant that can
solve an exam case but cannot identify a dangerous contract clause, communicate
uncertainty, protect a deadline, or draft a usable letter is not a generally
useful legal assistant.

The twelve assignments span German civil, commercial, consumer, employment,
criminal, administrative, data-protection, trade-secret, and EU AI law. Some
should benefit from direct statutory retrieval; others depend more on judgment,
communication, current amendments, or case-law synthesis. That mix prevents the
benchmark from being engineered only around RechtsGraph's strengths.

## Scoring

Each completed answer is judged independently and without the profile label,
the competing answer, tool traces, or token metrics. The judge receives:

- the exact assignment and candidate answer;
- a task-specific reference brief and primary/source references;
- explicit accepted alternatives;
- weighted criteria totaling 100 points;
- narrowly defined fatal legal-error rules.

Every criterion receives 0–4 points. The weighted result is normalized to 100
per task, after which the selected tasks are macro-averaged. A fatal error caps
that task at 50. With one judge this is transparently a single decision; with
multiple judge repeats it requires a strict majority. The report also shows capability
family scores, every paired task difference, win/tie/loss counts, and fatal-error
rates. Token and tool telemetry appear in a separate diagnostic section only.

The primary score is the unweighted task macro-average. A harness iteration is
promising only when the gain is distributed across tasks or capability families
and does not come with more fatal errors. A single aggregate gain caused by one
task is not sufficient evidence.

## Reproducibility and limits

The runner freezes the fixture, rubric, judge prompt, short verified source
passages, research notes, effective profile configurations, model settings,
git state, harness/configuration hashes, corpus build metadata, database
identity, and raw traces into each result folder.
Every candidate answer starts in a fresh session. Blinded labels are assigned
deterministically, and the answer key remains in a separate file. Judge outputs
are criterion-level, evidence-bearing, validated, and resumable.

The fixed five-task quick suite is appropriate for frequent development
regression. For answer-generation variance, repeat both candidates on the same
five-task stability suite; judge repetitions measure a different uncertainty
and should be targeted at disputed criteria or severe-error findings. Calibrate
the fixed LLM judge against at least six independently and blindly reviewed
answers spanning all five families before making external validity claims.
Until criterion-level agreement with legally trained reviewers is measured, the
numbers are development signals rather than proof of lawyer-equivalent quality.

The published/reference-solution cases are explicitly labelled
`known_solution`; they are not held out. Three other tasks form a protected
subset that is excluded from normal quick iteration. It reduces day-to-day
test-tuning risk, but it is not a truly secret external test. A future external
claim should use a separately sealed task set and multiple candidate samples
without changing this development version after results are seen.

## Execution boundary

The benchmark definition, dry-run plan, blinding, and synthetic aggregation can
all be tested offline. Candidate and judge model calls require an explicit
`--execute` flag. The current implementation is documented in
[`legal_competence_v1/README.md`](legal_competence_v1/README.md). The legacy
`benchmark_v1` lookup suite remains only as a technical smoke test.
