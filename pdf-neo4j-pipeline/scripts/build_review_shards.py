#!/usr/bin/env python3
"""Build review shards from normtext raw JSON output.

Usage:
    python scripts/build_review_shards.py \\
        --input output/pilot/normtext_pilot.json \\
        --document-source 01_KrWG.pdf \\
        --out-dir output/pilot/shards \\
        --pages-per-shard 3
"""

import argparse
import hashlib
import json
import os
import sys


def sha256_str(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def find_document(documents, source_pdf):
    basename = os.path.basename(source_pdf)
    for doc in documents:
        if doc.get("source_pdf") == basename or doc.get("source_pdf") == source_pdf:
            return doc
    return None


def pages_in_range(pages, page_range):
    start = page_range["start"]
    end = page_range["end"]
    return [p for p in pages if start <= p["page_number"] <= end]


def item_page_range(item):
    if not isinstance(item, dict):
        return None
    page_range = item.get("page_range")
    if isinstance(page_range, dict) and "start" in page_range and "end" in page_range:
        return {"start": int(page_range["start"]), "end": int(page_range["end"])}
    if "page_number" in item:
        page_number = int(item["page_number"])
        return {"start": page_number, "end": page_number}
    if "page" in item:
        page_number = int(item["page"])
        return {"start": page_number, "end": page_number}
    return None


def items_intersecting_page_range(items, page_range):
    start = page_range["start"]
    end = page_range["end"]
    result = []
    for item in items:
        item_range = item_page_range(item)
        if item_range is None:
            continue
        if item_range["start"] <= end and item_range["end"] >= start:
            result.append(item)
    return result


def collect_page_numbers(page_range):
    return set(range(page_range["start"], page_range["end"] + 1))


def collect_evidence_page_numbers(page_range, *item_lists):
    page_numbers = collect_page_numbers(page_range)
    for items in item_lists:
        for item in items:
            item_range = item_page_range(item)
            if item_range is not None:
                page_numbers.update(collect_page_numbers(item_range))
    return sorted(page_numbers)


def page_range_from_numbers(page_numbers):
    if not page_numbers:
        return None
    return {"start": min(page_numbers), "end": max(page_numbers)}


def pages_by_numbers(pages, page_numbers):
    wanted = set(page_numbers)
    return [p for p in pages if p["page_number"] in wanted]


def page_ref_for_shard(page):
    return {
        "page_id": page.get("page_id"),
        "page_number": page.get("page_number"),
        "pdf_page_index": page.get("pdf_page_index"),
        "text_sha256": page.get("text_sha256"),
        "text_extraction_backend": page.get("text_extraction_backend"),
    }


def page_refs_for_shard(pages):
    return [page_ref_for_shard(page) for page in pages]


def load_page_ref(page_ref, base_dir):
    if "text" in page_ref:
        return page_ref
    path = page_ref.get("path")
    if not path:
        return page_ref
    page_path = path if os.path.isabs(path) else os.path.join(base_dir, path)
    with open(page_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_document_pages(doc, base_dir):
    refs = doc.get("page_refs") or doc.get("pages", [])
    return [load_page_ref(page_ref, base_dir) for page_ref in refs]


def compact_unit_for_review(unit, included_unit_ids):
    compact = dict(unit)
    child_ids = compact.get("child_unit_ids")
    if isinstance(child_ids, list):
        compact["child_unit_ids"] = [uid for uid in child_ids if uid in included_unit_ids]
        omitted = [uid for uid in child_ids if uid not in included_unit_ids]
        if omitted:
            compact["omitted_child_unit_ids_outside_shard"] = omitted
    text = compact.pop("text", "")
    if text:
        compact["text_sha256"] = compact.get("text_sha256") or sha256_str(text)
        compact["text_char_count"] = len(text)
        compact["text_preview"] = text[:500]
    return compact


def compact_chunk_for_review(chunk):
    compact = dict(chunk)
    evidence_text = compact.pop("evidence_text", "")
    if evidence_text:
        compact["evidence_text_sha256"] = sha256_str(evidence_text)
        compact["evidence_text_char_count"] = len(evidence_text)
    return compact


def compact_items_for_review(units, chunks):
    included_unit_ids = {u.get("unit_id") for u in units}
    return [compact_unit_for_review(u, included_unit_ids) for u in units], [compact_chunk_for_review(c) for c in chunks]


def is_range_covered_by_pages(page_range, page_numbers):
    if page_range is None:
        return False
    wanted = set(page_numbers)
    return all(page in wanted for page in range(page_range["start"], page_range["end"] + 1))


def add_context_children_with_covered_evidence(units, chunks, units_list, chunks_list, evidence_page_numbers):
    """Include direct/recursive child units if their full pages are already loaded.

    Reviewers see complete evidence pages. If a parent unit is in scope and one
    of its children is fully visible on those evidence pages, including the child
    prevents false "missing unit" findings without expanding the evidence window.
    """
    included_unit_ids = {unit.get("unit_id") for unit in units_list}
    included_chunk_ids = {chunk.get("chunk_id") for chunk in chunks_list}
    changed = True

    while changed:
        changed = False
        for unit in units:
            unit_id = unit.get("unit_id")
            if unit_id in included_unit_ids:
                continue
            if unit.get("parent_unit_id") not in included_unit_ids:
                continue
            if not is_range_covered_by_pages(item_page_range(unit), evidence_page_numbers):
                continue
            units_list.append(unit)
            included_unit_ids.add(unit_id)
            changed = True

    for chunk in chunks:
        chunk_id = chunk.get("chunk_id")
        if chunk_id in included_chunk_ids:
            continue
        if chunk.get("unit_id") not in included_unit_ids:
            continue
        if not is_range_covered_by_pages(item_page_range(chunk), evidence_page_numbers):
            continue
        chunks_list.append(chunk)
        included_chunk_ids.add(chunk_id)


def build_review_instructions(shard_id, page_range):
    return {
        "shard_id": shard_id,
        "page_range": page_range,
        "instructions": [
            "Verify completeness for the nominal page_range: ensure all text from those PDF pages is represented or explicitly explained.",
            "Use evidence_pages for checking cross-page units/chunks; evidence_page_range may be wider than the nominal page_range.",
            "Verify page ranges: confirm that page_range/evidence_page_range match the actual PDF pages.",
            "Verify boundaries: check that unit and chunk boundaries align correctly with paragraph structure.",
            "For tables: verify row order, continuation rows, merged-line descriptions, footnotes, and page-spanning tables.",
            "Flag invented units: mark any structural_units that appear to be hallucinated or not present in the source PDF.",
            "Flag missing units: identify any paragraph headings, subsections, or waste codes that should be present but are missing.",
            "Decide: for each shard, choose one of: accepted, needs_revision, rejected.",
        ],
        "status": "pending_review",
    }


def collect_table_items(doc):
    tables = []
    table_rows = []
    for key in ("tables", "extracted_tables"):
        value = doc.get(key, [])
        if isinstance(value, list):
            tables.extend(value)
    for key in ("table_rows", "extracted_table_rows"):
        value = doc.get(key, [])
        if isinstance(value, list):
            table_rows.extend(value)
    return tables, table_rows


def build_shard(
    doc,
    page_range,
    nominal_pages,
    evidence_pages,
    evidence_page_range,
    units_list,
    chunks_list,
    tables_list,
    table_rows_list,
    issues_list,
    shard_num,
):
    shard_id = "shard_{:03d}".format(shard_num)
    review_instructions = build_review_instructions(shard_id, page_range)

    compact_units, compact_chunks = compact_items_for_review(units_list, chunks_list)

    shard = {
        "schema_version": "1.0.0-draft",
        "phase": "normtext_review_shard",
        "shard_id": shard_id,
        "document_id": doc["document_id"],
        "source_pdf": doc["source_pdf"],
        "page_range": page_range,
        "nominal_page_range": page_range,
        "evidence_page_range": evidence_page_range,
        "evidence_pages": evidence_pages,
        "nominal_pages": page_refs_for_shard(nominal_pages),
        "nominal_page_numbers": [page["page_number"] for page in nominal_pages],
        "evidence_page_numbers": [page["page_number"] for page in evidence_pages],
        "extracted_units": compact_units,
        "extracted_chunks": compact_chunks,
        "extracted_tables": tables_list,
        "extracted_table_rows": table_rows_list,
        "extraction_issues": issues_list,
        "review_instructions": review_instructions,
    }
    return shard


def main():
    parser = argparse.ArgumentParser(
        description="Build review shards from normtext raw JSON output."
    )
    parser.add_argument("--input", required=True, help="Path to raw normtext JSON.")
    parser.add_argument("--document-source", required=True, help="PDF source filename to shard.")
    parser.add_argument("--out-dir", required=True, help="Output directory for shards.")
    parser.add_argument("--pages-per-shard", type=int, default=3, help="Number of pages per shard.")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    doc = find_document(raw_data.get("documents", []), args.document_source)
    if doc is None:
        print(
            "ERROR: No document found matching source '{}' in {}".format(
                args.document_source, args.input
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    pages = load_document_pages(doc, os.path.dirname(args.input) or ".")
    units = doc.get("structural_units", [])
    chunks = doc.get("chunks", [])
    tables, table_rows = collect_table_items(doc)
    extraction_issues = raw_data.get("extraction_issues", [])
    total_pages = len(pages)

    if total_pages == 0:
        print("ERROR: Document has no pages.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.out_dir, exist_ok=True)

    shards_info = []
    shard_num = 1

    for start_page in range(0, total_pages, args.pages_per_shard):
        end_page = min(start_page + args.pages_per_shard - 1, total_pages - 1)
        page_range = {
            "start": pages[start_page]["page_number"],
            "end": pages[end_page]["page_number"],
        }

        nominal_pages = pages_in_range(pages, page_range)
        units_list = items_intersecting_page_range(units, page_range)
        chunks_list = items_intersecting_page_range(chunks, page_range)
        tables_list = items_intersecting_page_range(tables, page_range)
        table_rows_list = items_intersecting_page_range(table_rows, page_range)
        issues_list = items_intersecting_page_range(extraction_issues, page_range)
        evidence_page_numbers = collect_evidence_page_numbers(
            page_range, chunks_list, tables_list, table_rows_list, issues_list
        )
        add_context_children_with_covered_evidence(
            units, chunks, units_list, chunks_list, evidence_page_numbers
        )
        evidence_page_range = page_range_from_numbers(evidence_page_numbers)
        evidence_pages = pages_by_numbers(pages, evidence_page_numbers)

        shard = build_shard(
            doc,
            page_range,
            nominal_pages,
            evidence_pages,
            evidence_page_range,
            units_list,
            chunks_list,
            tables_list,
            table_rows_list,
            issues_list,
            shard_num,
        )

        shard_filename = "shard_{:03d}.json".format(shard_num)
        shard_path = os.path.join(args.out_dir, shard_filename)
        with open(shard_path, "w", encoding="utf-8") as f:
            json.dump(shard, f, indent=2, ensure_ascii=False)

        shards_info.append(
            {
                "shard_id": shard["shard_id"],
                "filename": shard_filename,
                "page_range": page_range,
                "nominal_page_range": page_range,
                "evidence_page_range": evidence_page_range,
                "num_pages": len(evidence_pages),
                "num_nominal_pages": len(nominal_pages),
                "num_evidence_pages": len(evidence_pages),
                "num_units": len(units_list),
                "num_chunks": len(chunks_list),
                "num_tables": len(tables_list),
                "num_table_rows": len(table_rows_list),
                "num_extraction_issues": len(issues_list),
            }
        )

        shard_num += 1

    index_data = {
        "schema_version": "1.0.0-draft",
        "phase": "normtext_review_shard_index",
        "document_id": doc["document_id"],
        "source_pdf": doc["source_pdf"],
        "total_shards": len(shards_info),
        "total_pages": total_pages,
        "pages_per_shard": args.pages_per_shard,
        "shards": shards_info,
    }

    index_path = os.path.join(args.out_dir, "index.json")
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, indent=2, ensure_ascii=False)

    print(
        "Created {} shards for document '{}' in {}".format(
            len(shards_info), doc["source_pdf"], args.out_dir
        )
    )


if __name__ == "__main__":
    main()
