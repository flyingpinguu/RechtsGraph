# Legal Domain Taxonomy

Diese Taxonomie definiert die erlaubten Kategorien fuer die Klassifikation von
`Document`-Nodes. Sie soll von LLM-Klassifikatoren und spaeter auch von
Validierungsskripten verwendet werden.

## Zielmodell

Jedes Dokument soll mindestens eine primaere Zuordnung erhalten:

```json
{
  "primary_legal_domain": "umweltrecht",
  "primary_legal_subdomain": "abfallrecht",
  "legal_domains": ["umweltrecht"],
  "legal_subdomains": ["abfallrecht"],
  "legal_topics": ["kreislaufwirtschaft", "entsorgung"],
  "classification_confidence": 0.86,
  "classification_source": "llm_reviewed"
}
```

Regel: `primary_legal_domain` und `primary_legal_subdomain` sind die beste
Hauptklassifikation. Die Listenfelder duerfen mehrere Werte enthalten, wenn ein
Dokument echte Querschnittsbezuege hat. Bei Unsicherheit eher eine grobe Domain
waehlen und die Subdomain auf `sonstiges` setzen.

## Erlaubte Domains Und Subdomains

- `verfassungsrecht`
  - `staatsorganisation`
  - `grundrechte`
  - `wahlrecht`
  - `parlamentsrecht`
  - `sonstiges`
- `verwaltungsrecht`
  - `allgemeines_verwaltungsrecht`
  - `verwaltungsverfahren`
  - `verwaltungsvollstreckung`
  - `oeffentlicher_dienst`
  - `kommunalrecht`
  - `sonstiges`
- `umweltrecht`
  - `abfallrecht`
  - `immissionsschutzrecht`
  - `wasserrecht`
  - `naturschutzrecht`
  - `bodenschutzrecht`
  - `chemikalienrecht`
  - `klimaschutzrecht`
  - `sonstiges`
- `baurecht_raumordnung`
  - `bauplanungsrecht`
  - `bauordnungsrecht`
  - `raumordnung`
  - `wohnungswesen`
  - `sonstiges`
- `wirtschaftsrecht`
  - `gesellschaftsrecht`
  - `handelsrecht`
  - `gewerberecht`
  - `aussenwirtschaft`
  - `vergaberecht`
  - `finanzmarktaufsicht`
  - `wettbewerbsrecht`
  - `sonstiges`
- `arbeits_sozialrecht`
  - `arbeitsrecht`
  - `arbeitsschutz`
  - `sozialversicherung`
  - `rentenrecht`
  - `pflegeversicherung`
  - `krankenversicherung`
  - `arbeitsfoerderung`
  - `sonstiges`
- `steuer_finanzrecht`
  - `steuerrecht`
  - `zollrecht`
  - `haushaltsrecht`
  - `abgabenrecht`
  - `finanzausgleich`
  - `sonstiges`
- `zivilrecht`
  - `buergerliches_recht`
  - `familienrecht`
  - `erbrecht`
  - `mietrecht`
  - `verbraucherschutz`
  - `sonstiges`
- `strafrecht_ordnungswidrigkeiten`
  - `strafrecht`
  - `nebenstrafrecht`
  - `ordnungswidrigkeitenrecht`
  - `strafvollzug`
  - `sonstiges`
- `prozessrecht_gerichtsverfassung`
  - `zivilprozessrecht`
  - `strafprozessrecht`
  - `verwaltungsprozessrecht`
  - `arbeitsgerichtsverfahren`
  - `sozialgerichtsverfahren`
  - `gerichtsverfassung`
  - `sonstiges`
- `gesundheitsrecht`
  - `arzneimittelrecht`
  - `medizinprodukterecht`
  - `aerzte_approbation`
  - `heilberufe`
  - `krankenhausrecht`
  - `infektionsschutz`
  - `sonstiges`
- `verkehr_transportrecht`
  - `strassenverkehr`
  - `schienenverkehr`
  - `luftverkehr`
  - `schifffahrt`
  - `gefahrgutrecht`
  - `fahrzeugzulassung`
  - `sonstiges`
- `energie_infrastrukturrecht`
  - `energierecht`
  - `telekommunikationsinfrastruktur`
  - `postrecht`
  - `netzinfrastruktur`
  - `sonstiges`
- `agrar_ernaehrungsrecht`
  - `agrarrecht`
  - `lebensmittelrecht`
  - `futtermittelrecht`
  - `tierschutz_tiergesundheit`
  - `forstrecht`
  - `sonstiges`
- `bildung_wissenschaft_kultur`
  - `bildungsrecht`
  - `hochschulrecht`
  - `wissenschaftsrecht`
  - `kulturrecht`
  - `sonstiges`
- `innen_sicherheit_migration`
  - `polizeirecht`
  - `bevoelkerungsschutz`
  - `migrationsrecht`
  - `aufenthaltsrecht`
  - `datenschutz_inneres`
  - `sonstiges`
- `aussen_eu_voelkerrecht`
  - `eu_recht`
  - `voelkerrecht`
  - `internationale_abkommen`
  - `auswaertiger_dienst`
  - `sonstiges`
- `digital_telekommunikation_medien`
  - `datenschutzrecht`
  - `telekommunikationsrecht`
  - `medienrecht`
  - `it_sicherheitsrecht`
  - `digitalregulierung`
  - `sonstiges`
- `verteidigung`
  - `wehrrecht`
  - `beschaffung_ruestung`
  - `soldatenrecht`
  - `sonstiges`
- `sonstiges_unclassified`
  - `sonstiges`

## Klassifikationsregeln Fuer LLMs

1. Klassifiziere das Dokument als Ganzes, nicht einzelne Paragraphen.
2. Nutze Titel, Vollzitat, amtliche Abkuerzung, Eingangsformel und zentrale
   Regelungsgegenstaende.
3. Gib genau eine `primary_legal_domain` und genau eine
   `primary_legal_subdomain` aus.
4. Nutze mehrere `legal_domains` nur bei echten Querschnittsgesetzen, nicht bei
   beiläufigen Verweisen.
5. `legal_topics` sind freie, kurze Schlagwoerter in `snake_case`; sie duerfen
   konkreter sein als die feste Taxonomie.
6. Nutze `sonstiges_unclassified`, wenn das Dokument kein Stammgesetz ist oder
   aus Titel und Text keine belastbare Zuordnung moeglich ist.
7. Setze `classification_confidence` niedrig, wenn nur Titelinformationen
   vorliegen oder mehrere Domains gleich plausibel sind.

## Erlaubte Classification Sources

- `deterministic_title`
- `deterministic_manifest`
- `llm_draft`
- `llm_reviewed`
- `manual`
- `unknown`
