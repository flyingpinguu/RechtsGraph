# STATUS.md (Stand: 2026-07-26)

## 0. XML-first Cutover

Fuer Quellen von `gesetze-im-internet.de` ist die semantische Extraktion von
PDF auf das GII-Katalog-XML umgestellt. Der GII-XML-Adapter uebersetzt das
GII-Schema in denselben kanonischen Raw-Vertrag, den Content-Graph,
Referenzextraktion und Neo4j-Export bereits verwenden. PDFs sind nur noch
sekundaere Seiten-/Layout-Provenienz sowie der explizite Fallback fuer acht
Eintraege ohne XML.

Das ist eine technische Source-of-Truth-Entscheidung, keine Aussage zur
rechtlichen Amtlichkeit: GII enthaelt konsolidierte, nicht amtliche Fassungen;
die amtliche Verkuendung erfolgt im Bundesgesetzblatt.

Ergebnis des Vollkorpuslaufs:

- 6.125/6.125 GII-Katalog-XML-Pakete erfolgreich verarbeitet;
- 5.954 XML-Dokumente mit passendem PDF, 171 ohne PDF;
- acht PDF-only-Eintraege separat erfolgreich verarbeitet;
- 139.428 Structural Units und 324.989 Chunks;
- 11.567 CALS-Tabellen mit 236.318 Zeilen;
- keine dokumentweiten ID-Kollisionen, fehlenden Assets oder ungueltigen
  Tabellenformen im unabhaengigen Audit;
- konservative PDF-Seitenzuordnung fuer 95,27 % der Chunks;
- die Pflichtregressionen ErsatzbaustoffV Tabelle 1 (20 x 18), EGBGB
  Art. 232 und AbfKlaerV-Hierarchie bestehen.

Die Vorversion ist als Commit `4341102` und Tag
`pre-gii-xml-migration` erhalten. Architektur, Befehle, Ergebnisse und
Entscheidung gegen Docling als primaere Semantikquelle stehen in
`XML_MIGRATION_PLAN.md`.

## 1. Projektziele & Aktueller Stand
Das Projekt extrahiert komplexe rechtliche Normtexte aus XML sowie PDFs
(Abfall- und Gefahrgutrecht) und überführt diese in einen Graphen (Neo4j). Der Fokus liegt auf deterministischer Extraktion für Struktur und Querverweise.
Die erste Phase der Pipeline ist weitgehend stabil: Dokumente werden in `StructuralUnits` (Paragraphen, Anlagen, Tabellen) und feinere `Chunks` zerlegt, Referenzen (z.B. "§ 8 Absatz 1") werden automatisch aufgelöst und als `REFERS_TO`-Kanten im Graphen abgebildet.

## 2. Pipeline-Architektur (`pdf-neo4j-pipeline/scripts/`)
1. **`extract_normtext.py`**: Kernstück der PDF-Verarbeitung. Liest Struktur, Absätze, Listen und komplexe geometrische Tabellen (z.B. aus ErsatzbaustoffV) aus und speichert sie als rohes JSON. Ausgelagerte Seiten-Rohtexte landen in separaten JSONs.
2. **`extract_content_nodes.py`**: Überführt das Raw-JSON in ein flaches "Content Node"-Modell (`Document`, `StructuralUnit`, `Chunk`) für den Graphen.
3. **`merge_content_graphs.py`**: Führt die Content-Graphen mehrerer Gesetze (z.B. KrWG, DepV, EfbV, ErsatzbaustoffV) zusammen.
4. **`extract_reference_relations.py`**: Analysiert Text-Chunks auf interne und externe Verweise und knüpft die dazugehörigen `REFERS_TO`-Kanten.
5. **`export_neo4j_cypher.py`**: Übersetzt das fertige Graph-Modell samt Kanten in ausführbare Cypher-Queries für den Neo4j-Import.
6. **Hilfsskripte**: 
   - `render_chunks.py`: Generiert visuelle PDF-Previews aus den Chunk-JSONs zur manuellen Qualitätskontrolle.
   - `build_review_shards.py`: Bündelt Chunks für (derzeit pausierte) LLM-Reviews.
   - `validate_parsed_jsons.py`: Batch-Validator für Raw-JSONs und Content-Graph-JSONs; prüft IDs, Struktur, Chunk-Dichte, Tabellenformen und Graph-Konsistenz.

Zusatzmodellierung: Die erlaubte Taxonomie fuer Rechtsgebiete von
`Document`-Nodes ist in `LEGAL_DOMAIN_TAXONOMY.md` beschrieben und als
maschinenlesbare Liste in `legal_domain_taxonomy.json` gespiegelt.

## 3. Kern-Entwicklungen & Historie

### 3.1. LLM-Reviews (Pausiert)
Ursprünglich wurden lokale Modelle (Qwen via Unsloth/Ollama) eingesetzt, um aus Rohextraktionen fehlerfreie Texte zu machen. Dafür wurden "Review Shards" gebaut. Tests (z.B. für GGBefG und VersatzV) zeigten jedoch: Lokale Modelle lieferten bei extrem dichten Tabellendaten inkonsistente Ergebnisse. Der Fokus wurde daher auf robustere, rein deterministische Extraktionslogik in Python verlagert.

### 3.2. Struktur-Extraktion & Inhaltsmodellierung
- **Struktur:** PDFs werden in Paragraphen und Anlagen unterteilt. Absätze mit Nummerierungen (Absatz 1, Nummer 2, Buchstabe a) verbleiben derzeit im Absatz-Chunk, um Fragmentierung zu vermeiden.
- **TOC-Filterung:** Um Inhaltsverzeichnisse (TOCs) nicht als eigene Paragraphen zu parsen, stoppt das Skript an spezifischen Schlüsselwörtern, bis echter Body-Text beginnt. (Muss bei neuen Gesetzen teils noch feinjustiert werden).

### 3.3. Tabellen-Parsing
Die größte Herausforderung stellten verschachtelte "Musteranlagen" (z.B. in der ErsatzbaustoffV) dar. 
- Tabellen werden nun anhand von sichtbaren Gitterlinien geometrisch aufgelöst (PyMuPDF).
- Komplexe Zeilen mit mehrzeiligen Spaltenköpfen (z.B. Grundwasserdeckschichten) werden geflacht.
- Tabellenzeilen sind nicht mehr nur Strings, sondern strukturierte Dictionaries, in denen Werte korrekt ihren Spaltennamen zugeordnet sind.

### 3.4. Referenzen & Cross-Document Verknüpfungen
- **Auflösung:** Verweise (wie "Anlage 2 oder 3", "§§ 15, 16 Absatz 1") werden in ihre einzelnen Zielkomponenten zerlegt.
- **Zielsuche:** Externe Referenzen auf Gesetze ohne Kurznamen (wie "des Kreislaufwirtschaftsgesetzes") werden anhand normalisierter Bezeichner aufgelöst.
- **Multi-Merge-Test:** Bei einem gemeinsamen Lauf von 4 Dokumenten (ErsatzbaustoffV, KrWG, DepV, EfbV) konnten 216 externe Referenzen (z.B. von ErsatzbaustoffV auf KrWG) erfolgreich aufgelöst werden. 138 hochkomplexe Referenzen verblieben als `ReferenceTarget`-Platzhalter.

## 4. Offene Punkte / Nächste Schritte

### 4.1. Batchlauf Abfall-PDFs vom 2026-07-04
- Alte Outputs unter `pdf-neo4j-pipeline/output/abfall_all/` wurden geleert und neu aufgebaut.
- Alle 40 PDFs aus `abfall_pdfs` wurden geparst; Parsing selbst lief ohne Prozessfehler durch.
- Raw-Validierung: 40 Dateien geprüft, 4 Fehler, 22 Warnungen. Die 4 Fehler stammen aus 2 Sonderdokumenten ohne erkannte Normstruktur (`Abkommen_DE_AT_Abfallverbringung_2009_Bundesrat_189-09`, `BGBl_2023_Berichtigung_Aenderung_abfallrechtlicher_Verordnungen`). Die 38 regulären Normtexte haben keine Raw-Fehler.
- Content-Graph-Erzeugung: Für 38 verwertbare Raw-JSONs wurden Content-Graph-JSONs erzeugt. Die beiden Sonderdokumente wurden übersprungen.
- Content-Graph-Validierung: 38 Dateien geprüft, 0 Fehler, 0 Warnungen.
- Tabellenfix: Für `32_AbfKlaerV` werden Anlage 1 Tabelle 1 sowie Anlage 2 Tabellen 1-3 nun geometrisch als `grid` geparst. Die vorherige Fehlklassifikation von Formulartext aus Anlage 3 als Tabelle wurde durch eine generische Formular-Heuristik verhindert.
- Reports:
  - `pdf-neo4j-pipeline/output/abfall_all/validation/parsed_json_validation.md`
  - `pdf-neo4j-pipeline/output/abfall_all/validation/content_graph_validation.md`

### 4.2. Offene Punkte

1. **Inhaltsverzeichnisse (TOC):** TOC-Erkennung wurde für große Gesetzestexte wie StGB/BattDG verbessert; bei neuen Layouts bleibt sie ein Qualitätscheck-Schwerpunkt.
2. **Feinere Strukturierung:** Norm-Ebenen wie "Abschnitt" oder "Unterabschnitt" werden vom Extractor aktuell nicht als separate hierarchische Knoten modelliert.
3. **Rest-Referenzen:** Sehr komplexe Ketten-Referenzen oder unklare externe Gesetzestexte münden in ungelösten Knoten. Die Regex-Logik im Reference-Extractor muss für Spezialfälle noch robuster werden.
4. **Neo4j-Schema-Optimierung:** Überprüfung, ob Node Properties in Neo4j noch weiter für Retrieval-Augmented Generation (RAG) verschlankt werden können (`NODE_PROPERTY_REVIEW.md`).

### 4.3. Teilliste-A-Testlauf vom 2026-07-04

- 50 PDFs aus `gesetze-im-internet.de/Teilliste_A.html` liegen unter `teilliste_a_50_pdfs`.
- Die 10 Raw-Fehlerpaare `NO_CHUNKS`/`NO_STRUCTURAL_UNITS` stammen aus Mantelgesetzen, Änderungsverordnungen oder Anordnungen ohne eigenständige Stammgesetzstruktur. Das ist für die Datenbank vorerst akzeptiert.
- Echte Parserfehler wurden reduziert:
  - Inline-Notenwerte wie `"sehr gut" (1)` werden nicht mehr als neue Absätze erkannt.
  - Anlagen-/Anhang-Referenzen wie `Anhang 36 Teil C Abs. 4` oder `Anlage 2 Teil A und B` werden nicht mehr als neue StructuralUnits erkannt.
  - Inhaltsverzeichnislisten mit `Tabelle 1`, `Tabelle 2`, ... werden nicht mehr als echte Tabellen geparst.
  - AbwV `Anhang 53 (Fotografische Prozesse) ohne Ansäuern...` in einer Tabellenzelle wird nicht mehr als neuer Anhang erkannt.
  - Implizite Tabellen in Anhängen enden jetzt an Normtext-Grenzen wie `(2)` oder `D Anforderungen...`, statt Folgetext mitzuschlucken.
  - Mehrere unbeschriftete Textsegmente in einem Anhang erhalten stabile IDs (`text_1`, `text_2`, ...).
- Aktuelle Raw-Validierung `output/teilliste_a_50/validation/parsed_json_validation.md`: 50 Dateien, 20 erwartbare Errors aus 10 Sonderdokumenten, 65 Warnings.
- Aktuelle Content-Graph-Validierung `output/teilliste_a_50/validation/content_graph_validation.md`: 40 verwertbare Graphs, 0 Errors, 0 Warnings.
- Merge-/Referenz-Smoke-Test:
  - `output/teilliste_a_50/merged/teilliste_a_50_merged_content_graph.json`: 40 Dokumente, 2.735 Nodes, 4.524 Relationships.
  - `output/teilliste_a_50/merged/teilliste_a_50_merged_content_graph_with_refs.json`: 5.939 `REFERS_TO`-Relations und 537 `ReferenceTarget`-Platzhalter.
  - `output/teilliste_a_50/validation/merged_content_graph_validation.md`: 0 Errors, 0 Warnings.
  - Neo4j-Cypher-Export: `output/teilliste_a_50/neo4j/teilliste_a_50_import_with_refs.cypher` (29.197 Zeilen, ca. 9,2 MB).
- Restwarnungen betreffen vor allem echte, aber nur per Fallback geparste tabellenartige Blöcke in der AbwV sowie große Anlagenchunks. Das ist kein Graph-Blocker, bleibt aber ein Qualitätsziel für die nächste Parserrunde.

### 4.4. Teilliste-A-Testlauf 51-100 vom 2026-07-04

- Weitere 50 PDFs aus `gesetze-im-internet.de/Teilliste_A.html` liegen unter `teilliste_a_51_100_pdfs`.
- Outputs liegen unter `pdf-neo4j-pipeline/output/teilliste_a_51_100/`.
- Download: 50/50 erfolgreich. Beim Download musste die Teilliste explizit als ISO-8859-1 gelesen werden, damit Umlaute in Titeln und PDF-URLs korrekt bleiben.
- Raw-Extraktion: 50 Raw-JSONs erzeugt, kein Prozessfehler.
- Content-Graph-Erzeugung: 45 verwertbare Graphs, 5 Raw-only-Dokumente ohne erkannte Stamm-Normstruktur.
- Raw-Validierung: 50 Dateien, 10 Errors, 31 Warnings.
  - Die 10 Errors sind 5 Dokumente mit je `NO_CHUNKS` und `NO_STRUCTURAL_UNITS`: `074_afg116g`, `080_afrentwbkuebkg`, `082_afrg`, `083_afsg`, `094_agmahnvordrvaendv`.
  - Diese Dokumente sind nach Sichtprüfung überwiegend Änderungs-, Zustimmungsgesetz- oder Übereinkommens-Texte ohne normale Paragraphenstruktur und daher vorerst erwartbare Sonderfälle.
  - 19 Warnings sind `low_paragraph_count`, meist bei sehr kurzen Jahres-/Zuständigkeitsverordnungen mit nur zwei Paragraphen.
  - 2 echte Qualitätswarnungen betreffen `073_afgbv`: `AFGBV Anlage 1` und `AFGBV Anlage 1 Tabelle 1` sind sehr große Chunks.
- Content-Graph-Validierung: 45 Graphs, 0 Errors, 0 Warnings; die 45 Info-Einträge `GRAPH_NO_REFERS_TO` sind vor der separaten Referenzextraktion erwartbar.
- Merge-/Referenz-Smoke-Test:
  - `output/teilliste_a_51_100/merged/teilliste_a_51_100_merged_content_graph.json`: 45 Dokumente, 2.336 Nodes, 3.898 Relationships.
  - `output/teilliste_a_51_100/merged/teilliste_a_51_100_merged_content_graph_with_refs.json`: 5.143 `REFERS_TO`-Relations und 649 `ReferenceTarget`-Platzhalter.
  - `output/teilliste_a_51_100/validation/merged_content_graph_validation.md`: 0 Errors, 0 Warnings.
  - Neo4j-Cypher-Export: `output/teilliste_a_51_100/neo4j/teilliste_a_51_100_import_with_refs.cypher` (19.986 Zeilen, ca. 7,2 MB).

### 4.5. Vollkorpus-Download Gesetze im Internet vom 2026-07-05

- Die beiden alten 50er-Testbatches (`teilliste_a_50_pdfs`, `teilliste_a_51_100_pdfs`) und deren Output-Ordner wurden entfernt.
- Ein neuer Download-Korpus liegt unter `gesetze_im_internet_pdfs/`, mit Unterordnern nach den Teillisten-Kategorien `A` bis `Z`.
- Download-Skript: `pdf-neo4j-pipeline/tmp/download_gii_pdfs.py`.
- Manifest und Report:
  - `gesetze_im_internet_pdfs/manifest.json`
  - `gesetze_im_internet_pdfs/download_report.md`
- Ergebnis: 5.970 Einträge gefunden und 5.970 PDFs im Manifest erfasst; keine Download-Fehler. Davon waren 5.418 neu heruntergeladen und 552 aus dem unterbrochenen Lauf bereits vorhanden.
- Nach dem Download wurden 52 Altdateien aus einem fehlerhaften ersten A-Lauf entfernt, sodass Dateizahl und Manifest wieder exakt übereinstimmen.
- Wichtig: Es wurde noch kein Parsing des Vollkorpus gestartet.

### 4.6. Domain-Klassifikation Vollkorpus vom 2026-07-06

- Ziel: `Document`-Nodes später mit `primary_legal_domain`, `primary_legal_subdomain`, weiteren Domain-Listen, Topics und Confidence versehen.
- Klassifikationsskript: `pdf-neo4j-pipeline/scripts/classify_legal_domains_openrouter.py`.
- Kontext pro Dokument wurde aus Kostengründen auf eine PDF-Startseite plus wenige heading-artige Zeilen reduziert. Der Prompt nutzt die feste Taxonomie aus `legal_domain_taxonomy.json`.
- Modelle:
  - Ersttests mit `minimax/minimax-m3`: günstig, aber bei größeren Batches zu viele JSON-/Schemafehler.
  - Hauptlauf mit `z-ai/glm-5.2`: stabiler; Restfehler wurden mit Batchgröße 1 erneut klassifiziert.
- Ergebnis:
  - 5.970 Dokumente klassifiziert.
  - 5.970 aktuelle Klassifikationen gültig.
  - 0 aktuelle Errors.
  - 419 Dokumente mit `needs_review=true` für spätere Qualitätsprüfung.
- Outputs:
  - `pdf-neo4j-pipeline/output/domain_classification/document_domain_classifications_final.json`
  - `pdf-neo4j-pipeline/output/domain_classification/document_domain_classifications.jsonl`
  - `pdf-neo4j-pipeline/output/domain_classification/document_domain_classification_report.md`
  - `pdf-neo4j-pipeline/output/domain_classification/document_domain_classification_batch_usage.jsonl`
- Token-Logging: Für die ab Aktivierung des Batch-Usage-Logs erfassten 4.949 Dokumente wurden 4.712.307 Prompt-Tokens, 905.912 Completion-Tokens und 5.618.219 Total-Tokens protokolliert. Die frühen Experimentläufe vor Usage-Logging sind im Token-Report nicht vollständig enthalten.

### 4.7. Vollkorpus-Raw-Parsing vom 2026-07-06

- Batch-Runner: `pdf-neo4j-pipeline/scripts/batch_extract_gii_raw.py`.
- Input: alle 5.970 PDFs aus `gesetze_im_internet_pdfs/manifest.json`.
- Output:
  - Raw JSONs: `pdf-neo4j-pipeline/output/gii_full/raw/`
  - Seiten-JSONs: `pdf-neo4j-pipeline/output/gii_full/pages/`
  - Prozessbericht: `pdf-neo4j-pipeline/output/gii_full/validation/raw_extraction_process_report.md`
  - Validierungsbericht: `pdf-neo4j-pipeline/output/gii_full/validation/parsed_json_validation_skip_pages.md`
- Ergebnis letzter Lauf:
  - 5.970/5.970 Prozess-OK, keine Extraktionsabbrüche.
  - 89.707 Structural Units.
  - 235.769 Chunks.
  - 2.375 Extraction-Issue-Warnungen.
  - Output-Größe: ca. 1,7 GB.
- Während des Laufs wurden mehrere generische Parserfixes ergänzt:
  - Fortsetzungs-/Referenzzeilen wie `§ 5 ...` mitten im Satz starten keinen neuen Paragraphen mehr, wenn die Vorzeile typischerweise eine laufende Referenz fortsetzt.
  - Gliederungsüberschriften vor echten Paragraphen werden nicht mehr versehentlich als laufender Satz behandelt.
  - Wiederholte Anlagenüberschriften innerhalb laufender Anlagenblöcke bzw. rückwärts nummerierte Formular-Innenüberschriften werden nicht mehr als neue Top-Level-Anlagen behandelt.
  - Wiederholte Tabellenlabels innerhalb derselben Anlage erhalten eindeutige `part_N`-Keys, solange die Tabellen noch nicht strukturell zusammengeführt werden.
- Validierung letzter Lauf:
  - 5.970 Raw-JSONs geprüft.
  - 3.976 Errors, davon 3.762 erwartbare `NO_STRUCTURAL_UNITS`/`NO_CHUNKS` aus 1.881 Sonderdokumenten ohne Stammgesetzstruktur.
  - Echte verbleibende Blocker:
    - 103 `CHUNK_DUPLICATE_ID`
    - 59 `UNIT_DUPLICATE_ID`
    - 52 `TABLE_NO_ROWS`
  - Weitere Warnungen betreffen vor allem große Chunks, Fallback-Tabellenparser, leere/uneinheitliche Tabellenspalten und doppelte Tabellenzeilen.
- Einschätzung: Der Vollkorpus ist raw geparst und gut genug für systematische Fehlertriage. Ein vollständiger Content-Graph-/Neo4j-Build über alle Dokumente sollte erst nach Behandlung oder gezieltem Ausschluss der verbleibenden ID- und Tabellen-Blocker erfolgen.

### 4.8. ID-Fixes und Vollkorpus-Content-Graph vom 2026-07-06

- Ursache der verbliebenen `UNIT_DUPLICATE_ID`/`CHUNK_DUPLICATE_ID`-Fehler:
  - Innerhalb einzelner PDFs: zusammengesetzte Dokumente, Formular-/Fundstellenbereiche oder mehrfach auftretende Paragraph-/Waste-Code-Nummern erzeugten denselben fachlichen Key.
  - Über den Vollkorpus hinweg: mehrere PDFs können denselben Gesetzestitel bzw. dieselben `document_global_key`-basierten Unit-Keys enthalten, z.B. Stammtext plus Bekanntmachung/Teilfassung.
- Fixes:
  - Raw-Extractor disambiguiert doppelte IDs innerhalb eines Dokuments deterministisch mit `_part_N` und zieht interne `unit_id`-/`chunk_id`-Referenzen mit.
  - Content-Graph-Export namespaced technische Node-IDs mit `document_id`, damit fachliche `global_key`s erhalten bleiben, aber Neo4j-/Merge-IDs corpus-weit eindeutig sind.
- Ergebnis Raw-Validierung nach Reparse:
  - 5.970/5.970 Prozess-OK.
  - 89.707 Structural Units.
  - 235.769 Chunks.
  - `UNIT_DUPLICATE_ID`: 0.
  - `CHUNK_DUPLICATE_ID`: 0.
  - Verbleibende Errors sind 1.881 `NO_STRUCTURAL_UNITS`, 1.881 `NO_CHUNKS` für Sonderdokumente ohne Stammgesetzstruktur sowie 52 `TABLE_NO_ROWS`.
- Vollkorpus-Content-Graph:
  - Einzelgraphen: `pdf-neo4j-pipeline/output/gii_full/content_graphs/`
  - Gemergter Graph: `pdf-neo4j-pipeline/output/gii_full/merged/gii_full_merged_content_graph.json`
  - Validierung: `pdf-neo4j-pipeline/output/gii_full/validation/merged_content_graph_validation.md`
  - Counts: 5.970 Dokumente, 331.446 Nodes, 556.481 Relationships.
  - Validierung gemergter Graph: 0 Errors, 0 Warnings, 1 Info (`GRAPH_NO_REFERS_TO`, vor Referenzextraktion erwartbar).
- Nächster sinnvoller Schritt: Referenzextraktion auf dem gemergten Content-Graph testen und danach erst Cypher/Neo4j-Export für den Vollkorpus erzeugen.

### 4.9. Vollkorpus-Referenzextraktion vom 2026-07-06

- Input: `pdf-neo4j-pipeline/output/gii_full/merged/gii_full_merged_content_graph.json`
- Output: `pdf-neo4j-pipeline/output/gii_full/merged/gii_full_merged_with_references.json`
- Während des ersten Laufs wurde ein Ebenenfehler gefunden: dokumentweite Ziele wurden versehentlich auch als `Chunk -> Document` bzw. `StructuralUnit -> Document` verbunden. Die Referenzlogik wurde korrigiert, sodass Dokumentziele nur auf Dokumentebene verbunden werden; Chunk-/Unit-Ebene bleiben bei Chunk/Unit oder expliziten `ReferenceTarget`-Nodes.
- Ergebnis:
  - 375.295 Nodes.
  - 1.333.197 Relationships.
  - 43.849 `ReferenceTarget`-Nodes.
  - 776.716 `REFERS_TO`-Relationships.
- Validierung:
  - Report: `pdf-neo4j-pipeline/output/gii_full/validation/merged_with_references_validation.md`
  - Ergebnis: 0 Errors, 0 Warnings, 0 Info.
- Nächster Schritt: Cypher-Export aus `gii_full_merged_with_references.json` und Import in Neo4j.

### 4.10. Neo4j-Vollkorpus-Import vom 2026-07-06

- Direkter Cypher-Import der 1,1-GB-Datei war für Neo4j Desktop zu schwer:
  - Erstes Problem: 1 GB Neo4j-Heap führte zu `Java heap space`/OOM.
  - Zweites Problem: Relationship-Matches ohne gemeinsames Label waren zu langsam.
  - Drittes Problem: große Cypher-Literal-Dateien sind für `cypher-shell` bei diesem Volumen unpraktisch.
- Neo4j Desktop Config wurde für den Vollkorpus erhöht:
  - `server.memory.heap.initial_size=2G`
  - `server.memory.heap.max_size=6G`
  - `server.memory.pagecache.size=4G`
- Export/Import-Pfad wurde erweitert:
  - `scripts/export_neo4j_cypher.py` nutzt jetzt ein gemeinsames `GraphNode`-Label und kann für geleerte DBs `CREATE` statt `MERGE` verwenden.
  - Neuer schneller Importpfad: `scripts/export_neo4j_csv.py`.
  - CSV-Output liegt im Neo4j-Importordner: `.../dbms-be622bb2-923d-4040-beb7-dfd92e3c68d3/import/gii_full_csv/`.
  - Importskript: `pdf-neo4j-pipeline/output/gii_full/neo4j/gii_full_load_csv.cypher`.
  - `rows_json` und andere `*_json`-Properties werden im CSV-Neo4j-Import ausgelassen, weil Neo4j `LOAD CSV` bei sehr großen JSON-Strings mit eingebetteten Quotes fehleranfällig ist. Die vollständigen Werte bleiben in den Content-Graph-JSONs erhalten.
- Ergebnis in Neo4j nach Import und Neustart:
  - 375.295 Nodes.
  - 1.333.197 Relationships.
  - Relationship-Typen:
    - `REFERS_TO`: 776.716
    - `CONTAINS_CHUNK`: 235.769
    - `NEXT_CHUNK`: 146.188
    - `CONTAINS_UNIT`: 89.707
    - `NEXT_UNIT`: 84.817
- Neo4j läuft wieder als Hintergrunddienst auf `neo4j://127.0.0.1:7687`.

### 4.11. Verbesserte Referenzauflösung und sicherer v2-Import vom 2026-07-07

- Ziel: Viele bisherige `ReferenceTarget`-Nodes waren eigentlich Dokumente/Normstellen, die im Korpus enthalten sind, aber wegen unterschiedlicher Namensräume nicht erkannt wurden.
- Ursache:
  - Technische Dokument-Keys im Graph heißen z.B. `0345_bgb_pdf`.
  - Externe Langzitate zielen aber auf ausgeschriebene Namen wie `buergerliches_gesetzbuch`.
  - Diese ausgeschriebenen Namen standen zwar in `full_citation`, wurden aber bisher nicht als Alias für die Zielauflösung genutzt.
- Änderungen:
  - `scripts/extract_reference_relations.py` erzeugt jetzt eindeutige Dokument-Aliasse aus:
    - `full_citation` bzw. daraus abgeleitetem Titel,
    - `full_title`,
    - PDF-Dateinamen ohne Nummernpräfix,
    - vorhandenen Abkürzungs-/Titel-/Key-Feldern.
  - Ambige Aliasse werden nicht zur Auflösung verwendet.
  - Beim Auflösen externer Verweise wird ein Ziel wie `bundes_apothekerordnung_para_4` jetzt auf den tatsächlichen technischen Korpus-Key, z.B. `0041_bapo_pdf_para_4`, gemappt.
  - `scripts/extract_content_nodes.py` ergänzt `Document.full_title` aus `full_citation`.
  - Neo4j-Export-Allowlist enthält nun `full_title`, `full_citation`, `citation_prefix` und `date_enacted`.
- Neuer Graph:
  - Content-Graph: `pdf-neo4j-pipeline/output/gii_full_v2/merged/gii_full_v2_merged_content_graph.json`
  - Referenz-Graph: `pdf-neo4j-pipeline/output/gii_full_v2/merged/gii_full_v2_merged_with_references.json`
  - Validierung: `pdf-neo4j-pipeline/output/gii_full_v2/validation/merged_with_references_validation.md`
  - Validierungsergebnis: 0 Errors, 0 Warnings, 0 Info.
- Ergebnis im Vergleich zum alten Import:
  - Alte DB `neo4j`: 375.295 Nodes, 1.333.197 Relationships, 43.849 `ReferenceTarget`-Nodes.
  - Neue DB `gii-v2`: 364.339 Nodes, 1.333.250 Relationships, 32.893 `ReferenceTarget`-Nodes.
  - Reduktion: 10.956 weniger `ReferenceTarget`-Nodes.
  - Alle 5.970 `Document`-Nodes in `gii-v2` haben `full_title`.
- Import:
  - Die bisherige Datenbank `neo4j` wurde nicht ersetzt und bleibt als Fallback intakt.
  - Verbesserte Datenbank wurde zusätzlich als `gii-v2` importiert.
  - `gii-v2` ist online auf derselben Instanz.
