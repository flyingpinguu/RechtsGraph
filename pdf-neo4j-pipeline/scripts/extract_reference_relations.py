#!/usr/bin/env python3
"""Extract deterministic legal reference relations from a content graph JSON.

This pass reads the graph JSON produced by `extract_content_nodes.py` and adds
non-hierarchical reference relations:

- Internal references such as "§§ 3 und 4" or "Anlage 2 Tabelle 1a"
- External long-name references such as "§ 69 Absatz 1 Nummer 8 des
  Kreislaufwirtschaftsgesetzes"
- External abbreviation references such as "§ 21 ElektroG"

The extractor is deliberately conservative. It only handles explicit textual
references and creates placeholder ReferenceTarget nodes when a target is not
present in the current content graph.
"""

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


SCHEMA_VERSION = "0.1.0"
HSPACE = r"[^\S\r\n]"
QUALIFIER_PATTERN = (
    r"(?:Abs\.|Absatz|Unterabsatz|Unterabs\.|Satz|Halbsatz|"
    r"Nummern?|Nr\.|Ziffer|Buchstabe|Buchst\.)"
)
PARA_BODY_PATTERN = (
    r"\d+[a-z]?"
    rf"(?:{HSPACE}*(?:{QUALIFIER_PATTERN}{HSPACE}*[A-Za-z0-9]+|"
    rf"(?:erster|zweiter){HSPACE}+Halbsatz|"
    rf"(?:und|oder|sowie|,|-|bis){HSPACE}*(?:{QUALIFIER_PATTERN}{HSPACE}*[A-Za-z0-9]+|"
    rf"\d+[a-z]?|[a-z]\b))){{0,30}}"
)
LAW_NAME_PATTERN = r"[A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9\-–\s]{3,100}?"
LAW_TITLE_SUFFIX_PATTERN = (
    r"(?:[Gg]esetzbuch(?:es|s)|[Gg]esetzes|[Vv]erordnung|"
    r"[Oo]rdnung|[Vv]ertrages)\b"
)
LAW_LEADING_MODIFIER_PATTERN = (
    r"(?:(?:bis\s+zum\s+\d{1,2}\.\s+[A-Za-zÄÖÜäöüß]+\s+\d{4}\s+"
    r"|damals\s+|jeweils\s+)?geltenden\s+)?"
)
GENERIC_LAW_NAME_PATTERN = (
    r"(?:Gesetzes|Gesetzbuchs|Gesetzbuches|Verordnung|Ordnung)"
    r"\s+(?:über|gegen|zur|zum|für|zur\s+Durchführung)\s+"
    r"[A-Za-zÄÖÜäöüß0-9\-–,()/\s]{3,180}?"
)
GENERIC_LAW_END_PATTERN = (
    r"(?="
    r"\s+(?:vom\s+\d|in\s+der\s+(?:jeweils\s+)?geltenden\s+Fassung|"
    r"sowie\s+(?:die|der|den)\s+§|und\s+(?:die|der|den)\s+§)"
    r"|\s+sowie\s+gegebenenfalls\b"
    r"|\s+sowie\s+(?:die|der|den|das)\s+[A-ZÄÖÜ]"
    r"|\s+sowie\s+[a-z]\)"
    r"|\s+(?:genannten|festgelegten|geregelten|vorgesehenen|bestimmten|bezeichneten)\b"
    r"|\s+(?:ist|sind|wird|werden|wurde|wurden|gilt|gelten|forscht|"
    r"gerichtlich|entsprechend|genanntes?|vergleichbare|maßgeblich|massgeblich)\b"
    r"|\s+an\s+(?:die|der|den|das)\b"
    r"|\s+oder\s+entsprechenden\b"
    r"|\s+in\s+der\s+Fassung\b"
    r"|\s+im\s+Planfeststellungsbeschluss\b"
    r"|\s+in\s+(?:Spalte|Nummer|Teil|Abschnitt)\b"
    r"|\s+\("
    r"|,\s*\d+[a-z]?\."
    r"|,\s*[a-z]\)"
    r"|,\s+(?:die|der|das|zuletzt|in\s+der)"
    r"|[;.]|$)"
)

PARA_EXTERNAL_LONG_RE = re.compile(
    rf"(?P<mention>§{{1,2}}\s*(?P<body>{PARA_BODY_PATTERN})"
    rf"\s+(?P<article>des|der)\s+{LAW_LEADING_MODIFIER_PATTERN}(?P<law>{LAW_NAME_PATTERN}"
    rf"{LAW_TITLE_SUFFIX_PATTERN}))"
)
PARA_EXTERNAL_GENERIC_RE = re.compile(
    rf"(?P<mention>§{{1,2}}\s*(?P<body>{PARA_BODY_PATTERN})"
    rf"\s+(?P<article>des|der)\s+(?P<law>{GENERIC_LAW_NAME_PATTERN})"
    rf"{GENERIC_LAW_END_PATTERN})"
)
ARTICLE_EXTERNAL_LONG_RE = re.compile(
    rf"(?P<mention>Artikel\s+(?P<body>{PARA_BODY_PATTERN})"
    rf"\s+(?P<article>des|der)\s+{LAW_LEADING_MODIFIER_PATTERN}(?P<law>{LAW_NAME_PATTERN}"
    rf"{LAW_TITLE_SUFFIX_PATTERN}))"
)
ARTICLE_PARA_EXTERNAL_LONG_RE = re.compile(
    rf"(?P<mention>Artikel\s+(?P<article_body>{PARA_BODY_PATTERN})\s+"
    rf"§{{1,2}}\s*(?P<para_body>{PARA_BODY_PATTERN})"
    rf"\s+(?:des|der)\s+(?P<law>{LAW_NAME_PATTERN}"
    rf"{LAW_TITLE_SUFFIX_PATTERN}))"
)
ARTICLE_EXTERNAL_GENERIC_RE = re.compile(
    rf"(?P<mention>Artikel\s+(?P<body>{PARA_BODY_PATTERN})"
    rf"\s+(?P<article>des|der)\s+(?P<law>{GENERIC_LAW_NAME_PATTERN})"
    rf"{GENERIC_LAW_END_PATTERN})"
)
INTERNAL_ARTICLE_RE = re.compile(
    rf"(?P<mention>\bArtikel\s+(?P<body>{PARA_BODY_PATTERN}))"
)
EU_ACT_IDENTIFIER_PATTERN = (
    r"(?:\((?:EG|EU|EWG|EAG)\)\s*(?:Nr\.\s*)?)?"
    r"\d{2,4}/\d{1,4}(?:/(?:EG|EU|EWG|EAG))?"
)
EU_ACT_TYPE_PATTERN = (
    r"(?:Delegierte(?:n)?\s+Verordnung|Durchführungsverordnung|"
    r"Durchfuehrungsverordnung|Verordnung|Richtlinie|Beschluss)"
)
EU_ACT_RE = re.compile(
    rf"\b(?P<act_type>{EU_ACT_TYPE_PATTERN})\s+"
    rf"(?P<identifier>{EU_ACT_IDENTIFIER_PATTERN})"
)
GOVERNING_EU_ACT_RE = re.compile(
    rf"\bgegen\s+(?:die\s+)?"
    rf"(?P<act_type>{EU_ACT_TYPE_PATTERN})\s+"
    rf"(?P<identifier>{EU_ACT_IDENTIFIER_PATTERN})"
    r".{0,1400}?\bverstößt\b",
    flags=re.DOTALL,
)
ARTICLE_EXTERNAL_EU_RE = re.compile(
    rf"(?P<mention>\bArtikel\s+(?P<body>{PARA_BODY_PATTERN})"
    rf"\s*,?\s+(?:des|der)\s+(?P<act_type>{EU_ACT_TYPE_PATTERN})\s+"
    rf"(?P<identifier>{EU_ACT_IDENTIFIER_PATTERN}))"
)
ARTICLE_EXTERNAL_DATED_ACT_RE = re.compile(
    rf"(?P<mention>\bArtikel\s+(?P<body>{PARA_BODY_PATTERN})"
    r"\s+(?:des|der)\s+(?P<act_type>Gesetzes|Verordnung)\s+vom\s+"
    r"(?P<date>\d{1,2}\.\s+[A-Za-zÄÖÜäöüß]+\s+\d{4}))"
)
ARTICLE_EXTERNAL_FRAMEWORK_RE = re.compile(
    rf"(?P<mention>\bArtikel\s+(?P<body>{PARA_BODY_PATTERN})"
    r"\s+(?:des|der)\s+(?P<act_type>Rahmenbeschlusses|Rahmenbeschluss)\s+"
    r"(?P<identifier>\d{4}/\d+/(?:JI|JAI)))"
)
ARTICLE_EXTERNAL_DATED_INSTRUMENT_RE = re.compile(
    rf"(?P<mention>\bArtikel\s+(?P<body>{PARA_BODY_PATTERN})"
    r"\s+(?:des|der)\s+(?P<act_type>Übereinkommens|Uebereinkommens)\s+vom\s+"
    r"(?P<date>\d{1,2}\.\s+[A-Za-zÄÖÜäöüß]+\s+\d{4}))"
)
PARA_EXTERNAL_ABBREV_RE = re.compile(
    rf"(?P<mention>§{{1,2}}\s*(?P<body>{PARA_BODY_PATTERN})"
    r"\s+(?P<abbr>[A-ZÄÖÜ][A-Za-zÄÖÜa-zäöüß0-9]{1,20}(?:G|V|O|GB|BGB|StGB|VZO))\b)"
)
ARTICLE_EXTERNAL_ABBREV_RE = re.compile(
    rf"(?P<mention>\b(?:Artikel|Art\.)\s+(?P<body>{PARA_BODY_PATTERN})"
    r"\s+(?P<abbr>[A-ZÄÖÜ][A-Za-zÄÖÜa-zäöüß0-9]{1,20}(?:G|V|O|GB|BGB|StGB|VZO))\b)"
)
INTERNAL_PARA_RE = re.compile(
    rf"(?P<mention>§{{1,2}}\s*(?P<body>{PARA_BODY_PATTERN}))"
)
CONTEXTUAL_SUBSECTION_RE = re.compile(
    r"(?P<mention>\b(?:Abs\.|Absatz)\s+(?P<num>\d+[a-z]?)\b)"
)
INTERNAL_ANNEX_RE = re.compile(
    rf"(?P<mention>\b(?P<annex_kind>Anlagen?|Anhang|Anhänge|Anhaenge)\s+(?P<body>\d+[a-z]?"
    rf"(?:{HSPACE}*(?:und|oder|,|-|bis){HSPACE}*\d+[a-z]?)*"
    rf")"
    rf"(?:\s+Tabelle\s+(?P<table_body>\d+[a-z]?"
    rf"(?:{HSPACE}*(?:und|oder|,|-|bis){HSPACE}*(?:Tabelle{HSPACE}+)?\d+[a-z]?)*))?)"
)
ANNEX_DETAIL_PATTERN = (
    r"(?:\s+(?:Nummern?|Nr\.|Teil|Abschnitt|Spalten?)\s+"
    r"\d+(?:\.\d+)*(?:[a-z])?"
    r"(?:\s*(?:und|oder|bis|,)\s*\d+(?:\.\d+)*(?:[a-z])?)?)*"
)
ANNEX_EXTERNAL_ABBREV_RE = re.compile(
    rf"(?P<mention>\b(?P<annex_kind>Anlagen?|Anhang|Anhänge|Anhaenge)\s+"
    rf"(?P<body>\d+[a-z]?)"
    rf"(?:\s+Tabelle\s+(?P<table_body>\d+[a-z]?))?"
    rf"{ANNEX_DETAIL_PATTERN}"
    r"\s+(?P<abbr>[A-ZÄÖÜ][A-Za-zÄÖÜa-zäöüß0-9]{1,20}(?:G|V|O|GB|BGB|StGB|VZO))\b)"
)
ANNEX_EXTERNAL_LONG_RE = re.compile(
    rf"(?P<mention>\b(?P<annex_kind>Anlagen?|Anhang|Anhänge|Anhaenge)\s+"
    rf"(?P<body>\d+[a-z]?)"
    rf"(?:\s+Tabelle\s+(?P<table_body>\d+[a-z]?))?"
    rf"{ANNEX_DETAIL_PATTERN}"
    rf"\s+(?P<article>des|der)\s+(?P<law>{LAW_NAME_PATTERN}"
    rf"{LAW_TITLE_SUFFIX_PATTERN}))"
)
ANNEX_EXTERNAL_GENERIC_RE = re.compile(
    rf"(?P<mention>\b(?P<annex_kind>Anlagen?|Anhang|Anhänge|Anhaenge)\s+"
    rf"(?P<body>\d+[a-z]?)"
    rf"(?:\s+Tabelle\s+(?P<table_body>\d+[a-z]?))?"
    rf"{ANNEX_DETAIL_PATTERN}"
    rf"\s+(?P<article>des|der)\s+(?P<law>{GENERIC_LAW_NAME_PATTERN})"
    rf"{GENERIC_LAW_END_PATTERN})"
)

CONTEXT_LAW_NAME_PATTERN = (
    rf"(?:{LAW_NAME_PATTERN}"
    r"(?:[Gg]esetz|[Vv]erordnung|[Oo]rdnung|[Gg]esetzbuch)"
    r"|(?:Gesetz|Verordnung|Ordnung)"
    r"\s+(?:über|gegen|zur|zum|für|zur\s+Durchführung)\s+"
    r"[A-Za-zÄÖÜäöüß0-9\-–,()/\s]{3,180}?)"
)
LAW_SECTION_CONTEXT_RE = re.compile(
    rf"\b(?:aus|nach)\s+(?:dem|der)\s+"
    rf"(?P<law>{CONTEXT_LAW_NAME_PATTERN})\s*:"
)
LAW_FORWARD_SCOPE_RE = re.compile(
    rf"\b(?:das|dem|der)\s+(?P<law>{CONTEXT_LAW_NAME_PATTERN})\s*,?\s+"
    r"mit\s+Ausnahme\s+(?:von|der)\s+"
)
LIST_ITEM_HEADING_RE = re.compile(r"(?m)^\s*\d+[a-z]?\.\s+")

EXTERNAL_SCOPE_BRIDGE_RE = re.compile(
    r"^\s*(?:"
    r"[,–-]\s*|"
    r"(?:des|der|die|den)\s*|"
    r"(?:und|oder|sowie)(?:\s+(?:des|der|die|den))?(?:\s+nach)?\s*|"
    r"(?:jeweils\s+)?(?:auch\s+)?in\s+Verbindung\s+mit\s*|"
    r"(?:und|oder)?\s*(?:die|eine|einer|ein)\s+"
    r"(?:Genehmigung|Anordnung|Zulassung(?:\s+vorzeitigen\s+Beginns)?|"
    r"Vorbescheid|Plangenehmigung|Planfeststellung)\s+nach\s*|"
    r"von\s+der\s+Erlaubnispflicht\s+nach\s*"
    r")*$",
    re.IGNORECASE,
)

QUALIFIER_RE = re.compile(
    r"\b(?P<kind>Abs\.|Absatz\b|Unterabsatz\b|Unterabs\.|Satz\b|Halbsatz\b|"
    r"Nummern?\b|Nr\.|Ziffer\b|Buchstabe\b|Buchst\.)\s*(?P<num>[A-Za-z0-9]+)"
)
ABS_QUALIFIER_RE = re.compile(r"\b(?:Abs\.|Absatz)\s*(?P<num>\d+[a-z]?)\b")
LOWER_THAN_ABS_RE = re.compile(
    r"\b(?:Unterabsatz|Unterabs\.|Satz|Halbsatz|Nummern?|Nr\.|Ziffer|"
    r"Buchstabe|Buchst\.)\b"
)
CONNECTED_NUM_RE = re.compile(
    r"\b(?P<connector>und|oder|sowie|bis)\s*(?:(?:Abs\.|Absatz)\s*)?(?P<num>\d+[a-z]?)\b|"
    r"(?P<comma>,|-)\s*(?:(?:Abs\.|Absatz)\s*)?(?P<comma_num>\d+[a-z]?)\b"
)
MULTI_NUM_RE = re.compile(r"\d+[a-z]?")

LAW_GENITIVE_ENDINGS = (
    ("gesetzbuches", "gesetzbuch"),
    ("gesetzbuchs", "gesetzbuch"),
    ("gesetzes", "gesetz"),
    ("vertrages", "vertrag"),
    ("verordnung", "verordnung"),
    ("ordnung", "ordnung"),
)
LAW_GENITIVE_PREFIXES = (
    ("gesetzbuches ", "gesetzbuch "),
    ("gesetzbuchs ", "gesetzbuch "),
    ("gesetzes ", "gesetz "),
)


def slugify(value: str) -> str:
    value = (value or "").lower()
    replacements = {
        "§": "para",
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_")


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(part) for part in parts)
    return "{}_{}".format(prefix, hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16])


def normalize_law_name(law_name: str) -> str:
    cleaned = re.sub(r"\s+", " ", law_name or "").strip()
    cleaned = re.sub(
        r"^Bürgerlichen\s+Gesetzbuch",
        "Bürgerliches Gesetzbuch",
        cleaned,
    )
    lower = cleaned.lower()
    for prefix, replacement in LAW_GENITIVE_PREFIXES:
        if lower.startswith(prefix):
            cleaned = replacement + cleaned[len(prefix):]
            lower = cleaned.lower()
            break
    for suffix, replacement in LAW_GENITIVE_ENDINGS:
        if lower.endswith(suffix):
            cleaned = cleaned[: -len(suffix)] + replacement
            break
    return slugify(cleaned)


def plausible_law_name(law_name: str) -> bool:
    cleaned = re.sub(r"\s+", " ", law_name or "").strip()
    lower = cleaned.lower()
    if not cleaned or len(cleaned.split()) > 18:
        return False
    if re.match(r"^artikels?\b", lower):
        return False
    clause_markers = (
        r"\binnerhalb\s+eines?\b",
        r"\bnach\s+dem\s+inkrafttreten\b",
        r"\btaeter\s+als\s+amtstraeger\b",
        r"\bzur\s+mitwirkung\s+bei\s+dem\b",
        r"\b(?:ist|sind|wird|werden|wurde|wurden|gilt|gelten)\s+"
        r"(?:anzuwenden|angeordnet|beruecksichtigt|berücksichtigt)\b",
    )
    return not any(re.search(pattern, lower) for pattern in clause_markers)


def normalize_abbreviation(abbr: str) -> str:
    return slugify(abbr)


def eu_act_key(act_type: str, identifier: str) -> str:
    normalized = (act_type or "").lower()
    if "verordnung" in normalized:
        act_type = "Verordnung"
    elif "richtlinie" in normalized:
        act_type = "Richtlinie"
    elif "beschluss" in normalized:
        act_type = "Beschluss"
    return slugify("{} {}".format(act_type or "", identifier or ""))


def dated_act_key(act_type: str, date_text: str) -> str:
    normalized_type = {
        "gesetzes": "gesetz",
        "verordnung": "verordnung",
        "übereinkommens": "übereinkommen",
        "uebereinkommens": "uebereinkommen",
    }.get((act_type or "").lower(), (act_type or "").lower())
    return slugify("{} vom {}".format(normalized_type, date_text or ""))


def named_act_key(act_type: str, identifier: str) -> str:
    normalized_type = {
        "rahmenbeschlusses": "rahmenbeschluss",
        "rahmenbeschluss": "rahmenbeschluss",
    }.get((act_type or "").lower(), (act_type or "").lower())
    return slugify("{} {}".format(normalized_type, identifier or ""))


def clean_citation_text(citation: Optional[str]) -> str:
    cleaned = re.sub(r"\s+", " ", citation or "").strip()
    return cleaned.strip('"').strip()


def strip_trailing_footnote_markers(value: Optional[str]) -> str:
    return re.sub(
        r"(?:\s*\[[^\[\]\r\n]+\])+\s*$",
        "",
        value or "",
    ).strip()


def title_aliases_from_citation(citation: Optional[str]) -> Set[str]:
    """Return conservative document title aliases from a full legal citation."""
    cleaned = clean_citation_text(citation)
    aliases: Set[str] = set()
    if not cleaned:
        return aliases
    aliases.add(cleaned)
    cut_patterns = (
        r"\s+in\s+der\s+Fassung\b",
        r"\s+in\s+der\s+im\s+Bundesgesetzblatt\b",
        r",\s+d(?:as|ie|er)\s+zuletzt\b",
        r",\s+zuletzt\b",
        r"\s+vom\s+\d{1,2}\.\s+[A-ZÄÖÜa-zäöüß]+\s+\d{4}\b",
    )
    for pattern in cut_patterns:
        match = re.search(pattern, cleaned)
        if match and match.start() > 5:
            aliases.add(cleaned[: match.start()].strip())
    for match in re.finditer(r"\(([^()]{4,120})\)", cleaned):
        inside = match.group(1).strip()
        if " - " in inside or " – " in inside:
            short_title = re.split(r"\s+[–-]\s+", inside, 1)[0].strip()
            if len(short_title) > 3:
                aliases.add(short_title)
    return aliases


def leading_compound_title_aliases(title: Optional[str]) -> Set[str]:
    cleaned = clean_citation_text(title)
    match = re.match(
        r"^(?P<name>[A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]*"
        r"(?:gesetz|verordnung|ordnung|gesetzbuch))"
        r"\s+(?:für|zur|zum|über|gegen)\b",
        cleaned,
        flags=re.IGNORECASE,
    )
    return {match.group("name")} if match else set()


def title_article_aliases(title: Optional[str]) -> Set[str]:
    """Return title variants that omit the first definite article."""
    normalized = slugify(strip_trailing_footnote_markers(title))
    aliases = set()
    for prefix in (
        "verordnung_ueber_die_",
        "verordnung_ueber_das_",
        "gesetz_ueber_die_",
        "gesetz_ueber_das_",
    ):
        if normalized.startswith(prefix):
            aliases.add(
                prefix.rsplit("_", 2)[0] + "_" + normalized[len(prefix):]
            )
    return aliases


def source_pdf_aliases(source_pdf: Optional[str]) -> Set[str]:
    stem = re.sub(r"\.pdf$", "", source_pdf or "", flags=re.IGNORECASE).strip()
    aliases = {stem} if stem else set()
    stripped = re.sub(r"^\d+[_-]", "", stem)
    if stripped and stripped != stem:
        aliases.add(stripped)
    return aliases


def source_xml_aliases(
    source_xml: Optional[str],
    source_zip: Optional[str] = None,
) -> Set[str]:
    """Return document-like names from XML members and their package paths."""
    aliases: Set[str] = set()
    generic_names = {"xml", "xml.zip", "download", "download.zip"}
    for source in (source_xml, source_zip):
        normalized = re.sub(r"[?#].*$", "", source or "").replace("\\", "/").strip("/")
        if not normalized:
            continue
        parts = [part for part in normalized.split("/") if part]
        candidates = [parts[-1]]
        if len(parts) > 1 and parts[-1].lower() in generic_names:
            candidates.append(parts[-2])
        for candidate in candidates:
            stem = candidate.strip()
            previous = None
            while stem and stem != previous:
                previous = stem
                stem = re.sub(r"\.(?:xml|zip)$", "", stem, flags=re.IGNORECASE)
            if not stem or stem.lower() in {"xml", "download"}:
                continue
            aliases.add(stem)
            stripped = re.sub(r"^\d+[_-]", "", stem)
            if stripped and stripped != stem:
                aliases.add(stripped)
    return aliases


def document_part_from_target_key(global_key: str) -> str:
    for marker in ("_para_", "_art_", "_anlage_", "_anhang_"):
        if marker in global_key:
            return global_key.split(marker, 1)[0]
    return global_key


def token_for_qualifier(kind: str) -> str:
    normalized = kind.replace(".", "").lower()
    if normalized == "abs" or normalized == "absatz":
        return "abs"
    if normalized == "satz":
        return "satz"
    if normalized in ("nummer", "nummern", "nr"):
        return "nr"
    if normalized == "buchstabe" or normalized == "buchst":
        return "buchst"
    return slugify(normalized)


def parse_single_para_body(body: str) -> Tuple[str, List[Tuple[str, str]]]:
    body = re.sub(r"\s+", " ", body or "").strip()
    number_match = re.match(r"(?P<num>\d+[a-z]?)", body)
    if not number_match:
        return "", []
    qualifiers = [
        (token_for_qualifier(match.group("kind")), match.group("num"))
        for match in QUALIFIER_RE.finditer(body)
    ]
    return number_match.group("num"), qualifiers


def split_para_body(body: str) -> List[str]:
    """Return referenced paragraph numbers from a simple §/§§ body."""
    body = re.sub(r"\s+", " ", body or "").strip()
    if QUALIFIER_RE.search(body):
        number, _qualifiers = parse_single_para_body(body)
        return [number] if number else []
    head = re.split(r"\b(?:Abs\.|Absatz|Satz|Nummer|Nr\.|Buchstabe|Buchst\.)\b", body, 1)[0]
    matches = list(MULTI_NUM_RE.finditer(head))
    if not matches:
        return MULTI_NUM_RE.findall(body[:8])
    numbers = [matches[0].group(0)]
    previous = matches[0]
    for current in matches[1:]:
        bridge = head[previous.end():current.start()]
        number = current.group(0)
        if re.search(r"(?:\bbis\b|[-–])", bridge):
            for expanded in expand_number_range(previous.group(0), number):
                append_unique(numbers, expanded)
        else:
            append_unique(numbers, number)
        previous = current
    return numbers


def split_table_body(body: Optional[str]) -> List[str]:
    if not body:
        return []
    cleaned = re.sub(r"\bTabelle\b", " ", body, flags=re.IGNORECASE)
    return MULTI_NUM_RE.findall(cleaned)


def annex_key_prefix(kind: str) -> str:
    normalized = slugify(kind)
    if normalized.startswith("anhang") or normalized.startswith("anhaenge"):
        return "anhang"
    return "anlage"


def document_key_from_unit_global_key(global_key: str) -> str:
    for marker in ("_para_", "_anlage_", "_anhang_", "_art_"):
        if marker in global_key:
            return global_key.split(marker, 1)[0]
    return global_key.split("_", 1)[0] if "_" in global_key else global_key


def article_context_from_unit_global_key(global_key: str) -> Optional[str]:
    match = re.match(r"^(?P<context>.+_art_[^_]+)_para_[^_]+(?:_|$)", global_key or "")
    return match.group("context") if match else None


def expand_number_range(start: str, end: str) -> List[str]:
    if not (start.isdigit() and end.isdigit()):
        return [end]
    left = int(start)
    right = int(end)
    if right <= left or right - left > 25:
        return [end]
    return [str(number) for number in range(left + 1, right + 1)]


def append_unique(values: List[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def subsection_numbers_for_body(body: str) -> List[str]:
    body = re.sub(r"\s+", " ", body or "").strip()
    matches = list(ABS_QUALIFIER_RE.finditer(body))
    numbers: List[str] = []
    for idx, match in enumerate(matches):
        current = match.group("num")
        append_unique(numbers, current)
        next_abs_start = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        lower_match = LOWER_THAN_ABS_RE.search(body, match.end(), next_abs_start)
        segment_end = lower_match.start() if lower_match else next_abs_start
        segment = body[match.end():segment_end]
        last_num = current
        for continuation in CONNECTED_NUM_RE.finditer(segment):
            connector = continuation.group("connector") or continuation.group("comma")
            number = continuation.group("num") or continuation.group("comma_num")
            if connector in ("bis", "-"):
                for expanded in expand_number_range(last_num, number):
                    append_unique(numbers, expanded)
                last_num = number
                continue
            append_unique(numbers, number)
            last_num = number
    return numbers


def global_key_for_para(document_key: str, para_num: str, qualifiers: List[Tuple[str, str]]) -> str:
    key = "{}_para_{}".format(document_key, slugify(para_num))
    for kind, num in qualifiers:
        key += "_{}_{}".format(kind, slugify(num))
    return key


def global_keys_for_para_body(document_key: str, prefix: str, body: str) -> List[str]:
    para_num, _qualifiers = parse_single_para_body(body)
    if not para_num:
        return []
    if prefix == "para":
        first_qualifier = QUALIFIER_RE.search(body)
        paragraph_numbers = (
            split_para_body(body[: first_qualifier.start()])
            if first_qualifier
            else split_para_body(body)
        )
        subsection_numbers = subsection_numbers_for_body(body)
        if subsection_numbers:
            target_para_num = paragraph_numbers[-1] if paragraph_numbers else para_num
            plain_para_numbers = paragraph_numbers[:-1]
            keys = [
                "{}_para_{}".format(document_key, slugify(number))
                for number in plain_para_numbers
            ]
            keys.extend(
                "{}_para_{}_abs_{}".format(document_key, slugify(target_para_num), slugify(number))
                for number in subsection_numbers
            )
            return keys
    if prefix == "art":
        first_qualifier = QUALIFIER_RE.search(body)
        article_numbers = split_para_body(
            body[: first_qualifier.start()] if first_qualifier else body
        )
        return [
            "{}_art_{}".format(document_key, slugify(number))
            for number in article_numbers
        ]
    if QUALIFIER_RE.search(body):
        return ["{}_{}_{}".format(document_key, prefix, slugify(para_num))]
    return [
        "{}_{}_{}".format(document_key, prefix, slugify(number))
        for number in split_para_body(body)
    ]


def target_level_from_global_key(global_key: str) -> str:
    root_index, root_marker = last_structural_marker(global_key)
    structural_tail = global_key[root_index:] if root_index >= 0 else ""
    if "_buchst_" in structural_tail:
        return "letter"
    if "_nr_" in structural_tail:
        return "number"
    if "_satz_" in structural_tail:
        return "sentence"
    if "_abs_" in structural_tail:
        return "subsection"
    if "_tabelle_" in structural_tail:
        return "table"
    if root_marker in ("anlage", "anhang"):
        return "annex"
    if root_marker == "art":
        return "article"
    if root_marker == "para":
        return "paragraph"
    return "document"


def last_structural_marker(global_key: str) -> Tuple[int, Optional[str]]:
    candidates = [
        (global_key.rfind("_{}_".format(marker)), marker)
        for marker in ("para", "art", "anlage", "anhang")
    ]
    index, marker = max(candidates, key=lambda item: item[0])
    return (index, marker) if index >= 0 else (-1, None)


def strip_to_nearest_keys(global_key: str) -> Iterable[str]:
    parts = global_key.split("_")
    cut_markers = ("buchst", "nr", "satz", "abs", "tabelle")
    root_index, _root_marker = last_structural_marker(global_key)
    if root_index < 0:
        return
    structural_start = len(global_key[:root_index].split("_"))
    while True:
        found = False
        for marker in cut_markers:
            positions = [
                index
                for index, part in enumerate(parts)
                if part == marker and index >= structural_start
            ]
            if positions:
                idx = positions[-1]
                if idx >= 0:
                    parts = parts[:idx]
                    found = True
                    yield "_".join(parts)
                    break
        if not found:
            break


def modeled_target_key(global_key: str) -> str:
    root_index, _root_marker = last_structural_marker(global_key)
    if root_index < 0:
        return global_key
    for marker in ("buchst", "nr", "satz"):
        marker_index = global_key.find("_{}_".format(marker), root_index)
        if marker_index >= 0:
            return global_key[:marker_index]
    return global_key


def text_without_heading(chunk: Dict[str, Any]) -> Tuple[str, int]:
    """Remove a self heading line while preserving reference-rich annex headings."""
    props = chunk["properties"]
    text = props.get("text") or ""
    lines = text.splitlines()
    if not lines:
        return text, 0
    first = lines[0].strip()
    legal = props.get("legal_citation") or ""
    unit_id = props.get("unit_id") or ""
    if "Anlage" in first or "Anhang" in first:
        return text, 0
    if first.startswith("§") and legal.startswith(first.split()[0]):
        offset = len(lines[0]) + (1 if "\n" in text else 0)
        return "\n".join(lines[1:]), offset
    if unit_id and "_para_" in unit_id and first.startswith("§"):
        offset = len(lines[0]) + (1 if "\n" in text else 0)
        return "\n".join(lines[1:]), offset
    return text, 0


def relationship(rel_type: str, start_id: str, end_id: str, properties: Dict[str, Any]) -> Dict[str, Any]:
    rel_id = stable_id(
        "rel",
        rel_type,
        start_id,
        end_id,
        properties.get("mention_text", ""),
        properties.get("char_start", ""),
        properties.get("target_global_key", ""),
        properties.get("requested_target_global_key", ""),
    )
    return {
        "id": rel_id,
        "type": rel_type,
        "start_node_id": start_id,
        "end_node_id": end_id,
        "properties": properties,
    }


def reference_target_node(global_key: str, props: Dict[str, Any]) -> Dict[str, Any]:
    node_id = "ref_target_{}".format(slugify(global_key))
    properties = {
        "global_key": global_key,
        "status": props.get("resolution_status", "unresolved"),
        "target_document_key": props.get("target_document_key"),
        "target_level": props.get("target_level"),
        "reference_kind": props.get("reference_kind"),
        "resolution_method": props.get("resolution_method"),
        "requested_target_global_key": props.get("requested_target_global_key"),
        "display_name": props.get("normalized_reference") or global_key,
    }
    if props.get("target_title_key"):
        properties["target_title_key"] = props["target_title_key"]
    if props.get("nearest_resolved_target_id"):
        properties["nearest_resolved_target_id"] = props["nearest_resolved_target_id"]
    return {
        "id": node_id,
        "labels": ["ReferenceTarget"],
        "properties": {k: v for k, v in properties.items() if v is not None},
    }


class ReferenceExtractor:
    def __init__(self, graph: Dict[str, Any]):
        self.graph = graph
        self.nodes_by_id = {node["id"]: node for node in graph.get("nodes") or []}
        self.existing_node_ids = set(self.nodes_by_id)
        self.global_to_node_id: Dict[str, str] = {}
        self.document_by_id: Dict[str, Dict[str, Any]] = {}
        self.document_key_by_id: Dict[str, str] = {}
        self.document_alias_to_global_key: Dict[str, str] = {}
        self._document_alias_candidates: Dict[str, Set[Tuple[str, str]]] = {}
        self.unit_by_id: Dict[str, Dict[str, Any]] = {}
        self.chunk_by_id: Dict[str, Dict[str, Any]] = {}
        self.chunks_by_unit_id: Dict[str, List[Dict[str, Any]]] = {}
        self.reference_targets: Dict[str, Dict[str, Any]] = {}
        self.relationship_keys = {
            (rel["type"], rel["start_node_id"], rel["end_node_id"], rel["id"])
            for rel in graph.get("relationships") or []
        }
        self.seen_reference_keys = set()
        self._index_graph()

    def register_document_alias(self, alias: Optional[str], document_global_key: str, document_id: str) -> None:
        if not alias or not document_global_key:
            return
        alias_keys = {
            slugify(alias),
            slugify(strip_trailing_footnote_markers(alias)),
        }
        alias_keys.update(
            alias_key.replace("_", "")
            for alias_key in list(alias_keys)
            if alias_key
        )
        for candidate_alias in alias_keys:
            if candidate_alias:
                self._document_alias_candidates.setdefault(candidate_alias, set()).add(
                    (document_global_key, document_id)
                )

    def _index_graph(self) -> None:
        for node in self.graph.get("nodes") or []:
            props = node.get("properties") or {}
            labels = set(node.get("labels") or [])
            global_key = props.get("global_key")
            if global_key:
                self.global_to_node_id[global_key] = node["id"]
            if "Document" in labels:
                self.document_by_id[node["id"]] = node
                document_global_key = props.get("document_global_key") or props.get("global_key") or props.get("document_key")
                if props.get("document_key"):
                    self.document_key_by_id[node["id"]] = props["document_key"]
                    self.global_to_node_id[props["document_key"]] = node["id"]
                if document_global_key:
                    self.register_document_alias(document_global_key, document_global_key, node["id"])
                for alias_field in (
                    "document_key",
                    "document_global_key",
                    "global_key",
                    "canonical_citation",
                    "abbreviation",
                    "title",
                    "short_title",
                    "full_title",
                    "full_citation",
                    "jurabk",
                    "source_pdf",
                    "source_xml",
                    "source_zip",
                    "base_celex",
                    "consolidated_celex",
                    "citation_aliases",
                ):
                    alias = props.get(alias_field)
                    if isinstance(alias, list):
                        for alias_value in alias:
                            self.register_document_alias(
                                alias_value,
                                document_global_key,
                                node["id"],
                            )
                    elif alias:
                        self.register_document_alias(alias, document_global_key, node["id"])
                for alias in source_pdf_aliases(props.get("source_pdf")):
                    self.register_document_alias(alias, document_global_key, node["id"])
                for alias in source_xml_aliases(
                    props.get("source_xml"),
                    props.get("source_zip"),
                ):
                    self.register_document_alias(alias, document_global_key, node["id"])
                for alias in title_aliases_from_citation(props.get("full_citation")):
                    self.register_document_alias(alias, document_global_key, node["id"])
                metadata_json = props.get("metadata_json")
                if isinstance(metadata_json, str):
                    try:
                        metadata = json.loads(metadata_json)
                    except (TypeError, ValueError):
                        metadata = {}
                    for alias in metadata.get("jurabk") or []:
                        self.register_document_alias(
                            alias,
                            document_global_key,
                            node["id"],
                        )
                for title_field in (
                    "title",
                    "short_title",
                    "full_title",
                    "full_citation",
                ):
                    for alias in leading_compound_title_aliases(
                        props.get(title_field)
                    ):
                        self.register_document_alias(
                            alias,
                            document_global_key,
                            node["id"],
                        )
                    for alias in title_article_aliases(props.get(title_field)):
                        self.register_document_alias(
                            alias,
                            document_global_key,
                            node["id"],
                        )
            if "StructuralUnit" in labels:
                self.unit_by_id[node["id"]] = node
            if "Chunk" in labels:
                self.chunk_by_id[node["id"]] = node
                unit_id = props.get("unit_id")
                if unit_id:
                    self.chunks_by_unit_id.setdefault(unit_id, []).append(node)

        for unit_chunks in self.chunks_by_unit_id.values():
            unit_chunks.sort(
                key=lambda chunk: (
                    (chunk.get("properties") or {}).get("sequence") or 0,
                    chunk["id"],
                )
            )
        # Older XML exports appended a physical table ordinal to otherwise
        # canonical table keys (for example ``..._tabelle_3_4``).  Register
        # the legal citation key as an alias so references to "Tabelle 3"
        # still resolve to the exact table rather than falling back to its
        # parent annex.
        for unit_id, unit in self.unit_by_id.items():
            props = unit.get("properties") or {}
            if props.get("unit_type") != "table":
                continue
            number = str(props.get("number") or "").strip()
            if not number or "unbezeichnet" in number.lower():
                continue
            parent = self.unit_by_id.get(props.get("parent_unit_id") or "")
            parent_global_key = (parent or {}).get("properties", {}).get("global_key")
            if not parent_global_key:
                continue
            semantic_table_key = "{}_tabelle_{}".format(
                parent_global_key,
                slugify(number),
            )
            self.global_to_node_id.setdefault(semantic_table_key, unit_id)
        for alias_key, candidates in self._document_alias_candidates.items():
            if len(candidates) != 1:
                continue
            document_global_key, document_id = next(iter(candidates))
            self.document_alias_to_global_key[alias_key] = document_global_key
            self.global_to_node_id.setdefault(alias_key, document_id)

    def document_key_for_chunk(self, chunk: Dict[str, Any]) -> str:
        props = chunk.get("properties") or {}
        unit = self.unit_by_id.get(props.get("unit_id") or "")
        if unit:
            unit_props = unit.get("properties") or {}
            return (
                unit_props.get("document_global_key")
                or document_key_from_unit_global_key(unit_props.get("global_key", ""))
                or unit_props.get("document_key")
                or ""
            )
        global_key = props.get("global_key") or ""
        return global_key.split("_", 1)[0] if "_" in global_key else ""

    def document_aliases_for_chunk(self, chunk: Dict[str, Any]) -> set:
        aliases = set()
        props = chunk.get("properties") or {}
        unit = self.unit_by_id.get(props.get("unit_id") or "")
        if unit:
            unit_props = unit.get("properties") or {}
            if unit_props.get("document_key"):
                aliases.add(slugify(unit_props["document_key"]))
            if unit_props.get("document_global_key"):
                aliases.add(slugify(unit_props["document_global_key"]))
            document = self.document_by_id.get(unit_props.get("document_id") or "")
            if document:
                doc_props = document.get("properties") or {}
                for field in (
                    "document_key",
                    "document_global_key",
                    "global_key",
                    "canonical_citation",
                    "abbreviation",
                    "title",
                    "short_title",
                    "jurabk",
                    "source_xml",
                    "source_zip",
                ):
                    value = doc_props.get(field)
                    if isinstance(value, list):
                        aliases.update(slugify(alias) for alias in value)
                    elif value:
                        aliases.add(slugify(value))
                aliases.update(
                    slugify(alias)
                    for alias in source_xml_aliases(
                        doc_props.get("source_xml"),
                        doc_props.get("source_zip"),
                    )
                )
        return aliases

    def source_unit_id_for_chunk(self, chunk: Dict[str, Any]) -> str:
        return (chunk.get("properties") or {}).get("unit_id") or ""

    def source_document_id_for_chunk(self, chunk: Dict[str, Any]) -> Optional[str]:
        unit = self.unit_by_id.get(self.source_unit_id_for_chunk(chunk))
        if unit:
            return (unit.get("properties") or {}).get("document_id")
        return None

    def unit_id_for_node(self, node_id: Optional[str]) -> Optional[str]:
        if not node_id:
            return None
        if node_id in self.unit_by_id:
            return node_id
        chunk = self.chunk_by_id.get(node_id)
        if chunk:
            return (chunk.get("properties") or {}).get("unit_id")
        return None

    def document_id_for_unit(self, unit_id: Optional[str]) -> Optional[str]:
        if not unit_id:
            return None
        unit = self.unit_by_id.get(unit_id)
        if unit:
            return (unit.get("properties") or {}).get("document_id")
        return None

    def document_id_for_node(self, node_id: Optional[str]) -> Optional[str]:
        if not node_id:
            return None
        if node_id in self.document_by_id:
            return node_id
        return self.document_id_for_unit(self.unit_id_for_node(node_id))

    def document_id_for_key(self, key: Optional[str]) -> Optional[str]:
        if not key:
            return None
        node_id = self.global_to_node_id.get(key) or self.global_to_node_id.get(slugify(key))
        if node_id in self.document_by_id:
            return node_id
        return None

    def mapped_document_key_for_alias(self, key: Optional[str]) -> Optional[str]:
        normalized = slugify(key or "")
        return (
            self.document_alias_to_global_key.get(normalized)
            or self.document_alias_to_global_key.get(normalized.replace("_", ""))
        )

    def representative_chunk_id_for_unit(self, unit_id: str) -> Optional[str]:
        chunks = [
            chunk
            for chunk in self.chunks_by_unit_id.get(unit_id, [])
            if not (chunk.get("properties") or {}).get("parent_chunk_id")
        ]
        if not chunks:
            return None
        preferred_types = ("paragraph_text", "annex_text", "waste_code_entry", "table_rows")
        for preferred_type in preferred_types:
            for chunk in chunks:
                if (chunk.get("properties") or {}).get("chunk_type") == preferred_type:
                    return chunk["id"]
        return chunks[0]["id"]

    def target_chunk_ids_for_node(self, node_id: str) -> List[str]:
        if node_id in self.chunk_by_id:
            return [node_id]
        if node_id in self.unit_by_id:
            representative_id = self.representative_chunk_id_for_unit(node_id)
            return [representative_id] if representative_id else []
        if node_id in self.reference_targets or node_id.startswith("ref_target_"):
            return [node_id]
        return []

    def target_unit_id_for_reference(self, target_id: str, nearest_id: Optional[str]) -> Optional[str]:
        return self.unit_id_for_node(target_id) or self.unit_id_for_node(nearest_id)

    def document_reference_target_node(self, target_document_key: str, props: Dict[str, Any]) -> Dict[str, Any]:
        global_key = target_document_key
        node_id = "ref_target_{}".format(slugify(global_key))
        properties = {
            "global_key": global_key,
            "status": "unresolved",
            "target_document_key": target_document_key,
            "target_level": "document",
            "reference_kind": props.get("reference_kind"),
            "resolution_method": props.get("resolution_method"),
            "display_name": target_document_key,
        }
        if props.get("target_title_key"):
            properties["target_title_key"] = props["target_title_key"]
        return {
            "id": node_id,
            "labels": ["ReferenceTarget"],
            "properties": {k: v for k, v in properties.items() if v is not None},
        }

    def resolve_target(self, target_global_key: str) -> Tuple[str, str, Optional[str]]:
        candidate_keys = [target_global_key]
        nested_para = re.match(
            r"^(?P<document>.+)_art_[^_]+_para_(?P<tail>.+)$",
            target_global_key,
        )
        if nested_para:
            candidate_keys.append(
                "{}_para_{}".format(
                    nested_para.group("document"),
                    nested_para.group("tail"),
                )
            )
        document_part = document_part_from_target_key(target_global_key)
        mapped_document_key = self.mapped_document_key_for_alias(document_part)
        if mapped_document_key and mapped_document_key != document_part:
            if target_global_key == document_part:
                candidate_keys.append(mapped_document_key)
            elif target_global_key.startswith(document_part):
                candidate_keys.append(mapped_document_key + target_global_key[len(document_part):])
        seen_candidates = set()
        for candidate_key in candidate_keys:
            if candidate_key in seen_candidates:
                continue
            seen_candidates.add(candidate_key)
            exact = self.global_to_node_id.get(candidate_key)
            if exact:
                return exact, "resolved", candidate_key
            for nearest_key in strip_to_nearest_keys(candidate_key):
                nearest = self.global_to_node_id.get(nearest_key)
                if nearest:
                    return nearest, "resolved", nearest_key
        return "ref_target_{}".format(slugify(target_global_key)), "unresolved", None

    def add_reference(
        self,
        source_chunk: Dict[str, Any],
        mention_text: str,
        char_start: int,
        char_end: int,
        target_document_key: str,
        target_global_key: str,
        reference_kind: str,
        normalized_reference: str,
        target_title_key: Optional[str] = None,
    ) -> None:
        target_global_key = modeled_target_key(target_global_key)
        requested_target_global_key = target_global_key
        source_id = source_chunk["id"]
        source_unit_id = self.source_unit_id_for_chunk(source_chunk)
        target_id, status, resolved_target_global_key = self.resolve_target(target_global_key)
        effective_target_global_key = resolved_target_global_key or target_global_key
        if status == "resolved":
            requested_level = target_level_from_global_key(
                requested_target_global_key
            )
            resolved_level = target_level_from_global_key(
                resolved_target_global_key or ""
            )
            resolution_method = (
                "exact"
                if requested_level == resolved_level
                else "nearest_ancestor"
            )
        else:
            mapped_document_key = self.mapped_document_key_for_alias(
                target_document_key
            )
            target_document_present = bool(
                self.document_id_for_key(target_document_key)
                or self.document_id_for_key(mapped_document_key)
            )
            resolution_method = (
                "target_unit_missing"
                if target_document_present
                else "target_document_missing"
            )

        # Skip pure self references introduced by paragraph/table headings.
        if target_id == source_id or target_id == source_unit_id:
            return

        ref_key = (
            source_id,
            requested_target_global_key,
            mention_text,
            char_start,
            char_end,
        )
        if ref_key in self.seen_reference_keys:
            return
        self.seen_reference_keys.add(ref_key)

        props = {
            "reference_id": stable_id(
                "ref",
                source_id,
                requested_target_global_key,
                char_start,
                char_end,
                mention_text,
            ),
            "source_chunk_id": source_id,
            "source_unit_id": source_unit_id,
            "source_document_id": self.source_document_id_for_chunk(source_chunk),
            "mention_text": mention_text,
            "normalized_reference": normalized_reference,
            "reference_kind": reference_kind,
            "target_document_key": target_document_key,
            "target_global_key": effective_target_global_key,
            "requested_target_global_key": requested_target_global_key,
            "resolved_target_global_key": resolved_target_global_key,
            "target_title_key": target_title_key,
            "target_level": target_level_from_global_key(effective_target_global_key),
            "resolution_status": status,
            "resolution_method": resolution_method,
            "char_start": char_start,
            "char_end": char_end,
            "extraction_method": "deterministic_regex_v0.1.0",
        }
        props = {k: v for k, v in props.items() if v is not None}

        if target_id not in self.existing_node_ids:
            node = reference_target_node(effective_target_global_key, props)
            self.reference_targets[node["id"]] = node
            self.existing_node_ids.add(node["id"])

        self.add_refers_to_relationships(source_chunk, target_id, None, props)

    def add_relationship_once(self, rel_type: str, start_id: str, end_id: str, props: Dict[str, Any]) -> None:
        if start_id == end_id:
            return
        rel = relationship(rel_type, start_id, end_id, props)
        rel_key = (rel["type"], rel["start_node_id"], rel["end_node_id"], rel["id"])
        if rel_key not in self.relationship_keys:
            self.graph.setdefault("relationships", []).append(rel)
            self.relationship_keys.add(rel_key)

    def add_refers_to_relationships(
        self,
        source_chunk: Dict[str, Any],
        target_id: str,
        nearest_id: Optional[str],
        props: Dict[str, Any],
    ) -> None:
        source_chunk_id = source_chunk["id"]
        source_unit_id = self.source_unit_id_for_chunk(source_chunk)
        source_document_id = self.source_document_id_for_chunk(source_chunk)

        # Chunk level: prefer Chunk -> Chunk. When a target is only represented
        # as a broad StructuralUnit, link to one representative chunk; otherwise
        # references such as "Anlage 8" explode into dozens of parallel edges.
        # The exact broad target is still preserved by the unit-level rollup.
        chunk_targets = self.target_chunk_ids_for_node(target_id)
        for chunk_target_id in chunk_targets:
            if chunk_target_id == source_chunk_id:
                continue
            self.add_relationship_once("REFERS_TO", source_chunk_id, chunk_target_id, props)

        # Unit level: roll the observed chunk reference up to its source unit and
        # the nearest known target unit. For unknown external targets, keep the
        # exact ReferenceTarget as the target.
        target_unit_id = self.target_unit_id_for_reference(target_id, nearest_id)
        if source_unit_id:
            unit_target_id = target_unit_id
            if not unit_target_id and (target_id in self.reference_targets or target_id.startswith("ref_target_")):
                unit_target_id = target_id
            if unit_target_id and unit_target_id != source_unit_id:
                self.add_relationship_once("REFERS_TO", source_unit_id, unit_target_id, props)

        # Document level: connect documents when the target document is known.
        # For unknown external documents, create/use a document-level
        # ReferenceTarget keyed by the normalized title/abbreviation.
        target_document_id = (
            self.document_id_for_node(target_id)
            or self.document_id_for_node(nearest_id)
            or self.document_id_for_key(props.get("target_document_key"))
        )
        document_target_id = target_document_id
        if not document_target_id and props.get("target_document_key"):
            document_target_id = "ref_target_{}".format(slugify(props["target_document_key"]))
            if document_target_id not in self.existing_node_ids:
                node = self.document_reference_target_node(props["target_document_key"], props)
                self.reference_targets[node["id"]] = node
                self.existing_node_ids.add(node["id"])

        if source_document_id and document_target_id and source_document_id != document_target_id:
            self.add_relationship_once("REFERS_TO", source_document_id, document_target_id, props)

    def extract(self) -> Dict[str, Any]:
        for chunk in list(self.chunk_by_id.values()):
            self.extract_from_chunk(chunk)
        if self.reference_targets:
            self.graph.setdefault("nodes", []).extend(self.reference_targets.values())
            self.nodes_by_id.update(self.reference_targets)
        self.validate_reference_levels()
        self.update_counts()
        self.graph["reference_extraction"] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "method": "deterministic_regex_v0.1.0",
            "reference_target_nodes_added": len(self.reference_targets),
            "refers_to_relationships_added": len(
                [rel for rel in self.graph.get("relationships") or [] if rel.get("type") == "REFERS_TO"]
            ),
        }
        return self.graph

    def validate_reference_levels(self) -> None:
        allowed_targets = {
            "Chunk": {"Chunk", "ReferenceTarget"},
            "StructuralUnit": {"StructuralUnit", "ReferenceTarget"},
            "Document": {"Document", "ReferenceTarget"},
        }
        invalid = []
        for rel in self.graph.get("relationships") or []:
            if rel.get("type") != "REFERS_TO":
                continue
            start = self.nodes_by_id.get(rel.get("start_node_id")) or {}
            end = self.nodes_by_id.get(rel.get("end_node_id")) or {}
            start_labels = set(start.get("labels") or [])
            end_labels = set(end.get("labels") or [])
            start_level = next((label for label in allowed_targets if label in start_labels), None)
            end_level = next((label for label in {"Chunk", "StructuralUnit", "Document", "ReferenceTarget"} if label in end_labels), None)
            if not start_level or end_level not in allowed_targets[start_level]:
                invalid.append(
                    "{}:{} -> {}:{}".format(
                        rel.get("id"),
                        rel.get("start_node_id"),
                        rel.get("end_node_id"),
                        sorted(end_labels),
                    )
                )
        if invalid:
            preview = "; ".join(invalid[:10])
            raise ValueError("Invalid REFERS_TO level mapping: {}".format(preview))

    def extract_from_chunk(self, chunk: Dict[str, Any]) -> None:
        text, offset = text_without_heading(chunk)
        if not text:
            return
        occupied: List[Tuple[int, int]] = []
        occupied.extend(self.extract_external_long(chunk, text, offset))
        occupied.extend(self.extract_external_acts(chunk, text, offset, occupied))
        occupied.extend(self.extract_external_abbrev(chunk, text, offset, occupied))
        occupied.extend(self.extract_external_context(chunk, text, offset, occupied))
        occupied.extend(self.extract_internal_article(chunk, text, offset, occupied))
        occupied.extend(self.extract_internal_annex(chunk, text, offset, occupied))
        occupied.extend(self.extract_internal_para(chunk, text, offset, occupied))
        self.extract_contextual_subsection(chunk, text, offset, occupied)

    def overlaps(self, start: int, end: int, spans: List[Tuple[int, int]]) -> bool:
        return any(start < span_end and end > span_start for span_start, span_end in spans)

    def external_target_keys(
        self,
        match: re.Match,
        law_key: str,
        prefix: str,
    ) -> List[str]:
        body = match.group("body")
        if prefix != "annex":
            return global_keys_for_para_body(law_key, prefix, body)
        annex_prefix = annex_key_prefix(match.group("annex_kind"))
        annex_numbers = split_para_body(body)
        table_numbers = split_table_body(match.groupdict().get("table_body"))
        if table_numbers:
            return [
                "{}_{}_{}_tabelle_{}".format(
                    law_key,
                    annex_prefix,
                    slugify(annex_number),
                    slugify(table_number),
                )
                for annex_number in annex_numbers
                for table_number in table_numbers
            ]
        return [
            "{}_{}_{}".format(
                law_key,
                annex_prefix,
                slugify(annex_number),
            )
            for annex_number in annex_numbers
        ]

    def add_external_match_references(
        self,
        chunk: Dict[str, Any],
        match: re.Match,
        offset: int,
        law_key: str,
        prefix: str,
        reference_kind: str = "external_long_name",
        target_title_key: Optional[str] = None,
    ) -> None:
        body = match.group("body")
        for target_key in self.external_target_keys(match, law_key, prefix):
            self.add_reference(
                chunk,
                match.group("mention"),
                offset + match.start(),
                offset + match.end(),
                law_key,
                target_key,
                reference_kind,
                "{} {} {}".format(law_key, prefix, body),
                target_title_key=target_title_key,
            )

    def propagate_external_scope_backward(
        self,
        chunk: Dict[str, Any],
        text: str,
        offset: int,
        anchor: re.Match,
        law_key: str,
        prefix: str,
        occupied: List[Tuple[int, int]],
        reference_kind: str = "external_long_name",
        target_title_key: Optional[str] = None,
    ) -> List[Tuple[int, int]]:
        """Apply a trailing law name to an immediately preceding citation list."""
        if prefix == "para":
            candidate_pattern = INTERNAL_PARA_RE
        elif prefix == "art":
            candidate_pattern = INTERNAL_ARTICLE_RE
        elif prefix == "annex":
            candidate_pattern = INTERNAL_ANNEX_RE
        else:
            return []

        propagated: List[Tuple[int, int]] = []
        current_start = anchor.start()
        candidates = [
            candidate
            for candidate in candidate_pattern.finditer(text)
            if candidate.end() <= current_start
        ]
        for candidate in reversed(candidates):
            if self.overlaps(candidate.start(), candidate.end(), occupied + propagated):
                break
            bridge = text[candidate.end():current_start]
            if not EXTERNAL_SCOPE_BRIDGE_RE.fullmatch(bridge):
                break
            # A repeated genitive article can either continue one external
            # citation list or introduce a separate citation.  Prefer the
            # latter when the preceding target exists in the source document,
            # as in "§ 78 Absatz 2 und des § 5 des VStGB".
            if (
                prefix == "para"
                and re.search(
                    r"\b(?:und|oder|sowie)\s+(?:des|der|die|den)\b",
                    bridge,
                    flags=re.IGNORECASE,
                )
            ):
                source_document_key = self.document_key_for_chunk(chunk)
                internal_candidate_keys = global_keys_for_para_body(
                    source_document_key,
                    "para",
                    candidate.group("body"),
                )
                if any(
                    self.resolve_target(target_key)[1] == "resolved"
                    for target_key in internal_candidate_keys
                ):
                    break
            self.add_external_match_references(
                chunk,
                candidate,
                offset,
                law_key,
                prefix,
                reference_kind=reference_kind,
                target_title_key=target_title_key,
            )
            propagated.append((candidate.start(), candidate.end()))
            current_start = candidate.start()
        return propagated

    def propagate_external_anaphora_forward(
        self,
        chunk: Dict[str, Any],
        text: str,
        offset: int,
        anchor: re.Match,
        law_key: str,
        occupied: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        """Resolve the repeated citation in boilerplate such as ", von denen § …"."""
        window_end = min(len(text), anchor.end() + 450)
        tail = text[anchor.end():window_end]
        anaphora = re.search(
            r"(?:,\s+von\s+denen\s+|"
            r"\bverordnet\b.{0,240}?\bund\s+zu\s+)",
            tail,
            flags=re.DOTALL,
        )
        if not anaphora:
            return []
        scope_start = anchor.end() + anaphora.end()
        match = INTERNAL_PARA_RE.search(text, scope_start, min(len(text), scope_start + 180))
        if not match or text[scope_start:match.start()].strip():
            return []
        if self.overlaps(match.start(), match.end(), occupied):
            return []
        self.add_external_match_references(
            chunk,
            match,
            offset,
            law_key,
            "para",
            target_title_key=law_key,
        )
        return [(match.start(), match.end())]

    def extract_external_long(self, chunk: Dict[str, Any], text: str, offset: int) -> List[Tuple[int, int]]:
        spans = []
        for match in ARTICLE_PARA_EXTERNAL_LONG_RE.finditer(text):
            if not plausible_law_name(match.group("law")):
                continue
            law_key = normalize_law_name(match.group("law"))
            article_numbers = split_para_body(match.group("article_body"))
            target_keys = []
            for article_number in article_numbers:
                article_key = "{}_art_{}".format(
                    law_key,
                    slugify(article_number),
                )
                target_keys.extend(
                    global_keys_for_para_body(
                        article_key,
                        "para",
                        match.group("para_body"),
                    )
                )
            for target_key in target_keys:
                self.add_reference(
                    chunk,
                    match.group("mention"),
                    offset + match.start(),
                    offset + match.end(),
                    law_key,
                    target_key,
                    "external_long_name",
                    target_key,
                    target_title_key=law_key,
                )
            if target_keys:
                spans.append((match.start(), match.end()))
        for pattern, prefix in (
            (PARA_EXTERNAL_GENERIC_RE, "para"),
            (PARA_EXTERNAL_LONG_RE, "para"),
            (ARTICLE_EXTERNAL_GENERIC_RE, "art"),
            (ARTICLE_EXTERNAL_LONG_RE, "art"),
            (ANNEX_EXTERNAL_GENERIC_RE, "annex"),
            (ANNEX_EXTERNAL_LONG_RE, "annex"),
        ):
            for match in pattern.finditer(text):
                if self.overlaps(match.start(), match.end(), spans):
                    continue
                if not plausible_law_name(match.group("law")):
                    continue
                law_key = normalize_law_name(match.group("law"))
                if not self.external_target_keys(match, law_key, prefix):
                    continue
                self.add_external_match_references(
                    chunk,
                    match,
                    offset,
                    law_key,
                    prefix,
                    target_title_key=law_key,
                )
                propagated = self.propagate_external_scope_backward(
                    chunk,
                    text,
                    offset,
                    match,
                    law_key,
                    prefix,
                    spans,
                    target_title_key=law_key,
                )
                spans.extend(propagated)
                spans.append((match.start(), match.end()))
                spans.extend(
                    self.propagate_external_anaphora_forward(
                        chunk,
                        text,
                        offset,
                        match,
                        law_key,
                        spans,
                    )
                )
        return spans

    def extract_external_acts(
        self,
        chunk: Dict[str, Any],
        text: str,
        offset: int,
        occupied: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        spans: List[Tuple[int, int]] = []
        scoped_eu_act_spans: List[Tuple[int, int]] = []
        direct_eu_spans = [
            direct_match.span()
            for direct_match in ARTICLE_EXTERNAL_EU_RE.finditer(text)
        ]
        for pattern, key_builder in (
            (
                ARTICLE_EXTERNAL_EU_RE,
                lambda match: eu_act_key(
                    match.group("act_type"),
                    match.group("identifier"),
                ),
            ),
            (
                ARTICLE_EXTERNAL_DATED_ACT_RE,
                lambda match: dated_act_key(
                    match.group("act_type"),
                    match.group("date"),
                ),
            ),
            (
                ARTICLE_EXTERNAL_FRAMEWORK_RE,
                lambda match: named_act_key(
                    match.group("act_type"),
                    match.group("identifier"),
                ),
            ),
            (
                ARTICLE_EXTERNAL_DATED_INSTRUMENT_RE,
                lambda match: dated_act_key(
                    match.group("act_type"),
                    match.group("date"),
                ),
            ),
        ):
            for match in pattern.finditer(text):
                if self.overlaps(match.start(), match.end(), occupied + spans):
                    continue
                target_document_key = key_builder(match)
                self.add_external_match_references(
                    chunk,
                    match,
                    offset,
                    target_document_key,
                    "art",
                    target_title_key=target_document_key,
                )
                propagated = self.propagate_external_scope_backward(
                    chunk,
                    text,
                    offset,
                    match,
                    target_document_key,
                    "art",
                    occupied + spans,
                    target_title_key=target_document_key,
                )
                spans.extend(propagated)
                spans.append((match.start(), match.end()))
                if pattern is ARTICLE_EXTERNAL_EU_RE:
                    tail = text[match.end():min(len(text), match.end() + 1200)]
                    amended = re.search(
                        r"\bgeändert\s+worden\s+ist,\s+"
                        r"(?:auch\s+)?in\s+Verbindung\s+mit\s+",
                        tail,
                    )
                    if amended:
                        scope_start = match.end() + amended.end()
                        for candidate in INTERNAL_ARTICLE_RE.finditer(
                            text,
                            scope_start,
                        ):
                            if self.overlaps(
                                candidate.start(),
                                candidate.end(),
                                direct_eu_spans,
                            ):
                                continue
                            if self.overlaps(
                                candidate.start(),
                                candidate.end(),
                                occupied + spans,
                            ):
                                continue
                            self.add_external_match_references(
                                chunk,
                                candidate,
                                offset,
                                target_document_key,
                                "art",
                                target_title_key=target_document_key,
                            )
                            spans.append((candidate.start(), candidate.end()))

        governing_acts = list(GOVERNING_EU_ACT_RE.finditer(text))
        for index, governing in enumerate(governing_acts):
            scoped_eu_act_spans.append(governing.span())
            target_document_key = eu_act_key(
                governing.group("act_type"),
                governing.group("identifier"),
            )
            block_end = (
                governing_acts[index + 1].start()
                if index + 1 < len(governing_acts)
                else len(text)
            )
            for match in INTERNAL_ARTICLE_RE.finditer(
                text,
                governing.end(),
                block_end,
            ):
                if self.overlaps(match.start(), match.end(), occupied + spans):
                    continue
                self.add_external_match_references(
                    chunk,
                    match,
                    offset,
                    target_document_key,
                    "art",
                    target_title_key=target_document_key,
                )
                spans.append((match.start(), match.end()))

        eu_mentions = list(EU_ACT_RE.finditer(text))
        for match in INTERNAL_ARTICLE_RE.finditer(text):
            if self.overlaps(match.start(), match.end(), occupied + spans):
                continue
            following = [
                act
                for act in eu_mentions
                if 0 <= act.start() - match.end() <= 400
            ]
            if not following:
                continue
            nearest = min(following, key=lambda act: act.start())
            scoped_eu_act_spans.append(nearest.span())
            target_document_key = eu_act_key(
                nearest.group("act_type"),
                nearest.group("identifier"),
            )
            self.add_external_match_references(
                chunk,
                match,
                offset,
                target_document_key,
                "art",
                target_title_key=target_document_key,
            )
            spans.append((match.start(), match.end()))

        eu_keys = {
            eu_act_key(match.group("act_type"), match.group("identifier"))
            for match in EU_ACT_RE.finditer(text)
        }
        if len(eu_keys) == 1:
            target_document_key = next(iter(eu_keys))
            internal_matches = list(INTERNAL_ARTICLE_RE.finditer(text))
            if internal_matches:
                scoped_eu_act_spans.extend(match.span() for match in eu_mentions)
            for match in internal_matches:
                if self.overlaps(match.start(), match.end(), occupied + spans):
                    continue
                self.add_external_match_references(
                    chunk,
                    match,
                    offset,
                    target_document_key,
                    "art",
                    target_title_key=target_document_key,
                )
                spans.append((match.start(), match.end()))

        # Preserve explicit document-level citations even when no article is
        # named. Article-scoped matches above already occupy their complete
        # mention span and therefore are not duplicated here.
        for match in eu_mentions:
            if self.overlaps(
                match.start(),
                match.end(),
                occupied + spans + scoped_eu_act_spans,
            ):
                continue
            target_document_key = eu_act_key(
                match.group("act_type"),
                match.group("identifier"),
            )
            self.add_reference(
                chunk,
                match.group(0),
                offset + match.start(),
                offset + match.end(),
                target_document_key,
                target_document_key,
                "external",
                target_document_key,
                target_title_key=target_document_key,
            )
            spans.append((match.start(), match.end()))
        return spans

    def extract_external_abbrev(
        self,
        chunk: Dict[str, Any],
        text: str,
        offset: int,
        occupied: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        spans = []
        for pattern, prefix in (
            (PARA_EXTERNAL_ABBREV_RE, "para"),
            (ARTICLE_EXTERNAL_ABBREV_RE, "art"),
            (ANNEX_EXTERNAL_ABBREV_RE, "annex"),
        ):
            for match in pattern.finditer(text):
                if self.overlaps(match.start(), match.end(), occupied + spans):
                    continue
                abbr_key = normalize_abbreviation(match.group("abbr"))
                if abbr_key in self.document_aliases_for_chunk(chunk):
                    continue
                target_document_key = self.document_alias_to_global_key.get(abbr_key, abbr_key)
                if not self.external_target_keys(match, target_document_key, prefix):
                    continue
                self.add_external_match_references(
                    chunk,
                    match,
                    offset,
                    target_document_key,
                    prefix,
                    reference_kind="external_abbreviation",
                )
                propagated = self.propagate_external_scope_backward(
                    chunk,
                    text,
                    offset,
                    match,
                    target_document_key,
                    prefix,
                    occupied + spans,
                    reference_kind="external_abbreviation",
                )
                spans.extend(propagated)
                spans.append((match.start(), match.end()))
        return spans

    def extract_internal_article(
        self,
        chunk: Dict[str, Any],
        text: str,
        offset: int,
        occupied: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        spans: List[Tuple[int, int]] = []
        doc_key = self.document_key_for_chunk(chunk)
        source_id = chunk["id"]
        source_unit_id = self.source_unit_id_for_chunk(chunk)
        for match in INTERNAL_ARTICLE_RE.finditer(text):
            if self.overlaps(match.start(), match.end(), occupied + spans):
                continue
            target_keys = global_keys_for_para_body(
                doc_key,
                "art",
                match.group("body"),
            )
            resolvable_keys = []
            for target_key in target_keys:
                target_id, status, resolved_key = self.resolve_target(target_key)
                if (
                    status == "resolved"
                    and resolved_key == target_key
                    and target_id not in {source_id, source_unit_id}
                ):
                    resolvable_keys.append(target_key)
            if not resolvable_keys:
                continue
            for target_key in resolvable_keys:
                self.add_reference(
                    chunk,
                    match.group("mention"),
                    offset + match.start(),
                    offset + match.end(),
                    doc_key,
                    target_key,
                    "internal",
                    target_key,
                )
            spans.append((match.start(), match.end()))
        return spans

    def extract_external_context(
        self,
        chunk: Dict[str, Any],
        text: str,
        offset: int,
        occupied: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        """Resolve references governed by an explicit preceding law context."""
        spans: List[Tuple[int, int]] = []
        headings = list(LAW_SECTION_CONTEXT_RE.finditer(text))
        for index, heading in enumerate(headings):
            law_key = normalize_law_name(heading.group("law"))
            block_end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            next_list_item = LIST_ITEM_HEADING_RE.search(text, heading.end())
            if next_list_item:
                block_end = min(block_end, next_list_item.start())
            for match in INTERNAL_PARA_RE.finditer(text, heading.end(), block_end):
                if self.overlaps(match.start(), match.end(), occupied + spans):
                    continue
                self.add_external_match_references(
                    chunk,
                    match,
                    offset,
                    law_key,
                    "para",
                    target_title_key=law_key,
                )
                spans.append((match.start(), match.end()))

        for scope in LAW_FORWARD_SCOPE_RE.finditer(text):
            law_key = normalize_law_name(scope.group("law"))
            current_end = scope.end()
            first = True
            for match in INTERNAL_PARA_RE.finditer(text, current_end):
                bridge = text[current_end:match.start()]
                if first:
                    if bridge.strip():
                        break
                    first = False
                elif not EXTERNAL_SCOPE_BRIDGE_RE.fullmatch(bridge):
                    break
                if self.overlaps(match.start(), match.end(), occupied + spans):
                    break
                self.add_external_match_references(
                    chunk,
                    match,
                    offset,
                    law_key,
                    "para",
                    target_title_key=law_key,
                )
                spans.append((match.start(), match.end()))
                current_end = match.end()
        return spans

    def extract_internal_annex(
        self,
        chunk: Dict[str, Any],
        text: str,
        offset: int,
        occupied: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        spans = []
        doc_key = self.document_key_for_chunk(chunk)
        for match in INTERNAL_ANNEX_RE.finditer(text):
            if self.overlaps(match.start(), match.end(), occupied):
                continue
            numbers = split_para_body(match.group("body"))
            annex_prefix = annex_key_prefix(match.group("annex_kind"))
            for number in numbers:
                table_numbers = split_table_body(match.group("table_body"))
                if table_numbers:
                    for table_number in table_numbers:
                        target_key = "{}_{}_{}_tabelle_{}".format(
                            doc_key,
                            annex_prefix,
                            slugify(number),
                            slugify(table_number),
                        )
                        self.add_reference(
                            chunk,
                            match.group("mention"),
                            offset + match.start(),
                            offset + match.end(),
                            doc_key,
                            target_key,
                            "internal",
                            target_key,
                        )
                else:
                    target_key = "{}_{}_{}".format(doc_key, annex_prefix, slugify(number))
                    self.add_reference(
                        chunk,
                        match.group("mention"),
                        offset + match.start(),
                        offset + match.end(),
                        doc_key,
                        target_key,
                        "internal",
                        target_key,
                    )
            spans.append((match.start(), match.end()))
        return spans

    def extract_contextual_subsection(
        self,
        chunk: Dict[str, Any],
        text: str,
        offset: int,
        occupied: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        spans = []
        source_global = (chunk.get("properties") or {}).get("global_key") or ""
        context_match = re.search(
            r"^(?P<doc>.+?)_(?P<kind>para|art)_(?P<num>\d+[a-z]?)",
            source_global,
        )
        if not context_match:
            return spans
        base_key = "{}_{}_{}".format(
            context_match.group("doc"),
            context_match.group("kind"),
            context_match.group("num"),
        )
        for match in CONTEXTUAL_SUBSECTION_RE.finditer(text):
            if self.overlaps(match.start(), match.end(), occupied):
                continue
            # Ignore the leading "(2)" style own subsection marker; this regex
            # catches textual "Absatz 1" references, not parenthesized labels.
            before = text[max(0, match.start() - 2):match.start()]
            if "(" in before:
                continue
            target_key = "{}_abs_{}".format(base_key, slugify(match.group("num")))
            self.add_reference(
                chunk,
                match.group("mention"),
                offset + match.start(),
                offset + match.end(),
                context_match.group("doc"),
                target_key,
                "internal",
                target_key,
            )
            spans.append((match.start(), match.end()))
        return spans

    def extract_internal_para(
        self,
        chunk: Dict[str, Any],
        text: str,
        offset: int,
        occupied: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        spans = []
        doc_key = self.document_key_for_chunk(chunk)
        unit = self.unit_by_id.get(self.source_unit_id_for_chunk(chunk)) or {}
        unit_global_key = (unit.get("properties") or {}).get("global_key") or ""
        target_key_prefix = article_context_from_unit_global_key(unit_global_key) or doc_key
        for match in INTERNAL_PARA_RE.finditer(text):
            if self.overlaps(match.start(), match.end(), occupied):
                continue
            body = match.group("body")
            target_keys = global_keys_for_para_body(target_key_prefix, "para", body)
            if not target_keys:
                continue
            for target_key in target_keys:
                self.add_reference(
                    chunk,
                    match.group("mention"),
                    offset + match.start(),
                    offset + match.end(),
                    doc_key,
                    target_key,
                    "internal",
                    target_key,
                )
            spans.append((match.start(), match.end()))
        return spans

    def update_counts(self) -> None:
        counts_by_label: Dict[str, int] = {}
        for node in self.graph.get("nodes") or []:
            for label in node.get("labels") or []:
                counts_by_label[label] = counts_by_label.get(label, 0) + 1
        counts_by_type: Dict[str, int] = {}
        for rel in self.graph.get("relationships") or []:
            counts_by_type[rel["type"]] = counts_by_type.get(rel["type"], 0) + 1
        self.graph["counts"] = {
            "nodes": len(self.graph.get("nodes") or []),
            "relationships": len(self.graph.get("relationships") or []),
            "nodes_by_label": counts_by_label,
            "relationships_by_type": counts_by_type,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Content graph JSON")
    parser.add_argument("--output", required=True, help="Graph JSON with reference relations")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    graph = load_json(args.input)
    updated = ReferenceExtractor(copy.deepcopy(graph)).extract()
    write_json(args.output, updated)
    counts = updated.get("counts") or {}
    refs = counts.get("relationships_by_type", {}).get("REFERS_TO", 0)
    targets = counts.get("nodes_by_label", {}).get("ReferenceTarget", 0)
    print(
        "Wrote {} with {} REFERS_TO relationships and {} ReferenceTarget nodes".format(
            args.output,
            refs,
            targets,
        )
    )


if __name__ == "__main__":
    main()
