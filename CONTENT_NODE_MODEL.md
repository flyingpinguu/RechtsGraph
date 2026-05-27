# Content Node Model

Dieses Dokument beschreibt die erste Graph-Schicht: deterministisch extrahierte
Content-Nodes aus Gesetzes-PDFs. Ziel ist eine stabile, zitierbare
Normtext-Struktur fuer spaetere GraphRAG-, Referenz- und Semantik-Schichten.

## Grundsatz

Die Content-Schicht enthaelt nur Text- und Strukturknoten, die aus dem Dokument
selbst ableitbar sind. Sie soll nicht frei semantisch interpretieren.

Nicht enthalten sind vorerst:

- Konzepte wie `Abfall`, `Verwertung`, `Grenzwert`
- juristische Akteure wie `zuständige Behörde`
- Pflichten, Verbote, Erlaubnisse oder Tatbestandsmerkmale
- Normverweis-Relationen zwischen Textstellen

Normverweise werden auf der niedrigsten belegbaren Ebene extrahiert,
typischerweise `Chunk -> Zielreferenz`. Hoehere Relationen wie
`Paragraph -> Paragraph` oder `Document -> Document` werden daraus
deterministisch ueber die Content-Hierarchie abgeleitet und ebenfalls als
`REFERS_TO` gespeichert. Die Ebene ergibt sich aus den Node-Typen; eine
gesonderte Markierung fuer abgeleitete Relationen wird vorerst nicht gesetzt.

## Content-Nodes

### Document

Ein Gesetzes- oder Verordnungsdokument.

Typische Eigenschaften:

- `document_id`
- `document_key`
- `title`
- `canonical_citation`
- `full_citation`
- `source_pdf`
- `sha256`

### StructuralUnit

Eine zitierbare oder strukturell relevante Normeinheit.

Aktuelle Typen:

- `paragraph`, z.B. `VersatzV § 4`
- `annex`, z.B. `VersatzV Anlage 2`
- `table`, z.B. `VersatzV Anlage 2 Tabelle 1`

Spaeter moeglich:

- `part`
- `chapter`
- `section`
- `article`
- `waste_code`

Typische Eigenschaften:

- `unit_id`
- `global_key`
- `document_id`
- `unit_type`
- `legal_citation`
- `display_name`
- `label`
- `number`
- `title`
- `parent_unit_id`
- `child_unit_ids`
- `page_range`

### Chunk

Eine feingranulare Retrieval- und Beleg-Einheit innerhalb einer
`StructuralUnit`. Chunks sind die primaere Ebene fuer spaetere Referenz- und
Entity-Extraktion.

Aktuelle Typen:

- `paragraph_text`
- `subsection`
- `annex_text`
- `annex_section`
- `table_rows`
- `table_note`
- `appendix_block`

Typische Eigenschaften:

- `chunk_id`
- `global_key`
- `unit_id`
- `chunk_type`
- `legal_citation`
- `display_name`
- `parent_chunk_id`
- `child_chunk_ids`
- `sequence`
- `page_range`
- `text`
- `text_sha256`

Bei Tabellenchunks zusaetzlich:

- `table_section`
- `columns`
- `column_header_text`
- `row_range`
- `rows`

## Seiten

PDF-Seiten werden vorerst nicht als Neo4j-Nodes modelliert. Sie bleiben als
separate JSON-Belegdateien erhalten und werden ueber `page_range`, `page_id`,
`text_sha256` und Dateipfade referenziert.

Das reicht fuer Review, Quellenanzeige und Auditierbarkeit, ohne den Graphen mit
technischen Seitenknoten aufzublaehen. Falls spaeter Page-Level-Retrieval,
Layoutanalyse oder visuelle Quellenansicht wichtig wird, kann `Page` als eigener
Node-Typ nachgezogen werden.

## Hierarchie

Primaere Graph-Struktur:

```text
Document
  -> StructuralUnit
       -> StructuralUnit
       -> Chunk
            -> Chunk
```

Beispiel:

```text
VersatzV
  -> VersatzV § 4
       -> VersatzV § 4 Abs. 1
       -> VersatzV § 4 Abs. 2

VersatzV
  -> VersatzV Anlage 2
       -> VersatzV Anlage 2 Tabelle 2
            -> Tabelle-2-Zeilenchunk: Anorganische Stoffe
            -> Tabelle-2-Zeilenchunk: Organische Stoffe
            -> Tabelle-2-Hinweise
```

## Graph-Relationen

Empfohlene primaere Relationen:

- `(Document)-[:CONTAINS_UNIT]->(StructuralUnit)`
- `(StructuralUnit)-[:CONTAINS_UNIT]->(StructuralUnit)`
- `(StructuralUnit)-[:CONTAINS_CHUNK]->(Chunk)`
- `(Chunk)-[:CONTAINS_CHUNK]->(Chunk)`
- `(StructuralUnit)-[:NEXT_UNIT]->(StructuralUnit)`
- `(Chunk)-[:NEXT_CHUNK]->(Chunk)`
- `(Chunk)-[:REFERS_TO]->(Chunk | ReferenceTarget)`
- `(StructuralUnit)-[:REFERS_TO]->(StructuralUnit | ReferenceTarget)`
- `(Document)-[:REFERS_TO]->(Document | ReferenceTarget)`

Rueckrichtungen wie `PART_OF` koennen bei Bedarf gespeichert werden, sind aber
aus den `CONTAINS_*`-Relationen ableitbar.

Im Neo4j-Export ist `ContentNode` keine gemeinsame Label-Klasse. Inhaltliche
Nodes tragen stattdessen Properties wie `is_content_node: true` und
`node_group: "content_node"`, damit die Visualisierung nach konkreten Labels
wie `Document`, `StructuralUnit`, `Chunk` und Subtypen gefaerbt werden kann.

## Regeln

1. Jedes PDF erzeugt genau einen `Document`-Node.
2. Paragraphen, Anlagen und erkannte Tabellen erzeugen `StructuralUnit`-Nodes.
3. Absatz-, Anlagenabschnitts- und Tabellenzeilen-Gruppen erzeugen `Chunk`-Nodes.
4. Tabellenzeilen werden in `table_rows`-Chunks gruppiert, nicht einzeln als
   Nodes erzeugt. Ein `table_rows`-Chunk enthaelt standardmaessig hoechstens
   40 Tabellenzeilen.
5. Tabellenkopf und Spaltenbeschriftungen muessen in jedem `table_rows`-Chunk
   enthalten sein.
6. IDs muessen stabil, zitiernah und rebuildbar sein.
7. Die feinste Content-Ebene fuer Verweisextraktion ist der `Chunk`.
8. Hoehere Verweisrelationen werden aus der Content-Hierarchie abgeleitet, nicht
   separat frei extrahiert, aber im Graphen explizit gespeichert.
