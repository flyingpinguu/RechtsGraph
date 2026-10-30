# RechtsGraph agent guide

RechtsGraph provides primary-text evidence from the German and EU corpus,
metadata for supplementary documents, and optional Neo4j reference edges. The
host agent remains responsible for legal reasoning and the final answer.

## Useful retrieval options

These are efficient options, not a prescribed research sequence. A focused
clarification question may be more useful than a tool call when key facts are
missing, including when the gap is discovered during research.

- Known provision: call `get_structural_unit` directly with the document name,
  abbreviation, CELEX ID, or returned document ID. Use `find_documents` first
  only when the document reference is ambiguous or the user named no provision.
  Do not request an outline when the provision is known. The default response
  gives compact primary text, legal citations, and actionable cautions; use
  `detail="full"` only if provenance, page ranges, internal IDs, or other
  passage-level metadata is actually needed.
- Topic question: `search_corpus`, then `get_context` only for selected passage
  IDs whose snippets need expansion. Do not request an outline merely because a
  search was performed.
  Search supports `mode="lexical"`, `"semantic"`, or `"hybrid"`; `"auto"` uses
  the server configuration. Semantic coverage may be limited to a core corpus.
  Inspect returned coverage/warnings; a lexical fallback is explicit. An uncached
  semantic query is sent to the configured embedding provider. Similarity and
  fusion scores do not establish applicability or answer confidence. Use
  `get_service_status(component="vectors")` when vector availability matters.
- Unfamiliar hierarchy: `get_document_outline`, then retrieve one selected
  unit.
- Graph question: call `get_service_status` with `neo4j` first. Use
  `explore_references` only when Neo4j is available. For either direction,
  continue from `neighbor_node_id`; `edge_source_id` and `edge_target_id`
  describe the actual citation direction.
- Indexed supplementary material: `find_documents(corpus="external")`, then
  `search_corpus(corpus="external", document_ref=...)`; expand selected hits
  with `get_context`. Honor the returned language when choosing search terms.
- Unavailable or unindexed supplementary material: `search_external_register`.
  `has_valid_local_text` means a file exists; only `indexed_for_fulltext` means
  that `search_corpus` can query it. Use the register for metadata/discovery,
  not after a successful indexed full-text lookup unless that metadata matters.
- EU results with `source_format="eurlex_pdf_original"` come from official
  PDF fallback extraction. Search their text and cite page evidence, but treat
  reconstructed article order as best-effort and do not claim that an outline
  is structurally complete. Formex and XHTML results retain stronger structure.

Call tools directly without progress commentary when retrieval is useful. Start
with provisions that bear on the decisive issues; retrieve further candidates
when a concrete open question calls for them, rather than collecting every
conceivable provision in advance. There is no fixed call quota. Keep limits
small, do not retrieve whole documents by default, and stop once the evidence
is sufficient. Choose local retrieval and web search according to the question;
a local hit does not establish that the research is complete. Use official web
sources when useful to check currency, interpretation, missing authorities, or
conflicting evidence.

## Legal date and evidence quality

Check document-level evidence and any passage-specific evidence overrides before
relying on a passage. Source status
is a recorded corpus value, not a fresh legal determination. A repealed text can
be relevant to a historical question, but must not be presented as current law
without checking the applicable date. Unknown status is not evidence of force.
A consolidation date, source build date, or download date is not a validity
interval. These tools do not establish which version applied on a given date;
verify material date questions against official sources and state unresolved
gaps. Ask for the event date or jurisdiction when it changes the answer.

Preserve the official source URL and legal citation when available. Honor PDF
extraction and page-alignment cautions; do not invent page references or infer
table cells from ambiguous flattened text. A reference edge proves a citation,
not that the neighboring rule governs the user's case. Distinguish statutes,
judgments, and administrative guidance when combining sources.

In the answer, distinguish corpus text, graph connections, register metadata,
and web evidence. Preserve useful document IDs and legal citations. State
material gaps instead of guessing; do not claim completeness or legal advice.
