# Node Property Review

Kurze Einschaetzung der aktuellen Neo4j-Node-Properties nach dem VersatzV-Test.
Ziel ist ein schlankeres, aber weiterhin auditierbares Schema.

## Grundsatz

Neo4j braucht nicht jede technische Zwischeninformation als Property. Fuer den
Graphen sollten Properties vor allem drei Zwecke erfuellen:

- stabile Identitaet und Zitierbarkeit
- hilfreiche Anzeige/Filterung in Neo4j und GraphRAG
- Quellen- und Review-Audit, soweit nicht besser in ausgelagerten JSONs liegt

## Wahrscheinlich Behalten

### Alle Content Nodes

- `graph_id` im Neo4j-Export
- `global_key`
- `display_name`
- `legal_citation`
- `is_content_node`
- `node_group`

### Document

- `document_key`
- `title`
- `canonical_citation`
- `source_pdf`
- `sha256`
- `page_count`

### StructuralUnit

- `unit_type`
- `document_key`
- `label`
- `number`
- `title`
- `page_start`
- `page_end`

### Chunk

- `chunk_type`
- `unit_id` oder spaeter besser `unit_graph_id`
- `sequence`
- `page_start`
- `page_end`
- `text`
- `text_sha256`

### ReferenceTarget

- `global_key`
- `status`
- `target_document_key`
- `target_level`
- `reference_kind`
- `display_name`

## Redundanzkandidaten

### ID-Dopplungen

Aktuell gibt es `graph_id` im Neo4j-Export und zusaetzlich typbezogene IDs wie
`document_id`, `unit_id`, `chunk_id`.

Vorschlag:

- In Neo4j langfristig `graph_id` als technische ID verwenden.
- Typbezogene IDs nur behalten, wenn sie in externen JSONs oder Skripten
  wirklich gebraucht werden.
- In Roh-/Content-JSON koennen `unit_id` und `chunk_id` weiterhin praktisch
  sein; im Neo4j-Export muessen sie nicht zwingend doppelt stehen.

### `metadata_json`

`metadata_json` am `Document` enthaelt einige Informationen, die bereits als
eigene Properties existieren oder eher technische Extraktionsdetails sind.

Vorschlag:

- Im Neo4j-Graphen nur die wichtigsten Dokument-Metadaten behalten.
- Technische Extraktionsdetails in Raw-JSON/Page-JSON lassen.

### `text_preview`

Bei Chunks ist `text_preview` aus `text` ableitbar. Bei StructuralUnits ist es
hilfreich, weil deren voller Text nicht im Neo4j-Node liegt.

Vorschlag:

- `Chunk.text_preview` im Neo4j-Export weglassen.
- `StructuralUnit.text_preview` optional behalten.

### Confidence/Review-Felder

`confidence`, `review_status`, `is_uncertain` stehen aktuell fast ueberall, sind
aber im jetzigen deterministischen Stand meist Defaultwerte.

Vorschlag:

- Behalten, sobald Review-Status wirklich gepflegt wird.
- Sonst vorerst aus dem Neo4j-Export entfernen und im Raw-JSON belassen.

### Hierarchie-Arrays

`child_unit_ids` und `child_chunk_ids` duplizieren die Kanten
`CONTAINS_UNIT` und `CONTAINS_CHUNK`.

Vorschlag:

- In Neo4j weglassen, weil die Relation der kanonische Ort der Hierarchie ist.
- In JSON koennen sie fuer Debugging und deterministische Verarbeitung bleiben.

### Tabellen-Details

`columns`, `column_header_text`, `rows_json`, `row_start`, `row_end`,
`row_count_in_chunk` sind fuer Tabellen-Retrieval wichtig, koennen aber den
Graphen optisch aufblaehen.

Vorschlag:

- `column_header_text`, `row_start`, `row_end`, `row_count_in_chunk` behalten.
- `rows_json` behalten, solange Tabellenchunks sonst nicht rekonstruierbar sind.
- `columns` optional entfernen, wenn `column_header_text` reicht.

## Empfohlenes Naechstes Schema

Fuer den Neo4j-Export koennte man eine schlanke Property-Ansicht einfuehren:

- Raw-/Content-JSON bleibt reichhaltig.
- Neo4j bekommt nur GraphRAG- und Visualisierungsproperties.
- Der Exporter filtert Properties label-spezifisch.

Das reduziert die visuelle Unruhe, ohne die Extraktionsdaten zu verlieren.

## Umsetzungsstand

Der Neo4j-Cypher-Exporter filtert Node-Properties inzwischen label-spezifisch.
Die Raw-/Content-JSONs bleiben bewusst reichhaltiger; der Neo4j-Graph bekommt
eine schlankere Ansicht fuer Visualisierung und Retrieval.
