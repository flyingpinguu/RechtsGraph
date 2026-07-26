# GII XML Migration Plan

## Implementation Result (2026-07-26)

The cutover is implemented on branch `codex/gii-xml-migration`. The
pre-migration implementation remains recoverable at commit `4341102` and tag
`pre-gii-xml-migration`.

The reconciled corpus contains 6,133 records:

- 6,125 GII catalog XML packages, all downloaded, validated, and parsed;
- 5,954 XML records with a matching local PDF;
- 171 XML-only records, accepted without fabricated page provenance; and
- 8 PDF-manifest-only records, handled by the explicit legacy fallback.

The 5,970-row PDF manifest reconciles as 5,954 distinct XML matches, 8
duplicate PDF aliases attached to an already matched XML record, and the 8
PDF-only records.

The audited XML output contains 139,428 structural units, 324,989 chunks,
11,567 CALS tables with 236,318 rows, and 4,160 referenced source assets.
Document-, unit-, and chunk-ID collisions are zero. All source assets are
present. The three mandatory difficult cases pass:

- `ErsatzbaustoffV Anlage 1 Tabelle 1`: 20 columns and 18 rows;
- `EGBGB Art 232`: the article and its 12 nested paragraphs are retained; and
- `AbfKlärV`: the two known rank conflicts remain explicit, classified
  warnings rather than silent hierarchy damage.

PDF alignment is conservative and secondary. It supplies page evidence to
95.27% of chunks and 90.88% of structural units. Missing or low-confidence
alignment never removes XML content.

All nine remaining `POSSIBLE_MISSED_TABLE` warnings were manually traced to
the source. Seven are title-association warnings whose CALS cells are present;
two preserve GII's own “table not representable” placeholders where neither
the XML ZIP nor the matching PDF contains recoverable table data.

Final verification:

- the downloader resumes with 6,125 verified XML packages, 8 explicit
  `xml_unavailable` PDF-only records, and no process errors;
- all 137 repository tests pass;
- the independent corpus audit passes 6,125 documents with zero invariant
  failure kinds;
- the generic raw validator reports zero errors (remaining warnings are
  review/coverage signals);
- both XML and PDF-fallback batches pass source-, version-, rules-, and
  page-artifact-aware resume checks; and
- a 12-document downstream smoke (four XML hard cases plus all eight
  fallbacks) passes merge, reference extraction, graph validation, ID/edge
  integrity, and Neo4j Cypher export.

“Authoritative” below is an engineering source-of-truth statement inside this
pipeline. It does not change the legal status of GII's consolidated texts;
the promulgated Bundesgesetzblatt remains the official legal publication.

### Why the adapter is still custom

“GII XML adapter” means the schema-specific boundary that translates the
official GII XML/DTD into this repository's existing canonical graph contract.
It is not a generic XML parser. Generic document frameworks can parse bytes
and provide useful PDF layout/OCR primitives, but they do not define the
project's legal-unit identities, parentage, mixed-content rules, CALS
semantics, graph keys, or reconciliation with the official GII catalog.

Docling would therefore be useful as an optional alternative implementation
behind the *PDF evidence/fallback* interface, especially for scans or
non-GII documents. It would not replace the GII adapter or improve on
pipeline-authoritative XML as the semantic source. A future Docling adoption
should be an evidence-based benchmark against the retained PDF extractor, not
a second semantic source of truth.

### Reproduction

```bash
cd /Users/christinck/Documents/graph_database/pdf-neo4j-pipeline

./.venv/bin/python scripts/download_gii_xml.py --workers 8 --resume
./.venv/bin/python scripts/batch_extract_gii_xml.py --workers 8 --resume
./.venv/bin/python scripts/batch_extract_gii_pdf_fallbacks.py \
  --workers 2 --resume
./.venv/bin/python scripts/audit_gii_xml_corpus.py --require-hard-cases
./.venv/bin/python -m pytest -q
```

## Objective

Replace PDF-first semantic extraction for `gesetze-im-internet.de` documents
with an XML-first ingestion path while preserving:

- the existing raw normtext contract consumed by the graph pipeline;
- stable legal identifiers and cross-document reference extraction;
- complex-table semantics, including hierarchical headers and spans;
- PDF page and geometry evidence when a matching PDF is available; and
- the deterministic PDF extractor as a recoverable fallback.

The pre-migration implementation is preserved by Git commit `4341102` and tag
`pre-gii-xml-migration`.

## Target Architecture

```text
GII XML ZIP ──> GII XML adapter ───────────────┐
                                               │
GII PDF ─────> page/geometry alignment ────────┼─> canonical raw normtext JSON
                                               │
other PDF ───> legacy deterministic extractor ─┘
                                                        │
                                                        v
                                  content graph -> reference graph -> Neo4j
```

XML is the pipeline-authoritative source for text, metadata, legal units,
lists, footnotes, and table cell/span structure. PDF data is secondary
provenance: page numbers, bounding boxes, page images, and a fallback for
source material absent from the XML package.

## Corpus

The daily GII XML table of contents (`gii-toc.xml`) is the authoritative
download index. As of 2026-07-26 it contains 6,125 XML packages. The existing
PDF manifest contains 5,970 entries, so download and parsing must retain XML
entries without a matching local PDF. Matching is based primarily on the GII
URL slug and secondarily on normalized title/metadata.

Downloaded packages, extracted source files, partial downloads, and generated
outputs are local artifacts and must not be committed.

## Canonical Contract

The adapter emits the existing top-level normtext envelope:

- `schema_version`
- `phase`
- `source_manifest_entry`
- `documents`
- `review_decisions`
- `extraction_issues`

Each document continues to expose metadata, `page_refs`, `structural_units`,
and `chunks` expected by the validators, graph exporter, renderer, and
reference extractor. XML-specific provenance is additive and must not require
downstream consumers to special-case the source.

New provenance fields may include:

- `source_format`
- `source_xml`
- `source_xml_sha256`
- `source_xml_document_number`
- `source_xml_build_date`
- `source_assets`
- `pdf_alignment_status`
- `pdf_alignment_confidence`
- cell-level row/column span and source-path information

## Implementation Phases

### 1. Recoverable Baseline

- Commit and tag the complete pre-migration code.
- Record known tests and known failures.
- Keep the legacy extractor callable throughout the migration.

### 2. Download and Manifest

- Read the authoritative `gii-toc.xml`.
- Reconcile entries with the existing PDF manifest.
- Download concurrently with bounded retries and timeouts.
- Write files atomically and validate ZIP/XML payloads before promotion.
- Record URL, title, slug, hashes, sizes, XML document/build identifiers,
  assets, PDF match, status, and errors in a resumable manifest.
- Remove failed partial files and stale extraction directories.

### 3. XML Adapter

- Parse document and norm metadata.
- Classify `§`, `Art`, Anlagen/Anhänge, formulas, Roman/lettered provisions,
  and hierarchy-only norms without relying on PDF typography.
- Preserve mixed inline content, nested definition lists, revisions,
  citations, notes, footnotes, images, and file attachments.
- Parse CALS tables using `colspec`, `spanspec`, `namest`, `nameend`,
  `spanname`, `colname`, and `morerows`.
- Preserve the lossless physical table grid and derive stable semantic leaf
  columns for the existing row-oriented graph representation.

### 4. PDF Alignment

- Match normalized XML unit/chunk text to extracted PDF page text.
- Attach page ranges and optional bounding boxes with a confidence score.
- Treat missing PDF matches as valid XML parses, not extraction failures.
- Fall back to legacy PDF semantics only for absent/invalid XML or explicitly
  unsupported source structures.

### 5. Validation and Iteration

- Unit-test XML mixed content, legal-unit classification, nesting, footnotes,
  assets, CALS spans, malformed input, and deterministic identifiers.
- Maintain regression fixtures for normal statutes, article laws,
  non-paragraph orders, forms, and table-heavy regulations.
- Compare XML output against hard expectations and the legacy parser rather
  than assuming either implementation is ground truth.
- Run the full corpus, triage every process error and structural/table blocker,
  and re-run until the migration gates pass.

### 6. Cutover

- Make the batch pipeline XML-primary.
- Retain an explicit `legacy_pdf` route.
- Document download, extraction, resume, validation, and cleanup commands.
- Delete disposable benchmark and partial-download artifacts.

## Migration Gates

The XML path is ready to become the default only when:

1. every valid downloaded XML package parses or has a classified, reviewable
   source error;
2. generated raw JSON passes schema/ID/hierarchy validation;
3. no table is silently dropped;
4. complex-table fixtures preserve expected cell text and span semantics;
5. legal units present in XML are not lost merely because their labels are not
   `§` or `Anlage`;
6. downstream content-graph and reference extraction tests pass;
7. repeated runs produce stable IDs and content hashes;
8. PDF alignment failures degrade to missing provenance, not missing content;
9. the legacy fallback remains executable; and
10. the downloaded corpus and generated temporary data are cleanly separated
    from tracked source.
