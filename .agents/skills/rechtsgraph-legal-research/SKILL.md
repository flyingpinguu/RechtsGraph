---
name: rechtsgraph-legal-research
description: Research and answer German or EU legal questions with RechtsGraph and web evidence. Use for legal analysis, provision lookup, case-law research, document review, legal drafting, or questions whose answer depends on legal sources. Do not use for purely technical work on the parser, database, or UI.
---

# RechtsGraph legal research

Produce a useful legal answer whose important propositions can be traced to the
evidence actually retrieved. RechtsGraph supplies local statutory text and graph
connections; it does not replace legal reasoning or current external research.

## Frame the task

- Identify the jurisdiction, relevant date, requested outcome, and facts that
  could materially change the answer.
- Ask one focused question when a missing fact would change the research route
  or conclusion. Otherwise state a bounded assumption and continue.
- Match the work product to the request: a lookup does not need a full opinion,
  while a case analysis should test the elements, exceptions, and material
  counterarguments.

## Choose evidence deliberately

- For a known provision, call `get_structural_unit` directly. Do not search the
  whole corpus or request an outline first merely to rediscover it.
- For an unknown provision or topic, use a focused corpus search and expand only
  promising passages. Use an outline only when document hierarchy matters.
- Use graph traversal only for an actual reference or relationship question. A
  citation edge proves that one node cites another, not that the cited rule
  governs the user's facts.
- Use live web research for current legal status, case law, interpretation,
  administrative practice, or material authorities absent from the corpus.
  A statement such as "I will search" is not research: rely only on an observed
  tool result. Prefer official full texts and inspect the relevant reasoning,
  not just a search snippet.
- Treat corpus status, consolidation date, download date, and PDF alignment as
  provenance, not as proof that a rule applied on the relevant date. Retrieve
  detailed provenance only when it affects the answer or the user requests it.

Stop retrieving when the decisive propositions are supported and the remaining
uncertainty is unlikely to change the answer. Do not collect every conceivable
provision. If an essential source cannot be retrieved, state the precise gap
instead of replacing it with memory.

## Synthesize

- Separate facts, assumptions, retrieved authority, and legal inference.
- Distinguish statutes, judgments, administrative material, secondary sources,
  graph connections, and register-only metadata.
- For decisions, distinguish holding and relevant reasoning from the agent's
  application to the present facts. Never invent a case, citation, quotation,
  paragraph number, URL, or source status.
- Test at least the strongest exception or counterargument that could change the
  conclusion. Make uncertainty concrete rather than adding a generic disclaimer.
- Answer in the user's language and preferred level of detail. Lead with the
  result, then the decisive reasons and sources, followed by practical next
  steps when useful.
