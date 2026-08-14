# RechtsGraph

German and EU law as a knowledge graph.

Deterministische Normtext-Pipeline fuer die erste GraphRAG-Schicht.

Fuer `gesetze-im-internet.de` ist XML jetzt die primaere technische
Inhaltsquelle:

- XML liefert Wortlaut, Metadaten, Normhierarchie, Listen, Fussnoten,
  Anlagen und CALS-Tabellen einschliesslich Zellspannen.
- Das passende PDF liefert nur sekundaere Provenienz wie Seitenbereiche und
  bleibt visuelle Referenz.
- Fehlt ein sicherer PDF-Treffer, bleibt der XML-Inhalt vollstaendig und der
  Seitenbereich bewusst leer.
- Nur acht Korpus-Eintraege ohne offizielles XML laufen ueber den erhaltenen
  deterministischen PDF-Fallback.

Der **GII-XML-Adapter** ist die projektspezifische Uebersetzung aus dem
offiziellen GII-XML/DTD in den bestehenden kanonischen Vertrag aus
`Document`, `StructuralUnit` und `Chunk`. Er ist damit kein allgemeiner
XML-Reader und kein externes Produkt.

Die alte PDF-zentrierte Version bleibt durch Commit `4341102` und Tag
`pre-gii-xml-migration` reproduzierbar.

## Installation

Vorausgesetzt werden Python 3.9 oder neuer und fuer den Graphimport eine
laufende Neo4j-Instanz. Das Repository enthaelt nur Pipeline-Code, Tests und
Dokumentation; heruntergeladene XML-/PDF-Korpora, generierte JSONs und
Neo4j-Importdateien bleiben lokal.

```bash
cd pdf-neo4j-pipeline
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m pytest
```

Alle folgenden Befehle gehen davon aus, dass sie aus
`pdf-neo4j-pipeline/` gestartet werden.

## Aktueller Stand

Aktive Skripte:

- `scripts/download_gii_xml.py`
- `scripts/batch_extract_gii_xml.py`
- `scripts/batch_extract_gii_pdf_fallbacks.py`
- `scripts/audit_gii_xml_corpus.py`
- `scripts/extract_normtext.py`
- `scripts/build_review_shards.py`
- `scripts/extract_content_nodes.py`
- `scripts/extract_reference_relations.py`
- `scripts/export_neo4j_cypher.py`

Vollkorpus-Stand vom 2026-07-26:

- 6.125 GII-Katalog-XML-Pakete heruntergeladen und erfolgreich adaptiert.
- 5.954 XML-Dokumente mit lokalem PDF, 171 XML-Dokumente ohne PDF.
- Acht explizite PDF-Fallbacks fuer Eintraege ohne offizielles XML.
- 139.428 Structural Units, 324.989 Chunks und 11.567 CALS-Tabellen.
- 95,27 % der XML-Chunks erhielten konservativ abgeleitete PDF-Seitenbelege.
- Vollstaendig eindeutige Dokument-, Unit- und Chunk-IDs; keine fehlenden
  Quelldateien oder Tabellenformfehler im unabhaengigen Korpusaudit.
- Neun verbleibende `POSSIBLE_MISSED_TABLE`-Warnungen wurden einzeln
  geprueft: sieben sind Titelzuordnungswarnungen bei vollstaendig vorhandenen
  CALS-Zellen, zwei sind bereits im GII-XML als nicht darstellbar
  gekennzeichnete Quellluecken.

Die generierten Korpora liegen unter `gesetze_im_internet_xml/`,
`output/gii_xml/` und `output/gii_pdf_fallbacks/` und werden nicht committet.
„Primaer“ bezeichnet hier die technische Source-of-Truth-Entscheidung der
Pipeline, nicht den rechtlichen Status: GII stellt konsolidierte, nicht
amtliche Fassungen bereit; fuer die amtliche Verkuendung bleibt das
Bundesgesetzblatt massgeblich.

## XML-Vollkorpus

Alle Befehle koennen aus diesem Verzeichnis mit den dokumentierten Defaults
ausgefuehrt werden:

```bash
# Offiziellen Tageskatalog lesen, XML-ZIPs atomar laden und mit dem
# vorhandenen PDF-Manifest abgleichen.
./.venv/bin/python scripts/download_gii_xml.py --workers 8 --resume

# XML in den kanonischen Raw-Vertrag ueberfuehren und, soweit sicher
# moeglich, PDF-Seitenbelege anhaengen.
./.venv/bin/python scripts/batch_extract_gii_xml.py --workers 8 --resume

# Nur die acht manifestierten Eintraege ohne XML verarbeiten.
./.venv/bin/python scripts/batch_extract_gii_pdf_fallbacks.py \
  --workers 2 --resume

# Vollstaendigkeit, IDs, Hierarchie, Assets, Tabellen und bekannte
# Komplexfaelle unabhaengig pruefen.
./.venv/bin/python scripts/audit_gii_xml_corpus.py --require-hard-cases

./.venv/bin/python scripts/validate_parsed_jsons.py \
  --input output/gii_xml/raw \
  --recursive \
  --skip-page-files \
  --fail-on error \
  --report output/gii_xml/validation/parsed_json_validation.md \
  --json-report output/gii_xml/validation/parsed_json_validation.json
```

`--force` baut einen Korpus neu auf; `--resume` verwendet nur Artefakte
wieder, deren Quellhash, Modus und Alignment-Version noch passen. Downloads
und Raw-Ausgaben werden zuerst in Staging-Dateien geschrieben und erst nach
Validierung atomar sichtbar gemacht. Ein fehlendes oder nicht ausrichtbares
PDF ist kein XML-Extraktionsfehler. Die acht nachweislich nur als PDF
vorliegenden Eintraege werden im Downloader als `xml_unavailable`, nicht als
Prozessfehler, ausgewiesen und danach vom engen PDF-Fallback uebernommen.

Der ausfuehrliche Entscheidungs- und Implementierungsnachweis steht in
`../XML_MIGRATION_PLAN.md`.

## EUR-Lex-Konsolidierungen aus Cellar

Der deutsche konsolidierte EU-Rechtsbestand wird separat aus Cellar bezogen.
Der Downloader inventarisiert konsolidierte Sektor-3-Akte descriptorweise ueber
SPARQL, waehlt je Basisrechtsakt den neuesten Stand bis zum Korpus-Stichtag und
laedt bevorzugt Formex 4. Aeltere Manifestationen fallen kontrolliert auf XHTML,
HTML oder PDF zurueck. Cellar-Works ohne deutsche Expression bleiben als
`unavailable_deu` im Register sichtbar.

```bash
./.venv/bin/python scripts/download_cellar_consolidated.py \
  --snapshot-date 2026-08-09 \
  --workers 8 \
  --resume
```

Standardausgabe ist `../eurlex_consolidated_de/` mit `register.json`,
`register.jsonl`, `SUMMARY.md` und den komprimierten Manifestationen unter
`packages/<descriptor>/`.

Der Formex-Adapter ueberfuehrt konsolidierte Verordnungen (`R`) und Richtlinien
(`L`) in denselben kanonischen Vertrag wie der deutsche GII-Adapter. CELEX-Keys
bilden stabile Dokument-, Artikel-, Absatz- und Anlagenadressen. Komplexe
Formex-Tabellen behalten Zellspannen, Kopfzeilen, Notizen und Asset-Verweise;
Tabellen ueber 3.500 Tokens werden zeilenweise geteilt und wiederholen Titel,
Spalten- und Tabellenkopf. Eine einzelne uebergrosse Tabellenzeile bleibt
atomar, damit keine Zelle zerschnitten wird.

```bash
# Formex in kanonische Raw-JSONs ueberfuehren.
./.venv/bin/python scripts/batch_extract_eurlex_formex.py \
  --workers 4 --resume

# Struktur, Hashes, IDs und Tabellenlimits pruefen.
./.venv/bin/python scripts/audit_eurlex_formex_corpus.py

# Getrennte, speicherschonende Content-Graphen und Artikel-Lookup bauen.
./.venv/bin/python scripts/batch_build_eurlex_content_graphs.py \
  --workers 4 --resume
./.venv/bin/python scripts/build_eurlex_article_lookup.py

# Neo4j-CSV und idempotentes LOAD-CSV-Skript erzeugen. Das CSV-Verzeichnis
# muss unter dem import-Verzeichnis der jeweiligen Desktop-Instanz liegen.
./.venv/bin/python scripts/export_eurlex_manifest_neo4j_csv.py \
  --graph-manifest output/eurlex_formex_rl/content_graph_manifest.json \
  --csv-dir "$NEO4J_IMPORT/eurlex_formex_rl" \
  --csv-uri-prefix file:///eurlex_formex_rl/ \
  --output-cypher output/eurlex_formex_rl/neo4j/eurlex_formex_rl_load.cypher

cypher-shell -d gii-xml-chunked \
  -f output/eurlex_formex_rl/neo4j/eurlex_formex_rl_load.cypher
```

Formex-ZIP und Raw-JSON bleiben die vollstaendige Inhalts- und
Provenienzquelle. Neo4j enthaelt die gemeinsame Abfrageebene aus Metadaten,
Struktur, Retrieval-Chunks und Beziehungen; grosses XML-Markup, Assets und
vollstaendiges `table_data` werden dort nicht dupliziert. Der kompakte Lookup
unter `output/eurlex_formex_rl/lookup/article_lookup.json` fuehrt von CELEX,
Artikel und Absatz zu den zugehoerigen Chunk-IDs.

Vollkorpus-Stand vom 2026-08-10:

- 5.084 konsolidierte Formex-Akte (`R`: 4.092, `L`: 992), ohne technische
  Extraktionsfehler.
- 371.208 Structural Units, 503.816 Chunks, 89.750 Artikel, 16.192 Anlagen
  und 40.353 Tabellen.
- Korpusaudit ohne harte Findings; 289 atomar erhaltene Tabellenzeilen liegen
  allein bereits ueber dem 3.500-Token-Limit.
- Import in `gii-xml-chunked`: zusammen mit dem deutschen Bestand 1.442.529
  Knoten und 4.390.330 Kanten.
- 838.202 konkrete EU-zu-EU- und 45.487 konkrete deutsche-zu-EU-Verweiskanten;
  externe oder nicht im gewaehlten Korpus enthaltene Ziele bleiben als
  auditable `ReferenceTarget`-Knoten offen.

## Legacy-PDF-Extraktion

Die PDF-Extraktion bleibt fuer Quellen ausserhalb des GII-XML-Korpus und als
expliziter Fallback erhalten:

```bash
cd /Users/christinck/Documents/graph_database/pdf-neo4j-pipeline
./.venv/bin/python scripts/extract_normtext.py \
  --output output/raw_versatzv_current.json \
  ../abfall_pdfs/09_VersatzV.pdf
```

Die Haupt-JSON speichert keine Seitentexte direkt. Seiten liegen ausgelagert
unter `output/pages/<document_key>/page_XXX.json`; das Dokument enthaelt nur
`page_refs` und eine textfreie `pages`-Kompatibilitaetsliste.

`scripts/extract_normtext.py` ist nur noch ein Kompatibilitaets-Wrapper. Die
importierbare Logik liegt unter `normtext_extractor/`; Tabellen laufen über
eine Parser-Registry in `normtext_extractor/table_parsers.py`. Neue Tabellen-
Edge-Cases sollen dort als strukturelle Parser ergänzt werden, nicht als
weitere Spezialzweige im CLI-Skript.

Optionale Source-Text-Anomaly-Regeln koennen extern geladen werden:

```bash
./.venv/bin/python scripts/extract_normtext.py \
  --output output/raw_doc.json \
  --rules-dir rules \
  ../abfall_pdfs/09_VersatzV.pdf
```

Regeldateien sind JSON-Dateien mit `source_text_anomaly_patterns`. Ohne
`--rules-dir` werden keine dokumentspezifischen Anomaly-Regeln geladen.

## Tests

```bash
./.venv/bin/python -m pytest
```

Die Tests laufen gegen temporaere Outputs und prüfen unter anderem:

- Tabellenformen in ErsatzbaustoffV Anlagen 1 bis 3.
- Keine doppelten Node-/Relationship-IDs in KrWG, DepV und EfbV.
- Vier-Dokumente-Merge, Referenzextraktion und Cypher-Export.

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

## Extraktions-Validierung

Der Batch-Validator prueft Roh-Extraktions-JSONs und Content-Graph-JSONs auf
Schema-, ID-, Hierarchie-, Seitenbereichs-, Chunk- und Tabellenauffaelligkeiten.
Ohne Argumente prueft er den aktuellen Standardlauf unter `output/refactor_4doc/raw`
und schreibt Markdown- und JSON-Reports in den zugehoerigen `validation`-Ordner:

```bash
./.venv/bin/python scripts/validate_parsed_jsons.py
```

Die alte Einzel-Tabellenpruefung ist weiterhin als Legacy-Modus verfuegbar:

```bash
./.venv/bin/python scripts/validate_parsed_jsons.py \
  --input output/raw_ersatzbaustoffv_current.json \
  --legacy-summary \
  --contains 'Anlage 3 Tabelle' \
  --expect-columns 11 \
  --expect-rows 26 \
  --row-marker-regex '^B[0-9]+$'
```

Weitere schnelle Regressionen:

```bash
./.venv/bin/python scripts/validate_parsed_jsons.py \
  --input output/raw_ersatzbaustoffv_current.json \
  --legacy-summary \
  --contains 'Anlage 2 Tabelle' \
  --expect-columns 11

./.venv/bin/python scripts/validate_parsed_jsons.py \
  --input output/raw_ersatzbaustoffv_current.json \
  --legacy-summary \
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
