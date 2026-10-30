#!/usr/bin/env python3
"""Build a ranked inventory of documents referenced outside the core corpora.

The relationship export is authoritative for open graph targets.  A second
pass over chunk text captures technical rule sets (ADR, DIN, TRGS, and similar)
whose citations do not look like paragraph/article references and therefore do
not necessarily produce graph edges yet.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_CSV_DIR = PROJECT_ROOT / "output" / "gii_full_chunked_resolved_v2" / "neo4j_csv"
DEFAULT_EURLEX_CSV_DIR = PROJECT_ROOT / "output" / "eurlex_formex_rl" / "neo4j_csv"
DEFAULT_CELLAR_REGISTER = REPO_ROOT / "eurlex_consolidated_de" / "register.json"
DEFAULT_GII_DOCUMENTS = DEFAULT_CSV_DIR / "nodes_009_document.csv"
DEFAULT_OUTPUT = REPO_ROOT / "external_documents" / "reference_inventory.json"

CASE_LAW_RE = re.compile(
    r"(?:^|_)(?:urteil|gericht|rechtsprechung|bverfg|bverwg|bgh|bfh|bag|bsg|"
    r"eugh|egmr|olg|ovg|vg|lsg|sg|arb(?:g|gericht)|entscheidung_des)(?:_|$)",
    re.IGNORECASE,
)
GENERIC_TARGET_RE = re.compile(
    r"^(?:gesetz|verordnung|richtlinie|beschluss|entscheidung)_(?:vom|des|der|zur|zum)$|"
    r"^(?:gesetz|verordnung)_vom_\d|"
    r"^(?:erst(?:e[ns]?)?|zweit(?:e[ns]?)?|dritt(?:e[ns]?)?)_(?:gesetz|verordnung)$|"
    r"^(?:tarifvertrag|geschaeftsordnung|einfuehrungsgesetz)$",
    re.IGNORECASE,
)
EU_KEY_RE = re.compile(
    r"^(?P<kind>verordnung|richtlinie|beschluss|entscheidung)_"
    r"(?P<region>eu|eg|ewg|eag)(?P<nr>_nr)?_(?P<a>\d{2,4})_(?P<b>\d{1,4})$",
    re.IGNORECASE,
)
EU_SUFFIX_KEY_RE = re.compile(
    r"^(?P<kind>verordnung|richtlinie|beschluss|entscheidung)_"
    r"(?P<year>\d{4})_(?P<number>\d{1,4})_(?P<region>eu|eg|ewg|eag)$",
    re.IGNORECASE,
)

TECHNICAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "adr": re.compile(
        r"\bADR\b|Europäische(?:s|n)? Übereinkommen.{0,90}"
        r"Beförderung gefährlicher Güter auf der Straße",
        re.IGNORECASE,
    ),
    "rid": re.compile(
        r"\bRID\b|Ordnung für die internationale Eisenbahnbeförderung gefährlicher Güter",
        re.IGNORECASE,
    ),
    "adn": re.compile(
        r"\bADN\b|Europäische(?:s|n)? Übereinkommen.{0,90}"
        r"Beförderung gefährlicher Güter auf Binnenwasserstraßen",
        re.IGNORECASE,
    ),
    "imdg_code": re.compile(r"\bIMDG(?:-|\s*)Code\b", re.IGNORECASE),
    "icao_technical_instructions": re.compile(
        r"\bICAO(?:-|\s*)(?:TI|T\.I\.|Technical Instructions)\b|\bDoc\s*9284\b",
        re.IGNORECASE,
    ),
    "iata_dgr": re.compile(r"\bIATA(?:-|\s*)DGR\b|Dangerous Goods Regulations", re.IGNORECASE),
    "un_model_regulations": re.compile(
        r"UN(?:-|\s*)Model Regulations|UN(?:-|\s*)Modellvorschriften|"
        r"Recommendations on the Transport of Dangerous Goods",
        re.IGNORECASE,
    ),
    "un_ghs": re.compile(
        r"Globally Harmonized System|Global harmonisiertes System|\bUN(?:-|\s*)GHS\b",
        re.IGNORECASE,
    ),
    "trgs": re.compile(r"\bTRGS\s*\d{3}(?:[-–/]\d+)?\b", re.IGNORECASE),
    "trbs": re.compile(r"\bTRBS\s*\d{3,4}(?:[-–/]\d+)?\b", re.IGNORECASE),
    "trba": re.compile(r"\bTRBA\s*\d{3}(?:[-–/]\d+)?\b", re.IGNORECASE),
    "asr": re.compile(r"\bASR\s*[AV]?\s*\d+(?:\.\d+)?\b", re.IGNORECASE),
    "dguv": re.compile(r"\bDGUV\s+(?:Regel|Information|Grundsatz|Vorschrift)\s+\d[\d-]*", re.IGNORECASE),
    "din": re.compile(r"\bDIN(?:\s+EN|\s+ISO|\s+EN\s+ISO)?\s+\d{3,6}(?:[-:]\d+)*", re.IGNORECASE),
    "iso": re.compile(r"(?<!DIN\s)\bISO\s+\d{3,6}(?:[-:]\d+)*", re.IGNORECASE),
    "vdi": re.compile(r"\bVDI\s+\d{3,5}(?:\s+Blatt\s+\d+)?", re.IGNORECASE),
    "dvgw": re.compile(r"\bDVGW(?:-|\s+)(?:Arbeitsblatt\s+)?[A-Z]\s*\d{2,4}", re.IGNORECASE),
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_cellar_celex(path: Path) -> set[str]:
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(row.get("base_celex") or "").upper()
        for row in data.get("documents") or []
        if row.get("base_celex")
    }


def slugify_document_alias(value: str) -> str:
    value = (value or "").casefold()
    for old, new in {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "§": "para"}.items():
        value = value.replace(old, new)
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_")


def grammatical_law_alias_keys(value: str) -> set[str]:
    """Mirror the resolver's conservative German title-inflection aliases."""

    normalized = slugify_document_alias(value)
    aliases = {normalized} if normalized else set()
    parts = normalized.split("_")
    if len(parts) < 2:
        return aliases
    first, legal_form = parts[0], parts[-1]
    replacement = None
    if legal_form.endswith(("gesetz", "gesetzbuch")):
        if first.endswith("en"):
            replacement = first[:-2] + "es"
        elif first.endswith("es"):
            replacement = first[:-2] + "en"
    elif legal_form.endswith(("verordnung", "ordnung")):
        if first.endswith("en"):
            replacement = first[:-2] + "e"
        elif first.endswith("e"):
            replacement = first[:-1] + "en"
    elif legal_form.endswith("vertrag"):
        if first.endswith("en"):
            replacement = first[:-2] + "er"
        elif first.endswith("er"):
            replacement = first[:-2] + "en"
    if replacement:
        aliases.add("_".join([replacement, *parts[1:]]))
    return aliases


def load_gii_documents(path: Path) -> dict[str, list[dict[str, str]]]:
    aliases: dict[str, list[dict[str, str]]] = defaultdict(list)
    if not path.exists():
        return aliases
    fields = (
        "graph_id",
        "document_global_key",
        "document_key",
        "global_key",
        "abbreviation",
        "canonical_citation",
        "citation_prefix",
        "full_citation",
        "full_title",
        "title",
        "short_title",
        "source_format",
        "source_xml",
        "source_zip",
        "source_pdf",
    )
    with path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            record = {field: row.get(field) or "" for field in fields}
            for field in (
                "document_global_key",
                "document_key",
                "global_key",
                "abbreviation",
                "canonical_citation",
                "citation_prefix",
                "full_citation",
                "full_title",
                "title",
                "short_title",
            ):
                for alias in grammatical_law_alias_keys(record[field]):
                    if alias and record not in aliases[alias]:
                        aliases[alias].append(record)
    return aliases


def celex_from_target_key(target_key: str) -> Optional[str]:
    match = EU_KEY_RE.fullmatch(target_key)
    suffix_match = EU_SUFFIX_KEY_RE.fullmatch(target_key)
    if suffix_match:
        descriptor = {
            "verordnung": "R",
            "richtlinie": "L",
            "beschluss": "D",
            "entscheidung": "D",
        }[suffix_match.group("kind").lower()]
        first = suffix_match.group("year")
        second = suffix_match.group("number")
        if int(first) < 1950 and len(second) == 4 and int(second) >= 1950:
            year, number = second, str(int(first) % 100)
        else:
            year, number = first, second
        return "3{}{}{}".format(
            year,
            descriptor,
            number.zfill(4),
        )
    if not match:
        return None
    kind = match.group("kind").lower()
    descriptor = {
        "verordnung": "R",
        "richtlinie": "L",
        "beschluss": "D",
        "entscheidung": "D",
    }[kind]
    first = match.group("a")
    second = match.group("b")
    region = match.group("region").lower()
    if match.group("nr") and region in {"eg", "ewg", "eag"}:
        year_number = int(second)
        if 1900 <= year_number < 1950:
            year_number += 100
        return "3{}{}{}".format(str(year_number), descriptor, first.zfill(4))
    if match.group("nr") and region == "eu" and int(first) >= 2015:
        return "3{}{}{}".format(first, descriptor, second.zfill(4))
    if match.group("nr") and region == "eu" and 1900 <= int(second) < 1950:
        return "3{}{}{}".format(str(int(second) + 100), descriptor, first.zfill(4))
    first_is_year = len(first) == 4 and 1950 <= int(first) <= dt.date.today().year + 2
    second_is_year = len(second) == 4 and 1950 <= int(second) <= dt.date.today().year + 2
    if first_is_year and (not second_is_year or int(first) >= 2015):
        year, number = first, second
    elif second_is_year:
        year, number = second, first
    elif len(first) == 4:
        year, number = first, second
    elif len(second) == 4:
        year, number = second, first
    else:
        return None
    return "3{}{}{}".format(year, descriptor, number.zfill(4))


def celex_candidates_from_target_key(target_key: str) -> list[str]:
    primary = celex_from_target_key(target_key)
    if not primary:
        return []
    candidates = [primary]
    match = EU_KEY_RE.fullmatch(target_key)
    if (
        match
        and match.group("region").lower() == "eu"
        and int(match.group("a")) >= 2015
        and 1900 <= int(match.group("b")) <= 1999
    ):
        short_number = str(int(match.group("b")) % 100).zfill(4)
        candidates.append(primary[:6] + short_number)
    return list(dict.fromkeys(candidates))


def target_category(
    target_key: str,
    cellar_celex: set[str],
    gii_documents: Optional[Mapping[str, list[dict[str, str]]]] = None,
) -> tuple[str, Optional[str]]:
    if gii_documents and len(gii_documents.get(target_key) or []) == 1:
        return "de_already_in_gii_corpus", None
    if CASE_LAW_RE.search(target_key):
        return "case_law_excluded", None
    celex_candidates = celex_candidates_from_target_key(target_key)
    if celex_candidates:
        for candidate in celex_candidates:
            if candidate in cellar_celex:
                return "eu_already_in_cellar", candidate
        descriptor_alternatives = {
            candidate[:5] + descriptor + candidate[6:]
            for candidate in celex_candidates
            for descriptor in ("R", "L", "D")
            if candidate[:5] + descriptor + candidate[6:] in cellar_celex
        }
        if len(descriptor_alternatives) == 1:
            return "eu_already_in_cellar", next(iter(descriptor_alternatives))
        return "eu_legal_act_missing_from_cellar_register", celex_candidates[0]
    if GENERIC_TARGET_RE.search(target_key):
        return "ambiguous_or_generic", None
    if any(word in target_key for word in ("gesetz", "verordnung", "ordnung", "gesetzbuch")):
        return "german_or_historical_legal_act", None
    if any(word in target_key for word in ("uebereinkommen", "abkommen", "vertrag", "konvention")):
        return "international_agreement", None
    return "unclassified_external_target", None


def iter_chunk_csv_rows(csv_dir: Path) -> Iterable[dict[str, str]]:
    for path in sorted(csv_dir.glob("nodes_*_chunk_*.csv")):
        with path.open(encoding="utf-8", newline="") as stream:
            yield from csv.DictReader(stream)


def build_relationship_inventory(
    paths: Iterable[Path],
    cellar_celex: set[str],
    gii_documents: Mapping[str, list[dict[str, str]]],
    sample_limit: int,
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    source_documents: dict[str, set[str]] = defaultdict(set)
    requested_keys: dict[str, set[str]] = defaultdict(set)
    samples: dict[str, list[dict[str, str]]] = defaultdict(list)
    for path in paths:
        with path.open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                if not row.get("start_node_id", "").startswith("chunk_"):
                    continue
                if row.get("resolution_status") != "unresolved":
                    continue
                target_key = row.get("target_document_key") or "unknown"
                counts[target_key] += 1
                source_documents[target_key].add(row.get("source_document_id") or "")
                requested_keys[target_key].add(row.get("requested_target_global_key") or "")
                sample = {
                    "mention_text": row.get("mention_text") or "",
                    "source_chunk_id": row.get("source_chunk_id") or "",
                    "resolution_method": row.get("resolution_method") or "",
                }
                if sample not in samples[target_key] and len(samples[target_key]) < sample_limit:
                    samples[target_key].append(sample)

    records = []
    for target_key, count in counts.most_common():
        category, celex = target_category(target_key, cellar_celex, gii_documents)
        record = {
            "target_document_key": target_key,
            "category": category,
            "mention_count": count,
            "source_document_count": len(source_documents[target_key] - {""}),
            "requested_target_count": len(requested_keys[target_key] - {""}),
            "samples": samples[target_key],
        }
        if celex:
            record["base_celex"] = celex
        core_candidates = gii_documents.get(target_key) or []
        if category == "de_already_in_gii_corpus" and len(core_candidates) == 1:
            record["core_document"] = core_candidates[0]
        records.append(record)
    return records


def build_technical_inventory(csv_dir: Path, sample_limit: int) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter()
    source_documents: dict[tuple[str, str], set[str]] = defaultdict(set)
    samples: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in iter_chunk_csv_rows(csv_dir):
        text = row.get("text") or ""
        if not text:
            continue
        for family, pattern in TECHNICAL_PATTERNS.items():
            for match in pattern.finditer(text):
                citation = " ".join(match.group(0).split())
                normalized = citation.casefold()
                key = (family, normalized)
                counts[key] += 1
                source_documents[key].add(row.get("document_global_key") or "")
                sample = {
                    "citation": citation,
                    "source_chunk_id": row.get("graph_id") or "",
                    "source_document_key": row.get("document_global_key") or "",
                }
                if sample not in samples[key] and len(samples[key]) < sample_limit:
                    samples[key].append(sample)

    records = []
    for (family, normalized), count in counts.most_common():
        records.append(
            {
                "family": family,
                "normalized_citation": normalized,
                "mention_count": count,
                "source_document_count": len(source_documents[(family, normalized)] - {""}),
                "samples": samples[(family, normalized)],
            }
        )
    return records


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-dir", type=Path, default=DEFAULT_CSV_DIR)
    parser.add_argument(
        "--eurlex-csv-dir",
        type=Path,
        default=DEFAULT_EURLEX_CSV_DIR,
        help="EUR-Lex Neo4j CSV export whose open cross-act references are included.",
    )
    parser.add_argument("--cellar-register", type=Path, default=DEFAULT_CELLAR_REGISTER)
    parser.add_argument("--gii-documents", type=Path, default=DEFAULT_GII_DOCUMENTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sample-limit", type=int, default=3)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    relationship_paths = [args.csv_dir / "relationships_refers_to.csv"]
    eurlex_relationship_path = args.eurlex_csv_dir / "relationships_refers_to.csv"
    if eurlex_relationship_path.is_file():
        relationship_paths.append(eurlex_relationship_path)
    cellar_celex = load_cellar_celex(args.cellar_register)
    gii_documents = load_gii_documents(args.gii_documents)
    relationship_targets = build_relationship_inventory(
        relationship_paths,
        cellar_celex,
        gii_documents,
        args.sample_limit,
    )
    technical_citations = build_technical_inventory(args.csv_dir, args.sample_limit)
    category_counts = Counter(row["category"] for row in relationship_targets)
    result = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "inputs": {
            "csv_dir": str(args.csv_dir.resolve()),
            "eurlex_csv_dir": str(args.eurlex_csv_dir.resolve()),
            "relationship_csvs": [str(path.resolve()) for path in relationship_paths],
            "cellar_register": str(args.cellar_register.resolve()),
            "gii_documents": str(args.gii_documents.resolve()),
        },
        "summary": {
            "unresolved_target_keys": len(relationship_targets),
            "unresolved_chunk_mentions": sum(row["mention_count"] for row in relationship_targets),
            "target_keys_by_category": dict(sorted(category_counts.items())),
            "technical_citation_variants": len(technical_citations),
            "technical_mentions": sum(row["mention_count"] for row in technical_citations),
        },
        "relationship_targets": relationship_targets,
        "technical_citations": technical_citations,
    }
    write_json(args.output, result)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
