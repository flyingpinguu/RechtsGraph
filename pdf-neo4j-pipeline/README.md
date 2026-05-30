# PDF Neo4j Pipeline

Deterministische Normtext-Pipeline fuer die erste GraphRAG-Schicht.

## Aktueller Stand

Aktive Skripte:

- `scripts/extract_normtext.py`
- `scripts/build_review_shards.py`
- `scripts/extract_content_nodes.py`
- `scripts/extract_reference_relations.py`
- `scripts/export_neo4j_cypher.py`

Aktive Artefakte:

- `output/raw_versatzv_current.json`
- `output/pages/versatzv/page_001.json` bis `page_015.json`
- `output/review_shards/VersatzV_current/`

Alte Review-Laeufe, Usage-Logs, Review-Prompts und Zwischenversionen wurden
entfernt. Lokale Reviewer werden vorerst nicht weiter genutzt.

## Extraktion

```bash
cd /Users/christinck/Documents/graph_database/pdf-neo4j-pipeline
./.venv/bin/python scripts/extract_normtext.py \
  --output output/raw_versatzv_current.json \
  ../abfall_pdfs/09_VersatzV.pdf
```

Die Haupt-JSON speichert keine Seitentexte direkt. Seiten liegen ausgelagert
unter `output/pages/<document_key>/page_XXX.json`; das Dokument enthaelt nur
`page_refs` und eine textfreie `pages`-Kompatibilitaetsliste.

## Review-Shards

```bash
./.venv/bin/python scripts/build_review_shards.py \
  --input output/raw_versatzv_current.json \
  --document-source 09_VersatzV.pdf \
  --out-dir output/review_shards/VersatzV_current \
  --pages-per-shard 3
```

Shard-Seitenmodell:

- `nominal_page_range`: Seiten, fuer die streng Vollstaendigkeit gilt.
- `nominal_pages`: textfreie Seitenmetadaten fuer diese Seiten.
- `nominal_page_numbers`: kompakte Seitenzahl-Liste.
- `evidence_page_range`: geladener Kontextbereich.
- `evidence_pages`: einzige Stelle im Shard mit vollem Seitentext.
- `evidence_page_numbers`: kompakte Seitenzahl-Liste.

Das alte doppelte Feld `pages` wird nicht mehr geschrieben.

## Tabellen-Validierung

Nach jeder Änderung am Tabellenparser zuerst die Roh-Extraktion neu erzeugen
und dann die erwarteten Tabellenformen prüfen:

```bash
./.venv/bin/python scripts/validate_table_shapes.py \
  --input output/raw_ersatzbaustoffv_current.json \
  --contains 'Anlage 3 Tabelle' \
  --expect-columns 11 \
  --expect-rows 26 \
  --row-marker-regex '^B[0-9]+$'
```

Weitere schnelle Regressionen:

```bash
./.venv/bin/python scripts/validate_table_shapes.py \
  --input output/raw_ersatzbaustoffv_current.json \
  --contains 'Anlage 2 Tabelle' \
  --expect-columns 11

./.venv/bin/python scripts/validate_table_shapes.py \
  --input output/raw_ersatzbaustoffv_current.json \
  --contains 'Anlage 1 Tabelle 1' \
  --expect-columns 20 \
  --expect-rows 18
```

## Modellierungsentscheidung

Das kanonische Modell bleibt flach:

- Normstruktur steht in `structural_units`.
- Retrieval-Text steht in `chunks`.
- Hierarchie laeuft ueber `parent_unit_id`, `child_unit_ids`,
  `parent_chunk_id`, `child_chunk_ids`.
- `global_key` und daraus abgeleitete `unit_id`/`chunk_id` nutzen den
  normalisierten ausgeschriebenen Normnamen oder Kurztitel, z.B.
  `versatzverordnung_para_4`.
- `document_key` bleibt als kurzer Alias erhalten, z.B. `versatzv`.

Tabellen bleiben `table`-Units unter Anlagen. Wenn eine Tabelle mehrere
Abschnitte hat, z.B. `Tabelle 2` mit `Anorganische Stoffe` und `Organische
Stoffe`, bleibt sie eine gemeinsame `table`-Unit. Die einzelnen `table_rows`
und `table_note`-Chunks tragen dann `table_section` und passende
`column_header_text`.

## Content-Node-Export

```bash
./.venv/bin/python scripts/extract_content_nodes.py \
  --input output/raw_versatzv_current.json \
  --output output/content_nodes/versatzv_content_graph.json
```

Der Export erzeugt `Document`-, `StructuralUnit`- und `Chunk`-Nodes sowie
`CONTAINS_UNIT`, `CONTAINS_CHUNK`, `NEXT_UNIT` und `NEXT_CHUNK`-Relationen.
PDF-Seiten bleiben externe Belegdateien und werden nicht als Neo4j-Nodes
geschrieben.

## Referenz-Relationen

```bash
./.venv/bin/python scripts/extract_reference_relations.py \
  --input output/content_nodes/versatzv_content_graph.json \
  --output output/content_nodes/versatzv_content_graph_with_refs.json
```

Der Referenz-Export ergänzt deterministisch erkannte Normverweise als
`REFERS_TO`-Relationen. Erkennung passiert auf Chunk-Ebene; daraus werden
zusaetzlich StructuralUnit- und Document-Level-Relationen abgeleitet. Nicht
vorhandene Ziele werden als `ReferenceTarget`-Placeholder angelegt.

## Mehrdokument-Merge

Mehrere Dokumente werden zuerst einzeln bis zum Content-Graph extrahiert, dann
zu einem gemeinsamen Content-Graph gemergt und erst danach durch die
Referenz-Extraktion geschickt. Dadurch koennen externe Verweise direkt auf
vorhandene Nodes geschlossen werden, sobald das Zielgesetz im Merge enthalten
ist.

```bash
./.venv/bin/python scripts/merge_content_graphs.py \
  --input output/multi_doc/content_graphs/*.json \
  --output output/multi_doc/merged/merged_content_graph.json

./.venv/bin/python scripts/extract_reference_relations.py \
  --input output/multi_doc/merged/merged_content_graph.json \
  --output output/multi_doc/merged/merged_content_graph_with_refs.json
```

## Neo4j-Cypher-Export

```bash
./.venv/bin/python scripts/export_neo4j_cypher.py \
  --input output/content_nodes/versatzv_content_graph_with_refs.json \
  --output output/neo4j/versatzv_import_with_refs.cypher
```

Import per `cypher-shell`:

```bash
cypher-shell -a neo4j://127.0.0.1:7687 -u neo4j -p '<password>' -f output/neo4j/versatzv_import_with_refs.cypher
```

In einer reinen Testdatenbank vorher alte Daten loeschen:

```cypher
MATCH (n)
DETACH DELETE n;
```

Der Export nutzt `ContentNode` nicht als Neo4j-Label. Inhaltliche Nodes tragen
stattdessen `is_content_node` und `node_group` als Properties; fuer Farben und
Filter in Neo4j eignen sich die konkreten Labels `Document`, `StructuralUnit`,
`Chunk`, `ReferenceTarget` und die Subtyp-Labels.

Danach im Neo4j Browser:

```cypher
MATCH p=(d:Document {document_key: 'versatzv'})-[:CONTAINS_UNIT|CONTAINS_CHUNK*1..3]->(n)
RETURN p
LIMIT 200;
```
