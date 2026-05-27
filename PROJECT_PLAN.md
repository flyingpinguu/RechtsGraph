# PROJECT_PLAN.md

# Graphdatenbank-Projekt: Gesetzes-PDFs

## Ziel

Eine konsistente, skalierbare Pipeline zum Extrahieren, Pruefen und Importieren
von hunderten Gesetzes-PDFs in eine Neo4j-Graphdatenbank.

## Architektur-Prinzipien

1. **Orchestrator = Haupt-Codex** – koordiniert Subagenten, verwaltet Status,
   steuert Pipeline-Phasen.
2. **Qwen-Subagenten** – fuehren konkrete Extraktion, Review und LLM-as-a-judge
   aus. duerfen freizuegig eingesetzt werden.
3. **Dokumenttyp-spezifische Extraktoren** – ein universelles Script reicht
   nicht. Verschiedene Gesetzentypen (z.B. KriftV, Chemikalienrecht,
   Abfallrecht, Gefahrgut) haben unterschiedliche Strukturen und brauchen
   eigene Extraktionsstrategien.
4. **Review-Pflicht** – jedes Extraktionsergebnis wird geprueft (durch Qwen oder
   human), Issues werden dokumentiert, Verbesserung bei Bedarf.
5. **Konsistenzueber hunderte PDFs** – Qwen-Kontexte muessen praezise genug sein,
   damit verschiedene Instanzen einheitliche Ergebnisse produzieren.

## Phasen

### Phase 1: Normtext-Schicht (aktuell)

- Extrahiere reine Normstrukturen aus PDFs:
  - Gesetzestitel, Ausfertigungsdatum, Inkrafttreten
  - Gliederung: Teil > Kapitel > Abschnitt > Paragraph > Absatz > Satz
  - Anlagen, Anhange, Tabellen
  - AVV-Codes (wo zutreffend)
- Ausgabe: strukturiertes JSON gemass `qwen_context/output_contract.md`
- Keine Semantik, keine Concept-Extraktion, keine Relationen – nur der
  dokumentierte Normtext mit Seitenbezug und Hierarchy.

### Phase 1: Verbessertes Extraktions- und Review-Verfahren

- **Raw-Extraktion ist kein vertrauenswuerdiges Korpus.** Der Extraktor erzeugt
  rohe, pro Dokument isolierte Ausgabedaten. Diese dienen nur als Eingabe fuer
  den Review, nicht als finale Quelle.
- **Pipeline baut kleine Review-Shards.** Pro Shard werden extrahierte Einheiten
  (Paragraphen, Absaetze, Tabellenzeilen) zusammen mit dem Original-Seiten-
  text als Beleg gebündelt.
- **Qwen-Review-Agenten pruefen Shards, keine ganzen PDFs oder riesigen JSONs.**
  Jede Review-Aufgabe ist auf einen kleinen, ueberschaubaren Kontext begrenzt.
- **Nur akzeptierte oder korrigierte Shards wandern in `reviewed_extraction`.**
  Shards mit `needs_revision` oder `rejected` bleiben aus dem ueberprueften
  Korpus heraus.
- **Review-Agenten verbessern keine Extraktor-Skripte direkt.** Sie produzieren
  typisierte Issues. Implementation-Agenten verbessern die Extraktoren spaeter
  auf Basis gebuendlter Issue-Muster.
- **Reviewer-Aufrufe muessen Usage/Timings mitschreiben, wenn sie wieder
  aktiviert werden.** Lokale Reviewer sind vorerst pausiert, weil sie bei
  dichten Tabellen-Shards widerspruechliche Befunde geliefert haben. Ein
  neuer Reviewer-Runner muss Telemetrie pro Call dauerhaft dokumentieren.
- **Empfohlene Shard-Grossen:**
  - Normaler Rechtstext: 2–5 Seiten oder 5–15 Einheiten
  - Dichte Tabellen/Kataloge: 1–2 Seiten
  - Aufgaben konkret und abgrenzbar halten.
- **Reihenfolge der Pipeline-Schritte:**
  `raw_extraction` -> `review_shards` -> `reviews` -> `reviewed_extraction` ->
  spaeterer Graph-Import.

### Phase 2: Fachobjekte / Semantik (spaeter)

- Gezieltere Extraktion von Fachbegriffen, Stoffen, Produkten,
  Zuständigkeiten, Fristen, Schwellenwerten.
- Stabile kanonische IDs fuer Referenzen (z.B. `DE/EnWG/§5/Abs2`).
- Semantische Typisierung der extrahierten Objekte.

### Phase 3: Beziehungen (spaeter)

- Verknuepfung von Normstellen untereinander (Verweisungen, Verweise-ausserhalb).
- Resolver-Schicht: LLM extrahiert Referenzen, Resolver mappt auf kanonische IDs.
- Unresolved References als Platzhalter oder Queue.

## Wichtige Dateien

- `PROJECT_PLAN.md` – dieser Plan
- `STATUS.md` – aktueller Stand
- `CONTENT_NODE_MODEL.md` – Modell der deterministisch extrahierten Content-Nodes
- `REFERENCE_RELATION_MODEL.md` – Modell fuer deterministische Normverweise
- `qwen_context/` – Anweisungen fuer Qwen-Subagenten
- `pdf-neo4j-pipeline/` – bestehende Pipeline-Skripte (python)
- `abfall_pdfs/`, `gefahrgut_pdfs/` – PDF-Quellkorpus

## Qwen-Kontexte

| Datei | Zweck |
|---|---|
| `qwen_context/README.md` | Übersicht aller Qwen-Kontexte |
| `qwen_context/normtext_extraction_brief.md` | Anweisungen fuer Normtext-Extraktion |
| `qwen_context/review_brief.md` | Anweisungen fuer Review von Extraktionen |
| `qwen_context/output_contract.md` | JSON-Konzept fuer Extraktionsausgabe |
