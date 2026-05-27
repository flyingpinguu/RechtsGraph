# Normtext-Extraktion — Brief für Qwen-Agenten

## Aufgabe

Extrahiere aus dem gegebenen Gesetzes-PDF die reine Normstruktur:
Titel, Gliederung, Paragraphen, Absätze, Anlagen, Anhänge,
AVV-Codes und andere regulatorische Codes.

**Wichtig:** Extrahiere keine Semantik, keine Concept-Extraktion,
keine Relationen. Nur der dokumentierte Normtext mit Seitenbezug
und Hierarchie.

## Juristische Gliederungshierarchie (Deutschland)

Folgende Hierarchieebenen sind zu erkennen und zu strukturieren:

1. **Gesetz** (vollständiger Titel, z.B. "Kriftverordnung")
2. **Teil** (z.B. "Allgemeine Vorschriften")
3. **Kapitel** (z.B. "Allgemeine Bestimmungen")
4. **Abschnitt** (z.B. "Definitionen")
5. **Paragraph / §** (z.B. "§ 1 Zweck")
6. **Absatz** (z.B. "(1)", "(2)")
7. **Satz** (einzelne Aussage innerhalb eines Absatzes)
8. **Nummer / Buchstabe** (z.B. "1.", "a)", "aa.")

**Ergänzend:**
- **Anlagen / Anhänge** – eigene Dokumente innerhalb des Gesetzes
- **Tabellen** – strukturierte Daten in Tabellenform
- **AVV-Codes** – Abfallverzeichnis-Verordnung-Codes (6-stellig, z.B. "17 05 04")

## Seitenbezug

- Jede extrahierte Einheit muss die **Seitennummer** enthalten,
  auf der sie beginnt.
- Wenn eine Einheit sich über mehrere Seiten erstreckt, notiere
  Start- und Endseite.
- Verwende die **gedruckte Seitennummer** (nicht die PDF-Seitenzahl
  bei unterschiedlichen Zählungen im Vorspann).

## Breadcrumbs

Jede Einheit erhält einen Breadcrumb-Pfad, der die Hierarchie
wiedergibt:

```
Gesetzstitel > Teil > Kapitel > Abschnitt > §X > AbsatzY
```

Beispiel:
```
Kriftverordnung > Teil 1 > Kapitel 1 > § 1
```

## Konservative Strategie

1. **Nicht raten:** Wenn die Hierarchie unklar ist, markiere als
   `uncertain` und notiere den Grund.
2. **Nicht erfinden:** Extrahiere nur, was im Text explizit steht.
   Keine impliziten Schlüsse.
3. **Evidence bewahren:** Jede extrahierte Einheit muss den
   Originaltext (Evidence-Text) enthalten.
4. **Seitenumbrüche beachten:** Paragraphen oder Absätze können
   sich über Seitenumbrüche erstrecken – zusammengehörige Teile
   sind zu verknüpfen.

## Umgang mit Anlagen und Tabellen

- **Anlagen** sind eigenständige Struktureinheiten mit eigener
  Hierarchie (Anlage > Nummer > Unterabschnitt).
- **Tabellen** werden als strukturierte Einheiten extrahiert,
  nicht als freier Text. Spaltenüberschriften sind zu erfassen.
- **AVV-Codes** – extrahiere vollständig mit Kontext (z.B.
  "Abfälle aus Oberflächenbehandlung: 17 05 04").

## Ausgabeformat

Produziere JSON gemäss `output_contract.md`.
Verwende die Typen `Document`, `StructuralUnit` und `Chunk`.

## Qualitätscheck vor Ausgabe

- [ ] Alle Paragraphen erfasst?
- [ ] Hierarchie konsistent (keine fehlenden Ebenen)?
- [ ] Seitennummern korrekt?
- [ ] Breadcrumbs vollständig?
- [ ] Evidence-Text bei jeder Einheit vorhanden?
- [ ] Keine erfundenen oder interpretierten Inhalte?
