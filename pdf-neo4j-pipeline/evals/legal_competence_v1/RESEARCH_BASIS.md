# Research basis for the RechtsGraph legal-competence benchmark

The benchmark measures the quality of legal work delivered to a user. It does
not award points for using RechtsGraph, browsing the web, making a particular
tool call, or reproducing an internal graph identifier. Tool traces are retained
only to explain score differences after the legal work has been graded.

## Sources that shaped the design

### LegalBench

LegalBench separates legal reasoning into issue spotting, rule recall, rule
application, rule conclusion, interpretation, and rhetorical understanding.
Its taxonomy is useful for checking reasoning coverage, but its many short
classification tasks are not by themselves a proxy for useful legal work.

- Paper: https://openreview.net/pdf?id=WqSPQFxFRC
- Task taxonomy: https://hazyresearch.stanford.edu/legalbench/tasks/

### BenGER

BenGER is specific to German law and provides exam-style free-text cases with
reference solutions across civil, criminal, and public law. Its judge was
validated against several blind human reviews. Four cases in this benchmark use
the same general reference-solution approach; the fourth case is drawn from the
CC BY 4.0 BenGER Benchathon data.

- Repository and licence information:
  https://github.com/SebastianNagl/benger-platform
- Paper: https://arxiv.org/abs/2605.28183

### AI-Powered Lawyering

This randomized study deliberately tested different junior-lawyer work
products: a client email, partner research memo, complaint analysis, NDA,
procedural motion, and persuasive letter. It graded accuracy, analysis,
organization, clarity, professionalism, and hallucinated authorities using
task-specific rubrics. This benchmark adapts that work-product orientation to
German and EU law rather than copying the US tasks.

- Paper and appendices:
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5162111

### German employment-contract review datasets

The employment-contract task uses four problematic clauses and expert
annotations from the German Employment Contract Clauses dataset, plus one
deliberately unobjectionable control clause. Version 3 contains clause-level
legality labels, explanations, rules, and source references. The dataset is
licensed CC BY-NC 4.0 and is used here with attribution for a non-commercial
development benchmark.

- Dataset: https://github.com/sebischair/Employment-Contract-Clauses-German
- Paper: https://arxiv.org/abs/2507.01734

### GDPval and JudgmentBench

Both emphasize realistic deliverables rather than isolated knowledge probes.
GDPval publishes gold deliverables and rubrics and treats LLM judging as an
approximation that should be checked against experts. JudgmentBench releases
task-specific rubrics, supporting documents, pointwise scores, pairwise
preferences, and expert annotations. The benchmark therefore uses blinded,
pointwise, criterion-level judging and keeps a human calibration procedure.

- GDPval grading: https://evals.openai.com/gdpval/grading
- GDPval paper: https://cdn.openai.com/pdf/d5eb7428-c4e9-4a33-bd86-86dd4bcf12ce/GDPval.pdf
- JudgmentBench: https://huggingface.co/datasets/judgmentbench/JudgmentBench

### OpenAI Evals and graders

OpenAI's Evals API treats an evaluation as fixed data plus explicit testing
criteria, and supports model graders for open-ended output. This benchmark
follows that separation but stores all artifacts locally so model, prompt,
rubric, source snapshot, and raw judge response remain auditable.

- Evals API: https://developers.openai.com/api/reference/resources/evals/methods/create
- Graders API: https://platform.openai.com/docs/api-reference/graders

## Design consequences

1. All twelve tasks produce realistic legal work products.
2. Candidate profiles receive identical user prompts, model, effort, budget,
   and web access. The only primary difference is access to RechtsGraph.
3. Each answer is judged separately and blind to profile and tool traces.
4. The judge receives a task-specific reference brief, criteria, accepted
   alternatives, explicit fatal-error rules, and where applicable false-positive
   controls or mandatory drafting elements.
5. Every task is normalized to 100 points before macro-averaging, so a long
   exam case cannot dominate a short but commercially important document task.
6. Overall score, capability-family scores, major-error rate, and paired
   per-task lift are reported separately.
7. Technical retrieval checks live outside this benchmark.
8. One task is a deterministic, bounded dialogue so question selection and
   adaptation are measured without introducing a second stochastic user model.
9. Candidate repetitions on a fixed five-task subset estimate answer variance;
   targeted judge repeats estimate judging uncertainty and are not conflated.

## Coverage matrix

| Family | Tasks | Primary capability |
| --- | ---: | --- |
| Case analysis | 4 | issue spotting, rule application, conclusion |
| Research and advice | 2 | current law, synthesis, operational advice |
| Document review | 3 | interpretation, risk detection, correction |
| Drafting and communication | 2 | usable legal documents for an audience |
| Triage and judgment | 1 | uncertainty, missing facts, deadline protection |

The set covers civil and commercial law, public and EU law, criminal law,
employment law, data protection, AI regulation, consumer law, and commercial
confidentiality. It intentionally includes some tasks on which a statutory graph
may help substantially and others on which it may add little. That is necessary
to measure product value rather than to manufacture a favorable result.
