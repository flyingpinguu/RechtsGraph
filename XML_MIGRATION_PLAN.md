# GII XML Migration Plan

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

XML is authoritative for text, metadata, legal units, lists, footnotes, and
table cell/span structure. PDF data is secondary provenance: page numbers,
bounding boxes, page images, and a fallback for source material absent from
the XML package.

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
