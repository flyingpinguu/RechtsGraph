# Qwen-Contexte — Übersicht

Dieser Ordner enthält die Anweisungen (Briefe) für Qwen-Subagenten.
Jeder Brief ist so geschrieben, dass verschiedene Qwen-Instanzen
unabhängig voneinander einheitliche Ergebnisse produzieren.

## Kontexte

### `normtext_extraction_brief.md`
**Zweck:** Qwen-Agenten anweisen, Normtext aus Gesetzes-PDFs zu extrahieren.
**Inhalt:** Juristische Hierarchie (Teil → Kapitel → Abschnitt → § → Absatz → Satz),
Seitenbezug, Breadcrumbs, konservativer Umgang mit Unsicherheiten,
Umgang mit Anlagen/Tabellen/AVV-Codes.
**Ausgabe:** strukturierte JSON-Daten gemäss `output_contract.md`.

### `review_brief.md`
**Zweck:** Qwen-Agenten anweisen, Extraktionsergebnisse zu prüfen.
**Inhalt:** Vollständigkeit der Hierarchie, korrekte Paragraphen-/Absatzgrenzen,
Seitenbezug, Dokumentation von Issues, keine erfundenen Einheiten.
**Ausgabe:** Review-Entscheidungen (accepted/rejected/needs_review)
gemäss `output_contract.md`.

### `output_contract.md`
**Zweck:** Definiert das JSON-Format der Normtext-Schicht.
**Inhalt:** Typen Document, StructuralUnit, Chunk, ExtractionIssue,
ReviewDecision mit ID-Schema, Feldern und Validierungsregeln.

## Verwendung

1. Qwen-Instanz mit `normtext_extraction_brief.md` starten → PDF extrahieren
2. Qwen-Instanz mit `review_brief.md` starten → Ergebnisse prüfen
3. Ergebnisse gemäss `output_contract.md` zurückgeben
4. Haupt-Codex (Orchestrator) sammelt, validiert und steuert weitere Schritte

## Prinzipien

- **Konsistenz:** Dieselben Briefe → gleiche Ergebnisse, egal welche Qwen-Instanz
- **Konservativ:** Lieber weniger als erfundene Extraktion
- **Dokumentiert:** Jedes Ergebnis hat Evidence-Text und Seitenangabe
- **Review-bar:** Jedes Ergebnis ist prüfbar und korrigierbar
