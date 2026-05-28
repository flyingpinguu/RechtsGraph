# STATUS.md

## 2026-05-17

### Angelegt / Generiert
- `PROJECT_PLAN.md` – gemeinsamer Projektplan mit Phasen (Normtext → Semantik → Beziehungen)
- `STATUS.md` – dieser Statusbericht
- `qwen_context/README.md` – Übersicht der Qwen-Agenten-Kontexte
- `qwen_context/normtext_extraction_brief.md` – Brief fuer Normtext-Extraktion
- `qwen_context/review_brief.md` – Brief fuer Review von Extraktionen
- `qwen_context/output_contract.md` – JSON-Konzept fuer Normtext-Schicht

### Bestehend (vorher)
- `pdf-neo4j-pipeline/` – Python-Pipeline (Extraktion, Review-Shards, Neo4j-Import)
- `abfall_pdfs/` – PDF-Quellkorpus Abfallrecht
- `gefahrgut_pdfs/` – PDF-Quellkorpus Gefahrgut/Chemikalienrecht
- `guide for relations.md` – Konzept fuer Referenzauflösung im GraphRAG

### Naechste Schritte
1. Qwen-Subagenten mit `normtext_extraction_brief.md` an echte PDFs testen
2. Review-Pipeline mit `review_brief.md` validieren
3. Dokumenttyp-spezifische Extraktoren fuer Abfallrecht und Gefahrgut entwickeln
4. `output_contract.md` mit den Python-Skripten in `pdf-neo4j-pipeline/` abgleichen

### Pilotlauf Phase 1 Normtext
- Qwen/Unsloth issue: Subagents failed until Unsloth Studio was started with `unsloth studio run` and the Qwen GGUF model loaded.
- Created `pdf-neo4j-pipeline/scripts/extract_normtext.py`.
- Ran pilot extraction for `abfall_pdfs/01_KrWG.pdf`, `abfall_pdfs/02_AVV.pdf`, `gefahrgut_pdfs/GGBefG.pdf`.
- Output: `pdf-neo4j-pipeline/output/pilot/normtext_pilot.json`.
- Review inputs/artifacts: `review_input_summary.json`, `review_report.md`, `review_issues.json`.
- Pilot review decisions: KrWG `needs_revision`, AVV `needs_revision`, GGBefG `accepted_for_pilot`.
- Next fixes: TOC filtering, legal title/citation extraction, Teil/Abschnitt/Anlage hierarchy, AVV continuation and code hierarchy.

### Review-Gate Test mit Shards
- Documented improved process in `PROJECT_PLAN.md`: `raw_extraction` -> `review_shards` -> `reviews` -> `reviewed_extraction` -> later graph import.
- Created `pdf-neo4j-pipeline/scripts/build_review_shards.py` and `pdf-neo4j-pipeline/scripts/merge_reviewed_shards.py`.
- Ran gated process for `gefahrgut_pdfs/GGBefG.pdf`.
- Raw output: `pdf-neo4j-pipeline/output/raw_ggbefg.json`.
- Review shards: `pdf-neo4j-pipeline/output/review_shards/GGBefG/` with 3 shards of 3 pages each.
- Qwen review outputs: `pdf-neo4j-pipeline/output/reviews/GGBefG/review_shard_001.json`, `review_shard_002.json`, `review_shard_003.json`.
- All 3 shards were `needs_revision`; therefore `pdf-neo4j-pipeline/output/reviewed/GGBefG_reviewed.json` contains 0 accepted pages, units, and chunks.
- Main issue pattern: current raw extractor does not handle cross-page paragraph continuations reliably.

### Extractor-Fix und Qwen-Usage-Erfassung
- Improved `pdf-neo4j-pipeline/scripts/extract_normtext.py` for GGBefG-style paragraph extraction:
  - Paragraph units are now detected across the whole document, not independently per page.
  - Repeating `gesetze-im-internet.de` page headers are removed from unit text.
  - Section chunks no longer split a bare paragraph heading away from its first subsection.
  - First-page metadata extraction now captures title, abbreviation, full citation, enactment date, and status note where present.
- Re-ran GGBefG extraction:
  - Raw output: `pdf-neo4j-pipeline/output/raw_ggbefg_v2.json`.
  - Review shards: `pdf-neo4j-pipeline/output/review_shards/GGBefG_v2/`.
  - Fixed cross-page ranges include `§ 2` pages 1-2, `§ 3` pages 2-3, `§ 5` pages 3-4, `§ 7` pages 4-5, `§ 8` pages 5-6, `§ 9a` pages 7-8, `§ 11` pages 8-9.
- Added `pdf-neo4j-pipeline/scripts/qwen_chat_with_usage.py`.
  - Calls local Unsloth/Qwen through `/v1/chat/completions`.
  - Persists `usage`, `timings`, `input_context_tokens`, and `context_tokens` to JSONL.
  - Smoke test wrote `pdf-neo4j-pipeline/output/qwen_usage.jsonl`.

### Manuelles Review `GGBefG_v2`
- Qwen worker reasoning effort set to `high` in `/Users/christinck/.codex/agents/qwen-full-access.toml`.
- Manual review result: raw extraction fixes the main cross-page truncations from the first GGBefG review.
- Remaining blocker is shard packaging, not paragraph extraction:
  - Shards include full cross-page units/chunks when they intersect the shard range.
  - The `pages` evidence inside each shard only contains the nominal shard pages.
  - Therefore some extracted units contain text from pages not present as evidence in the same shard, e.g. `§ 5` in shard 1/2 and `§ 9` in shard 2/3.
- Next fix: `build_review_shards.py` should either include all evidence pages needed by included units/chunks or split cross-boundary units into review-local excerpts with explicit continuation metadata.

### Shard-Builder-Fix
- Improved `pdf-neo4j-pipeline/scripts/build_review_shards.py`.
  - Shards keep their nominal `page_range`.
  - Shards now include `nominal_page_range`, `evidence_page_range`, `nominal_pages`, and `evidence_pages`.
  - `pages` is currently the full evidence page list for backward compatibility with existing reviewers.
  - Evidence pages are expanded to cover every included cross-page unit/chunk.
  - Table-ready support added for document-level `tables`, `extracted_tables`, `table_rows`, and `extracted_table_rows`.
- Rebuilt GGBefG shards into `pdf-neo4j-pipeline/output/review_shards/GGBefG_v3/`.
  - `shard_001`: nominal pages 1-3, evidence pages 1-4.
  - `shard_002`: nominal pages 4-6, evidence pages 3-7.
  - `shard_003`: nominal pages 7-9, evidence pages 6-9.
- Added and ran synthetic table shard test at `pdf-neo4j-pipeline/output/review_shards/table_test/`.

### Qwen-Review-Lauf `GGBefG_v3`
- Review prompts written to `pdf-neo4j-pipeline/output/review_prompts/GGBefG_v3/`.
- Qwen review outputs written to `pdf-neo4j-pipeline/output/reviews/GGBefG_v3/`.
- Qwen context/usage telemetry written to `pdf-neo4j-pipeline/output/usage/qwen_usage_GGBefG_v3_reviews.jsonl`.
- Review summary written to `pdf-neo4j-pipeline/output/reviews/GGBefG_v3/review_summary.json`.
- Decisions:
  - `shard_001`: `accepted`, 0 issues, `input_context_tokens=35902`, `context_tokens=36051`.
  - `shard_002`: `needs_revision`, 2 issues, `input_context_tokens=43355`, `context_tokens=43988`.
  - `shard_003`: first full prompt failed without usage/timings; compact retry succeeded with `accepted`, 0 issues, `input_context_tokens=17035`, `context_tokens=17183`.
- Reviewed merge output: `pdf-neo4j-pipeline/output/reviewed/GGBefG_v3_reviewed.json`.
  - Accepted shards: 2 (`shard_001`, `shard_003`).
  - Skipped shards: 1 (`shard_002`).
  - Output contains 8 pages, 13 units, 42 chunks.
- Note: because evidence shards include full cross-page units, accepted neighboring shards can include content that reaches into nominally skipped page ranges. Merge policy should be made stricter/explicit before scaling.

### Telemetrie- und Extraktor-Fix `GGBefG_v4`
- Clarified Qwen telemetry in `pdf-neo4j-pipeline/scripts/qwen_chat_with_usage.py`.
  - Stores `prompt_tokens`, `completion_tokens`, `output_tokens`, `total_tokens`, `thinking_tokens`, `thinking_tokens_reported`, `input_context_tokens`, `max_context_tokens`, and `context_tokens`.
  - Current Unsloth launch has thinking disabled (`enable_thinking=false`), and llama.cpp does not report thinking tokens separately in these calls.
- Improved `pdf-neo4j-pipeline/scripts/extract_normtext.py`.
  - Added optional PyMuPDF backend for fallback/comparison.
  - Added per-page `text_extraction_backend` and backend hashes.
  - Added metadata for `secondary_pdf_backend`, backend counts, and backend comparisons.
  - Added source-text anomaly detection without silently rewriting legal text.
- Added `pdf-neo4j-pipeline/requirements.txt` with `pypdf` and `pymupdf`.
- Re-ran extraction:
  - Raw output: `pdf-neo4j-pipeline/output/raw_ggbefg_v4.json`.
  - Shards: `pdf-neo4j-pipeline/output/review_shards/GGBefG_v4/`.
  - Reviewed output: `pdf-neo4j-pipeline/output/reviewed/GGBefG_v4_reviewed.json`.
- `§ 7b Abs. 3` source anomaly is now recorded as `possible_missing_verb_in_source_text` on page 5 and included in `shard_002`.
- Re-reviewed `shard_002` with this issue context:
  - Decision: `accepted`.
  - Context usage: `prompt_tokens=44938`, `completion_tokens=132`, `input_context_tokens=44938`, `max_context_tokens=45070`, `thinking_tokens=null`.
- `GGBefG_v4_reviewed.json` now contains all 3 accepted shards: 9 pages, 18 units, 55 chunks.

### Qwen-Server mit Thinking
- Restarted local Unsloth/Qwen server on `2026-05-19`.
- Previous effective `llama-server` command had `--chat-template-kwargs {"enable_thinking": false}`.
- New parent command appends `--chat-template-kwargs {"enable_thinking":true}`.
- Observed child command contains both kwargs in order: first `false`, then appended `true`.
- Smoke test confirms visible Qwen thinking is active:
  - Output before wrapper cleanup contained a leading `<think>...</think>` block.
  - Telemetry file: `pdf-neo4j-pipeline/output/usage/qwen_usage_thinking_smoke.jsonl`.
  - Clean output file: `pdf-neo4j-pipeline/output/smoke/qwen_thinking_true_smoke_clean.txt`.
- Updated `pdf-neo4j-pipeline/scripts/qwen_chat_with_usage.py`.
  - Leading `<think>...</think>` content is stripped from the saved final output by default.
  - Telemetry now records `thinking_output_present`, `thinking_output_chars`, and `final_output_chars`.
  - `thinking_tokens` still remains `null` unless Unsloth/llama.cpp reports a separate token field; visible thinking is currently included in completion/output token counts.

### Aufräumen der Artefakte
- Removed obsolete/generated pilot, smoke, render-check, table-test, and GGBefG v1-v3 outputs.
- Removed obsolete review prompt files and raw review text dumps.
- Removed `.DS_Store` files under the project.
- Current retained output set is intentionally small:
  - `pdf-neo4j-pipeline/output/raw_ggbefg_v4.json`
  - `pdf-neo4j-pipeline/output/review_shards/GGBefG_v4/`
  - `pdf-neo4j-pipeline/output/reviews/GGBefG_v4/`
  - `pdf-neo4j-pipeline/output/reviewed/GGBefG_v4_reviewed.json`
  - `pdf-neo4j-pipeline/output/usage/qwen_usage_GGBefG_v4_reviews.jsonl`
  - `pdf-neo4j-pipeline/output/usage/qwen_usage_thinking_smoke.jsonl`

### Extractor-Struktur-Upgrade für zitierbare Units/Chunks
- Improved `pdf-neo4j-pipeline/scripts/extract_normtext.py`.
  - Structural units received readable global IDs and keys. This was later
    updated from abbreviation-based keys to long-name keys, e.g.
    `unit_versatzverordnung_para_4`.
  - Structural units now include `legal_citation`, `display_name`, `document_key`, `title`, and `text_sha256`.
  - Chunks now include `global_key`, `legal_citation`, `display_name`, `chunk_type`, `parent_chunk_id`, `child_chunk_ids`, `label`, `number`, and `sequence`.
  - Paragraph chunks are split into `subsection` chunks where `(1)`, `(2)` etc. exist.
  - Numbered and lettered list items are extracted as `list_item` child chunks.
  - Nested list items are represented, e.g. `VersatzV § 4 Abs. 2 Nr. 2` has children `Buchst. a` and `Buchst. b`.
  - `Anlage ...` headings are now recognized as structural-unit boundaries so paragraph text does not run into annexes.
  - Anlagen are extracted as `annex` units with coarse chunks. Current default:
    one `annex_text` chunk plus explicit `table` units where tables are
    recognized.
- Re-ran extraction for `abfall_pdfs/09_VersatzV.pdf`.
  - Raw output: `pdf-neo4j-pipeline/output/raw_versatzv_v2.json`.
  - Result: 15 pages, 11 structural units (`7 paragraph`, `4 annex`), 64 chunks.
  - `§ 7` now ends on page 2 instead of incorrectly spanning pages 2-15.
  - Review shards created in `pdf-neo4j-pipeline/output/review_shards/VersatzV_v2/`.
- Smoke-tested the upgraded extractor on `gefahrgut_pdfs/GGBefG.pdf`; extraction completed with 18 paragraph units and the new chunk fields.
- Updated `qwen_context/output_contract.md` to document the new fields and ID rules.

### Flaches Modell, ausgelagerte Seiten und Tabellenchunks
- Updated the extraction model after design review.
  - The canonical raw document JSON no longer stores full page text inline.
  - Page text is written to separate JSON files under `pdf-neo4j-pipeline/output/pages/<document_key>/page_XXX.json`.
  - Main document JSON keeps `page_refs` and a text-free `pages` compatibility array with `path`, `page_number`, `page_id`, `text_sha256`, and backend metadata.
  - `build_review_shards.py` now loads required page text from page refs before writing review shards.
- Removed automatic paragraph list-item subchunking for now.
  - Absatz chunks such as `§ 4 Abs. 2` stay intact even if they contain numbered or lettered lists.
  - No `list_item` chunks are emitted in the current default extraction.
- Added first-pass table handling.
  - Tables inside Anlagen are modeled as `table` structural units under their parent `annex` unit.
  - Table retrieval chunks use `chunk_type=table_rows`.
  - Historical note: table row chunks were initially split into small row
    groups. Current default is one `table_rows` chunk per detected table.
  - Each table row chunk includes `columns`, `column_header_text`, `row_range`, `rows`, and `text` with the column headers prepended.
- Re-ran extraction for `abfall_pdfs/09_VersatzV.pdf`.
  - Raw output: `pdf-neo4j-pipeline/output/raw_versatzv_v3.json`.
  - Page files: `pdf-neo4j-pipeline/output/pages/versatzv/page_001.json` through `page_015.json`.
  - Review shards: `pdf-neo4j-pipeline/output/review_shards/VersatzV_v3/`.
  - Result: 15 page refs, 14 structural units (`7 paragraph`, `4 annex`, `3 table`), 56 chunks.
  - Historical note: this run still emitted `annex_section`/`appendix_block`;
    current default no longer emits those chunk types.
  - Table examples: `VersatzV Anlage 2 Tabelle 1`, `Tabelle 1a`, and `Tabelle 2` are table units with row chunks.
- Removed obsolete `raw_versatzv_v2.json` and `review_shards/VersatzV_v2/`.

### Review-Brief und VersatzV-v3-Reviewtest
- Updated `qwen_context/review_brief.md`.
  - The brief now matches the current flat model with `extracted_units`, `extracted_chunks`, page/evidence pages, ausgelagerten page refs in raw JSON, and table row chunks.
  - It explicitly says paragraph list items are not separate chunks for now.
  - It defines strict JSON-only review output with `decision`, `confidence`, `summary`, `checked_items`, and `issues`.
- Improved `pdf-neo4j-pipeline/scripts/build_review_shards.py`.
  - Review shards now compact redundant fields before prompting:
    - `extracted_units[].text` is replaced with hash/count/preview.
    - `extracted_chunks[].evidence_text` is replaced with hash/count.
  - This reduced VersatzV review shard size from ~2.2 MB total to ~575 KB total.
- Initial direct Unsloth `/v1/chat/completions` review attempt failed.
  - Server was not running at first (`Connection refused`).
  - After restart, Unsloth returned SSE `server_error` even for a tiny prompt.
  - No usable Unsloth review outputs were produced.
- Initial autonomous `qwen_full_access` subagent review attempt was also stopped.
  - Five agents were started, one per shard.
  - They ran for several minutes without writing review files.
  - They were shut down and replaced with direct Ollama model calls for measurable telemetry.
- Added `pdf-neo4j-pipeline/scripts/ollama_review_shards.py`.
  - Calls Ollama `/api/chat` directly.
  - Logs `prompt_eval_count`, `eval_count`, durations, bytes, model, and settings to JSONL.
  - Uses model `qwen3.6:35b-a3b-q8_0`, `temperature=0.1`, `num_ctx=65536`, `num_predict=8192`, `think=false`.
- Ran VersatzV v3 review over all five shards.
  - Review outputs: `pdf-neo4j-pipeline/output/reviews/VersatzV_v3/review_shard_001.json` through `review_shard_005.json`.
  - Summary: `pdf-neo4j-pipeline/output/reviews/VersatzV_v3/review_summary.json`.
  - Token/duration log: `pdf-neo4j-pipeline/output/usage/ollama_usage_VersatzV_v3_reviews.jsonl`.
  - Results:
    - `shard_001`: `accepted`, 0 issues, prompt tokens 26,762, output tokens 3,840, wall time 236.976 s.
    - `shard_002`: `needs_revision`, 7 issues, prompt tokens 40,454, output tokens 2,036, wall time 185.457 s.
    - `shard_003`: `rejected`, 4 issues, prompt tokens 33,108, output tokens 1,462, wall time 143.380 s.
    - `shard_004`: `needs_revision`, 5 issues, prompt tokens 55,804, output tokens 1,486, wall time 260.585 s.
    - `shard_005`: `needs_revision`, 2 issues, prompt tokens 29,989, output tokens 1,252, wall time 124.849 s.
- Review findings indicate next extractor/shard-builder work:
  - Table 2 parsing in Anlage 2 needs improvement; footnotes and prose are being treated as rows.
  - Anlage 3/4 sectioning creates long coarse chunks and page ranges that are too broad.
  - Evidence-page expansion still becomes too wide for long annex units.
  - Table-like appendices in Anlage 3 are not yet modeled as table units/chunks.

### Fokus-Fix VersatzV Tabellen und Review-Shards
- Worked through the criticism from the VersatzV reviews on `shard_002` and `shard_004`.
- Updated `pdf-neo4j-pipeline/scripts/extract_normtext.py`.
  - Table 2 in Anlage 2 is now kept as one `table` unit with explicit `table_sections`.
  - The organic and inorganic row chunks now carry their own `table_section`, matching `column_header_text`, row ranges, and page ranges.
  - Table notes/footnotes for the organic section are emitted as `table_note`, not as `table_rows`.
  - `Untersuchungsparameter` appendix tables are grouped into logical rows instead of raw PDF line fragments.
  - ISO/DIN norm notices below appendix tables are emitted as `table_note`.
  - Table row and note chunks now get more precise page ranges from source line pages.
  - A feature flag `SPLIT_TABLE_SECTIONS_AS_UNITS = False` records the current decision to keep multi-section tables as one table unit with sectioned chunks.
- Updated `pdf-neo4j-pipeline/scripts/build_review_shards.py`.
  - Direct/recursive child units are included when their full pages are already in the evidence window. This reduces false "missing child unit" findings for visible evidence pages.
- Updated review context.
  - `qwen_context/review_brief.md` documents `table_section`, `table_sections`, concise output limits, and the fact that chunks are linked by `chunk.unit_id`.
  - `qwen_context/output_contract.md` documents `table_section`/`table_sections`.
  - Added `qwen_context/review_brief_focused_regression.md` for targeted regression checks after extractor fixes.
- Rebuilt VersatzV final candidate:
  - Raw output: `pdf-neo4j-pipeline/output/raw_versatzv_v15.json`.
  - Review shards: `pdf-neo4j-pipeline/output/review_shards/VersatzV_v15/`.
  - Page files remain under `pdf-neo4j-pipeline/output/pages/versatzv/`.
  - Result: 16 structural units (`7 paragraph`, `4 annex`, `5 table`) and 63 chunks (`10 table_rows`, `5 table_note`).
- Focused review results with local Ollama/Qwen:
  - Model: `qwen3.6:35b-a3b-q8_0`, `num_ctx=65536`, `num_predict=1024`, `temperature=0.0`, `think=false`.
  - Usage log: `pdf-neo4j-pipeline/output/usage/ollama_usage_VersatzV_v15_focus_reviews.jsonl`.
  - `shard_004` focused regression review accepted with 0 issues.
  - `shard_002` focused regression review completed but produced a false finding: it claimed the organic Tabelle-2 chunk had the anorganic header even though the JSON has `table_section="Organische Stoffe"` and `column_header_text="Organische Stoffe Konzentration (in mikrog/l)"`.
- Assessment:
  - The extractor fixes for the original concrete issues are visible in the JSON.
  - The local LLM reviewer is useful for finding new risks, but for dense table shards it can contradict itself and must be treated as advisory, not authoritative.

### Aufraeumen und Shard-Seitenmodell
- Local reviewer work is paused for now.
- Updated `pdf-neo4j-pipeline/scripts/build_review_shards.py`.
  - Review shards no longer write the duplicate `pages` field.
  - `nominal_pages` is now text-free metadata only.
  - `nominal_page_numbers` and `evidence_page_numbers` are written explicitly.
  - `evidence_pages` is the only shard field containing full page text.
- Rebuilt the current VersatzV shards:
  - Raw output kept as `pdf-neo4j-pipeline/output/raw_versatzv_current.json`.
  - Shards kept as `pdf-neo4j-pipeline/output/review_shards/VersatzV_current/`.
  - Page JSONs kept under `pdf-neo4j-pipeline/output/pages/versatzv/`.
- Removed old generated artifacts:
  - old `raw_versatzv_v*.json` and `raw_ggbefg_v4.json`
  - old `output/reviews/`, `output/review_prompts/`, `output/reviewed/`, and `output/usage/`
  - old review shard directories except `VersatzV_current`
  - `.DS_Store` files
- Removed obsolete reviewer/merge scripts while local review is paused:
  - `pdf-neo4j-pipeline/scripts/qwen_chat_with_usage.py`
  - `pdf-neo4j-pipeline/scripts/ollama_review_shards.py`
  - `pdf-neo4j-pipeline/scripts/merge_reviewed_shards.py`
- Active scripts now:
  - `pdf-neo4j-pipeline/scripts/extract_normtext.py`
  - `pdf-neo4j-pipeline/scripts/build_review_shards.py`
  - `pdf-neo4j-pipeline/scripts/extract_content_nodes.py`
  - `pdf-neo4j-pipeline/scripts/extract_reference_relations.py`
  - `pdf-neo4j-pipeline/scripts/export_neo4j_cypher.py`

### Content-Node-Modell
- Added `CONTENT_NODE_MODEL.md`.
- Decision recorded: the first graph layer consists of deterministic content
  nodes only (`Document`, `StructuralUnit`, `Chunk`).
- PDF pages remain external evidence JSONs for now, not Neo4j nodes.
- Concepts, legal actors, duties, permissions, and norm references are excluded
  from this layer.
- Later reference extraction should happen on the lowest reliable level
  (`Chunk -> target reference`); paragraph- and document-level relations are
  derived deterministically from the content hierarchy and stored as
  `REFERS_TO` on their respective levels.

### Content-Node-Export-Skript
- Added `pdf-neo4j-pipeline/scripts/extract_content_nodes.py`.
- The script exports deterministic content graph payloads from raw extraction
  JSON: `Document`, `StructuralUnit`, `Chunk`, plus hierarchy and sequence
  relationships.
- Table chunks with nested `rows` are flattened for Neo4j-compatible
  properties while preserving full rows in `rows_json`.

### Neo4j-Cypher-Export
- Added `pdf-neo4j-pipeline/scripts/export_neo4j_cypher.py`.
- The script converts `output/content_nodes/*.json` into executable Cypher with
  label-specific `graph_id` uniqueness constraints, node batches and
  relationship batches.
- `ContentNode` is not exported as a Neo4j label. Content status is represented
  by properties (`is_content_node`, `node_group`) so Neo4j visualization can
  style concrete labels such as `Document`, `StructuralUnit`, and `Chunk`.

### Tabellenzeilen-Chunking
- Updated `pdf-neo4j-pipeline/scripts/extract_normtext.py`.
- `table_rows` chunks now group up to 40 table rows instead of 10.
- Table notes remain grouped separately with a smaller limit because they are
  explanatory text, not regular table rows.

### Reference-Relation-Modell
- Added `REFERENCE_RELATION_MODEL.md`.
- Corpus scan result: many external references use long-name genitive patterns
  such as `§ ... des Kreislaufwirtschaftsgesetzes`, but abbreviation forms such
  as `§ 21 ElektroG` and article references such as `Artikel 13 Absatz 1 des
  Grundgesetzes` also occur.
- Decision recorded: reference extraction should create primary
  `Chunk -> target reference` records, using long-name `title_key`s for unknown
  external documents and resolving aliases later.

### Reference-Relation-Extractor
- Added `pdf-neo4j-pipeline/scripts/extract_reference_relations.py`.
- The script reads content graph JSON and adds deterministic `REFERS_TO`
  relations from chunks plus deterministic rollups to structural-unit and
  document level. Unresolved or partially resolved targets are represented with
  `ReferenceTarget` placeholders.
- Rebuilt VersatzV reference graph and Cypher export:
  - `pdf-neo4j-pipeline/output/content_nodes/versatzv_content_graph_with_refs.json`
  - `pdf-neo4j-pipeline/output/neo4j/versatzv_import_with_refs.cypher`
- Added a reference-level validation guard: `Chunk` references may target only
  `Chunk` or `ReferenceTarget`; `StructuralUnit` references may target only
  `StructuralUnit` or `ReferenceTarget`; `Document` references may target only
  `Document` or `ReferenceTarget`.
- Added `NEO4J_QUERIES.md` with practical Cypher queries for visualization,
  reference inspection, graph counts, table chunks, and import cleanup.
- Expanded `NEO4J_QUERIES.md` with the focused view:
  `Document`/`StructuralUnit` nodes plus `CONTAINS_UNIT` and `REFERS_TO`.
- Added `NODE_PROPERTY_REVIEW.md` with a first schema-slimming proposal.
- Initialized a local Git repository for the project. Generated outputs and
  virtual environments are ignored; source PDFs, scripts, and documentation are
  versioned.
- Updated key strategy: `document_key` remains a short alias such as `versatzv`,
  while `document_global_key`/`global_key` uses the normalized long norm name or
  short title, e.g. `versatzverordnung`. Unit and chunk keys now follow this
  form, e.g. `unit_versatzverordnung_para_4`.
- Updated reference extraction to build internal targets from
  `document_global_key` and to resolve known abbreviations to imported
  document-global keys.
- Updated the Neo4j Cypher exporter to filter node properties by label. Raw and
  content JSONs stay rich; Neo4j receives a slimmer visualization/RAG view.

### ErsatzbaustoffV Strukturtest
- Ran the current pipeline on `abfall_pdfs/06_ErsatzbaustoffV.pdf`.
- Generated artifacts under ignored `pdf-neo4j-pipeline/output/`:
  - `raw_ersatzbaustoffv_current.json`
  - `content_nodes/ersatzbaustoffv_content_graph.json`
  - `content_nodes/ersatzbaustoffv_content_graph_with_refs.json`
  - `neo4j/ersatzbaustoffv_import_with_refs.cypher`
- Imported the ErsatzbaustoffV graph into the local Neo4j test database after
  clearing the previous import.
- Neo4j counts after import: 320 nodes, 1399 relationships.
- Important finding: the current extractor does not yet model `Abschnitt` and
  `Unterabschnitt` as separate StructuralUnits. It still emits only paragraph,
  annex, table/waste-code style units plus chunks.
- Important finding: the ErsatzbaustoffV table of contents is partially parsed
  as real paragraph/annex units, producing duplicate node IDs in the generated
  JSON. Neo4j merges these duplicates during import, but extractor-side TOC
  filtering must be improved before scaling this document type.
- Fixed the first TOC false-positive class in `extract_normtext.py`.
  - Added a document-line filter that skips `Inhaltsübersicht` until the first
    heading followed by real body text.
  - Tightened paragraph heading detection so lines like `§ 5 Absatz 2, § 6 ...`
    are treated as references, not new paragraph units.
  - Tightened annex heading detection so line starts like `Anlage 2 oder 3 ...`
    and `Anlage 7 auszustellen ...` are treated as body/reference text.
  - Paragraph detection now stops after the first real annex heading.
- Re-ran ErsatzbaustoffV after the fix:
  - JSON/Cypher has 306 nodes and 1398 relationships.
  - Duplicate node IDs: 0.
  - Duplicate relationship IDs: 0.
  - Neo4j import completed after clearing the test database.
  - Neo4j verification: 306 nodes, 1398 relationships, 994 `REFERS_TO`, 0
    `Chunk -> StructuralUnit` `REFERS_TO`, 0 old `ersatzbaustoffv_*` prefix
    nodes.
- Regression: re-ran VersatzV. Counts remained stable at 83 nodes and 242
  relationships with no duplicate IDs.

### Reference-Debugging ErsatzbaustoffV
- User finding checked: `ersatzbaustoffverordnung_para_25_abs_3` looked like a
  source of very many references in Neo4j, although its text contains only a few
  Anlage references.
- Root cause 1: broad targets such as `Anlage 8` were expanded from one
  StructuralUnit target to every direct chunk below that unit. Fixed by linking
  chunk-level references to one representative target chunk and preserving the
  exact broad relation on StructuralUnit level.
- Root cause 2: the Anlage-reference regex could cross a line/list boundary,
  so text like `Anlage 2 oder 3` followed by list item `8.` could be misread as
  `Anlage 2 oder 3 und 8`. Fixed by allowing connector continuations only over
  horizontal whitespace.
- Root cause 3: plural `Nummern` could be parsed as `Nummer n`. Fixed by
  treating `Nummer`/`Nummern` as one qualifier family.
- Root cause 4: internal table targets were previously unresolved when the
  table structure was missing. The table heading detector now recognizes
  headings with inline titles such as `Tabelle 2: ...`, producing table units
  with keys like `ersatzbaustoffverordnung_anlage_4_tabelle_2`.
- Re-ran ErsatzbaustoffV end-to-end:
  - Content graph: 550 nodes, 1011 hierarchy/sequence relationships.
  - Reference graph: 625 nodes, 1625 relationships.
  - StructuralUnits include 46 `table` units.
  - `REFERS_TO`: 614.
  - `ersatzbaustoffverordnung_para_25_abs_3` now has 7 outgoing chunk-level
    references: `Anlage 8` once and `Anlage 2 oder 3` three times, split to
    Anlage 2 and Anlage 3.
  - Checked bad patterns: no `_nr_n` targets, no false `anlage_10`, no false
    `Anlage 2 oder 3 und ...` references, no self-document ReferenceTarget for
    `ersatzbaustoffverordnung`.
- Re-ran VersatzV regression after the extractor/reference changes:
  - Reference graph: 83 nodes, 193 relationships.
  - StructuralUnits include 5 `table` units.
  - `REFERS_TO`: 62.
  - Reference level validation: 0 invalid level mappings.

### Simpler Table/Reference Model
- Updated table chunking: every detected table now produces exactly one
  `table_rows` chunk containing the whole table text. Table notes are no longer
  exported as separate chunks.
- Updated reference resolution: references to unmodeled fine levels
  (`Satz`, `Nummer`, `Buchstabe`) are normalized to the nearest modeled target
  level, typically paragraph or subsection. The pipeline no longer emits
  `partially_resolved` for these cases.
- Re-ran ErsatzbaustoffV:
  - Reference graph: 341 nodes, 1164 relationships.
  - StructuralUnits: 46 table units.
  - Chunks: 46 `table_rows`, 0 `table_note`.
  - `REFERS_TO`: 626; statuses: 548 resolved, 78 unresolved, 0 partially
    resolved.
  - Target levels now exclude sentence/number/letter.
- Re-ran VersatzV:
  - Reference graph: 74 nodes, 187 relationships.
  - StructuralUnits: 5 table units.
  - Chunks: 5 `table_rows`, 0 `table_note`.
  - `REFERS_TO`: 68; statuses: 59 resolved, 9 unresolved, 0 partially
    resolved.

### Complex Reference Chains
- Incorporated manual edge review findings from Gemini:
  - External long-name references may omit `der/des`, e.g.
    `§ 8 Absatz 6 Bundes-Bodenschutz- und Altlastenverordnung`.
  - External long-name references may carry multiple Absatz targets, e.g.
    `§ 8 Absatz 1 und 2 der Entsorgungsfachbetriebeverordnung`.
  - Internal chains may repeat or omit the word `Absatz`, e.g.
    `§ 9 Absatz 1 und Absatz 3 bis 5` and `§ 9 Absatz 1 und 3 bis 5`.
- Updated `extract_reference_relations.py`:
  - Paragraph bodies now include chained Absatz/Qualifier fragments before the
    target law name.
  - External law names can be matched with or without `der/des`.
  - Absatz chains are expanded on the modeled Absatz level.
  - Mixed references such as `§§ 15, 16 Absatz 1` now resolve to `§ 15` and
    `§ 16 Absatz 1`.
- Re-ran ErsatzbaustoffV reference extraction:
  - Reference graph: 350 nodes, 1253 relationships.
  - `REFERS_TO`: 715.
  - Validation checks: 0 invalid level mappings.
  - Targeted bad-edge checks: 0 hits for the reviewed false internal edges.
- Re-ran VersatzV reference extraction:
  - `REFERS_TO`: 68, unchanged.

### Annex Section Split Disabled
- Disabled automatic `annex_section` and `appendix_block` chunk creation in
  `extract_normtext.py`.
- Rationale: numbered decimal lines inside annexes are often form fields or
  table-internal labels, especially in Musteranlagen such as ErsatzbaustoffV
  Anlagen 7 and 8. Treating them as legal sections created misleading graph
  nodes.
- Current annex behavior:
  - Non-table annex content stays in one `annex_text` chunk per annex.
  - Explicit table headings such as `Tabelle 1:` are still converted into
    `table` StructuralUnits with one `table_rows` chunk.
  - Implicit tables without `Tabelle` heading are detected only through narrow
    column-header patterns. This currently covers ErsatzbaustoffV Anlage 5 and
    Anlage 6.
- Re-ran ErsatzbaustoffV:
  - Reference graph: 283 nodes, 1119 relationships.
  - Chunk labels: 102 `subsection`, 46 `table_rows`, 8 `annex_text`, 4
    `paragraph_text`, 2 `waste_code_entry`.
  - `Chunk_annex_section`: 0; `Chunk_appendix_block`: 0.
  - Anlagen 7 and 8 are each represented as one `annex_text` chunk.
- Re-ran VersatzV:
  - Reference graph: 38 nodes, 118 relationships.
  - `Chunk_annex_section`: 0; `Chunk_appendix_block`: 0.

### Implicit Annex Table Detection
- Added implicit table detection in `split_annex_into_chunks`.
  - Recognized header pattern for Anlage 5:
    `Parameter Dimension Bewertungsrelevanter Bereich Norm Normbezeichnung`.
  - Recognized header pattern for Anlage 6:
    `Parameter Dim. Bestimmungsbereich zulässige Überschreitung in %`.
  - These produce `table` StructuralUnits and `table_rows` chunks even without
    an explicit `Tabelle ...` heading.
- Re-ran ErsatzbaustoffV:
  - Raw/content/reference/Cypher outputs refreshed.
  - Reference graph: 287 nodes, 1123 relationships.
  - Anlage 5 now has `ErsatzbaustoffV Anlage 5 Tabelle 1`, 63 row strings, page
    range 116-119.
  - Anlage 6 now has `ErsatzbaustoffV Anlage 6 Tabelle 1`, 32 row strings, page
    range 119-120.
  - Chunk labels: 102 `subsection`, 48 `table_rows`, 8 `annex_text`, 4
    `paragraph_text`, 2 `waste_code_entry`.
- Regression run for VersatzV:
  - Still 3 `table_rows`, 4 `annex_text`, 7 `subsection`, 5 `paragraph_text`.

### Geometric Parsing for Wide Material Tables
- Added a PyMuPDF word-coordinate parser for wide material-value tables.
  - Applies when a detected table title contains `Materialwerte`.
  - Uses visible column header positions instead of line-text splitting.
  - Stores parsed rows as dictionaries keyed by column name where possible.
- Re-ran ErsatzbaustoffV after the change.
  - Anlage 1 Tabelle 1: 36 parsed row dictionaries; columns include `RC-1`
    through `GKOS` and continuation columns `CUM-1` through `HMVA-2`.
  - Anlage 1 Tabelle 2: 11 parsed row dictionaries; columns `GS-0` through
    `GS-3`.
  - Anlage 1 Tabelle 4: 37 parsed row dictionaries; columns combine paired
    material classes such as `BM-F0*, BG-F0*`.
  - Anlage 1 Tabelle 3 is also parsed geometrically, but remains the most
    delicate case because its multi-line soil-class headers and dense values
    are harder to reconstruct perfectly from PDF word positions.
- Rebuilt content graph, reference graph, Cypher export, and imported the new
  ErsatzbaustoffV graph into local Neo4j.
  - Neo4j count: 287 nodes, 1123 relationships.
- Regression run for VersatzV:
  - Counts unchanged: 3 `table_rows`, 4 `annex_text`, 7 `subsection`, 5
    `paragraph_text`.

### Merge Continuation Panels in Wide Tables
- Updated the geometric material-table parser so continuation panels are merged
  into the same row when they share `Parameter` and `Dim.`.
- Re-ran ErsatzbaustoffV:
  - Anlage 1 Tabelle 1 now has 18 wide rows instead of 36 panel rows.
  - Example: `pH-Wert1` now contains both the first block (`RC-1` through
    `GKOS`) and the continuation block (`CUM-1` through `HMVA-2`) in one row.
  - Content graph, reference graph, Cypher export, and local Neo4j import were
    refreshed.
  - Neo4j count unchanged: 287 nodes, 1123 relationships.
- Regression run for VersatzV:
  - Content/reference/Cypher outputs rebuilt; table chunk counts unchanged.

### Nested Groundwater Header Tables
- Added a dedicated geometric parser for Anlage 2 Einbautabellen with nested
  `Eigenschaft der Grundwasserdeckschicht` headers.
  - Trigger is narrow: table text must contain `Eigenschaft der
    Grundwasserdeckschicht`, `Einbauweise`, and `Wasserschutzbereichen`.
  - Header hierarchy is flattened into stable column names.
  - The grouped header numbers `4`, `5`, and `6` are still split into separate
    `Sand` and `Lehm, Schluff, Ton` value columns.
- Re-ran ErsatzbaustoffV:
  - Anlage 2 Tabelle 1 now has 17 rows and 11 columns:
    `Einbauweise Nummer`, `Einbauweise`, and 9 flattened groundwater-condition
    columns.
  - All 27 Anlage 2 tables now use the same 11-column structure.
  - Table row counts range from 1 to 20, reflecting actual table length.
  - Content graph, reference graph, Cypher export, and local Neo4j import were
    refreshed.
  - Neo4j count unchanged: 287 nodes, 1123 relationships.
- Regression run for VersatzV:
  - Content/reference/Cypher outputs rebuilt; counts unchanged.

### Generalized Nested Symbol Table Detection
- Refactored the nested-header table handling so detection is geometric rather
  than only keyword-based.
  - The parser looks for a leaf-number header row and subsequent data rows; it
    does not depend on the concrete cell values.
  - It derives flattened column paths from the header rows above the leaf
    columns.
  - Known federal groundwater-cover tables still receive a template
    normalization step, because their visual header spans are too ambiguous for
    purely geometric path assignment to be fully reliable.
- Re-ran ErsatzbaustoffV:
  - Anlage 2 Tabelle 1 remains correctly parsed with 17 rows and 11 columns.
  - All 27 Anlage 2 tables use the 11-column flattened structure.
  - Content graph, reference graph, Cypher export, and local Neo4j import were
    refreshed.
  - Neo4j count unchanged: 287 nodes, 1123 relationships.
- Regression run for VersatzV:
  - Content/reference/Cypher outputs rebuilt; counts unchanged.

### Grid-Based Nested Symbol Headers
- Removed the remaining groundwater-cover template normalization from the
  nested symbol-table parser.
- The parser now reads PyMuPDF table line geometry and assigns header words to
  their actual grid-cell spans before flattening nested column paths.
  - This handles merged header cells such as grouped conditions and leaf
    columns with shared numbers.
  - Value columns are anchored by grid lines and leaf-number headers, not by
    the text or symbols inside data cells.
  - Data-cell contents are collected generically as cell text.
  - It still falls back to word-position heuristics if a PDF page has no
    usable drawn grid lines.
- Re-ran ErsatzbaustoffV:
  - Anlage 2 Tabelle 1 is parsed from geometry into 11 columns and 17 rows.
  - All 27 Anlage 2 tables use the same 11-column structure.
  - Multiline title rows are excluded from the flattened column names even when
    hyphenation/spacing differs between table label and repeated title row.
  - Content graph, reference graph, and Cypher export were refreshed.
  - Current content graph: 252 nodes, 408 hierarchy/sequence relationships
    before references; reference pass adds 715 `REFERS_TO` relationships and
    35 `ReferenceTarget` nodes.

### Nested Table Values Are Not Structural Triggers
- Follow-up correction: nested-table value columns are no longer inferred from
  symbolic cell contents such as plus/minus markers.
- The parser now derives value-column centers from drawn grid lines at data-row
  height and uses the leaf-number header row only to attach grouped header
  numbers to the resulting leaf columns.
- Data cells are collected as generic text from their grid cells.
- Re-ran ErsatzbaustoffV:
  - Anlage 2 Tabelle 1 still has the expected 11 columns and 17 rows.
  - All 27 Anlage 2 tables retain the expected 11-column structure.
  - Anlage 1 Tabelle 1 remains 20 columns and 18 merged rows.
  - Content graph, reference graph, and Cypher export were refreshed.
