#!/usr/bin/env python3
"""Classify downloaded Gesetze-im-Internet PDFs into legal domains via OpenRouter.

The script does not parse norm structure. It extracts a compact document context
from each PDF (metadata, first pages, and heading-like lines) and asks an LLM to
choose categories from ../legal_domain_taxonomy.json.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - handled at runtime
    fitz = None


ROOT = Path(__file__).resolve().parents[2]
PIPELINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PDF_DIR = ROOT / "gesetze_im_internet_pdfs"
DEFAULT_MANIFEST = DEFAULT_PDF_DIR / "manifest.json"
DEFAULT_TAXONOMY = ROOT / "legal_domain_taxonomy.json"
DEFAULT_OUT_DIR = PIPELINE_ROOT / "output" / "domain_classification"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


SPACE_RE = re.compile(r"[ \t]+")
BLANK_RE = re.compile(r"\n{3,}")
FOOTER_RE = re.compile(
    r"(?:-\s*Seite\s+\d+\s*(?:von\s+\d+)?\s*-|"
    r"Ein Service des Bundesministeriums der Justiz.*?www\.gesetze-im-internet\.de)",
    re.IGNORECASE,
)
HEADING_RE = re.compile(
    r"^(?:"
    r"§{1,2}\s*\d+[a-zA-Z]?(?:\s|$).{0,180}|"
    r"Artikel\s+\d+[a-zA-Z]?(?:\s|$).{0,180}|"
    r"Art\.\s*\d+[a-zA-Z]?(?:\s|$).{0,180}|"
    r"Abschnitt\s+\d+[a-zA-Z]?(?:\s|$).{0,180}|"
    r"Unterabschnitt\s+\d+[a-zA-Z]?(?:\s|$).{0,180}|"
    r"Teil\s+\d+[a-zA-Z]?(?:\s|$).{0,180}|"
    r"Kapitel\s+\d+[a-zA-Z]?(?:\s|$).{0,180}|"
    r"Anlage\s+(?:\d+[a-zA-Z]?|[IVXLC]+)(?:\s|$).{0,180}|"
    r"Anhang\s+(?:\d+[a-zA-Z]?|[IVXLC]+)(?:\s|$).{0,180}"
    r")$",
    re.IGNORECASE,
)


def normalize_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = FOOTER_RE.sub("", text)
    lines = [SPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return BLANK_RE.sub("\n\n", text).strip()


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n[TRUNCATED]"


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def compact_taxonomy(taxonomy: dict[str, Any]) -> list[str]:
    lines = []
    for domain, subdomains in taxonomy["domains"].items():
        lines.append("{}: {}".format(domain, ", ".join(subdomains)))
    return lines


def first_lines(text: str, max_lines: int = 16) -> list[str]:
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) < 3:
            continue
        lines.append(line[:220])
        if len(lines) >= max_lines:
            break
    return lines


def heading_lines(page_texts: list[str], max_headings: int) -> list[str]:
    seen: set[str] = set()
    headings: list[str] = []
    for text in page_texts:
        for raw_line in text.splitlines():
            line = SPACE_RE.sub(" ", raw_line).strip()
            if not line or len(line) > 220:
                continue
            if HEADING_RE.match(line) and line not in seen:
                seen.add(line)
                headings.append(line)
                if len(headings) >= max_headings:
                    return headings
    return headings


def extract_pdf_context(
    pdf_path: Path,
    first_pages: int,
    heading_pages: int,
    max_first_pages_chars: int,
    max_headings: int,
) -> dict[str, Any]:
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed. Install pymupdf in the local venv.")

    doc = fitz.open(str(pdf_path))
    try:
        page_count = doc.page_count
        scan_pages = min(page_count, max(first_pages, heading_pages))
        page_texts = []
        for idx in range(scan_pages):
            page_texts.append(normalize_text(doc.load_page(idx).get_text("text") or ""))
    finally:
        doc.close()

    first_page_texts = page_texts[: min(first_pages, len(page_texts))]
    first_pages_text = "\n\n--- PAGE BREAK ---\n\n".join(first_page_texts)
    headings = heading_lines(page_texts[: min(heading_pages, len(page_texts))], max_headings)

    return {
        "pdf_page_count": page_count,
        "first_pages_text": truncate(first_pages_text, max_first_pages_chars),
        "heading_like_lines": headings,
    }


def document_key(entry: dict[str, Any]) -> str:
    rel = entry.get("relative_file_path") or ""
    return Path(rel).stem


def build_document_payload(
    entry: dict[str, Any],
    pdf_dir: Path,
    first_pages: int,
    heading_pages: int,
    max_first_pages_chars: int,
    max_headings: int,
) -> dict[str, Any]:
    pdf_path = pdf_dir / entry["relative_file_path"]
    context = extract_pdf_context(
        pdf_path,
        first_pages=first_pages,
        heading_pages=heading_pages,
        max_first_pages_chars=max_first_pages_chars,
        max_headings=max_headings,
    )
    return {
        "document_key": document_key(entry),
        "manifest": {
            "ordinal": entry.get("ordinal"),
            "category": entry.get("category"),
            "category_index": entry.get("category_index"),
            "title": entry.get("title"),
            "detail_url": entry.get("detail_url"),
            "pdf_url": entry.get("pdf_url"),
            "relative_file_path": entry.get("relative_file_path"),
            "bytes": entry.get("bytes"),
            "sha256": entry.get("sha256"),
        },
        "pdf_context": context,
    }


def system_prompt() -> str:
    return (
        "You classify German federal legal documents into a fixed legal-domain taxonomy. "
        "Use only the allowed domain and subdomain values. Classify the document as a whole. "
        "Prefer the best substantive legal field. Use sonstiges_unclassified/sonstiges only "
        "when the document is not a standalone substantive norm text or when the available "
        "title/text is genuinely insufficient. Return one valid JSON object only. Do not use "
        "markdown, comments, trailing commas, prose outside JSON, or null message content."
    )


def user_prompt(taxonomy: dict[str, Any], documents: list[dict[str, Any]]) -> str:
    requested_schema = {
        "classifications": [
            {
                "document_key": "...",
                "primary_legal_domain": "...",
                "primary_legal_subdomain": "...",
                "legal_domains": ["..."],
                "legal_subdomains": ["..."],
                "legal_topics": ["max_4_short_snake_case_tags"],
                "classification_confidence": 0.0,
                "classification_source": "llm_draft",
                "needs_review": True,
                "reason": "German, max 120 chars",
            }
        ]
    }
    payload = {
        "task": "Classify each document into the taxonomy. Return one classification per document.",
        "rules": [
            "primary_legal_domain must be a key from taxonomy.domains.",
            "primary_legal_subdomain must be listed under the chosen primary_legal_domain.",
            "Never combine a subdomain with the wrong domain. If no specific subdomain fits, use 'sonstiges' under the chosen domain.",
            "legal_domains should usually contain only the primary domain.",
            "Use multiple legal_domains only for genuine cross-cutting documents.",
            "legal_subdomains must contain subdomains that exist under at least one selected legal_domains value.",
            "classification_confidence is 0.0 to 1.0.",
            "Set needs_review true when confidence < 0.75 or when using sonstiges_unclassified.",
            "Use classification_source='llm_draft'.",
            "Keep reason under 120 characters.",
            "Do not invent categories.",
            "Output must be parseable JSON with double-quoted keys and strings.",
        ],
        "allowed_taxonomy_lines": compact_taxonomy(taxonomy),
        "output_schema": requested_schema,
        "documents": documents,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def call_openrouter(
    api_key: str,
    model: str,
    taxonomy: dict[str, Any],
    documents: list[dict[str, Any]],
    temperature: float,
    max_tokens: int,
    timeout: int,
    use_response_format: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": user_prompt(taxonomy, documents)},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if use_response_format:
        body["response_format"] = {"type": "json_object"}

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://local.codex/graph-database",
            "X-Title": "Legal Domain Classification Pipeline",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {body_text[:1000]}") from exc

    content = response["choices"][0]["message"].get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("OpenRouter response message content was empty")
    parsed = parse_json_response(content)
    return parsed, response.get("usage") or {}


def parse_json_response(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start : end + 1])
        raise


def extract_classifications(parsed: Any) -> list[dict[str, Any]]:
    if isinstance(parsed, list):
        candidates = parsed
    elif isinstance(parsed, dict):
        candidates = None
        for key in ("classifications", "classification", "results", "documents", "items"):
            value = parsed.get(key)
            if isinstance(value, list):
                candidates = value
                break
        if candidates is None:
            list_values = [value for value in parsed.values() if isinstance(value, list)]
            dict_lists = [
                value
                for value in list_values
                if all(isinstance(item, dict) and "document_key" in item for item in value)
            ]
            candidates = dict_lists[0] if dict_lists else None
    else:
        candidates = None

    if not isinstance(candidates, list):
        raise RuntimeError("response does not contain a classification list")
    classifications = [item for item in candidates if isinstance(item, dict)]
    if not classifications:
        raise RuntimeError("classification list was empty or malformed")
    return classifications


def load_done_keys(jsonl_path: Path) -> set[str]:
    done = set()
    if not jsonl_path.exists():
        return done
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") == "ok" and row.get("document_key"):
                done.add(row["document_key"])
    return done


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def append_jsonl_row(path: Path, row: dict[str, Any]) -> None:
    append_jsonl(path, [row])


def validate_classification(
    classification: dict[str, Any],
    expected_key: str,
    taxonomy: dict[str, Any],
) -> list[str]:
    errors = []
    domains = taxonomy["domains"]
    if classification.get("document_key") != expected_key:
        errors.append("document_key mismatch")
    domain = classification.get("primary_legal_domain")
    subdomain = classification.get("primary_legal_subdomain")
    if domain not in domains:
        errors.append(f"invalid primary_legal_domain: {domain}")
    elif subdomain not in domains[domain]:
        errors.append(f"invalid primary_legal_subdomain for {domain}: {subdomain}")
    confidence = classification.get("classification_confidence")
    if not isinstance(confidence, (int, float)) or not (0 <= confidence <= 1):
        errors.append("classification_confidence must be a number between 0 and 1")
    if classification.get("classification_source") != "llm_draft":
        errors.append("classification_source must be llm_draft")
    if not isinstance(classification.get("legal_domains"), list):
        errors.append("legal_domains must be a list")
    if not isinstance(classification.get("legal_subdomains"), list):
        errors.append("legal_subdomains must be a list")
    if not isinstance(classification.get("legal_topics"), list):
        errors.append("legal_topics must be a list")
    return errors


def normalize_classification(classification: dict[str, Any]) -> dict[str, Any]:
    confidence = classification.get("classification_confidence")
    if isinstance(confidence, str):
        try:
            confidence = float(confidence)
        except ValueError:
            confidence = 0.0
    classification["classification_confidence"] = confidence
    classification.setdefault("legal_domains", [classification.get("primary_legal_domain")])
    classification.setdefault("legal_subdomains", [classification.get("primary_legal_subdomain")])
    classification.setdefault("legal_topics", [])
    classification.setdefault("classification_source", "llm_draft")
    if "needs_review" not in classification:
        classification["needs_review"] = bool(
            not isinstance(confidence, (int, float))
            or confidence < 0.75
            or classification.get("primary_legal_domain") == "sonstiges_unclassified"
        )
    classification.setdefault("reason", "")
    return classification


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def select_entries(manifest: dict[str, Any], offset: int, limit: int | None) -> list[dict[str, Any]]:
    entries = [
        entry
        for entry in manifest.get("entries", [])
        if entry.get("status") in {"ok", "ok_existing"} and entry.get("relative_file_path")
    ]
    if offset:
        entries = entries[offset:]
    if limit is not None:
        entries = entries[:limit]
    return entries


def build_report(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    latest_rows_by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("document_key")
        if key:
            latest_rows_by_key[key] = row
    current_rows = list(latest_rows_by_key.values())
    ok_rows = [row for row in current_rows if row.get("status") == "ok"]
    error_rows = [row for row in current_rows if row.get("status") != "ok"]
    review_rows = [row for row in ok_rows if row.get("classification", {}).get("needs_review")]
    domain_counts: dict[str, int] = {}
    for row in ok_rows:
        domain = row.get("classification", {}).get("primary_legal_domain", "unknown")
        domain_counts[domain] = domain_counts.get(domain, 0) + 1

    lines = [
        "# Legal Domain Classification Report",
        "",
        f"- Generated: `{summary['generated_at']}`",
        f"- Model: `{summary['model']}`",
        f"- Input entries selected: `{summary['selected_entries']}`",
        f"- Current unique documents in output: `{len(current_rows)}`",
        f"- Classified ok: `{len(ok_rows)}`",
        f"- Errors: `{len(error_rows)}`",
        f"- Needs review: `{len(review_rows)}`",
        f"- Prompt tokens: `{summary.get('prompt_tokens', 0)}`",
        f"- Completion tokens: `{summary.get('completion_tokens', 0)}`",
        f"- Total tokens: `{summary.get('total_tokens', 0)}`",
        "",
        "## Domain Counts",
        "",
    ]
    for domain, count in sorted(domain_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- `{domain}`: `{count}`")
    lines.extend(["", "## Needs Review", ""])
    for row in review_rows[:200]:
        cls = row["classification"]
        lines.append(
            "- `{}` `{}` / `{}` confidence `{}`: {}".format(
                row["document_key"],
                cls.get("primary_legal_domain"),
                cls.get("primary_legal_subdomain"),
                cls.get("classification_confidence"),
                cls.get("reason", ""),
            )
        )
    if len(review_rows) > 200:
        lines.append(f"- ... {len(review_rows) - 200} more")
    lines.extend(["", "## Errors", ""])
    if not error_rows:
        lines.append("No errors.")
    for row in error_rows[:200]:
        lines.append(f"- `{row.get('document_key')}`: {row.get('error')}")
    if len(error_rows) > 200:
        lines.append(f"- ... {len(error_rows) - 200} more")
    lines.append("")
    return "\n".join(lines)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def usage_totals_from_jsonl(path: Path) -> dict[str, int]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if not path.exists():
        return totals
    for row in read_jsonl(path):
        if row.get("status") != "ok":
            continue
        for key in totals:
            totals[key] += int(row.get(key) or 0)
    return totals


def latest_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("document_key")
        if key:
            latest_by_key[key] = row
    return list(latest_by_key.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--model", default="minimax/minimax-m3")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--first-pages", type=int, default=2)
    parser.add_argument("--heading-pages", type=int, default=8)
    parser.add_argument("--max-first-pages-chars", type=int, default=9000)
    parser.add_argument("--max-headings", type=int, default=24)
    parser.add_argument("--max-tokens", type=int, default=5000)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--only-missing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-response-format", action="store_true")
    args = parser.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY")

    manifest = load_json(args.manifest)
    taxonomy = load_json(args.taxonomy)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = args.out_dir / "document_domain_classifications.jsonl"
    final_json_path = args.out_dir / "document_domain_classifications_final.json"
    usage_jsonl_path = args.out_dir / "document_domain_classification_batch_usage.jsonl"
    report_path = args.out_dir / "document_domain_classification_report.md"
    summary_path = args.out_dir / "document_domain_classification_summary.json"

    entries = select_entries(manifest, offset=args.offset, limit=args.limit)
    if args.only_missing:
        done = load_done_keys(jsonl_path)
        entries = [entry for entry in entries if document_key(entry) not in done]

    print(f"selected_entries={len(entries)}")
    print(f"output_jsonl={jsonl_path}")
    if not entries:
        all_rows = read_jsonl(jsonl_path)
        final_rows = latest_rows(all_rows)
        summary = {
            "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "model": "mixed",
            "selected_entries": 0,
            "jsonl_path": str(jsonl_path),
            "report_path": str(report_path),
            **usage_totals_from_jsonl(usage_jsonl_path),
        }
        write_json(summary_path, summary)
        write_json(final_json_path, final_rows)
        report_path.write_text(build_report(summary, all_rows), encoding="utf-8")
        print(f"summary={summary_path}")
        print(f"report={report_path}")
        return 0

    if not api_key and not args.dry_run:
        print("OPENROUTER_API_KEY is not set.", file=sys.stderr)
        return 2

    selected_rows: list[dict[str, Any]] = []
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    batch_id_start = int(time.time())

    for batch_number, batch_entries in enumerate(chunks(entries, args.batch_size), start=1):
        batch_id = f"{batch_id_start}-{batch_number:05d}"
        documents = []
        batch_keys = [document_key(entry) for entry in batch_entries]
        try:
            usage = {}
            for entry in batch_entries:
                documents.append(
                    build_document_payload(
                        entry,
                        pdf_dir=args.pdf_dir,
                        first_pages=args.first_pages,
                        heading_pages=args.heading_pages,
                        max_first_pages_chars=args.max_first_pages_chars,
                        max_headings=args.max_headings,
                    )
                )
            if args.dry_run:
                print(user_prompt(taxonomy, documents))
                return 0

            last_error = None
            for attempt in range(args.retries + 1):
                try:
                    parsed, usage = call_openrouter(
                        api_key=api_key or "",
                        model=args.model,
                        taxonomy=taxonomy,
                        documents=documents,
                        temperature=args.temperature,
                        max_tokens=args.max_tokens,
                        timeout=args.timeout,
                        use_response_format=not args.no_response_format,
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt >= args.retries:
                        raise
                    wait_seconds = min(30, 2 ** attempt * 3)
                    print(
                        f"batch {batch_id} attempt {attempt + 1} failed: {exc}; retrying in {wait_seconds}s",
                        flush=True,
                    )
                    time.sleep(wait_seconds)
            else:  # pragma: no cover - loop always breaks or raises
                raise RuntimeError(str(last_error))
            for key in usage_totals:
                usage_totals[key] += int(usage.get(key) or 0)
            append_jsonl_row(
                usage_jsonl_path,
                {
                    "batch_id": batch_id,
                    "batch_number": batch_number,
                    "model": args.model,
                    "document_count": len(batch_entries),
                    "document_keys": batch_keys,
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                    "total_tokens": int(usage.get("total_tokens") or 0),
                    "status": "ok",
                },
            )

            classifications = extract_classifications(parsed)
            by_key = {
                classification.get("document_key"): normalize_classification(classification)
                for classification in classifications
                if isinstance(classification, dict)
            }

            rows = []
            for entry in batch_entries:
                key = document_key(entry)
                classification = by_key.get(key)
                if not classification:
                    rows.append(
                        {
                            "status": "error",
                            "document_key": key,
                            "relative_file_path": entry.get("relative_file_path"),
                            "batch_id": batch_id,
                            "model": args.model,
                            "error": "missing classification in model response",
                        }
                    )
                    continue
                validation_errors = validate_classification(classification, key, taxonomy)
                rows.append(
                    {
                        "status": "ok" if not validation_errors else "invalid",
                        "document_key": key,
                        "relative_file_path": entry.get("relative_file_path"),
                        "title": entry.get("title"),
                        "detail_url": entry.get("detail_url"),
                        "pdf_url": entry.get("pdf_url"),
                        "batch_id": batch_id,
                        "model": args.model,
                        "classification": classification,
                        "validation_errors": validation_errors,
                    }
                )
        except Exception as exc:  # keep batch resumable
            rows = [
                {
                    "status": "error",
                    "document_key": key,
                    "relative_file_path": entry.get("relative_file_path"),
                    "batch_id": batch_id,
                    "model": args.model,
                    "error": str(exc),
                }
                for key, entry in zip(batch_keys, batch_entries)
            ]

        append_jsonl(jsonl_path, rows)
        selected_rows.extend(rows)
        ok_count = sum(1 for row in rows if row.get("status") == "ok")
        print(
            "batch {}/{} ok={}/{} keys={}".format(
                batch_number,
                (len(entries) + args.batch_size - 1) // args.batch_size,
                ok_count,
                len(rows),
                ",".join(batch_keys),
            ),
            flush=True,
        )
        if args.sleep:
            time.sleep(args.sleep)

    all_rows = read_jsonl(jsonl_path)
    final_rows = latest_rows(all_rows)
    cumulative_usage = usage_totals_from_jsonl(usage_jsonl_path)
    summary = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": args.model,
        "selected_entries": len(entries),
        "jsonl_path": str(jsonl_path),
        "report_path": str(report_path),
        **cumulative_usage,
        "last_run_prompt_tokens": usage_totals["prompt_tokens"],
        "last_run_completion_tokens": usage_totals["completion_tokens"],
        "last_run_total_tokens": usage_totals["total_tokens"],
    }
    write_json(summary_path, summary)
    write_json(final_json_path, final_rows)
    report_path.write_text(build_report(summary, all_rows), encoding="utf-8")
    print(f"summary={summary_path}")
    print(f"report={report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
