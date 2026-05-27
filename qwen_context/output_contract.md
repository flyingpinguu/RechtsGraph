# Output Contract — JSON-Konzept für Normtext-Schicht

## Überblick

Dieses Dokument definiert das JSON-Format für die Ausgabe der
Normtext-Extraktion (Phase 1). Es gilt für alle Qwen-Subagenten
und die Python-Pipeline-Skripte.

## Schema-Version

```
schema_version: "1.0.0-draft"
phase: "normtext"
```

## Wurzelebene

```json
{
  "schema_version": "1.0.0-draft",
  "phase": "normtext",
  "documents": [ ... ],
  "review_decisions": [ ... ],
  "extraction_issues": [ ... ]
}
```

---

## Document

Repräsentiert ein einzelnes Gesetzes-PDF.

| Feld | Typ | Beschreibung |
|---|---|---|
| `document_id` | string | Stabile ID: `doc_<sha256_short>` (erste 12 Zeichen des SHA256 des PDFs) |
| `source_pdf` | string | Pfad/Name der PDF-Datei |
| `sha256` | string | SHA256-Hash des gesamten PDFs |
| `title` | string | Gesetzestitel (z.B. "Kriftverordnung") |
| `full_citation` | string | Vollständige Zitierweise (z.B. "KriftV vom 15.03.2017, BGBl. I S. 478") |
| `canonical_citation` | string | Kanonische Kurzform (z.B. "KriftV") |
| `document_key` | string | Maschinenlesbarer Dokument-Key für globale IDs, z.B. `versatzv` |
| `citation_prefix` | string | Zitierpräfix für menschenlesbare Referenzen, z.B. `VersatzV` |
| `abbreviation` | string | Offizielle Abkürzung, wenn erkannt |
| `date_enacted` | string | Ausfertigungsdatum (ISO 8601) |
| `date_in_force` | string | Inkrafttretensdatum (ISO 8601) |
| `pages` | array | Seitenreferenzen ohne Rohtext; Alias/Kompatibilitätsfeld zu `page_refs` |
| `page_refs` | array | Liste aller ausgelagerten Seiten-JSON-Dateien |
| `structural_units` | array | Hierarchische Normstruktur |
| `metadata` | object | Zusätzliche Metadaten (optional) |

### page_refs / pages

Im Haupt-JSON werden Seitentexte nicht gespeichert. Jede Seite liegt als
separate JSON-Datei unter `output/pages/<document_key>/page_XXX.json`.
Der Review-Shard-Builder lädt die benötigten Seiten anhand von `path`.

| Feld | Typ | Beschreibung |
|---|---|---|
| `page_id` | string | Stabile ID: `page_<document_id_short>_<3-stellige Seitennummer>` |
| `page_number` | integer | Gedruckte Seitennummer (1-basiert) |
| `pdf_page_index` | integer | 0-basierter PDF-Seitenindex |
| `path` | string | Relativer Pfad zur separaten Seiten-JSON |
| `text_sha256` | string | SHA256 des Seiten_texts |
| `text_extraction_backend` | string | Verwendeter PDF-Text-Backend |

---

## StructuralUnit

Repräsentiert eine Hierarchieebene der Normstruktur.

| Feld | Typ | Beschreibung |
|---|---|---|
| `unit_id` | string | Stabile, zitiernahe ID: z.B. `unit_versatzv_para_4` |
| `global_key` | string | Maschinenlesbarer globaler Key ohne Prefix, z.B. `versatzv_para_4` |
| `legal_citation` | string | Menschlich zitierbarer Name, z.B. `VersatzV § 4` |
| `display_name` | string | Lesbarer Name inklusive Titel, soweit verfügbar |
| `document_id` | string | Verweis auf das Document |
| `document_key` | string | Maschinenlesbarer Dokument-Key |
| `unit_type` | string | Aktuell vor allem: `paragraph`, `annex`, `table`, `waste_code`; später auch `part`, `chapter`, `section` |
| `label` | string | Beschriftung (z.B. "§ 1", "(1)", "Anlage 1") |
| `number` | string | Numerierung (z.B. "1", "2a", "3.1") |
| `title` | string | Überschrift der Einheit, wenn vorhanden |
| `breadcrumbs` | array | Pfad der übergeordneten Einheiten |
| `parent_unit_id` | string | ID der übergeordneten Einheit (null wenn Wurzel) |
| `child_unit_ids` | array | Direkte Kind-Units, z.B. Tabellen unter einer Anlage |
| `page_range` | object | `{ "start": <int>, "end": <int> }` |
| `text` | string | Volltext der Einheit |
| `columns` | array | Bei Tabellen: erkannte Spaltenbeschriftungen |
| `column_header_text` | string | Bei Tabellen: originale Kopfzeile |
| `row_count` | integer | Bei Tabellen: Anzahl erkannter Zeilen |
| `table_sections` | array | Bei Tabellen mit mehreren Kopfzeilen/Abschnitten: Abschnittsnamen, z.B. `Anorganische Stoffe`, `Organische Stoffe` |
| `confidence` | float | 0.0–1.0, Extraktions-Sicherheit |
| `review_status` | string | Einer von: `pending`, `accepted`, `rejected`, `needs_review` |
| `is_uncertain` | boolean | true wenn Hierarchie unklar war |
| `uncertainty_reason` | string | Grund bei `is_uncertain: true` |

---

## Chunk

Repräsentiert einen Textabschnitt innerhalb einer StructuralUnit.
Dient als feingranulare Einheit für Review und spätere Semantik-Extraktion.

| Feld | Typ | Beschreibung |
|---|---|---|
| `chunk_id` | string | Stabile, zitiernahe ID: z.B. `chunk_versatzv_para_4_abs_2` oder `chunk_versatzv_anlage_2_tabelle_1_rows_1_10` |
| `global_key` | string | Maschinenlesbarer globaler Key ohne Prefix |
| `legal_citation` | string | Menschlich zitierbarer Name, z.B. `VersatzV § 4 Abs. 2` oder `VersatzV Anlage 2 Tabelle 1 Zeilen 1-10` |
| `display_name` | string | Lesbarer Name für UI/Review |
| `chunk_type` | string | Einer von: `paragraph_text`, `subsection`, `annex_text`, `annex_section`, `table_rows`, `table_note`, `appendix_block`, `waste_code_entry` |
| `unit_id` | string | Verweis auf die übergeordnete StructuralUnit |
| `parent_chunk_id` | string | Übergeordneter Chunk, derzeit meist null |
| `child_chunk_ids` | array | Direkte Kind-Chunks |
| `label` | string | Lokales Label, z.B. `Abs. 2`, `1.`, `a)` |
| `number` | string/integer | Lokale Nummer |
| `sequence` | integer | Reihenfolge innerhalb des Parent-Kontexts |
| `page_id` | string | Seite, auf der der Chunk beginnt |
| `page_range` | object | `{ "start": <int>, "end": <int> }` |
| `text` | string | Chunk-Text |
| `text_sha256` | string | SHA256 des Chunk-Texts |
| `evidence_text` | string | Originaltext aus dem PDF |
| `row_range` | object | Bei `table_rows`: `{ "start": <int>, "end": <int> }` |
| `table_section` | string | Bei `table_rows`/`table_note`: Abschnitt innerhalb einer Tabelle, wenn vorhanden |
| `columns` | array | Bei `table_rows`: Spaltenbeschriftungen, in `text` ebenfalls explizit enthalten |
| `column_header_text` | string | Bei `table_rows`: originale Kopfzeile |
| `rows` | array | Bei `table_rows`: Zeilen dieses Chunks, standardmäßig maximal 40 |
| `confidence` | float | 0.0–1.0 |
| `review_status` | string | Einer von: `pending`, `accepted`, `rejected`, `needs_review` |

---

## ExtractionIssue

Dokumentiert ein Problem in der Extraktion.

| Feld | Typ | Beschreibung |
|---|---|---|
| `issue_id` | string | Stabile ID: `issue_<document_id_short>_<sequential>` |
| `document_id` | string | Betroffenes Document |
| `target_unit_id` | string | Betroffene StructuralUnit (kann null sein) |
| `target_chunk_id` | string | Betroffener Chunk (kann null sein) |
| `issue_type` | string | Einer von: `missing_hierarchy`, `wrong_page`, `invented_unit`, `truncated_text`, `missing_paragraph`, `wrong_label`, `uncertain_hierarchy`, `table_not_parsed`, `annex_not_found` |
| `severity` | string | Einer von: `critical`, `major`, `minor` |
| `description` | string | Menschlich lesbare Beschreibung |
| `evidence` | string | Referenztext aus dem Original-PDF |
| `corrected_value` | string | Korrekter Wert (kann null sein) |
| `review_status` | string | Einer von: `open`, `addressed`, `accepted_as_known_issue` |

---

## ReviewDecision

Entscheidung eines Reviews über eine Einheit oder den gesamten Dokumentenabschnitt.

| Feld | Typ | Beschreibung |
|---|---|---|
| `review_id` | string | Stabile ID: `review_<document_id_short>_<sequential>` |
| `document_id` | string | Betroffenes Document |
| `target_unit_id` | string | Betroffene StructuralUnit (kann null sein, dann gilt für gesamtes Document) |
| `target_chunk_id` | string | Betroffener Chunk (kann null sein) |
| `decision` | string | Einer von: `accepted`, `rejected`, `needs_review` |
| `reviewer` | string | "qwen" oder "human" |
| `reason` | string | Begründung der Entscheidung |
| `issues_found` | array | Liste von issue_id-Strings |
| `corrected_text` | string | Korrigierter Text (kann null sein) |
| `timestamp` | string | ISO 8601 Zeitpunkt des Reviews |

---

## ID-Generierungsregeln

1. **document_id:** `doc_` + erste 12 Zeichen des SHA256 des PDFs
2. **page_id:** `page_` + erste 6 Zeichen von document_id + `_` + 3-stellige, null-polierte Seitennummer
3. **unit_id:** `unit_` + `document_key` + juristischer Pfad, z.B. `unit_versatzv_para_4`
4. **chunk_id:** `chunk_` + `document_key` + juristischer Pfad, z.B. `chunk_versatzv_para_4_abs_2`; Tabellenchunks erhalten einen Zeilenbereich, z.B. `chunk_versatzv_anlage_2_tabelle_1_rows_1_40`
5. **issue_id:** `issue_` + erste 6 Zeichen von document_id + `_` + fortlaufende Nummer
6. **review_id:** `review_` + erste 6 Zeichen von document_id + `_` + fortlaufende Nummer

Alle IDs sind deterministisch und rebuildbar.

## Validierungsregeln

- Jedes Document hat mindestens eine Page-Referenz
- Jede StructuralUnit hat document_id, unit_type, label, legal_citation und page_range
- Jede StructuralUnit hat einen parent_unit_id oder ist Wurzel
- Jeder Chunk hat eine unit_id und text
- page_number ist 1-basiert und positiv
- confidence ist zwischen 0.0 und 1.0
- review_status ist einer der erlaubten Werte
