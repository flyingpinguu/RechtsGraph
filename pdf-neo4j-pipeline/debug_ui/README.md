# RechtsGraph Agent Debugger

Start aus diesem Verzeichnis mit `npm start`. Die Standardadresse ist
`http://127.0.0.1:4317`; getrennte Instanzen benötigen unterschiedliche Ports.

## Gesprächsführung

Eine Frage startet eine gespeicherte Unterhaltung. Der Agent kann eine normale
Rückfrage stellen; die nächste Nachricht setzt denselben nativen Thread fort.
Das funktioniert vor und während der Recherche. „Neue Unterhaltung“ startet
einen unabhängigen Fall. Modell, Reasoning, Netzwerk und Anweisungen bleiben
innerhalb einer Unterhaltung fest. Alte temporäre Runs können nur gelesen werden.

Jede Nachricht wird als eigener Run gespeichert. `POST /api/runs` beginnt eine
Unterhaltung; `POST /api/runs/:latestRunId/replies` mit `{"prompt":"…"}` setzt
sie fort. `GET /api/conversations/:conversationId` liefert das Transkript.
Parallele Antworten und Verzweigungen werden abgewiesen. Nach Server-Neustart
wird der native Thread mit dem gespeicherten Anweisungsstand fortgesetzt.
Die Anzeige trennt Verbrauch des letzten Modellaufrufs, dieses Turns und des
gesamten Gesprächs; Tokenzahlen sind keine Abrechnung oder Plus-Limit-Messung.

## Anleitung und Vergleich

- `npm start`: Web und RechtsGraph mit der allgemeinen Arbeitsanleitung.
- `npm run start:web-only`: Web mit derselben allgemeinen Arbeitsanleitung.
- `npm run start:baseline`: Web ohne juristische Arbeitsanleitung; minimale
  technische Betriebsanweisungen bleiben vorhanden.

Die [Arbeitsanleitung](LEGAL_RESEARCH_GUIDE.md) bietet Möglichkeiten statt einer
festen Prüfreihenfolge. Hinweise zu Rückfragen gelten auch für einfache Fragen;
umfangreiche Recherche und Rechtsprechung sind keine Pflichtschritte.

Der [Vergleichsrunner](../evals/README.md) kann Anleitung, Werkzeuge oder deren
Gesamteffekt vergleichen. Neben einzelnen Frage-Antwort-Turns unterstützt er
begrenzte Dialogfälle mit vorab festgelegten Sachverhaltsangaben: Rückfragen
werden deterministisch beantwortet und als Teil desselben nativen Threads
bewertet.

Die projektgebundenen juristischen Skills liegen unter
`../../.agents/skills/`. Codex sieht zunächst nur Name und Beschreibung und lädt
die vollständige Anleitung bei passender Aufgabe. RechtsGraph bleibt dabei die
Daten- und Toolschicht; die Skills steuern Recherche, Subsumtion, EU-Prüfung und
Dokumentreview. Das `web_only`-Profil untersagt ihre Verwendung bewusst, damit
der zusätzliche Harness-Effekt getrennt messbar bleibt.

[Acht Fälle aus 2025/2026 mit veröffentlichten Lösungen](../reports/recent_solved_case_candidates_2026-09-11.md)
sind Kandidaten für solche Tests. Falltext und Bewertungslösung müssen getrennt
werden. Ein Verbot, die konkrete Musterlösung nachzuschlagen, gehört in identische
Testbedingungen für alle Varianten, nicht in die allgemeine Arbeitsanleitung.

## Web-Policy und Capability-Check

Jeder neue Run kann `webPolicy` als `optional`, `required` oder `forbidden`
setzen; ohne Angabe gilt `optional`. `required` ergänzt eine turnspezifische
Anweisung und gilt nur dann als erfüllt, wenn das gespeicherte Protokoll einen
erfolgreichen `webSearch`-Call mit mindestens einer HTTP-Quelle enthält. Eine
bloße Behauptung im Antworttext, ein fehlgeschlagener Call oder ein leeres
Suchresultat genügen nicht. `forbidden` schlägt bei jedem beobachteten Webcall
an. Die Auswertung steht als `webPolicyEvaluation` im Run.

Beim Start fragt der Server zusätzlich
`modelProvider/capabilities/read` beim Codex App Server ab. `/api/config` zeigt
das Ergebnis unter `providerCapabilities`; Evaluationsrunner verlassen sich
nicht mehr allein auf den String `web_search="live"`.

## Prüfung

`npm run check && npm test` prüft Syntax, Profile, Vergleichsrunner und Dialog-API
mit simuliertem App Server. Die Tests decken unter anderem Fortsetzung nach
Neustart, gespeicherte Anweisungen, lange Gesprächshistorien und Schutz vor
widersprüchlichen Fortsetzungen ab. Sie belegen keine juristische Antwortqualität.
