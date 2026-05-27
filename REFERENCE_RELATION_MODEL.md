# Reference Relation Model

Dieses Dokument beschreibt die Logik fuer nicht-hierarchische Normverweise.
Sie baut auf dem Content Graph auf und erzeugt zunaechst nur explizite,
textlich belegbare Referenzen.

## Grundsatz

Referenzen werden auf der niedrigsten belegbaren Ebene erkannt:

```text
Chunk -> Referenzziel
```

Hoehere Relationen wie `Paragraph -> Paragraph` oder `Document -> Document`
werden deterministisch aus der Content-Hierarchie abgeleitet. Sie werden nicht
frei separat extrahiert, aber im Graphen ebenfalls als `REFERS_TO` gespeichert.
Ob eine Relation primaer oder abgeleitet ist, ist vorerst nur durch die Ebene
der beteiligten Nodes impliziert.

## Quellen

Eingabe ist der Content Graph JSON, insbesondere:

- `Chunk.text`
- `Chunk.global_key`
- `Chunk.unit_id`
- `Chunk.legal_citation`
- `StructuralUnit.global_key`
- `Document.document_key`
- `Document.document_global_key`
- `Document.title`
- `Document.canonical_citation`

## Referenzarten

### Interne Referenz

Eine interne Referenz nennt keine fremde Norm, sondern verweist innerhalb des
aktuellen Dokuments.

Beispiele:

```text
die Anforderungen nach den §§ 3 und 4
entgegen § 3 oder § 4 Abs. 1 Satz 1
Anlage 2 Tabelle 1a
```

Zielschluessel werden aus dem aktuellen `document_global_key` gebildet. Dieser
Key basiert auf dem normalisierten ausgeschriebenen Normnamen oder Kurztitel,
damit er dieselbe Form hat wie externe Langnamenverweise.

```text
versatzverordnung_para_3
versatzverordnung_para_4_abs_1_satz_1
versatzverordnung_anlage_2_tabelle_1a
```

Wenn ein Ziel im aktuellen Content Graph existiert, wird die Referenz als
`resolved` markiert. Wenn die Ebene feiner ist als vorhandene Content-Nodes,
z.B. `Satz 1`, kann das Ziel als offener feiner Zielschluessel gespeichert
werden und zugleich auf den naechsthoeheren vorhandenen Node zeigen.

### Externe Referenz mit Langname

Viele externe Bundesrechtsverweise nennen den ausgeschriebenen Kurznamen des
Gesetzes oder der Verordnung im Genitiv.

Beispiele aus dem Korpus:

```text
§ 69 Absatz 1 Nummer 8 des Kreislaufwirtschaftsgesetzes
§ 63 des Bundesberggesetzes
§ 3a Absatz 2 Satz 2 und 3 des Verwaltungsverfahrensgesetzes
§ 5 Abs. 1 der Altholzverordnung
Artikel 13 Absatz 1 des Grundgesetzes
```

Fuer solche Ziele wird ein dokumentartiger Zielschluessel aus dem Langnamen
gebildet:

```text
kreislaufwirtschaftsgesetz_para_69_abs_1_nr_8
bundesberggesetz_para_63
verwaltungsverfahrensgesetz_para_3a_abs_2_satz_2
altholzverordnung_para_5_abs_1
grundgesetz_art_13_abs_1
```

Der Langname ist damit selbst Teil der stabilen Zielidentitaet. Wenn das
entsprechende Dokument spaeter mit anderem `document_key` importiert wird,
z.B. `krwg`, muss eine Alias-/Resolver-Schicht
`kreislaufwirtschaftsgesetz -> krwg` setzen.

### Externe Referenz mit Abkuerzung

Der Korpus enthaelt auch direkte Abkuerzungsformen.

Beispiele:

```text
§ 21 ElektroG
§ 28 NachwV
§ 2 Absatz 2 AltfahrzeugV
§ 37 Absatz 1 ElektroG
```

Solche Referenzen werden ebenfalls deterministisch normalisiert:

```text
elektrog_para_21
nachwv_para_28
altfahrzeugv_para_2_abs_2
```

Wenn eine Abkuerzung bereits als `Document.document_key`,
`Document.canonical_citation` oder `Document.abbreviation` im Content Graph
bekannt ist, mappt der Resolver sie auf den `document_global_key` des
importierten Dokuments.

## Zielobjekte

Eine extrahierte Referenz sollte mindestens speichern:

- `reference_id`
- `source_chunk_id`
- `source_unit_id`
- `source_document_id`
- `mention_text`
- `reference_kind`: `internal`, `external_long_name`, `external_abbreviation`
- `target_document_key`
- `target_global_key`
- `target_level`: `document`, `paragraph`, `subsection`, `sentence`,
  `number`, `letter`, `annex`, `table`, `article`
- `resolution_status`: `resolved`, `unresolved`, `partially_resolved`,
  `ambiguous`
- `nearest_resolved_target_id`, falls nur eine groebere Ebene existiert

## Graph-Relationen

Chunk-Ebene:

```text
(Chunk)-[:REFERS_TO]->(Target)
```

Wenn das Ziel noch nicht als Content Node existiert, wird ein Placeholder-Node
verwendet:

```text
(:ReferenceTarget {global_key: target_global_key, status: "unresolved"})
```

Spaeter kann der Placeholder mit einem echten `StructuralUnit`- oder
`Chunk`-Node zusammengefuehrt werden.

Abgeleitete Ebenen:

```text
(StructuralUnit)-[:REFERS_TO]->(StructuralUnit | ReferenceTarget)
(Document)-[:REFERS_TO]->(Document | ReferenceTarget)
```

Bei intern aufloesbaren Zielen werden Chunk-Referenzen bevorzugt auf vorhandene
Chunk-Nodes gelegt. Wenn ein Ziel eine breite StructuralUnit ist, z.B.
`Anlage 8`, zeigt die Chunk-Relation nur auf einen repraesentativen Chunk
dieser Einheit. Sonst wuerde ein einzelner Verweis auf eine ganze Anlage auf
alle Tabellen- oder Abschnitts-Chunks der Anlage auffaechern. Die breite
Relation selbst bleibt ueber den StructuralUnit-Rollup erhalten.

Wenn ein Ziel feiner zitiert ist als die vorhandene Struktur, z.B. `Satz 1`
oder eine nicht modellierte Nummer, kann ein `ReferenceTarget` den exakten
Zielschluessel halten. `nearest_resolved_target_id` zeigt dann auf die
naechsthoeher vorhandene Ebene.

Tabellenverweise wie `Anlage 4 Tabelle 2` werden nur dann voll aufgeloest,
wenn die Normtext-Extraktion fuer diese Anlage echte `table`-StructuralUnits
mit Keys wie `..._anlage_4_tabelle_2` erzeugt. Andernfalls fallen sie auf die
naechsthoeher vorhandene Anlage zurueck.

## Eindeutigkeit

Bundesrechtliche Gesetzes- und Verordnungsnamen sind in der Praxis fuer unsere
Zwecke sehr gut als stabile Langnamen nutzbar. Trotzdem sollte das System nicht
blind annehmen, dass ein Langname schon dem internen `document_key` entspricht.

Deshalb gilt:

1. Der ausgeschriebene Normname erzeugt einen stabilen `title_key`.
2. Der importierte Content Graph kann zusaetzlich einen kurzen `document_key`
   haben.
3. Ein Resolver verbindet `title_key`, Abkuerzung und `document_key` mit dem
   `document_global_key`.

Beispiel:

```text
title_key: kreislaufwirtschaftsgesetz
document_key: krwg
document_global_key: kreislaufwirtschaftsgesetz
canonical_citation: KrWG
```

## Befund aus dem lokalen Korpus

Ein Regex-Scan ueber die lokalen Abfall- und Gefahrgut-PDFs zeigt:

- Sehr viele externe Verweise folgen dem Muster
  `§ ... des/der <Normname>`.
- Es gibt aber auch Abkuerzungsformen wie `§ 21 ElektroG` oder `§ 28 NachwV`.
- Artikelverweise kommen ebenfalls vor, z.B. `Artikel 13 Absatz 1 des
  Grundgesetzes`.
- EU-Verordnungen und Durchfuehrungsverordnungen haben eigene Muster und
  sollten spaeter separat behandelt werden.

Die erste deterministische Referenzextraktion sollte daher interne Verweise,
externe Langnamen und externe Abkuerzungen unterstuetzen, aber EU-Rechtsakte und
mehrdeutige Sammelverweise zunaechst als `unresolved` oder `ambiguous`
markieren.
