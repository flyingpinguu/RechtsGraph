# Neo4j Query Sammlung

Nuetzliche Cypher-Queries fuer die Visualisierung und Kontrolle der
Normtext- und Referenzgraphen.

## Alles Anzeigen

```cypher
MATCH p=(a)-[r]->(b)
RETURN p
LIMIT 500;
```

## Nur Dokument Und Structural Units

Zeigt Dokument, Paragraphen, Anlagen und Tabellen ohne Chunks.

```cypher
MATCH p=(d:Document)-[:CONTAINS_UNIT*1..3]->(u:StructuralUnit)
RETURN p;
```

## Nur Dokument Und Structural Units Mit Hierarchie Und Verweisen

Zeigt nur `Document`- und `StructuralUnit`-Nodes mit `CONTAINS_UNIT` und
`REFERS_TO`. Chunks, `CONTAINS_CHUNK` und `NEXT_*` bleiben ausgeblendet.

```cypher
MATCH p=(a)-[r:CONTAINS_UNIT|REFERS_TO]->(b)
WHERE (a:Document OR a:StructuralUnit)
  AND (b:Document OR b:StructuralUnit)
RETURN p;
```

Variante mit externen offenen Zielen:

```cypher
MATCH p=(a)-[r:CONTAINS_UNIT|REFERS_TO]->(b)
WHERE (a:Document OR a:StructuralUnit)
  AND (b:Document OR b:StructuralUnit OR b:ReferenceTarget)
RETURN p;
```

## Structural Units Mit Reihenfolge

```cypher
MATCH p=(a:StructuralUnit)-[:NEXT_UNIT]->(b:StructuralUnit)
RETURN p;
```

## Ein Dokument Mit Direkten Units

```cypher
MATCH p=(d:Document {document_key: 'versatzv'})-[:CONTAINS_UNIT]->(u:StructuralUnit)
RETURN p;
```

## Eine Structural Unit Mit Chunks

Beispiel fuer `VersatzV § 4`.

```cypher
MATCH p=(u:StructuralUnit {global_key: 'versatzverordnung_para_4'})-[:CONTAINS_CHUNK]->(c:Chunk)
RETURN p;
```

## Chunk-Reihenfolge Einer Unit

```cypher
MATCH (u:StructuralUnit {global_key: 'versatzverordnung_para_4'})-[:CONTAINS_CHUNK]->(c:Chunk)
OPTIONAL MATCH p=(c)-[:NEXT_CHUNK]->(:Chunk)
RETURN p;
```

## Alle Referenzen

```cypher
MATCH p=(a)-[:REFERS_TO]->(b)
RETURN p
LIMIT 500;
```

## Referenzen Nach Ebene Zaehlen

```cypher
MATCH (a)-[:REFERS_TO]->(b)
RETURN labels(a) AS source_labels, labels(b) AS target_labels, count(*) AS count
ORDER BY count DESC;
```

## Nur Chunk-Level Referenzen

```cypher
MATCH p=(a:Chunk)-[:REFERS_TO]->(b)
RETURN p
LIMIT 300;
```

## Nur StructuralUnit-Level Referenzen

```cypher
MATCH p=(a:StructuralUnit)-[:REFERS_TO]->(b)
RETURN p
LIMIT 300;
```

## Nur Document-Level Referenzen

```cypher
MATCH p=(a:Document)-[:REFERS_TO]->(b)
RETURN p;
```

## Externe Offene Referenzen

```cypher
MATCH p=(a)-[:REFERS_TO]->(t:ReferenceTarget)
RETURN p
LIMIT 300;
```

## Referenzen Einer Bestimmten Unit

Beispiel: alle direkten Referenzen von `VersatzV § 4`.

```cypher
MATCH p=(u:StructuralUnit {global_key: 'versatzverordnung_para_4'})-[:REFERS_TO]->(target)
RETURN p;
```

## Referenzen Aus Chunks Einer Unit

Beispiel: Referenzen aus allen Chunks von `VersatzV § 4`.

```cypher
MATCH (u:StructuralUnit {global_key: 'versatzverordnung_para_4'})-[:CONTAINS_CHUNK]->(c:Chunk)
MATCH p=(c)-[:REFERS_TO]->(target)
RETURN p;
```

## Incoming Referenzen Auf Eine Unit

Beispiel: wer verweist auf `VersatzV § 4`.

```cypher
MATCH p=(source)-[:REFERS_TO]->(u:StructuralUnit {global_key: 'versatzverordnung_para_4'})
RETURN p;
```

## Falsche Chunk-Zu-StructuralUnit Referenzen Finden

Sollte leer sein. Wenn nicht, ist die Referenzableitung fehlerhaft oder ein
alter Import liegt noch in der Datenbank.

```cypher
MATCH p=(c:Chunk)-[:REFERS_TO]->(u:StructuralUnit)
RETURN p;
```

## Labels Zaehlen

```cypher
MATCH (n)
UNWIND labels(n) AS label
RETURN label, count(*) AS count
ORDER BY count DESC;
```

## Relationship-Typen Zaehlen

```cypher
MATCH ()-[r]->()
RETURN type(r) AS relationship_type, count(*) AS count
ORDER BY count DESC;
```

## Tabelleneinheiten Und Tabellenchunks

```cypher
MATCH p=(t:StructuralUnit_table)-[:CONTAINS_CHUNK]->(c:Chunk_table_rows)
RETURN p;
```

## Text Eines Chunks Anzeigen

```cypher
MATCH (c:Chunk {global_key: 'versatzverordnung_para_4_abs_1'})
RETURN c.global_key, c.display_name, c.chunk_type, c.text;
```

## Datenbank Fuer Neuimport Leeren

Nur in einer Testdatenbank verwenden.

```cypher
MATCH (n)
DETACH DELETE n;
```
